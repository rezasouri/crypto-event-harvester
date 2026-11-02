"""Offline tests for the human review dataset workflow."""

import csv
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
from crypto_event_harvester.review_dataset import (
    _candidate_id,
    export_review_dataset,
    import_review_dataset,
)
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
            "event_type": ico.event_type,
        }
        values.update(changes)
        return replace(event, **values)

    events = [
        dated(ico),
        dated(coindar),
        dated(cryptoevents, title="Ethereum Shanghai Ecosystem Upgrade"),
    ]
    sync_events(
        database_path,
        events,
        observed_at_utc="2026-10-28T10:00:00+00:00",
    )
    return events


def _read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_export_is_read_only_and_contains_stable_revision_bound_candidates(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review" / "batch.csv"
    _seed(database_path)

    result = export_review_dataset(
        database_path,
        output_path,
        strategy="similarity",
        min_score=0.70,
        limit=20,
    )

    rows = _read_csv(output_path)
    assert result["status"] == "exported"
    assert result["source_requests"] == 0
    assert result["source_events_modified"] == 0
    assert result["database_rows_modified"] == 0
    assert result["automatic_merge_allowed"] is False
    assert result["exported_count"] == len(rows)
    assert len(rows) >= 2
    assert len({row["candidate_id"] for row in rows}) == len(rows)
    assert all(row["review_decision"] == "" for row in rows)
    assert all(row["left_revision"] == "1" for row in rows)
    assert all(row["right_revision"] == "1" for row in rows)
    assert all(row["strategy"] == "similarity" for row in rows)

    conn = sqlite3.connect(database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='reconciliation_decisions'"
    ).fetchone()[0] == 0
    conn.close()


def test_dry_run_then_apply_records_human_labels_for_gate17_evaluation(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    _seed(database_path)
    export_review_dataset(
        database_path, output_path, strategy="similarity", min_score=0.70, limit=20
    )
    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "accept"
    rows[0]["review_note"] = "Same underlying event"
    rows[1]["review_decision"] = "reject"
    rows[1]["review_note"] = "Related topic, different event"
    _write_csv(output_path, rows[:2])

    preview = import_review_dataset(database_path, output_path)
    assert preview["status"] == "validated"
    assert preview["mode"] == "dry_run"
    assert preview["validated_count"] == 2
    assert preview["applied_count"] == 0

    conn = sqlite3.connect(database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()

    applied = import_review_dataset(database_path, output_path, apply=True)
    assert applied["status"] == "applied"
    assert applied["applied_count"] == 2
    assert applied["idempotent_count"] == 0
    assert applied["source_events_modified"] == 0

    evaluation = evaluate_reconciliation(
        database_path, thresholds=[0.70, 0.90], min_reviewed_pairs=2
    )
    assert evaluation["status"] == "evaluated"
    assert evaluation["reviewed_pair_count"] == 2
    assert evaluation["accepted_label_count"] == 1
    assert evaluation["rejected_label_count"] == 1


def test_import_skips_unsure_rows_without_migrating_or_writing(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    _seed(database_path)
    export_review_dataset(
        database_path, output_path, strategy="similarity", min_score=0.70, limit=20
    )
    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "unsure"
    _write_csv(output_path, rows[:1])

    result = import_review_dataset(database_path, output_path, apply=True)

    assert result["status"] == "applied"
    assert result["skipped_unreviewed_count"] == 1
    assert result["applied_count"] == 0
    conn = sqlite3.connect(database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_import_rejects_tampered_candidate_identity_before_any_write(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    _seed(database_path)
    export_review_dataset(
        database_path, output_path, strategy="similarity", min_score=0.70, limit=20
    )
    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "accept"
    rows[0]["left_revision"] = "2"
    _write_csv(output_path, rows[:1])

    try:
        import_review_dataset(database_path, output_path, apply=True)
    except ValueError as exc:
        assert "candidate_id does not match" in str(exc)
    else:
        raise AssertionError("tampered candidate identity should be rejected")

    conn = sqlite3.connect(database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_stale_exported_revision_is_never_applied(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    events = _seed(database_path)
    export_review_dataset(
        database_path, output_path, strategy="similarity", min_score=0.70, limit=20
    )
    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "accept"
    _write_csv(output_path, rows[:1])

    reviewed_event_id = rows[0]["left_event_id"]
    reviewed_event = next(event for event in events if event.event_id == reviewed_event_id)
    changed = replace(reviewed_event, description="A later source revision")
    sync_events(
        database_path,
        [changed],
        observed_at_utc="2026-10-29T10:00:00+00:00",
    )
    result = import_review_dataset(database_path, output_path, apply=True)

    assert result["status"] == "completed_with_errors"
    assert result["stale_count"] == 1
    assert result["applied_count"] == 0
    assert result["rows"][0]["status"] == "stale"
    conn = sqlite3.connect(database_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM reconciliation_decisions"
    ).fetchone()[0] == 0
    conn.close()


def test_import_rejects_conflicting_labels_for_same_revision_pair(tmp_path):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    _seed(database_path)
    export_review_dataset(
        database_path, output_path, strategy="similarity", min_score=0.70, limit=20
    )
    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "accept"
    rows[1]["left_event_id"] = rows[0]["left_event_id"]
    rows[1]["right_event_id"] = rows[0]["right_event_id"]
    rows[1]["left_revision"] = rows[0]["left_revision"]
    rows[1]["right_revision"] = rows[0]["right_revision"]
    rows[1]["strategy"] = "exact"
    rows[1]["min_score"] = ""
    rows[1]["candidate_id"] = _candidate_id(
        rows[1]["left_event_id"],
        int(rows[1]["left_revision"]),
        rows[1]["right_event_id"],
        int(rows[1]["right_revision"]),
        strategy="exact",
        min_score=0.80,
    )
    rows[1]["review_decision"] = "reject"
    _write_csv(output_path, rows[:2])

    try:
        import_review_dataset(database_path, output_path, apply=True)
    except ValueError as exc:
        assert "conflicting labels" in str(exc)
    else:
        raise AssertionError("conflicting batch labels should fail before writes")


def test_review_dataset_cli_export_and_dry_run_import(tmp_path, capsys):
    database_path = tmp_path / "events.sqlite"
    output_path = tmp_path / "review.csv"
    _seed(database_path)

    assert main(
        [
            "reconcile",
            "--db",
            str(database_path),
            "--action",
            "review-export",
            "--strategy",
            "similarity",
            "--min-score",
            "0.70",
            "--output",
            str(output_path),
        ]
    ) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["status"] == "exported"
    assert output_path.is_file()

    rows = _read_csv(output_path)
    rows[0]["review_decision"] = "accept"
    _write_csv(output_path, rows[:1])
    assert main(
        [
            "reconcile",
            "--db",
            str(database_path),
            "--action",
            "review-import",
            "--input",
            str(output_path),
        ]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["mode"] == "dry_run"
    assert imported["validated_count"] == 1
