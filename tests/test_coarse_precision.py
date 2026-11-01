"""Coarse month/quarter precision is preserved without a fabricated calendar day."""
import csv
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.coindar_backfill import (
    backfill_undated,
    enrich_undated,
    run_backfill,
)
from crypto_event_harvester.date_precision import coarse_period
from crypto_event_harvester.normalization import (
    SCHEMA_VERSION,
    UnifiedEvent,
    normalize_event,
)
from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.providers.coindar import parse_listing
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.storage import list_events, store_status, sync_events

FIXTURES = Path(__file__).parent / "fixtures"
COINDAR = (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
START, END = "2026-11-01", "2026-11-02"


@pytest.mark.parametrize(("label", "precision", "period"), [
    ("October 2026", "month", "2026-10"),
    ("Oct 2026", "month", "2026-10"),
    ("October 2026 UTC", "month", "2026-10"),
    ("FEBRUARY 2030", "month", "2030-02"),
    ("December 2026", "month", "2026-12"),
    ("Q1 2027", "quarter", "2027-Q1"),
    ("from Q1, 2027", "quarter", "2027-Q1"),
    ("Q4 2030 UTC", "quarter", "2030-Q4"),
    ("Unknown", "unknown", None),
    ("Late October 2026", "unknown", None),
    ("October 13, 2026", "unknown", None),
    ("2026-10-01", "unknown", None),
    ("Q5 2026", "unknown", None),
    ("Q1 0000", "unknown", None),
    ("October 0000", "unknown", None),
])
def test_coarse_period_never_interprets_partial_or_invalid_labels(label, precision, period):
    assert coarse_period(label) == (precision, period)


def _coarse_event(label):
    html = COINDAR.replace("<h2>Q1 2027</h2>", f"<h2>{label}</h2>")
    return parse_listing(html)[-1]


@pytest.mark.parametrize(("label", "precision", "period"), [
    ("October 2026", "month", "2026-10"),
    ("Q1 2027", "quarter", "2027-Q1"),
])
def test_coindar_native_and_unified_preserve_period_without_day(label, precision, period):
    native = _coarse_event(label)
    assert native.date_text == label
    assert native.date_precision == precision
    assert native.period_key == period
    assert native.date_role == "listed"
    assert native.event_date is None
    normalized = normalize_event(native)
    assert normalized.schema_version == "1.1"
    assert normalized.date_text == label
    assert normalized.date_precision == precision
    assert normalized.period_key == period
    assert normalized.start_date is None
    assert normalized.end_date is None
    assert normalized.start_at_utc is None
    assert normalized.end_at_utc is None
    assert normalized.timezone is None


def test_icodrops_quarter_has_canonical_key_and_no_dates():
    html = (FIXTURES / "icodrops_upcoming.html").read_text(encoding="utf-8")
    original = parse_icodrops(html)[0]
    native = replace(
        original, date_text="from Q1, 2027",
        date_precision="quarter", date_role="start", event_date=None,
    )
    value = normalize_event(native)
    assert value.period_key == "2027-Q1"
    assert value.date_precision == "quarter"
    assert value.date_role == "start"
    assert value.start_date is None
    assert value.end_date is None


@pytest.mark.parametrize(("changes", "message"), [
    ({"period_key": None}, "period_key"),
    ({"period_key": "2026-13"}, "period_key"),
    ({"date_precision": "day"}, "period_key"),
    ({"start_date": "2026-10-01", "end_date": "2026-10-01"}, "explicit calendar"),
    ({"date_text": "Around October 2026"}, "period_key"),
    ({"schema_version": "1.0"}, "schema 1.0"),
])
def test_month_precision_contract_rejects_fake_or_mismatched_dates(changes, message):
    value = normalize_event(_coarse_event("October 2026"))
    with pytest.raises(ValueError, match=message):
        replace(value, **changes)


def test_legacy_schema_v1_records_remain_deserializable():
    date_event = normalize_event(parse_listing(COINDAR)[0])
    legacy = date_event.to_dict()
    legacy["schema_version"] = "1.0"
    legacy.pop("period_key")
    restored = UnifiedEvent(**legacy)
    assert restored.schema_version == "1.0"
    assert restored.period_key is None
    assert restored.start_date == "2026-11-01"


def test_v11_sync_keeps_history_of_v10_records(tmp_path):
    event = normalize_event(parse_listing(COINDAR)[0])
    legacy = replace(event, schema_version="1.0")
    db = tmp_path / "events.sqlite"
    assert sync_events(db, [legacy]).inserted == 1
    assert sync_events(db, [event]).updated == 1
    assert sync_events(db, [event]).unchanged == 1
    result = list_events(db, provider="coindar", limit=10)
    assert result[0]["event"]["schema_version"] == SCHEMA_VERSION
    assert result[0]["revision"] == 2


def test_cli_coarse_unified_json_and_csv(tmp_path):
    source = tmp_path / "month.html"
    source.write_text(COINDAR.replace("<h2>Q1 2027</h2>", "<h2>October 2026</h2>"))
    jpath = tmp_path / "coindar.json"
    cpath = tmp_path / "coindar.csv"
    cmd = ["scrape", "--provider", "coindar", "--schema", "unified",
           "--input-html", str(source)]
    assert main(cmd + ["--output", str(jpath)]) == 0
    assert main(cmd + ["--format", "csv", "--output", str(cpath)]) == 0
    data = json.loads(jpath.read_text())
    monthly = data["events"][-1]
    assert data["schema_version"] == SCHEMA_VERSION
    assert monthly["date_precision"] == "month"
    assert monthly["period_key"] == "2026-10"
    assert monthly["start_date"] is None
    with cpath.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["date_precision"] == "month"
    assert rows[-1]["period_key"] == "2026-10"
    assert rows[-1]["start_date"] == ""


def test_saved_legacy_undated_enrich_no_network_and_idempotent(tmp_path, monkeypatch, capsys):
    db = tmp_path / "history.sqlite"
    html = COINDAR.replace("<h2>Q1 2027</h2>", "<h2>October 2026</h2>")

    def fetched(url, *, engine, timeout_seconds):
        return PageSnapshot(url, url, engine, 200, html)

    monkeypatch.setattr(
        "crypto_event_harvester.coindar_backfill.fetch_page", fetched,
    )
    outcome = run_backfill(db, from_date=START, to_date=END, pages=1)
    assert outcome.undated == 1
    assert store_status(db)["events"] == 3

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM coindar_backfill_undated"
        ).fetchone()
        old = json.loads(row[0])
        old["date_precision"] = "unknown"
        old["date_role"] = "unknown"
        old.pop("period_key")
        conn.execute(
            "UPDATE coindar_backfill_undated SET payload_json = ?",
            (json.dumps(old),),
        )

    def no_http(*args, **kwargs):
        pytest.fail("coarse enrichment must not fetch live Coindar HTML")

    monkeypatch.setattr(
        "crypto_event_harvester.coindar_backfill.fetch_page", no_http,
    )
    args = ["backfill", "--db", str(db), "--from-date", START, "--to-date", END]
    assert main(args + ["--action", "enrich"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "scanned": 1, "enriched": 1, "month": 1, "quarter": 0, "unknown": 0
    }
    assert main(args + ["--action", "enrich"]) == 0
    assert json.loads(capsys.readouterr().out)["enriched"] == 0
    record = backfill_undated(db, from_date=START, to_date=END)[0]
    assert record["date_precision"] == "month"
    assert record["period_key"] == "2026-10"
    assert record["event_date"] is None
    assert store_status(db)["events"] == 3
    assert store_status(db)["revisions"] == 3


def test_unknown_date_cache_does_not_claim_interval_membership(tmp_path, monkeypatch):
    db = tmp_path / "history.sqlite"
    html = COINDAR.replace("<h2>Q1 2027</h2>", "<h2>October 2026</h2>")
    monkeypatch.setattr(
        "crypto_event_harvester.coindar_backfill.fetch_page",
        lambda url, *, engine, timeout_seconds: PageSnapshot(url, url, engine, 200, html),
    )
    outcome = run_backfill(db, from_date=START, to_date=END, pages=1)
    assert outcome.undated == 1
    assert outcome.inserted == 3
    assert store_status(db)["events"] == 3
    cached = backfill_undated(db, from_date=START, to_date=END)
    assert cached[0]["date_precision"] == "month"
    assert cached[0]["event_date"] is None
    assert cached[0]["period_key"] == "2026-10"
