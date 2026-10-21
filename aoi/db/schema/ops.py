"""ops schema: crawl scheduling, politeness and ground-truth samples and labels."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from aoi.db.schema.base import LayerBase, created_at, pg_enum, pk, updated_at
from aoi.db.schema.enums import CircuitState, TargetRole, TaskState

SCHEMA = "ops"


class Host(LayerBase):
    __tablename__ = "host"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_host_hostname"),
        CheckConstraint("rps > 0", name="ck_host_rps_positive"),
        {"schema": SCHEMA, "comment": "Per-host politeness and circuit-breaker state."},
    )

    id: Mapped[uuid.UUID] = pk()
    hostname: Mapped[str] = mapped_column(
        String(253), nullable=False, comment="Lower-case hostname, no port."
    )
    rps: Mapped[Decimal] = mapped_column(
        Numeric(6, 3),
        nullable=False,
        server_default="0.5",
        comment="Maximum requests per second to this host.",
    )
    next_allowed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="Earliest time the next request to this host may start.",
    )
    circuit_state: Mapped[CircuitState] = mapped_column(
        pg_enum(CircuitState, "circuit_state", SCHEMA),
        nullable=False,
        server_default="closed",
        comment="closed = requests flow; open = paused after failures; half_open = probing.",
    )
    consecutive_failures: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default="0",
        comment="Failures since the last success; drives the circuit breaker.",
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class CrawlTarget(LayerBase):
    __tablename__ = "crawl_target"
    __table_args__ = (
        UniqueConstraint("source_id", "url_sha256", name="uq_crawl_target_source_url"),
        CheckConstraint(
            "recrawl_interval BETWEEN interval '1 hour' AND interval '7 days'",
            name="ck_crawl_target_recrawl_bounds",
        ),
        {"schema": SCHEMA, "comment": "A URL we revisit on an adaptive schedule."},
    )

    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.source.id"), nullable=False, comment="The source this URL belongs to."
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops.host.id"), nullable=False, comment="Host for politeness accounting."
    )
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="Canonical URL.")
    url_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Hex SHA-256 of `url`; the uniqueness key."
    )
    role: Mapped[TargetRole] = mapped_column(
        pg_enum(TargetRole, "target_role", SCHEMA),
        nullable=False,
        comment="listing, detail or feed.",
    )
    etag: Mapped[str | None] = mapped_column(
        String(300), comment="ETag from the last successful response, for conditional GET."
    )
    last_modified: Mapped[str | None] = mapped_column(
        String(100), comment="Last-Modified header from the last successful response, verbatim."
    )
    content_sha256: Mapped[str | None] = mapped_column(
        String(64), comment="Hex SHA-256 of the last body; extraction runs only when it changes."
    )
    recrawl_interval: Mapped[timedelta] = mapped_column(
        Interval,
        nullable=False,
        server_default=text("interval '1 day'"),
        comment="Adaptive: halves on change, doubles otherwise, within [1 h, 7 d].",
    )
    next_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="When the scheduler should next enqueue a fetch.",
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When it was retired: never fetched again, ignored by lifecycle passes; kept.",
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


# --- ground truth: samples and labels (plan §4.2; labels come only from the project reviewer) ---


class Sample(LayerBase):
    __tablename__ = "sample"
    __table_args__ = (
        UniqueConstraint("name", name="uq_sample_name"),
        CheckConstraint("n > 0 AND n <= frame_size", name="ck_sample_n"),
        {"schema": SCHEMA, "comment": "A reproducible random sample drawn for labelling."},
    )

    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Unique name, e.g. 'g1-deadline-2026-11'."
    )
    gate: Mapped[str | None] = mapped_column(
        String(10), comment="Launch gate this sample measures (G1-G6), if any."
    )
    frame: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Definition of the population sampled from (SQL or prose)."
    )
    frame_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Population size N when the sample was drawn."
    )
    seed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Random seed; with frame, reproduces the draw."
    )
    n: Mapped[int] = mapped_column(Integer, nullable=False, comment="Number of items drawn.")
    strata: Mapped[Any | None] = mapped_column(
        JSONB, comment="Per stratum: {name: {frame_size, n}}; null for a simple random sample."
    )
    drawn_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Who or what drew the sample (tool and version)."
    )
    created_at: Mapped[datetime] = created_at()


class SampleItem(LayerBase):
    __tablename__ = "sample_item"
    __table_args__ = (
        UniqueConstraint("sample_id", "ordinal", name="uq_sample_item_ordinal"),
        UniqueConstraint("sample_id", "item", name="uq_sample_item_item"),
        {"schema": SCHEMA, "comment": "One drawn item of a sample."},
    )

    id: Mapped[uuid.UUID] = pk()
    sample_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops.sample.id"), nullable=False, comment="The sample."
    )
    ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Draw order within the sample, from 1."
    )
    stratum: Mapped[str | None] = mapped_column(
        String(100), comment="Stratum the item was drawn from; null if unstratified."
    )
    item: Mapped[Any] = mapped_column(
        JSONB,
        nullable=False,
        comment='What is labelled, e.g. {"posting_id": ...} or {"posting_ids": [a, b]}.',
    )


class Label(LayerBase):
    __tablename__ = "label"
    __table_args__ = (
        Index(
            "uq_label_current_item_labeller",
            "sample_item_id",
            "labeller",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        {
            "schema": SCHEMA,
            "comment": "A human judgement on a sample item. Only the project reviewer's labels "
            "count toward a gate.",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    sample_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops.sample_item.id"), nullable=False, comment="The item judged."
    )
    labeller: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Who labelled it, e.g. 'reviewer'."
    )
    label: Mapped[Any] = mapped_column(
        JSONB, nullable=False, comment='The judgement, e.g. {"correct": true}.'
    )
    note: Mapped[str | None] = mapped_column(Text, comment="Free-text remark by the labeller.")
    labelled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="When the label was given.",
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the same labeller replaced this label."
    )


class ErasureLog(LayerBase):
    __tablename__ = "erasure_log"
    __table_args__ = (
        {
            "schema": SCHEMA,
            "comment": "Rows deleted under a legal erasure (ADR 0027). Append-only (ADR 0028).",
        },
    )

    # Written by the ops.guard_delete() trigger, so the key is generated server-side.
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    table_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Schema-qualified table the row was deleted from."
    )
    row_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False, comment="Primary key of the deleted row (its content is not kept)."
    )
    erasure_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Ticket of the erasure request, from aoi.erasure_ref."
    )
    erased_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("CURRENT_USER"),
        comment="Database role that ran the delete.",
    )
    erased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="When the row was deleted.",
    )


class Task(LayerBase):
    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_task_attempts"),
        CheckConstraint("(locked_by IS NULL) = (locked_at IS NULL)", name="ck_task_lock_pair"),
        CheckConstraint(
            "state <> 'running' OR locked_by IS NOT NULL", name="ck_task_running_locked"
        ),
        Index(
            "ix_task_claimable",
            "kind",
            "run_after",
            postgresql_where="state IN ('queued', 'failed')",
        ),
        Index(
            "uq_task_open_target",
            "kind",
            "crawl_target_id",
            unique=True,
            postgresql_where="crawl_target_id IS NOT NULL "
            "AND state IN ('queued', 'running', 'failed')",
        ),
        Index(
            "uq_task_open_discover",
            "source_id",
            unique=True,
            postgresql_where="kind = 'discover' AND state IN ('queued', 'running', 'failed')",
        ),
        UniqueConstraint("retry_of", name="uq_task_retry_of"),
        {
            "schema": SCHEMA,
            "comment": "The work queue (ADR 0014, 0030). Workers claim under a lease.",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Handler that runs it: discover, fetch, extract, …"
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.source.id"), nullable=False, comment="Source the work belongs to."
    )
    crawl_target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops.crawl_target.id"),
        comment="Target a fetch task fetches; at most one open task per kind and target.",
    )
    host_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops.host.id"),
        comment="Host the task contacts; claimable only once the host is due.",
    )
    payload: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default="{}", comment="Handler input, e.g. a target id."
    )
    state: Mapped[TaskState] = mapped_column(
        pg_enum(TaskState, "task_state", SCHEMA),
        nullable=False,
        server_default="queued",
        comment="queued, running, done, failed (retry at run_after) or dead (gave up).",
    )
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", comment="Higher is claimed first."
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="Not claimable before this time (the retry backoff).",
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default="0",
        comment="Claims so far; counted at claim time, so crashes count too.",
    )
    locked_by: Mapped[str | None] = mapped_column(
        String(100), comment="Worker holding the lease, e.g. 'host:pid'."
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the lease was taken."
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, comment="Error class and message of the last failed attempt."
    )
    retry_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops.task.id"),
        comment="Dead task this one retries after review (aoi task requeue); one retry each.",
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
