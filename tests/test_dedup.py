"""Tests for deduplication — aoi.pipeline.dedup."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from aoi.db.enums import IdentityKind, OpportunityStatus
from aoi.db.models import Opportunity, Source
from aoi.pipeline.dedup import AUTO_MERGE, REVIEW, candidates, classify, score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _make_source(db, name: str) -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
        enabled=True,
    )
    db.add(src)
    db.flush()
    return src


def _make_opp(
    db,
    source: Source,
    *,
    identity_key: str,
    title: str = "Postdoctoral Researcher",
    application_url: str | None = None,
    deadline: datetime | None = None,
    country: str | None = None,
) -> Opportunity:
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=identity_key,
        title=title,
        type="postdoc",
        status=OpportunityStatus.OPEN,
        primary_language="en",
        deadline=deadline,
        country=country,
        application_url=application_url,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db.add(opp)
    db.flush()
    return opp


# ---------------------------------------------------------------------------
# score — unit tests (no DB needed)
# ---------------------------------------------------------------------------


class TestScore:
    def test_identical_records_high_score(self) -> None:
        """Two identical records should produce a very high probability."""
        a = MagicMock(spec=Opportunity)
        a.title = "Postdoctoral Researcher in Quantum Computing"
        a.application_url = "https://example.com/apply/123"
        a.country = "CH"
        a.deadline = _NOW + timedelta(days=30)

        b = MagicMock(spec=Opportunity)
        b.title = "Postdoctoral Researcher in Quantum Computing"
        b.application_url = "https://example.com/apply/123"
        b.country = "CH"
        b.deadline = _NOW + timedelta(days=30)

        result = score(a, b)
        assert result.probability > 0.95
        assert result.signals["application_url"] == 4.0

    def test_different_records_low_score(self) -> None:
        """Two clearly different records should produce a low probability."""
        a = MagicMock(spec=Opportunity)
        a.title = "Postdoctoral Researcher in Quantum Computing"
        a.application_url = "https://example.com/apply/123"
        a.country = "CH"
        a.deadline = _NOW + timedelta(days=30)

        b = MagicMock(spec=Opportunity)
        b.title = "PhD in Medieval History"
        b.application_url = "https://other.com/apply/456"
        b.country = "US"
        b.deadline = _NOW + timedelta(days=180)

        result = score(a, b)
        assert result.probability < 0.5

    def test_same_url_different_titles_still_positive(self) -> None:
        """Shared application_url is a strong signal even with different titles."""
        a = MagicMock(spec=Opportunity)
        a.title = "Postdoc in Lattice QCD"
        a.application_url = "https://university.edu/apply/42"
        a.country = "SE"
        a.deadline = _NOW + timedelta(days=20)

        b = MagicMock(spec=Opportunity)
        b.title = "Postdoctoral Researcher in Lattice Gauge Theory"
        b.application_url = "https://university.edu/apply/42"
        b.country = "SE"
        b.deadline = _NOW + timedelta(days=20)

        result = score(a, b)
        assert result.probability > 0.90
        assert result.signals["application_url"] == 4.0

    def test_same_deadline_contributes_positively(self) -> None:
        """Identical deadline within 0 days yields +2.5 bits."""
        a = MagicMock(spec=Opportunity)
        a.title = "Researcher"
        a.application_url = None
        a.country = None
        a.deadline = _NOW + timedelta(days=30)

        b = MagicMock(spec=Opportunity)
        b.title = "Researcher"
        b.application_url = None
        b.country = None
        b.deadline = _NOW + timedelta(days=30)

        result = score(a, b)
        assert result.signals["deadline"] == 2.5

    def test_distant_deadline_is_negative(self) -> None:
        """Deadlines far apart yield negative signal."""
        a = MagicMock(spec=Opportunity)
        a.title = "Researcher"
        a.application_url = None
        a.country = None
        a.deadline = _NOW

        b = MagicMock(spec=Opportunity)
        b.title = "Researcher"
        b.application_url = None
        b.country = None
        b.deadline = _NOW + timedelta(days=60)

        result = score(a, b)
        assert result.signals["deadline"] == -2.0

    def test_country_mismatch_is_negative(self) -> None:
        """Different countries should be a negative signal."""
        a = MagicMock(spec=Opportunity)
        a.title = "Researcher"
        a.application_url = None
        a.country = "CH"
        a.deadline = None

        b = MagicMock(spec=Opportunity)
        b.title = "Researcher"
        b.application_url = None
        b.country = "US"
        b.deadline = None

        result = score(a, b)
        assert result.signals["country"] == -2.5

    def test_signals_dict_populated(self) -> None:
        """The signals dict contains at least the title key."""
        a = MagicMock(spec=Opportunity)
        a.title = "Postdoc"
        a.application_url = None
        a.country = None
        a.deadline = None

        b = MagicMock(spec=Opportunity)
        b.title = "Postdoc"
        b.application_url = None
        b.country = None
        b.deadline = None

        result = score(a, b)
        assert "title" in result.signals


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    def test_auto_merge_requires_high_prob_and_two_strong(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=0.98,
            signals={"title": 2.5, "application_url": 4.0, "country": 0.8},
        )
        assert classify(sc) == "auto_merge"

    def test_high_prob_but_one_strong_goes_to_review(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=0.98,
            signals={"title": 2.5, "country": 0.8},
        )
        assert classify(sc) == "review"

    def test_review_band(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=0.90,
            signals={"title": 1.5, "country": 0.8},
        )
        assert classify(sc) == "review"

    def test_below_review_is_distinct(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=0.50,
            signals={"title": -0.5},
        )
        assert classify(sc) == "distinct"

    def test_exactly_at_auto_merge_threshold(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=AUTO_MERGE,
            signals={"title": 2.5, "application_url": 4.0},
        )
        assert classify(sc) == "auto_merge"

    def test_exactly_at_review_threshold(self) -> None:
        from aoi.pipeline.dedup import Scored

        sc = Scored(
            other_id="abc",
            probability=REVIEW,
            signals={"title": 1.5},
        )
        assert classify(sc) == "review"


# ---------------------------------------------------------------------------
# candidates — integration tests (real DB)
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_finds_by_shared_application_url(self, db) -> None:
        src = _make_source(db, "dedup-test-1")
        a = _make_opp(db, src, identity_key="a1", application_url="https://uni.edu/apply/42")
        b = _make_opp(db, src, identity_key="b1", application_url="https://uni.edu/apply/42")
        _make_opp(db, src, identity_key="c1", application_url="https://other.edu/apply/99")

        result = candidates(db, a)
        result_ids = {o.id for o in result}
        assert b.id in result_ids

    def test_finds_by_deadline_within_one_day(self, db) -> None:
        src = _make_source(db, "dedup-test-2")
        dl = _NOW + timedelta(days=30)
        a = _make_opp(db, src, identity_key="a2", deadline=dl)
        b = _make_opp(db, src, identity_key="b2", deadline=dl + timedelta(hours=12))
        _make_opp(db, src, identity_key="c2", deadline=dl + timedelta(days=10))

        result = candidates(db, a)
        result_ids = {o.id for o in result}
        assert b.id in result_ids

    def test_no_match_returns_empty(self, db) -> None:
        src = _make_source(db, "dedup-test-3")
        a = _make_opp(db, src, identity_key="a3")
        _make_opp(db, src, identity_key="b3")

        result = candidates(db, a)
        assert result == []

    def test_excludes_self(self, db) -> None:
        src = _make_source(db, "dedup-test-4")
        a = _make_opp(db, src, identity_key="a4", application_url="https://uni.edu/apply/42")

        result = candidates(db, a)
        result_ids = {o.id for o in result}
        assert a.id not in result_ids

    def test_limit_capped(self, db) -> None:
        """Candidates should be limited to at most 60."""
        src = _make_source(db, "dedup-test-5")
        url = "https://uni.edu/apply/shared"
        a = _make_opp(db, src, identity_key="anchor", application_url=url)
        for i in range(70):
            _make_opp(db, src, identity_key=f"other-{i}", application_url=url)

        result = candidates(db, a)
        assert len(result) <= 60
