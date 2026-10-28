"""SQLite incremental synchronization and history regression tests (offline)."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.normalization import normalize_events
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_conferences
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.storage import (
    event_history,
    list_events,
    store_status,
    sync_events,
)

FIXTURES = Path(__file__).parent / "fixtures"
T1 = "2026-10-28T10:00:00+00:00"
T2 = "2026-10-28T11:00:00Z"
T3 = "2026-10-28T12:00:00+00:00"


def fixtures():
    token = parse_icodrops((FIXTURES / "icodrops_upcoming.html").read_text())
    conference = parse_conferences((FIXTURES / "cryptoevents_home.html").read_text())
    return normalize_events(token + conference)


def test_insert_both_providers_and_version_history(tmp_path):
    db = tmp_path / "events.sqlite"
    records = fixtures()
    report = sync_events(db, records, observed_at_utc=T1)
    assert report.to_dict() == {
        "inserted": 6, "updated": 0, "unchanged": 0, "stale_skipped": 0, "total": 6
    }
    status = store_status(db)
    assert status["database_version"] == 1
    assert status["events"] == status["revisions"] == 6
    assert status["providers"] == {"cryptoevents": 4, "icodrops": 2}
    saved = list_events(db)
    assert len(saved) == 6
    assert saved[0]["revision"] == 1
    assert saved[0]["first_seen_at_utc"] == "2026-10-28T10:00:00.000000+00:00"
    assert saved[0]["event"]["event_id"] == saved[0]["event_id"]
    history = event_history(db, saved[0]["event_id"])
    assert [x["revision"] for x in history] == [1]
    assert history[0]["event"] == saved[0]["event"]
    assert len(history[0]["payload_sha256"]) == 64


def test_identical_replay_increments_no_revision_and_advances_last_seen(tmp_path):
    db = tmp_path / "events.sqlite"
    records = fixtures()
    sync_events(db, records, observed_at_utc=T1)
    result = sync_events(db, records, observed_at_utc=T2)
    assert result.inserted == result.updated == result.stale_skipped == 0
    assert result.unchanged == 6
    status = store_status(db)
    assert status["events"] == status["revisions"] == 6
    assert status["latest_seen_at_utc"] == "2026-10-28T11:00:00.000000+00:00"
    first = list_events(db)[0]
    assert first["first_seen_at_utc"] == "2026-10-28T10:00:00.000000+00:00"
    assert first["updated_at_utc"] == first["first_seen_at_utc"]


def test_changed_event_appends_immutable_revision_and_preserves_first_seen(tmp_path):
    db = tmp_path / "events.sqlite"
    original = fixtures()[0]
    sync_events(db, [original], observed_at_utc=T1)
    changed = replace(original, title="CLIX — Updated launch round", date_text="Nov 20, 2026",
                      start_date="2026-11-20", end_date="2026-11-20", date_precision="day")
    report = sync_events(db, [changed], observed_at_utc=T2)
    assert report.to_dict() == {
        "inserted": 0, "updated": 1, "unchanged": 0, "stale_skipped": 0, "total": 1
    }
    rows = list_events(db)
    assert rows[0]["revision"] == 2
    assert rows[0]["first_seen_at_utc"] == "2026-10-28T10:00:00.000000+00:00"
    assert rows[0]["updated_at_utc"] == "2026-10-28T11:00:00.000000+00:00"
    revisions = event_history(db, original.event_id)
    assert [x["revision"] for x in revisions] == [1, 2]
    assert revisions[0]["event"]["date_text"] == "Upcoming"
    assert revisions[1]["event"]["date_text"] == "Nov 20, 2026"
    assert revisions[0]["payload_sha256"] != revisions[1]["payload_sha256"]


def test_stale_observations_never_rollback_newer_state(tmp_path):
    db = tmp_path / "events.sqlite"
    original = fixtures()[0]
    newer = replace(original, title="Updated")
    sync_events(db, [original], observed_at_utc=T1)
    sync_events(db, [newer], observed_at_utc=T3)
    assert sync_events(db, [original], observed_at_utc=T2).stale_skipped == 1
    assert sync_events(db, [original], observed_at_utc=T3).stale_skipped == 1
    assert list_events(db)[0]["event"]["title"] == "Updated"
    assert len(event_history(db, original.event_id)) == 2
    assert store_status(db)["latest_seen_at_utc"] == "2026-10-28T12:00:00.000000+00:00"


def test_missing_records_are_not_removed_by_partial_snapshot(tmp_path):
    db = tmp_path / "events.sqlite"
    records = fixtures()
    sync_events(db, records, observed_at_utc=T1)
    sync_events(db, [records[0]], observed_at_utc=T2)
    sync_events(db, [], observed_at_utc=T3)
    assert store_status(db)["events"] == 6
    assert store_status(db)["revisions"] == 6


def test_atomic_rollback_if_one_insert_fails(tmp_path):
    db = tmp_path / "events.sqlite"
    records = fixtures()
    sync_events(db, [], observed_at_utc=T1)
    denied_id = records[1].event_id
    assert denied_id.startswith("icodrops:")
    with sqlite3.connect(db) as conn:
        # This is a test-local controlled ID; SQLite cannot parameterize trigger DDL.
        conn.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON events "
            f"WHEN NEW.event_id = '{denied_id}' "
            "BEGIN SELECT RAISE(ABORT, 'simulate failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="simulate failure"):
        sync_events(db, records, observed_at_utc=T2)
    # Earlier inserts in the same batch must not survive the failed transaction.
    assert store_status(db)["events"] == 0
    assert store_status(db)["revisions"] == 0


def test_filter_and_bounded_read(tmp_path):
    db = tmp_path / "events.sqlite"
    sync_events(db, fixtures(), observed_at_utc=T1)
    rows = list_events(db, provider="icodrops", limit=1)
    assert len(rows) == 1
    assert rows[0]["event"]["provider"] == "icodrops"
    assert event_history(db, "missing:event") == []
    with pytest.raises(ValueError, match="limit"):
        list_events(db, limit=0)
    with pytest.raises(ValueError, match="limit"):
        list_events(db, limit=1001)


@pytest.mark.parametrize("timestamp", [
    "2026-10-28T12:00:00", "garbage", "", "2026-10-28T99:00:00Z"
])
def test_reject_invalid_or_naive_observation_clock(tmp_path, timestamp):
    db = tmp_path / "events.sqlite"
    with pytest.raises(ValueError, match="observed_at_utc"):
        sync_events(db, fixtures(), observed_at_utc=timestamp)
    assert not db.exists()


def test_accept_timezone_offset_as_instant(tmp_path):
    db = tmp_path / "events.sqlite"
    sync_events(db, fixtures()[:1], observed_at_utc="2026-10-28T13:30:00+03:30")
    assert store_status(db)["latest_seen_at_utc"] == "2026-10-28T10:00:00.000000+00:00"


def test_reject_duplicate_snapshot_before_db_write(tmp_path):
    db = tmp_path / "events.sqlite"
    event = fixtures()[0]
    with pytest.raises(ValueError, match="duplicate"):
        sync_events(db, [event, event], observed_at_utc=T1)
    assert not db.exists()


def test_read_unknown_db_fails_without_creating_file(tmp_path):
    db = tmp_path / "not-here.sqlite"
    with pytest.raises(FileNotFoundError):
        store_status(db)
    assert not db.exists()


def test_reject_future_database_version(tmp_path):
    db = tmp_path / "future.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="unsupported"):
        sync_events(db, fixtures()[:1], observed_at_utc=T1)


def test_no_phantom_time_of_day_in_persisted_events(tmp_path):
    db = tmp_path / "events.sqlite"
    sync_events(db, fixtures(), observed_at_utc=T1)
    for row in list_events(db):
        event = row["event"]
        assert event["start_at_utc"] is None
        assert event["end_at_utc"] is None
        assert event["timezone"] is None
    assert json.loads(json.dumps(list_events(db))) == list_events(db)
