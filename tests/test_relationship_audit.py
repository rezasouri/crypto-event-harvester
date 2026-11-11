"""Offline tests for the read-only relationship graph integrity audit."""

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
from crypto_event_harvester.relationship_audit import audit_relationship_graph
from crypto_event_harvester.relationships import retract_relationship, review_relationship
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


def _clone(event, source_event_id, *, title, project=None):
    return replace(
        event,
        event_id=f"{event.provider}:{source_event_id}",
        source_event_id=source_event_id,
        source_url=f"https://example.test/{source_event_id}",
        listing_url=f"https://example.test/{source_event_id}",
        title=title,
        project=project,
        ticker=None,
        description=f"Description for {title}",
    )


def _review(db, subject, object_event, relation_type, decision="accepted"):
    conn = sqlite3.connect(db)
    revisions = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT event_id, revision FROM events WHERE event_id IN (?, ?)",
            (subject.event_id, object_event.event_id),
        )
    }
    conn.close()
    return review_relationship(
        db,
        subject_event_id=subject.event_id,
        object_event_id=object_event.event_id,
        relation_type=relation_type,
        decision=decision,
        expected_subject_revision=revisions[subject.event_id],
        expected_object_revision=revisions[object_event.event_id],
        evidence={"basis": "fixture source review", "review": "human"},
        note="Reviewed against the fixture source records.",
    )


def _retract(db, relationship_id):
    with patch(
        "crypto_event_harvester.relationships._now",
        return_value="2026-11-02T10:00:00+00:00",
    ):
        return retract_relationship(
            db, relationship_id=relationship_id, note="Fixture retraction."
        )


def _codes(report):
    return {item["code"] for item in report["findings"]}


def test_clean_graph_audit_reports_consistent_projection_and_schema_guards(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")

    report = audit_relationship_graph(db)

    assert report["status"] == "consistent"
    assert report["review_status"] == "clear"
    assert report["mode"] == "read_only"
    assert report["events_total"] == 2
    assert report["source_revisions_total"] == 2
    assert report["decisions_total"] == 1
    assert report["decisions_by_status"] == {"accepted": 1}
    assert report["accepted_edges_total"] == 1
    assert report["current_edges_total"] == 1
    assert report["stale_edges_total"] == 0
    assert report["required_guard_triggers"] == 9
    assert report["missing_guard_triggers"] == []
    assert report["sqlite_integrity_check"] == ["ok"]
    assert report["foreign_key_violation_count"] == 0
    assert report["findings_total"] == 0
    assert report["database_rows_modified"] == 0
    assert report["source_requests"] == 0
    assert report["canonical_identity_modified"] is False


def test_stale_edge_is_a_review_signal_not_a_structural_error(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")
    updated = replace(
        subject,
        description="The source description has changed.",
    )
    sync_events(
        db, [updated], observed_at_utc="2026-11-02T10:00:00+00:00"
    )

    report = audit_relationship_graph(db)

    assert report["status"] == "consistent"
    assert report["review_status"] == "review_required"
    assert report["stale_edges_total"] == 1
    assert report["current_edges_total"] == 0
    assert "stale_accepted_edge" in _codes(report)


def test_multiple_predicates_and_reciprocal_edges_are_advisory(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")
    _review(db, subject, conference, "sponsors")
    _review(db, conference, subject, "participates_in")
    _review(db, conference, subject, "related_to")
    _review(db, subject, conference, "related_to")

    report = audit_relationship_graph(db)

    assert report["status"] == "consistent"
    assert report["review_status"] == "review_required"
    assert report["multiple_predicate_pairs"] == 2
    assert report["reciprocal_same_predicate_pairs"] == 2
    assert report["findings_by_code"]["multiple_predicates_for_pair"] == 2
    assert report["findings_by_code"]["reciprocal_directed_predicate"] == 1
    assert report["findings_by_code"]["reciprocal_symmetric_related_to"] == 1


def test_accept_retract_reaccept_chain_is_valid(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    first = _review(db, subject, conference, "attends")
    _retract(db, first["relationship_id"])
    second = _review(db, subject, conference, "attends")

    report = audit_relationship_graph(db)

    assert report["status"] == "consistent"
    assert report["review_status"] == "clear"
    assert report["decisions_by_status"] == {
        "accepted": 2,
        "retracted": 1,
    }
    assert report["accepted_edges_total"] == 1
    assert report["current_edges_total"] == 1
    assert "invalid_retraction_chain" not in _codes(report)
    assert report["findings_total"] == 0
    assert second["status"] == "accepted"


def test_missing_current_accepted_projection_is_an_error(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")

    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER trg_relationship_edge_delete_requires_retraction")
    conn.execute("DELETE FROM event_relationships")
    conn.commit()
    conn.close()

    report = audit_relationship_graph(db)

    assert report["status"] == "inconsistent"
    assert report["findings_by_code"]["current_acceptance_missing_from_projection"] == 1


def test_edge_projection_must_match_accepted_decision_evidence(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")

    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER trg_relationship_decisions_no_update")
    conn.execute(
        "UPDATE event_relationship_decisions SET evidence_json='{}'"
    )
    conn.commit()
    conn.close()

    report = audit_relationship_graph(db)

    assert report["status"] == "inconsistent"
    assert "decision_evidence_snapshot_mismatch" in _codes(report)
    assert "edge_evidence_projection_mismatch" in _codes(report)


def test_payload_hash_mismatch_is_detected(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, _ = _seed(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE events SET payload_json=? WHERE event_id=?",
        ('{"event_id":"wrong","provider":"coindar"}', subject.event_id),
    )
    conn.commit()
    conn.close()

    report = audit_relationship_graph(db)

    assert report["status"] == "inconsistent"
    assert "current_event_hash_mismatch" in _codes(report)
    assert "current_event_identity_mismatch" in _codes(report)
    assert "current_revision_payload_mismatch" in _codes(report)


def test_missing_guard_trigger_is_reported(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER trg_relationship_decisions_no_update")
    conn.commit()
    conn.close()

    report = audit_relationship_graph(db)

    assert report["status"] == "inconsistent"
    assert report["missing_guard_triggers"] == [
        "trg_relationship_decisions_no_update"
    ]
    assert "required_guard_trigger_missing" in _codes(report)


def test_findings_are_bounded_and_prioritized(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")
    updated = replace(subject, description="New revision.")
    sync_events(db, [updated], observed_at_utc="2026-11-02T10:00:00Z")
    _review(db, updated, conference, "participates_in")
    _review(db, conference, subject, "participates_in")

    report = audit_relationship_graph(db, limit=1)

    assert report["findings_total"] >= 1
    assert report["findings_returned"] == 1
    assert report["findings_truncated"] is True
    assert report["findings"][0]["severity"] == "warning"


@pytest.mark.parametrize("limit", [0, 1001])
def test_audit_rejects_invalid_limits(tmp_path, limit):
    db = tmp_path / "events.sqlite"
    _seed(db)
    with pytest.raises(ValueError, match="limit"):
        audit_relationship_graph(db, limit=limit)


def test_audit_cli_and_read_only_guarantees(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed(db)
    _review(db, subject, conference, "participates_in")

    conn = sqlite3.connect(db)
    before = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in (
            "events", "event_revisions", "event_relationship_decisions",
            "event_relationships",
        )
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert main(
        ["relationship", "--db", str(db), "--action", "audit", "--limit", "10"]
    ) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["status"] == "consistent"
    assert cli_report["mode"] == "read_only"

    conn = sqlite3.connect(db)
    after = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in before
    }
    assert after == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_audit_rejects_legacy_database_without_migration(tmp_path):
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="never migrates"):
        audit_relationship_graph(db)

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() == [("events",)]
    conn.close()
