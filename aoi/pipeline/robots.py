"""Robots.txt assessment for AOI crawl sources."""

from typing import Any
from urllib.robotparser import RobotFileParser

import structlog

from aoi.pipeline.fetch import FetchResult, fetch

log = structlog.get_logger()


def assess_robots(
    source_id: str,
    domain: str,
    user_agent: str,
) -> dict[str, Any]:
    """Fetch robots.txt for a domain and determine if crawling is permitted.

    Returns a dict with keys:
    - verdict: "allowed" | "disallowed" | "unreachable"
    - rationale: human-readable explanation
    - crawl_delay: float | None — seconds extracted from Crawl-delay directive
    - robots_body: str | None — raw robots.txt content
    """
    robots_url = f"https://{domain}/robots.txt"

    log.info("robots.assess", domain=domain, source_id=source_id)

    result: FetchResult | None = None
    try:
        result = fetch(robots_url)
    except Exception as exc:
        log.warning("robots.fetch_failed", domain=domain, error=str(exc))
        return {
            "verdict": "unreachable",
            "rationale": f"Failed to fetch robots.txt: {exc}",
            "crawl_delay": None,
            "robots_body": None,
        }

    if result.status != 200:
        log.info(
            "robots.not_found",
            domain=domain,
            status=result.status,
        )
        return {
            "verdict": "allowed",
            "rationale": f"robots.txt returned HTTP {result.status} — crawling permitted",
            "crawl_delay": None,
            "robots_body": None,
        }

    robots_body = result.body.decode("utf-8", errors="replace")

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(robots_body.splitlines())

    can_fetch = parser.can_fetch(user_agent, "/")
    _raw_delay = parser.crawl_delay(user_agent)
    crawl_delay: float | None = float(_raw_delay) if _raw_delay is not None else None

    if can_fetch:
        verdict = "allowed"
        rationale = f"robots.txt permits crawling for user-agent {user_agent!r}"
    else:
        verdict = "disallowed"
        rationale = f"robots.txt disallows crawling for user-agent {user_agent!r}"

    log.info(
        "robots.assessed",
        domain=domain,
        verdict=verdict,
        crawl_delay=crawl_delay,
    )

    return {
        "verdict": verdict,
        "rationale": rationale,
        "crawl_delay": crawl_delay,
        "robots_body": robots_body,
    }
