"""Offline tests for retrospective relationship quality evaluation."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import (
    parse_listing as parse_cryptoevents,
)
from crypto_event_harvester.relationship_evaluation import (
    evaluate_relationship_quality,
)
from crypto_event_harvester.relationships import (
    relationship_id_for,
    retract_relationship,
    review_relationship,
)
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _base_events():
    coindar = normalize_event(
        parse_coindar(
            (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
        )[0]
    )
    cryptoevents = normalize_event(
        parse_cryptoevents(
            (FIXTURES / "cryptoevents_home.html").read_text(encoding="utf-8")
        )[0]
    )
    return coindar, cryptoevents


def _seed_pair(
    database_path,
    *,
    slug="alpha",
    project="Alpha Protocol",
    conference_title="Web3 Summit Singapore 2026 Conference",
    location="Singapore",
    description=None,
    observed_at="2026-11-01T10:00:00+00:00",
):
    coindar, cryptoevents = _base_events()
    subject = replace(
        coindar,
        event_id=f"coindar:quality-{slug}",
        source_event_id=f"quality-{slug}",
        source_url=f"https://www.coindar.org/en/event/quality-{slug}",
        listing_url=f"https://www.coindar.org/en/event/quality-{slug}",
        title=f"{project} at {conference_title}",
        project=project,
        ticker=None,
        description=description
        or (
            f"{project} will participate in {conference_title.removesuffix(' 2026 Conference')} "
            f"in {location} on November 1, 2026."
        ),
        start_date="2026-11-01",
        end_date="2026-11-01",
        date_text="2026-11-01",
        date_precision="day",
        period_key=None,
    )
    conference = replace(
        cryptoevents,
        event_id=f"cryptoevents:quality-{slug}",
        source_event_id=f"quality-{slug}",
        source_url=f"https://cryptoevents.global/quality-{slug}/",
        listing_url=f"https://cryptoevents.global/quality-{slug}/",
        title=conference_title,
        description=f"{conference_title} is a conference in {location}.",
        event_type="conference",
        category="conference",
        location=location,
        start_date="2026-11-01",
        end_date="2026-11-02",
        date_text="Nov 1-2, 2026",
        date_precision="range",
        period_key=None,
    )
    sync_events(
        database_path,
        [subject, conference],
        observed_at_utc=observed_at,
    )
    return subject, conference


def _review(database_path, subject, conference, decision, *, evidence=None, note=None):
    return review_relationship(
        database_path,
        subject_event_id=subject.event_id,
        object_event_id=conference.event_id,
        relation_type="participates_in",
        decision=decision,
        expected_subject_revision=1,
        expected_object_revision=1,
        evidence=evidence
        if evidence is not None
        else {"basis": "The source explicitly states participation.", "review": "human"},
        note=note if note is not None else "Checked the source record.",
    )


def test_evaluation_compares_candidate_scores_with_both_human_labels(tmp_path):
    db = tmp_path / "events.sqlite"
    accepted_subject, accepted_target = _seed_pair(db, slug="accepted")
    rejected_subject, rejected_target = _seed_pair(
        db,
        slug="rejected",
        project="Beta Network",
        conference_title="DeFi Builders Forum Berlin 2026",
        location="Berlin",
        observed_at="2026-11-01T10:01:00+00:00",
    )
    _review(db, accepted_subject, accepted_target, "accepted")
    _review(db, rejected_subject, rejected_target, "rejected")

    report = evaluate_relationship_quality(
        db,
        thresholds=[0.90, 0.99],
        min_reviewed_pairs=2,
    )

    assert report["status"] == "evaluated"
    assert report["evaluation_ready"] is True
    assert report["reviewed_relationship_revision_count"] == 2
    assert report["accepted_label_count"] == 1
    assert report["rejected_label_count"] == 1
    assert report["candidate_score_coverage"] == 1.0
    assert report["candidate_threshold_sweep"][0]["sample_size"] == 2
    assert report["candidate_threshold_sweep"][0]["true_positive"] == 1
    assert report["candidate_threshold_sweep"][0]["false_positive"] == 1
    assert report["candidate_threshold_sweep"][0]["precision"] == 0.5
    assert report["evidence_quality_counts"] == {"complete": 2}
    assert report["audit_issue_counts"] == {}
    assert report["source_requests"] == 0
    assert report["database_rows_modified"] == 0
    assert report["canonical_identity_modified"] is False


def test_single_class_is_not_reported_as_evaluation_ready(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    _review(db, subject, conference, "accepted")

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)

    assert report["status"] == "insufficient_class_diversity"
    assert report["evaluation_ready"] is False
    assert report["accepted_label_count"] == 1
    assert report["rejected_label_count"] == 0


def test_missing_reviewer_basis_is_flagged_without_claiming_low_probability(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    _review(db, subject, conference, "accepted", evidence={}, note="")

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)

    assert report["evidence_quality_counts"] == {"insufficient": 1}
    assert report["audit_issue_counts"]["missing_reviewer_basis"] == 1
    assert report["audit_issue_counts"]["missing_review_marker"] == 1
    assert report["audit_issue_counts"]["missing_reviewer_note"] == 1
    assert report["score_kind"] == "deterministic_evidence_ranking_not_probability"


def test_retraction_is_a_lifecycle_action_not_a_negative_label(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    accepted = _review(db, subject, conference, "accepted")
    relation_id = relationship_id_for(
        subject.event_id, conference.event_id, "participates_in"
    )
    retract_relationship(db, relationship_id=relation_id, note="Reviewer withdrew claim.")

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)
    finding = report["reviewed_relationships"][0]

    assert report["accepted_label_count"] == 1
    assert report["rejected_label_count"] == 0
    assert report["retracted_action_count"] == 1
    assert report["decision_records_by_status"] == {
        "accepted": 1,
        "retracted": 1,
    }
    assert report["status"] == "insufficient_class_diversity"
    assert finding is not None
    assert finding["decision_id"] == accepted["decision_id"]
    assert finding["latest_action"] == "retracted"
    assert finding["current_projection"] == "retracted"
    assert "retracted_edge_still_active" not in finding["issues"]


def test_stale_revision_is_reported_but_historical_snapshot_is_evaluated(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    _review(db, subject, conference, "accepted")
    updated_subject = replace(
        subject,
        description="Updated source description for Alpha Protocol.",
    )
    sync_events(
        db,
        [updated_subject],
        observed_at_utc="2026-11-01T10:10:00+00:00",
    )

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)
    finding = next(item for item in report["findings"] if item["subject_event_id"] == subject.event_id)

    assert finding["stale"] is True
    assert finding["current_projection"] == "stale"
    assert "stale_source_revision" in finding["issues"]
    assert finding["candidate_evidence"] is not None


def test_newer_accepted_revision_supersedes_old_review_without_false_audit_error(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    _review(db, subject, conference, "accepted")
    updated_subject = replace(
        subject,
        description=(
            "Alpha Protocol will participate in Web3 Summit Singapore "
            "in Singapore on November 1, 2026. Updated source."
        ),
    )
    sync_events(
        db,
        [updated_subject],
        observed_at_utc="2026-11-01T10:10:00+00:00",
    )
    review_relationship(
        db,
        subject_event_id=subject.event_id,
        object_event_id=conference.event_id,
        relation_type="participates_in",
        decision="accepted",
        expected_subject_revision=2,
        expected_object_revision=1,
        evidence={"basis": "Rechecked updated source.", "review": "human"},
        note="Reviewed revision two.",
    )

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)
    old = next(
        item
        for item in report["reviewed_relationships"]
        if item["subject_revision"] == 1
    )
    current = next(
        item
        for item in report["reviewed_relationships"]
        if item["subject_revision"] == 2
    )

    assert old["current_projection"] == "superseded_by_newer_revision"
    assert "accepted_edge_missing" not in old["issues"]
    assert current["current_projection"] == "active"


def test_no_candidate_signal_is_reported_as_unscored_not_as_rejection(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(
        db,
        description="Alpha Protocol has a community update on November 1, 2026.",
    )
    _review(db, subject, conference, "accepted")

    report = evaluate_relationship_quality(db, min_reviewed_pairs=1)
    finding = report["findings"][0]

    assert report["candidate_score_coverage"] == 0.0
    assert report["candidate_threshold_sweep"][0]["sample_size"] == 0
    assert finding["candidate_evidence"] is None
    assert "no_current_candidate_signal" in finding["issues"]
    assert finding["label"] == "accepted"


def test_no_decisions_returns_empty_status(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_pair(db)

    report = evaluate_relationship_quality(db)

    assert report["status"] == "no_reviewed_relationships"
    assert report["reviewed_relationship_revision_count"] == 0
    assert report["findings"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_reviewed_pairs": 0},
        {"limit": 0},
        {"thresholds": []},
        {"thresholds": [float("nan")]},
        {"thresholds": [1.1]},
    ],
)
def test_evaluation_rejects_invalid_parameters(tmp_path, kwargs):
    db = tmp_path / "events.sqlite"
    _seed_pair(db)

    with pytest.raises(ValueError):
        evaluate_relationship_quality(db, **kwargs)


def test_evaluation_is_read_only_and_cli_exposes_action(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    subject, conference = _seed_pair(db)
    _review(db, subject, conference, "accepted")
    before = sqlite3.connect(db)
    before_counts = {
        table: before.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in (
            "events",
            "event_revisions",
            "event_relationship_decisions",
            "event_relationships",
        )
    }
    before_version = before.execute("PRAGMA user_version").fetchone()[0]
    before.close()

    assert main(
        [
            "relationship",
            "--db",
            str(db),
            "--action",
            "evaluate",
            "--min-reviewed-pairs",
            "1",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "read_only"
    assert output["database_rows_modified"] == 0

    after = sqlite3.connect(db)
    after_counts = {
        table: after.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in before_counts
    }
    assert after_counts == before_counts
    assert after.execute("PRAGMA user_version").fetchone()[0] == before_version
    assert after.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert after.execute("PRAGMA foreign_key_check").fetchall() == []
    after.close()
