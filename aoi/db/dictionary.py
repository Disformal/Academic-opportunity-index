"""Generate docs/data-dictionary.md from the layered-schema models.

The models are the source of truth: every column carries a `comment`, and a test keeps
the committed file identical to what this module renders. Regenerate with:

    uv run python -m aoi.db.dictionary
"""

from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import CheckConstraint, Column, Enum, Table
from sqlalchemy.dialects.postgresql.base import PGDialect

from aoi.db.schema import LayerBase

log = structlog.get_logger()

DICTIONARY_FILE = Path(__file__).resolve().parents[2] / "docs" / "data-dictionary.md"

# Conventional columns, described here instead of on every model.
STANDARD_COLUMNS = {
    "id": "Primary key (UUID).",
    "created_at": "When the row was inserted.",
    "updated_at": "When the row was last changed (maintained by a trigger).",
}

_SCHEMAS = [
    ("evidence", "L0: the fetch log. Raw responses live in WARC files (ADR 0006)."),
    (
        "core",
        "L1 observations and L2 entities: sources, assertions, postings, positions, "
        "organizations, disciplines, funders, awards (ADR 0007).",
    ),  # fmt: skip
    ("ops", "Crawl scheduling, ground-truth samples and labels, the erasure log."),
    ("analytics", "L3 research tables (built in M8)."),
]

_HEADER = """# AOI data dictionary

Generated from the models in `aoi/db/schema/` by `uv run python -m aoi.db.dictionary`.
Do not edit by hand: a test fails when this file and the models differ.

Rows in evidence, assertions, postings, history, links, samples and labels are never deleted
(ADR 0005, 0027, 0028). Times are UTC. *Observed time* is when AOI saw something;
*valid time* is when it was true at the source.
"""


def _type(column: Column[Any]) -> str:
    if isinstance(column.type, Enum):
        return "enum: " + ", ".join(column.type.enums)
    # SQLAlchemy's dialect constructors are untyped.
    return str(column.type.compile(dialect=PGDialect()))  # type: ignore[no-untyped-call]


def _key(column: Column[Any]) -> str:
    parts = ["PK"] if column.primary_key else []
    parts += [f"→ {fk.target_fullname}" for fk in column.foreign_keys]
    return " ".join(parts)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(table: Table) -> list[str]:
    lines = [
        f"### `{table.fullname}`",
        "",
        _cell(table.comment or ""),
        "",
        "| Column | Type | Nullable | Key | Description |",
        "|---|---|---|---|---|",
    ]
    for column in table.columns:
        description = column.comment or STANDARD_COLUMNS[column.name]
        lines.append(
            f"| `{column.name}` | {_cell(_type(column))} | "
            f"{'yes' if column.nullable else 'no'} | {_key(column)} | {_cell(description)} |"
        )
    checks = sorted(
        (c for c in table.constraints if isinstance(c, CheckConstraint)),
        key=lambda c: str(c.name),
    )
    if checks:
        lines += ["", "Rules:", ""]
        lines += [f"- `{c.name}`: `{_cell(str(c.sqltext))}`" for c in checks]
    return [*lines, ""]


def render_dictionary() -> str:
    """Render the whole dictionary as Markdown."""
    lines = [_HEADER]
    for schema, description in _SCHEMAS:
        tables = sorted(
            (t for t in LayerBase.metadata.tables.values() if t.schema == schema),
            key=lambda t: t.name,
        )
        lines += [f"## Schema `{schema}`", "", description, ""]
        if not tables:
            lines += ["*No tables yet.*", ""]
        for table in tables:
            lines += _table(table)
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    DICTIONARY_FILE.write_text(render_dictionary(), encoding="utf-8")
    log.info("dictionary.written", path=str(DICTIONARY_FILE))


if __name__ == "__main__":  # pragma: no cover - entry point for `python -m`
    main()
