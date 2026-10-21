"""Tests for the INSPIRE-HEP adapter (sources/inspire.py)."""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from aoi.pipeline.sources.inspire import BASE, PAGE_SIZE, RawJob, _page_url, harvest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "inspire_page.json"


def _fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _make_fake_fetch_result(body: bytes, status: int = 200):  # type: ignore[no-untyped-def]
    """Build a minimal FetchResult-like object from the fetch module."""
    from aoi.pipeline.fetch import FetchResult

    return FetchResult(
        url="https://inspirehep.net/api/jobs?page=1",
        status=status,
        headers={"content-type": "application/json"},
        body=body,
        elapsed_ms=120,
        not_modified=False,
        resolved_ip="188.185.0.1",
        final_url="https://inspirehep.net/api/jobs?page=1",
        http_version="HTTP/1.1",
        request_headers={},
    )


# ---------------------------------------------------------------------------
# Unit tests — no network, no DB
# ---------------------------------------------------------------------------


def test_page_url_page_1_contains_base() -> None:
    url = _page_url(1)
    assert url.startswith(BASE)
    assert "page=1" in url
    assert f"size={PAGE_SIZE}" in url


def test_page_url_different_pages() -> None:
    url1 = _page_url(1)
    url2 = _page_url(2)
    assert "page=1" in url1
    assert "page=2" in url2


def test_harvest_yields_raw_jobs_from_fixture() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    assert len(jobs) == 2
    assert all(isinstance(j, RawJob) for j in jobs)


def test_harvest_raw_job_has_control_number() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    control_numbers = {j.control_number for j in jobs}
    assert "1234567" in control_numbers
    assert "7654321" in control_numbers


def test_harvest_scrubs_personal_email_from_description() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    # First job: j.smith@cern.ch should be scrubbed (personal), jobs@cern.ch kept (role)
    first_job = next(j for j in jobs if j.control_number == "1234567")
    assert "j.smith@cern.ch" not in first_job.document_text
    assert "[contact removed]" in first_job.document_text
    assert "jobs@cern.ch" in first_job.document_text


def test_harvest_scrubs_personal_email_keeps_role_in_second_job() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    # Second job: personal email removed, role email kept
    second_job = next(j for j in jobs if j.control_number == "7654321")
    assert "prof.researcher@unige.ch" not in second_job.document_text
    assert "phd-admissions@unige.ch" in second_job.document_text


def test_harvest_json_pointers_populated() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    first = next(j for j in jobs if j.control_number == "1234567")
    assert "position" in first.pointers
    assert "deadline_date" in first.pointers
    assert "status" in first.pointers


def test_harvest_stops_when_hits_less_than_page_size() -> None:
    """If a page returns fewer hits than PAGE_SIZE, pagination stops."""
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()
    fetch_call_count = 0

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    def counting_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        return fake_result

    with (
        patch("aoi.pipeline.sources.inspire.fetch", side_effect=counting_fetch),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        # max_pages=10 but fixture has 2 hits < PAGE_SIZE (250), so stops after page 1
        jobs = list(harvest(source_id, max_pages=10))

    assert fetch_call_count == 1
    assert len(jobs) == 2


def test_harvest_respects_max_pages() -> None:
    """Pagination stops at max_pages even when pages are full."""
    # Build a fixture with exactly PAGE_SIZE hits (so it looks like there are more pages)
    # by duplicating a single hit 250 times
    raw_fixture = json.loads(_fixture_bytes())
    hit = raw_fixture["hits"]["hits"][0]
    full_page_hits = []
    for i in range(PAGE_SIZE):
        h = json.loads(json.dumps(hit))
        h["metadata"]["control_number"] = 1000000 + i
        h["id"] = str(1000000 + i)
        full_page_hits.append(h)

    full_page_fixture = json.dumps(
        {"hits": {"hits": full_page_hits, "total": PAGE_SIZE * 5}}
    ).encode()

    source_id = uuid.uuid4()
    fetch_call_count = 0

    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    def counting_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        from aoi.pipeline.fetch import FetchResult

        return FetchResult(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
            body=full_page_fixture,
            elapsed_ms=120,
            not_modified=False,
            resolved_ip="188.185.0.1",
            final_url=url,
            http_version="HTTP/1.1",
            request_headers={},
        )

    with (
        patch("aoi.pipeline.sources.inspire.fetch", side_effect=counting_fetch),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=3))

    assert fetch_call_count == 3
    assert len(jobs) == PAGE_SIZE * 3


def test_harvest_raw_job_fetch_id_is_uuid() -> None:
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()
    fixed_fetch_id = str(uuid.uuid4())

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": fixed_fetch_id, "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1))

    for job in jobs:
        assert isinstance(job.fetch_id, uuid.UUID)


def test_harvest_early_stop_after_consecutive_known() -> None:
    """Harvest stops after 10 consecutive known items."""
    # Build a fixture with 15 items — first 5 are new, last 10 are known
    raw_fixture = json.loads(_fixture_bytes())
    hit_template = raw_fixture["hits"]["hits"][0]
    hits = []
    for i in range(15):
        h = json.loads(json.dumps(hit_template))
        h["metadata"]["control_number"] = 2000000 + i
        h["id"] = str(2000000 + i)
        hits.append(h)

    page_data = json.dumps({"hits": {"hits": hits, "total": 100}}).encode()

    # Items 5–14 (control numbers 2000005–2000014) are "known"
    known_keys = frozenset(f"inspire:{2000005 + i}" for i in range(10))

    source_id = uuid.uuid4()
    fetch_call_count = 0

    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    def counting_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        from aoi.pipeline.fetch import FetchResult

        return FetchResult(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
            body=page_data,
            elapsed_ms=120,
            not_modified=False,
            resolved_ip="188.185.0.1",
            final_url=url,
            http_version="HTTP/1.1",
            request_headers={},
        )

    with (
        patch("aoi.pipeline.sources.inspire.fetch", side_effect=counting_fetch),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=10, known_keys=known_keys))

    # Should yield only the 5 new items before hitting 10 consecutive known
    assert len(jobs) == 5
    # Should stop after 1 page (early-stop triggered mid-page)
    assert fetch_call_count == 1


def test_harvest_no_early_stop_when_no_known_keys() -> None:
    """When known_keys is None, harvest processes all items without early-stop."""
    fixture = _fixture_bytes()
    source_id = uuid.uuid4()

    fake_result = _make_fake_fetch_result(fixture)
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    with (
        patch("aoi.pipeline.sources.inspire.fetch", return_value=fake_result),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(harvest(source_id, max_pages=1, known_keys=None))

    # All 2 items from fixture yielded — no early-stop
    assert len(jobs) == 2


def test_harvest_early_stop_resets_on_new_item() -> None:
    """Consecutive known counter resets when a new item appears."""
    raw_fixture = json.loads(_fixture_bytes())
    hit_template = raw_fixture["hits"]["hits"][0]
    # Pattern: 5 known, 1 new, 5 known — should NOT trigger early-stop (max streak = 5 < 10)
    hits = []
    for i in range(11):
        h = json.loads(json.dumps(hit_template))
        h["metadata"]["control_number"] = 3000000 + i
        h["id"] = str(3000000 + i)
        hits.append(h)

    page_data = json.dumps({"hits": {"hits": hits, "total": 100}}).encode()

    # Known: 0–4 and 6–10 (items at positions 0-4 and 6-10)
    known_keys = frozenset(
        [f"inspire:{3000000 + i}" for i in range(5)]
        + [f"inspire:{3000000 + i}" for i in range(6, 11)]
    )

    source_id = uuid.uuid4()
    fake_evidence = type(
        "StoredEvidence",
        (),
        {"fetch_id": str(uuid.uuid4()), "content_digest": "a" * 64, "prefix": "evidence/test/"},
    )()

    def counting_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        from aoi.pipeline.fetch import FetchResult

        return FetchResult(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
            body=page_data,
            elapsed_ms=120,
            not_modified=False,
            resolved_ip="188.185.0.1",
            final_url=url,
            http_version="HTTP/1.1",
            request_headers={},
        )

    with (
        patch("aoi.pipeline.sources.inspire.fetch", side_effect=counting_fetch),
        patch("aoi.pipeline.sources.inspire.store_fetch", return_value=fake_evidence),
    ):
        jobs = list(
            harvest(source_id, max_pages=1, known_keys=known_keys, early_stop_consecutive=10)
        )

    # Only item at index 5 (control_number=3000005) is new
    assert len(jobs) == 1
    assert jobs[0].control_number == "3000005"


@pytest.mark.live
def test_inspire_live_pagination() -> None:
    """Live test against the real INSPIRE API. Run manually only."""
    source_id = uuid.uuid4()
    jobs = list(harvest(source_id, max_pages=1))
    assert len(jobs) > 0
    assert all(isinstance(j, RawJob) for j in jobs)
