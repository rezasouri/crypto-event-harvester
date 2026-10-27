import csv
import json
from pathlib import Path

from crypto_event_harvester.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_cli_parse_saved_html_as_json(tmp_path, capsys):
    dest = tmp_path / "events.json"
    assert main([
        "scrape", "--provider", "icodrops", "--section", "upcoming",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
        "--format", "json", "--output", str(dest), "--min-events", "2",
    ]) == 0
    payload = json.loads(dest.read_text())
    assert payload["extracted_count"] == 2
    assert payload["events"][0]["event_date"] is None
    assert json.loads(capsys.readouterr().out)["events"] == 2


def test_cli_csv_output(tmp_path):
    dest = tmp_path / "events.csv"
    assert main([
        "scrape", "--section", "ended", "--format", "csv",
        "--input-html", str(FIXTURES / "icodrops_ended.html"),
        "--output", str(dest),
    ]) == 0
    with dest.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["event_date"] == "2026-09-29"


def test_cli_rejects_invented_event_minimum(tmp_path):
    dest = tmp_path / "events.json"
    assert main([
        "scrape", "--section", "ended",
        "--input-html", str(FIXTURES / "icodrops_ended.html"),
        "--min-events", "20", "--output", str(dest),
    ]) == 2
    assert not dest.exists()


def test_cli_rejects_multisection_offline(tmp_path):
    assert main([
        "scrape", "--section", "all",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
    ]) == 2
