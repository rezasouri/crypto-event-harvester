"""Human-reviewed semantic relationships between source event records.

Canonical identity remains separate: same-event decisions are stored only in
the canonical reconciliation subsystem. This graph stores directed semantic
edges such as a project event record participating in a conference record.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .storage import _connect

RELATION_TYPES = (
    "participates_in",
    "sponsors",
    "speaks_at",
    "organizes",
    "attends",
    "exhibits_at",
    "related_to",
)
_SNAPSHOT_FIELDS = (
    "title",
    "source_url",
    "start_date",
    "end_date",
    "date_precision",
    "event_type",
    "project",
    "ticker",
    "category",
    "location",
    "description",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def relationship_id_for(
    subject_event_id: str, object_event_id: str, relation_type: str
) -> str:
    """Return a stable identifier for one directed predicate between two records."""
    _validate_relation_type(relation_type)
    if not subject_event_id or not object_event_id or subject_event_id == object_event_id:
        raise ValueError("two distinct subject and object event IDs are required")
    raw = "\0".join((subject_event_id, object_event_id, relation_type)).encode("utf-8")
    return "relationship:" + hashlib.sha256(raw).hexdigest()[:32]


def _validate_relation_type(relation_type: str) -> None:
    if relation_type == "same_event":
        raise ValueError(
            "same_event belongs to canonical reconciliation; use reconcile accept/reject"
        )
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"relation_type must be one of: {', '.join(RELATION_TYPES)}")


def _open(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    return _connect(path, create=True)


def _load_pair(
    conn: sqlite3.Connection, subject_event_id: str, object_event_id: str
) -> tuple[dict, int, dict, int]:
    if (
        not subject_event_id
        or not object_event_id
        or subject_event_id == object_event_id
    ):
        raise ValueError("two distinct subject and object event IDs are required")
    rows = conn.execute(
        "SELECT event_id, provider, revision, payload_json FROM events "
        "WHERE event_id IN (?, ?)",
        (subject_event_id, object_event_id),
    ).fetchall()
    by_id = {row["event_id"]: row for row in rows}
    missing = sorted({subject_event_id, object_event_id} - set(by_id))
    if missing:
        raise ValueError(f"unknown source event ID(s): {', '.join(missing)}")

    def unpack(event_id: str) -> tuple[dict, int]:
        row = by_id[event_id]
        payload = json.loads(row["payload_json"])
        if payload.get("event_id") != event_id or payload.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
        return payload, row["revision"]

    subject, subject_revision = unpack(subject_event_id)
    object_event, object_revision = unpack(object_event_id)
    return subject, subject_revision, object_event, object_revision


def _check_expected_revisions(
    subject_revision: int,
    object_revision: int,
    expected_subject_revision: int | None,
    expected_object_revision: int | None,
) -> None:
    if (expected_subject_revision is None) != (expected_object_revision is None):
        raise ValueError("expected subject and object revisions must be supplied together")
    if expected_subject_revision is None:
        return
    for label, expected, current in (
        ("subject", expected_subject_revision, subject_revision),
        ("object", expected_object_revision, object_revision),
    ):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ValueError(f"expected {label} revision must be a positive integer")
        if expected != current:
            raise ValueError(
                f"stale relationship review: {label} event was exported at revision "
                f"{expected} but the current revision is {current}"
            )


def _evidence_snapshot(
    subject: dict,
    subject_revision: int,
    object_event: dict,
    object_revision: int,
    reviewer_evidence: dict | None,
) -> str:
    if reviewer_evidence is None:
        reviewer_evidence = {}
    if not isinstance(reviewer_evidence, dict):
        raise ValueError("evidence must be a JSON object")
    snapshot = {
        "subject": {
            "event_id": subject["event_id"],
            "provider": subject["provider"],
            "revision": subject_revision,
            **{field: subject.get(field) for field in _SNAPSHOT_FIELDS},
        },
        "object": {
            "event_id": object_event["event_id"],
            "provider": object_event["provider"],
            "revision": object_revision,
            **{field: object_event.get(field) for field in _SNAPSHOT_FIELDS},
        },
        "reviewer_evidence": reviewer_evidence,
    }
    try:
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"evidence must contain JSON-serializable values: {exc}") from exc


def review_relationship(
    database_path: str | Path,
    *,
    subject_event_id: str,
    object_event_id: str,
    relation_type: str,
    decision: str,
    expected_subject_revision: int | None = None,
    expected_object_revision: int | None = None,
    evidence: dict | None = None,
    note: str | None = None,
) -> dict:
    """Record an accepted/rejected directed relationship against exact revisions."""
    _validate_relation_type(relation_type)
    if decision not in {"accepted", "rejected"}:
        raise ValueError("decision must be accepted or rejected")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise ValueError("note must be text no longer than 2000 characters")

    relation_id = relationship_id_for(subject_event_id, object_event_id, relation_type)
    conn = _open(database_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            subject, subject_revision, object_event, object_revision = _load_pair(
                conn, subject_event_id, object_event_id
            )
            _check_expected_revisions(
                subject_revision,
                object_revision,
                expected_subject_revision,
                expected_object_revision,
            )
            evidence_json = _evidence_snapshot(
                subject, subject_revision, object_event, object_revision, evidence
            )
            latest = conn.execute(
                "SELECT decision_id, decision FROM event_relationship_decisions "
                "WHERE relationship_id=? AND subject_revision=? AND object_revision=? "
                "ORDER BY rowid DESC LIMIT 1",
                (relation_id, subject_revision, object_revision),
            ).fetchone()
            current = conn.execute(
                "SELECT accepted_decision_id FROM event_relationships "
                "WHERE relationship_id=?",
                (relation_id,),
            ).fetchone()

            if latest is not None and latest["decision"] == decision:
                if decision == "rejected" or (
                    current is not None and current["accepted_decision_id"] == latest["decision_id"]
                ):
                    return {
                        "status": "rejected" if decision == "rejected" else "accepted",
                        "decision_id": latest["decision_id"],
                        "relationship_id": relation_id,
                        "idempotent": True,
                        "database_rows_modified": 0,
                    }
                raise RuntimeError("accepted relationship decision exists without its graph edge")
            if latest is not None and latest["decision"] in {"accepted", "rejected"}:
                raise ValueError(
                    f"conflict: relationship already has a {latest['decision']} decision "
                    "for these exact source revisions"
                )

            decision_id = uuid.uuid4().hex
            timestamp = _now()
            conn.execute(
                "INSERT INTO event_relationship_decisions ("
                "decision_id, relationship_id, subject_event_id, object_event_id, "
                "subject_revision, object_revision, relation_type, decision, evidence_json, "
                "supersedes_decision_id, note, decided_at_utc"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    decision_id,
                    relation_id,
                    subject_event_id,
                    object_event_id,
                    subject_revision,
                    object_revision,
                    relation_type,
                    decision,
                    evidence_json,
                    note,
                    timestamp,
                ),
            )
            if decision == "accepted":
                conn.execute(
                    "INSERT INTO event_relationships ("
                    "relationship_id, subject_event_id, object_event_id, relation_type, "
                    "subject_revision, object_revision, evidence_json, accepted_decision_id, "
                    "created_at_utc, updated_at_utc"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(subject_event_id, object_event_id, relation_type) "
                    "DO UPDATE SET subject_revision=excluded.subject_revision, "
                    "object_revision=excluded.object_revision, evidence_json=excluded.evidence_json, "
                    "accepted_decision_id=excluded.accepted_decision_id, "
                    "updated_at_utc=excluded.updated_at_utc",
                    (
                        relation_id,
                        subject_event_id,
                        object_event_id,
                        relation_type,
                        subject_revision,
                        object_revision,
                        evidence_json,
                        decision_id,
                        timestamp,
                        timestamp,
                    ),
                )
            return {
                "status": decision,
                "decision_id": decision_id,
                "relationship_id": relation_id,
                "subject_revision": subject_revision,
                "object_revision": object_revision,
                "idempotent": False,
                "database_rows_modified": 2 if decision == "accepted" else 1,
                "source_events_modified": 0,
            }
    finally:
        conn.close()


def retract_relationship(
    database_path: str | Path, *, relationship_id: str, note: str | None = None
) -> dict:
    """Retract one accepted edge while preserving its decision history."""
    if not relationship_id:
        raise ValueError("relationship_id is required")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise ValueError("note must be text no longer than 2000 characters")
    conn = _open(database_path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            edge = conn.execute(
                "SELECT * FROM event_relationships WHERE relationship_id=?",
                (relationship_id,),
            ).fetchone()
            if edge is None:
                raise ValueError(f"unknown active relationship: {relationship_id}")
            decision_id = uuid.uuid4().hex
            timestamp = _now()
            evidence = json.loads(edge["evidence_json"])
            evidence["retraction"] = {"note": note}
            evidence_json = json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            conn.execute(
                "INSERT INTO event_relationship_decisions ("
                "decision_id, relationship_id, subject_event_id, object_event_id, "
                "subject_revision, object_revision, relation_type, decision, evidence_json, "
                "supersedes_decision_id, note, decided_at_utc"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'retracted', ?, ?, ?, ?)",
                (
                    decision_id,
                    relationship_id,
                    edge["subject_event_id"],
                    edge["object_event_id"],
                    edge["subject_revision"],
                    edge["object_revision"],
                    edge["relation_type"],
                    evidence_json,
                    edge["accepted_decision_id"],
                    note,
                    timestamp,
                ),
            )
            conn.execute(
                "DELETE FROM event_relationships WHERE relationship_id=?",
                (relationship_id,),
            )
            return {
                "status": "retracted",
                "decision_id": decision_id,
                "relationship_id": relationship_id,
                "database_rows_modified": 2,
                "source_events_modified": 0,
            }
    finally:
        conn.close()


def list_relationships(
    database_path: str | Path,
    *,
    subject_event_id: str | None = None,
    object_event_id: str | None = None,
    relation_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List accepted semantic edges and flag source-revision staleness."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if relation_type is not None:
        _validate_relation_type(relation_type)
    conn = _open(database_path)
    try:
        clauses: list[str] = []
        params: list[object] = []
        if subject_event_id:
            clauses.append("r.subject_event_id=?")
            params.append(subject_event_id)
        if object_event_id:
            clauses.append("r.object_event_id=?")
            params.append(object_event_id)
        if relation_type:
            clauses.append("r.relation_type=?")
            params.append(relation_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT r.*, s.revision AS subject_current_revision, "
            "o.revision AS object_current_revision, "
            "s.payload_json AS subject_payload_json, o.payload_json AS object_payload_json "
            "FROM event_relationships r "
            "JOIN events s ON s.event_id=r.subject_event_id "
            "JOIN events o ON o.event_id=r.object_event_id"
            + where
            + " ORDER BY r.created_at_utc, r.relationship_id LIMIT ?",
            (*params, limit),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "relationship_id": row["relationship_id"],
                "relation_type": row["relation_type"],
                "subject_event_id": row["subject_event_id"],
                "object_event_id": row["object_event_id"],
                "subject_revision": row["subject_revision"],
                "object_revision": row["object_revision"],
                "subject_current_revision": row["subject_current_revision"],
                "object_current_revision": row["object_current_revision"],
                "stale": (
                    row["subject_revision"] != row["subject_current_revision"]
                    or row["object_revision"] != row["object_current_revision"]
                ),
                "subject": json.loads(row["subject_payload_json"]),
                "object": json.loads(row["object_payload_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "accepted_decision_id": row["accepted_decision_id"],
                "created_at_utc": row["created_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
            })
        return result
    finally:
        conn.close()


def relationship_history(
    database_path: str | Path,
    *,
    relationship_id: str | None = None,
    event_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return append-only review history for one relationship or source event."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if relationship_id and event_id:
        raise ValueError("filter by relationship_id or event_id, not both")
    conn = _open(database_path)
    try:
        if relationship_id:
            rows = conn.execute(
                "SELECT * FROM event_relationship_decisions WHERE relationship_id=? "
                "ORDER BY rowid DESC LIMIT ?",
                (relationship_id, limit),
            ).fetchall()
        elif event_id:
            rows = conn.execute(
                "SELECT * FROM event_relationship_decisions "
                "WHERE subject_event_id=? OR object_event_id=? "
                "ORDER BY rowid DESC LIMIT ?",
                (event_id, event_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM event_relationship_decisions "
                "ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "decision_id": row["decision_id"],
                "relationship_id": row["relationship_id"],
                "subject_event_id": row["subject_event_id"],
                "object_event_id": row["object_event_id"],
                "subject_revision": row["subject_revision"],
                "object_revision": row["object_revision"],
                "relation_type": row["relation_type"],
                "decision": row["decision"],
                "evidence": json.loads(row["evidence_json"]),
                "supersedes_decision_id": row["supersedes_decision_id"],
                "note": row["note"],
                "decided_at_utc": row["decided_at_utc"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def relationship_status(database_path: str | Path) -> dict:
    """Summarize current graph edges, stale snapshots and immutable review actions."""
    conn = _open(database_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM event_relationships").fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM event_relationships r "
            "JOIN events s ON s.event_id=r.subject_event_id "
            "JOIN events o ON o.event_id=r.object_event_id "
            "WHERE r.subject_revision<>s.revision OR r.object_revision<>o.revision"
        ).fetchone()[0]
        by_type = conn.execute(
            "SELECT relation_type, COUNT(*) AS count FROM event_relationships "
            "GROUP BY relation_type ORDER BY relation_type"
        ).fetchall()
        decisions = conn.execute(
            "SELECT decision, COUNT(*) AS count FROM event_relationship_decisions "
            "GROUP BY decision ORDER BY decision"
        ).fetchall()
        return {
            "database_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "relationships": total,
            "stale_relationships": stale,
            "relationships_by_type": {row["relation_type"]: row["count"] for row in by_type},
            "decisions_total": sum(row["count"] for row in decisions),
            "decisions_by_status": {row["decision"]: row["count"] for row in decisions},
            "same_event_uses_canonical_reconciliation": True,
            "automatic_relationship_creation": False,
        }
    finally:
        conn.close()
