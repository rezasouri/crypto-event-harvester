"""Transactional, human-reviewed cross-provider event reconciliation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .cross_provider_candidates import (
    _compatible,
    _identity_key,
    _similarity_evidence,
    match_candidates,
    match_similar_candidates,
)
from .storage import _connect


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _open(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    # create=True performs any supported additive migration in storage._connect.
    return _connect(path, create=True)


def _load_pair(conn: sqlite3.Connection, left_id: str, right_id: str) -> tuple[dict, int, dict, int]:
    if not left_id or not right_id or left_id == right_id:
        raise ValueError("two distinct event IDs are required")
    rows = conn.execute(
        "SELECT event_id, provider, revision, payload_json FROM events "
        "WHERE event_id IN (?, ?) ORDER BY event_id",
        (left_id, right_id),
    ).fetchall()
    if len(rows) != 2:
        present = {row["event_id"] for row in rows}
        missing = sorted({left_id, right_id} - present)
        raise ValueError(f"unknown source event ID(s): {', '.join(missing)}")
    items = []
    for row in rows:
        item = json.loads(row["payload_json"])
        if item.get("event_id") != row["event_id"] or item.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
        items.append((item, row["revision"]))
    return items[0][0], items[0][1], items[1][0], items[1][1]


def _match(left: dict, right: dict, strategy: str, min_score: float) -> dict:
    if strategy == "exact":
        left_key, right_key = _identity_key(left), _identity_key(right)
        if left_key is None or left_key != right_key or not _compatible(left, right):
            raise ValueError("pair is no longer eligible under the exact strategy")
        title, start, end = left_key
        return {
            "canonical_title": title,
            "exact_start_date": start,
            "exact_end_date": end,
            "nonconflicting_metadata_only": True,
        }
    if strategy == "similarity":
        evidence = _similarity_evidence(left, right)
        if evidence is None or evidence["score"] < min_score:
            raise ValueError("pair is no longer eligible under the similarity strategy")
        return evidence
    raise ValueError("strategy must be exact or similarity")


def _check_expected_revisions(
    left_event_id: str,
    left_revision: int,
    right_event_id: str,
    right_revision: int,
    expected_revisions: dict[str, int] | None,
) -> None:
    if expected_revisions is None:
        return
    event_ids = {left_event_id, right_event_id}
    if set(expected_revisions) != event_ids:
        raise ValueError("expected_revisions must contain exactly the two reviewed event IDs")
    for event_id, current_revision in (
        (left_event_id, left_revision),
        (right_event_id, right_revision),
    ):
        expected = expected_revisions[event_id]
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ValueError("expected source revisions must be positive integers")
        if current_revision != expected:
            raise ValueError(
                "stale review candidate: "
                f"{event_id} was exported at revision {expected} "
                f"but the current revision is {current_revision}"
            )


def validate_reconciliation_pair(
    database_path: str | Path,
    *,
    left_event_id: str,
    right_event_id: str,
    strategy: str,
    min_score: float = 0.80,
    expected_revisions: dict[str, int] | None = None,
) -> dict:
    """Validate an exported review pair without migrating or writing to SQLite."""
    conn = _connect(Path(database_path), create=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        left, left_revision, right, right_revision = _load_pair(
            conn, left_event_id, right_event_id
        )
        _check_expected_revisions(
            left["event_id"],
            left_revision,
            right["event_id"],
            right_revision,
            expected_revisions,
        )
        evidence = _match(left, right, strategy, min_score)
        return {
            "source_revisions": {
                left["event_id"]: left_revision,
                right["event_id"]: right_revision,
            },
            "strategy": strategy,
            "evidence": evidence,
            "database_rows_modified": 0,
        }
    finally:
        conn.close()


def reconcile_pair(
    database_path: str | Path,
    *,
    left_event_id: str,
    right_event_id: str,
    strategy: str,
    decision: str,
    min_score: float = 0.80,
    note: str | None = None,
    expected_revisions: dict[str, int] | None = None,
) -> dict:
    """Revalidate and record one decision atomically against current snapshots."""
    if decision not in {"accepted", "rejected"}:
        raise ValueError("decision must be accepted or rejected")
    if not 0 <= min_score <= 1:
        raise ValueError("min-score must be between 0 and 1")
    conn = _open(database_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            left, lrev, right, rrev = _load_pair(conn, left_event_id, right_event_id)
            _check_expected_revisions(
                left["event_id"], lrev, right["event_id"], rrev, expected_revisions
            )
            # Keep persisted pair order canonical and align evidence/revisions.
            if left["event_id"] > right["event_id"]:
                left, right, lrev, rrev = right, left, rrev, lrev
            evidence = _match(left, right, strategy, min_score)
            pair = (left["event_id"], right["event_id"], lrev, rrev, decision, strategy)
            opposite = conn.execute(
                "SELECT * FROM reconciliation_decisions WHERE left_event_id=? AND "
                "right_event_id=? AND left_revision=? AND right_revision=? AND decision<>? "
                "ORDER BY decided_at_utc, decision_id LIMIT 1",
                (left["event_id"], right["event_id"], lrev, rrev, decision),
            ).fetchone()
            if opposite is not None:
                raise ValueError(
                    "conflict: opposite decision already exists for this event pair and "
                    "source revisions "
                    f"({opposite['decision']} via {opposite['strategy']})"
                )

            old = conn.execute(
                "SELECT * FROM reconciliation_decisions WHERE left_event_id=? AND "
                "right_event_id=? AND left_revision=? AND right_revision=? AND decision=? "
                "AND strategy=?",
                pair,
            ).fetchone()
            if old is not None:
                return _decision_result(old, json.loads(old["evidence_json"]), idempotent=True)

            canonical_id = None
            if decision == "accepted":
                memberships = conn.execute(
                    "SELECT m.event_id, m.canonical_event_id, e.provider "
                    "FROM canonical_event_members m JOIN events e USING(event_id) "
                    "WHERE m.event_id IN (?, ?)",
                    (left["event_id"], right["event_id"]),
                ).fetchall()
                by_event = {row["event_id"]: row for row in memberships}
                lm, rm = by_event.get(left["event_id"]), by_event.get(right["event_id"])
                if lm and rm and lm["canonical_event_id"] != rm["canonical_event_id"]:
                    raise ValueError("conflict: events belong to different canonical identities")
                canonical_id = (lm or rm)["canonical_event_id"] if lm or rm else f"canonical:{uuid.uuid4()}"
                provider_collision = conn.execute(
                    "SELECT m.event_id FROM canonical_event_members m JOIN events e USING(event_id) "
                    "WHERE m.canonical_event_id=? AND e.provider IN (?, ?) "
                    "AND m.event_id NOT IN (?, ?)",
                    (canonical_id, left["provider"], right["provider"], left["event_id"], right["event_id"]),
                ).fetchone()
                if provider_collision:
                    raise ValueError(
                        f"conflict: canonical identity already contains provider member "
                        f"{provider_collision['event_id']}"
                    )
                now = _now()
                if not lm and not rm:
                    conn.execute("INSERT INTO canonical_events VALUES (?, ?, ?)", (canonical_id, now, now))
                else:
                    conn.execute(
                        "UPDATE canonical_events SET updated_at_utc=? WHERE canonical_event_id=?",
                        (now, canonical_id),
                    )
                for item, revision, member in ((left, lrev, lm), (right, rrev, rm)):
                    if member is None:
                        conn.execute(
                            "INSERT INTO canonical_event_members VALUES (?, ?, ?, ?)",
                            (canonical_id, item["event_id"], revision, now),
                        )
                    else:
                        # A source event can gain revisions after it was linked.
                        # Re-accepting the current pair refreshes the reviewed revision.
                        conn.execute(
                            "UPDATE canonical_event_members SET source_revision_at_link=? "
                            "WHERE canonical_event_id=? AND event_id=?",
                            (revision, canonical_id, item["event_id"]),
                        )

            decision_id = f"decision:{uuid.uuid4()}"
            now = _now()
            conn.execute(
                "INSERT INTO reconciliation_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    left["event_id"],
                    right["event_id"],
                    lrev,
                    rrev,
                    decision,
                    strategy,
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    canonical_id,
                    note,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM reconciliation_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            return _decision_result(row, evidence, idempotent=False)
    finally:
        conn.close()


def _decision_result(row: sqlite3.Row, evidence: dict, *, idempotent: bool) -> dict:
    return {
        "decision_id": row["decision_id"],
        "decision": row["decision"],
        "canonical_event_id": row["canonical_event_id"],
        "source_event_ids": [row["left_event_id"], row["right_event_id"]],
        "source_revisions": {
            row["left_event_id"]: row["left_revision"],
            row["right_event_id"]: row["right_revision"],
        },
        "strategy": row["strategy"],
        "evidence": evidence,
        "idempotent": idempotent,
        "conflict": None,
    }


def reconciliation_status(database_path: str | Path) -> dict:
    conn = _open(database_path)
    try:
        return {
            "canonical_events": conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            "memberships": conn.execute("SELECT COUNT(*) FROM canonical_event_members").fetchone()[0],
            "accepted_decisions": conn.execute(
                "SELECT COUNT(*) FROM reconciliation_decisions WHERE decision='accepted'"
            ).fetchone()[0],
            "rejected_decisions": conn.execute(
                "SELECT COUNT(*) FROM reconciliation_decisions WHERE decision='rejected'"
            ).fetchone()[0],
            "stale_memberships": conn.execute(
                "SELECT COUNT(*) FROM canonical_event_members m "
                "JOIN events e USING(event_id) "
                "WHERE m.source_revision_at_link <> e.revision"
            ).fetchone()[0],
            "empty_canonical_events": conn.execute(
                "SELECT COUNT(*) FROM canonical_events c "
                "WHERE NOT EXISTS (SELECT 1 FROM canonical_event_members m "
                "WHERE m.canonical_event_id = c.canonical_event_id)"
            ).fetchone()[0],
            "unlinked_actions": conn.execute(
                "SELECT COUNT(*) FROM canonical_membership_actions WHERE action='unlinked'"
            ).fetchone()[0],
            "split_actions": conn.execute(
                "SELECT COUNT(*) FROM canonical_membership_actions WHERE action='split'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def pending_candidates(
    database_path: str | Path, *, strategy: str = "similarity", min_score: float = 0.80, limit: int = 100
) -> dict:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if strategy not in {"exact", "similarity"}:
        raise ValueError("strategy must be exact or similarity")
    conn = _open(database_path)
    try:
        decisions = conn.execute(
            "SELECT DISTINCT left_event_id, left_revision, right_event_id, right_revision "
            "FROM reconciliation_decisions"
        ).fetchall()
        linked_pairs = conn.execute(
            """
            SELECT left_member.event_id AS left_event_id,
                   left_member.source_revision_at_link AS left_linked_revision,
                   left_event.revision AS left_revision,
                   right_member.event_id AS right_event_id,
                   right_member.source_revision_at_link AS right_linked_revision,
                   right_event.revision AS right_revision
            FROM canonical_event_members AS left_member
            JOIN canonical_event_members AS right_member
              ON left_member.canonical_event_id = right_member.canonical_event_id
             AND left_member.event_id < right_member.event_id
            JOIN events AS left_event ON left_event.event_id = left_member.event_id
            JOIN events AS right_event ON right_event.event_id = right_member.event_id
            WHERE left_event.provider <> right_event.provider
            """
        ).fetchall()
    finally:
        conn.close()

    excluded = {
        (row["left_event_id"], row["left_revision"], row["right_event_id"], row["right_revision"])
        for row in decisions
    }
    # A pair already linked into one canonical identity is not pending while
    # both linked source revisions are still current. A changed source revision
    # reopens the pair for human review.
    for row in linked_pairs:
        if (
            row["left_linked_revision"] == row["left_revision"]
            and row["right_linked_revision"] == row["right_revision"]
        ):
            excluded.add(
                (row["left_event_id"], row["left_revision"], row["right_event_id"], row["right_revision"])
            )

    report = (
        match_candidates(database_path, limit=limit, excluded_pairs=excluded).to_dict()
        if strategy == "exact"
        else match_similar_candidates(
            database_path, limit=limit, min_score=min_score, excluded_pairs=excluded
        ).to_dict()
    )
    report["pending_total"] = report["candidates_total"]
    return report


def canonical_list(database_path: str | Path, *, limit: int = 100) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    conn = _open(database_path)
    try:
        rows = conn.execute(
            "WITH selected AS (SELECT canonical_event_id FROM canonical_events "
            "ORDER BY canonical_event_id LIMIT ?) "
            "SELECT c.canonical_event_id, c.created_at_utc, c.updated_at_utc, "
            "m.event_id, m.source_revision_at_link, m.linked_at_utc, e.provider, "
            "e.revision AS current_revision, e.payload_json "
            "FROM selected s JOIN canonical_events c USING(canonical_event_id) "
            "LEFT JOIN canonical_event_members m USING(canonical_event_id) "
            "LEFT JOIN events e USING(event_id) ORDER BY c.canonical_event_id, m.event_id",
            (limit,),
        ).fetchall()
        result = {}
        for row in rows:
            item = result.setdefault(
                row["canonical_event_id"],
                {
                    "canonical_event_id": row["canonical_event_id"],
                    "created_at_utc": row["created_at_utc"],
                    "updated_at_utc": row["updated_at_utc"],
                    "members": [],
                },
            )
            if row["event_id"]:
                item["members"].append(
                    {
                        "event_id": row["event_id"],
                        "provider": row["provider"],
                        "source_revision_at_link": row["source_revision_at_link"],
                        "current_revision": row["current_revision"],
                        "membership_stale": row["source_revision_at_link"] != row["current_revision"],
                        "linked_at_utc": row["linked_at_utc"],
                        "event": json.loads(row["payload_json"]),
                    }
                )
        for item in result.values():
            item["member_count"] = len(item["members"])
            item["lifecycle_status"] = "active" if item["members"] else "empty"
        return list(result.values())[:limit]
    finally:
        conn.close()


def unlink_member(
    database_path: str | Path,
    *,
    canonical_event_id: str,
    event_id: str,
    note: str | None = None,
) -> dict:
    """Remove one canonical membership while preserving an immutable audit record."""
    if not canonical_event_id or not event_id:
        raise ValueError("unlink requires a canonical identity ID and an event ID")
    conn = _open(database_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = conn.execute(
                "SELECT canonical_event_id FROM canonical_events WHERE canonical_event_id=?",
                (canonical_event_id,),
            ).fetchone()
            if identity is None:
                raise ValueError(f"unknown canonical identity: {canonical_event_id}")
            member = conn.execute(
                "SELECT m.source_revision_at_link, m.linked_at_utc, e.revision "
                "FROM canonical_event_members m JOIN events e USING(event_id) "
                "WHERE m.canonical_event_id=? AND m.event_id=?",
                (canonical_event_id, event_id),
            ).fetchone()
            if member is None:
                raise ValueError(
                    f"event {event_id} is not a member of canonical identity {canonical_event_id}"
                )

            operation_id = f"membership-operation:{uuid.uuid4()}"
            action_id = f"membership-action:{uuid.uuid4()}"
            now = _now()
            conn.execute(
                "INSERT INTO canonical_membership_actions "
                "VALUES (?, ?, 'unlinked', ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    operation_id,
                    canonical_event_id,
                    event_id,
                    member["source_revision_at_link"],
                    member["revision"],
                    member["linked_at_utc"],
                    note,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM canonical_event_members WHERE canonical_event_id=? AND event_id=?",
                (canonical_event_id, event_id),
            )
            conn.execute(
                "UPDATE canonical_events SET updated_at_utc=? WHERE canonical_event_id=?",
                (now, canonical_event_id),
            )
            return {
                "status": "unlinked",
                "operation_id": operation_id,
                "action_id": action_id,
                "canonical_event_id": canonical_event_id,
                "event_id": event_id,
                "source_revision_at_link": member["source_revision_at_link"],
                "current_source_revision": member["revision"],
                "identity_retained": True,
                "note": note,
                "acted_at_utc": now,
            }
    finally:
        conn.close()


def split_canonical_identity(
    database_path: str | Path,
    *,
    canonical_event_id: str,
    event_ids: list[str],
    note: str | None = None,
) -> dict:
    """Move a proper subset of members into a new identity, with one audit row per member."""
    if not canonical_event_id:
        raise ValueError("split requires a canonical identity ID")
    if not event_ids:
        raise ValueError("split requires at least one member event ID")
    if any(not item for item in event_ids):
        raise ValueError("split event IDs must be non-empty")
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("split event IDs must be unique")

    conn = _open(database_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = conn.execute(
                "SELECT canonical_event_id FROM canonical_events WHERE canonical_event_id=?",
                (canonical_event_id,),
            ).fetchone()
            if identity is None:
                raise ValueError(f"unknown canonical identity: {canonical_event_id}")
            members = conn.execute(
                "SELECT m.event_id, m.source_revision_at_link, m.linked_at_utc, e.revision "
                "FROM canonical_event_members m JOIN events e USING(event_id) "
                "WHERE m.canonical_event_id=? ORDER BY m.event_id",
                (canonical_event_id,),
            ).fetchall()
            by_event = {row["event_id"]: row for row in members}
            missing = sorted(set(event_ids) - set(by_event))
            if missing:
                raise ValueError(
                    f"event ID(s) are not members of canonical identity {canonical_event_id}: "
                    + ", ".join(missing)
                )
            if len(event_ids) >= len(members):
                raise ValueError("split must move a proper subset and leave at least one source member")

            new_canonical_id = f"canonical:{uuid.uuid4()}"
            operation_id = f"membership-operation:{uuid.uuid4()}"
            now = _now()
            conn.execute(
                "INSERT INTO canonical_events VALUES (?, ?, ?)",
                (new_canonical_id, now, now),
            )
            moved = []
            for member_event_id in sorted(event_ids):
                member = by_event[member_event_id]
                action_id = f"membership-action:{uuid.uuid4()}"
                conn.execute(
                    "INSERT INTO canonical_membership_actions VALUES (?, ?, 'split', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action_id,
                        operation_id,
                        canonical_event_id,
                        new_canonical_id,
                        member_event_id,
                        member["source_revision_at_link"],
                        member["revision"],
                        member["linked_at_utc"],
                        note,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE canonical_event_members SET canonical_event_id=? "
                    "WHERE canonical_event_id=? AND event_id=?",
                    (new_canonical_id, canonical_event_id, member_event_id),
                )
                moved.append(
                    {
                        "event_id": member_event_id,
                        "source_revision_at_link": member["source_revision_at_link"],
                        "current_source_revision": member["revision"],
                        "action_id": action_id,
                    }
                )
            conn.execute(
                "UPDATE canonical_events SET updated_at_utc=? WHERE canonical_event_id=?",
                (now, canonical_event_id),
            )
            return {
                "status": "split",
                "operation_id": operation_id,
                "source_canonical_event_id": canonical_event_id,
                "new_canonical_event_id": new_canonical_id,
                "moved_members": moved,
                "remaining_member_event_ids": sorted(set(by_event) - set(event_ids)),
                "note": note,
                "acted_at_utc": now,
            }
    finally:
        conn.close()


def membership_history(
    database_path: str | Path,
    *,
    event_id: str | None = None,
    canonical_event_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Read the append-only unlink/split audit trail."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if event_id is not None and not event_id:
        raise ValueError("event ID filter must be non-empty")
    if canonical_event_id is not None and not canonical_event_id:
        raise ValueError("canonical identity filter must be non-empty")
    conn = _open(database_path)
    try:
        clauses = []
        params: list[object] = []
        if event_id is not None:
            clauses.append("event_id=?")
            params.append(event_id)
        if canonical_event_id is not None:
            clauses.append("(canonical_event_id=? OR target_canonical_event_id=?)")
            params.extend((canonical_event_id, canonical_event_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT * FROM canonical_membership_actions" + where
            + " ORDER BY acted_at_utc, operation_id, action_id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def decision_history(
    database_path: str | Path, *, event_id: str | None = None, limit: int = 100
) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    conn = _open(database_path)
    try:
        if event_id:
            rows = conn.execute(
                "SELECT * FROM reconciliation_decisions WHERE left_event_id=? OR right_event_id=? "
                "ORDER BY decided_at_utc, decision_id LIMIT ?",
                (event_id, event_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconciliation_decisions ORDER BY decided_at_utc, decision_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in rows]
    finally:
        conn.close()
