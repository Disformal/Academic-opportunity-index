"""WARC evidence (ADR 0006, 0030 §3): one gzip WARC object per fetch, stored before its fetch row.

Each object holds a request record and a response record, or a revisit record when the body is
identical to an earlier capture. The body is stored as fetch() returns it, already decoded, so the
headers that described the wire encoding are kept under X-Archive-Orig-* and Content-Length is
set to the stored body's length; archive tools then read the record correctly.
"""

import base64
import hashlib
import io
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import urlsplit

import structlog
from warcio.archiveiterator import ArchiveIterator  # type: ignore[import-untyped]
from warcio.statusandheaders import StatusAndHeaders  # type: ignore[import-untyped]
from warcio.warcwriter import WARCWriter  # type: ignore[import-untyped]

from aoi.config import get_settings
from aoi.storage.evidence import _get_s3_client

log = structlog.get_logger()

_WIRE_HEADERS = frozenset({"content-encoding", "transfer-encoding", "content-length"})


@dataclass(frozen=True)
class Exchange:
    """One HTTP GET as it happened: final URL, the headers sent, and the response."""

    url: str
    request_headers: Mapping[str, str]
    http_version: str
    status: int
    response_headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime


@dataclass(frozen=True)
class EarlierCapture:
    """The capture an identical body is a revisit of."""

    record_id: str
    url: str
    fetched_at: datetime


@dataclass(frozen=True)
class StoredWarc:
    key: str
    record_id: str
    """WARC-Record-ID of the response or revisit record (goes on evidence.fetch)."""


@dataclass(frozen=True)
class Capture:
    record_type: str
    record_id: str
    url: str
    http_version: str
    status: int
    headers: dict[str, str]
    body: bytes
    fetched_at: datetime
    payload_digest: str
    refers_to: str | None


def _warc_date(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _payload_digest(body: bytes) -> str:
    """The WARC payload digest warcio computes: base32 SHA-1."""
    return "sha1:" + base64.b32encode(hashlib.sha1(body).digest()).decode()


def _response_headers(exchange: Exchange) -> StatusAndHeaders:
    headers = [
        (f"X-Archive-Orig-{name}" if name.lower() in _WIRE_HEADERS else name, value)
        for name, value in exchange.response_headers.items()
    ]
    headers.append(("Content-Length", str(len(exchange.body))))
    try:
        phrase = HTTPStatus(exchange.status).phrase
    except ValueError:
        phrase = ""
    statusline = f"{exchange.status} {phrase}".strip()
    return StatusAndHeaders(statusline, headers, protocol=exchange.http_version)


def store_exchange(
    fetch_id: uuid.UUID, exchange: Exchange, *, same_as: EarlierCapture | None = None
) -> StoredWarc:
    """Write the exchange as one gzip WARC object; return its key once it is stored."""
    parts = urlsplit(exchange.url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    date = {"WARC-Date": _warc_date(exchange.fetched_at)}

    buffer = io.BytesIO()
    writer = WARCWriter(buffer, gzip=True)
    if same_as is None:
        record = writer.create_warc_record(
            exchange.url,
            "response",
            payload=io.BytesIO(exchange.body),
            http_headers=_response_headers(exchange),
            warc_headers_dict=date,
        )
    else:
        record = writer.create_revisit_record(
            exchange.url,
            digest=_payload_digest(exchange.body),
            refers_to_uri=same_as.url,
            refers_to_date=_warc_date(same_as.fetched_at),
            http_headers=_response_headers(exchange),
            warc_headers_dict=date,
        )
        record.rec_headers.add_header("WARC-Refers-To", same_as.record_id)
    record_id = str(record.rec_headers.get_header("WARC-Record-ID"))

    request = writer.create_warc_record(
        exchange.url,
        "request",
        payload=io.BytesIO(b""),
        http_headers=StatusAndHeaders(
            f"GET {target} {exchange.http_version}",
            list(exchange.request_headers.items()),
            is_http_request=True,
        ),
        warc_headers_dict={**date, "WARC-Concurrent-To": record_id},
    )
    writer.write_record(request)
    writer.write_record(record)

    key = f"warc/{exchange.fetched_at:%Y/%m/%d}/{fetch_id}.warc.gz"
    _get_s3_client().put_object(
        Bucket=get_settings().s3_bucket_evidence,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="application/warc",
    )
    log.info("warc.stored", key=key, record_type=record.rec_type, bytes=buffer.tell())
    return StoredWarc(key=key, record_id=record_id)


def read_capture(key: str) -> Capture:
    """Read the response or revisit record of a stored exchange."""
    obj = _get_s3_client().get_object(Bucket=get_settings().s3_bucket_evidence, Key=key)
    for record in ArchiveIterator(io.BytesIO(obj["Body"].read())):
        if record.rec_type not in ("response", "revisit"):
            continue
        http = record.http_headers
        refers_to = record.rec_headers.get_header("WARC-Refers-To")
        return Capture(
            record_type=str(record.rec_type),
            record_id=str(record.rec_headers.get_header("WARC-Record-ID")),
            url=str(record.rec_headers.get_header("WARC-Target-URI")),
            http_version=str(http.protocol),
            status=int(http.get_statuscode()),
            headers={str(name): str(value) for name, value in http.headers},
            body=bytes(record.content_stream().read()),
            fetched_at=datetime.fromisoformat(str(record.rec_headers.get_header("WARC-Date"))),
            payload_digest=str(record.rec_headers.get_header("WARC-Payload-Digest")),
            refers_to=None if refers_to is None else str(refers_to),
        )
    raise ValueError(f"no response or revisit record in {key}")
