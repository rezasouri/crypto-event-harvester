"""Coindar CLI integration tests using local HTML only."""
import csv
import json
from pathlib import Path

from crypto_event_harvester.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"


def test_coindar_native_json_export(tmp_path):
    output = tmp_path / "coindar.json"
    assert main([
        "scrape", "--provider", "coindar", "--input-html", str(FIXTURE),
        "--min-events", "4", "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provider"] == "coindar"
    assert payload["sections"] == ["upcoming"]
    assert payload["extracted_count"] == 4
    assert payload["events"][0]["event_id"] == "coindar:155001"


def test_coindar_unified_json_export(tmp_path):
    output = tmp_path / "coindar-unified.json"
    assert main([
        "scrape", "--provider", "coindar", "--schema", "unified",
        "--input-html", str(FIXTURE), "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "unified"
    assert payload["events"][0]["event_type"] == "crypto_event"
    assert payload["events"][0]["provider"] == "coindar"
    assert payload["events"][0]["start_date"] == "2026-11-01"
    assert payload["events"][0]["start_at_utc"] is None
    assert payload["events"][0]["timezone"] is None


def test_coindar_csv_export(tmp_path):
    output = tmp_path / "coindar.csv"
    assert main([
        "scrape", "--provider", "coindar", "--input-html", str(FIXTURE),
        "--format", "csv", "--output", str(output),
    ]) == 0
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert rows[0]["coin"] == "Alien Worlds"


def test_coindar_can_sync_to_existing_sqlite_contract(tmp_path, capsys):
    db, output = tmp_path / "events.sqlite", tmp_path / "events.json"
    assert main([
        "scrape", "--provider", "coindar", "--schema", "unified",
        "--input-html", str(FIXTURE), "--db", str(db), "--output", str(output),
    ]) == 0
    result = json.loads(capsys.readouterr().err)
    assert result["sync"]["inserted"] == 4
    assert main(["store", "--db", str(db), "--action", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["providers"] == {"coindar": 4}


def test_coindar_rejects_unsupported_sections_and_pagination(capsys):
    assert main(["scrape", "--provider", "coindar", "--section", "ended"]) == 2
    assert "upcoming" in capsys.readouterr().err
    assert main(["scrape", "--provider", "coindar", "--pages", "2"]) == 2
    assert "only for cryptoevents" in capsys.readouterr().err


def test_coindar_minimum_event_threshold_is_enforced(tmp_path):
    output = tmp_path / "too-few.json"
    assert main([
        "scrape", "--provider", "coindar", "--input-html", str(FIXTURE),
        "--min-events", "5", "--output", str(output),
    ]) == 2
    assert not output.exists()
