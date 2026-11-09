"""Regression tests for read-only matching coverage and relation diagnosis."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.cross_provider_candidates import match_similar_candidates
from crypto_event_harvester.matching_coverage import _relationship_hint, diagnose_matching_coverage
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(db):
    coindar = normalize_event(
        parse_coindar(
            (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
        )[0]
    )
    conference = normalize_event(
        parse_cryptoevents(
            (FIXTURES / "cryptoevents_home.html").read_text(encoding="utf-8")
        )[0]
    )
    participation = replace(
        coindar,
        title="India Blockchain Week 2026 in Mumbai",
        project="Verified Emeralds",
        ticker="VEREM",
        description=(
            "Verified Emeralds will participate in India Blockchain Week 2026 "
            "in Mumbai on November 1st-2nd."
        ),
        start_date="2026-11-01",
        end_date="2026-11-01",
        date_precision="day",
        date_text="2026-11-01",
        period_key=None,
    )
    main_event = replace(
        conference,
        title="India Blockchain Week 2026 Conference",
        location="Mumbai",
        start_date="2026-11-01",
        end_date="2026-11-02",
        date_precision="range",
        date_text="Nov 1-2, 2026",
        description="India Blockchain Week 2026 is a two-day conference in Mumbai.",
    )
    sync_events(db, [participation, main_event])
    return participation, main_event


def test_related_conference_participation_is_not_a_canonical_identity(tmp_path):
    db = tmp_path / "harvest.sqlite"
    participation, main_event = _seed(db)

    before = sqlite3.connect(db)
    before_version = before.execute("PRAGMA user_version").fetchone()[0]
    before_events = before.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_decisions = before.execute(
        "SELECT COUNT(*) FROM reconciliation_decisions"
    ).fetchone()[0]
    before.close()

    result = diagnose_matching_coverage(db)

    assert result["events_total"] == 2
    assert result["eligible_with_explicit_calendar_dates"] == 2
    assert result["overlapping_cross_provider_pairs_examined"] == 1
    assert result["strict_match_candidates_before_prior_decision_exclusions"] == 0
    assert result["possibly_related_pairs_total"] == 1
    hint = result["possibly_related_pairs"][0]
    assert hint["left"]["event_id"] in {participation.event_id, main_event.event_id}
    assert hint["right"]["event_id"] in {participation.event_id, main_event.event_id}
    assert hint["relation_hint"] == "possible_participation_mention"
    assert hint["participation_language_detected"] is True
    assert hint["location_mentioned"] is True
    assert hint["not_an_identity_decision"] is True
    assert hint["strict_matcher_reason"] == "title_below_title_only_threshold"
    assert result["automatic_merge_allowed"] is False
    assert result["database_rows_modified"] == 0
    assert result["source_requests"] == 0

    stats = result["provider_metadata"]
    assert stats["coindar"]["metadata_present_counts"]["project"] == 1
    assert stats["cryptoevents"]["metadata_present_counts"]["location"] == 1
    assert stats["cryptoevents"]["metadata_present_counts"]["ticker"] == 0

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before_version
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert conn.execute(
        "SELECT COUNT(*) FROM reconciliation_decisions"
    ).fetchone()[0] == before_decisions
    conn.close()
    assert match_similar_candidates(db).candidates_total == 0


def test_participation_url_slug_is_evidence_when_description_omits_verb():
    left = {
        "event_id": "coindar:155763",
        "provider": "coindar",
        "title": "Hong Kong FinTech Week in Hong Kong",
        "description": "Bitget Token community update in Hong Kong.",
        "source_url": (
            "https://www.coindar.org/en/event/"
            "bitget-token-to-participate-in-hong-kong-fintech-week-155763"
        ),
        "start_date": "2026-11-03",
        "end_date": "2026-11-03",
    }
    right = {
        "event_id": "cryptoevents:hong-kong-fintech-week",
        "provider": "cryptoevents",
        "title": "Hong Kong FinTech Week x StartmeupHK Festival 2026",
        "description": "A conference in Hong Kong.",
        "location": "Hong Kong",
        "source_url": "https://cryptoevents.global/hong-kong-fintech-week/",
        "start_date": "2026-11-02",
        "end_date": "2026-11-06",
    }
    hint = _relationship_hint(
        left,
        right,
        title_similarity=0.6029,
        character_similarity=0.6286,
        token_jaccard=0.5714,
    )
    assert hint["relation_hint"] == "possible_participation_mention"
    assert hint["participation_language_detected"] is True
    assert hint["participation_signal_sources"] == ["coindar_source_url"]
    assert hint["not_an_identity_decision"] is True


def test_no_date_overlap_means_no_related_hints(tmp_path):
    db = tmp_path / "harvest.sqlite"
    _, conference = _seed(db)
    conn = sqlite3.connect(db)
    conn.close()

    # Use a new database so the changed conference stays a single revision.
    db2 = tmp_path / "nonoverlapping.sqlite"
    participation, _ = _seed(db2)
    shifted_conference = replace(
        conference,
        start_date="2026-11-10",
        end_date="2026-11-11",
        date_text="Nov 10-11, 2026",
    )
    sync_events(db2, [shifted_conference])

    result = diagnose_matching_coverage(db2)
    assert result["overlapping_cross_provider_pairs_examined"] == 0
    assert result["possibly_related_pairs_total"] == 0


def test_read_only_diagnosis_does_not_migrate_schema_v1(tmp_path):
    db = tmp_path / "harvest.sqlite"
    _seed(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    for table in (
        "canonical_membership_actions",
        "reconciliation_decisions",
        "canonical_event_members",
        "canonical_events",
    ):
        conn.execute(f'DROP TABLE "{table}"')
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    result = diagnose_matching_coverage(db)

    assert result["status"] == "diagnosed"
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='reconciliation_decisions'"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    ("limit", "title_min", "strict_min"),
    [(0, 0.55, 0.8), (101, 0.55, 0.8), (20, float("nan"), 0.8), (20, 0.55, 2.0)],
)
def test_reject_invalid_report_parameters(tmp_path, limit, title_min, strict_min):
    db = tmp_path / "harvest.sqlite"
    _seed(db)
    with pytest.raises(ValueError):
        diagnose_matching_coverage(
            db, limit=limit, min_title_similarity=title_min, strict_min_score=strict_min
        )


def test_diagnose_coverage_cli_returns_json_and_rejects_writes(tmp_path, capsys):
    db = tmp_path / "harvest.sqlite"
    _seed(db)

    assert main(
        [
            "reconcile", "--db", str(db), "--action", "diagnose-coverage",
            "--min-title-similarity", "0.50", "--limit", "10",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "diagnosed"
    assert report["possibly_related_pairs_total"] == 1
    assert report["database_rows_modified"] == 0

    assert main(
        [
            "reconcile", "--db", str(db), "--action", "diagnose-coverage",
            "--apply",
        ]
    ) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"


def test_same_provider_events_cannot_be_related_to_each_other(tmp_path):
    db = tmp_path / "harvest.sqlite"
    event, _ = _seed(db)
    second = replace(
        event,
        event_id="coindar:other",
        source_event_id="other",
    )
    # Separate DB with only two Coindar observations.
    db2 = tmp_path / "same-provider.sqlite"
    sync_events(db2, [event, second])

    report = diagnose_matching_coverage(db2)
    assert report["overlapping_cross_provider_pairs_examined"] == 0
    assert report["possibly_related_pairs_total"] == 0
