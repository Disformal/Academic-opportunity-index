"""Structured extraction utilities for AOI.

Two public functions:

extract_json_ld_job(html, base_url)
    Extract a schema.org/JobPosting from JSON-LD embedded in the page.
    Returns a normalised field dict or None if no JobPosting is found.
    Confidence for the returned fields: 0.99 (publisher-declared).

extract_main_text(html, url)
    Extract the main content text via trafilatura.
    Falls back to selectolax body text if trafilatura returns None.
    Used to replace raw tree.body.text() calls in adapters so that
    document_text no longer includes navbars, footers, and cookie banners.
"""

from typing import Any

import structlog

log = structlog.get_logger()


def extract_json_ld_job(html: str, base_url: str) -> dict[str, str] | None:
    """Extract schema.org/JobPosting fields from JSON-LD in the page.

    Uses extruct to parse all JSON-LD blocks in the HTML.  Searches for a
    block with ``@type == "JobPosting"`` and maps the following fields:

    schema.org field                 → our field_path
    ─────────────────────────────────────────────────
    title                            → title
    validThrough                     → deadline
    hiringOrganization.name          → organization
    jobLocation.address.addressCountry → country
    description                      → description
    url                              → application_url

    Only fields that are present and non-empty in the source are included in
    the returned dict.  Returns None if no JobPosting is found.
    """
    if not html:
        return None

    try:
        import extruct  # type: ignore[import-untyped]

        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
    except Exception:
        log.debug("structured_extract.json_ld.parse_error", base_url=base_url)
        return None

    json_ld_items: list[dict[str, Any]] = data.get("json-ld", [])
    job: dict[str, Any] | None = None

    for item in json_ld_items:
        item_type = item.get("@type", "")
        # @type can be a string or a list
        types = [item_type] if isinstance(item_type, str) else item_type
        if "JobPosting" in types:
            job = item
            break

    if job is None:
        return None

    result: dict[str, str] = {}

    # title
    title = job.get("title") or job.get("name")
    if title:
        result["title"] = str(title)

    # deadline — validThrough
    valid_through = job.get("validThrough")
    if valid_through:
        result["deadline"] = str(valid_through)

    # organization — hiringOrganization.name
    hiring_org = job.get("hiringOrganization")
    if isinstance(hiring_org, dict):
        org_name = hiring_org.get("name")
        if org_name:
            result["organization"] = str(org_name)

    # country — jobLocation.address.addressCountry
    job_location = job.get("jobLocation")
    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            country = address.get("addressCountry")
            if country:
                result["country"] = str(country)

    # description
    description = job.get("description")
    if description:
        result["description"] = str(description)

    # application URL
    url = job.get("url")
    if url:
        result["application_url"] = str(url)

    log.debug(
        "structured_extract.json_ld.found",
        base_url=base_url,
        fields=list(result.keys()),
    )
    return result if result else None


def extract_main_text(html: str, url: str) -> str:
    """Extract main content text from HTML via trafilatura.

    trafilatura (Barbaresi, ACL 2021) is the benchmark-leading content
    extractor used in FineWeb and the German National Library.  It strips
    navigation, footers, cookie banners, and boilerplate — leaving the
    article/job-description text.

    Falls back to selectolax body.text() if trafilatura returns None (e.g. for
    very short pages or pages without identifiable main content).

    Args:
        html: Raw HTML string.
        url: Canonical URL of the page (used by trafilatura for heuristics).

    Returns:
        Cleaned main-content text string (may be empty but never None).
    """
    if not html:
        return ""

    try:
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
        if text:
            return text
    except Exception:
        log.debug("structured_extract.trafilatura.error", url=url)

    # Fallback: selectolax body text (same as previous behaviour)
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        if tree.body:
            return tree.body.text(strip=True) or ""
    except Exception:
        log.debug("structured_extract.fallback.error", url=url)

    return ""
