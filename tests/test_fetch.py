"""Tests for the HTTP fetch layer — Stage 4.

Tests use monkeypatching for socket.getaddrinfo and httpx.Client.
No real HTTP requests are made. Tests that would hit real URLs are
marked @pytest.mark.integration.
"""

import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status: int = 200,
    body: bytes = b"<html>body</html>",
    headers: dict[str, str] | None = None,
    http_version: str = "HTTP/1.1",
    request_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock httpx.Response, with the request it answered."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.content = body
    resp.headers = httpx.Headers(headers or {"content-type": "text/html"})
    resp.http_version = http_version
    resp.request = MagicMock(spec=httpx.Request)
    resp.request.headers = httpx.Headers(request_headers or {"user-agent": "aoi-test"})
    resp.elapsed = MagicMock()
    resp.elapsed.total_seconds.return_value = 0.1
    return resp


def _patch_getaddrinfo_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch socket.getaddrinfo to return a public IP."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
    )


# ---------------------------------------------------------------------------
# 1. BlockedAddress raised for private IPs (SSRF protection)
# ---------------------------------------------------------------------------


def test_fetch_raises_blocked_address_for_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch() raises BlockedAddress before any TCP connection for private IPs."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))],
    )

    from aoi.pipeline.canonicalize import BlockedAddress
    from aoi.pipeline.fetch import fetch

    with pytest.raises(BlockedAddress):
        fetch("https://internal.corp/api")


# ---------------------------------------------------------------------------
# 2. Successful 200 fetch returns FetchResult
# ---------------------------------------------------------------------------


def test_fetch_200_returns_fetch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() returns a FetchResult with the correct fields for a 200 response."""
    _patch_getaddrinfo_public(monkeypatch)

    mock_response = _make_response(200, b"hello world")

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import FetchResult, fetch

        result = fetch("https://example.com/page")

    assert isinstance(result, FetchResult)
    assert result.url == "https://example.com/page"
    assert result.status == 200
    assert result.body == b"hello world"
    assert result.not_modified is False
    assert result.resolved_ip == "93.184.216.34"
    assert result.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# 3. 304 Not Modified response returns empty body and not_modified=True
# ---------------------------------------------------------------------------


def test_fetch_304_returns_not_modified(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() returns not_modified=True and empty body for a 304 response."""
    _patch_getaddrinfo_public(monkeypatch)

    mock_response = _make_response(304, b"")

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch(
            "https://example.com/page",
            etag='"abc123"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        )

    assert result.status == 304
    assert result.body == b""
    assert result.not_modified is True


# ---------------------------------------------------------------------------
# 4. Conditional GET headers are sent when etag/last_modified provided
# ---------------------------------------------------------------------------


def test_fetch_sends_conditional_get_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() sends If-None-Match and If-Modified-Since when provided."""
    _patch_getaddrinfo_public(monkeypatch)

    mock_response = _make_response(200, b"content")

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        fetch(
            "https://example.com/page",
            etag='"abc123"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        )

        call_kwargs = mock_client_instance.get.call_args
        # headers are always passed as a keyword argument
        sent_headers = call_kwargs.kwargs.get("headers", {})

    assert "If-None-Match" in sent_headers
    assert sent_headers["If-None-Match"] == '"abc123"'
    assert "If-Modified-Since" in sent_headers
    assert sent_headers["If-Modified-Since"] == "Wed, 21 Oct 2015 07:28:00 GMT"


# ---------------------------------------------------------------------------
# 5. Body size enforcement: BodyTooLarge raised when body exceeds limit
# ---------------------------------------------------------------------------


def test_fetch_raises_body_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() raises BodyTooLarge when response body exceeds max_body_bytes."""
    _patch_getaddrinfo_public(monkeypatch)

    # Override settings to use a tiny limit
    from aoi.config import get_settings

    original_get_settings = get_settings

    def patched_get_settings():  # type: ignore[no-untyped-def]
        s = original_get_settings()
        object.__setattr__(s, "max_body_bytes", 10)
        return s

    monkeypatch.setattr("aoi.pipeline.fetch.get_settings", patched_get_settings)

    oversized_body = b"x" * 100
    mock_response = _make_response(200, oversized_body)

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import BodyTooLarge, fetch

        with pytest.raises(BodyTooLarge):
            fetch("https://example.com/page")


# ---------------------------------------------------------------------------
# 6. User-Agent header is sent from config
# ---------------------------------------------------------------------------


def test_fetch_sends_user_agent_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() sends the User-Agent header from config."""
    _patch_getaddrinfo_public(monkeypatch)

    mock_response = _make_response(200, b"ok")

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        fetch("https://example.com/page")

        call_kwargs = mock_client_instance.get.call_args
        sent_headers = call_kwargs.kwargs.get("headers", {})

    assert "User-Agent" in sent_headers
    assert "AOI" in sent_headers["User-Agent"]


# ---------------------------------------------------------------------------
# 7. FetchResult dataclass is frozen (immutable)
# ---------------------------------------------------------------------------


def test_fetch_result_is_frozen() -> None:
    """FetchResult is a frozen dataclass — fields cannot be reassigned."""
    from aoi.pipeline.fetch import FetchResult

    result = FetchResult(
        url="https://example.com/",
        status=200,
        headers={},
        body=b"",
        elapsed_ms=50,
        not_modified=False,
        resolved_ip="1.2.3.4",
        final_url="https://example.com/",
        http_version="HTTP/1.1",
        request_headers={},
    )

    with pytest.raises((AttributeError, TypeError)):
        result.status = 404  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. Error response (4xx/5xx) still returns FetchResult (not raised)
# ---------------------------------------------------------------------------


def test_fetch_error_status_returns_fetch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() returns FetchResult for 404/500 — callers decide what to do."""
    _patch_getaddrinfo_public(monkeypatch)

    mock_response = _make_response(404, b"Not Found")

    with patch("aoi.pipeline.fetch.httpx.Client") as MockClient:
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = mock_response
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/gone")

    assert result.status == 404
    assert result.not_modified is False


# ---------------------------------------------------------------------------
# 9. 429 retry: retries on 429 then succeeds
# ---------------------------------------------------------------------------


def test_fetch_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() retries after a 429 and returns the eventual 200."""
    _patch_getaddrinfo_public(monkeypatch)

    resp_429 = _make_response(429, b"Too Many Requests")
    resp_200 = _make_response(200, b"ok")

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep") as mock_sleep,
        patch("aoi.pipeline.fetch._limiter"),
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.side_effect = [resp_429, resp_200]
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/page")

    assert result.status == 200
    assert result.body == b"ok"
    assert mock_sleep.call_count == 1
    # First retry uses ~60s base backoff (plus jitter)
    wait_arg = mock_sleep.call_args[0][0]
    assert 59.0 <= wait_arg <= 66.5


# ---------------------------------------------------------------------------
# 10. 429 retry: respects Retry-After header
# ---------------------------------------------------------------------------


def test_fetch_respects_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() uses Retry-After when it exceeds the computed backoff."""
    _patch_getaddrinfo_public(monkeypatch)

    resp_429 = _make_response(429, b"Too Many Requests", {"retry-after": "120"})
    resp_200 = _make_response(200, b"ok")

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep") as mock_sleep,
        patch("aoi.pipeline.fetch._limiter"),
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.side_effect = [resp_429, resp_200]
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/page")

    assert result.status == 200
    # Should wait at least 120s (Retry-After), not just 60s base
    wait_arg = mock_sleep.call_args[0][0]
    assert wait_arg >= 120.0


# ---------------------------------------------------------------------------
# 11. 429 retry: gives up after max retries
# ---------------------------------------------------------------------------


def test_fetch_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch() returns 429 to the caller after exhausting retries."""
    _patch_getaddrinfo_public(monkeypatch)

    resp_429 = _make_response(429, b"Too Many Requests")

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep"),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        # 1 initial + 3 retries = 4 total
        mock_client_instance.get.return_value = resp_429
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/page")

    assert result.status == 429
    # 1 initial call + 3 retries = 4 total
    assert mock_client_instance.get.call_count == 4


# ---------------------------------------------------------------------------
# 12. 429 retry: gives up when Retry-After exceeds max wait
# ---------------------------------------------------------------------------


def test_fetch_gives_up_when_retry_after_exceeds_max_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch() does not retry when Retry-After exceeds the max wait cap."""
    _patch_getaddrinfo_public(monkeypatch)

    resp_429 = _make_response(429, b"Too Many Requests", {"retry-after": "3600"})

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep") as mock_sleep,
        patch("aoi.pipeline.fetch._limiter"),
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.return_value = resp_429
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/page")

    assert result.status == 429
    # Should not have slept — gave up immediately
    mock_sleep.assert_not_called()
    # Only the initial request, no retries
    assert mock_client_instance.get.call_count == 1


# ---------------------------------------------------------------------------
# 13. 429 retry: penalizes rate limiter after recovery
# ---------------------------------------------------------------------------


def test_fetch_penalizes_rate_limiter_after_429_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After recovering from a 429, the rate limiter is penalized."""
    _patch_getaddrinfo_public(monkeypatch)

    resp_429 = _make_response(429, b"Too Many Requests")
    resp_200 = _make_response(200, b"ok")

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep"),
        patch("aoi.pipeline.fetch._limiter") as mock_limiter,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.get.side_effect = [resp_429, resp_200]
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import _PENALTY_SECONDS, fetch

        fetch("https://example.com/page")

    mock_limiter.penalize.assert_called_once_with("example.com", _PENALTY_SECONDS)


def test_fetch_without_429_retries_returns_the_429_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task worker leaves the backoff to the queue: no sleep, no second request."""
    _patch_getaddrinfo_public(monkeypatch)

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        patch("aoi.pipeline.fetch.time.sleep") as mock_sleep,
        patch("aoi.pipeline.fetch._limiter"),
    ):
        mock_client_instance = _mock_client([_make_response(429, b"Too Many Requests")])
        MockClient.return_value = mock_client_instance

        from aoi.pipeline.fetch import fetch

        result = fetch("https://example.com/page", max_429_retries=0)

    assert result.status == 429
    mock_sleep.assert_not_called()
    assert mock_client_instance.get.call_count == 1


# ---------------------------------------------------------------------------
# Redirects: every hop passes the SSRF check (ADR 0023)
# ---------------------------------------------------------------------------


def _patch_getaddrinfo_by_host(monkeypatch: pytest.MonkeyPatch, ips: dict[str, str]) -> None:
    """Patch socket.getaddrinfo to resolve each hostname to the given IP."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ips[host], 0))],
    )


def _mock_client(responses: list[MagicMock]) -> MagicMock:
    """A mock httpx.Client whose get() returns the given responses in order."""
    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = responses
    return client


def test_fetch_blocks_redirect_to_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public host redirecting to the metadata IP raises before the second request."""
    _patch_getaddrinfo_by_host(
        monkeypatch, {"example.com": "93.184.216.34", "169.254.169.254": "169.254.169.254"}
    )
    redirect = _make_response(302, b"", {"location": "http://169.254.169.254/latest/meta-data"})
    client = _mock_client([redirect])

    from aoi.pipeline.canonicalize import BlockedAddress
    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
        pytest.raises(BlockedAddress),
    ):
        fetch("https://example.com/job")

    assert client.get.call_count == 1


def test_fetch_follows_redirect_to_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect to another public host is followed and its body returned."""
    _patch_getaddrinfo_by_host(
        monkeypatch, {"example.com": "93.184.216.34", "jobs.example.org": "93.184.216.35"}
    )
    redirect = _make_response(301, b"", {"location": "https://jobs.example.org/final"})
    final = _make_response(200, b"final page")
    client = _mock_client([redirect, final])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        result = fetch("https://example.com/job")

    assert result.status == 200
    assert result.body == b"final page"
    assert client.get.call_args_list[1].args[0] == "https://jobs.example.org/final"


def test_fetch_resolves_relative_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative Location header is resolved against the current URL."""
    _patch_getaddrinfo_by_host(monkeypatch, {"example.com": "93.184.216.34"})
    redirect = _make_response(307, b"", {"location": "/jobs/42"})
    final = _make_response(200, b"job 42")
    client = _mock_client([redirect, final])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        fetch("https://example.com/listing")

    assert client.get.call_args_list[1].args[0] == "https://example.com/jobs/42"


def test_fetch_raises_too_many_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than five redirect hops raises httpx.TooManyRedirects."""
    _patch_getaddrinfo_by_host(monkeypatch, {"example.com": "93.184.216.34"})
    loop = [_make_response(302, b"", {"location": "/again"}) for _ in range(10)]
    client = _mock_client(loop)

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
        pytest.raises(httpx.TooManyRedirects),
    ):
        fetch("https://example.com/start")


def test_fetch_disables_httpx_redirect_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx must not follow redirects itself, or hops would skip the SSRF check."""
    _patch_getaddrinfo_public(monkeypatch)
    client = _mock_client([_make_response(200)])

    from aoi.pipeline.fetch import fetch

    with patch("aoi.pipeline.fetch.httpx.Client", return_value=client) as MockClient:
        fetch("https://example.com/page")

    assert MockClient.call_args.kwargs["follow_redirects"] is False


# ---------------------------------------------------------------------------
# Egress proxy (Smokescreen, ADR 0023)
# ---------------------------------------------------------------------------


def test_fetch_routes_through_egress_proxy_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With AOI_EGRESS_PROXY set, the httpx client is built with that proxy."""
    _patch_getaddrinfo_public(monkeypatch)
    monkeypatch.setenv("AOI_EGRESS_PROXY", "http://smokescreen:4750")
    client = _mock_client([_make_response(200)])

    from aoi.pipeline.fetch import fetch

    with patch("aoi.pipeline.fetch.httpx.Client", return_value=client) as MockClient:
        fetch("https://example.com/page")

    assert MockClient.call_args.kwargs["proxy"] == "http://smokescreen:4750"


def test_fetch_refuses_without_egress_proxy_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In production (AOI_DEBUG=false) fetch fails closed if no egress proxy is set."""
    _patch_getaddrinfo_public(monkeypatch)
    monkeypatch.setenv("AOI_DEBUG", "false")
    monkeypatch.setenv("AOI_EGRESS_PROXY", "")

    from aoi.pipeline.fetch import EgressProxyMissing, fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client") as MockClient,
        pytest.raises(EgressProxyMissing),
    ):
        fetch("https://example.com/page")

    MockClient.assert_not_called()


def test_fetch_allows_exactly_max_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five redirect hops followed by a 200 succeed; only a sixth hop is refused."""
    _patch_getaddrinfo_by_host(monkeypatch, {"example.com": "93.184.216.34"})
    hops = [_make_response(302, b"", {"location": f"/hop{i}"}) for i in range(5)]
    client = _mock_client([*hops, _make_response(200, b"arrived")])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        result = fetch("https://example.com/start")

    assert result.body == b"arrived"
    assert client.get.call_count == 6


@pytest.mark.parametrize(
    "retry_after, minimum_wait",
    [
        ("HTTP-DATE+300", 290.0),  # an HTTP-date five minutes ahead beats the 60 s base
        ("soon", 60.0),  # unparseable header falls back to the base backoff
    ],
)
def test_fetch_parses_retry_after_date_and_garbage(
    retry_after: str,
    minimum_wait: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry-After given as an HTTP-date is honoured; garbage falls back to backoff."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    if retry_after.startswith("HTTP-DATE"):
        retry_after = format_datetime(datetime.now(UTC) + timedelta(seconds=300), usegmt=True)
    _patch_getaddrinfo_public(monkeypatch)
    client = _mock_client(
        [_make_response(429, b"", {"retry-after": retry_after}), _make_response(200, b"ok")]
    )

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch.time.sleep") as mock_sleep,
        patch("aoi.pipeline.fetch._limiter"),
    ):
        fetch("https://example.com/page")

    assert mock_sleep.call_args[0][0] >= minimum_wait


# ---------------------------------------------------------------------------
# What the WARC record needs (ADR 0030 §3): final URL and hop, version, headers sent
# ---------------------------------------------------------------------------


def test_fetch_reports_the_final_url_and_the_final_hops_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_getaddrinfo_by_host(
        monkeypatch, {"example.com": "93.184.216.34", "jobs.example.org": "93.184.216.35"}
    )
    redirect = _make_response(301, b"", {"location": "https://jobs.example.org/final"})
    client = _mock_client([redirect, _make_response(200, b"final page")])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        result = fetch("https://example.com/job")

    assert result.url == "https://example.com/job"
    assert result.final_url == "https://jobs.example.org/final"
    assert result.resolved_ip == "93.184.216.35"


def test_fetch_without_redirect_reports_the_requested_url_as_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_getaddrinfo_public(monkeypatch)
    client = _mock_client([_make_response(200)])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        result = fetch("https://example.com/job")

    assert result.final_url == "https://example.com/job"
    assert result.resolved_ip == "93.184.216.34"


@pytest.mark.parametrize("status", [200, 304])
def test_fetch_reports_the_http_version_and_the_request_headers_sent(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _patch_getaddrinfo_public(monkeypatch)
    sent = {"host": "example.com", "user-agent": "aoi/0.1", "accept-encoding": "gzip, br"}
    response = _make_response(
        status, b"" if status == 304 else b"x", http_version="HTTP/2", request_headers=sent
    )
    client = _mock_client([response])

    from aoi.pipeline.fetch import fetch

    with (
        patch("aoi.pipeline.fetch.httpx.Client", return_value=client),
        patch("aoi.pipeline.fetch._limiter"),
    ):
        result = fetch("https://example.com/job")

    assert result.http_version == "HTTP/2"
    assert result.request_headers == sent
