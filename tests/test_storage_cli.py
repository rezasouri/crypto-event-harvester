"""End-to-end offline CLI regression tests for incremental SQLite sync."""

import json
from pathlib import Path

from crypto_event_harvester.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_two_provider_scrapes_share_db_without_duplicate_revisions(tmp_path, capsys):
    db = tmp_path / "harvest.sqlite"
    for provider, html in [
        ("icodrops", "icodrops_upcoming.html"),
        ("cryptoevents", "cryptoevents_home.html"),
        ("icodrops", "icodrops_upcoming.html"),
    ]:
        assert main([
            "scrape", "--provider", provider, "--input-html", str(FIXTURES / html),
            "--schema", "unified", "--db", str(db), "--limit", "1",
        ]) == 0
        stdout = json.loads(capsys.readouterr().out)
        assert stdout["exported_count"] == 1
    assert main(["store", "--db", str(db), "--action", "status"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["events"] == stats["revisions"] == 6
    assert stats["providers"] == {"cryptoevents": 4, "icodrops": 2}


def test_native_export_can_sync_unified_db(tmp_path, capsys):
    db = tmp_path / "harvest.sqlite"
    assert main([
        "scrape", "--provider", "icodrops", "--input-html",
        str(FIXTURES / "icodrops_ended.html"), "--db", str(db),
    ]) == 0
    native = json.loads(capsys.readouterr().out)
    assert "schema_version" not in native
    assert native["events"][0]["event_date"] == "2026-09-29"
    assert main(["store", "--db", str(db), "--action", "list", "--provider", "icodrops"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["event"]["start_date"] == "2026-09-29"
    assert rows[0]["event"]["schema_version"] == "1.1"


def test_cli_history_and_missing_db_errors(tmp_path, capsys):
    db = tmp_path / "harvest.sqlite"
    assert main(["store", "--db", str(db)]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert main(["store", "--db", str(db), "--action", "history"]) == 2
    assert "--event-id" in capsys.readouterr().err
    assert main([
        "scrape", "--input-html", str(FIXTURES / "icodrops_ended.html"),
        "--db", str(db),
    ]) == 0
    capsys.readouterr()
    assert main([
        "store", "--db", str(db), "--action", "history",
        "--event-id", "icodrops:jumper:icm-on-legion",
    ]) == 0
    revisions = json.loads(capsys.readouterr().out)
    assert len(revisions) == 1
    assert revisions[0]["revision"] == 1
    assert revisions[0]["event"]["title"] == "Jumper — ICM on Legion"
