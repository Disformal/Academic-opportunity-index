"""m1_calls_funders_awards_pseudonyms

core.call_details (funding calls, bound to the position's kind like the other details
tables) and the research-graph keys of ADR 0010, schema now and data after launch:
core.funder (ROR and/or Crossref Funder ID), core.award, core.grant_reference (grant
numbers from postings, traced to their assertion), core.person_pseudonym (HMAC only,
ADR 0011) and core.award_person.

Revision ID: 3ad4633a21b5
Revises: 59d1d8c85e66
Create Date: 2026-10-17 13:50:04.660179

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3ad4633a21b5"
down_revision: str | Sequence[str] | None = "59d1d8c85e66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES_WITH_UPDATED_AT = ["funder", "award"]


def upgrade() -> None:
    """Create the call, funder, award, grant and pseudonym tables."""

    op.create_table(
        "funder",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="Funder name."),
        sa.Column(
            "ror_id", sa.String(length=9), nullable=True, comment="The funder's ROR record."
        ),
        sa.Column(
            "crossref_funder_id",
            sa.String(length=30),
            nullable=True,
            comment="Crossref Funder Registry ID (e.g. '501100000781' for the ERC).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ror_id IS NOT NULL OR crossref_funder_id IS NOT NULL", name="ck_funder_has_key"
        ),
        sa.ForeignKeyConstraint(
            ["ror_id"],
            ["core.organization.ror_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("crossref_funder_id", name="uq_funder_crossref_id"),
        sa.UniqueConstraint("ror_id", name="uq_funder_ror_id"),
        schema="core",
        comment="A funding body, keyed by ROR and/or Crossref Funder ID.",
    )
    op.create_table(
        "award",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=50),
            nullable=False,
            comment="Award database, e.g. 'cordis', 'nih_reporter'.",
        ),
        sa.Column(
            "native_id", sa.Text(), nullable=False, comment="The award's ID in that database."
        ),
        sa.Column("funder_id", sa.Uuid(), nullable=True, comment="Who funded it."),
        sa.Column("ror_id", sa.String(length=9), nullable=True, comment="Host organization."),
        sa.Column("title", sa.Text(), nullable=True, comment="Project title."),
        sa.Column(
            "amount", sa.Numeric(precision=16, scale=2), nullable=True, comment="Awarded amount."
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=True, comment="ISO 4217 code of amount."
        ),
        sa.Column("start_date", sa.Date(), nullable=True, comment="Project start."),
        sa.Column("end_date", sa.Date(), nullable=True, comment="Project end."),
        sa.Column(
            "discipline_code", sa.String(length=40), nullable=True, comment="Main discipline."
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount IS NULL OR currency IS NOT NULL", name="ck_award_amount_currency"
        ),
        sa.CheckConstraint("end_date >= start_date", name="ck_award_dates"),
        sa.ForeignKeyConstraint(
            ["discipline_code"],
            ["core.discipline.code"],
        ),
        sa.ForeignKeyConstraint(
            ["funder_id"],
            ["core.funder.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ror_id"],
            ["core.organization.ror_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "native_id", name="uq_award_source_native_id"),
        schema="core",
        comment="A funded project from an award database.",
    )
    op.create_table(
        "call_details",
        sa.Column("position_id", sa.Uuid(), nullable=False, comment="The position."),
        sa.Column(
            "kind",
            postgresql.ENUM(name="opportunity_kind", schema="core", create_type=False),
            server_default="funding_call",
            nullable=False,
            comment="Always 'funding_call'; part of the foreign key that enforces it.",
        ),
        sa.Column("funder_id", sa.Uuid(), nullable=True, comment="Who funds the call."),
        sa.Column(
            "call_identifier",
            sa.Text(),
            nullable=True,
            comment="The funder's call code, e.g. 'ERC-2027-StG'.",
        ),
        sa.Column(
            "opens_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When applications open (valid time).",
        ),
        sa.Column(
            "funding_min",
            sa.Numeric(precision=16, scale=2),
            nullable=True,
            comment="Lower bound of funding per award, as stated.",
        ),
        sa.Column(
            "funding_max",
            sa.Numeric(precision=16, scale=2),
            nullable=True,
            comment="Upper bound of funding per award, as stated.",
        ),
        sa.Column(
            "funding_currency",
            sa.String(length=3),
            nullable=True,
            comment="ISO 4217 code of the stated funding.",
        ),
        sa.Column(
            "duration_months",
            sa.SmallInteger(),
            nullable=True,
            comment="Length of the funded project.",
        ),
        sa.CheckConstraint("kind = 'funding_call'", name="ck_call_details_kind"),
        sa.CheckConstraint("funding_max >= funding_min", name="ck_call_details_funding_order"),
        sa.CheckConstraint(
            "funding_min IS NULL AND funding_max IS NULL OR funding_currency IS NOT NULL",
            name="ck_call_details_funding_currency",
        ),
        sa.ForeignKeyConstraint(
            ["funder_id"],
            ["core.funder.id"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "kind"],
            ["core.position.id", "core.position.kind"],
        ),
        sa.PrimaryKeyConstraint("position_id"),
        schema="core",
        comment="Fields specific to funding calls.",
    )
    op.create_table(
        "grant_reference",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "assertion_id",
            sa.Uuid(),
            nullable=False,
            comment="The assertion the grant number was extracted in (provenance).",
        ),
        sa.Column("raw", sa.Text(), nullable=False, comment="As written in the posting."),
        sa.Column(
            "normalised",
            sa.Text(),
            nullable=True,
            comment="Canonical form per funder rules, e.g. '101042368'.",
        ),
        sa.Column(
            "funder_id",
            sa.Uuid(),
            nullable=True,
            comment="Funder the number belongs to, once known.",
        ),
        sa.Column(
            "award_id",
            sa.Uuid(),
            nullable=True,
            comment="Matched award, once award data is ingested.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assertion_id"],
            ["core.assertion.id"],
        ),
        sa.ForeignKeyConstraint(
            ["award_id"],
            ["core.award.id"],
        ),
        sa.ForeignKeyConstraint(
            ["funder_id"],
            ["core.funder.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
        comment="A grant number found in a posting, raw and normalised.",
    )
    op.create_table(
        "person_pseudonym",
        sa.Column(
            "pid",
            sa.String(length=64),
            nullable=False,
            comment="Hex HMAC-SHA256(secret, ORCID or source person key). Never the key itself.",
        ),
        sa.Column(
            "key_kind",
            sa.Enum("orcid", "source_key", name="person_key_kind", schema="core"),
            nullable=False,
            comment="What was hashed: orcid or source_key.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("pid ~ '^[0-9a-f]{64}$'", name="ck_person_pseudonym_pid"),
        sa.PrimaryKeyConstraint("pid"),
        schema="core",
        comment="A countable person with no identifying data (ADR 0011).",
    )
    op.create_table(
        "award_person",
        sa.Column("award_id", sa.Uuid(), nullable=False, comment="Award."),
        sa.Column("pid", sa.String(length=64), nullable=False, comment="Pseudonymised person."),
        sa.Column(
            "role",
            sa.String(length=50),
            nullable=False,
            comment="Role as the award database states it, e.g. 'pi', 'co_pi'.",
        ),
        sa.ForeignKeyConstraint(
            ["award_id"],
            ["core.award.id"],
        ),
        sa.ForeignKeyConstraint(
            ["pid"],
            ["core.person_pseudonym.pid"],
        ),
        sa.PrimaryKeyConstraint("award_id", "pid", "role"),
        schema="core",
        comment="People on an award, as pseudonyms only.",
    )

    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON core.{table}
            FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
        """)


def downgrade() -> None:
    """Drop the tables and the enum created above."""
    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON core.{table}")
    for table in (
        "award_person",
        "person_pseudonym",
        "grant_reference",
        "call_details",
        "award",
        "funder",
    ):
        op.drop_table(table, schema="core")
    op.execute("DROP TYPE IF EXISTS core.person_key_kind")
