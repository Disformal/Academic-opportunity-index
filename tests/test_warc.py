"""WARC evidence (ADR 0006, 0030 §3): one gzip WARC object per fetch, in the test evidence bucket.

The object holds a request record and a response record (or a revisit record when the body is
identical to an earlier capture). The body is stored as fetch() returns it, i.e. decoded, so
headers that described the wire encoding are kept under X-Archive-Orig-*.
"""

import gzip
import io
import uuid
from datetime import UTC, datetime
from urllib.parse import unquote

import pytest
from mypy_boto3_s3 import S3Client
from warcio.statusandheaders import StatusAndHeaders  # type: ignore[import-untyped]
from warcio.warcwriter import WARCWriter  # type: ignore[import-untyped]

from aoi.config import get_settings
from aoi.storage.warc import EarlierCapture, Exchange, read_capture, store_exchange

pytestmark = pytest.mark.integration

FETCHED_AT = datetime(2026, 10, 18, 9, 30, 15, 123456, tzinfo=UTC)
BODY = "<html><h1>Postdoc in Theoretical Physics</h1><p>Θεωρητική Φυσική</p></html>".encode()


def _exchange(**overrides: object) -> Exchange:
    fields: dict[str, object] = {
        "url": "https://inspirehep.net/api/jobs/2911234",
        "request_headers": {"Host": "inspirehep.net", "User-Agent": "aoi/0.1", "Accept": "*/*"},
        "http_version": "HTTP/2",
        "status": 200,
        "response_headers": {"Content-Type": "text/html; charset=utf-8", "ETag": '"abc"'},
        "body": BODY,
        "fetched_at": FETCHED_AT,
    }
    fields.update(overrides)
    return Exchange(**fields)  # type: ignore[arg-type]


def test_a_stored_exchange_reads_back_with_its_status_headers_and_body() -> None:
    fetch_id = uuid.uuid4()

    stored = store_exchange(fetch_id, _exchange())
    capture = read_capture(stored.key)

    assert stored.key == f"warc/2026/10/18/{fetch_id}.warc.gz"
    assert capture.record_type == "response"
    assert capture.record_id == stored.record_id
    assert capture.url == "https://inspirehep.net/api/jobs/2911234"
    assert capture.http_version == "HTTP/2"
    assert capture.status == 200
    assert capture.headers["Content-Type"] == "text/html; charset=utf-8"
    assert capture.headers["ETag"] == '"abc"'
    assert capture.body == BODY
    assert capture.fetched_at == FETCHED_AT


def test_the_object_is_gzip_with_a_request_record_linked_to_the_response(
    s3_client: S3Client,
) -> None:
    stored = store_exchange(uuid.uuid4(), _exchange())

    obj = s3_client.get_object(Bucket=get_settings().s3_bucket_evidence, Key=stored.key)
    raw = gzip.decompress(obj["Body"].read()).decode("utf-8", errors="replace")

    assert raw.index("WARC-Type: request") < raw.index("WARC-Type: response")
    assert f"WARC-Concurrent-To: {stored.record_id}" in raw
    assert "GET /api/jobs/2911234 HTTP/2" in raw
    assert "User-Agent: aoi/0.1" in raw


def test_wire_encoding_headers_are_kept_under_x_archive_orig() -> None:
    stored = store_exchange(
        uuid.uuid4(),
        _exchange(
            response_headers={
                "Content-Type": "text/html",
                "Content-Encoding": "gzip",
                "Transfer-Encoding": "chunked",
                "Content-Length": "312",
            }
        ),
    )

    headers = read_capture(stored.key).headers

    assert "Content-Encoding" not in headers
    assert "Transfer-Encoding" not in headers
    assert headers["X-Archive-Orig-Content-Encoding"] == "gzip"
    assert headers["X-Archive-Orig-Transfer-Encoding"] == "chunked"
    assert headers["X-Archive-Orig-Content-Length"] == "312"
    assert headers["Content-Length"] == str(len(BODY))


def test_an_identical_body_is_stored_as_a_revisit_of_the_earlier_capture() -> None:
    first = store_exchange(uuid.uuid4(), _exchange())
    earlier = EarlierCapture(
        record_id=first.record_id,
        url="https://inspirehep.net/api/jobs/2911234",
        fetched_at=FETCHED_AT,
    )
    later = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

    stored = store_exchange(uuid.uuid4(), _exchange(fetched_at=later), same_as=earlier)
    capture = read_capture(stored.key)

    assert capture.record_type == "revisit"
    assert capture.refers_to == first.record_id
    assert capture.body == b""
    assert capture.status == 200
    assert capture.fetched_at == later
    assert capture.payload_digest == read_capture(first.key).payload_digest


def test_a_not_modified_response_is_stored_with_an_empty_body() -> None:
    stored = store_exchange(uuid.uuid4(), _exchange(status=304, body=b""))

    capture = read_capture(stored.key)

    assert capture.status == 304
    assert capture.body == b""


def test_an_unregistered_status_code_is_stored() -> None:
    stored = store_exchange(uuid.uuid4(), _exchange(status=599, body=b"proxy timeout"))

    assert read_capture(stored.key).status == 599


def test_a_non_ascii_header_value_is_kept_percent_encoded() -> None:
    disposition = 'attachment; filename="Προκήρυξη θέσης.pdf"'
    stored = store_exchange(
        uuid.uuid4(),
        _exchange(response_headers={"Content-Disposition": disposition}),
    )

    value = read_capture(stored.key).headers["Content-Disposition"]

    # warcio rewrites non-ASCII values in the RFC 8187 form (filename*=UTF-8''...).
    assert "Προκήρυξη θέσης.pdf" in unquote(value)


def test_reading_an_object_without_a_response_record_fails_loudly(s3_client: S3Client) -> None:
    buffer = io.BytesIO()
    writer = WARCWriter(buffer, gzip=True)
    writer.write_record(
        writer.create_warc_record(
            "https://inspirehep.net/api/jobs/1",
            "request",
            payload=io.BytesIO(b""),
            http_headers=StatusAndHeaders("GET /api/jobs/1 HTTP/1.1", [], is_http_request=True),
        )
    )
    key = f"warc/2026/10/18/{uuid.uuid4()}.warc.gz"
    s3_client.put_object(Bucket=get_settings().s3_bucket_evidence, Key=key, Body=buffer.getvalue())

    with pytest.raises(ValueError, match="no response or revisit record"):
        read_capture(key)
