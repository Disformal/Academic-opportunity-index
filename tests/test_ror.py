"""Tests for the ROR dump loader (ADR 0009) and `aoi ror sync`."""

import json
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from aoi.db.schema.core import Organization, OrganizationRelation
from aoi.db.schema.enums import OrgRelationType, OrgStatus
from aoi.reference.ror import read_dump, sync_ror

pytestmark = pytest.mark.integration

# Seven unmodified records cut from the v2.14 dump; every relation target is included.
FIXTURE = Path(__file__).parent / "fixtures" / "ror" / "v2.14-2026-10-06-sample.json"
FIXTURE_IDS = [
    "03ekzka28",
    "00nzje529",
    "008zgvp64",
    "03bdvdc06",
    "023z51242",
    "04czrrc08",
    "01nn35n25",
]


def _zip(tmp_path: Path, records: list[dict[str, Any]], member: str) -> Path:
    path = tmp_path / f"{member.removesuffix('.json')}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, json.dumps(records))
    return path


def _records() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def dump(tmp_path: Path) -> Path:
    return _zip(tmp_path, _records(), "v2.14-2026-10-06-ror-data.json")


# --- reading the dump ---------------------------------------------------------------------


def test_read_dump_takes_release_from_json_member_name(dump: Path) -> None:
    release, records = read_dump(dump)
    assert release == "v2.14-2026-10-06"
    assert len(records) == 7


def test_read_dump_rejects_zip_without_ror_json(tmp_path: Path) -> None:
    path = _zip(tmp_path, [], "something-else.json")
    with pytest.raises(ValueError, match="ror-data"):
        read_dump(path)


# --- loading organizations and relations --------------------------------------------------


def test_sync_ror_loads_physics_institute_with_greek_name(db: Session, dump: Path) -> None:
    sync_ror(db, dump)
    iasa = db.get(Organization, "01nn35n25")
    assert iasa is not None
    assert iasa.display_name == "Institute of Accelerating Systems and Applications"
    assert {"value": "Ινστιτούτο Επιταχυντικών Συστημάτων και Εφαρμογών", "types": ["label"],
            "lang": "el"} in iasa.names  # fmt: skip
    assert (iasa.country_code, iasa.city, iasa.lat, iasa.lon) == (
        "GR",
        "Athens",
        37.98376,
        23.72784,
    )
    assert (iasa.status, iasa.established, iasa.ror_release) == (
        OrgStatus.ACTIVE,
        1994,
        "v2.14-2026-10-06",
    )


def test_sync_ror_uses_first_location_when_several(db: Session, dump: Path) -> None:
    sync_ror(db, dump)
    bayan = db.get(Organization, "04czrrc08")
    assert bayan is not None
    assert (bayan.country_code, bayan.city) == ("US", "Chicago")


def test_sync_ror_keeps_crossref_funder_ids(db: Session, dump: Path) -> None:
    sync_ror(db, dump)
    funder = db.get(Organization, "023z51242")
    assert funder is not None
    assert funder.fundref_ids == ["501100001588"]
    assert funder.types == ["government", "funder"]


def test_sync_ror_loads_withdrawn_record_and_its_successor(db: Session, dump: Path) -> None:
    sync_ror(db, dump)
    old = db.get(Organization, "03bdvdc06")
    assert old is not None and old.status == OrgStatus.WITHDRAWN
    relations = db.scalars(
        select(OrganizationRelation).where(OrganizationRelation.ror_id == "03bdvdc06")
    ).all()
    assert [(r.related_ror_id, r.type) for r in relations] == [
        ("008zgvp64", OrgRelationType.SUCCESSOR)
    ]


def test_sync_ror_loads_both_directions_of_parent_child(db: Session, dump: Path) -> None:
    sync_ror(db, dump)
    rows = db.execute(
        select(OrganizationRelation.ror_id, OrganizationRelation.related_ror_id,
               OrganizationRelation.type)
    ).all()  # fmt: skip
    assert set(rows) >= {
        ("03ekzka28", "00nzje529", OrgRelationType.CHILD),
        ("00nzje529", "03ekzka28", OrgRelationType.PARENT),
    }


def test_sync_ror_twice_leaves_same_rows(db: Session, dump: Path) -> None:
    first = sync_ror(db, dump)
    second = sync_ror(db, dump)
    assert first == second
    fixture_orgs = select(func.count()).where(Organization.ror_id.in_(FIXTURE_IDS))
    assert db.scalar(fixture_orgs) == 7
    assert db.scalar(select(func.count()).select_from(OrganizationRelation)) == 3


def test_sync_ror_newer_release_updates_rows(db: Session, dump: Path, tmp_path: Path) -> None:
    sync_ror(db, dump)
    records = _records()
    iasa = next(r for r in records if r["id"].endswith("01nn35n25"))
    iasa["status"] = "inactive"
    for name in iasa["names"]:
        if "ror_display" in name["types"]:
            name["value"] = "IASA (renamed)"
    sync_ror(db, _zip(tmp_path, records, "v2.15-2026-11-03-ror-data.json"))
    db.expire_all()
    row = db.get(Organization, "01nn35n25")
    assert row is not None
    assert (row.display_name, row.status, row.ror_release) == (
        "IASA (renamed)",
        OrgStatus.INACTIVE,
        "v2.15-2026-11-03",
    )


# --- the CLI commits (real transaction boundary) ------------------------------------------


@pytest.fixture()
def clean_fixture_orgs(engine: Engine) -> Iterator[None]:
    """Remove the fixture's organizations before and after a committing test."""

    def clean() -> None:
        with engine.begin() as conn:
            params = {"ids": FIXTURE_IDS}
            conn.execute(
                text("DELETE FROM core.organization_relation WHERE ror_id = ANY(:ids)"), params
            )
            conn.execute(text("DELETE FROM core.organization WHERE ror_id = ANY(:ids)"), params)

    clean()
    yield
    clean()


def test_ror_sync_cli_commits_organizations(
    engine: Engine, dump: Path, clean_fixture_orgs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aoi import cli

    @contextmanager
    def test_db_session() -> Iterator[Session]:
        with Session(engine) as session, session.begin():
            yield session

    monkeypatch.setattr(cli, "db_session", test_db_session)
    cli.main(["ror", "sync", "--file", str(dump)])

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM core.organization WHERE ror_id = ANY(:ids)"),
            {"ids": FIXTURE_IDS},
        ).scalar_one()
    assert count == 7
