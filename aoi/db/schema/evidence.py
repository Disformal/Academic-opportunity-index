"""evidence schema (L0): the fetch log. Bodies live in WARC files (ADR 0006)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from aoi.db.schema.base import LayerBase, created_at, pg_enum, pk
from aoi.db.schema.enums import FetchOutcome

SCHEMA = "evidence"


class Fetch(LayerBase):
    __tablename__ = "fetch"
    __table_args__ = (
        Index("ix_fetch_target_time", "crawl_target_id", "requested_at"),
        CheckConstraint(
            "(warc_key IS NULL) = (warc_record_id IS NULL)", name="ck_fetch_warc_pair"
        ),
        {"schema": SCHEMA, "comment": "Every outbound request, successful or not. Immutable."},
    )

    id: Mapped[uuid.UUID] = pk()
    crawl_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops.crawl_target.id"), nullable=False, comment="The target that was fetched."
    )
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="URL requested.")
    final_url: Mapped[str | None] = mapped_column(Text, comment="URL after redirects.")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When the request started (UTC)."
    )
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, comment="Total time to response.")
    outcome: Mapped[FetchOutcome] = mapped_column(
        pg_enum(FetchOutcome, "fetch_outcome", SCHEMA),
        nullable=False,
        comment="hit, not_modified, error, blocked (SSRF / robots) or rate_limited.",
    )
    http_status: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Final HTTP status; null when no response was received."
    )
    resolved_ip: Mapped[str | None] = mapped_column(
        INET, comment="Address validate_host resolved for the final URL's host (ADR 0023)."
    )
    body_sha256: Mapped[str | None] = mapped_column(String(64), comment="Hex SHA-256 of the body.")
    body_bytes: Mapped[int | None] = mapped_column(Integer, comment="Body size in bytes.")
    warc_record_id: Mapped[str | None] = mapped_column(
        String(100),
        comment="WARC-Record-ID of the response or revisit record; null if no response came.",
    )
    warc_key: Mapped[str | None] = mapped_column(
        Text, comment="S3 key of the WARC object, stored before this row (ADR 0027, 0030)."
    )
    error: Mapped[str | None] = mapped_column(Text, comment="Error class and message, if any.")
    created_at: Mapped[datetime] = created_at()
