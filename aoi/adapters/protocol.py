"""The adapter protocol (plan §4.4, ADR 0031).

Adapters are pure functions of a Document built from stored WARC evidence, so the same parse can
be re-run by replay (ADR 0006). parse_listing runs on listing and feed documents and returns the
postings shown plus the targets to crawl next; parse_detail runs on every document and returns
parsed postings, each with its within-source identity (ADR 0008) and its assertions.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from aoi.db.schema.core import Source
from aoi.db.schema.enums import AssertionMethod, OpportunityKind, PostingIdentityKind, TargetRole
from aoi.storage.warc import Capture


@dataclass(frozen=True)
class Document:
    """A stored response as an adapter sees it."""

    url: str
    headers: dict[str, str]
    body: bytes
    fetched_at: datetime

    @classmethod
    def from_capture(cls, capture: Capture) -> "Document":
        """A Document from a response record; a revisit has no body and must be resolved first."""
        if capture.record_type != "response":
            raise ValueError(f"{capture.record_id} is a {capture.record_type}, not a response")
        return cls(
            url=capture.url,
            headers=capture.headers,
            body=capture.body,
            fetched_at=capture.fetched_at,
        )


@dataclass(frozen=True)
class CrawlTargetSpec:
    """A URL to crawl, and its role."""

    url: str
    role: TargetRole


@dataclass(frozen=True)
class PostingRef:
    """Within-source identity of a posting (ADR 0008)."""

    identity_kind: PostingIdentityKind
    identity_key: str
    detail_url: str | None = None
    kind: OpportunityKind | None = None


@dataclass(frozen=True)
class AssertionSpec:
    """One field value and its evidence; mirrors core.assertion, whose CHECKs apply."""

    field: str
    value: Any
    method: AssertionMethod
    confidence: Decimal
    raw: str | None = None
    pointer: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    quote: str | None = None


@dataclass(frozen=True)
class ListingPage:
    refs: tuple[PostingRef, ...]
    """Postings this listing shows (the M3 lifecycle notices the ones that disappear)."""
    targets: tuple[CrawlTargetSpec, ...]
    """Next pages and detail pages to crawl."""


@dataclass(frozen=True)
class ParsedPosting:
    ref: PostingRef
    assertions: tuple[AssertionSpec, ...]


@runtime_checkable
class Adapter(Protocol):
    name: str
    version: str

    def discover(self, source: Source) -> Iterator[CrawlTargetSpec]: ...
    def parse_listing(self, doc: Document) -> ListingPage: ...
    def parse_detail(self, doc: Document) -> list[ParsedPosting]: ...
