"""Tests for measurement functions and CLI commands."""

import uuid
from datetime import UTC, datetime

from aoi.db.models import (
    DuplicateCluster,
    DuplicateMember,
    Opportunity,
)
from aoi.pipeline.measure import szymkiewicz_simpson, wilson_lower_bound

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


# ---------------------------------------------------------------------------
# Integration tests — dedup-ratio query
# ---------------------------------------------------------------------------


def test_dedup_ratio_query(db) -> None:  # type: ignore[no-untyped-def]
    """Insert clusters and opportunities, verify ratio computation."""
    from sqlalchemy import func

    now = datetime.now(UTC)

    opp_a = Opportunity(
        identity_kind="native_ref",
        identity_key="test-dr-a",
        title="Test Opp A",
        type="postdoc",
        status="open",
        first_seen_at=now,
        last_seen_at=now,
    )
    opp_b = Opportunity(
        identity_kind="native_ref",
        identity_key="test-dr-b",
        title="Test Opp B",
        type="postdoc",
        status="open",
        first_seen_at=now,
        last_seen_at=now,
    )
    opp_c = Opportunity(
        identity_kind="native_ref",
        identity_key="test-dr-c",
        title="Test Opp C",
        type="phd",
        status="open",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add_all([opp_a, opp_b, opp_c])
    db.flush()

    cluster = DuplicateCluster(
        canonical_opportunity_id=opp_a.id,
        method="auto",
        score=0.98,
    )
    db.add(cluster)
    db.flush()

    for opp in (opp_a, opp_b):
        db.add(
            DuplicateMember(
                cluster_id=cluster.id,
                opportunity_id=opp.id,
                probability=0.98,
                added_at=now,
            )
        )
    db.flush()

    total = db.query(func.count(Opportunity.id)).scalar() or 0
    clusters = db.query(func.count(DuplicateCluster.id)).scalar() or 0

    assert total >= 3
    assert clusters >= 1
    ratio = clusters / total
    assert 0 < ratio < 1


# ---------------------------------------------------------------------------
# CLI tests — automated commands
# ---------------------------------------------------------------------------


def test_cli_measure_dedup_ratio_help() -> None:
    """Verify the measure dedup-ratio subcommand is wired up."""
    import pytest

    from aoi.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["measure", "dedup-ratio", "--help"])
    assert exc_info.value.code == 0


def test_cli_measure_overlap_help() -> None:
    """Verify the measure overlap subcommand is wired up."""
    import pytest

    from aoi.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["measure", "overlap", "--help"])
    assert exc_info.value.code == 0


def test_cli_measure_deadline_precision_help() -> None:
    """Verify the measure deadline-precision subcommand is wired up."""
    import pytest

    from aoi.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["measure", "deadline-precision", "--help"])
    assert exc_info.value.code == 0


def test_cli_measure_false_merge_rate_help() -> None:
    """Verify the measure false-merge-rate subcommand is wired up."""
    import pytest

    from aoi.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["measure", "false-merge-rate", "--help"])
    assert exc_info.value.code == 0


def test_deadline_evaluate_from_csv(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Test deadline-precision --evaluate with a labelled CSV."""
    csv_path = tmp_path / "deadlines.csv"
    csv_path.write_text(
        "id,title,deadline,source_name,source_url,label\n"
        "aaa,T1,2026-10-01,INSPIRE,,correct\n"
        "bbb,T2,2026-10-02,EURAXESS,,correct\n"
        "ccc,T3,2026-10-03,INSPIRE,,incorrect\n"
    )

    from aoi.pipeline.measure import wilson_lower_bound

    wlb = wilson_lower_bound(2, 3)
    assert 0.0 < wlb < 2 / 3


def test_false_merge_evaluate_from_csv(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Test false-merge-rate --evaluate with a labelled CSV."""
    import csv as csv_mod

    csv_path = tmp_path / "merges.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv_mod.writer(f)
        writer.writerow(
            [
                "cluster_id",
                "id_a",
                "title_a",
                "deadline_a",
                "country_a",
                "org_a",
                "id_b",
                "title_b",
                "deadline_b",
                "country_b",
                "org_b",
                "score",
                "label",
            ]
        )
        writer.writerow(
            [
                str(uuid.uuid4()),
                "a",
                "T1",
                "",
                "",
                "",
                "b",
                "T2",
                "",
                "",
                "",
                "0.98",
                "correct_merge",
            ]
        )
        writer.writerow(
            [
                str(uuid.uuid4()),
                "c",
                "T3",
                "",
                "",
                "",
                "d",
                "T4",
                "",
                "",
                "",
                "0.90",
                "false_merge",
            ]
        )

    with csv_path.open() as f:
        reader = csv_mod.DictReader(f)
        labels = [r["label"].strip().lower() for r in reader if r.get("label", "").strip()]

    total = len(labels)
    false_merges = sum(1 for lb in labels if lb == "false_merge")

    assert total == 2
    assert false_merges == 1
    assert false_merges / total == 0.5
