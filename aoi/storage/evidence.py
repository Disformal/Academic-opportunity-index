"""Content-addressed blob storage on S3 (SeaweedFS)."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

import boto3
import structlog
import zstandard as zstd
from botocore.config import Config
from botocore.exceptions import ClientError
from mypy_boto3_s3 import S3Client

from aoi.config import get_settings

log = structlog.get_logger()

_s3: S3Client | None = None


def _get_s3_client() -> S3Client:
    global _s3
    if _s3 is None:
        s = get_settings()
        _s3 = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
    return _s3


def extractor_version() -> str:
    """Return the installed aoi package version."""
    return version("aoi")


@dataclass(frozen=True)
class StoredEvidence:
    fetch_id: str
    prefix: str
    content_digest: str
    body_bytes: int
    stored_bytes: int
    blob_was_new: bool


def _redact(headers: dict[str, str]) -> dict[str, str]:
    """Return headers dict with sensitive values replaced by [REDACTED]."""
    sensitive = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
    return {k: "[REDACTED]" if k.lower() in sensitive else v for k, v in headers.items()}


def ensure_buckets() -> None:
    """Create aoi-evidence and aoi-derived buckets if they do not exist."""
    client = _get_s3_client()
    s = get_settings()
    for bucket in [s.s3_bucket_evidence, s.s3_bucket_derived]:
        try:
            client.create_bucket(Bucket=bucket)
            log.info("storage.ensure_buckets", bucket=bucket, created=True)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                log.debug("storage.ensure_buckets", bucket=bucket, already_exists=True)
            else:
                raise


def _blob_key(digest: str) -> str:
    """Return the S3 key for a content blob given its hex digest."""
    return f"blobs/sha256/{digest[:2]}/{digest[2:4]}/{digest}.zst"


def _fetch_prefix(source_id: str, fetch_id: str, fetched_at: datetime) -> str:
    """Return the S3 key prefix for a fetch (ends with '/')."""
    yyyy = fetched_at.strftime("%Y")
    mm = fetched_at.strftime("%m")
    dd = fetched_at.strftime("%d")
    return f"evidence/{source_id}/{yyyy}/{mm}/{dd}/{fetch_id}/"


def store_fetch(
    *,
    source_id: str,
    fetch_id: str,
    url: str,
    request_headers: dict[str, str],
    status: int,
    response_headers: dict[str, str],
    body: bytes,
    fetched_at: datetime | None = None,
) -> StoredEvidence:
    """Store a fetched HTTP response and return a StoredEvidence record."""
    if fetched_at is None:
        fetched_at = datetime.now(UTC)

    client = _get_s3_client()
    s = get_settings()
    bucket = s.s3_bucket_evidence

    # Compute content digest
    content_digest = hashlib.sha256(body).hexdigest()
    blob_key = _blob_key(content_digest)

    # Check if blob already exists
    blob_was_new = False
    try:
        client.head_object(Bucket=bucket, Key=blob_key)
        log.debug("storage.blob_exists", digest=content_digest)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "404":
            # Blob does not exist — compress and store
            cctx = zstd.ZstdCompressor(level=10)
            compressed = cctx.compress(body)
            client.put_object(
                Bucket=bucket,
                Key=blob_key,
                Body=compressed,
                ContentType="application/octet-stream",
            )
            blob_was_new = True
            log.info(
                "storage.blob_stored",
                digest=content_digest,
                raw_bytes=len(body),
                stored_bytes=len(compressed),
            )
        else:
            raise

    # Determine stored size (HEAD to get ContentLength)
    head = client.head_object(Bucket=bucket, Key=blob_key)
    stored_bytes = head["ContentLength"]

    # Build fetch prefix
    prefix = _fetch_prefix(source_id, fetch_id, fetched_at)

    # Write request.json
    request_payload = {
        "fetch_id": fetch_id,
        "url": url,
        "headers": _redact(request_headers),
        "fetched_at": fetched_at.isoformat(),
    }
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}request.json",
        Body=json.dumps(request_payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    # Write response.json
    response_payload = {
        "status": status,
        "headers": response_headers,
        "content_digest": content_digest,
        "body_bytes": len(body),
    }
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}response.json",
        Body=json.dumps(response_payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    # Write manifest.json
    manifest_payload = {
        "fetch_id": fetch_id,
        "source_id": source_id,
        "url": url,
        "fetched_at": fetched_at.isoformat(),
        "status": status,
        "content_digest": content_digest,
        "blob_key": blob_key,
        "body_bytes": len(body),
        "stored_bytes": stored_bytes,
        "extractor_version": extractor_version(),
    }
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}manifest.json",
        Body=json.dumps(manifest_payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    log.info(
        "storage.fetch_stored",
        fetch_id=fetch_id,
        source_id=source_id,
        content_digest=content_digest,
        blob_was_new=blob_was_new,
    )

    return StoredEvidence(
        fetch_id=fetch_id,
        prefix=prefix,
        content_digest=content_digest,
        body_bytes=len(body),
        stored_bytes=stored_bytes,
        blob_was_new=blob_was_new,
    )


def read_body(digest: str) -> bytes:
    """Read and decompress a content blob by its SHA-256 digest."""
    client = _get_s3_client()
    s = get_settings()
    blob_key = _blob_key(digest)
    response = client.get_object(Bucket=s.s3_bucket_evidence, Key=blob_key)
    compressed = response["Body"].read()
    dctx = zstd.ZstdDecompressor()
    return dctx.decompress(compressed)


def store_text(content_digest: str, text: str) -> str:
    """Store extracted text in the derived bucket and return the S3 key."""
    client = _get_s3_client()
    s = get_settings()
    key = f"derived/{content_digest}.txt"
    client.put_object(
        Bucket=s.s3_bucket_derived,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    log.info("storage.text_stored", content_digest=content_digest, key=key)
    return key


def fetch_evidence(prefix: str) -> dict[str, Any]:
    """Read and return the manifest.json for a fetch prefix."""
    client = _get_s3_client()
    s = get_settings()
    key = f"{prefix}manifest.json"
    response = client.get_object(Bucket=s.s3_bucket_evidence, Key=key)
    result: dict[str, Any] = json.loads(response["Body"].read())
    return result
