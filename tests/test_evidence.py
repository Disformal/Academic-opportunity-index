"""Tests for AOI evidence store — Stage 3."""

import hashlib
import json
import uuid

from aoi.config import get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _unique_fetch_id() -> str:
    return str(uuid.uuid4())


def _unique_body(label: str = "") -> bytes:
    """Return bytes that are unique per test run (contains a UUID)."""
    return f"{label}-{uuid.uuid4()}".encode()


# ---------------------------------------------------------------------------
# 1. Roundtrip: store returns correct StoredEvidence
# ---------------------------------------------------------------------------


def test_store_fetch_roundtrip_returns_stored_evidence() -> None:
    """store_fetch returns StoredEvidence with correct digest, blob_was_new, and body_bytes."""
    from aoi.storage.evidence import store_fetch

    body = _unique_body("roundtrip")
    fetch_id = _unique_fetch_id()
    result = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id,
        url="https://example.com/job/1",
        request_headers={"Accept": "application/json"},
        status=200,
        response_headers={"Content-Type": "application/json"},
        body=body,
    )

    expected_digest = hashlib.sha256(body).hexdigest()
    assert result.fetch_id == fetch_id
    assert result.content_digest == expected_digest
    assert result.blob_was_new is True
    assert result.body_bytes == len(body)


# ---------------------------------------------------------------------------
# 2. Read back returns original bytes
# ---------------------------------------------------------------------------


def test_read_body_returns_original_bytes() -> None:
    """read_body returns the exact bytes originally stored."""
    from aoi.storage.evidence import read_body, store_fetch

    body = _unique_body("read-body")
    fetch_id = _unique_fetch_id()
    result = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id,
        url="https://example.com/job/2",
        request_headers={},
        status=200,
        response_headers={},
        body=body,
    )

    retrieved = read_body(result.content_digest)
    assert retrieved == body


# ---------------------------------------------------------------------------
# 3. Deduplication: same content stored once
# ---------------------------------------------------------------------------


def test_store_fetch_deduplicates_identical_content() -> None:
    """Storing same bytes twice results in blob_was_new=False on second call."""
    from aoi.storage.evidence import store_fetch

    body = _unique_body("dedup")
    fetch_id_1 = _unique_fetch_id()
    fetch_id_2 = _unique_fetch_id()

    result1 = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id_1,
        url="https://example.com/job/3a",
        request_headers={},
        status=200,
        response_headers={},
        body=body,
    )
    result2 = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id_2,
        url="https://example.com/job/3b",
        request_headers={},
        status=200,
        response_headers={},
        body=body,
    )

    assert result1.blob_was_new is True
    assert result2.blob_was_new is False
    assert result1.content_digest == result2.content_digest


# ---------------------------------------------------------------------------
# 4. Digest matches SHA-256
# ---------------------------------------------------------------------------


def test_content_digest_matches_sha256() -> None:
    """content_digest equals hashlib.sha256(body).hexdigest()."""
    from aoi.storage.evidence import store_fetch

    body = _unique_body("digest-check")
    fetch_id = _unique_fetch_id()
    result = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id,
        url="https://example.com/job/4",
        request_headers={},
        status=200,
        response_headers={},
        body=body,
    )

    assert result.content_digest == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# 5. store_text creates derived object
# ---------------------------------------------------------------------------


def test_store_text_creates_derived_object(s3_client) -> None:  # type: ignore[no-untyped-def]
    """store_text returns a key matching derived/{digest}.txt and content is readable."""
    from aoi.storage.evidence import store_text

    digest = hashlib.sha256(b"some content").hexdigest()
    text_content = "extracted text content for testing"

    key = store_text(digest, text_content)

    assert key == f"derived/{digest}.txt"

    s = get_settings()
    obj = s3_client.get_object(Bucket=s.s3_bucket_derived, Key=key)
    body_read = obj["Body"].read().decode("utf-8")
    assert body_read == text_content


# ---------------------------------------------------------------------------
# 6. Secrets are redacted in request.json
# ---------------------------------------------------------------------------


def test_secrets_are_redacted_in_request_json(s3_client) -> None:  # type: ignore[no-untyped-def]
    """Authorization header value is not stored in request.json."""
    from aoi.storage.evidence import store_fetch

    fetch_id = _unique_fetch_id()
    result = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id,
        url="https://example.com/job/5",
        request_headers={"Authorization": "Bearer supersecret", "Accept": "application/json"},
        status=200,
        response_headers={},
        body=_unique_body("redact"),
    )

    s = get_settings()
    key = f"{result.prefix}request.json"
    obj = s3_client.get_object(Bucket=s.s3_bucket_evidence, Key=key)
    request_data = json.loads(obj["Body"].read())

    assert "supersecret" not in json.dumps(request_data)
    assert "[REDACTED]" in json.dumps(request_data)


# ---------------------------------------------------------------------------
# 7. fetch_evidence returns manifest
# ---------------------------------------------------------------------------


def test_fetch_evidence_returns_manifest() -> None:
    """fetch_evidence returns parsed manifest dict with the correct content_digest."""
    from aoi.storage.evidence import fetch_evidence, store_fetch

    body = _unique_body("manifest")
    fetch_id = _unique_fetch_id()
    result = store_fetch(
        source_id="test-source",
        fetch_id=fetch_id,
        url="https://example.com/job/6",
        request_headers={},
        status=200,
        response_headers={},
        body=body,
    )

    manifest = fetch_evidence(result.prefix)

    assert manifest["content_digest"] == result.content_digest
    assert manifest["fetch_id"] == fetch_id


# ---------------------------------------------------------------------------
# 8. ensure_buckets creates buckets
# ---------------------------------------------------------------------------


def test_ensure_buckets_creates_buckets(s3_client) -> None:  # type: ignore[no-untyped-def]
    """ensure_buckets ensures aoi-evidence and aoi-derived buckets exist."""
    from aoi.storage.evidence import ensure_buckets

    ensure_buckets()

    s = get_settings()
    # SeaweedFS does not return buckets in list_buckets; use head_bucket for existence check.
    for bucket in [s.s3_bucket_evidence, s.s3_bucket_derived]:
        s3_client.head_bucket(Bucket=bucket)  # raises if bucket does not exist
