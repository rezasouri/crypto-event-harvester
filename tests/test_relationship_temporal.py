"""Offline tests for relationship decision timelines and historical as-of views."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.relationship_temporal import (
    relationship_as_of,
    relationship_timeline,
)
from crypto_event_harvester.relationships import (
    relationship_id_for,
    retract_relationship,
    review_relationship,
)
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(database_path):
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
        title="Verified Emeralds at India Blockchain Week",
        project="Verified Emeralds",
        ticker="VEREM",
        description="Verified Emeralds will attend India Blockchain Week.",
        start_date="2026-11-01",
        end_date="2026-11-01",
        date_text="2026-11-01",
        date_precision="day",
        period_key=None,
    )
    conference = replace(
        crypto,
        title="India Blockchain Week 2026 Conference",
        description="India Blockchain Week is a conference in Mumbai.",
        location="Mumbai",
        start_date="2026-11-01",
        end_date="2026-11-02",
        date_text="Nov 1-2, 2026",
        date_precision="range",
        period_key=None,
    )
    sync_events(
        database_path,
        [subject, conference],
        observed_at_utc="2026-11-01T10:00:00+00:00",
    )
    return subject, conference


def _decide(database_path, subject, conference, kind, at_utc, relation_type="attends"):
    with patch("crypto_event_harvester.relationships._now", return_value=at_utc):
        return review_relationship(
            database_path,
            subject_event_id=subject.event_id,
            object_event_id=conference.event_id,
            relation_type=relation_type,
            decision=kind,
            evidence={"basis": "fixture source review"},
            note="Offline test decision.",
        )


def _retract(database_path, relationship_id, at_utc):
    with patch("crypto_event_harvester.relationships._now", return_value=at_utc):
        return retract_relationship(
            database_path,
            relationship_id=relationship_id,
            note="Offline test retraction.",
        )


def test_as_of_replays_accept_stale_reaccept_and_retraction(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    first = _decide(
        db, subject, conference, "accepted", "2026-11-02T10:00:00+00:00"
    )
    relation_id = first["relationship_id"]

    before = relationship_as_of(db, at_utc="2026-11-02T09:59:59Z")
    assert before["active_edges_total"] == 0
    assert before["decision_actions_considered"] == 0

    accepted = relationship_as_of(
        db, at_utc="2026-11-02T12:00:00+02:00"
    )
    assert accepted["at_utc"] == "2026-11-02T10:00:00.000000Z"
    assert accepted["active_edges_total"] == 1
    edge = accepted["active_edges"][0]
    assert edge["relationship_id"] == relation_id
    assert edge["relation_type"] == "attends"
    assert edge["subject_revision"] == 1
    assert edge["subject"]["title"] == subject.title
    assert accepted["stale_accepted_edges_excluded"] == 0

    updated = replace(
        subject,
        title="Verified Emeralds at India Blockchain Week — updated",
        description="Updated source event description.",
    )
    sync_events(
        db,
        [updated],
        observed_at_utc="2026-11-02T12:00:00+00:00",
    )
    assert relationship_as_of(
        db, at_utc="2026-11-02T11:59:59Z"
    )["active_edges"][0]["subject"]["title"] == subject.title

    stale = relationship_as_of(db, at_utc="2026-11-02T12:00:00Z")
    assert stale["active_edges_total"] == 0
    assert stale["stale_accepted_edges_excluded"] == 1
    assert stale["latest_decision_counts"] == {"accepted": 1}

    second = _decide(
        db, subject, conference, "accepted", "2026-11-02T14:00:00Z"
    )
    assert second["subject_revision"] == 2

    revised = relationship_as_of(db, at_utc="2026-11-02T14:00:00Z")
    assert revised["active_edges_total"] == 1
    assert revised["active_edges"][0]["subject_revision"] == 2
    assert revised["active_edges"][0]["subject"]["title"] == updated.title

    _retract(db, relation_id, "2026-11-02T16:00:00Z")
    finished = relationship_as_of(db, at_utc="2026-11-02T16:00:00Z")
    assert finished["active_edges_total"] == 0
    assert finished["latest_decision_counts"] == {"retracted": 1}
    assert finished["stale_accepted_edges_excluded"] == 0


def test_as_of_excludes_rejected_relations_and_supports_event_filter(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _decide(db, subject, conference, "accepted", "2026-11-02T10:00:00Z")
    _decide(
        db, subject, conference, "rejected",
        "2026-11-02T11:00:00Z", relation_type="sponsors",
    )

    report = relationship_as_of(
        db, at_utc="2026-11-02T12:00:00Z",
        event_id=conference.event_id,
    )
    assert report["latest_relationship_decisions_total"] == 2
    assert report["latest_decision_counts"] == {"accepted": 1, "rejected": 1}
    assert report["active_edges_total"] == 1

    unrelated = relationship_as_of(
        db, at_utc="2026-11-02T12:00:00Z",
        event_id="coindar:non-existent",
    )
    assert unrelated["latest_relationship_decisions_total"] == 0
    assert unrelated["active_edges_total"] == 0


def test_timeline_includes_retraction_and_revision_history_chronologically(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    first = _decide(
        db, subject, conference, "accepted", "2026-11-02T10:00:00Z"
    )
    updated = replace(subject, description="New observed description.")
    sync_events(
        db,
        [updated],
        observed_at_utc="2026-11-02T11:00:00Z",
    )
    second = _decide(
        db, subject, conference, "accepted", "2026-11-02T12:00:00Z"
    )
    withdrawn = _retract(
        db, first["relationship_id"], "2026-11-02T13:00:00Z"
    )
    timeline = relationship_timeline(
        db, relationship_id=first["relationship_id"]
    )

    assert timeline["actions_total"] == 3
    assert [a["decision"] for a in timeline["actions"]] == [
        "accepted", "accepted", "retracted",
    ]
    assert [a["subject_revision"] for a in timeline["actions"]] == [1, 2, 2]
    assert timeline["actions"][-1]["supersedes_decision_id"] == second["decision_id"]
    assert timeline["actions"][-1]["decision_id"] == withdrawn["decision_id"]
    assert timeline["actions"] == sorted(
        timeline["actions"], key=lambda action: action["decided_at_utc"]
    )
    assert "note" not in timeline["actions"][0]
    assert "evidence" not in timeline["actions"][0]

    last_two = relationship_timeline(
        db, relationship_id=first["relationship_id"], limit=2
    )
    assert last_two["status"] == "truncated"
    assert last_two["actions_total"] == 3
    assert last_two["actions_returned"] == 2
    assert last_two["actions"][0]["decision_id"] == second["decision_id"]

    by_event = relationship_timeline(db, event_id=subject.event_id)
    assert by_event["actions_total"] == 3


def test_as_of_reports_missing_observation_when_decision_precedes_source(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _decide(
        db, subject, conference, "accepted", "2026-10-30T10:00:00Z"
    )
    report = relationship_as_of(db, at_utc="2026-10-31T10:00:00Z")

    assert report["latest_decision_counts"] == {"accepted": 1}
    assert report["active_edges_total"] == 0
    assert report["accepted_edges_without_observed_source_snapshots"] == 1


def test_as_of_limit_truncates_rows_but_keeps_total(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _decide(db, subject, conference, "accepted", "2026-11-02T10:00:00Z")
    _decide(
        db, subject, conference, "accepted",
        "2026-11-02T11:00:00Z", relation_type="participates_in",
    )
    report = relationship_as_of(
        db, at_utc="2026-11-02T12:00:00Z", limit=1
    )

    assert report["active_edges_total"] == 2
    assert report["active_edges_returned"] == 1
    assert report["active_edges_truncated"] is True
    assert report["active_edges"][0]["relation_type"] == "attends"


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "2026-11-02",
        "2026-11-02T10:00:00",
        "yesterday",
        "2026-11-02T25:00:00Z",
    ],
)
def test_as_of_rejects_naive_or_invalid_timestamps(tmp_path, timestamp):
    db = tmp_path / "events.sqlite"
    _seed(db)
    with pytest.raises(ValueError, match="at_utc"):
        relationship_as_of(db, at_utc=timestamp)


@pytest.mark.parametrize("limit", [0, 1001])
def test_temporal_commands_validate_limits(tmp_path, limit):
    db = tmp_path / "events.sqlite"
    _seed(db)
    with pytest.raises(ValueError, match="limit"):
        relationship_as_of(db, at_utc="2026-11-02T10:00:00Z", limit=limit)
    with pytest.raises(ValueError, match="limit"):
        relationship_timeline(db, limit=limit)


def test_timeline_disallows_both_filters(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    relation_id = relationship_id_for(
        subject.event_id, conference.event_id, "attends"
    )
    with pytest.raises(ValueError, match="filter by"):
        relationship_timeline(
            db, relationship_id=relation_id, event_id=subject.event_id
        )


def test_temporal_commands_are_read_only_and_do_not_migrate(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _decide(db, subject, conference, "accepted", "2026-11-02T10:00:00Z")
    conn = sqlite3.connect(db)
    before = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in (
            "events", "event_revisions", "event_relationships",
            "event_relationship_decisions",
        )
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    relationship_as_of(db, at_utc="2026-11-02T10:00:00Z")
    relationship_timeline(db)

    conn = sqlite3.connect(db)
    assert {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in before
    } == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="never migrates"):
        relationship_timeline(legacy)


def test_temporal_cli_actions_and_error_exit(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _decide(db, subject, conference, "accepted", "2026-11-02T10:00:00Z")
    assert main(
        [
            "relationship", "--db", str(db), "--action", "as-of",
            "--at-utc", "2026-11-02T10:00:00Z", "--event-id", subject.event_id,
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["active_edges_total"] == 1
    assert result["mode"] == "read_only"

    assert main(
        [
            "relationship", "--db", str(db), "--action", "timeline",
            "--event-id", conference.event_id,
        ]
    ) == 0
    timeline = json.loads(capsys.readouterr().out)
    assert timeline["actions_total"] == 1

    assert main(
        ["relationship", "--db", str(db), "--action", "as-of"]
    ) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "error"
    assert "--at-utc" in failure["error"]
