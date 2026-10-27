import csv
import json
from pathlib import Path

from crypto_event_harvester.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"


def test_cryptoevents_json_export(tmp_path, capsys):
    output = tmp_path / "events.json"
    assert main([
        "scrape", "--provider", "cryptoevents", "--section", "upcoming",
        "--input-html", str(FIXTURE), "--min-events", "4", "--output", str(output),
    ]) == 0
    parsed = json.loads(output.read_text())
    assert parsed["provider"] == "cryptoevents"
    assert parsed["extracted_count"] == 4
    assert parsed["events"][0]["start_date"] == "2026-10-27"
    assert json.loads(capsys.readouterr().out)["events"] == 4


def test_cryptoevents_csv_export(tmp_path, capsys):
    output = tmp_path / "events.csv"
    assert main([
        "scrape", "--provider", "cryptoevents", "--input-html", str(FIXTURE),
        "--format", "csv", "--output", str(output),
    ]) == 0
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert rows[0]["end_date"] == "2026-10-29"
    capsys.readouterr()


def test_cryptoevents_rejects_unsupported_section(capsys):
    assert main(["scrape", "--provider", "cryptoevents", "--section", "ended"]) == 2
    assert "upcoming" in capsys.readouterr().err


def test_cryptoevents_enforces_threshold(tmp_path):
    output = tmp_path / "events.json"
    assert main([
        "scrape", "--provider", "cryptoevents", "--input-html", str(FIXTURE),
        "--min-events", "50", "--output", str(output),
    ]) == 2
    assert not output.exists()
