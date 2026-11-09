"""Offline tests for deterministic, read-only relationship candidate discovery."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.relationship_candidates import discover_relationship_candidates
from crypto_event_harvester.relationships import review_relationship
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(
    database_path,
    *,
    project="QuarkChain",
    subject_title="Devcon 8 in Mumbai",
    description="QuarkChain will participate in Devcon 8 in Mumbai on November 3rd.",
    source_url=(
        "https://www.coindar.org/en/event/"
        "quarkchain-to-participate-in-devcon-8-in-mumbai-on-november-3rd"
    ),
    object_title="Devcon 2026 Mumbai",
    object_location="Mumbai",
    subject_start="2026-11-03",
    subject_end="2026-11-03",
    object_start="2026-11-03",
    object_end="2026-11-06",
):
    coindar = normalize_event(
        parse_coindar(
            (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
        )[0]
    )
    crypto = normalize_event(
        parse_cryptoevents(
            (FIXTURES / "cryptoevents_home.html").read_text(encoding="utf-8")
        )[0]
    )
    subject = replace(
        coindar,
        event_id="coindar:gate22-subject",
        source_event_id="gate22-subject",
        title=subject_title,
        project=project,
        ticker="QKC",
        description=description,
        source_url=source_url,
        start_date=subject_start,
        end_date=subject_end,
        date_text=subject_start,
        date_precision="day",
        period_key=None,
    )
    object_event = replace(
        crypto,
        event_id="cryptoevents:gate22-conference",
        source_event_id="gate22-conference",
        title=object_title,
        event_type="conference",
        category="conference",
        description=f"{object_title} is a conference in {object_location}.",
        location=object_location,
        start_date=object_start,
        end_date=object_end,
        date_text=f"{object_start} to {object_end}",
        date_precision="range",
        period_key=None,
    )
    sync_events(
        database_path,
        [subject, object_event],
        observed_at_utc="2026-11-01T10:00:00+00:00",
    )
    return subject, object_event


def test_discovers_participation_candidate_with_explainable_evidence(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)

    before = sqlite3.connect(db)
    before_version = before.execute("PRAGMA user_version").fetchone()[0]
    before_events = before.execute("SELECT * FROM events ORDER BY event_id").fetchall()
    before_revisions = before.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall()
    before_decisions = before.execute(
        "SELECT COUNT(*) FROM event_relationship_decisions"
    ).fetchone()[0]
    before.close()

    report = discover_relationship_candidates(db)

    assert report["status"] == "diagnosed"
    assert report["mode"] == "review_only"
    assert report["overlapping_cross_provider_pairs_examined"] == 1
    assert report["candidates_total"] == 1
    candidate = report["candidates"][0]
    assert candidate["status"] == "review_required"
    assert candidate["relation_type"] == "participates_in"
    assert candidate["subject"]["event_id"] == subject.event_id
    assert candidate["object"]["event_id"] == object_event.event_id
    assert candidate["subject"]["revision"] == 1
    assert candidate["object"]["revision"] == 1
    assert candidate["score"] >= 0.60
    assert candidate["evidence"]["relation_cue"]["phrase"] == "participate"
    assert candidate["evidence"]["target_match"]["shared_tokens"] == ["devcon", "mumbai"]
    assert candidate["evidence"]["target_match"]["location_mentioned"] is True
    assert candidate["not_an_identity_decision"] is True
    assert report["source_requests"] == 0
    assert report["source_events_modified"] == 0
    assert report["database_rows_modified"] == 0
    assert report["automatic_relationship_creation"] is False

    after = sqlite3.connect(db)
    assert after.execute("PRAGMA user_version").fetchone()[0] == before_version
    assert after.execute("SELECT * FROM events ORDER BY event_id").fetchall() == before_events
    assert after.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall() == before_revisions
    assert after.execute(
        "SELECT COUNT(*) FROM event_relationship_decisions"
    ).fetchone()[0] == before_decisions
    after.close()


def test_attendance_language_maps_to_attends(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(
        db,
        project="Makina",
        subject_title="Devcon 8 in Mumbai",
        description=(
            "The Makina team will attend Devcon VIII, taking place from "
            "November 3 to 6 at the Jio World Centre in Mumbai."
        ),
        source_url=(
            "https://www.coindar.org/en/event/"
            "makina-to-participate-in-devcon-8-in-mumbai-on-november-3"
        ),
    )

    report = discover_relationship_candidates(db)
    assert report["candidates_total"] == 1
    assert report["candidates"][0]["relation_type"] == "attends"
    assert report["candidates"][0]["evidence"]["relation_cue"]["mode"] == "attendee"


def test_url_slug_can_supply_explicit_relationship_language(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(
        db,
        project="Bitget Token",
        subject_title="Hong Kong FinTech Week in Hong Kong",
        description="Bitget Token community update in Hong Kong.",
        source_url=(
            "https://www.coindar.org/en/event/"
            "bitget-token-to-participate-in-hong-kong-fintech-week-"
            "x-startmeuphk-festival-2026"
        ),
        object_title="Hong Kong FinTech Week x StartmeupHK Festival 2026",
        object_location="Hong Kong",
        subject_start="2026-11-03",
        subject_end="2026-11-03",
        object_start="2026-11-02",
        object_end="2026-11-06",
    )

    report = discover_relationship_candidates(db)
    assert report["candidates_total"] == 1
    cue = report["candidates"][0]["evidence"]["relation_cue"]
    assert report["candidates"][0]["relation_type"] == "participates_in"
    assert cue["field"] == "source_url"
    assert cue["mode"] == "participant"


def test_sponsor_mention_for_another_actor_is_not_attributed_to_project(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(
        db,
        project="HashKey Platform Token",
        subject_title="Hong Kong FinTech Week 2026 in Hong Kong",
        description=(
            "HashKey Platform Token will take part in Hong Kong FinTech Week "
            "x StartmeupHK 2026 as HashKey Group returns as a Gold Sponsor."
        ),
        source_url=(
            "https://www.coindar.org/en/event/"
            "hashkey-platform-token-to-participate-in-hong-kong-fintech-week-2026"
        ),
        object_title="Hong Kong FinTech Week x StartmeupHK Festival 2026",
        object_location="Hong Kong",
        subject_start="2026-11-02",
        subject_end="2026-11-02",
        object_start="2026-11-02",
        object_end="2026-11-06",
    )

    report = discover_relationship_candidates(db)
    assert report["candidates_total"] == 1
    assert report["candidates"][0]["relation_type"] == "participates_in"


def test_current_accepted_or_rejected_pair_is_not_resuggested(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    review_relationship(
        db,
        subject_event_id=subject.event_id,
        object_event_id=object_event.event_id,
        relation_type="participates_in",
        decision="accepted",
        expected_subject_revision=1,
        expected_object_revision=1,
        evidence={"review": "human"},
    )

    report = discover_relationship_candidates(db)
    assert report["candidates"] == []
    assert report["reviewed_candidates_excluded"] == 1


def test_nonoverlapping_dates_produce_no_candidate(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(
        db,
        subject_start="2026-11-03",
        subject_end="2026-11-03",
        object_start="2026-11-10",
        object_end="2026-11-11",
    )

    report = discover_relationship_candidates(db)
    assert report["overlapping_cross_provider_pairs_examined"] == 0
    assert report["candidates_total"] == 0


def test_read_only_discovery_does_not_migrate_legacy_database(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute("DROP TABLE event_relationships")
    conn.execute("DROP TABLE event_relationship_decisions")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    report = discover_relationship_candidates(db)
    assert report["candidates_total"] == 1

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='event_relationship_decisions'"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("limit,min_score", [(0, 0.6), (1001, 0.6), (10, float("nan")), (10, 1.1)])
def test_invalid_candidate_parameters_fail_closed(tmp_path, limit, min_score):
    db = tmp_path / "events.sqlite"
    _seed(db)
    with pytest.raises(ValueError):
        discover_relationship_candidates(db, limit=limit, min_score=min_score)


def test_relationship_candidates_cli_outputs_json(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    _seed(db)

    assert main(
        [
            "relationship",
            "--db",
            str(db),
            "--action",
            "candidates",
            "--min-score",
            "0.60",
            "--limit",
            "10",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "diagnosed"
    assert report["candidates_total"] == 1
    assert report["database_rows_modified"] == 0

    assert main(
        [
            "relationship",
            "--db",
            str(db),
            "--action",
            "candidates",
            "--min-score",
            "2.0",
        ]
    ) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"
