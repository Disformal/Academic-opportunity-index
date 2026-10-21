"""OAI-PMH harvester for AOI.

Implements OAI-PMH 2.0 ListRecords verb with resumption-token pagination.
Yields OAIRecord objects containing Dublin Core metadata.

Usage::

    from aoi.pipeline.oai_pmh import harvest_oai

    for record in harvest_oai(endpoint, source_id=source.id, from_date=last_harvested_at):
        process_oai_record(session, source, record, result)
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import structlog

from aoi.pipeline.fetch import fetch
from aoi.storage.evidence import store_fetch

log = structlog.get_logger()

NS_OAI = "http://www.openarchives.org/OAI/2.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class OAIRecord:
    """A single OAI-PMH record with Dublin Core metadata."""

    identifier: str  # e.g. oai:zenodo.org:12345
    datestamp: datetime  # from <header><datestamp>, always UTC-aware
    sets: list[str]
    metadata: dict[str, list[str]]  # dc field name → list of values
    fetch_id: uuid.UUID
    content_digest: str


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_datestamp(s: str) -> datetime:
    """Parse OAI-PMH datestamp to a UTC-aware datetime.

    Handles both ``YYYY-MM-DD`` (date granularity) and
    ``YYYY-MM-DDTHH:MM:SSZ`` (datetime granularity).
    """
    if not s:
        return datetime.now(UTC)
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    except ValueError:
        log.debug("oai_pmh.parse.bad_datestamp", raw=s)
        return datetime.now(UTC)


def parse_record(
    record_el: ET.Element,
    fetch_id: uuid.UUID,
    content_digest: str,
) -> OAIRecord | None:
    """Parse a single <record> OAI-PMH element into an OAIRecord.

    Returns None for deleted records (header status='deleted') or records
    with a missing header.
    """
    header = record_el.find(f"{{{NS_OAI}}}header")
    if header is None:
        return None

    if header.get("status") == "deleted":
        return None

    identifier_el = header.find(f"{{{NS_OAI}}}identifier")
    datestamp_el = header.find(f"{{{NS_OAI}}}datestamp")

    identifier = (identifier_el.text or "").strip() if identifier_el is not None else ""
    datestamp_str = (datestamp_el.text or "").strip() if datestamp_el is not None else ""
    datestamp = _parse_datestamp(datestamp_str)

    sets = [
        s.text.strip() for s in header.findall(f"{{{NS_OAI}}}setSpec") if s.text and s.text.strip()
    ]

    # Collect Dublin Core fields from anywhere inside <metadata>
    metadata: dict[str, list[str]] = {}
    metadata_el = record_el.find(f"{{{NS_OAI}}}metadata")
    if metadata_el is not None:
        for el in metadata_el.iter():
            if el.tag.startswith(f"{{{NS_DC}}}"):
                dc_field = el.tag[len(f"{{{NS_DC}}}") :]
                value = (el.text or "").strip()
                if value:
                    metadata.setdefault(dc_field, []).append(value)

    return OAIRecord(
        identifier=identifier,
        datestamp=datestamp,
        sets=sets,
        metadata=metadata,
        fetch_id=fetch_id,
        content_digest=content_digest,
    )


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------


def _build_url(endpoint: str, params: dict[str, str]) -> str:
    """Append OAI-PMH query parameters to the endpoint URL."""
    query = urlencode(params)
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{query}"


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


def harvest_oai(
    endpoint: str,
    *,
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    from_date: datetime | None = None,
    source_id: uuid.UUID,
    per_second: float = 1.0,
) -> Iterator[OAIRecord]:
    """Harvest an OAI-PMH endpoint, following resumption tokens.

    Stores every HTTP response in the evidence store (SeaweedFS).  Deleted
    records are silently skipped.  Pagination is handled automatically via
    the ``<resumptionToken>`` element — the first request uses verb/params,
    subsequent requests send only ``verb=ListRecords&resumptionToken=TOKEN``.

    Args:
        endpoint: Base OAI-PMH URL, e.g. ``https://zenodo.org/oai2d``.
        metadata_prefix: OAI metadata format.  ``oai_dc`` (Dublin Core) is
            the universal fallback supported by all OAI-PMH repositories.
        set_spec: Optional set identifier for filtered harvest.
        from_date: Harvest only records modified on or after this date.
            Enables incremental harvest when set to ``source.last_harvested_at``.
        source_id: UUID of the ``Source`` row (for evidence storage).
        per_second: Politeness rate limit.  Default 1 req/s.

    Yields:
        :class:`OAIRecord` for each non-deleted record.
    """
    params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date.date().isoformat()

    while True:
        url = _build_url(endpoint, params)
        log.info("oai_pmh.harvest.request", url=url)

        result = fetch(url, per_second=per_second, accept="application/xml")

        fetched_at = datetime.now(UTC)
        fetch_id = uuid.uuid4()

        evidence = store_fetch(
            source_id=str(source_id),
            fetch_id=str(fetch_id),
            url=url,
            request_headers={"Accept": "application/xml"},
            status=result.status,
            response_headers=result.headers,
            body=result.body,
            fetched_at=fetched_at,
        )

        if result.status != 200:
            log.warning("oai_pmh.harvest.bad_status", url=url, status=result.status)
            break

        try:
            root = ET.fromstring(result.body)
        except ET.ParseError:
            log.warning("oai_pmh.harvest.parse_error", url=url)
            break

        list_records = root.find(f"{{{NS_OAI}}}ListRecords")
        if list_records is None:
            log.warning("oai_pmh.harvest.no_list_records", url=url)
            break

        record_fetch_id = uuid.UUID(str(evidence.fetch_id))
        record_digest = evidence.content_digest

        for record_el in list_records.findall(f"{{{NS_OAI}}}record"):
            record = parse_record(record_el, record_fetch_id, record_digest)
            if record is not None:
                yield record

        token_el = list_records.find(f"{{{NS_OAI}}}resumptionToken")
        token = (token_el.text or "").strip() if token_el is not None else ""
        if not token:
            log.info("oai_pmh.harvest.done", url=url)
            break

        log.debug("oai_pmh.harvest.resumption_token", token=token[:40])
        params = {"verb": "ListRecords", "resumptionToken": token}
