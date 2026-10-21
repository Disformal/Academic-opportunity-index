"""A real pipeline worker (fetch, extract, resolve, lifecycle) for tests/test_chaos_pipeline.py,
which SIGKILLs it. Not a test.

Only the network boundary is replaced: fetch() serves deterministic JSON listing pages for
https://queue-test-e2e.example.org/api/jobs?list=L&page=P (three jobs per page; list 0 has three
pages, the others one). The handlers, the adapter, the object store and the database are real.
Each handler pauses after its writes, so a kill lands between the writes and the commit.
"""

import json
import os
import time
from urllib.parse import parse_qs, urlsplit

from fake_adapter import FakeApiAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aoi.db.schema.ops import Task
from aoi.pipeline.fetch import FetchResult
from aoi.tasks.handlers import fetch as fetch_handler
from aoi.tasks.handlers import handlers_for
from aoi.tasks.worker import Handler, serve

HOST = "queue-test-e2e.example.org"


def page_body(url: str) -> bytes:
    query = parse_qs(urlsplit(url).query)
    listing, page = int(query["list"][0]), int(query["page"][0])
    hits = [
        {
            "id": listing * 100 + page * 10 + i,
            "title": f"Postdoc {listing}-{page}-{i}",
            "url": f"https://{HOST}/jobs/{listing}-{page}-{i}",
        }
        for i in range(3)
    ]
    more = listing == 0 and page < 3
    next_url = f"https://{HOST}/api/jobs?list={listing}&page={page + 1}" if more else None
    return json.dumps({"hits": hits, "next": next_url}).encode()


def fake_fetch(url: str, **_conditional: object) -> FetchResult:
    time.sleep(0.05)
    return FetchResult(
        url=url,
        final_url=url,
        status=200,
        headers={"content-type": "application/json"},
        body=page_body(url),
        elapsed_ms=50,
        not_modified=False,
        resolved_ip="203.0.113.10",
        http_version="HTTP/1.1",
        request_headers={"host": HOST, "user-agent": "aoi-chaos"},
    )


def _pausing(handler: Handler) -> Handler:
    def run(session: Session, task: Task) -> None:
        handler(session, task)
        session.flush()
        time.sleep(0.1)

    return run


def main() -> None:
    url = os.environ["AOI_TEST_DATABASE_URL"]
    if "aoi_test" not in url:
        raise SystemExit("chaos_pipeline_worker runs only against the test database")
    fetch_handler.fetch = fake_fetch  # type: ignore[assignment]
    handlers = handlers_for(
        ["fetch", "extract", "resolve", "lifecycle"],
        {os.environ["CHAOS_SOURCE"]: FakeApiAdapter()},
    )
    serve(
        create_engine(url),
        {kind: _pausing(handler) for kind, handler in handlers.items()},
        worker=f"chaos:{os.getpid()}",
        idle_seconds=0.05,
    )


if __name__ == "__main__":
    main()
