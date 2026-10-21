"""m1_positions_links_history_details

L2 entities of ADR 0008: core.position, core.position_discipline, core.position_link
(superseded, never deleted; one current link per posting), core.posting_history and
core.position_history (observed_from/observed_to = observed time; source-stated dates are
separate columns), and the kind-specific core.job_details / core.fellowship_details.

The details tables carry (position_id, kind) with a composite foreign key to
core.position(id, kind) and a CHECK on kind, so details can only attach to positions of
the right kind. Scholarships share fellowship_details.

Revision ID: 59d1d8c85e66
Revises: c018501f6c06
Create Date: 2026-10-17 13:47:34.039183

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "59d1d8c85e66"
down_revision: str | Sequence[str] | None = "c018501f6c06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once here and shared between tables (core.opportunity_kind exists already).
_ENUMS: dict[str, tuple[str, ...]] = {
    "date_precision": ("day", "month", "year"),
    "link_method": ("auto", "human"),
    "career_stage": ("r1", "r2", "r3", "r4"),
    "required_degree": ("none", "bachelor", "master", "phd"),
    "contract_type": ("permanent", "fixed_term", "tenure_track"),
    "salary_period": ("year", "month", "hour"),
}

_TABLES_WITH_UPDATED_AT = ["position"]


def upgrade() -> None:
    """Create the position, link, history and details tables."""
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name, schema="core").create(op.get_bind())

    op.create_table(
        "position",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="opportunity_kind", schema="core", create_type=False),
            nullable=False,
            comment="job, funding_call, fellowship or scholarship.",
        ),
        sa.Column("title", sa.Text(), nullable=False, comment="Resolved title."),
        sa.Column("ror_id", sa.String(length=9), nullable=True, comment="Resolved organization."),
        sa.Column(
            "country_code",
            sa.String(length=2),
            nullable=True,
            comment="ISO 3166-1 alpha-2 where the position is based.",
        ),
        sa.Column("city", sa.Text(), nullable=True, comment="City where the position is based."),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Source-stated publication date (valid time).",
        ),
        sa.Column(
            "deadline_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Application deadline (valid time); null = none stated.",
        ),
        sa.Column(
            "deadline_precision",
            postgresql.ENUM(name="date_precision", schema="core", create_type=False),
            nullable=True,
            comment="day, month or year; set iff deadline_at is set.",
        ),
        sa.Column("application_url", sa.Text(), nullable=True, comment="Where to apply."),
        sa.Column(
            "in_scope",
            sa.Boolean(),
            nullable=True,
            comment="Within the site's coverage (e.g. physics); null until classified.",
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
            "(deadline_at IS NULL) = (deadline_precision IS NULL)",
            name="ck_position_deadline_precision",
        ),
        sa.ForeignKeyConstraint(
            ["ror_id"],
            ["core.organization.ror_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "kind", name="uq_position_id_kind"),
        schema="core",
        comment="The real-world opportunity behind one or more postings.",
    )
    op.create_table(
        "position_discipline",
        sa.Column("position_id", sa.Uuid(), nullable=False, comment="The position."),
        sa.Column(
            "discipline_code",
            sa.String(length=40),
            nullable=False,
            comment="One of its disciplines.",
        ),
        sa.ForeignKeyConstraint(
            ["discipline_code"],
            ["core.discipline.code"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["core.position.id"],
        ),
        sa.PrimaryKeyConstraint("position_id", "discipline_code"),
        schema="core",
        comment="Disciplines a position belongs to.",
    )
    op.create_table(
        "position_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False, comment="The posting being linked."),
        sa.Column(
            "position_id", sa.Uuid(), nullable=False, comment="The position it is linked to."
        ),
        sa.Column(
            "method",
            postgresql.ENUM(name="link_method", schema="core", create_type=False),
            nullable=False,
            comment="auto (linker) or human (review queue).",
        ),
        sa.Column(
            "probability",
            sa.Numeric(precision=4, scale=3),
            nullable=True,
            comment="Linker match probability in [0, 1]; required for auto links.",
        ),
        sa.Column(
            "signals",
            postgresql.JSONB(),
            nullable=True,
            comment="Comparison values that produced the probability.",
        ),
        sa.Column(
            "decided_by",
            sa.String(length=100),
            nullable=False,
            comment="Linker name and version, or the human reviewer.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a newer decision replaced this link.",
        ),
        sa.CheckConstraint(
            "method = 'human' OR probability IS NOT NULL", name="ck_position_link_probability"
        ),
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 1", name="ck_position_link_probability_range"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["core.position.id"],
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["core.posting.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
        comment="posting -> position. Superseded, never deleted (ADR 0008).",
    )
    op.create_index(
        "uq_position_link_current_posting",
        "position_link",
        ["posting_id"],
        unique=True,
        schema="core",
        postgresql_where="superseded_at IS NULL",
    )
    op.create_index(
        "ix_position_link_position", "position_link", ["position_id"], unique=False, schema="core"
    )
    op.create_table(
        "posting_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("posting_id", sa.Uuid(), nullable=False, comment="The posting."),
        sa.Column(
            "observed_from",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time we first saw this version.",
        ),
        sa.Column(
            "observed_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Observed time a newer version replaced it; null = current.",
        ),
        sa.Column(
            "stated_posted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Source-stated publication date (valid time).",
        ),
        sa.Column(
            "stated_deadline_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Source-stated deadline (valid time).",
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(),
            nullable=False,
            comment="All resolved fields of this version.",
        ),
        sa.CheckConstraint("observed_to > observed_from", name="ck_posting_history_order"),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["core.posting.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
        comment="Resolved posting fields over observed time.",
    )
    op.create_index(
        "uq_posting_history_current",
        "posting_history",
        ["posting_id"],
        unique=True,
        schema="core",
        postgresql_where="observed_to IS NULL",
    )
    op.create_table(
        "position_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False, comment="The position."),
        sa.Column(
            "observed_from",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time this version was first resolved.",
        ),
        sa.Column(
            "observed_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Observed time a newer version replaced it; null = current.",
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(),
            nullable=False,
            comment="All resolved fields of this version.",
        ),
        sa.CheckConstraint("observed_to > observed_from", name="ck_position_history_order"),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["core.position.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
        comment="Resolved position fields over observed time.",
    )
    op.create_index(
        "uq_position_history_current",
        "position_history",
        ["position_id"],
        unique=True,
        schema="core",
        postgresql_where="observed_to IS NULL",
    )
    op.create_table(
        "job_details",
        sa.Column("position_id", sa.Uuid(), nullable=False, comment="The position."),
        sa.Column(
            "kind",
            postgresql.ENUM(name="opportunity_kind", schema="core", create_type=False),
            server_default="job",
            nullable=False,
            comment="Always 'job'; part of the foreign key that enforces it.",
        ),
        sa.Column(
            "contract_type",
            postgresql.ENUM(name="contract_type", schema="core", create_type=False),
            nullable=True,
            comment="permanent, fixed_term or tenure_track.",
        ),
        sa.Column(
            "fte",
            sa.Numeric(precision=3, scale=2),
            nullable=True,
            comment="Full-time equivalent in (0, 1].",
        ),
        sa.Column(
            "career_stage",
            postgresql.ENUM(name="career_stage", schema="core", create_type=False),
            nullable=True,
            comment="R1-R4 (EU research careers).",
        ),
        sa.Column(
            "required_degree",
            postgresql.ENUM(name="required_degree", schema="core", create_type=False),
            nullable=True,
            comment="Minimum degree required to apply.",
        ),
        sa.Column(
            "max_years_since_phd",
            sa.SmallInteger(),
            nullable=True,
            comment="Eligibility limit in years after the PhD.",
        ),
        sa.Column(
            "required_languages",
            postgresql.ARRAY(sa.String(length=3)),
            server_default="{}",
            nullable=False,
            comment="ISO 639 codes of languages required for the role.",
        ),
        sa.Column(
            "duration_months",
            sa.SmallInteger(),
            nullable=True,
            comment="Length of the appointment or award.",
        ),
        sa.Column("start_date", sa.Date(), nullable=True, comment="Stated start date."),
        sa.Column(
            "salary_min",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Lower bound of pay or stipend, as stated.",
        ),
        sa.Column(
            "salary_max",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Upper bound of pay or stipend, as stated.",
        ),
        sa.Column(
            "salary_currency",
            sa.String(length=3),
            nullable=True,
            comment="ISO 4217 code of the stated pay.",
        ),
        sa.Column(
            "salary_period",
            postgresql.ENUM(name="salary_period", schema="core", create_type=False),
            nullable=True,
            comment="year, month or hour.",
        ),
        sa.Column(
            "salary_eur_annual",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Midpoint pay normalised to EUR per year (ECB reference rate).",
        ),
        sa.Column(
            "fx_date",
            sa.Date(),
            nullable=True,
            comment="Date of the ECB rate used for salary_eur_annual.",
        ),
        sa.CheckConstraint("kind = 'job'", name="ck_job_details_kind"),
        sa.CheckConstraint("fte > 0 AND fte <= 1", name="ck_job_details_fte"),
        sa.CheckConstraint(
            "salary_eur_annual IS NULL OR fx_date IS NOT NULL", name="ck_job_details_salary_fx"
        ),
        sa.CheckConstraint("salary_max >= salary_min", name="ck_job_details_salary_order"),
        sa.CheckConstraint(
            "salary_min IS NULL AND salary_max IS NULL "
            "OR salary_currency IS NOT NULL AND salary_period IS NOT NULL",
            name="ck_job_details_salary_unit",
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "kind"],
            ["core.position.id", "core.position.kind"],
        ),
        sa.PrimaryKeyConstraint("position_id"),
        schema="core",
        comment="Fields specific to jobs.",
    )
    op.create_table(
        "fellowship_details",
        sa.Column("position_id", sa.Uuid(), nullable=False, comment="The position."),
        sa.Column(
            "kind",
            postgresql.ENUM(name="opportunity_kind", schema="core", create_type=False),
            nullable=False,
            comment="fellowship or scholarship; part of the foreign key that enforces it.",
        ),
        sa.Column(
            "career_stage",
            postgresql.ENUM(name="career_stage", schema="core", create_type=False),
            nullable=True,
            comment="R1-R4 (EU research careers).",
        ),
        sa.Column(
            "required_degree",
            postgresql.ENUM(name="required_degree", schema="core", create_type=False),
            nullable=True,
            comment="Minimum degree required to apply.",
        ),
        sa.Column(
            "max_years_since_phd",
            sa.SmallInteger(),
            nullable=True,
            comment="Eligibility limit in years after the PhD.",
        ),
        sa.Column(
            "required_languages",
            postgresql.ARRAY(sa.String(length=3)),
            server_default="{}",
            nullable=False,
            comment="ISO 639 codes of languages required for the role.",
        ),
        sa.Column(
            "duration_months",
            sa.SmallInteger(),
            nullable=True,
            comment="Length of the appointment or award.",
        ),
        sa.Column("start_date", sa.Date(), nullable=True, comment="Stated start date."),
        sa.Column(
            "salary_min",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Lower bound of pay or stipend, as stated.",
        ),
        sa.Column(
            "salary_max",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Upper bound of pay or stipend, as stated.",
        ),
        sa.Column(
            "salary_currency",
            sa.String(length=3),
            nullable=True,
            comment="ISO 4217 code of the stated pay.",
        ),
        sa.Column(
            "salary_period",
            postgresql.ENUM(name="salary_period", schema="core", create_type=False),
            nullable=True,
            comment="year, month or hour.",
        ),
        sa.Column(
            "salary_eur_annual",
            sa.Numeric(precision=12, scale=2),
            nullable=True,
            comment="Midpoint pay normalised to EUR per year (ECB reference rate).",
        ),
        sa.Column(
            "fx_date",
            sa.Date(),
            nullable=True,
            comment="Date of the ECB rate used for salary_eur_annual.",
        ),
        sa.CheckConstraint(
            "kind IN ('fellowship', 'scholarship')", name="ck_fellowship_details_kind"
        ),
        sa.CheckConstraint(
            "salary_eur_annual IS NULL OR fx_date IS NOT NULL",
            name="ck_fellowship_details_salary_fx",
        ),
        sa.CheckConstraint("salary_max >= salary_min", name="ck_fellowship_details_salary_order"),
        sa.CheckConstraint(
            "salary_min IS NULL AND salary_max IS NULL "
            "OR salary_currency IS NOT NULL AND salary_period IS NOT NULL",
            name="ck_fellowship_details_salary_unit",
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "kind"],
            ["core.position.id", "core.position.kind"],
        ),
        sa.PrimaryKeyConstraint("position_id"),
        schema="core",
        comment="Fields specific to fellowships and scholarships.",
    )

    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON core.{table}
            FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
        """)


def downgrade() -> None:
    """Drop the tables and enums created above."""
    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON core.{table}")
    for table in (
        "fellowship_details",
        "job_details",
        "position_history",
        "posting_history",
        "position_link",
        "position_discipline",
        "position",
    ):
        op.drop_table(table, schema="core")
    for name in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS core.{name}")
