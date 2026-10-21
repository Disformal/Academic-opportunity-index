"""Tests for identity resolution — aoi.pipeline.identity."""

import pytest

from aoi.db.enums import IdentityKind
from aoi.pipeline.identity import (
    IdentityInputs,
    compute_fingerprint,
    normalize_title,
    resolve,
)

# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------


class TestComputeFingerprint:
    def test_deterministic_same_inputs(self) -> None:
        """Same inputs always produce the same fingerprint."""
        fp1 = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        fp2 = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        assert fp1 == fp2

    def test_case_insensitive(self) -> None:
        """Fingerprints are case-insensitive."""
        fp1 = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        fp2 = compute_fingerprint(
            title="phd in physics", org="cern", country="ch", deadline="2026-10-01"
        )
        assert fp1 == fp2

    def test_whitespace_normalized(self) -> None:
        """Extra whitespace is collapsed before hashing."""
        fp1 = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        fp2 = compute_fingerprint(
            title="  PhD  in  Physics  ", org=" CERN ", country="CH", deadline="2026-10-01"
        )
        assert fp1 == fp2

    def test_different_inputs_different_fingerprint(self) -> None:
        """Different inputs produce different fingerprints."""
        fp1 = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        fp2 = compute_fingerprint(
            title="PhD in Mathematics", org="CERN", country="CH", deadline="2026-10-01"
        )
        assert fp1 != fp2

    def test_none_fields_handled(self) -> None:
        """None fields are represented consistently in the fingerprint."""
        fp1 = compute_fingerprint(title="PhD in Physics", org=None, country=None, deadline=None)
        fp2 = compute_fingerprint(title="PhD in Physics", org=None, country=None, deadline=None)
        assert fp1 == fp2

    def test_none_vs_empty_string_different(self) -> None:
        """None and empty string produce different fingerprints."""
        fp1 = compute_fingerprint(title="PhD in Physics", org=None, country="CH", deadline=None)
        fp2 = compute_fingerprint(title="PhD in Physics", org="", country="CH", deadline=None)
        assert fp1 != fp2

    def test_returns_hex_sha256(self) -> None:
        """Fingerprint is a 64-char hex string (SHA-256)."""
        fp = compute_fingerprint(title="test", org="org", country="CH", deadline="2026-01-01")
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_native_ref_wins_over_all(self) -> None:
        """native_ref is the highest-priority identity."""
        inputs = IdentityInputs(
            native_ref="1234567",
            application_url="https://example.com/apply",
            detail_url="https://example.com/jobs/1234567",
            title="PhD in Physics",
            organization="CERN",
            country="CH",
            deadline="2026-10-01",
        )
        kind, key = resolve(inputs)
        assert kind == IdentityKind.NATIVE_REF
        assert key == "1234567"

    def test_application_url_when_no_native_ref(self) -> None:
        """application_url is used when native_ref is absent."""
        inputs = IdentityInputs(
            native_ref=None,
            application_url="https://example.com/apply",
            detail_url="https://example.com/jobs/42",
            title="PhD in Physics",
            organization="CERN",
            country="CH",
            deadline="2026-10-01",
        )
        kind, key = resolve(inputs)
        assert kind == IdentityKind.APPLICATION_URL
        assert key == "https://example.com/apply"

    def test_detail_url_when_no_native_ref_or_application_url(self) -> None:
        """detail_url is used when native_ref and application_url are absent."""
        inputs = IdentityInputs(
            native_ref=None,
            application_url=None,
            detail_url="https://example.com/jobs/42",
            title="PhD in Physics",
            organization="CERN",
            country="CH",
            deadline="2026-10-01",
        )
        kind, key = resolve(inputs)
        assert kind == IdentityKind.DETAIL_URL
        assert key == "https://example.com/jobs/42"

    def test_fingerprint_as_fallback(self) -> None:
        """Fingerprint is used when no URL-based identity is available."""
        inputs = IdentityInputs(
            native_ref=None,
            application_url=None,
            detail_url=None,
            title="PhD in Physics",
            organization="CERN",
            country="CH",
            deadline="2026-10-01",
        )
        kind, key = resolve(inputs)
        assert kind == IdentityKind.FINGERPRINT
        expected = compute_fingerprint(
            title="PhD in Physics", org="CERN", country="CH", deadline="2026-10-01"
        )
        assert key == expected

    def test_cross_source_same_application_url(self) -> None:
        """Two items from different sources with the same application_url resolve identically."""
        inspire_inputs = IdentityInputs(
            native_ref=None,
            application_url="https://university.edu/apply/123",
            detail_url="https://inspirehep.net/jobs/999",
            title="Postdoc in HEP",
            organization="MIT",
            country="US",
            deadline="2026-12-01",
        )
        euraxess_inputs = IdentityInputs(
            native_ref=None,
            application_url="https://university.edu/apply/123",
            detail_url="https://euraxess.ec.europa.eu/jobs/555",
            title="Postdoctoral Researcher in High Energy Physics",
            organization="Massachusetts Institute of Technology",
            country="US",
            deadline="2026-12-01",
        )
        kind1, key1 = resolve(inspire_inputs)
        kind2, key2 = resolve(euraxess_inputs)
        assert kind1 == kind2 == IdentityKind.APPLICATION_URL
        assert key1 == key2

    def test_empty_string_native_ref_skipped(self) -> None:
        """Empty string native_ref falls through to next tier."""
        inputs = IdentityInputs(
            native_ref="",
            application_url="https://example.com/apply",
            detail_url=None,
            title="test",
            organization=None,
            country=None,
            deadline=None,
        )
        kind, key = resolve(inputs)
        assert kind == IdentityKind.APPLICATION_URL

    def test_fingerprint_requires_title(self) -> None:
        """Fingerprint with no title raises ValueError — cannot identify."""
        inputs = IdentityInputs(
            native_ref=None,
            application_url=None,
            detail_url=None,
            title="",
            organization=None,
            country=None,
            deadline=None,
        )
        with pytest.raises(ValueError, match="title"):
            resolve(inputs)


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------


class TestNormalizeTitle:
    def test_lowercase_and_strip(self) -> None:
        assert normalize_title("  Postdoctoral Researcher  ") == "postdoctoral researcher"

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("PhD   in   Physics") == "phd in physics"

    def test_strips_common_prefixes(self) -> None:
        """Titles often start with level prefixes that add noise to fuzzy matching."""
        assert normalize_title("Postdoc: Lattice QCD") == "lattice qcd"
        assert normalize_title("PhD Position in HEP") == "hep"
        result = normalize_title("Postdoctoral Researcher in Quantum Computing")
        assert result == "quantum computing"

    def test_strips_trailing_location(self) -> None:
        """Trailing parenthesized location info should be removed."""
        assert normalize_title("Researcher (m/f/d)") == "researcher"

    def test_empty_after_stripping(self) -> None:
        """If the title reduces to empty after normalization, return the lowercased original."""
        result = normalize_title("PhD")
        assert result  # must not be empty

    def test_idempotent(self) -> None:
        once = normalize_title("Postdoctoral Researcher in Quantum Computing")
        twice = normalize_title(once)
        assert once == twice
