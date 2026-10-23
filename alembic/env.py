"""Alembic environment configuration."""

import os

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object — provides access to values within alembic.ini.
config = context.config

# Override sqlalchemy.url from environment.
# AOI_DATABASE_URL must be set; load .env if running outside the normal shell env.
_database_url = os.environ.get("AOI_DATABASE_URL")
if not _database_url:
    # Attempt to load .env so alembic upgrade head works from a fresh terminal.
    try:
        from dotenv import load_dotenv

        load_dotenv()
        _database_url = os.environ.get("AOI_DATABASE_URL")
    except ImportError:
        pass

if _database_url:
    config.set_main_option("sqlalchemy.url", _database_url)

from aoi.db.schema import LayerBase  # noqa: E402

target_metadata = LayerBase.metadata

# Autogenerate compares only our schemas (ADR 0007), not system ones.
_SCHEMAS = {None, "public", "evidence", "core", "analytics", "ops"}


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Limit autogenerate reflection to the AOI schemas."""
    if type_ == "schema":
        return name in _SCHEMAS
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL without a live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply to a live database)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
