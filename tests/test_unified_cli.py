import csv
import json
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("provider", "filename", "expected_type", "expected_start", "expected_end"),
    [
        ("icodrops", "icodrops_ended.html", "token_event", "2026-09-29", "2026-09-29"),
        ("cryptoevents", "cryptoevents_home.html", "conference", "2026-10-27", "2026-10-29"),
        ("coindar", "coindar_listing_sample.html", "crypto_event", "2026-11-01", "2026-11-01"),
    ],
)
def test_cli_unified_json_export(tmp_path, provider, filename, expected_type, expected_start,
                                 expected_end):
    dest = tmp_path / "unified.json"
    assert main([
        "scrape", "--provider", provider, "--schema", "unified",
        "--input-html", str(FIXTURES / filename), "--output", str(dest),
    ]) == 0
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["schema"] == "unified"
    assert payload["schema_version"] == "1.1"
    assert payload["extracted_count"] == len(payload["events"])
    first = payload["events"][0]
    assert first["event_type"] == expected_type
    assert first["start_date"] == expected_start
    assert first["end_date"] == expected_end
    assert first["start_at_utc"] is None
    assert first["end_at_utc"] is None
    assert first["timezone"] is None


def test_unified_csv_has_same_columns_for_both_sources(tmp_path):
    files = {}
    for provider, filename in [
        ("icodrops", "icodrops_ended.html"),
        ("cryptoevents", "cryptoevents_home.html"),
        ("coindar", "coindar_listing_sample.html"),
    ]:
        output = tmp_path / f"{provider}.csv"
        assert main([
            "scrape", "--provider", provider, "--schema", "unified", "--format", "csv",
            "--input-html", str(FIXTURES / filename), "--output", str(output),
        ]) == 0
        with output.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            assert rows
            files[provider] = (reader.fieldnames, rows)
    assert files["icodrops"][0] == files["cryptoevents"][0]
    assert files["icodrops"][1][0]["event_type"] == "token_event"
    assert files["cryptoevents"][1][0]["event_type"] == "conference"


def test_existing_native_schema_is_unchanged(tmp_path):
    output = tmp_path / "native.json"
    assert main([
        "scrape", "--provider", "icodrops",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
        "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text())
    assert "schema_version" not in payload
    assert "schema" not in payload
    assert "start_date" not in payload["events"][0]
    assert payload["events"][0]["date_text"] == "Upcoming"


def test_limit_does_not_skip_validation_or_modify_source_count(tmp_path):
    dest = tmp_path / "unified.json"
    assert main([
        "scrape", "--provider", "cryptoevents", "--schema", "unified",
        "--input-html", str(FIXTURES / "cryptoevents_home.html"),
        "--min-events", "4", "--limit", "1", "--output", str(dest),
    ]) == 0
    payload = json.loads(dest.read_text())
    assert payload["extracted_count"] == 4
    assert payload["exported_count"] == 1
    assert len(payload["events"]) == 1
