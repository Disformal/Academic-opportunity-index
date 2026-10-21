"""A small adapter for tests, shaped like an API source (ADR 0031). Not a test module.

A listing document is JSON: {"hits": [{"id", "title", "url"}, ...], "next": url or null}.
One listing page yields many parsed postings (as an INSPIRE API page does) and its next page.
"""

import json
from collections.abc import Iterator
from decimal import Decimal

from aoi.adapters.protocol import (
    AssertionSpec,
    CrawlTargetSpec,
    Document,
    ListingPage,
    ParsedPosting,
    PostingRef,
)
from aoi.db.schema.core import Source
from aoi.db.schema.enums import AssertionMethod, PostingIdentityKind, TargetRole


class FakeApiAdapter:
    name = "fake-api"
    version = "1.0.0"

    def discover(self, source: Source) -> Iterator[CrawlTargetSpec]:
        yield CrawlTargetSpec(url=f"{source.base_url}?page=1", role=TargetRole.LISTING)

    def parse_listing(self, doc: Document) -> ListingPage:
        data = json.loads(doc.body)
        refs = tuple(_ref(hit) for hit in data["hits"])
        targets = (
            (CrawlTargetSpec(url=data["next"], role=TargetRole.LISTING),) if data["next"] else ()
        )
        return ListingPage(refs=refs, targets=targets)

    def parse_detail(self, doc: Document) -> list[ParsedPosting]:
        data = json.loads(doc.body)
        return [
            ParsedPosting(
                ref=_ref(hit),
                assertions=(
                    AssertionSpec(
                        field="title",
                        value=hit["title"],
                        raw=hit["title"],
                        method=AssertionMethod.OFFICIAL_API,
                        pointer=f"/hits/{i}/title",
                        confidence=Decimal("1"),
                    ),
                ),
            )
            for i, hit in enumerate(data["hits"])
        ]


def _ref(hit: dict[str, str]) -> PostingRef:
    return PostingRef(
        identity_kind=PostingIdentityKind.NATIVE_REF,
        identity_key=str(hit["id"]),
        detail_url=hit["url"],
    )
