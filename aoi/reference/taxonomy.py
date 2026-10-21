"""Load the discipline taxonomy (ontology/disciplines.yaml) into core.discipline.

Disciplines are a multi-parent graph: overlaps between fields are intended. The file is
validated before anything is written (unknown parents, cycles, duplicate codes, codes
that do not match their scheme). Disciplines are upserted; parent links are replaced
wholesale, since the file is the complete taxonomy. The caller commits.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aoi.db.schema.core import Discipline, DisciplineParent
from aoi.db.schema.enums import DisciplineScheme

log = structlog.get_logger()

TAXONOMY_FILE = Path(__file__).resolve().parents[2] / "ontology" / "disciplines.yaml"

_PREFIX = {DisciplineScheme.OECD_FOS_2007: "fos:", DisciplineScheme.ARXIV: "arxiv:"}


@dataclass(frozen=True)
class TaxonomyEntry:
    code: str
    scheme: DisciplineScheme
    label: str
    parents: list[str]


@dataclass(frozen=True)
class TaxonomySyncResult:
    disciplines: int
    parent_links: int


def _check_acyclic(parents: dict[str, list[str]]) -> None:
    done: set[str] = set()

    def visit(code: str, path: list[str]) -> None:
        if code in path:
            raise ValueError(f"cycle in taxonomy: {' -> '.join([*path, code])}")
        if code not in done:
            for parent in parents[code]:
                visit(parent, [*path, code])
            done.add(code)

    for code in parents:
        visit(code, [])


def load_taxonomy(path: Path) -> list[TaxonomyEntry]:
    """Parse and validate the taxonomy file."""
    data: dict[str, list[dict[str, Any]]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries: list[TaxonomyEntry] = []
    for section, items in data.items():
        if section not in DisciplineScheme.__members__.values():
            raise ValueError(f"unknown scheme section {section!r}")
        scheme = DisciplineScheme(section)
        for item in items:
            code = str(item["code"])
            if not code.startswith(_PREFIX[scheme]):
                raise ValueError(f"{code!r} lacks the {_PREFIX[scheme]!r} prefix of {section}")
            parents = [str(p) for p in item.get("parents", [])]
            entries.append(TaxonomyEntry(code, scheme, str(item["label"]), parents))

    codes = [e.code for e in entries]
    duplicates = {c for c in codes if codes.count(c) > 1}
    if duplicates:
        raise ValueError(f"duplicate codes: {sorted(duplicates)}")
    for entry in entries:
        for parent in entry.parents:
            if parent not in codes:
                raise ValueError(f"{entry.code!r} has unknown parent {parent!r}")
    _check_acyclic({e.code: e.parents for e in entries})
    return entries


def sync_taxonomy(session: Session, path: Path) -> TaxonomySyncResult:
    """Upsert every discipline in the file and replace all parent links."""
    entries = load_taxonomy(path)
    upsert = pg_insert(Discipline)
    session.execute(
        upsert.on_conflict_do_update(
            index_elements=[Discipline.code],
            set_={"scheme": upsert.excluded.scheme, "label": upsert.excluded.label},
        ),
        [{"code": e.code, "scheme": e.scheme, "label": e.label} for e in entries],
    )
    links = [{"child_code": e.code, "parent_code": p} for e in entries for p in e.parents]
    session.execute(delete(DisciplineParent))
    session.execute(insert(DisciplineParent), links)

    result = TaxonomySyncResult(len(entries), len(links))
    log.info("taxonomy.sync", disciplines=result.disciplines, parent_links=result.parent_links)
    return result
