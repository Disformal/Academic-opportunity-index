"""Pytest configuration and shared fixtures for AOI tests."""

import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from botocore.client import BaseClient
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Load .env into os.environ before any fixture or guard check runs.
# This is the only place dotenv is called — tests read from os.environ after this.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# Tests get their own buckets, as they get their own database: the dev buckets hold evidence
# that is kept forever (ADR 0006). Environment variables override .env in Settings.
os.environ["AOI_S3_BUCKET_EVIDENCE"] = "aoi-test-evidence"
os.environ["AOI_S3_BUCKET_DERIVED"] = "aoi-test-derived"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run if the test database is not configured correctly."""
    url = os.environ.get("AOI_TEST_DATABASE_URL", "")
    if not url:
        raise pytest.UsageError(
            "AOI_TEST_DATABASE_URL not set. Copy .env.example to .env and set it."
        )
    if "aoi_test" not in url:
        raise pytest.UsageError(
            "AOI_TEST_DATABASE_URL must point to the test database (aoi_test), not production."
        )


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    """Run Alembic migrations against the test database before any tests."""
    result = _alembic("upgrade", "head")
    if result.returncode != 0:
        pytest.exit(
            f"Alembic migration failed on test database:\n{result.stderr}",
            returncode=1,
        )


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        env={**os.environ, "AOI_DATABASE_URL": os.environ["AOI_TEST_DATABASE_URL"]},
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def migrate() -> Callable[..., None]:
    """Run an Alembic command (e.g. "downgrade", "<rev>") on the test database; it must succeed.

    A test that downgrades upgrades to head again in a `finally`, or later tests run on an old
    schema.
    """

    def run(*args: str) -> None:
        result = _alembic(*args)
        assert result.returncode == 0, result.stderr

    return run


@pytest.fixture(scope="session")
def engine():  # type: ignore[no-untyped-def]
    """Create a SQLAlchemy engine connected to the test database."""
    url = os.environ["AOI_TEST_DATABASE_URL"]
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine) -> Session:  # type: ignore[no-untyped-def]
    """Provide a transactional database session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    yield session
    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture(scope="session")
def s3_client() -> BaseClient:
    """Boto3 S3 client for test verification (the buckets are the test ones; see above)."""
    import boto3
    from botocore.config import Config

    from aoi.config import get_settings

    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


@pytest.fixture(scope="session", autouse=True)
def ensure_s3_buckets() -> None:
    """Create S3 buckets once before any tests that need them."""
    from aoi.storage.evidence import ensure_buckets

    ensure_buckets()


def _clean_queue(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ops.task"))
        conn.execute(text("DELETE FROM core.source WHERE name LIKE 'queue-test-%'"))


@pytest.fixture()
def queue_source_id(engine: Engine) -> Iterator[uuid.UUID]:
    """A committed source for committing ops.task tests; truncates ops.task before and after.

    Cleaning before as well as after matters: a killed run leaves committed tasks behind.
    """
    from aoi.db.schema.core import Source
    from aoi.db.schema.enums import SourceKind

    _clean_queue(engine)
    with Session(engine) as session, session.begin():
        source = Source(
            name=f"queue-test-{uuid.uuid4().hex[:8]}",
            kind=SourceKind.API,
            base_url="https://inspirehep.net/api/jobs",
        )
        session.add(source)
        session.flush()
        sid = source.id
    yield sid
    _clean_queue(engine)


@pytest.fixture()
def add_tasks(engine: Engine) -> Callable[..., list[uuid.UUID]]:
    """Commit the given ops.task rows; return their ids in order."""
    from aoi.db.schema.ops import Task

    def add(*tasks: Task) -> list[uuid.UUID]:
        with Session(engine) as session, session.begin():
            session.add_all(tasks)
            session.flush()
            return [t.id for t in tasks]

    return add


@pytest.fixture()
def add_host(engine: Engine, queue_source_id: uuid.UUID) -> Iterator[Callable[..., uuid.UUID]]:
    """Commit an ops.host row (extra keyword arguments set its columns); returns its id.

    Hosts are removed after the test, once their tasks have been truncated.
    """
    from aoi.db.schema.ops import Host

    def add(*, rps: str = "0.5", **fields: Any) -> uuid.UUID:
        with Session(engine) as session, session.begin():
            host = Host(
                hostname=f"queue-test-{uuid.uuid4().hex[:8]}.example.org",
                rps=Decimal(rps),
                **fields,
            )
            session.add(host)
            session.flush()
            return host.id

    yield add
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ops.task"))
        conn.execute(text("DELETE FROM ops.host WHERE hostname LIKE 'queue-test-%'"))


@pytest.fixture()
def add_target(
    engine: Engine, queue_source_id: uuid.UUID, add_host: Callable[..., uuid.UUID]
) -> Iterator[Callable[..., uuid.UUID]]:
    """Commit an ops.crawl_target of the queue test source on a fresh fast host; returns its id.

    Cleanup truncates what the pipeline wrote from it (fetches, extraction runs, postings and
    their assertions; test DB only) and removes the test source's crawl targets.
    """
    import hashlib

    from aoi.db.schema.enums import TargetRole
    from aoi.db.schema.ops import CrawlTarget

    def add(*, url: str, role: TargetRole = TargetRole.LISTING) -> uuid.UUID:
        host_id = add_host(rps="500")
        with Session(engine) as session, session.begin():
            target = CrawlTarget(
                source_id=queue_source_id,
                host_id=host_id,
                url=url,
                url_sha256=hashlib.sha256(url.encode()).hexdigest(),
                role=role,
            )
            session.add(target)
            session.flush()
            return target.id

    yield add
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE evidence.fetch, core.extraction_run, core.posting CASCADE"))
        conn.execute(text("TRUNCATE ops.task"))
        conn.execute(
            text("DELETE FROM ops.crawl_target WHERE source_id = :id"), {"id": queue_source_id}
        )
