"""Read-only transaction-time views of append-only relationship decisions.

A historical view uses the decision timestamp and the latest source revision
observed by that timestamp. It never treats the event's scheduled dates as
decision or observation timestamps.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .relationship_evaluation import _open_readonly

_NODE_FIELDS = (
    "title",
    "provider",
    "project",
    "ticker",
    "event_type",
    "category",
    "location",
    "start_date",
    "end_date",
    "source_url",
)


def _parse_utc(value: str, *, field: str) -> datetime:
    """Validate an offset-aware ISO timestamp and normalize it to UTC."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an offset-aware ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an offset-aware ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset or Z")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _limit(value: int) -> None:
    if not 1 <= value <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _read_history(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT rowid AS sequence, decision_id, relationship_id,
               subject_event_id, object_event_id,
               subject_revision, object_revision, relation_type,
               decision, supersedes_decision_id, decided_at_utc
        FROM event_relationship_decisions
        ORDER BY rowid
        """
    ).fetchall()
    history = []
    for row in rows:
        action = dict(row)
        action["_instant"] = _parse_utc(
            action["decided_at_utc"], field="decided_at_utc"
        )
        history.append(action)
    history.sort(key=lambda item: (item["_instant"], item["sequence"]))
    return history


def relationship_timeline(
    database_path: str | Path,
    *,
    relationship_id: str | None = None,
    event_id: str | None = None,
    limit: int = 100,
) -> dict:
    """Return chronological review actions, including retractions and revisions."""
    _limit(limit)
    if relationship_id and event_id:
        raise ValueError("filter by relationship_id or event_id, not both")

    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        history = _read_history(conn)
    finally:
        conn.close()

    if relationship_id:
        history = [
            item for item in history
            if item["relationship_id"] == relationship_id
        ]
    elif event_id:
        history = [
            item for item in history
            if event_id in (
                item["subject_event_id"], item["object_event_id"]
            )
        ]

    total = len(history)
    selected = history[-limit:]
    actions = []
    for item in selected:
        actions.append(
            {
                "sequence": item["sequence"],
                "decision_id": item["decision_id"],
                "relationship_id": item["relationship_id"],
                "subject_event_id": item["subject_event_id"],
                "object_event_id": item["object_event_id"],
                "subject_revision": item["subject_revision"],
                "object_revision": item["object_revision"],
                "relation_type": item["relation_type"],
                "decision": item["decision"],
                "supersedes_decision_id": item["supersedes_decision_id"],
                "decided_at_utc": _utc_text(item["_instant"]),
            }
        )

    return {
        "status": "complete" if total <= limit else "truncated",
        "mode": "read_only",
        "database_version": version,
        "scope": "append_only_relationship_decision_actions",
        "timeline_order": "chronological_latest",
        "relationship_id_filter": relationship_id,
        "event_id_filter": event_id,
        "actions_total": total,
        "actions_returned": len(actions),
        "actions": actions,
        "source_requests": 0,
        "database_rows_modified": 0,
        "canonical_identity_modified": False,
        "limitations": [
            "Action timestamps record when decisions were stored, "
            "not when the scheduled events occurred.",
            "Historical results depend on the integrity of stored decision timestamps.",
            "Reviewer notes and evidence bodies are not exported.",
        ],
    }


def _as_of_revisions(conn, instant: datetime) -> dict[str, dict]:
    """Choose the highest observed revision available at the requested instant."""
    rows = conn.execute(
        """
        SELECT event_id, revision, observed_at_utc, payload_json
        FROM event_revisions ORDER BY event_id, revision
        """
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        observed = _parse_utc(
            row["observed_at_utc"], field="observed_at_utc"
        )
        if observed > instant:
            continue
        event_id = row["event_id"]
        current = latest.get(event_id)
        if current is not None and row["revision"] <= current["revision"]:
            continue
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError(f"invalid event revision payload: {event_id}")
        if payload.get("event_id") != event_id:
            raise ValueError(
                f"stored event identity disagrees with revision provenance: {event_id}"
            )
        latest[event_id] = {
            "event_id": event_id,
            "revision": row["revision"],
            "observed_at_utc": _utc_text(observed),
            "snapshot": {
                "event_id": event_id,
                "revision": row["revision"],
                **{field: payload.get(field) for field in _NODE_FIELDS},
            },
        }
    return latest


def relationship_as_of(
    database_path: str | Path,
    *,
    at_utc: str,
    event_id: str | None = None,
    limit: int = 100,
) -> dict:
    """Reconstruct accepted edges known at an instant, without future revisions.

    This is an observation/decision-time reconstruction, not a full historical
    SQL transaction snapshot. Scheduled event dates do not affect edge state.
    """
    _limit(limit)
    instant = _parse_utc(at_utc, field="at_utc")
    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        history = _read_history(conn)
        latest_revisions = _as_of_revisions(conn, instant)
    finally:
        conn.close()

    actions = [item for item in history if item["_instant"] <= instant]
    latest_decisions: dict[str, dict] = {}
    for action in actions:
        latest_decisions[action["relationship_id"]] = action

    latest_status_counts = Counter()
    active: list[dict] = []
    stale = 0
    unavailable_snapshots = 0
    for action in latest_decisions.values():
        if event_id and event_id not in (
            action["subject_event_id"], action["object_event_id"]
        ):
            continue
        latest_status_counts[action["decision"]] += 1
        if action["decision"] != "accepted":
            continue

        subject = latest_revisions.get(action["subject_event_id"])
        object_event = latest_revisions.get(action["object_event_id"])
        if subject is None or object_event is None:
            unavailable_snapshots += 1
            continue
        if (
            action["subject_revision"] != subject["revision"]
            or action["object_revision"] != object_event["revision"]
        ):
            stale += 1
            continue

        active.append(
            {
                "relationship_id": action["relationship_id"],
                "accepted_decision_id": action["decision_id"],
                "subject_event_id": action["subject_event_id"],
                "object_event_id": action["object_event_id"],
                "subject_revision": action["subject_revision"],
                "object_revision": action["object_revision"],
                "relation_type": action["relation_type"],
                "decided_at_utc": _utc_text(action["_instant"]),
                "subject": subject["snapshot"],
                "object": object_event["snapshot"],
            }
        )

    active.sort(
        key=lambda item: (
            item["subject_event_id"],
            item["object_event_id"],
            item["relation_type"],
        )
    )
    return {
        "status": "reconstructed",
        "mode": "read_only",
        "database_version": version,
        "scope": "historical_decision_and_source_observation_time",
        "at_utc": _utc_text(instant),
        "inclusive_cutoff": True,
        "event_id_filter": event_id,
        "decision_actions_considered": len(actions),
        "latest_relationship_decisions_total": sum(latest_status_counts.values()),
        "latest_decision_counts": dict(sorted(latest_status_counts.items())),
        "active_edges_total": len(active),
        "active_edges": active[:limit],
        "active_edges_returned": min(limit, len(active)),
        "active_edges_truncated": len(active) > limit,
        "stale_accepted_edges_excluded": stale,
        "accepted_edges_without_observed_source_snapshots": unavailable_snapshots,
        "source_requests": 0,
        "database_rows_modified": 0,
        "canonical_identity_modified": False,
        "limitations": [
            "As-of time refers to recorded decision and source observation times.",
            "Event start/end dates describe scheduled events, not edge validity.",
            "Source revisions first observed after the cutoff are never used.",
            "This is not a historical database transaction snapshot; backfilled "
            "or inaccurate observation timestamps may limit reconstruction.",
            "No semantic relationships are inferred from indirect graph paths.",
        ],
    }
