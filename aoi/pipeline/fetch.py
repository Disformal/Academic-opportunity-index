"""HTTP fetch layer for AOI — the only place httpx is imported."""

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from aoi.config import get_settings
from aoi.pipeline.canonicalize import validate_host
from aoi.pipeline.rate_limit import _limiter

log = structlog.get_logger()

# 429 retry policy — generous backoff to avoid abusing upstream sites.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 60.0
_BACKOFF_MULTIPLIER = 2.0
_MAX_WAIT_SECONDS = 600.0  # give up if Retry-After exceeds 10 minutes
_PENALTY_SECONDS = 30.0  # slow subsequent requests after a 429 recovery

# Redirects are followed by hand so every hop passes validate_host (ADR 0023).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP-date). Returns seconds to wait."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        delay = (dt - datetime.now(UTC)).total_seconds()
        return max(0.0, delay)
    except (ValueError, TypeError):
        return None


def _get_following_redirects(
    client: httpx.Client, url: str, headers: dict[str, str], rps: float, resolved_ip: str
) -> tuple[httpx.Response, str, str]:
    """GET *url*, following redirects so that every new host is SSRF-checked first.

    *resolved_ip* is the address validate_host returned for *url*'s host. Returns the final
    response, its URL, and the address validate_host returned for the final host.

    Raises BlockedAddress if a hop resolves to a non-public address, and
    httpx.TooManyRedirects after _MAX_REDIRECTS hops.
    """
    response = client.get(url, headers=headers)
    for _ in range(_MAX_REDIRECTS):
        location = response.headers.get("location")
        if response.status_code not in _REDIRECT_STATUSES or location is None:
            return response, url, resolved_ip
        url = urljoin(url, location)
        host = urlsplit(url).hostname or ""
        resolved_ip = validate_host(host)
        _limiter.wait(host, rps)
        log.debug("fetch.redirect", url=url, status=response.status_code)
        response = client.get(url, headers=headers)
    if response.status_code in _REDIRECT_STATUSES:
        raise httpx.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects", request=None)
    return response, url, resolved_ip


class BodyTooLarge(Exception):
    """Raised when the response body exceeds max_body_bytes."""


class EgressProxyMissing(Exception):
    """Raised in production when no egress proxy is configured (fail closed)."""


@dataclass(frozen=True)
class FetchResult:
    """Result of a single HTTP fetch."""

    url: str
    final_url: str
    """URL of the final response, after redirects."""
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    not_modified: bool
    resolved_ip: str | None
    """Address validate_host returned for the final URL's host."""
    http_version: str
    """Protocol of the final response, e.g. 'HTTP/1.1' or 'HTTP/2'."""
    request_headers: dict[str, str]
    """Headers httpx actually sent with the final request (including its defaults)."""


def fetch(
    url: str,
    *,
    per_second: float | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    accept: str = "*/*",
    max_429_retries: int = _MAX_RETRIES,
) -> FetchResult:
    """The only way this project makes an outbound request.

    Steps:
    1. Parse host from URL
    2. validate_host(host) — SSRF check BEFORE any TCP connection
    3. Rate limit via _limiter.wait(host, per_second or settings.rate_limit_rps)
    4. Build headers: User-Agent, Accept, conditional GET headers
    5. Make GET request with httpx.Client; follow redirects by hand, SSRF-checking every hop
    6. If 304, body = b"" and not_modified = True
    7. Check body size against max_body_bytes
    8. Return FetchResult, with the final URL, the final hop's address, the HTTP version
       and the request headers sent (what a WARC record needs, ADR 0030)

    Raises BlockedAddress if the host, or any redirect target, resolves to a
    non-public address. Raises httpx.TooManyRedirects after 5 hops.
    Raises EgressProxyMissing if AOI_DEBUG is false and AOI_EGRESS_PROXY is unset.
    Raises BodyTooLarge if response body exceeds max_body_bytes.
    """
    settings = get_settings()
    if not settings.debug and not settings.egress_proxy:
        raise EgressProxyMissing("AOI_EGRESS_PROXY must be set when AOI_DEBUG is false")
    parts = urlsplit(url)
    host = parts.hostname or ""

    # SSRF check — always before any TCP connection
    first_hop_ip = validate_host(host)

    # Rate limiting
    rps = per_second if per_second is not None else settings.rate_limit_rps
    _limiter.wait(host, rps)

    # Build request headers
    headers: dict[str, str] = {
        "User-Agent": settings.user_agent,
        "Accept": accept,
    }
    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified

    log.debug("fetch.request", url=url, host=host, resolved_ip=first_hop_ip)

    start = time.monotonic()
    retried = False
    with httpx.Client(
        http2=True,
        follow_redirects=False,
        timeout=settings.http_timeout_seconds,
        proxy=settings.egress_proxy or None,
    ) as client:
        response, final_url, resolved_ip = _get_following_redirects(
            client, url, headers, rps, first_hop_ip
        )

        # Retry loop for 429 Too Many Requests
        for attempt in range(max_429_retries):
            if response.status_code != 429:
                break

            wait = _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER**attempt)
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None:
                wait = max(wait, retry_after)

            if wait > _MAX_WAIT_SECONDS:
                log.warning("fetch.429_giving_up", url=url, wait=wait)
                break

            wait += wait * random.uniform(0, 0.1)

            log.warning(
                "fetch.429_retry",
                url=url,
                attempt=attempt + 1,
                wait_seconds=round(wait, 1),
            )
            time.sleep(wait)

            _limiter.wait(host, rps)
            response, final_url, resolved_ip = _get_following_redirects(
                client, url, headers, rps, first_hop_ip
            )
            retried = True

    # Penalize host rate limiter after a 429 recovery
    if retried and response.status_code != 429:
        _limiter.penalize(host, _PENALTY_SECONDS)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    # Handle 304 Not Modified
    if response.status_code == 304:
        log.debug("fetch.not_modified", url=url, elapsed_ms=elapsed_ms)
        return FetchResult(
            url=url,
            final_url=final_url,
            status=304,
            headers=dict(response.headers),
            body=b"",
            elapsed_ms=elapsed_ms,
            not_modified=True,
            resolved_ip=resolved_ip,
            http_version=response.http_version,
            request_headers=dict(response.request.headers),
        )

    body = response.content

    # Enforce body size limit
    if len(body) > settings.max_body_bytes:
        raise BodyTooLarge(
            f"Response body {len(body)} bytes exceeds limit {settings.max_body_bytes} bytes"
        )

    log.info(
        "fetch.complete",
        url=url,
        status=response.status_code,
        body_bytes=len(body),
        elapsed_ms=elapsed_ms,
    )

    return FetchResult(
        url=url,
        final_url=final_url,
        status=response.status_code,
        headers=dict(response.headers),
        body=body,
        elapsed_ms=elapsed_ms,
        not_modified=False,
        resolved_ip=resolved_ip,
        http_version=response.http_version,
        request_headers=dict(response.request.headers),
    )
