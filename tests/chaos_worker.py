"""A worker process for the chaos test (SIGKILL) and the clean-stop test (SIGTERM). Not a test."""

import os
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aoi.db.schema.ops import Task
from aoi.tasks.worker import serve


def _slow_child(session: Session, task: Task) -> None:
    session.add(Task(kind="child", source_id=task.source_id, payload={"parent": str(task.id)}))
    session.flush()
    # Widen the window in which a kill lands mid-task, after the write reached the database.
    time.sleep(0.2)


def main() -> None:
    url = os.environ["AOI_TEST_DATABASE_URL"]
    if "aoi_test" not in url:
        raise SystemExit("chaos_worker runs only against the test database")
    serve(
        create_engine(url),
        {"chaos": _slow_child},
        worker=f"chaos:{os.getpid()}",
        idle_seconds=0.05,
    )


if __name__ == "__main__":
    main()
