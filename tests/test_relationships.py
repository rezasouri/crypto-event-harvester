"""Offline tests for the semantic event relationship graph and its audit trail."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.relationships import (
    list_relationships,
    relationship_history,
    relationship_id_for,
    relationship_status,
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
        description="Verified Emeralds will participate in India Blockchain Week in Mumbai.",
        start_date="2026-11-01",
        end_date="2026-11-01",
        date_text="2026-11-01",
        date_precision="day",
        period_key=None,
    )
    object_event = replace(
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
        [subject, object_event],
        observed_at_utc="2026-11-01T10:00:00+00:00",
    )
    return subject, object_event


def _accept(db, subject, object_event, **kwargs):
    return review_relationship(
        db,
        subject_event_id=subject.event_id,
        object_event_id=object_event.event_id,
        relation_type="participates_in",
        decision="accepted",
        **kwargs,
    )


def test_accept_creates_directed_edge_and_snapshots_review_evidence(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    before = sqlite3.connect(db)
    source_before = before.execute(
        "SELECT * FROM events ORDER BY event_id"
    ).fetchall()
    revisions_before = before.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall()
    before.close()

    accepted = _accept(
        db,
        subject,
        object_event,
        expected_subject_revision=1,
        expected_object_revision=1,
        evidence={"source": "Coindar URL slug", "reviewer": "manual"},
        note="The source record describes project participation in the conference.",
    )

    assert accepted["status"] == "accepted"
    assert accepted["idempotent"] is False
    assert accepted["database_rows_modified"] == 2
    assert accepted["source_events_modified"] == 0
    assert accepted["subject_revision"] == 1
    assert accepted["object_revision"] == 1

    relation_id = relationship_id_for(
        subject.event_id, object_event.event_id, "participates_in"
    )
    listed = list_relationships(db)
    assert len(listed) == 1
    edge = listed[0]
    assert edge["relationship_id"] == relation_id
    assert edge["subject_event_id"] == subject.event_id
    assert edge["object_event_id"] == object_event.event_id
    assert edge["relation_type"] == "participates_in"
    assert edge["stale"] is False
    assert edge["evidence"]["reviewer_evidence"]["source"] == "Coindar URL slug"
    assert edge["evidence"]["subject"]["revision"] == 1
    assert edge["evidence"]["object"]["revision"] == 1

    history = relationship_history(db, relationship_id=relation_id)
    assert len(history) == 1
    assert history[0]["decision"] == "accepted"
    assert history[0]["decision_id"] == accepted["decision_id"]
    assert relationship_status(db)["relationships_by_type"] == {"participates_in": 1}

    after = sqlite3.connect(db)
    assert after.execute("SELECT * FROM events ORDER BY event_id").fetchall() == source_before
    assert after.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall() == revisions_before
    assert after.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0] == 0
    after.close()


def test_rejected_relationship_is_audited_but_not_added_to_graph(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    args = {
        "subject_event_id": subject.event_id,
        "object_event_id": object_event.event_id,
        "relation_type": "participates_in",
        "decision": "rejected",
        "note": "The title overlap is coincidental.",
    }

    rejected = review_relationship(db, **args)
    repeated = review_relationship(db, **args)

    assert rejected["status"] == "rejected"
    assert repeated["idempotent"] is True
    assert repeated["decision_id"] == rejected["decision_id"]
    assert list_relationships(db) == []
    assert relationship_status(db)["decisions_by_status"] == {"rejected": 1}
    with pytest.raises(ValueError, match="conflict"):
        review_relationship(db, **{**args, "decision": "accepted"})


def test_expected_revision_guard_and_stale_edge_refresh(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    accepted = _accept(db, subject, object_event)
    changed = replace(
        subject,
        description="Updated description with revised participation details.",
    )
    sync_events(db, [changed], observed_at_utc="2026-11-02T10:00:00+00:00")

    stale = list_relationships(db)[0]
    assert stale["stale"] is True
    assert stale["subject_revision"] == 1
    assert stale["subject_current_revision"] == 2

    with pytest.raises(ValueError, match="stale relationship review"):
        _accept(
            db,
            subject,
            object_event,
            expected_subject_revision=1,
            expected_object_revision=1,
        )

    refreshed = _accept(
        db,
        subject,
        object_event,
        expected_subject_revision=2,
        expected_object_revision=1,
    )
    assert refreshed["status"] == "accepted"
    assert refreshed["idempotent"] is False
    edge = list_relationships(db)[0]
    assert edge["stale"] is False
    assert edge["subject_revision"] == 2
    assert edge["accepted_decision_id"] == refreshed["decision_id"]
    history = relationship_history(db, relationship_id=accepted["relationship_id"])
    assert [item["decision"] for item in history] == ["accepted", "accepted"]


def test_retract_is_audited_and_same_revision_can_be_accepted_again(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    accepted = _accept(db, subject, object_event)
    retracted = retract_relationship(
        db,
        relationship_id=accepted["relationship_id"],
        note="Manual correction after review.",
    )

    assert retracted["status"] == "retracted"
    assert list_relationships(db) == []
    assert relationship_status(db)["decisions_by_status"] == {
        "accepted": 1,
        "retracted": 1,
    }
    history = relationship_history(db, relationship_id=accepted["relationship_id"])
    assert [item["decision"] for item in history] == ["retracted", "accepted"]
    assert history[0]["supersedes_decision_id"] == accepted["decision_id"]

    accepted_again = _accept(db, subject, object_event)
    assert accepted_again["status"] == "accepted"
    assert accepted_again["idempotent"] is False
    assert len(list_relationships(db)) == 1


def test_retraction_of_unknown_or_already_retracted_edge_fails(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    with pytest.raises(ValueError, match="unknown active relationship"):
        retract_relationship(db, relationship_id="relationship:missing")

    accepted = _accept(db, subject, object_event)
    retract_relationship(db, relationship_id=accepted["relationship_id"])
    with pytest.raises(ValueError, match="unknown active relationship"):
        retract_relationship(db, relationship_id=accepted["relationship_id"])


@pytest.mark.parametrize(
    ("relation_type", "message"),
    [
        ("same_event", "canonical reconciliation"),
        ("invalid", "relation_type must be one of"),
    ],
)
def test_identity_and_unknown_predicates_are_rejected(tmp_path, relation_type, message):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    with pytest.raises(ValueError, match=message):
        review_relationship(
            db,
            subject_event_id=subject.event_id,
            object_event_id=object_event.event_id,
            relation_type=relation_type,
            decision="accepted",
        )


def test_reject_self_relationship_and_non_object_evidence(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    with pytest.raises(ValueError, match="distinct"):
        review_relationship(
            db,
            subject_event_id=subject.event_id,
            object_event_id=subject.event_id,
            relation_type="participates_in",
            decision="accepted",
        )
    with pytest.raises(ValueError, match="JSON object"):
        review_relationship(
            db,
            subject_event_id=subject.event_id,
            object_event_id=object_event.event_id,
            relation_type="participates_in",
            decision="accepted",
            evidence=["not", "an", "object"],
        )


def test_sqlite_guards_require_review_actions_and_keep_history_append_only(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    accepted = _accept(db, subject, object_event)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE event_relationship_decisions SET note='rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event_relationship_decisions")
    with pytest.raises(sqlite3.IntegrityError, match="audited retraction"):
        conn.execute(
            "DELETE FROM event_relationships WHERE relationship_id=?",
            (accepted["relationship_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="latest accepted review decision"):
        conn.execute(
            "INSERT INTO event_relationships ("
            "relationship_id, subject_event_id, object_event_id, relation_type, "
            "subject_revision, object_revision, evidence_json, accepted_decision_id, "
            "created_at_utc, updated_at_utc"
            ") VALUES ('forged', ?, ?, 'sponsors', 1, 1, '{}', 'forged', 'now', 'now')",
            (subject.event_id, object_event.event_id),
        )
    conn.rollback()
    conn.close()


def test_v3_to_v4_migration_preserves_existing_canonical_state(tmp_path):
    from crypto_event_harvester.reconciliation import reconcile_pair

    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    object_event = replace(
        object_event,
        title=subject.title,
        event_type=subject.event_type,
        start_date=subject.start_date,
        end_date=subject.end_date,
        date_precision=subject.date_precision,
        date_text=subject.date_text,
        project=subject.project,
        ticker=subject.ticker,
    )
    sync_events(db, [object_event], observed_at_utc="2026-11-02T10:00:00+00:00")
    canonical = reconcile_pair(
        db,
        left_event_id=subject.event_id,
        right_event_id=object_event.event_id,
        strategy="exact",
        decision="accepted",
    )

    conn = sqlite3.connect(db)
    before_events = conn.execute("SELECT * FROM events ORDER BY event_id").fetchall()
    before_revisions = conn.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall()
    before_members = conn.execute(
        "SELECT * FROM canonical_event_members ORDER BY event_id"
    ).fetchall()
    before_decisions = conn.execute(
        "SELECT * FROM reconciliation_decisions ORDER BY decision_id"
    ).fetchall()
    conn.execute("DROP TABLE event_relationships")
    conn.execute("DROP TABLE event_relationship_decisions")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    status = relationship_status(db)
    assert status["database_version"] == 4
    assert status["relationships"] == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT * FROM events ORDER BY event_id").fetchall() == before_events
    assert conn.execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall() == before_revisions
    assert conn.execute(
        "SELECT * FROM canonical_event_members ORDER BY event_id"
    ).fetchall() == before_members
    assert conn.execute(
        "SELECT * FROM reconciliation_decisions ORDER BY decision_id"
    ).fetchall() == before_decisions
    assert canonical["canonical_event_id"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_relationship_cli_accept_list_history_retract_and_status(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)

    assert main([
        "relationship", "--db", str(db), "--action", "accept",
        "--subject-event-id", subject.event_id,
        "--object-event-id", object_event.event_id,
        "--relation-type", "participates_in",
        "--subject-revision", "1",
        "--object-revision", "1",
        "--evidence-json", '{"review":"human"}',
        "--note", "Verified conference participation",
    ]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"

    assert main([
        "relationship", "--db", str(db), "--action", "list",
        "--relation-type", "participates_in",
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    relationship_id = listed[0]["relationship_id"]

    assert main([
        "relationship", "--db", str(db), "--action", "history",
        "--relationship-id", relationship_id,
    ]) == 0
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 1

    assert main([
        "relationship", "--db", str(db), "--action", "retract",
        "--relationship-id", relationship_id,
        "--note", "Incorrect interpretation",
    ]) == 0
    retracted = json.loads(capsys.readouterr().out)
    assert retracted["status"] == "retracted"

    assert main([
        "relationship", "--db", str(db), "--action", "status",
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["relationships"] == 0
    assert status["decisions_by_status"] == {"accepted": 1, "retracted": 1}


def test_relationship_cli_rejects_bad_evidence_json(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    subject, object_event = _seed(db)
    result = main([
        "relationship", "--db", str(db), "--action", "accept",
        "--subject-event-id", subject.event_id,
        "--object-event-id", object_event.event_id,
        "--relation-type", "participates_in",
        "--evidence-json", "[]",
    ])
    assert result == 2
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"
