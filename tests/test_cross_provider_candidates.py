"""Conservative cross-source duplicate candidates, no source event merges."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.cross_provider_candidates import match_candidates
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.storage import event_history, store_status, sync_events

FIXTURES = Path(__file__).parent / "fixtures"
T0 = "2026-11-01T08:00:00+00:00"
T1 = "2026-11-01T09:00:00+00:00"


def _samples():
    ico = normalize_event(parse_icodrops(
        (FIXTURES / "icodrops_upcoming.html").read_text(encoding="utf-8")
    )[0])
    coindar = normalize_event(parse_coindar(
        (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
    )[0])
    conference = normalize_event(parse_cryptoevents(
        (FIXTURES / "cryptoevents_home.html").read_text(encoding="utf-8")
    )[0])
    return ico, coindar, conference


def _dated(event, *, title="Major Protocol Upgrade", date="2026-11-10", **kw):
    return replace(
        event, title=title, start_date=date, end_date=date, date_precision="day",
        date_text=date, period_key=None, **kw,
    )


def test_cross_provider_exact_title_and_day_yields_review_candidate(tmp_path):
    db = tmp_path / "events.sqlite"
    a, b, _ = _samples()
    a = _dated(a, title="Major Protocol Upgrade", project="ExampleCoin", ticker="EXC")
    b = _dated(b, title="major   protocol-upgrade", project="ExampleCoin", ticker="EXC")
    sync_events(db, [a, b], observed_at_utc=T0)

    result = match_candidates(db)
    assert result.examined == result.eligible == 2
    assert result.candidates_total == 1
    report = result.to_dict()
    assert report["source_requests"] == 0
    assert report["source_events_modified"] == 0
    assert report["mode"] == "review_only"
    candidate = report["candidates"][0]
    assert candidate["match_type"] == "review_required"
    assert candidate["evidence"]["canonical_title"] == "major protocol upgrade"
    assert candidate["evidence"]["exact_start_date"] == "2026-11-10"
    assert {candidate["left"]["provider"], candidate["right"]["provider"]} == {
        "icodrops", "coindar",
    }
    assert store_status(db)["revisions"] == 2


@pytest.mark.parametrize(("a_opts", "b_opts"), [
    ({"date": "2026-11-10"}, {"date": "2026-11-11"}),
    ({"project": "Coin A"}, {"project": "Coin B"}),
    ({"ticker": "AAA"}, {"ticker": "BBB"}),
    ({"title": "Listing"}, {"title": "listing"}),
    ({"title": "ABC"}, {"title": "abc"}),
    ({"title": "Major Upgrade A"}, {"title": "Major Upgrade B"}),
])
def test_conflicting_or_generic_metadata_is_not_linked(tmp_path, a_opts, b_opts):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    a_defaults = {"title": "Major Protocol Upgrade", "date": "2026-11-10"}
    b_defaults = a_defaults.copy()
    a_defaults.update(a_opts)
    b_defaults.update(b_opts)
    sync_events(db, [_dated(ico, **a_defaults), _dated(coindar, **b_defaults)])
    assert match_candidates(db).candidates_total == 0


def test_missing_date_and_coarse_period_never_create_false_match(tmp_path):
    db = tmp_path / "events.sqlite"
    a, b, _ = _samples()
    month = replace(
        b, title="Major Protocol Upgrade", start_date=None, end_date=None,
        date_text="November 2026", date_precision="month", period_key="2026-11",
    )
    uncertain = replace(
        a, title="Major Protocol Upgrade", start_date=None, end_date=None,
        date_text="Upcoming", date_precision="unknown", period_key=None,
    )
    sync_events(db, [month, uncertain])
    assert match_candidates(db).eligible == 0
    assert match_candidates(db).candidates_total == 0


def test_day_vs_range_with_same_start_is_not_matched(tmp_path):
    db = tmp_path / "events.sqlite"
    a, b, _ = _samples()
    day = _dated(a)
    period = replace(
        _dated(b), date_precision="range", end_date="2026-11-12",
    )
    sync_events(db, [day, period])
    assert match_candidates(db).candidates_total == 0


def test_no_intra_provider_linking_even_when_title_and_date_equal(tmp_path):
    db = tmp_path / "events.sqlite"
    rows = parse_coindar((FIXTURES / "coindar_listing_sample.html").read_text())
    a = _dated(normalize_event(rows[0]))
    b = _dated(normalize_event(rows[1]))
    sync_events(db, [a, b])
    result = match_candidates(db)
    assert result.candidates_total == 0


def test_limit_only_truncates_output_not_total_count(tmp_path):
    db = tmp_path / "events.sqlite"
    a, b, c = _samples()
    sync_events(db, [_dated(x) for x in (a, b, c)])
    result = match_candidates(db, limit=1)
    assert result.candidates_total == 3
    assert len(result.candidates) == 1
    assert result.to_dict()["returned"] == 1


def test_revision_changes_candidate_evidence_without_writing(tmp_path):
    db = tmp_path / "events.sqlite"
    a, b, _ = _samples()
    first = _dated(a)
    second = _dated(b)
    sync_events(db, [first, second], observed_at_utc=T0)
    assert match_candidates(db).candidates_total == 1
    sync_events(db, [replace(first, title="A Different Event")], observed_at_utc=T1)
    before = store_status(db)
    assert match_candidates(db).candidates_total == 0
    assert store_status(db) == before
    assert [item["revision"] for item in event_history(db, first.event_id)] == [1, 2]


def test_missing_db_and_invalid_limit_do_not_create_files(tmp_path):
    db = tmp_path / "nonexistent.sqlite"
    with pytest.raises(FileNotFoundError):
        match_candidates(db)
    for limit in (0, -1, 1001):
        with pytest.raises(ValueError, match="limit"):
            match_candidates(db, limit=limit)
    assert not db.exists()


def test_cli_json_file_and_output_are_read_only(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    a, b, _ = _samples()
    sync_events(db, [_dated(a), _dated(b)])
    before = store_status(db)
    output = tmp_path / "reports" / "candidates.json"
    args = ["candidates", "--db", str(db)]
    assert main(args + ["--limit", "10", "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"output": str(output), "candidates_total": 1, "returned": 1}
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["source_requests"] == 0
    assert report["candidates_total"] == 1
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert store_status(db) == before


def test_cli_invalid_limit_rejected(tmp_path, capsys):
    assert main([
        "candidates", "--db", str(tmp_path / "events.sqlite"), "--limit", "0",
    ]) == 2
    assert "limit" in capsys.readouterr().err
