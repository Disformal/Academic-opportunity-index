"""Load the ROR data dump into core.organization (ADR 0009).

The dump is the v2-schema JSON inside the zip published on Zenodo. Organizations are
upserted (a record missing from a later dump keeps its older ror_release); relations
are replaced wholesale, since each dump is a complete snapshot. The caller commits.
"""

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aoi.db.schema.core import Organization, OrganizationRelation

log = structlog.get_logger()

_MEMBER = re.compile(r"(v\d+(?:\.\d+)*-\d{4}-\d{2}-\d{2})-ror-data\.json")
_ROR_PREFIX = "https://ror.org/"


@dataclass(frozen=True)
class RorSyncResult:
    release: str
    organizations: int
    relations: int


def read_dump(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return (release, records) from a ROR dump zip. The release comes from the JSON name."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            match = _MEMBER.fullmatch(name)
            if match:
                with zf.open(name) as f:
                    return match.group(1), json.load(f)
    raise ValueError(f"{path} contains no '<release>-ror-data.json' member")


def _organization(record: dict[str, Any], release: str) -> dict[str, Any]:
    place = record["locations"][0]["geonames_details"]  # every v2.14 record has one
    return {
        "ror_id": record["id"].removeprefix(_ROR_PREFIX),
        "display_name": next(n["value"] for n in record["names"] if "ror_display" in n["types"]),
        "names": record["names"],
        "types": record["types"],
        "status": record["status"],
        "country_code": place["country_code"],
        "city": place["name"],
        "lat": place["lat"],
        "lon": place["lng"],
        "established": record["established"],
        "fundref_ids": [
            value
            for ext in record["external_ids"]
            if ext["type"] == "fundref"
            for value in ext["all"]
        ],
        "ror_release": release,
    }


def sync_ror(session: Session, path: Path) -> RorSyncResult:
    """Upsert every organization in the dump and replace all relations."""
    release, records = read_dump(path)
    organizations = [_organization(r, release) for r in records]
    relations = [
        {
            "ror_id": r["id"].removeprefix(_ROR_PREFIX),
            "related_ror_id": rel["id"].removeprefix(_ROR_PREFIX),
            "type": rel["type"],
        }
        for r in records
        for rel in r["relationships"]
    ]

    upsert = pg_insert(Organization)
    session.execute(
        upsert.on_conflict_do_update(
            index_elements=[Organization.ror_id],
            set_={
                column: upsert.excluded[column]
                for column in organizations[0]
                if column != "ror_id"
            },
        ),
        organizations,
    )
    session.execute(delete(OrganizationRelation))
    session.execute(insert(OrganizationRelation), relations)

    result = RorSyncResult(release, len(organizations), len(relations))
    log.info(
        "ror.sync",
        release=result.release,
        organizations=result.organizations,
        relations=result.relations,
    )
    return result
