"""AOI health check — verifies DB, S3, migrations, tests, and lint."""

import subprocess
import sys
from pathlib import Path

import structlog
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from aoi.config import get_settings

log = structlog.get_logger()


def _check_database(database_url: str) -> bool:
    """Return True if the database is reachable and SELECT 1 succeeds."""
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        log.info("health.db", status="ok", url=database_url.split("@")[-1])
        return True
    except Exception as exc:
        log.error("health.db", status="error", error=str(exc))
        return False


def _check_s3(endpoint: str) -> bool:
    """Return True if the S3 endpoint is reachable and required buckets exist."""
    try:
        from aoi.storage.evidence import ensure_buckets

        ensure_buckets()
        log.info("health.s3", status="ok", endpoint=endpoint)
        return True
    except Exception as exc:
        log.error("health.s3", status="error", endpoint=endpoint, error=str(exc))
        return False


def _check_migrations(database_url: str, alembic_ini: str) -> bool:
    """Return True if the database is at the Alembic head revision."""
    try:
        cfg = AlembicConfig(alembic_ini)
        script = ScriptDirectory.from_config(cfg)
        head_revision: str | None = script.get_current_head()

        engine = create_engine(database_url)
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_revision = ctx.get_current_revision()
        engine.dispose()

        if head_revision is None:
            log.warning("health.migrations", status="no_migrations", current=current_revision)
            return True  # no migrations yet is not a failure at Stage 1

        ok = current_revision == head_revision
        log.info(
            "health.migrations",
            status="ok" if ok else "behind",
            current=current_revision,
            head=head_revision,
        )
        return ok
    except Exception as exc:
        log.error("health.migrations", status="error", error=str(exc))
        return False


def _check_test_count(tests_dir: Path) -> int:
    """Return the count of test files matching tests/test_*.py."""
    return len(list(tests_dir.glob("test_*.py")))


def _check_lint(project_root: Path) -> bool:
    """Return True if ruff check passes with no errors."""
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "."],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    log.info("health.lint", status="ok" if ok else "errors", output=result.stdout.strip() or None)
    return ok


def main() -> int:
    """Run all health checks and return exit code 0 if critical checks pass."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
        processors=[
            structlog.dev.ConsoleRenderer(),
        ],
    )

    project_root = Path(__file__).parent.parent
    alembic_ini = str(project_root / "alembic.ini")
    tests_dir = project_root / "tests"

    settings = get_settings()

    db_ok = _check_database(settings.database_url)
    s3_ok = _check_s3(settings.s3_endpoint)
    _check_migrations(settings.database_url, alembic_ini)

    test_count = _check_test_count(tests_dir)
    log.info("health.tests", count=test_count)

    _check_lint(project_root)

    critical_ok = db_ok and s3_ok
    log.info("health.summary", critical_ok=critical_ok, db=db_ok, s3=s3_ok)
    return 0 if critical_ok else 1


if __name__ == "__main__":
    sys.exit(main())
