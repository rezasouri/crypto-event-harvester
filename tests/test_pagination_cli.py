"""Multi-page CLI integration tests using mocked requests only."""

import json
from pathlib import Path

from crypto_event_harvester import pagination
from crypto_event_harvester.cli import main
from crypto_event_harvester.providers.cryptoevents import parse_listing

FIXTURE = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"


def test_cli_pagination_unified_and_sqlite(tmp_path, monkeypatch, capsys):
    original = parse_listing(FIXTURE.read_text())
    extra = pagination.parse_ajax_records([
        {
            "link": "https://cryptoevents.global/new-calendar-entry/",
            "title": "Extra conference",
            "excerpt": "<p>Extra</p>",
            "city": "Berlin",
            "start_date": "1794560400",
            "end_date": "1794592800",
            "timezone": "(UTC-05:00) Eastern Time| -5",
        }
    ])
    captured = {}

    def fake_pages(**kwargs):
        captured.update(kwargs)
        return pagination.PageResult(original + extra, 2, 1, "partial_page")

    monkeypatch.setattr(pagination, "fetch_paginated_listing", fake_pages)
    dest, db = tmp_path / "events.json", tmp_path / "events.sqlite"
    assert main([
        "scrape", "--provider", "cryptoevents", "--schema", "unified",
        "--pages", "2", "--db", str(db), "--output", str(dest),
    ]) == 0
    capsys.readouterr()
    result = json.loads(dest.read_text(encoding="utf-8"))
    assert result["extracted_count"] == 5
    assert captured["max_pages"] == 2
    assert main(["store", "--db", str(db), "--action", "status"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["events"] == stats["revisions"] == 5


def test_cli_rejects_unsupported_pagination_without_io(capsys):
    assert main(["scrape", "--provider", "icodrops", "--pages", "2"]) == 2
    assert main(["scrape", "--provider", "cryptoevents", "--pages", "6"]) == 2
    assert main(["scrape", "--provider", "cryptoevents", "--pages", "0"]) == 2
    assert main(["scrape", "--provider", "cryptoevents", "--engine", "playwright",
                 "--pages", "2"]) == 2
    assert main(["scrape", "--provider", "cryptoevents", "--pages", "2",
                 "--input-html", str(FIXTURE)]) == 2
    assert "--pages" in capsys.readouterr().err
