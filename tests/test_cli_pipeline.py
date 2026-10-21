"""Tests for `aoi pipeline run` — dedup is disabled until M5 (ADR 0017, 0025)."""

from unittest.mock import MagicMock, patch

import pytest

from aoi.cli import main


def test_pipeline_dedup_flag_refuses_while_dedup_disabled() -> None:
    """`aoi pipeline run --dedup` exits non-zero and never runs dedup."""
    with (
        patch("aoi.pipeline.run.run_dedup") as mock_dedup,
        pytest.raises(SystemExit) as exc,
    ):
        main(["pipeline", "run", "--dedup"])

    assert exc.value.code != 0
    mock_dedup.assert_not_called()


def test_pipeline_all_harvests_without_running_dedup() -> None:
    """`aoi pipeline run --all` harvests every source but does not run dedup."""
    with (
        patch("aoi.db.session.db_session", return_value=MagicMock()),
        patch("aoi.pipeline.run._persist_robots_assessment"),
        patch("aoi.pipeline.run.run_source", return_value=MagicMock()) as mock_run_source,
        patch("aoi.pipeline.run.run_dedup") as mock_dedup,
        patch("aoi.cli.log"),
    ):
        main(["pipeline", "run", "--all"])

    assert mock_run_source.call_count == 2
    mock_dedup.assert_not_called()
