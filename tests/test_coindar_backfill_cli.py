"""Backfill CLI status/run/reset, JSON report and strict argument validation."""
import json
from pathlib import Path

from crypto_event_harvester import coindar_backfill as archive
from crypto_event_harvester.cli import main
from crypto_event_harvester.probe import PageSnapshot

FIXTURE = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"


def test_cli_run_status_and_reset_preserve_event_records(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "events.sqlite"
    output = tmp_path / "reports" / "archive.json"
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200, FIXTURE.read_text())

    monkeypatch.setattr(archive, "fetch_page", fetch)
    args = ["backfill", "--db", str(db), "--from-date", "2026-11-01",
            "--to-date", "2026-11-02"]
    assert main(args + ["--pages", "1", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["inserted"] == 3
    assert report["total_pages"] == 1
    assert len(calls) == 1
    capsys.readouterr()

    assert main(args + ["--action", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "exhausted"
    assert status["unique_in_window"] == 3

    assert main(args + ["--action", "reset"]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["checkpoint_reset"] is True
    assert cleared["event_rows_preserved"] is True

    assert main(["store", "--db", str(db), "--action", "status"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["events"] == 3


def test_cli_invalid_range_no_network_no_db(tmp_path, capsys):
    db = tmp_path / "data.sqlite"
    assert main([
        "backfill", "--db", str(db),
        "--from-date", "2026-11-03", "--to-date", "2026-11-01"
    ]) == 2
    assert "from-date" in capsys.readouterr().err
    assert not db.exists()


def test_cli_status_does_not_create_missing_sqlite_file(tmp_path, capsys):
    db = tmp_path / "missing.sqlite"
    assert main([
        "backfill", "--db", str(db), "--from-date", "2026-11-01",
        "--to-date", "2026-11-02", "--action", "status"
    ]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not db.exists()



def test_cli_undated_action_requires_no_network(tmp_path, monkeypatch, capsys):
    db = tmp_path / "history.sqlite"
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200, FIXTURE.read_text())

    monkeypatch.setattr(archive, "fetch_page", fetch)
    args = ["backfill", "--db", str(db), "--from-date", "2026-11-01",
            "--to-date", "2026-11-02"]
    assert main(args + ["--pages", "1"]) == 0
    assert len(calls) == 1
    capsys.readouterr()
    monkeypatch.setattr(archive, "fetch_page", lambda *a, **kw: 1 / 0)
    assert main(args + ["--action", "undated", "--limit", "10"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["date_text"] == "Q1 2027"
    assert records[0]["event_date"] is None

    assert main(args + ["--action", "reset"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["undated_records_preserved"] is True
    assert main(args + ["--action", "undated"]) == 0
    assert json.loads(capsys.readouterr().out) == records
