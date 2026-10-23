"""Tests for the measurement functions (aoi/stats/measure.py)."""

from aoi.stats.measure import szymkiewicz_simpson, wilson_lower_bound

# ---------------------------------------------------------------------------
# Unit tests — wilson_lower_bound
# ---------------------------------------------------------------------------


def test_wilson_lower_bound_all_correct() -> None:
    wlb = wilson_lower_bound(20, 20)
    # 20/20 perfect → lower bound should be high (> 0.80)
    assert wlb > 0.80
    assert wlb <= 1.0


def test_wilson_lower_bound_mixed() -> None:
    wlb = wilson_lower_bound(15, 20)
    # 75% success rate, lower bound should be below the point estimate
    assert 0.50 < wlb < 0.75


def test_wilson_lower_bound_zero() -> None:
    wlb = wilson_lower_bound(0, 20)
    assert wlb == 0.0


def test_wilson_lower_bound_empty() -> None:
    wlb = wilson_lower_bound(0, 0)
    assert wlb == 0.0


def test_wilson_lower_bound_single_success() -> None:
    wlb = wilson_lower_bound(1, 1)
    # 1/1 → lower bound is low because of tiny sample
    assert 0.0 < wlb < 1.0


# ---------------------------------------------------------------------------
# Unit tests — szymkiewicz_simpson
# ---------------------------------------------------------------------------


def test_szymkiewicz_simpson_full_overlap() -> None:
    # A is a subset of B → 1.0
    assert szymkiewicz_simpson({"a", "b"}, {"a", "b", "c"}) == 1.0


def test_szymkiewicz_simpson_no_overlap() -> None:
    assert szymkiewicz_simpson({"a", "b"}, {"c", "d"}) == 0.0


def test_szymkiewicz_simpson_partial() -> None:
    # {a, b, c} ∩ {b, c, d} = {b, c}, min(3, 3) = 3 → 2/3
    coeff = szymkiewicz_simpson({"a", "b", "c"}, {"b", "c", "d"})
    assert abs(coeff - 2 / 3) < 1e-9


def test_szymkiewicz_simpson_empty_set() -> None:
    assert szymkiewicz_simpson(set(), {"a"}) == 0.0
    assert szymkiewicz_simpson({"a"}, set()) == 0.0


def test_szymkiewicz_simpson_identical_sets() -> None:
    assert szymkiewicz_simpson({"x", "y"}, {"x", "y"}) == 1.0
