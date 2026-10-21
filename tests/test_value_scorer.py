"""Tests for value scoring — aoi.pipeline.value_scorer."""

from datetime import UTC, datetime, timedelta

from aoi.pipeline.value_scorer import Candidate, choose, score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _cand(
    value: object,
    derivation: str = "official_api",
    first_party: bool = True,
    trust_tier: int = 1,
    confidence: float = 0.95,
    created_at: datetime | None = None,
    source_name: str = "INSPIRE-HEP",
) -> Candidate:
    return Candidate(
        value=value,
        derivation=derivation,
        first_party=first_party,
        trust_tier=trust_tier,
        confidence=confidence,
        created_at=created_at or _NOW,
        source_name=source_name,
    )


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


class TestScore:
    def test_best_case_score(self) -> None:
        """Human derivation, first_party, tier 1, fresh, full confidence."""
        c = _cand(
            value="test",
            derivation="human",
            first_party=True,
            trust_tier=1,
            confidence=1.0,
            created_at=_NOW,
        )
        s = score(c, now=_NOW)
        # 1.0 * 1.0 * 1.0 * 1.0 * 1.0 = 1.0
        assert s == 1.0

    def test_derivation_affects_score(self) -> None:
        """Parser derivation should score lower than official_api."""
        api = _cand(value="x", derivation="official_api")
        parser = _cand(value="x", derivation="parser")
        assert score(api, now=_NOW) > score(parser, now=_NOW)

    def test_first_party_false_reduces_score(self) -> None:
        a = _cand(value="x", first_party=True)
        b = _cand(value="x", first_party=False)
        assert score(a, now=_NOW) > score(b, now=_NOW)

    def test_higher_tier_reduces_score(self) -> None:
        """Trust tier 5 should score lower than tier 1."""
        t1 = _cand(value="x", trust_tier=1)
        t5 = _cand(value="x", trust_tier=5)
        assert score(t1, now=_NOW) > score(t5, now=_NOW)

    def test_recency_decay(self) -> None:
        """Older assertions should score lower."""
        fresh = _cand(value="x", created_at=_NOW)
        stale = _cand(value="x", created_at=_NOW - timedelta(days=90))
        assert score(fresh, now=_NOW) > score(stale, now=_NOW)

    def test_confidence_scales_linearly(self) -> None:
        high = _cand(value="x", confidence=0.95)
        low = _cand(value="x", confidence=0.50)
        assert score(high, now=_NOW) > score(low, now=_NOW)

    def test_score_positive(self) -> None:
        """Score should always be positive."""
        c = _cand(
            value="x",
            derivation="inherited",
            first_party=False,
            trust_tier=5,
            confidence=0.1,
            created_at=_NOW - timedelta(days=365),
        )
        assert score(c, now=_NOW) > 0


# ---------------------------------------------------------------------------
# choose
# ---------------------------------------------------------------------------


class TestChoose:
    def test_single_candidate_wins(self) -> None:
        c = _cand(value="Postdoc in QCD", source_name="INSPIRE-HEP")
        result = choose("title", [c], now=_NOW)
        assert result["value"] == "Postdoc in QCD"
        assert result["winner"] == "INSPIRE-HEP"
        assert result["disputed"] is False

    def test_higher_score_wins(self) -> None:
        a = _cand(value="2027-01-31", derivation="official_api", source_name="INSPIRE")
        b = _cand(value="2027-02-01", derivation="parser", source_name="EURAXESS")
        result = choose("deadline", [a, b], now=_NOW)
        assert result["value"] == "2027-01-31"
        assert result["winner"] == "INSPIRE"

    def test_competing_values_listed(self) -> None:
        a = _cand(value="SE", derivation="official_api", source_name="INSPIRE")
        b = _cand(value="Sweden", derivation="parser", source_name="EURAXESS")
        result = choose("country", [a, b], now=_NOW)
        assert len(result["competing"]) == 1
        assert result["competing"][0]["value"] == "Sweden"

    def test_disputed_when_close_scores_different_values(self) -> None:
        """Two first-party sources within 15% with different values → disputed."""
        a = _cand(
            value="2027-01-31",
            derivation="official_api",
            first_party=True,
            trust_tier=1,
            source_name="SRC-A",
        )
        b = _cand(
            value="2027-02-01",
            derivation="official_api",
            first_party=True,
            trust_tier=1,
            source_name="SRC-B",
        )
        result = choose("deadline", [a, b], now=_NOW)
        assert result["disputed"] is True
        assert result["publication_state"] == "disputed"

    def test_not_disputed_when_same_value(self) -> None:
        """Two sources with the same value should not be disputed."""
        a = _cand(
            value="SE",
            derivation="official_api",
            first_party=True,
            source_name="SRC-A",
        )
        b = _cand(
            value="SE",
            derivation="official_api",
            first_party=True,
            source_name="SRC-B",
        )
        result = choose("country", [a, b], now=_NOW)
        assert result["disputed"] is False

    def test_not_disputed_when_non_first_party(self) -> None:
        """Even close scores are not disputed if not both first-party."""
        a = _cand(
            value="2027-01-31",
            derivation="official_api",
            first_party=True,
            source_name="SRC-A",
        )
        b = _cand(
            value="2027-02-01",
            derivation="official_api",
            first_party=False,
            source_name="SRC-B",
        )
        result = choose("deadline", [a, b], now=_NOW)
        assert result["disputed"] is False

    def test_critical_field_below_bar_is_provisional(self) -> None:
        """A critical field with a low score should be marked provisional."""
        c = _cand(
            value="https://example.com/apply",
            derivation="inherited",
            first_party=False,
            trust_tier=5,
            confidence=0.5,
            created_at=_NOW - timedelta(days=90),
            source_name="WEAK-SRC",
        )
        result = choose("application_url", [c], now=_NOW)
        assert result["publication_state"] == "provisional"

    def test_non_critical_field_is_verified(self) -> None:
        """A normal field with a decent score should be verified."""
        c = _cand(value="Postdoc in QCD", source_name="INSPIRE")
        result = choose("title", [c], now=_NOW)
        assert result["publication_state"] == "verified"

    def test_competing_capped_at_three(self) -> None:
        """At most 3 competing entries should be returned."""
        cands = [
            _cand(
                value=f"val-{i}",
                derivation="parser",
                trust_tier=i + 1,
                source_name=f"SRC-{i}",
            )
            for i in range(5)
        ]
        result = choose("title", cands, now=_NOW)
        assert len(result["competing"]) <= 3
