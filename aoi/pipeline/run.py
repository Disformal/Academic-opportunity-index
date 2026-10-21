"""Pipeline orchestration — connects adapters to the database.

Usage::

    uv run python -m aoi.pipeline.run --source inspire
    uv run python -m aoi.pipeline.run --source euraxess

Each invocation:
1. Harvests raw items from the source adapter (network I/O + S3 evidence storage).
2. For each item, persists: CrawlUrl → Fetch → SourceDocument → ExtractionRun →
   resolves identity → creates/updates Opportunity → writes FieldAssertions →
   creates OpportunityVersion if fields changed.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import structlog
import yaml
from lingua import Language, LanguageDetectorBuilder
from sqlalchemy.orm import Session

from aoi.db.enums import (
    ClosureReason,
    Derivation,
    FetchOutcome,
    IdentityKind,
    OpportunityStatus,
    PageRole,
)
from aoi.db.models import (
    CrawlUrl,
    ExtractionRun,
    Fetch,
    FieldAssertion,
    Opportunity,
    PipelineRun,
    RobotsAssessment,
    Source,
    SourceDocument,
)
from aoi.pipeline.assertions import assert_field
from aoi.pipeline.canonicalize import canonicalize, url_hash
from aoi.pipeline.circuit_breaker import check as breaker_check
from aoi.pipeline.closure import decide as closure_decide
from aoi.pipeline.fetch_cache import touch_verified_at
from aoi.pipeline.identity import IdentityInputs, resolve
from aoi.pipeline.normalize import normalize_deadline
from aoi.pipeline.oai_pmh import OAIRecord
from aoi.pipeline.sources.euraxess import RawOpportunity
from aoi.pipeline.sources.inspire import RawJob
from aoi.pipeline.versioning import maybe_version
from aoi.storage.evidence import store_text

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Job type dictionary (loaded once)
# ---------------------------------------------------------------------------

_JOB_TYPE_DICT: dict[str, str] | None = None


def _load_job_type_dict() -> dict[str, str]:
    """Load ontology/job_types.yaml and build a lowercase-phrase → canonical-type map."""
    global _JOB_TYPE_DICT
    if _JOB_TYPE_DICT is not None:
        return _JOB_TYPE_DICT

    yaml_path = Path(__file__).resolve().parent.parent.parent / "ontology" / "job_types.yaml"
    raw: dict[str, list[str]] = yaml.safe_load(yaml_path.read_text())

    lookup: dict[str, str] = {}
    for canonical, phrases in raw.items():
        for phrase in phrases:
            lookup[phrase.lower()] = canonical

    _JOB_TYPE_DICT = lookup
    return lookup


def normalize_job_type(raw_type: str) -> str:
    """Map a raw job type string to its canonical form using the dictionary."""
    if not raw_type:
        return "other"
    lookup = _load_job_type_dict()
    return lookup.get(raw_type.lower(), "other")


# ---------------------------------------------------------------------------
# Language detection (singleton detector)
# ---------------------------------------------------------------------------

_DETECTOR = LanguageDetectorBuilder.from_languages(
    Language.ENGLISH,
    Language.GERMAN,
    Language.FRENCH,
    Language.SPANISH,
    Language.ITALIAN,
    Language.DUTCH,
    Language.PORTUGUESE,
    Language.SWEDISH,
).build()

_LANG_CODE_MAP = {lang: lang.iso_code_639_1.name.lower() for lang in Language.all()}


def detect_language(text: str) -> str:
    """Detect the primary language of a text. Returns ISO 639-1 code or 'en' as default."""
    if not text or len(text) < 20:
        return "en"
    detected = _DETECTOR.detect_language_of(text)
    if detected is None:
        return "en"
    return _LANG_CODE_MAP.get(detected, "en")


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Summary of a pipeline run."""

    source: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    closed: int = 0
    quarantined: int = 0


# ---------------------------------------------------------------------------
# DB record helpers
# ---------------------------------------------------------------------------


def _ensure_crawl_url(
    session: Session,
    source: Source,
    url: str,
    page_role: PageRole,
) -> CrawlUrl:
    """Get or create a CrawlUrl for the given URL."""
    canonical = canonicalize(url)
    h = url_hash(canonical)

    existing = session.query(CrawlUrl).filter_by(source_id=source.id, url_hash=h).first()
    if existing is not None:
        existing.last_fetched_at = datetime.now(UTC)
        return existing

    crawl_url = CrawlUrl(
        source_id=source.id,
        url_canonical=canonical,
        url_original=url,
        url_hash=h,
        page_role=page_role,
        last_fetched_at=datetime.now(UTC),
    )
    session.add(crawl_url)
    session.flush()
    return crawl_url


def _record_fetch(
    session: Session,
    crawl_url: CrawlUrl,
    source: Source,
    content_digest: str,
) -> Fetch:
    """Record a successful fetch in the database."""
    f = Fetch(
        crawl_url_id=crawl_url.id,
        source_id=source.id,
        fetched_at=datetime.now(UTC),
        http_status=200,
        outcome=FetchOutcome.HIT,
        content_digest=content_digest,
    )
    session.add(f)
    session.flush()
    return f


def _record_source_document(
    session: Session,
    fetch: Fetch,
    source: Source,
    content_digest: str,
    media_type: str,
    document_text: str,
    language: str,
) -> SourceDocument:
    """Record a source document and persist extracted text to S3."""
    text_key = store_text(content_digest, document_text)

    sdoc = SourceDocument(
        fetch_id=fetch.id,
        source_id=source.id,
        content_digest=content_digest,
        media_type=media_type,
        detected_language=language,
        text_object_key=text_key,
        char_count=len(document_text),
    )
    session.add(sdoc)
    session.flush()
    return sdoc


def _start_extraction_run(
    session: Session,
    sdoc: SourceDocument,
    extractor_id: str,
) -> ExtractionRun:
    """Start an extraction run."""
    erun = ExtractionRun(
        source_document_id=sdoc.id,
        extractor_id=extractor_id,
        extractor_version=version("aoi"),
        started_at=datetime.now(UTC),
    )
    session.add(erun)
    session.flush()
    return erun


def _finish_extraction_run(
    erun: ExtractionRun,
    items_emitted: int,
    outcome: str = "success",
) -> None:
    """Mark an extraction run as finished."""
    erun.finished_at = datetime.now(UTC)
    erun.items_emitted = items_emitted
    erun.outcome = outcome


def _upsert_opportunity(
    session: Session,
    identity_kind: IdentityKind,
    identity_key: str,
    title: str,
    job_type: str,
    status: OpportunityStatus,
    language: str,
    deadline: datetime | None = None,
    deadline_precision: str | None = None,
    country: str | None = None,
    application_url: str | None = None,
) -> tuple[Opportunity, bool]:
    """Insert or update an Opportunity. Returns (opportunity, is_new)."""
    existing = (
        session.query(Opportunity)
        .filter_by(identity_kind=identity_kind, identity_key=identity_key)
        .first()
    )
    now = datetime.now(UTC)

    if existing is not None:
        existing.last_seen_at = now
        existing.title = title
        existing.type = job_type
        existing.status = status
        existing.primary_language = language
        existing.deadline = deadline
        existing.deadline_precision = deadline_precision
        existing.country = country
        existing.application_url = application_url
        # ck_closed_consistent: (status = 'closed') = (closed_at IS NOT NULL)
        if status == OpportunityStatus.CLOSED and existing.closed_at is None:
            existing.closed_at = now
        elif status != OpportunityStatus.CLOSED:
            existing.closed_at = None
        session.flush()
        return existing, False

    opp = Opportunity(
        identity_kind=identity_kind,
        identity_key=identity_key,
        title=title,
        type=job_type,
        status=status,
        primary_language=language,
        deadline=deadline,
        deadline_precision=deadline_precision,
        country=country,
        application_url=application_url,
        first_seen_at=now,
        last_seen_at=now,
        closed_at=now if status == OpportunityStatus.CLOSED else None,
    )
    session.add(opp)
    session.flush()
    return opp, True


# ---------------------------------------------------------------------------
# INSPIRE field extraction
# ---------------------------------------------------------------------------

# Map INSPIRE status values to OpportunityStatus
_INSPIRE_STATUS_MAP: dict[str, OpportunityStatus] = {
    "open": OpportunityStatus.OPEN,
    "closed": OpportunityStatus.CLOSED,
    "pending": OpportunityStatus.UPCOMING,
}


def _inspire_country(raw: RawJob) -> str | None:
    """Extract a 2-letter country code from INSPIRE regions, if possible."""
    regions = raw.payload.get("regions", [])
    # INSPIRE uses region names, not ISO codes. We cannot reliably map
    # multi-country regions to a single code. Return None and let the
    # field assertion carry the raw region string.
    if not regions:
        return None
    # Single-country shorthand for common cases
    _REGION_MAP: dict[str, str] = {
        "Europe": None,  # type: ignore[dict-item]
        "North America": None,  # type: ignore[dict-item]
        "Asia": None,  # type: ignore[dict-item]
    }
    first = regions[0] if isinstance(regions, list) else regions
    return _REGION_MAP.get(first)


def process_inspire_item(
    session: Session,
    source: Source,
    raw: RawJob,
) -> Opportunity:
    """Process a single INSPIRE RawJob into the database."""
    # DB bookkeeping: CrawlUrl, Fetch, SourceDocument, ExtractionRun
    crawl_url = _ensure_crawl_url(
        session,
        source,
        f"https://inspirehep.net/api/jobs/{raw.control_number}",
        PageRole.DETAIL,
    )
    fetch = _record_fetch(session, crawl_url, source, raw.content_digest)

    language = detect_language(raw.document_text)

    sdoc = _record_source_document(
        session,
        fetch,
        source,
        raw.content_digest,
        media_type="application/json",
        document_text=raw.document_text,
        language=language,
    )

    erun = _start_extraction_run(session, sdoc, "inspire")

    # Identity resolution
    ranks = raw.payload.get("ranks", [])
    rank_str = ranks[0] if ranks else ""
    job_type = normalize_job_type(rank_str)

    # Application URL from urls array
    urls = raw.payload.get("urls", [])
    app_url = urls[0]["value"] if urls else None

    inputs = IdentityInputs(
        native_ref=raw.control_number,
        application_url=app_url,
        detail_url=f"https://inspirehep.net/jobs/{raw.control_number}",
        title=raw.payload.get("position", ""),
        organization=_inspire_institution(raw),
        country=_inspire_country(raw),
        deadline=raw.payload.get("deadline_date"),
    )
    identity_kind, identity_key = resolve(inputs)

    # Deadline normalization
    raw_deadline = raw.payload.get("deadline_date")
    deadline_dt = None
    deadline_precision = None
    if raw_deadline:
        nd = normalize_deadline(raw_deadline)
        if nd is not None:
            deadline_dt = nd.value
            deadline_precision = nd.precision

    # Status
    raw_status = raw.payload.get("status", "")
    status = _INSPIRE_STATUS_MAP.get(raw_status, OpportunityStatus.UNKNOWN)

    # Upsert opportunity
    opp, is_new = _upsert_opportunity(
        session,
        identity_kind=identity_kind,
        identity_key=identity_key,
        title=raw.payload.get("position", "Untitled"),
        job_type=job_type,
        status=status,
        language=language,
        deadline=deadline_dt,
        deadline_precision=deadline_precision,
        country=_inspire_country(raw),
        application_url=app_url,
    )

    # Write field assertions
    items = 0

    def _assert(field_path: str, value: Any, raw_value: str | None, **kwargs: Any) -> None:
        nonlocal items
        assert_field(
            session,
            opportunity_id=opp.id,
            field_path=field_path,
            value=value,
            raw_value=raw_value,
            confidence=kwargs.get("confidence", 0.95),
            derivation=Derivation.OFFICIAL_API,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            json_pointer=kwargs.get("json_pointer"),
            text_start=kwargs.get("text_start"),
            text_end=kwargs.get("text_end"),
        )
        items += 1

    _assert(
        "title",
        raw.payload.get("position", ""),
        raw.payload.get("position"),
        json_pointer=raw.pointers.get("position"),
    )
    _assert("type", job_type, rank_str, json_pointer=raw.pointers.get("ranks"))
    _assert("status", raw_status, raw_status, json_pointer=raw.pointers.get("status"))

    if raw_deadline:
        _assert(
            "deadline", raw_deadline, raw_deadline, json_pointer=raw.pointers.get("deadline_date")
        )

    institution = _inspire_institution(raw)
    if institution:
        _assert(
            "organization", institution, institution, json_pointer=raw.pointers.get("institutions")
        )

    country = _inspire_country(raw)
    regions = raw.payload.get("regions", [])
    if regions:
        _assert(
            "country",
            country or regions[0],
            regions[0] if isinstance(regions, list) else regions,
            json_pointer=raw.pointers.get("regions"),
        )

    if app_url:
        _assert("application_url", app_url, app_url, json_pointer="/metadata/urls/0/value")

    _finish_extraction_run(erun, items)

    # Versioning
    maybe_version(
        session,
        opp.id,
        extraction_run_id=erun.id,
        change_reason="new_extraction" if is_new else "re_extraction",
    )

    return opp


def _inspire_institution(raw: RawJob) -> str | None:
    """Extract institution name from INSPIRE payload."""
    institutions = raw.payload.get("institutions", [])
    if institutions and isinstance(institutions, list):
        result: str | None = institutions[0].get("value")
        return result
    return None


# ---------------------------------------------------------------------------
# EURAXESS field extraction
# ---------------------------------------------------------------------------

# Map EURAXESS country names to ISO 3166-1 alpha-2 (common cases)
_COUNTRY_MAP: dict[str, str] = {
    "austria": "AT",
    "belgium": "BE",
    "bulgaria": "BG",
    "croatia": "HR",
    "cyprus": "CY",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "ireland": "IE",
    "italy": "IT",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "netherlands": "NL",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "slovakia": "SK",
    "slovenia": "SI",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "united kingdom": "GB",
    "norway": "NO",
    "iceland": "IS",
    "turkey": "TR",
    "israel": "IL",
}


def _euraxess_country(raw: RawOpportunity) -> str | None:
    """Map EURAXESS country name to ISO 3166-1 alpha-2."""
    country_name = raw.fields.get("Country", "")
    return _COUNTRY_MAP.get(country_name.lower())


def process_euraxess_item(
    session: Session,
    source: Source,
    raw: RawOpportunity,
) -> Opportunity:
    """Process a single EURAXESS RawOpportunity into the database."""
    crawl_url = _ensure_crawl_url(session, source, raw.detail_url, PageRole.DETAIL)
    fetch = _record_fetch(session, crawl_url, source, raw.content_digest)

    language = detect_language(raw.document_text)

    sdoc = _record_source_document(
        session,
        fetch,
        source,
        raw.content_digest,
        media_type="text/html",
        document_text=raw.document_text,
        language=language,
    )

    erun = _start_extraction_run(session, sdoc, "euraxess")

    # Type from Researcher Profile
    researcher_profile = raw.fields.get("Researcher Profile", "")
    job_type = normalize_job_type(researcher_profile)

    # Identity resolution
    inputs = IdentityInputs(
        native_ref=raw.job_id,
        application_url=raw.application_url,
        detail_url=raw.detail_url,
        title=raw.title,
        organization=raw.fields.get("Organisation/Company") or raw.fields.get("Organisation"),
        country=_euraxess_country(raw),
        deadline=raw.deadline_iso,
    )
    identity_kind, identity_key = resolve(inputs)

    # Deadline
    deadline_dt = None
    deadline_precision = None
    if raw.deadline_iso:
        nd = normalize_deadline(raw.deadline_iso)
        if nd is not None:
            deadline_dt = nd.value
            deadline_precision = nd.precision

    country = _euraxess_country(raw)

    opp, is_new = _upsert_opportunity(
        session,
        identity_kind=identity_kind,
        identity_key=identity_key,
        title=raw.title,
        job_type=job_type,
        status=OpportunityStatus.OPEN,
        language=language,
        deadline=deadline_dt,
        deadline_precision=deadline_precision,
        country=country,
        application_url=raw.application_url,
    )

    # Write field assertions
    items = 0

    def _assert(
        field_path: str,
        value: Any,
        raw_value: str | None,
        derivation: Derivation = Derivation.PARSER,
        **kwargs: Any,
    ) -> None:
        nonlocal items
        assert_field(
            session,
            opportunity_id=opp.id,
            field_path=field_path,
            value=value,
            raw_value=raw_value,
            confidence=kwargs.get("confidence", 0.90),
            derivation=derivation,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            json_pointer=kwargs.get("json_pointer"),
            text_start=kwargs.get("text_start"),
            text_end=kwargs.get("text_end"),
        )
        items += 1

    # Title — use field_positions if available
    title_pos = raw.field_positions.get("title")
    _assert(
        "title",
        raw.title,
        raw.title,
        text_start=title_pos[0] if title_pos else 0,
        text_end=title_pos[1] if title_pos else len(raw.title),
    )

    # Type from Researcher Profile
    rp_pos = raw.field_positions.get("Researcher Profile")
    _assert(
        "type",
        job_type,
        researcher_profile,
        text_start=rp_pos[0] if rp_pos else None,
        text_end=rp_pos[1] if rp_pos else None,
        json_pointer=None if rp_pos else "/fields/Researcher Profile",
    )

    # Deadline
    if raw.deadline_iso:
        _assert(
            "deadline",
            raw.deadline_iso,
            raw.deadline_iso,
            derivation=Derivation.STRUCTURED_MARKUP,
            json_pointer="/deadline_iso",
        )

    # Organization — EURAXESS changed label from "Organisation" to "Organisation/Company"
    org_label = "Organisation/Company" if "Organisation/Company" in raw.fields else "Organisation"
    org = raw.fields.get(org_label)
    if org:
        org_pos = raw.field_positions.get(org_label)
        _assert(
            "organization",
            org,
            org,
            text_start=org_pos[0] if org_pos else None,
            text_end=org_pos[1] if org_pos else None,
            json_pointer=None if org_pos else f"/fields/{org_label}",
        )

    # Country
    country_name = raw.fields.get("Country")
    if country_name:
        country_pos = raw.field_positions.get("Country")
        _assert(
            "country",
            country or country_name,
            country_name,
            text_start=country_pos[0] if country_pos else None,
            text_end=country_pos[1] if country_pos else None,
            json_pointer=None if country_pos else "/fields/Country",
        )

    # Application URL
    if raw.application_url:
        _assert(
            "application_url",
            raw.application_url,
            raw.application_url,
            derivation=Derivation.PARSER,
            text_start=0,
            text_end=len(raw.application_url),
        )

    _finish_extraction_run(erun, items)

    # Versioning
    maybe_version(
        session,
        opp.id,
        extraction_run_id=erun.id,
        change_reason="new_extraction" if is_new else "re_extraction",
    )

    return opp


# ---------------------------------------------------------------------------
# Closure pass
# ---------------------------------------------------------------------------


def _closure_pass(
    session: Session,
    source: Source,
    seen_ids: set[uuid.UUID],
    result: RunResult,
) -> None:
    """Evaluate unseen open opportunities for closure, then apply via circuit breaker.

    After the harvest loop, opportunities from this source that were NOT seen
    are candidates for closure.  For each candidate we gather available signals
    (deadline, consecutive_absences) and call the closure decision function.

    The circuit breaker prevents catastrophic mass-closures caused by parser
    breakage or layout changes.
    """
    # Find open opportunities linked to this source that weren't seen
    unseen = (
        session.query(Opportunity)
        .filter(
            Opportunity.status != OpportunityStatus.CLOSED,
            ~Opportunity.id.in_(seen_ids) if seen_ids else Opportunity.id.isnot(None),
        )
        .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
        .join(ExtractionRun, FieldAssertion.extraction_run_id == ExtractionRun.id)
        .join(SourceDocument, ExtractionRun.source_document_id == SourceDocument.id)
        .filter(SourceDocument.source_id == source.id)
        .distinct()
        .all()
    )

    if not unseen:
        return

    # Evaluate each candidate
    closure_candidates: list[tuple[Opportunity, str]] = []
    now = datetime.now(UTC)

    for opp in unseen:
        decision = closure_decide(
            http_status=None,
            confirmed_gone_twice=False,
            consecutive_absences=0,
            body_text=None,
            language=opp.primary_language,
            deadline=opp.deadline,
            now=now,
            looks_like_soft_404=False,
        )
        if decision.closed and decision.reason is not None:
            closure_candidates.append((opp, decision.reason.value))
            log.info(
                "closure.candidate",
                opportunity_id=str(opp.id),
                reason=decision.reason.value,
                confidence=decision.confidence,
                note=decision.note,
            )

    if not closure_candidates:
        return

    # Count active opportunities from this source (for circuit breaker)
    active_count = (
        session.query(Opportunity)
        .filter(Opportunity.status != OpportunityStatus.CLOSED)
        .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
        .join(ExtractionRun, FieldAssertion.extraction_run_id == ExtractionRun.id)
        .join(SourceDocument, ExtractionRun.source_document_id == SourceDocument.id)
        .filter(SourceDocument.source_id == source.id)
        .distinct()
        .count()
    )

    breaker = breaker_check(
        active_before=active_count,
        would_close=len(closure_candidates),
        source_name=source.name,
    )

    if not breaker.allowed:
        log.warning("closure.breaker_triggered", message=breaker.message)
        result.quarantined += len(closure_candidates)
        return

    log.info("closure.breaker_ok", message=breaker.message)

    # Apply closures
    for opp, reason_value in closure_candidates:
        opp.status = OpportunityStatus.CLOSED
        opp.closed_at = now
        opp.closure_reason = ClosureReason(reason_value)
        result.closed += 1
        log.info(
            "closure.applied",
            opportunity_id=str(opp.id),
            reason=reason_value,
        )

    session.flush()


# ---------------------------------------------------------------------------
# Harvest wrappers (thin shims for mocking in tests)
# ---------------------------------------------------------------------------


def _harvest_inspire(
    source: Source,
    session: Session,
    early_stop_consecutive: int = 10,
) -> Iterator[RawJob]:
    """Harvest from the INSPIRE API with incremental early-stop."""
    from aoi.pipeline.sources.inspire import harvest

    # Query known identity keys for this source via provenance chain
    known_keys = frozenset(
        row[0]
        for row in session.query(Opportunity.identity_key)
        .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
        .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
        .where(SourceDocument.source_id == source.id)
        .distinct()
        .all()
    )

    log.info("pipeline.known_keys", source=source.name, count=len(known_keys))

    yield from harvest(
        source.id,
        known_keys=known_keys if known_keys else None,
        early_stop_consecutive=early_stop_consecutive,
        session=session,
    )


def _harvest_euraxess(
    source: Source,
    session: Session,
    early_stop_consecutive: int = 10,
) -> Iterator[RawOpportunity]:
    """Harvest from EURAXESS, passing known IDs for early-stop."""
    from aoi.pipeline.sources.euraxess import harvest

    # Query existing EURAXESS job IDs via provenance chain
    # identity_key for EURAXESS is "euraxess:<job_id>" — extract the job_id part
    known_keys = frozenset(
        row[0]
        for row in session.query(Opportunity.identity_key)
        .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
        .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
        .where(SourceDocument.source_id == source.id)
        .distinct()
        .all()
    )

    # EURAXESS harvest expects bare job IDs (e.g. "468285"), not identity keys
    # identity_key format is "euraxess:<job_id>"
    known_ids = frozenset(k.split(":", 1)[1] for k in known_keys if k.startswith("euraxess:"))

    log.info("pipeline.known_ids", source=source.name, count=len(known_ids))

    yield from harvest(
        source.id,
        known_ids=known_ids if known_ids else None,
        early_stop_consecutive=early_stop_consecutive,
        session=session,
    )


# ---------------------------------------------------------------------------
# OAI-PMH field extraction
# ---------------------------------------------------------------------------


def _oai_application_url(record: OAIRecord) -> str | None:
    """Return the first http/https URI from dc:identifier, or None."""
    for v in record.metadata.get("identifier", []):
        if v.startswith(("http://", "https://")):
            return v
    return None


def process_oai_record(
    session: Session,
    source: "Source",
    record: OAIRecord,
) -> Opportunity:
    """Process a single OAI-PMH OAIRecord into the database."""
    # Build a stable URL for the CrawlUrl row from the OAI identifier
    detail_url = _oai_application_url(record) or f"oai:{record.identifier}"

    crawl_url = _ensure_crawl_url(session, source, detail_url, PageRole.DETAIL)
    fetch_record = _record_fetch(session, crawl_url, source, record.content_digest)

    # Document text: concatenate all metadata values
    doc_parts: list[str] = []
    for values in record.metadata.values():
        doc_parts.extend(values)
    document_text = "\n".join(doc_parts)

    language = detect_language(document_text)

    sdoc = _record_source_document(
        session,
        fetch_record,
        source,
        record.content_digest,
        media_type="application/xml",
        document_text=document_text,
        language=language,
    )

    erun = _start_extraction_run(session, sdoc, "oai_pmh")

    # Field extraction from Dublin Core
    titles = record.metadata.get("title", [])
    title = titles[0] if titles else record.identifier

    descriptions = record.metadata.get("description", [])
    description = descriptions[0] if descriptions else ""

    publishers = record.metadata.get("publisher", [])
    organization = publishers[0] if publishers else None

    dates = record.metadata.get("date", [])
    raw_date = dates[0] if dates else None

    app_url = _oai_application_url(record)

    coverage = record.metadata.get("coverage", [])
    raw_country = coverage[0] if coverage else None

    job_type = normalize_job_type("")

    # Deadline normalization
    deadline_dt = None
    deadline_precision = None
    if raw_date:
        nd = normalize_deadline(raw_date)
        if nd is not None:
            deadline_dt = nd.value
            deadline_precision = nd.precision
        else:
            deadline_precision = "approximate"

    # Identity resolution
    inputs = IdentityInputs(
        native_ref=record.identifier,
        application_url=app_url,
        detail_url=detail_url,
        title=title,
        organization=organization,
        country=None,
        deadline=raw_date,
    )
    identity_kind, identity_key = resolve(inputs)

    opp, is_new = _upsert_opportunity(
        session,
        identity_kind=identity_kind,
        identity_key=identity_key,
        title=title,
        job_type=job_type,
        status=OpportunityStatus.OPEN,
        language=language,
        deadline=deadline_dt,
        deadline_precision=deadline_precision,
        country=None,
        application_url=app_url,
    )

    # Write field assertions
    items = 0

    def _assert(field_path: str, value: Any, raw_value: str | None, **kwargs: Any) -> None:
        nonlocal items
        assert_field(
            session,
            opportunity_id=opp.id,
            field_path=field_path,
            value=value,
            raw_value=raw_value,
            confidence=kwargs.get("confidence", 0.90),
            derivation=Derivation.OFFICIAL_API,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            json_pointer=kwargs.get("json_pointer"),
        )
        items += 1

    _assert("title", title, title, json_pointer="/metadata/title/0")

    if description:
        _assert("description", description, description, json_pointer="/metadata/description/0")

    if raw_date:
        _assert("deadline", raw_date, raw_date, json_pointer="/metadata/date/0")

    if organization:
        _assert("organization", organization, organization, json_pointer="/metadata/publisher/0")

    if raw_country:
        _assert("country", raw_country, raw_country, json_pointer="/metadata/coverage/0")

    if app_url:
        _assert("application_url", app_url, app_url, json_pointer="/metadata/identifier")

    _finish_extraction_run(erun, items)

    maybe_version(
        session,
        opp.id,
        extraction_run_id=erun.id,
        change_reason="new_extraction" if is_new else "re_extraction",
    )

    return opp


def _harvest_oai(source: "Source") -> "Iterator[OAIRecord]":
    """Harvest from an OAI-PMH endpoint using source.oai_pmh_endpoint."""
    from aoi.pipeline.oai_pmh import harvest_oai

    endpoint = source.oai_pmh_endpoint
    if not endpoint:
        raise ValueError(f"Source {source.name!r} has no oai_pmh_endpoint configured.")

    yield from harvest_oai(
        endpoint,
        from_date=source.last_harvested_at,
        source_id=source.id,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_source(
    session: Session,
    source_name: str,
    *,
    limit: int = 0,
    triggered_by: str = "cli",
    run_id: uuid.UUID | None = None,
) -> RunResult:
    """Run the full pipeline for a named source.

    Args:
        session: Active database session.
        source_name: "inspire" or "euraxess".
        limit: Stop after processing this many items (0 = unlimited).
        triggered_by: Who triggered this run ("cli", "api", or "cron").
        run_id: If provided, use an existing PipelineRun row instead of creating one.

    Returns:
        RunResult with counts of created/updated/unchanged/errors.
    """
    source = session.query(Source).filter_by(name=source_name).first()
    if source is None:
        raise ValueError(f"Unknown source: {source_name!r}. Create the Source row first.")

    # Create or load pipeline run record
    if run_id is not None:
        pipeline_run = session.query(PipelineRun).filter_by(id=run_id).first()
        if pipeline_run is None:
            raise ValueError(f"PipelineRun {run_id!r} not found.")
    else:
        pipeline_run = PipelineRun(
            source_id=source.id,
            started_at=datetime.now(UTC),
            status="running",
            triggered_by=triggered_by,
        )
        session.add(pipeline_run)
        session.flush()

    result = RunResult(source=source_name)
    seen_ids: set[uuid.UUID] = set()

    log.info(
        "pipeline.run.start",
        source=source_name,
        source_id=str(source.id),
        run_id=str(pipeline_run.id),
    )

    processed = 0

    try:
        if source_name == "inspire":
            for inspire_item in _harvest_inspire(source, session):
                try:
                    opp = process_inspire_item(session, source, inspire_item)
                    seen_ids.add(opp.id)
                    _tally(session, result, opp)
                except Exception:
                    session.rollback()
                    log.exception(
                        "pipeline.run.item_error",
                        source=source_name,
                        control_number=inspire_item.control_number,
                    )
                    result.errors += 1
                processed += 1
                if limit and processed >= limit:
                    log.info("pipeline.run.limit_reached", limit=limit)
                    break

        elif source_name == "euraxess":
            for euraxess_item in _harvest_euraxess(source, session):
                if euraxess_item.not_modified:
                    # 304: assertions still valid; touch last_verified_at, no new records
                    _handle_not_modified(
                        session, source, euraxess_item.detail_url, result, seen_ids
                    )
                    processed += 1
                    if limit and processed >= limit:
                        log.info("pipeline.run.limit_reached", limit=limit)
                        break
                    continue
                try:
                    opp = process_euraxess_item(session, source, euraxess_item)
                    seen_ids.add(opp.id)
                    _tally(session, result, opp)
                except Exception:
                    session.rollback()
                    log.exception(
                        "pipeline.run.item_error",
                        source=source_name,
                        job_id=euraxess_item.job_id,
                    )
                    result.errors += 1
                processed += 1
                if limit and processed >= limit:
                    log.info("pipeline.run.limit_reached", limit=limit)
                    break

        elif source_name.startswith("oai:"):
            for oai_record in _harvest_oai(source):
                try:
                    opp = process_oai_record(session, source, oai_record)
                    seen_ids.add(opp.id)
                    _tally(session, result, opp)
                except Exception:
                    session.rollback()
                    log.exception(
                        "pipeline.run.item_error",
                        source=source_name,
                        identifier=oai_record.identifier,
                    )
                    result.errors += 1
                processed += 1
                if limit and processed >= limit:
                    log.info("pipeline.run.limit_reached", limit=limit)
                    break

        else:
            raise ValueError(f"Unknown source: {source_name!r}")

        # Closure pass: evaluate unseen opportunities
        _closure_pass(session, source, seen_ids, result)

        # Record harvest timestamp for incremental mode
        source.last_harvested_at = datetime.now(UTC)

        # Finalize pipeline run — success
        pipeline_run.finished_at = datetime.now(UTC)
        pipeline_run.status = "completed"
        pipeline_run.created = result.created
        pipeline_run.updated = result.updated
        pipeline_run.unchanged = result.unchanged
        pipeline_run.errors = result.errors
        pipeline_run.closed = result.closed
        pipeline_run.quarantined = result.quarantined
        session.flush()

    except Exception as exc:
        pipeline_run.finished_at = datetime.now(UTC)
        pipeline_run.status = "failed"
        pipeline_run.error_message = str(exc)
        session.flush()
        raise

    log.info(
        "pipeline.run.done",
        source=source_name,
        run_id=str(pipeline_run.id),
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        closed=result.closed,
        quarantined=result.quarantined,
        errors=result.errors,
    )

    return result


# ---------------------------------------------------------------------------
# Robots assessment persistence
# ---------------------------------------------------------------------------

_SOURCE_DOMAINS: dict[str, str] = {
    "inspire": "inspirehep.net",
    "euraxess": "euraxess.ec.europa.eu",
}


def _persist_robots_assessment(session: Session, source: Source) -> None:
    """Fetch robots.txt for the source domain and persist the result."""
    from aoi.config import get_settings
    from aoi.pipeline.robots import assess_robots

    domain = _SOURCE_DOMAINS.get(source.name)
    if domain is None:
        log.warning("robots.unknown_domain", source=source.name)
        return

    settings = get_settings()
    result = assess_robots(
        source_id=str(source.id),
        domain=domain,
        user_agent=settings.user_agent,
    )

    assessment = RobotsAssessment(
        source_id=source.id,
        domain=domain,
        fetched_at=datetime.now(UTC),
        robots_body=result["robots_body"],
        verdict=result["verdict"],
        rationale=result["rationale"],
        assessed_by="aoi.pipeline.robots",
    )
    session.add(assessment)
    session.flush()

    log.info(
        "robots.persisted",
        source=source.name,
        domain=domain,
        verdict=result["verdict"],
    )


# ---------------------------------------------------------------------------
# Deduplication pass
# ---------------------------------------------------------------------------


def run_dedup(session: Session) -> int:
    """Run a full deduplication pass across all opportunities.

    Finds candidate pairs, scores them, and auto-merges those that meet the
    threshold (probability >= 0.97 AND >= 2 strong signals).

    Returns the count of new merges created.
    """
    from aoi.pipeline.dedup import candidates, classify, merge
    from aoi.pipeline.dedup import score as dedup_score

    seen: set[tuple[str, str]] = set()
    merged = 0
    opps = session.query(Opportunity).all()

    log.info("dedup.run.start", total_opportunities=len(opps))

    for opp in opps:
        for other in candidates(session, opp):
            sorted_ids = sorted((str(opp.id), str(other.id)))
            ids: tuple[str, str] = (sorted_ids[0], sorted_ids[1])
            if ids in seen:
                continue
            seen.add(ids)
            sc = dedup_score(opp, other)
            if classify(sc) == "auto_merge":
                merge(session, opp, other, score=sc.probability, signals=sc.signals)
                merged += 1
                log.info(
                    "dedup.merged",
                    opp_a=str(opp.id),
                    opp_b=str(other.id),
                    score=sc.probability,
                )

    session.flush()
    log.info("dedup.run.done", merged=merged)
    return merged


def _handle_not_modified(
    session: Session,
    source: Source,
    detail_url: str,
    result: RunResult,
    seen_ids: set[uuid.UUID],
) -> None:
    """Handle a 304 Not Modified response for a single detail URL.

    Finds the most recent SourceDocument fetched from detail_url, updates
    last_verified_at on its current FieldAssertions, adds the linked
    Opportunity to seen_ids, and increments result.unchanged.

    This is a no-op if no previous Fetch exists for the URL (first-ever run
    should not produce a 304).
    """
    canonical = canonicalize(detail_url)
    h = url_hash(canonical)

    crawl_url = session.query(CrawlUrl).filter_by(source_id=source.id, url_hash=h).first()
    if crawl_url is None:
        log.debug("not_modified.no_crawl_url", url=detail_url)
        return

    last_fetch = (
        session.query(Fetch)
        .filter_by(crawl_url_id=crawl_url.id, outcome=FetchOutcome.HIT)
        .order_by(Fetch.fetched_at.desc())
        .first()
    )
    if last_fetch is None:
        log.debug("not_modified.no_previous_fetch", url=detail_url)
        return

    sdoc = session.query(SourceDocument).filter_by(fetch_id=last_fetch.id).first()
    if sdoc is None:
        log.debug("not_modified.no_source_document", fetch_id=str(last_fetch.id))
        return

    updated = touch_verified_at(session, sdoc.id)
    log.info(
        "not_modified.verified",
        url=detail_url,
        source_document_id=str(sdoc.id),
        assertions_touched=updated,
    )

    # Find the opportunity linked to this source document and add to seen_ids
    opp_id_row = (
        session.query(FieldAssertion.opportunity_id).filter_by(source_document_id=sdoc.id).first()
    )
    if opp_id_row is not None:
        seen_ids.add(opp_id_row[0])

    result.unchanged += 1


def _tally(session: Session, result: RunResult, opp: Opportunity) -> None:
    """Update the result counters based on latest versioning state."""
    # Check if a version was just created (in the current session)
    from aoi.db.models import OpportunityVersion

    latest_version = (
        session.query(OpportunityVersion)
        .filter_by(opportunity_id=opp.id)
        .order_by(OpportunityVersion.version_number.desc())
        .first()
    )

    if latest_version is None:
        result.unchanged += 1
    elif latest_version.version_number == 1:
        result.created += 1
    else:
        result.updated += 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    from aoi.db.session import db_session

    parser = argparse.ArgumentParser(description="Run the AOI pipeline for a source.")
    parser.add_argument(
        "--source", required=True, help="Source to crawl (inspire, euraxess, or oai:<name>)."
    )
    parser.add_argument("--limit", type=int, default=0, help="Stop after N items (0 = unlimited).")
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="UUID of existing pipeline_run row (used by API trigger).",
    )
    args = parser.parse_args()

    run_uuid = uuid.UUID(args.run_id) if args.run_id else None

    with db_session() as session:
        run_result = run_source(session, args.source, limit=args.limit, run_id=run_uuid)
        session.commit()

    log.info("pipeline.cli.done", **vars(run_result))
