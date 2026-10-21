"""Tests for robots.txt assessment — Stage 4."""

from unittest.mock import patch

from aoi.pipeline.canonicalize import BlockedAddress
from aoi.pipeline.fetch import FetchResult
from aoi.pipeline.robots import assess_robots

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_AGENT = "AOI/0.1"


def _make_fetch_result(status: int, body: bytes) -> FetchResult:
    return FetchResult(
        url="https://example.com/robots.txt",
        status=status,
        headers={"content-type": "text/plain"},
        body=body,
        elapsed_ms=50,
        not_modified=False,
        resolved_ip="93.184.216.34",
        final_url="https://example.com/robots.txt",
        http_version="HTTP/1.1",
        request_headers={},
    )


# ---------------------------------------------------------------------------
# 1. robots.txt allows crawling
# ---------------------------------------------------------------------------


def test_assess_robots_allowed() -> None:
    """Returns 'allowed' when robots.txt permits our user-agent."""
    robots_body = "User-agent: *\nAllow: /\n"
    result = _make_fetch_result(200, robots_body.encode())

    with patch("aoi.pipeline.robots.fetch", return_value=result):
        assessment = assess_robots("src-1", "example.com", _USER_AGENT)

    assert assessment["verdict"] == "allowed"
    assert assessment["robots_body"] == robots_body
    assert assessment["crawl_delay"] is None


# ---------------------------------------------------------------------------
# 2. robots.txt disallows crawling
# ---------------------------------------------------------------------------


def test_assess_robots_disallowed() -> None:
    """Returns 'disallowed' when robots.txt blocks our user-agent."""
    robots_body = "User-agent: *\nDisallow: /\n"
    result = _make_fetch_result(200, robots_body.encode())

    with patch("aoi.pipeline.robots.fetch", return_value=result):
        assessment = assess_robots("src-1", "example.com", _USER_AGENT)

    assert assessment["verdict"] == "disallowed"
    assert assessment["robots_body"] == robots_body


# ---------------------------------------------------------------------------
# 3. robots.txt not found (404) — crawling permitted
# ---------------------------------------------------------------------------


def test_assess_robots_404_means_allowed() -> None:
    """Returns 'allowed' when robots.txt is missing (HTTP 404)."""
    result = _make_fetch_result(404, b"Not Found")

    with patch("aoi.pipeline.robots.fetch", return_value=result):
        assessment = assess_robots("src-1", "example.com", _USER_AGENT)

    assert assessment["verdict"] == "allowed"
    assert assessment["robots_body"] is None


# ---------------------------------------------------------------------------
# 4. robots.txt fetch fails — unreachable
# ---------------------------------------------------------------------------


def test_assess_robots_fetch_failure_returns_unreachable() -> None:
    """Returns 'unreachable' when the fetch raises an exception."""
    with patch("aoi.pipeline.robots.fetch", side_effect=BlockedAddress("blocked")):
        assessment = assess_robots("src-1", "evil.internal", _USER_AGENT)

    assert assessment["verdict"] == "unreachable"
    assert "blocked" in assessment["rationale"].lower()
    assert assessment["crawl_delay"] is None
    assert assessment["robots_body"] is None


# ---------------------------------------------------------------------------
# 5. robots.txt with Crawl-delay directive
# ---------------------------------------------------------------------------


def test_assess_robots_extracts_crawl_delay() -> None:
    """Extracts Crawl-delay from robots.txt when present."""
    robots_body = "User-agent: *\nAllow: /\nCrawl-delay: 5\n"
    result = _make_fetch_result(200, robots_body.encode())

    with patch("aoi.pipeline.robots.fetch", return_value=result):
        assessment = assess_robots("src-1", "example.com", _USER_AGENT)

    assert assessment["verdict"] == "allowed"
    assert assessment["crawl_delay"] == 5.0
