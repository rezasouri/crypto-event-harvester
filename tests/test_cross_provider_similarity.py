"""Scored cross-provider matching: conservative fuzzy candidates, never auto-merge."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.cross_provider_candidates import match_similar_candidates
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.storage import store_status, sync_events

FIXTURES = Path(__file__).parent / "fixtures"


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


def _dated(
    event, *, title="Ethereum Shanghai Upgrade", date="2026-11-10",
    end_date=None, project=None, ticker=None, **changes,
):
    values = {
        "title": title,
        "start_date": date,
        "end_date": end_date or date,
        "date_precision": "day" if (end_date or date) == date else "range",
        "date_text": date if (end_date or date) == date else f"{date} - {end_date}",
        "project": project,
        "ticker": ticker,
        "period_key": None,
    }
    values.update(changes)
    return replace(event, **values)


def test_similarity_finds_fuzzy_title_with_matching_project(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(
        ico, title="Ethereum Shanghai Upgrade", project="Ethereum", ticker="ETH"
    )
    right = _dated(
        coindar, title="Ethereum Shanghai Ecosystem Upgrade",
        project="Ethereum", ticker="ETH",
    )
    sync_events(db, [left, right])
    result = match_similar_candidates(db)
    report = result.to_dict()
    assert report["strategy"] == "similarity"
    assert report["source_requests"] == 0
    assert report["source_events_modified"] == 0
    assert result.candidates_total == 1
    candidate = result.candidates[0]
    assert candidate["match_type"] == "review_required"
    assert candidate["evidence"]["project_match"] is True
    assert candidate["evidence"]["ticker_match"] is True
    assert candidate["evidence"]["title_similarity"] < 1.0
    assert candidate["evidence"]["score"] >= 0.80
    assert candidate["left"]["provider"] != candidate["right"]["provider"]


def test_similarity_allows_reordered_words_without_entity_metadata(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(ico, title="Ethereum Shanghai Upgrade")
    right = _dated(coindar, title="Shanghai Upgrade Ethereum")
    sync_events(db, [left, right])
    result = match_similar_candidates(db)
    assert result.candidates_total == 1
    assert result.candidates[0]["evidence"]["threshold_path"] == "title_only_strict"


@pytest.mark.parametrize(("left_changes", "right_changes"), [
    ({"project": "Ethereum"}, {"project": "Solana"}),
    ({"ticker": "ETH"}, {"ticker": "SOL"}),
    ({"title": "Ethereum Shanghai Upgrade"}, {"title": "Solana Mainnet Migration"}),
    ({"date": "2026-11-10"}, {"date": "2026-11-12"}),
])
def test_conflicts_and_nonoverlapping_dates_are_rejected(
    tmp_path, left_changes, right_changes
):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left_args = {"title": "Ethereum Shanghai Upgrade", "date": "2026-11-10"}
    right_args = {"title": "Ethereum Shanghai Upgrade", "date": "2026-11-10"}
    left_args.update(left_changes)
    right_args.update(right_changes)
    left = _dated(ico, **left_args)
    right = _dated(coindar, **right_args)
    sync_events(db, [left, right])
    assert match_similar_candidates(db).candidates_total == 0


def test_event_type_conflict_is_a_hard_veto(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, _, conference = _samples()
    left = _dated(ico, title="Ethereum Global Conference", project="Ethereum")
    right = _dated(
        conference, title="Ethereum Global Conference",
        event_type="conference", project="Ethereum",
    )
    # ICO Drops is a token event; CryptoEvents is a conference.
    sync_events(db, [left, right])
    assert match_similar_candidates(db).candidates_total == 0


def test_generic_or_single_concept_titles_without_entity_are_rejected(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(ico, title="Ethereum Upgrade")
    right = _dated(coindar, title="Ethereum Mainnet Upgrade")
    sync_events(db, [left, right])
    assert match_similar_candidates(db).candidates_total == 0


def test_coarse_dates_and_missing_dates_are_not_eligible(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(ico)
    right = replace(
        _dated(coindar), start_date=None, end_date=None,
        date_precision="month", date_text="November 2026", period_key="2026-11",
    )
    sync_events(db, [left, right])
    result = match_similar_candidates(db)
    assert result.eligible == 1
    assert result.candidates_total == 0


def test_partial_date_overlap_is_exposed_as_review_evidence(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(ico, title="Ethereum Shanghai Upgrade", end_date="2026-11-12")
    right = _dated(coindar, title="Ethereum Shanghai Upgrade", date="2026-11-11")
    sync_events(db, [left, right])
    result = match_similar_candidates(db)
    assert result.candidates_total == 1
    assert result.candidates[0]["evidence"]["date_relation"] == "left_contains_right"
    assert result.candidates[0]["confidence_band"] in {"medium", "high"}


def test_min_score_filters_results_and_is_validated(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    left = _dated(ico, title="Ethereum Shanghai Upgrade", project="Ethereum")
    right = _dated(
        coindar, title="Ethereum Shanghai Ecosystem Upgrade", project="Ethereum"
    )
    sync_events(db, [left, right])
    assert match_similar_candidates(db, min_score=0.99).candidates_total == 0
    for score in (-0.1, 1.1):
        with pytest.raises(ValueError, match="min-score"):
            match_similar_candidates(db, min_score=score)


def test_similarity_does_not_modify_event_rows_or_revisions(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    sync_events(db, [
        _dated(ico, title="Ethereum Shanghai Upgrade", project="Ethereum"),
        _dated(
            coindar, title="Ethereum Shanghai Ecosystem Upgrade", project="Ethereum"
        ),
    ])
    before = store_status(db)
    match_similar_candidates(db)
    assert store_status(db) == before


def test_cli_similarity_json_export(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    ico, coindar, _ = _samples()
    sync_events(db, [
        _dated(ico, title="Ethereum Shanghai Upgrade", project="Ethereum"),
        _dated(
            coindar, title="Ethereum Shanghai Ecosystem Upgrade", project="Ethereum"
        ),
    ])
    output = tmp_path / "similarity.json"
    assert main([
        "candidates", "--db", str(db), "--strategy", "similarity",
        "--min-score", "0.80", "--output", str(output),
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["strategy"] == "similarity"
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["strategy"] == "similarity"
    assert report["candidates_total"] == 1


def test_same_provider_pairs_are_never_compared(tmp_path):
    db = tmp_path / "events.sqlite"
    coindar, _, _ = _samples()
    first = _dated(coindar, title="Ethereum Shanghai Upgrade")
    second = _dated(
        replace(coindar, event_id="coindar:another", source_event_id="another"),
        title="Ethereum Shanghai Upgrade",
    )
    sync_events(db, [first, second])
    result = match_similar_candidates(db)
    assert result.overlapping_pairs_examined == 0
    assert result.candidates_total == 0


def test_limit_returns_highest_scores_not_first_encountered_pairs(tmp_path):
    db = tmp_path / "events.sqlite"
    ico, coindar, conference = _samples()
    first = _dated(
        ico, title="Ethereum Shanghai Upgrade", project="Ethereum",
        event_type="crypto_event",
    )
    second = _dated(
        coindar, title="Ethereum Shanghai Ecosystem Upgrade", project="Ethereum",
        event_type="crypto_event",
    )
    third = _dated(
        conference, title="Ethereum Shanghai Upgrade", project="Ethereum",
        event_type="crypto_event",
    )
    sync_events(db, [first, second, third])
    result = match_similar_candidates(db, limit=1)
    assert result.candidates_total == 3
    assert len(result.candidates) == 1
    assert result.candidates[0]["evidence"]["title_similarity"] == 1.0
    assert result.candidates[0]["evidence"]["score"] == 1.0
