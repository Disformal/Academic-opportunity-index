"""Operator commands for crawl targets: add one (e.g. a one-off archive sweep) and retire some.

A retired target keeps its row and its fetches (nothing is deleted); the scheduler never queues it
and lifecycle passes ignore it. These tests really commit.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.enums import TargetRole
from aoi.db.schema.ops import CrawlTarget

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
