"""Declarative base and column helpers for the layered schemas."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import text


class LayerBase(DeclarativeBase):
    pass


def pg_enum(py_enum: type[enum.Enum], name: str, schema: str) -> SAEnum:
    """A PostgreSQL enum in `schema` that stores the value (lowercase string), not the name."""
    return SAEnum(
        py_enum, name=name, schema=schema, values_callable=lambda e: [m.value for m in e]
    )


def pk() -> Mapped[uuid.UUID]:
    """Standard UUID primary key with auto-generated default."""
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


def updated_at() -> Mapped[datetime]:
    """Maintained by the update_updated_at() trigger."""
    return mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
