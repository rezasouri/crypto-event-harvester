"""Offline acceptance tests for Coindar checkpointed historical discovery."""
import sqlite3
from pathlib import Path

import httpx
import pytest

from crypto_event_harvester import coindar_backfill as archive
from crypto_event_harvester.coindar_pagination import past_search_url
from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.storage import store_status

FIXTURE = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"
START, END = "2026-11-01", "2026-11-02"


def _url(page):
    return past_search_url(START).replace("page=1", f"page={page}")


def _html(page, *, next_page=None):
    html = FIXTURE.read_text(encoding="utf-8")
    for source_id in ("155001", "155002", "155003", "155004"):
        html = html.replace(source_id, str(int(source_id) + 1000 * (page - 1)))
    if next_page is not None:
        html = html.replace(
            "</main>", f'<a href="{_url(next_page)}">{next_page}</a></main>'
        )
    return html


def _fetch(monkeypatch, pages):
    calls = []

    def fake_fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        page = len(calls)
        if page > len(pages):
            pytest.fail("exceeded available fixture pages / page budget")
        expected_url = _url(page)
        assert url == expected_url
        assert engine == "http"
        return PageSnapshot(url, url, engine, 200, pages[page - 1])

    monkeypatch.setattr(archive, "fetch_page", fake_fetch)
    return calls


def test_resumes_without_refetching_previous_pages(tmp_path, monkeypatch):
    db = tmp_path / "harvest.sqlite"
    calls = _fetch(
        monkeypatch, [_html(1, next_page=2), _html(2, next_page=3), _html(3)]
    )

    one = archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert one.pages_this_run == one.total_pages == 1
    assert one.state == "active"
    assert one.next_page == 2
    assert one.inserted == 3
    checkpoint = archive.backfill_status(db, from_date=START, to_date=END)
    assert checkpoint["unique_ids_seen"] == 4
    assert checkpoint["unique_in_window"] == 3
    assert checkpoint["next_url"] == _url(2)

    two = archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert two.pages_this_run == 1 and two.total_pages == 2
    assert two.inserted == 3 and two.next_page == 3

    three = archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert three.state == "exhausted" and three.stop_reason == "no_next_link"
    assert three.total_pages == 3 and three.inserted == 3
    assert len(calls) == 3
    assert calls == [_url(1), _url(2), _url(3)]
    assert store_status(db)["providers"] == {"coindar": 9}
    final_status = archive.backfill_status(db, from_date=START, to_date=END)
    assert final_status["unique_ids_seen"] == 12
    assert final_status["unique_in_window"] == 9
    assert final_status["next_page"] is None


def test_terminal_checkpoint_makes_no_further_request(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = _fetch(monkeypatch, [_html(1)])
    first = archive.run_backfill(db, from_date=START, to_date=END, pages=2)
    assert first.state == "exhausted" and first.pages_this_run == 1
    again = archive.run_backfill(db, from_date=START, to_date=END, pages=5)
    assert again.pages_this_run == 0
    assert len(calls) == 1
    assert store_status(db)["revisions"] == 3


def test_network_error_preserves_previous_page_and_cursor(tmp_path, monkeypatch):
    db = tmp_path / "harvest.sqlite"
    calls = []

    def flaky(url, *, engine, timeout_seconds):
        calls.append(url)
        if len(calls) == 2:
            raise httpx.ConnectTimeout("synthetic timeout")
        return PageSnapshot(url, url, engine, 200, _html(1, next_page=2))

    monkeypatch.setattr(archive, "fetch_page", flaky)
    with pytest.raises(httpx.ConnectTimeout):
        archive.run_backfill(db, from_date=START, to_date=END, pages=3)
    assert calls == [_url(1), _url(2)]
    assert store_status(db)["events"] == 3
    status = archive.backfill_status(db, from_date=START, to_date=END)
    assert status["pages_completed"] == 1
    assert status["next_page"] == 2

    def resume(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200, _html(2))

    monkeypatch.setattr(archive, "fetch_page", resume)
    resumed = archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert resumed.pages_this_run == 1 and resumed.inserted == 3
    assert calls[-1] == _url(2)
    assert store_status(db)["events"] == 6


def test_same_identifiers_on_second_page_halt_without_duplicates(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(
            url, url, engine, 200,
            _html(1, next_page=2) if len(calls) == 1 else _html(1),
        )

    monkeypatch.setattr(archive, "fetch_page", fetch)
    result = archive.run_backfill(db, from_date=START, to_date=END, pages=5)
    assert result.state == "stopped" and result.stop_reason == "no_new_ids"
    assert result.pages_this_run == 2
    assert result.inserted == 3
    assert store_status(db)["events"] == store_status(db)["revisions"] == 3


def test_reset_clears_only_checkpoint_not_normalized_events(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = _fetch(monkeypatch, [_html(1)])
    archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert archive.reset_backfill(db, from_date=START, to_date=END)
    assert not archive.reset_backfill(db, from_date=START, to_date=END)
    assert archive.backfill_status(db, from_date=START, to_date=END)["state"] == "not_started"
    assert store_status(db)["events"] == 3
    assert store_status(db)["revisions"] == 3
    calls.clear()
    again = archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    assert again.inserted == 0 and again.unchanged == 3
    assert store_status(db)["revisions"] == 3


def test_windows_are_isolated_by_both_dates(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200, _html(1))

    monkeypatch.setattr(archive, "fetch_page", fetch)
    archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    archive.run_backfill(db, from_date=START, to_date=START, pages=1)
    assert len(calls) == 2
    assert archive.backfill_status(db, from_date=START, to_date=END)["unique_in_window"] == 3
    assert archive.backfill_status(db, from_date=START, to_date=START)["unique_in_window"] == 2
    assert store_status(db)["events"] == 3


def test_invalid_dates_and_page_budgets_do_not_create_db(tmp_path):
    db = tmp_path / "not-yet.sqlite"
    for from_date, to_date, pages in [
        ("not-a-date", END, 2), ("2026-11-03", END, 2),
        (START, "2026-12-99", 2), (START, END, 0), (START, END, 6),
    ]:
        with pytest.raises(ValueError):
            archive.run_backfill(db, from_date=from_date, to_date=to_date, pages=pages)
    assert not db.exists()


def test_status_existing_older_schema_is_read_only(tmp_path):
    db = tmp_path / "existing.sqlite"
    from crypto_event_harvester.storage import sync_events

    sync_events(db, [])
    status = archive.backfill_status(db, from_date=START, to_date=END)
    assert status["state"] == "not_started"


def test_tampered_cursor_is_rejected_before_http_request(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    _fetch(monkeypatch, [_html(1, next_page=2)])
    archive.run_backfill(db, from_date=START, to_date=END, pages=1)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE coindar_backfill_windows SET next_url = ?",
            ("https://outside.example/en/search?page=2",),
        )
    monkeypatch.setattr(archive, "fetch_page", lambda *a, **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="checkpoint"):
        archive.run_backfill(db, from_date=START, to_date=END, pages=1)


def test_page_403_does_not_advance_checkpoint_or_remove_events(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(
            url, url, engine, 200 if len(calls) == 1 else 403,
            _html(1, next_page=2) if len(calls) == 1 else "Access denied",
        )

    monkeypatch.setattr(archive, "fetch_page", fetch)
    with pytest.raises(RuntimeError, match="unavailable"):
        archive.run_backfill(db, from_date=START, to_date=END, pages=2)
    status = archive.backfill_status(db, from_date=START, to_date=END)
    assert status["next_page"] == 2
    assert status["pages_completed"] == 1
    assert store_status(db)["events"] == 3


def test_no_event_date_never_invents_window_membership(tmp_path, monkeypatch):
    db = tmp_path / "events.sqlite"
    calls = _fetch(monkeypatch, [_html(1)])
    result = archive.run_backfill(
        db, from_date="2026-10-01", to_date="2026-10-31", pages=1
    )
    assert result.inserted == 0
    assert store_status(db)["events"] == 0
    status = archive.backfill_status(
        db, from_date="2026-10-01", to_date="2026-10-31"
    )
    assert status["unique_in_window"] == 0
    assert status["unique_ids_seen"] == 4
    assert len(calls) == 1
