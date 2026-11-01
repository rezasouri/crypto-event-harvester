"""CLI tests for resumable CryptoEvents pagination."""
import json

from crypto_event_harvester.cli import main


def test_cli_run_status_and_reset(tmp_path, monkeypatch, capsys):
    from crypto_event_harvester import cryptoevents_backfill as archive
    from crypto_event_harvester.providers.cryptoevents import parse_listing
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"
    monkeypatch.setattr(archive, "fetch_listing", lambda **kw: parse_listing(fixture.read_text()))
    db = tmp_path / "events.sqlite"
    args = [
        "cryptoevents-backfill", "--db", str(db), "--start-date", "2026-11-01",
    ]
    assert main(args + ["--pages", "1"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["homepage_gets"] if "homepage_gets" in run else run["requests"]["homepage_gets"] == 1
    assert run["state"] == "active"
    assert run["next_offset"] == 20

    assert main(args + ["--action", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["next_offset"] == 20
    assert status["pages_completed"] == 1

    assert main(args + ["--action", "reset"]) == 0
    reset = json.loads(capsys.readouterr().out)
    assert reset == {"checkpoint_reset": True, "event_rows_preserved": True}


def test_cli_rejects_invalid_page_budget_without_network(tmp_path, capsys):
    assert main([
        "cryptoevents-backfill", "--db", str(tmp_path / "events.sqlite"),
        "--pages", "6",
    ]) == 2
    assert "--pages" in capsys.readouterr().err
