"""CLI bounded pages, local SQLite histories and refusal of unsupported options."""

import json
from pathlib import Path

from crypto_event_harvester import coindar_pagination as pagination
from crypto_event_harvester.cli import main
from crypto_event_harvester.providers.coindar import parse_listing

FIXTURE = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"


def test_multipage_coindar_native_and_sqlite(tmp_path, monkeypatch, capsys):
    original = parse_listing(FIXTURE.read_text())
    additional = [original[0].__class__(**{
        **original[0].to_dict(),
        "event_id": "coindar:888000",
        "source_url": "https://www.coindar.org/en/event/new-event-888000",
        "title": "New Calendar Item",
    })]
    called = []

    def fake_paginated(**kwargs):
        called.append(kwargs)
        return pagination.CoindarPageResult(original + additional, 2, 1, "page_limit")

    monkeypatch.setattr(pagination, "fetch_coindar_pages", fake_paginated)
    db = tmp_path / "history.sqlite"
    output = tmp_path / "events.json"
    assert main([
        "scrape", "--provider", "coindar", "--pages", "2",
        "--schema", "unified", "--section", "ended", "--start-date", "2026-11-01",
        "--db", str(db), "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text())
    assert report["extracted_count"] == 5
    assert report["events"][-1]["source_event_id"] == "888000"
    assert called[0]["max_pages"] == 2 and called[0]["start_date"] == "2026-11-01"
    capsys.readouterr()
    assert main(["store", "--db", str(db), "--action", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["events"] == status["revisions"] == 5


def test_reject_bad_coindar_options_without_network(capsys):
    assert main(["scrape", "--provider", "coindar", "--section", "all"]) == 2
    assert main(["scrape", "--provider", "coindar", "--section", "active"]) == 2
    assert main(["scrape", "--provider", "icodrops", "--pages", "2"]) == 2
    assert main(["scrape", "--provider", "icodrops", "--start-date", "2026-11-01"]) == 2
    assert main(["scrape", "--provider", "coindar", "--start-date", "2026-11-01"]) == 2
    assert main(["scrape", "--provider", "coindar", "--pages", "2",
                 "--input-html", str(FIXTURE)]) == 2
    assert main(["scrape", "--provider", "coindar", "--pages", "2",
                 "--engine", "selenium"]) == 2
    assert "requires" in capsys.readouterr().err or True


def test_single_page_saved_ended_is_still_allowed(tmp_path):
    output = tmp_path / "past.json"
    assert main([
        "scrape", "--provider", "coindar", "--section", "ended",
        "--input-html", str(FIXTURE), "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text())
    assert report["extracted_count"] == 4


def test_invalid_historical_date_never_writes_db(tmp_path):
    db = tmp_path / "history.sqlite"
    assert main([
        "scrape", "--provider", "coindar", "--section", "ended",
        "--start-date", "2026-99-99", "--db", str(db),
    ]) == 2
    assert not db.exists()
