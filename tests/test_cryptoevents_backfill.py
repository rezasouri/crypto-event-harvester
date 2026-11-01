"""Checkpointed CryptoEvents pagination: offline resume and request-budget tests."""
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crypto_event_harvester import cryptoevents_backfill as archive
from crypto_event_harvester import pagination
from crypto_event_harvester.providers.cryptoevents import parse_listing
from crypto_event_harvester.storage import list_events, store_status

FIXTURE = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"
START = "2026-11-01"
START_EPOCH = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())


def _ajax_rows(offset: int, count: int = 20) -> list[dict]:
    return [
        {
            "link": f"https://cryptoevents.global/gate12-event-{offset + index}/",
            "title": f"Gate 12 Event {offset + index}",
            "excerpt": f"Description {offset + index}",
            "city": "Berlin",
            "start_date": "1794560400",
            "end_date": "1794592800",
            "timezone": "(UTC+00:00) GMT|0",
        }
        for index in range(count)
    ]


def _base_events():
    return parse_listing(FIXTURE.read_text(encoding="utf-8"))


def test_first_run_saves_homepage_and_first_ajax_page(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    home_calls = []
    ajax_calls = []

    def home(**kwargs):
        home_calls.append(kwargs)
        return _base_events()

    def ajax(offset, **kwargs):
        ajax_calls.append((offset, kwargs["start_day_utc"]))
        return pagination.parse_ajax_records(_ajax_rows(offset)), 20

    monkeypatch.setattr(archive, "fetch_listing", home)
    monkeypatch.setattr(archive, "fetch_ajax_page", ajax)
    result = archive.run_crawl(db, start_date=START, pages=2)

    assert result.state == "active"
    assert result.pages_this_run == result.total_pages == 2
    assert result.homepage_gets == result.ajax_posts == 1
    assert result.new_ids_this_run == result.unique_ids_seen == 24
    assert result.next_offset == 40
    assert home_calls == [{"engine": "http", "timeout_seconds": 20}]
    assert ajax_calls == [(20, START_EPOCH)]
    assert store_status(db)["events"] == 24
    assert len(list_events(db, provider="cryptoevents", limit=100)) == 24


def test_resume_starts_at_saved_offset_without_homepage_get(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    monkeypatch.setattr(
        archive, "fetch_ajax_page",
        lambda offset, **kw: (pagination.parse_ajax_records(_ajax_rows(offset)), 20),
    )
    first = archive.run_crawl(db, start_date=START, pages=2)
    assert first.next_offset == 40

    offsets = []

    def resume(offset, *, start_day_utc, timeout_seconds):
        offsets.append((offset, start_day_utc))
        if offset == 40:
            return pagination.parse_ajax_records(_ajax_rows(offset)), 20
        return pagination.parse_ajax_records(_ajax_rows(offset, 5)), 5

    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: pytest.fail("homepage refetched"))
    monkeypatch.setattr(archive, "fetch_ajax_page", resume)
    second = archive.run_crawl(db, start_date=START, pages=2)

    assert offsets == [(40, START_EPOCH), (60, START_EPOCH)]
    assert second.homepage_gets == 0
    assert second.ajax_posts == second.pages_this_run == 2
    assert second.state == "complete"
    assert second.stop_reason == "partial_page"
    assert second.total_pages == 4
    assert second.unique_ids_seen == 49
    assert store_status(db)["events"] == 49


def test_checkpoint_freezes_utc_start_day_across_resume(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    monkeypatch.setattr(
        archive, "fetch_ajax_page",
        lambda offset, **kw: (pagination.parse_ajax_records(_ajax_rows(offset)), 20),
    )
    archive.run_crawl(db, start_date=START, pages=1)

    seen_epochs = []

    def later(offset, *, start_day_utc, timeout_seconds):
        seen_epochs.append(start_day_utc)
        return pagination.parse_ajax_records(_ajax_rows(offset, 5)), 5

    monkeypatch.setattr(archive, "fetch_ajax_page", later)
    archive.run_crawl(db, start_date=START, pages=1)
    assert seen_epochs == [START_EPOCH]


def test_failed_page_does_not_advance_cursor(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    monkeypatch.setattr(
        archive, "fetch_ajax_page",
        lambda offset, **kw: (_ for _ in ()).throw(RuntimeError("HTTP 429")),
    )
    with pytest.raises(RuntimeError, match="429"):
        archive.run_crawl(db, start_date=START, pages=2)
    status = archive.crawl_status(db, start_date=START)
    assert status["state"] == "active"
    assert status["pages_completed"] == 1
    assert status["next_offset"] == 20
    assert status["unique_ids_seen"] == len(_base_events())
    assert store_status(db)["events"] == len(_base_events())


def test_replay_after_sync_before_checkpoint_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    page = pagination.parse_ajax_records(_ajax_rows(20))
    monkeypatch.setattr(archive, "fetch_ajax_page", lambda *a, **kw: (page, 20))
    real_sync = archive.sync_events
    failed = False

    def sync_then_interrupt(path, events):
        nonlocal failed
        result = real_sync(path, events)
        if not failed:
            failed = True
            raise RuntimeError("simulated process interruption")
        return result

    monkeypatch.setattr(archive, "sync_events", sync_then_interrupt)
    with pytest.raises(RuntimeError, match="interruption"):
        archive.run_crawl(db, start_date=START, pages=2)
    status = archive.crawl_status(db, start_date=START)
    assert status["next_offset"] == 0
    assert status["pages_completed"] == 0
    assert store_status(db)["events"] == len(_base_events())

    monkeypatch.setattr(archive, "sync_events", real_sync)
    replay = archive.run_crawl(db, start_date=START, pages=2)
    assert replay.homepage_gets == 1
    assert replay.ajax_posts == 1
    assert replay.inserted == 20
    assert replay.total_pages == 2
    assert store_status(db)["events"] == len(_base_events()) + 20
    assert store_status(db)["revisions"] == len(_base_events()) + 20


def test_no_new_ids_stops_and_keeps_records_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    home = _base_events()
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: home)
    monkeypatch.setattr(
        archive, "fetch_ajax_page", lambda *a, **kw: (home, 20),
    )
    result = archive.run_crawl(db, start_date=START, pages=2)
    assert result.state == "complete"
    assert result.stop_reason == "no_new_ids"
    assert result.total_pages == 2
    assert result.unique_ids_seen == len(home)
    assert store_status(db)["events"] == len(home)


def test_completed_crawl_does_not_make_more_requests(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    monkeypatch.setattr(
        archive, "fetch_ajax_page", lambda *a, **kw: ([], 0),
    )
    archive.run_crawl(db, start_date=START, pages=2)
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: pytest.fail("unexpected GET"))
    monkeypatch.setattr(archive, "fetch_ajax_page", lambda *a, **kw: pytest.fail("unexpected POST"))
    result = archive.run_crawl(db, start_date=START, pages=5)
    assert result.pages_this_run == 0
    assert result.homepage_gets == result.ajax_posts == 0
    assert result.state == "complete"


def test_reset_removes_only_cursor_and_seen_ids(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    monkeypatch.setattr(
        archive, "fetch_ajax_page",
        lambda offset, **kw: (pagination.parse_ajax_records(_ajax_rows(offset)), 20),
    )
    archive.run_crawl(db, start_date=START, pages=1)
    before = store_status(db)
    assert archive.reset_crawl(db, start_date=START) is True
    assert archive.crawl_status(db, start_date=START)["state"] == "not_started"
    assert store_status(db)["events"] == before["events"]
    assert store_status(db)["revisions"] == before["revisions"]


def test_status_for_another_day_is_independent(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    archive.run_crawl(db, start_date=START, pages=1)
    assert archive.crawl_status(db, start_date="2026-11-02")["state"] == "not_started"


@pytest.mark.parametrize("pages", [0, 6])
def test_invalid_page_budget_rejected_before_network(tmp_path, monkeypatch, pages):
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="--pages"):
        archive.run_crawl(tmp_path / "events.sqlite", start_date=START, pages=pages)


def test_invalid_start_date_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        archive.run_crawl(tmp_path / "events.sqlite", start_date="2026-11-1")


def test_hard_total_page_cap_stops_crawl(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: _base_events())
    calls = []

    def full_page(offset, **kwargs):
        calls.append(offset)
        return pagination.parse_ajax_records(_ajax_rows(offset)), 20

    monkeypatch.setattr(archive, "fetch_ajax_page", full_page)
    archive.run_crawl(db, start_date=START, pages=1)
    with archive._open_db(db, create=False) as conn:
        conn.execute(
            "UPDATE cryptoevents_crawl_runs SET pages_completed = ? "
            "WHERE start_date_utc = ?", (archive.MAX_TOTAL_PAGES - 1, START),
        )
    result = archive.run_crawl(db, start_date=START, pages=1)
    assert result.state == "complete"
    assert result.stop_reason == "total_page_limit"
    assert result.total_pages == archive.MAX_TOTAL_PAGES
    assert len(calls) == 1
