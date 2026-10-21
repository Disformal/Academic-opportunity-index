"""Tests for the discipline taxonomy (ontology/disciplines.yaml) and `aoi taxonomy sync`."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from aoi.db.schema.core import Discipline, DisciplineParent
from aoi.reference.taxonomy import TAXONOMY_FILE, load_taxonomy, sync_taxonomy


def _ancestors(code: str, parents: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    todo = list(parents[code])
    while todo:
        current = todo.pop()
        if current not in seen:
            seen.add(current)
            todo += parents[current]
    return seen


# --- the taxonomy file ---------------------------------------------------------------------


def test_taxonomy_has_six_fos_groups_and_42_fields() -> None:
    codes = [e.code for e in load_taxonomy(TAXONOMY_FILE) if e.code.startswith("fos:")]
    groups = [c for c in codes if "." not in c]
    assert len(groups) == 6
    assert len(codes) - len(groups) == 42


def test_every_arxiv_physics_category_reaches_physical_sciences() -> None:
    entries = load_taxonomy(TAXONOMY_FILE)
    parents = {e.code: e.parents for e in entries}
    outside_physics = {"arxiv:cs", "arxiv:math"}
    for entry in entries:
        if entry.code.startswith("arxiv:") and entry.code not in outside_physics:
            assert "fos:1.3" in _ancestors(entry.code, parents), entry.code


def test_medical_physics_overlaps_physics_and_medicine() -> None:
    entries = {e.code: e for e in load_taxonomy(TAXONOMY_FILE)}
    assert set(entries["arxiv:physics.med-ph"].parents) >= {"arxiv:physics", "fos:3.2"}


# --- the loader refuses broken files -------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            'oecd_fos_2007:\n  - {code: "fos:1", label: "A", parents: ["fos:9"]}\n',
            "unknown parent",
        ),
        (
            'oecd_fos_2007:\n  - {code: "fos:1", label: "A", parents: ["fos:2"]}\n'
            '  - {code: "fos:2", label: "B", parents: ["fos:1"]}\n',
            "cycle",
        ),
        (
            'oecd_fos_2007:\n  - {code: "fos:1", label: "A"}\n  - {code: "fos:1", label: "B"}\n',
            "duplicate",
        ),  # fmt: skip
        ('arxiv:\n  - {code: "fos:1", label: "A"}\n', "prefix"),
        ('biology:\n  - {code: "biology:1", label: "A"}\n', "scheme"),
    ],
)
def test_load_taxonomy_rejects_broken_file(tmp_path: Path, body: str, problem: str) -> None:
    path = tmp_path / "disciplines.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=problem):
        load_taxonomy(path)


# --- seeding the database ------------------------------------------------------------------


@pytest.mark.integration
def test_sync_taxonomy_stores_every_parent_link(db: Session) -> None:
    entries = load_taxonomy(TAXONOMY_FILE)
    result = sync_taxonomy(db, TAXONOMY_FILE)
    assert result.disciplines == len(entries)
    assert result.parent_links == sum(len(e.parents) for e in entries)
    med_ph = db.scalars(
        select(DisciplineParent.parent_code).where(
            DisciplineParent.child_code == "arxiv:physics.med-ph"
        )
    ).all()
    assert set(med_ph) == {"arxiv:physics", "fos:3.2", "fos:2.6"}


@pytest.mark.integration
def test_sync_taxonomy_twice_leaves_same_rows(db: Session) -> None:
    first = sync_taxonomy(db, TAXONOMY_FILE)
    assert sync_taxonomy(db, TAXONOMY_FILE) == first
    assert db.scalar(select(func.count()).select_from(Discipline)) == first.disciplines


@pytest.fixture()
def clean_taxonomy(engine: Engine) -> Iterator[None]:
    """Empty the taxonomy tables before and after a committing test (test DB only)."""

    def clean() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM core.discipline_parent"))
            conn.execute(text("DELETE FROM core.discipline"))

    clean()
    yield
    clean()


@pytest.mark.integration
def test_taxonomy_sync_cli_commits(
    engine: Engine, clean_taxonomy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aoi import cli

    @contextmanager
    def test_db_session() -> Iterator[Session]:
        with Session(engine) as session, session.begin():
            yield session

    monkeypatch.setattr(cli, "db_session", test_db_session)
    cli.main(["taxonomy", "sync"])

    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM core.discipline")).scalar_one()
    assert count == len(load_taxonomy(TAXONOMY_FILE))
