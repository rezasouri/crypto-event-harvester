"""Offline manual reconciliation, migration, and CLI coverage."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.providers.icodrops import parse_listing as parse_icodrops
from crypto_event_harvester.reconciliation import (
    canonical_list,
    decision_history,
    membership_history,
    pending_candidates,
    reconcile_pair,
    reconciliation_status,
    split_canonical_identity,
    unlink_member,
)
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def _samples():
    ico = normalize_event(parse_icodrops((FIXTURES / "icodrops_upcoming.html").read_text())[0])
    coindar = normalize_event(parse_coindar((FIXTURES / "coindar_listing_sample.html").read_text())[0])
    crypto = normalize_event(parse_cryptoevents((FIXTURES / "cryptoevents_home.html").read_text())[0])
    return ico, coindar, crypto


def _dated(
    event,
    *,
    event_id=None,
    source_event_id=None,
    title="Ethereum Shanghai Upgrade",
    date="2026-11-10",
    project="Ethereum",
    **changes,
):
    values = {
        "event_id": event_id or event.event_id,
        "source_event_id": source_event_id or event.source_event_id,
        "title": title,
        "start_date": date,
        "end_date": date,
        "date_text": date,
        "date_precision": "day",
        "project": project,
        "ticker": "ETH",
        "period_key": None,
    }
    values.update(changes)
    return replace(event, **values)


def _seed(db):
    ico, coindar, crypto = _samples()
    events = [
        _dated(ico),
        _dated(coindar),
        _dated(crypto, title="Ethereum Shanghai Ecosystem Upgrade"),
    ]
    sync_events(db, events, observed_at_utc="2026-10-28T10:00:00+00:00")
    return events


def test_accept_create_expand_reversed_and_idempotent(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, third = _seed(db)
    third = replace(third, event_type=first.event_type, title="Ethereum Shanghai Upgrade")
    sync_events(db, [third], observed_at_utc="2026-10-29T10:00:00+00:00")
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    assert accepted["canonical_event_id"].startswith("canonical:")
    assert len(canonical_list(db)[0]["members"]) == 2
    repeated = reconcile_pair(
        db,
        left_event_id=second.event_id,
        right_event_id=first.event_id,
        strategy="exact",
        decision="accepted",
    )
    assert repeated["idempotent"] is True
    assert repeated["decision_id"] == accepted["decision_id"]
    expanded = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=third.event_id,
        strategy="similarity",
        decision="accepted",
        min_score=0.8,
    )
    assert expanded["canonical_event_id"] == accepted["canonical_event_id"]
    assert len(canonical_list(db)[0]["members"]) == 3
    # The transitive second/third pair is already represented by the same
    # current canonical identity and must not be proposed again.
    assert pending_candidates(db, strategy="exact")["candidates"] == []


def test_stale_membership_reopens_and_accept_refreshes_revision(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    assert pending_candidates(db, strategy="exact")["candidates"] == []

    changed = replace(first, description="source description revised after linking")
    sync_events(db, [changed], observed_at_utc="2026-10-29T10:00:00+00:00")
    assert reconciliation_status(db)["stale_memberships"] == 1
    stale_members = {item["event_id"]: item for item in canonical_list(db)[0]["members"]}
    assert stale_members[first.event_id]["membership_stale"] is True
    pending = pending_candidates(db, strategy="exact")
    assert pending["candidates_total"] == 1
    candidate = pending["candidates"][0]
    assert {candidate["left"]["event_id"], candidate["right"]["event_id"]} == {
        first.event_id, second.event_id
    }

    refreshed = reconcile_pair(
        db,
        left_event_id=second.event_id,
        right_event_id=first.event_id,
        strategy="exact",
        decision="accepted",
    )
    assert refreshed["canonical_event_id"] == accepted["canonical_event_id"]
    members = {item["event_id"]: item for item in canonical_list(db)[0]["members"]}
    assert members[first.event_id]["source_revision_at_link"] == 2
    assert members[first.event_id]["current_revision"] == 2
    assert members[first.event_id]["membership_stale"] is False
    assert reconciliation_status(db)["stale_memberships"] == 0
    assert pending_candidates(db, strategy="exact")["candidates"] == []


def test_opposite_decisions_for_same_revisions_are_conflicts(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    with pytest.raises(ValueError, match="opposite decision"):
        reconcile_pair(
            db,
            left_event_id=second.event_id,
            right_event_id=first.event_id,
            strategy="similarity",
            decision="rejected",
        )
    history = decision_history(db, event_id=first.event_id)
    assert len(history) == 1
    assert history[0]["decision"] == "accepted"


def test_reject_audits_without_members_and_revision_change_reopens(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    result = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="rejected",
        note="reviewed",
    )
    assert result["canonical_event_id"] is None
    assert result["idempotent"] is False
    assert (
        reconcile_pair(
            db,
            left_event_id=second.event_id,
            right_event_id=first.event_id,
            strategy="exact",
            decision="rejected",
        )["idempotent"]
        is True
    )
    assert canonical_list(db) == []
    assert len(pending_candidates(db, strategy="exact")["candidates"]) == 0
    changed = replace(first, description="changed source detail")
    sync_events(db, [changed], observed_at_utc="2026-10-29T10:00:00+00:00")
    assert pending_candidates(db, strategy="exact")["candidates"]
    assert len(decision_history(db, event_id=first.event_id)) == 1


def test_same_provider_and_separate_canonical_conflicts(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, third = _seed(db)
    fourth = _dated(second, event_id="coindar:other", source_event_id="other")
    third = replace(third, event_type=first.event_type, title="Ethereum Shanghai Upgrade")
    sync_events(db, [fourth, third], observed_at_utc="2026-10-29T10:00:00+00:00")
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    with pytest.raises(ValueError, match="same provider|provider member"):
        reconcile_pair(
            db,
            left_event_id=first.event_id,
            right_event_id=fourth.event_id,
            strategy="exact",
            decision="accepted",
        )
    reconcile_pair(
        db,
        left_event_id=third.event_id,
        right_event_id=fourth.event_id,
        strategy="exact",
        decision="accepted",
    )
    with pytest.raises(ValueError, match="different canonical identities"):
        reconcile_pair(
            db,
            left_event_id=first.event_id,
            right_event_id=third.event_id,
            strategy="exact",
            decision="accepted",
        )
    assert reconciliation_status(db)["canonical_events"] == 2


def test_stale_ineligible_pairs_and_failed_transactions(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    with pytest.raises(ValueError, match="min-score"):
        reconcile_pair(
            db,
            left_event_id=first.event_id,
            right_event_id=second.event_id,
            strategy="similarity",
            decision="accepted",
            min_score=1.1,
        )
    changed = replace(first, title="Solana unrelated conference", project="Solana", ticker="SOL")
    sync_events(db, [changed], observed_at_utc="2026-10-29T10:00:00+00:00")
    with pytest.raises(ValueError, match="eligible"):
        reconcile_pair(
            db,
            left_event_id=first.event_id,
            right_event_id=second.event_id,
            strategy="exact",
            decision="accepted",
        )
    assert reconciliation_status(db) == {
        "canonical_events": 0,
        "memberships": 0,
        "accepted_decisions": 0,
        "rejected_decisions": 0,
        "stale_memberships": 0,
        "empty_canonical_events": 0,
        "unlinked_actions": 0,
        "split_actions": 0,
    }


def test_version_one_migration_preserves_existing_data_and_is_repeatable(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(db)
    conn = sqlite3.connect(db)
    before_events = conn.execute("SELECT * FROM events ORDER BY event_id").fetchall()
    before_revisions = conn.execute("SELECT * FROM event_revisions ORDER BY event_id, revision").fetchall()
    conn.execute("CREATE TABLE checkpoints (name TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO checkpoints VALUES ('x', 'preserved')")
    for name in (
        "trg_canonical_member_provider_update",
        "trg_canonical_member_delete_audited",
        "trg_canonical_member_move_audited",
        "trg_membership_actions_no_update",
        "trg_membership_actions_no_delete",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in (
        "canonical_membership_actions",
        "reconciliation_decisions",
        "canonical_event_members",
        "canonical_events",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    assert reconciliation_status(db)["canonical_events"] == 0
    assert reconciliation_status(db)["canonical_events"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT * FROM events ORDER BY event_id").fetchall() == before_events
    assert (
        conn.execute("SELECT * FROM event_revisions ORDER BY event_id, revision").fetchall()
        == before_revisions
    )
    assert conn.execute("SELECT * FROM checkpoints").fetchall() == [("x", "preserved")]
    conn.close()


@pytest.mark.parametrize("action", ["status", "pending", "list", "history", "membership-history"])
def test_cli_read_actions_emit_json(tmp_path, capsys, action):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    if action == "history":
        reconcile_pair(
            db,
            left_event_id=first.event_id,
            right_event_id=second.event_id,
            strategy="exact",
            decision="rejected",
        )
    args = ["reconcile", "--db", str(db), "--action", action]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) is not None


def test_cli_accept_reject_and_invalid_arguments(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    assert (
        main(
            [
                "reconcile",
                "--db",
                str(db),
                "--action",
                "accept",
                "--left-event-id",
                first.event_id,
                "--right-event-id",
                second.event_id,
                "--strategy",
                "exact",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["decision"] == "accepted"
    assert (
        main(
            [
                "reconcile",
                "--db",
                str(db),
                "--action",
                "reject",
                "--left-event-id",
                first.event_id,
                "--right-event-id",
                second.event_id,
                "--strategy",
                "exact",
            ]
        )
        == 2
    )
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["status"] == "conflict"
    assert "opposite decision" in conflict["error"]

    assert main(["reconcile", "--db", str(db), "--action", "accept"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"
    assert "require both event IDs" in error["error"]



def test_unlink_member_keeps_identity_and_preserves_both_histories(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    before_events = sqlite3.connect(db).execute(
        "SELECT * FROM events ORDER BY event_id"
    ).fetchall()
    result = unlink_member(
        db,
        canonical_event_id=accepted["canonical_event_id"],
        event_id=first.event_id,
        note="Source records describe different event editions",
    )
    assert result["status"] == "unlinked"
    assert result["event_id"] == first.event_id
    listed = canonical_list(db)
    assert len(listed) == 1
    assert listed[0]["canonical_event_id"] == accepted["canonical_event_id"]
    assert listed[0]["lifecycle_status"] == "active"
    assert listed[0]["member_count"] == 1
    assert [member["event_id"] for member in listed[0]["members"]] == [second.event_id]
    assert reconciliation_status(db)["unlinked_actions"] == 1
    assert membership_history(db, event_id=first.event_id)[0]["action"] == "unlinked"
    assert decision_history(db, event_id=first.event_id)[0]["decision"] == "accepted"
    assert sqlite3.connect(db).execute(
        "SELECT * FROM events ORDER BY event_id"
    ).fetchall() == before_events

    unlink_member(
        db,
        canonical_event_id=accepted["canonical_event_id"],
        event_id=second.event_id,
        note="Empty the retained identity without deleting its history",
    )
    empty = canonical_list(db)[0]
    assert empty["members"] == []
    assert empty["member_count"] == 0
    assert empty["lifecycle_status"] == "empty"
    assert reconciliation_status(db)["empty_canonical_events"] == 1
    with pytest.raises(ValueError, match="not a member"):
        unlink_member(
            db,
            canonical_event_id=accepted["canonical_event_id"],
            event_id=second.event_id,
        )


def test_split_moves_only_selected_members_and_records_operation_history(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, third = _seed(db)
    third = replace(third, event_type=first.event_type, title="Ethereum Shanghai Upgrade")
    sync_events(db, [third], observed_at_utc="2026-10-29T10:00:00+00:00")
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=third.event_id,
        strategy="similarity",
        decision="accepted",
        min_score=0.8,
    )
    before_events = sqlite3.connect(db).execute(
        "SELECT * FROM events ORDER BY event_id"
    ).fetchall()
    before_revisions = sqlite3.connect(db).execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall()

    result = split_canonical_identity(
        db,
        canonical_event_id=accepted["canonical_event_id"],
        event_ids=[third.event_id],
        note="Third provider entry is a separate event",
    )
    assert result["status"] == "split"
    assert result["source_canonical_event_id"] == accepted["canonical_event_id"]
    assert result["moved_members"][0]["event_id"] == third.event_id
    assert result["remaining_member_event_ids"] == sorted([first.event_id, second.event_id])
    identities = {item["canonical_event_id"]: item for item in canonical_list(db)}
    assert set(identities) == {accepted["canonical_event_id"], result["new_canonical_event_id"]}
    assert identities[accepted["canonical_event_id"]]["member_count"] == 2
    assert identities[result["new_canonical_event_id"]]["members"][0]["event_id"] == third.event_id
    assert reconciliation_status(db)["split_actions"] == 1
    audit = membership_history(
        db, canonical_event_id=result["new_canonical_event_id"]
    )
    assert len(audit) == 1
    assert audit[0]["action"] == "split"
    assert audit[0]["canonical_event_id"] == accepted["canonical_event_id"]
    assert audit[0]["target_canonical_event_id"] == result["new_canonical_event_id"]
    assert sqlite3.connect(db).execute(
        "SELECT * FROM events ORDER BY event_id"
    ).fetchall() == before_events
    assert sqlite3.connect(db).execute(
        "SELECT * FROM event_revisions ORDER BY event_id, revision"
    ).fetchall() == before_revisions


def test_split_rejects_invalid_subsets_without_partial_writes(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    canonical_id = accepted["canonical_event_id"]
    before = canonical_list(db)
    with pytest.raises(ValueError, match="proper subset"):
        split_canonical_identity(
            db, canonical_event_id=canonical_id, event_ids=[first.event_id, second.event_id]
        )
    with pytest.raises(ValueError, match="unique"):
        split_canonical_identity(
            db, canonical_event_id=canonical_id, event_ids=[first.event_id, first.event_id]
        )
    with pytest.raises(ValueError, match="not members"):
        split_canonical_identity(
            db, canonical_event_id=canonical_id, event_ids=["missing:event"]
        )
    assert canonical_list(db) == before
    assert membership_history(db) == []
    assert reconciliation_status(db)["split_actions"] == 0


def test_membership_changes_require_append_only_audit_records(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="audit action"):
        conn.execute(
            "DELETE FROM canonical_event_members WHERE canonical_event_id=? AND event_id=?",
            (accepted["canonical_event_id"], first.event_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="split audit action"):
        conn.execute(
            "UPDATE canonical_event_members SET canonical_event_id='unreviewed' "
            "WHERE canonical_event_id=? AND event_id=?",
            (accepted["canonical_event_id"], first.event_id),
        )
    conn.rollback()
    conn.close()

    unlink_member(
        db,
        canonical_event_id=accepted["canonical_event_id"],
        event_id=first.event_id,
    )
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE canonical_membership_actions SET note='rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM canonical_membership_actions")
    conn.rollback()
    conn.close()


def test_version_two_migration_preserves_existing_canonical_state(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    conn = sqlite3.connect(db)
    before_members = conn.execute(
        "SELECT * FROM canonical_event_members ORDER BY event_id"
    ).fetchall()
    before_decisions = conn.execute(
        "SELECT * FROM reconciliation_decisions ORDER BY decision_id"
    ).fetchall()
    for name in (
        "trg_canonical_member_provider_update",
        "trg_canonical_member_delete_audited",
        "trg_canonical_member_move_audited",
        "trg_membership_actions_no_update",
        "trg_membership_actions_no_delete",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP TABLE canonical_membership_actions")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    assert reconciliation_status(db)["canonical_events"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute(
        "SELECT * FROM canonical_event_members ORDER BY event_id"
    ).fetchall() == before_members
    assert conn.execute(
        "SELECT * FROM reconciliation_decisions ORDER BY decision_id"
    ).fetchall() == before_decisions
    assert conn.execute("SELECT COUNT(*) FROM canonical_membership_actions").fetchone()[0] == 0
    conn.close()


def test_cli_unlink_split_and_membership_history(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    first, second, third = _seed(db)
    third = replace(third, event_type=first.event_type, title="Ethereum Shanghai Upgrade")
    sync_events(db, [third], observed_at_utc="2026-10-29T10:00:00+00:00")
    accepted = reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=third.event_id,
        strategy="similarity",
        decision="accepted",
        min_score=0.8,
    )
    canonical_id = accepted["canonical_event_id"]

    assert main([
        "reconcile", "--db", str(db), "--action", "split",
        "--canonical-event-id", canonical_id,
        "--event-ids", third.event_id,
        "--note", "Separate provider event",
    ]) == 0
    split = json.loads(capsys.readouterr().out)
    new_id = split["new_canonical_event_id"]
    assert split["status"] == "split"

    assert main([
        "reconcile", "--db", str(db), "--action", "membership-history",
        "--canonical-event-id", new_id,
    ]) == 0
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 1
    assert history[0]["action"] == "split"

    assert main([
        "reconcile", "--db", str(db), "--action", "unlink",
        "--canonical-event-id", canonical_id,
        "--event-id", first.event_id,
        "--note", "Manual correction",
    ]) == 0
    unlinked = json.loads(capsys.readouterr().out)
    assert unlinked["status"] == "unlinked"
    assert unlinked["event_id"] == first.event_id

    assert main([
        "reconcile", "--db", str(db), "--action", "unlink",
        "--event-id", first.event_id,
    ]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["status"] == "error"
    assert "--canonical-event-id" in error["error"]

def test_database_constraints_and_original_snapshots_unchanged(tmp_path):
    db = tmp_path / "events.sqlite"
    first, second, _ = _seed(db)
    before = sqlite3.connect(db).execute("SELECT * FROM events ORDER BY event_id").fetchall()
    reconcile_pair(
        db,
        left_event_id=first.event_id,
        right_event_id=second.event_id,
        strategy="exact",
        decision="accepted",
    )
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO canonical_event_members VALUES ('missing', 'missing', 1, 'now')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM reconciliation_decisions")
    conn.rollback()
    assert conn.execute("SELECT * FROM events ORDER BY event_id").fetchall() == before
    assert conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] == 3
    conn.close()
