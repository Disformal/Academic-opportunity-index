"""INSPIRE-HEP jobs through the search API, permitted fields only (docs/sources/inspire.md).

The regular crawl reads the open-jobs listing; the archive is swept once in deadline-year slices,
because the API refuses results past 10,000. A single job is fetched through the search endpoint
(`q=control_number:<id>`): the record endpoint /api/jobs/<id> ignores `fields` and returns
contact details, so no URL here ever points at it. A response holding a field we did not request
is refused (ForbiddenContent), so the extract task fails visibly instead of storing it as values.

Every page carries full records, so parse_detail turns each hit into a posting with its values;
each value points into the page (/hits/hits/<i>/...). A job without `urls` applies through its
INSPIRE page, derived from the control number with lower confidence.
"""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from aoi.adapters.protocol import (
    AssertionSpec,
    CrawlTargetSpec,
    Document,
    ListingPage,
    ParsedPosting,
    PostingRef,
)
from aoi.db.schema.core import Source
from aoi.db.schema.enums import AssertionMethod, OpportunityKind, PostingIdentityKind, TargetRole

BASE = "https://inspirehep.net/api/jobs"
PUBLIC_PAGE = "https://inspirehep.net/jobs/{}"
FIELDS = (
    "control_number",
    "position",
    "institutions",
    "ranks",
    "arxiv_categories",
    "regions",
    "address",
    "deadline_date",
    "date_closed",
    "status",
    "urls",
    "accelerator_experiments",
    "external_job_identifier",
    "legacy_creation_date",
)
_QUERY_TAIL = f"sort=mostrecent&size=250&page=1&fields={','.join(FIELDS)}"
OPEN_LISTING = f"{BASE}?q={quote('status:open')}&{_QUERY_TAIL}"
_API = Decimal("1")
_DERIVED = Decimal("0.5")


class ForbiddenContent(Exception):
    """The response holds a field we did not request; it must not become values."""


def job_url(control_number: str) -> str:
    """One job through the search endpoint, which honours `fields`."""
    return f"{BASE}?q={quote(f'control_number:{control_number}')}&fields={','.join(FIELDS)}"


def archive_url(year: int) -> str:
    """The first page of one deadline year of the archive."""
    query = quote(f"deadline_date:[{year}-01-01 TO {year}-12-31]")
    return f"{BASE}?q={query}&{_QUERY_TAIL}"


class InspireAdapter:
    name = "inspire"
    version = "1.0.0"

    def discover(self, source: Source) -> Iterator[CrawlTargetSpec]:
        yield CrawlTargetSpec(url=OPEN_LISTING, role=TargetRole.LISTING)

    def parse_listing(self, doc: Document) -> ListingPage:
        data = _load(doc)
        next_page = data.get("links", {}).get("next")
        targets = (CrawlTargetSpec(url=next_page, role=TargetRole.LISTING),) if next_page else ()
        return ListingPage(refs=tuple(_ref(hit) for hit in data["hits"]["hits"]), targets=targets)

    def parse_detail(self, doc: Document) -> list[ParsedPosting]:
        return [
            ParsedPosting(ref=_ref(hit), assertions=_assertions(hit, f"/hits/hits/{i}"))
            for i, hit in enumerate(_load(doc)["hits"]["hits"])
        ]


def _load(doc: Document) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(doc.body)
    for hit in data["hits"]["hits"]:
        unrequested = set(hit["metadata"]) - set(FIELDS)
        if unrequested:
            raise ForbiddenContent(f"{doc.url} holds unrequested fields {sorted(unrequested)}")
    return data


def _ref(hit: dict[str, Any]) -> PostingRef:
    control_number = str(hit["metadata"]["control_number"])
    return PostingRef(
        identity_kind=PostingIdentityKind.NATIVE_REF,
        identity_key=control_number,
        detail_url=job_url(control_number),
        kind=OpportunityKind.JOB,
    )


def _assertions(hit: dict[str, Any], at: str) -> tuple[AssertionSpec, ...]:
    md = hit["metadata"]
    meta = f"{at}/metadata"
    found: list[AssertionSpec] = []

    def add(field: str, value: Any, pointer: str, raw: str | None = None) -> None:
        if value not in (None, "", []):
            found.append(_spec(field, value, pointer, raw, _API))

    add("title", md.get("position"), f"{meta}/position", md.get("position"))
    add("status", md.get("status"), f"{meta}/status", md.get("status"))
    add("deadline", md.get("deadline_date"), f"{meta}/deadline_date", md.get("deadline_date"))
    add("closed_at", md.get("date_closed"), f"{meta}/date_closed", md.get("date_closed"))
    if md.get("legacy_creation_date"):
        legacy = md["legacy_creation_date"]
        add("posted_at", legacy, f"{meta}/legacy_creation_date", legacy)
    else:
        add("posted_at", hit.get("created"), f"{at}/created", hit.get("created"))
    if md.get("urls"):
        url = md["urls"][0]["value"]
        add("application_url", url, f"{meta}/urls/0/value", url)
    else:
        number = str(md["control_number"])
        found.append(
            _spec(
                "application_url",
                PUBLIC_PAGE.format(number),
                f"{meta}/control_number",
                number,
                _DERIVED,
            )
        )
    institutions = md.get("institutions", [])
    add("organizations", [i["value"] for i in institutions], f"{meta}/institutions")
    # Aligned with organizations; null where an institution has no INSPIRE record.
    ids = [i["record"]["$ref"].rsplit("/", 1)[1] if "record" in i else None for i in institutions]
    if any(ids):
        add("inspire_institution_ids", ids, f"{meta}/institutions")
    add("career_stages", md.get("ranks"), f"{meta}/ranks")
    add("arxiv_categories", md.get("arxiv_categories"), f"{meta}/arxiv_categories")
    add("regions", md.get("regions"), f"{meta}/regions")
    add("address", md.get("address"), f"{meta}/address")
    add(
        "experiments",
        [e["name"] for e in md.get("accelerator_experiments", []) if "name" in e],
        f"{meta}/accelerator_experiments",
    )
    external = md.get("external_job_identifier")
    add("external_job_identifier", external, f"{meta}/external_job_identifier", external)
    return tuple(found)


def _spec(
    field: str, value: Any, pointer: str, raw: str | None, confidence: Decimal
) -> AssertionSpec:
    return AssertionSpec(
        field=field,
        value=value,
        method=AssertionMethod.OFFICIAL_API,
        confidence=confidence,
        raw=raw,
        pointer=pointer,
    )
