"""Audit CLI: saved HTML, output and offline comparison."""

import json
from pathlib import Path

from crypto_event_harvester.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_icodrops_strict_offline_audit(tmp_path, capsys):
    path = tmp_path / "reports" / "icodrops.json"
    status = main([
        "audit", "--source", "icodrops",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
        "--min-records", "2", "--strict", "--output", str(path),
    ])
    assert status == 0
    assert json.loads(capsys.readouterr().out)["status"] == "parsed_listing"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["parsed_record_count"] == 2


def test_coindar_saved_html_parses_event_cards(tmp_path, capsys):
    path = tmp_path / "coindar.json"
    assert main([
        "audit", "--source", "coindar",
        "--input-html", str(FIXTURES / "coindar_listing_sample.html"),
        "--strict", "--min-records", "2", "--output", str(path),
    ]) == 0
    capsys.readouterr()
    result = json.loads(path.read_text())
    assert result["status"] == "parsed_listing"
    assert result["parsed_record_count"] == 4


def test_strict_threshold_fails_without_erasing_report(tmp_path, capsys):
    path = tmp_path / "failed.json"
    status = main([
        "audit", "--source", "cryptoevents",
        "--input-html", str(FIXTURES / "cryptoevents_home.html"),
        "--min-records", "20", "--strict", "--output", str(path),
    ])
    assert status == 1
    assert path.exists()
    assert json.loads(path.read_text())["parsed_record_count"] == 4
    capsys.readouterr()


def test_compare_to_saved_previous_audit(tmp_path, capsys):
    baseline = tmp_path / "before.json"
    final = tmp_path / "after.json"
    args = [
        "audit", "--source", "icodrops",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
    ]
    assert main(args + ["--output", str(baseline)]) == 0
    capsys.readouterr()
    assert main(args + ["--compare-to", str(baseline), "--output", str(final)]) == 0
    capsys.readouterr()
    assert json.loads(final.read_text())["comparison"]["changed_content_count"] == 0


def test_wrong_provider_baseline_fails_without_writing_new_report(tmp_path, capsys):
    previous = tmp_path / "coindar.json"
    assert main([
        "audit", "--source", "coindar",
        "--input-html", str(FIXTURES / "coindar_listing_sample.html"),
        "--output", str(previous),
    ]) == 0
    capsys.readouterr()
    output = tmp_path / "bad.json"
    assert main([
        "audit", "--source", "icodrops",
        "--input-html", str(FIXTURES / "icodrops_upcoming.html"),
        "--compare-to", str(previous), "--output", str(output),
    ]) == 2
    assert not output.exists()


def test_missing_local_file_reports_fetch_error_strictly(tmp_path, capsys):
    result = main([
        "audit", "--source", "coindar",
        "--input-html", str(tmp_path / "missing.html"), "--strict",
    ])
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fetch_error"
    assert report["listing_fetch_attempts"] == 0


def test_negative_minimum_rejected_before_fetch(capsys):
    assert main(["audit", "--source", "icodrops", "--min-records", "-1"]) == 2
    assert "negative" in capsys.readouterr().err
