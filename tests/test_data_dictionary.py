"""Tests for the generated data dictionary (docs/data-dictionary.md)."""

from pathlib import Path

import pytest

from aoi.db import dictionary
from aoi.db.dictionary import DICTIONARY_FILE, STANDARD_COLUMNS, render_dictionary
from aoi.db.schema import LayerBase


def test_every_layer_column_has_a_description() -> None:
    undocumented = [
        f"{table.fullname}.{column.name}"
        for table in LayerBase.metadata.sorted_tables
        for column in table.columns
        if not column.comment and column.name not in STANDARD_COLUMNS
    ]
    assert undocumented == []


def test_every_layer_table_has_a_description() -> None:
    assert [t.fullname for t in LayerBase.metadata.sorted_tables if not t.comment] == []


def test_dictionary_has_a_section_for_every_table() -> None:
    text = render_dictionary()
    for table in LayerBase.metadata.tables.values():
        assert f"### `{table.fullname}`" in text


def test_dictionary_lists_enum_values() -> None:
    text = render_dictionary()
    assert "enum: pending, active, stale, closed, removed" in text


def test_dictionary_shows_foreign_keys() -> None:
    row = next(line for line in render_dictionary().splitlines() if "| `fetch_id` |" in line)
    assert "evidence.fetch.id" in row


def test_dictionary_shows_check_rules() -> None:
    assert "ck_posting_closure_reason" in render_dictionary()


def test_committed_dictionary_is_current() -> None:
    """If this fails, run: uv run python -m aoi.db.dictionary"""
    assert DICTIONARY_FILE.read_text(encoding="utf-8") == render_dictionary()


def test_main_writes_the_rendered_dictionary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data-dictionary.md"
    monkeypatch.setattr(dictionary, "DICTIONARY_FILE", target)
    dictionary.main()
    assert target.read_text(encoding="utf-8") == render_dictionary()
