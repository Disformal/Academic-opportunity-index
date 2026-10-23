"""Operator commands for crawl targets: add one (e.g. a one-off archive sweep) and retire some.

A retired target keeps its row and its fetches (nothing is deleted); the scheduler never queues it
and lifecycle passes ignore it. Workers extracting listings add the pages they link to; two
workers adding the same new URL (or host) at once get the same row. These tests really commit.
"""

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from aoi.adapters.protocol import CrawlTargetSpec
from aoi.db.schema.core import Source
from aoi.db.schema.enums import TargetRole
from aoi.db.schema.ops import CrawlTarget, Host
from aoi.tasks import targets

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
HOST = "https://queue-test-targets.example.org"


@pytest.fixture()
def source_name(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """The queue test source's name, with the CLI pointed at the test DB (add_target cleans up)."""
    from aoi.db import session as db_session_module

    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)
    with Session(engine) as session:
        return session.get_one(Source, queue_source_id).name


def _targets(engine: Engine) -> dict[str, CrawlTarget]:
    with Session(engine) as session:
        rows = {t.url: t for t in session.scalars(select(CrawlTarget))}
        session.expunge_all()
        return rows


def test_retire_marks_the_matching_targets_of_the_source(
    engine: Engine, source_name: str, add_target: AddTarget
) -> None:
    from aoi import cli

    add_target(url=f"{HOST}/api/jobs?size=25&page=1")
    add_target(url=f"{HOST}/api/jobs?size=25&page=2")
    add_target(url=f"{HOST}/api/jobs?size=250&page=1")

    cli.main(["target", "retire", "--source", source_name, "--match", "size=25&"])

    retired = {url: t.retired_at is not None for url, t in _targets(engine).items()}
    assert retired == {
        f"{HOST}/api/jobs?size=25&page=1": True,
        f"{HOST}/api/jobs?size=25&page=2": True,
        f"{HOST}/api/jobs?size=250&page=1": False,
    }


def test_retire_keeps_the_first_retirement_time(
    engine: Engine, source_name: str, add_target: AddTarget
) -> None:
    from aoi import cli

    add_target(url=f"{HOST}/api/jobs?size=25&page=1")
    cli.main(["target", "retire", "--source", source_name, "--match", "size=25"])
    first = _targets(engine)[f"{HOST}/api/jobs?size=25&page=1"].retired_at

    cli.main(["target", "retire", "--source", source_name, "--match", "size=25"])

    assert _targets(engine)[f"{HOST}/api/jobs?size=25&page=1"].retired_at == first


def test_restore_clears_the_retirement_of_the_matching_targets_only(
    engine: Engine, source_name: str, add_target: AddTarget
) -> None:
    from aoi import cli

    add_target(url=f"{HOST}/api/jobs?size=25&page=1")
    add_target(url=f"{HOST}/api/jobs?size=250&page=1")
    cli.main(["target", "retire", "--source", source_name, "--match", "size=25"])

    cli.main(["target", "restore", "--source", source_name, "--match", "size=250"])

    retired = {url: t.retired_at is not None for url, t in _targets(engine).items()}
    assert retired == {
        f"{HOST}/api/jobs?size=25&page=1": True,
        f"{HOST}/api/jobs?size=250&page=1": False,
    }


def test_add_creates_a_due_listing_target_with_its_canonical_url(
    engine: Engine, source_name: str
) -> None:
    from aoi import cli

    cli.main(
        ["target", "add", "--source", source_name, "--role", "listing", f"{HOST}/api/jobs?page=1"]
    )

    (target,) = _targets(engine).values()
    assert target.role == TargetRole.LISTING
    assert target.retired_at is None
    assert target.url.startswith(HOST)


def _add_and_commit(engine: Engine, source_id: uuid.UUID, spec: CrawlTargetSpec) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        return targets.add_target(session, source_id, spec).id


def _wait_for_a_blocked_insert(engine: Engine) -> None:
    """Until a backend of the test database waits on a lock (a fresh snapshot each poll)."""
    deadline = time.monotonic() + 10
    query = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while True:
        with engine.connect() as conn:
            if conn.scalar(query):
                return
        assert time.monotonic() < deadline, "the second insert never waited on the first"
        time.sleep(0.05)


def _race(engine: Engine, source_id: uuid.UUID, spec: CrawlTargetSpec) -> tuple[uuid.UUID, ...]:
    """The first worker inserts the target and holds it uncommitted while the second, which
    found nothing, inserts the same URL; then the first commits. Both targets' ids."""
    with Session(engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_id = targets.add_target(first, source_id, spec).id
        second = pool.submit(_add_and_commit, engine, source_id, spec)
        _wait_for_a_blocked_insert(engine)
        first.commit()
        return first_id, second.result()


def test_two_workers_adding_the_same_new_url_get_the_same_target(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_host: Callable[..., uuid.UUID],
    add_target: AddTarget,
) -> None:
    with Session(engine) as session:
        hostname = session.get_one(Host, add_host()).hostname
    spec = CrawlTargetSpec(f"https://{hostname}/api/jobs?page=2", TargetRole.LISTING)

    first_id, second_id = _race(engine, queue_source_id, spec)

    assert first_id == second_id
    assert list(_targets(engine)) == [f"https://{hostname}/api/jobs?page=2"]


def test_two_workers_adding_urls_on_the_same_new_host_get_the_same_host(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    hostname = f"queue-test-{uuid.uuid4().hex[:8]}.example.org"
    spec = CrawlTargetSpec(f"https://{hostname}/api/jobs?page=2", TargetRole.LISTING)

    first_id, second_id = _race(engine, queue_source_id, spec)

    assert first_id == second_id
    with Session(engine) as session:
        hosts = session.scalars(select(Host).where(Host.hostname == hostname)).all()
    assert len(hosts) == 1
