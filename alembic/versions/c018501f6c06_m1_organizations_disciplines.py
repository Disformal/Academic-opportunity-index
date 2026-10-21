"""m1_organizations_disciplines

Reference entities: core.organization (ROR mirror, ADR 0009), core.organization_relation,
core.org_alias (raw string -> ROR, superseded never overwritten) and core.discipline.

Revision ID: c018501f6c06
Revises: 58e91fc1e994
Create Date: 2026-10-17 14:05:31.402117

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c018501f6c06"
down_revision: str | Sequence[str] | None = "58e91fc1e994"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS = ["org_status", "org_relation_type", "alias_method", "discipline_scheme"]


def upgrade() -> None:
    """Create the organization and discipline tables."""
    op.create_table(
        "organization",
        sa.Column(
            "ror_id",
            sa.String(9),
            primary_key=True,
            comment="Bare ROR ID, e.g. '01ggx4157' (no URL prefix).",
        ),
        sa.Column("display_name", sa.Text(), nullable=False, comment="The ROR display name."),
        sa.Column(
            "names",
            postgresql.JSONB(),
            nullable=False,
            comment="All ROR names: [{value, types, lang}] (labels, aliases, acronyms).",
        ),
        sa.Column(
            "types",
            postgresql.ARRAY(sa.String(20)),
            nullable=False,
            comment="ROR types, e.g. education, facility, funder, government.",
        ),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", "withdrawn", name="org_status", schema="core"),
            nullable=False,
            comment="active, inactive or withdrawn.",
        ),
        sa.Column(
            "country_code",
            sa.String(2),
            nullable=True,
            comment="ISO 3166-1 alpha-2 of the first ROR location.",
        ),
        sa.Column(
            "city", sa.Text(), nullable=True, comment="GeoNames name of the first location."
        ),
        sa.Column("lat", sa.Double(), nullable=True, comment="Latitude of the first location."),
        sa.Column("lon", sa.Double(), nullable=True, comment="Longitude of the first location."),
        sa.Column("established", sa.SmallInteger(), nullable=True, comment="Year founded."),
        sa.Column(
            "fundref_ids",
            postgresql.ARRAY(sa.String(30)),
            server_default="{}",
            nullable=False,
            comment="Crossref Funder IDs listed in ROR; the join key to funder data.",
        ),
        sa.Column(
            "ror_release",
            sa.String(30),
            nullable=False,
            comment="ROR dump release this row was loaded from.",
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
        sa.CheckConstraint("ror_id ~ '^0[a-z0-9]{6}[0-9]{2}$'", name="ck_organization_ror_id"),
        schema="core",
        comment="Mirror of the ROR registry, one row per ROR record.",
    )
    op.execute("""
        CREATE TRIGGER trg_organization_updated_at
        BEFORE UPDATE ON core.organization
        FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
    """)

    op.create_table(
        "organization_relation",
        sa.Column(
            "ror_id",
            sa.String(9),
            sa.ForeignKey("core.organization.ror_id"),
            nullable=False,
            comment="The organization the relation is on.",
        ),
        sa.Column(
            "related_ror_id",
            sa.String(9),
            sa.ForeignKey("core.organization.ror_id"),
            nullable=False,
            comment="The related organization.",
        ),
        sa.Column(
            "type",
            sa.Enum(
                "parent",
                "child",
                "related",
                "successor",
                "predecessor",
                name="org_relation_type",
                schema="core",
            ),
            nullable=False,
            comment="parent, child, related, successor or predecessor (as seen from ror_id).",
        ),
        sa.PrimaryKeyConstraint("ror_id", "related_ror_id", "type"),
        schema="core",
        comment="ROR relationships between organizations.",
    )

    op.create_table(
        "org_alias",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "raw",
            sa.Text(),
            nullable=False,
            comment="Organization string exactly as a source wrote it.",
        ),
        sa.Column(
            "ror_id",
            sa.String(9),
            sa.ForeignKey("core.organization.ror_id"),
            nullable=True,
            comment="Matched organization; null = checked, no confident match.",
        ),
        sa.Column(
            "method",
            sa.Enum("ror_api", "rule", "human", name="alias_method", schema="core"),
            nullable=False,
            comment="ror_api (affiliation matching), rule or human.",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(4, 3),
            nullable=True,
            comment="Match score in [0, 1]; required unless method is human.",
        ),
        sa.Column(
            "decided_by",
            sa.String(100),
            nullable=False,
            comment="Matcher name and version, or the human reviewer.",
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
            comment="When a newer mapping replaced this one.",
        ),
        sa.CheckConstraint(
            "method = 'human' OR confidence IS NOT NULL", name="ck_org_alias_confidence"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_org_alias_range"),
        schema="core",
        comment="Raw organization string -> ROR. Superseded, never overwritten.",
    )
    op.create_index(
        "uq_org_alias_current_raw",
        "org_alias",
        ["raw"],
        unique=True,
        schema="core",
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "discipline",
        sa.Column(
            "code",
            sa.String(40),
            primary_key=True,
            comment="Scheme-prefixed code, e.g. 'fos:1.3', 'arxiv:hep-th'.",
        ),
        sa.Column(
            "scheme",
            sa.Enum("oecd_fos_2007", "arxiv", name="discipline_scheme", schema="core"),
            nullable=False,
            comment="oecd_fos_2007 or arxiv.",
        ),
        sa.Column("label", sa.Text(), nullable=False, comment="English label."),
        sa.Column(
            "parent_code",
            sa.String(40),
            sa.ForeignKey("core.discipline.code"),
            nullable=True,
            comment="Broader discipline; arXiv categories point to their FOS field.",
        ),
        schema="core",
        comment="Taxonomy: OECD FOS 2007 plus arXiv physics categories.",
    )


def downgrade() -> None:
    """Drop the organization and discipline tables and their enums."""
    op.drop_table("discipline", schema="core")
    op.drop_index("uq_org_alias_current_raw", table_name="org_alias", schema="core")
    op.drop_table("org_alias", schema="core")
    op.drop_table("organization_relation", schema="core")
    op.execute("DROP TRIGGER IF EXISTS trg_organization_updated_at ON core.organization")
    op.drop_table("organization", schema="core")
    for name in _ENUMS:
        op.execute(f"DROP TYPE IF EXISTS core.{name}")
