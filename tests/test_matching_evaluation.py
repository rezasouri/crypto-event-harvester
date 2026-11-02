"""Offline tests for retrospective matching-quality evaluation."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from crypto_event_harvester.cli import main
from crypto_event_harvester.matching_evaluation import evaluate_reconciliation
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.reconciliation import reconcile_pair
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(database_path):
    ico = normalize_event(
        parse_icodrops((FIXTURES / "icodrops_upcoming.html").read_text())[0]
    )
    coindar = normalize_event(
        parse_coindar((FIXTURES / "coindar_listing_sample.html").read_text())[0]
    )
    cryptoevents = normalize_event(
        parse_cryptoevents((FIXTURES / "cryptoevents_home.html").read_text())[0]
    )

    def dated(event, *, title="Ethereum Shanghai Upgrade", **changes):
        values = {
            "title": title,
            "start_date": "2026-11-10",
            "end_date": "2026-11-10",
            "date_text": "2026-11-10",
            "date_precision": "day",
            "project": "Ethereum",
            "ticker": "ETH",
            "period_key": None,
        }
        values.update(changes)
        return replace(event, **values)

    first = dated(ico)
    second = dated(coindar)
    third = dated(
        cryptoevents,
        title="Ethereum Shanghai Ecosystem Upgrade",
        event_type=first.event_type,
    )
    sync_events(
        database_path,
        [first, second, third],
        observed_at_utc="2026-10-28T10:00:00+00:00",
    )
    return first, second, third


def test_empty_review_history_reports_no_metrics_without_inventing_labels(tmp_path):
    database_path = tmp_path / "events.sqlite"
    _seed(database_path)

    result = evaluate_reconciliation(database_path)

    assert result["status"] == "no_reviewed_pairs"
    assert result["reviewed_pair_count"] == 0
    assert result["accepted_label_count"] == 0
    assert result["rejected_label_count"] == 0
    assert result["exact_strategy"]["precision"] is None
    assert result["similarity_threshold_sweep"][0]["precision"] is None
    assert result["source_requests"] == 0
    assert result["database_rows_modified"] == 0


def test_evaluation_is_read_only_for_legacy_v1_database(tmp_path):
    database_path = tmp_path / "events.sqlite"
    _seed(database_path)

    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA foreign_keys = OFF")
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

    result = evaluate_reconciliation(database_path)

    assert result["status"] == "no_reviewed_pairs"
    assert result["database_version"] == 1
    conn = sqlite3.connect(database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='reconciliation_decisions'"
    ).fetchone()[0] == 0
    conn.close()


def test_evaluation_uses_historical_revisions_and_reports_threshold_tradeoff(tmp_path):
    database_path = tmp_path / "events.sqlite"
    first, second, third = _seed(database_path)

    accepted = reconcile_pair(
        database_path,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    rejected = reconcile_pair(
        database_path,
        left_event_id=first.event_id,
        right_event_id=third.event_id,
        strategy="similarity",
        decision="rejected",
        min_score=0.80,
        note="Similar title, but a different event",
    )
    assert accepted["decision"] == "accepted"
    assert rejected["decision"] == "rejected"

    # A later source revision must not rewrite the historical pair used as a label.
    changed_first = replace(first, description="A later source description")
    sync_events(
        database_path,
        [changed_first],
        observed_at_utc="2026-10-29T10:00:00+00:00",
    )

    result = evaluate_reconciliation(
        database_path,
        thresholds=[0.80, 0.90],
        min_reviewed_pairs=2,
    )

    assert result["status"] == "evaluated"
    assert result["evaluation_scope"] == "historically_reviewed_event_revision_pairs_only"
    assert result["reviewed_pair_count"] == 2
    assert result["accepted_label_count"] == 1
    assert result["rejected_label_count"] == 1
    assert result["similarity_score_coverage"] == 1.0
    assert result["exact_strategy"]["true_positive"] == 1
    assert result["exact_strategy"]["true_negative"] == 1

    low, high = result["similarity_threshold_sweep"]
    assert low["min_score"] == 0.80
    assert low["true_positive"] == 1
    assert low["false_positive"] == 1
    assert low["precision"] == 0.5
    assert high["min_score"] == 0.90
    assert high["true_positive"] == 1
    assert high["true_negative"] == 1
    assert high["precision"] == 1.0

    assert result["source_requests"] == 0
    assert result["source_events_modified"] == 0
    assert result["database_rows_modified"] == 0


def test_evaluation_marks_small_samples_as_insufficient(tmp_path):
    database_path = tmp_path / "events.sqlite"
    first, second, _ = _seed(database_path)
    reconcile_pair(
        database_path,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )

    result = evaluate_reconciliation(database_path, min_reviewed_pairs=30)

    assert result["status"] == "insufficient_reviewed_sample"
    assert result["reviewed_pair_count"] == 1
    assert result["exact_strategy"]["true_positive"] == 1


def test_evaluation_rejects_invalid_thresholds(tmp_path):
    database_path = tmp_path / "events.sqlite"
    _seed(database_path)

    for thresholds in ([0.8, 1.1], [0.8, float("nan")]):
        try:
            evaluate_reconciliation(database_path, thresholds=thresholds)
        except ValueError as exc:
            assert "thresholds must be finite" in str(exc)
        else:
            raise AssertionError("invalid threshold should fail")


def test_reconcile_evaluate_cli_emits_json(tmp_path, capsys):
    database_path = tmp_path / "events.sqlite"
    _seed(database_path)

    exit_code = main(
        [
            "reconcile",
            "--db",
            str(database_path),
            "--action",
            "evaluate",
            "--thresholds",
            "0.8",
            "0.9",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "no_reviewed_pairs"
    assert output["reviewed_pair_count"] == 0
