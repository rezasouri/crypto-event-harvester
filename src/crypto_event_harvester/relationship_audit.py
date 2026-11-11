"""Read-only integrity and quality audit for the semantic relationship graph."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .relationship_evaluation import _open_readonly
from .relationships import RELATION_TYPES, relationship_id_for

_REQUIRED_TRIGGERS = (
    "trg_relationship_decision_transition",
    "trg_relationship_retraction_matches_edge",
    "trg_relationship_decisions_no_update",
    "trg_relationship_decisions_no_delete",
    "trg_relationship_edge_insert_requires_decision",
    "trg_relationship_edge_update_requires_decision",
    "trg_relationship_decision_projects_accepted_edge",
    "trg_relationship_decision_projects_retraction",
    "trg_relationship_edge_delete_requires_retraction",
)


def _bounded(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _json_object(value: object) -> dict | None:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _finding(
    findings: list[dict],
    *,
    code: str,
    severity: str,
    entity_type: str,
    entity_id: str,
    details: str,
) -> None:
    findings.append(
        {
            "code": code,
            "severity": severity,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": details,
        }
    )


def audit_relationship_graph(
    database_path: str | Path,
    *,
    limit: int = 100,
) -> dict:
    """Audit decision history and its accepted-edge projection without writes."""
    _bounded(limit)
    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity = [row[0] for row in integrity_rows]
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        trigger_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }

        event_rows = conn.execute(
            """
            SELECT event_id, provider, revision, payload_json, payload_sha256
            FROM events ORDER BY event_id
            """
        ).fetchall()
        revision_rows = conn.execute(
            """
            SELECT event_id, revision, observed_at_utc, payload_json, payload_sha256
            FROM event_revisions ORDER BY event_id, revision
            """
        ).fetchall()
        decision_rows = conn.execute(
            """
            SELECT rowid AS sequence, decision_id, relationship_id,
                   subject_event_id, object_event_id,
                   subject_revision, object_revision, relation_type,
                   decision, evidence_json, supersedes_decision_id,
                   decided_at_utc
            FROM event_relationship_decisions ORDER BY rowid
            """
        ).fetchall()
        edge_rows = conn.execute(
            """
            SELECT relationship_id, subject_event_id, object_event_id,
                   relation_type, subject_revision, object_revision,
                   evidence_json, accepted_decision_id,
                   created_at_utc, updated_at_utc
            FROM event_relationships ORDER BY relationship_id
            """
        ).fetchall()
    finally:
        conn.close()

    findings: list[dict] = []
    events: dict[str, dict] = {}
    revisions: dict[tuple[str, int], dict] = {}
    decisions: list[dict] = [dict(row) for row in decision_rows]
    edges: list[dict] = [dict(row) for row in edge_rows]

    for row in event_rows:
        event_id = row["event_id"]
        payload = _json_object(row["payload_json"])
        if payload is None:
            _finding(
                findings, code="invalid_current_event_payload", severity="error",
                entity_type="event", entity_id=event_id,
                details="Current event payload is not a JSON object.",
            )
            continue
        digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            _finding(
                findings, code="current_event_hash_mismatch", severity="error",
                entity_type="event", entity_id=event_id,
                details="Current payload SHA-256 does not match the stored digest.",
            )
        if payload.get("event_id") != event_id or payload.get("provider") != row["provider"]:
            _finding(
                findings, code="current_event_identity_mismatch", severity="error",
                entity_type="event", entity_id=event_id,
                details="Payload event ID/provider disagrees with the current row.",
            )
        events[event_id] = {
            "provider": row["provider"],
            "revision": row["revision"],
            "payload": payload,
            "payload_sha256": row["payload_sha256"],
        }

    for row in revision_rows:
        key = (row["event_id"], row["revision"])
        payload = _json_object(row["payload_json"])
        if payload is None:
            _finding(
                findings, code="invalid_revision_payload", severity="error",
                entity_type="source_revision", entity_id=f"{key[0]}@{key[1]}",
                details="Historical source payload is not a JSON object.",
            )
            continue
        digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            _finding(
                findings, code="revision_hash_mismatch", severity="error",
                entity_type="source_revision", entity_id=f"{key[0]}@{key[1]}",
                details="Historical payload SHA-256 does not match the stored digest.",
            )
        if payload.get("event_id") != row["event_id"]:
            _finding(
                findings, code="revision_identity_mismatch", severity="error",
                entity_type="source_revision", entity_id=f"{key[0]}@{key[1]}",
                details="Historical payload event ID disagrees with its revision row.",
            )
        revisions[key] = {
            "payload": payload,
            "payload_sha256": row["payload_sha256"],
            "observed_at_utc": row["observed_at_utc"],
        }

    for event_id, current in events.items():
        current_revision = revisions.get((event_id, current["revision"]))
        if current_revision is None:
            _finding(
                findings, code="current_revision_missing", severity="error",
                entity_type="event", entity_id=event_id,
                details="Current event revision has no matching immutable revision row.",
            )
            continue
        if current_revision["payload_sha256"] != current["payload_sha256"]:
            _finding(
                findings, code="current_revision_payload_mismatch", severity="error",
                entity_type="event", entity_id=event_id,
                details="Current event payload differs from its matching revision snapshot.",
            )
        if current_revision["payload"].get("provider") != current["provider"]:
            _finding(
                findings, code="current_revision_provider_mismatch", severity="error",
                entity_type="event", entity_id=event_id,
                details="Current revision provider disagrees with the events table.",
            )

    decision_by_id = {item["decision_id"]: item for item in decisions}
    tuple_history: dict[tuple, list[dict]] = defaultdict(list)
    latest_by_tuple: dict[tuple, dict] = {}
    decision_counts = Counter()
    valid_relation_types = set(RELATION_TYPES)

    for item in decisions:
        decision_id = item["decision_id"]
        relation_id = item["relationship_id"]
        relation_type = item["relation_type"]
        decision_counts[item["decision"]] += 1
        key = (
            relation_id,
            item["subject_revision"],
            item["object_revision"],
            relation_type,
        )
        tuple_history[key].append(item)

        try:
            expected_id = relationship_id_for(
                item["subject_event_id"], item["object_event_id"], relation_type
            )
        except (AttributeError, TypeError, ValueError):
            expected_id = None
        if expected_id != relation_id:
            _finding(
                findings, code="decision_relationship_id_mismatch", severity="error",
                entity_type="decision", entity_id=decision_id,
                details="Relationship ID does not match its directed endpoints and predicate.",
            )
        if relation_type not in valid_relation_types:
            _finding(
                findings, code="unknown_relation_type", severity="error",
                entity_type="decision", entity_id=decision_id,
                details="Decision uses a predicate outside the supported relation vocabulary.",
            )
        if item["subject_event_id"] == item["object_event_id"]:
            _finding(
                findings, code="self_relationship", severity="error",
                entity_type="decision", entity_id=decision_id,
                details="A semantic relationship cannot connect an event to itself.",
            )
        for side in ("subject", "object"):
            event_id = item[f"{side}_event_id"]
            revision = item[f"{side}_revision"]
            snapshot = revisions.get((event_id, revision))
            if snapshot is None:
                _finding(
                    findings, code="decision_source_revision_missing", severity="error",
                    entity_type="decision", entity_id=decision_id,
                    details=f"{side} source revision {event_id}@{revision} is unavailable.",
                )

        evidence = _json_object(item["evidence_json"])
        if evidence is None:
            _finding(
                findings, code="invalid_decision_evidence", severity="error",
                entity_type="decision", entity_id=decision_id,
                details="Decision evidence is not a JSON object.",
            )
        else:
            for side in ("subject", "object"):
                event_id = item[f"{side}_event_id"]
                revision = item[f"{side}_revision"]
                revision_record = revisions.get((event_id, revision))
                snapshot = evidence.get(side)
                expected_payload = revision_record["payload"] if revision_record else None
                expected_provider = (
                    expected_payload.get("provider") if expected_payload else None
                )
                expected_url = (
                    expected_payload.get("source_url") if expected_payload else None
                )
                consistent = (
                    isinstance(snapshot, dict)
                    and snapshot.get("event_id") == event_id
                    and snapshot.get("revision") == revision
                    and snapshot.get("provider") == expected_provider
                    and snapshot.get("source_url") == expected_url
                )
                if not consistent:
                    _finding(
                        findings, code="decision_evidence_snapshot_mismatch",
                        severity="warning", entity_type="decision",
                        entity_id=decision_id,
                        details=f"{side} evidence snapshot does not match its immutable source revision.",
                    )

    for key, history in tuple_history.items():
        previous: dict | None = None
        for item in history:
            decision = item["decision"]
            if decision in {"accepted", "rejected"}:
                if previous is not None and previous["decision"] in {
                    "accepted", "rejected"
                }:
                    _finding(
                        findings, code="duplicate_or_conflicting_review_action",
                        severity="error", entity_type="decision",
                        entity_id=item["decision_id"],
                        details="A new review action follows an accepted/rejected action without a retraction.",
                    )
            elif decision == "retracted":
                superseded = decision_by_id.get(item["supersedes_decision_id"])
                valid_supersession = (
                    previous is not None
                    and previous["decision"] == "accepted"
                    and previous["decision_id"] == item["supersedes_decision_id"]
                    and superseded is not None
                    and superseded["sequence"] < item["sequence"]
                    and superseded["relationship_id"] == item["relationship_id"]
                    and superseded["subject_event_id"] == item["subject_event_id"]
                    and superseded["object_event_id"] == item["object_event_id"]
                    and superseded["subject_revision"] == item["subject_revision"]
                    and superseded["object_revision"] == item["object_revision"]
                    and superseded["relation_type"] == item["relation_type"]
                )
                if not valid_supersession:
                    _finding(
                        findings, code="invalid_retraction_chain", severity="error",
                        entity_type="decision", entity_id=item["decision_id"],
                        details="Retraction does not supersede the immediately preceding accepted decision for this revision tuple.",
                    )
            previous = item
        latest_by_tuple[key] = history[-1]

    current_edges = 0
    stale_edges = 0
    edge_by_tuple: dict[tuple, dict] = {}
    relation_counts = Counter()
    provider_pairs = Counter()
    pair_predicates: dict[tuple[str, str], set[str]] = defaultdict(set)
    directed_pairs: set[tuple[str, str, str]] = set()

    for edge in edges:
        edge_id = edge["relationship_id"]
        relation_type = edge["relation_type"]
        relation_counts[relation_type] += 1
        pair_predicates[(edge["subject_event_id"], edge["object_event_id"])].add(
            relation_type
        )
        directed_pairs.add(
            (edge["subject_event_id"], edge["object_event_id"], relation_type)
        )
        subject = events.get(edge["subject_event_id"])
        object_event = events.get(edge["object_event_id"])
        if subject and object_event:
            provider_pairs[f'{subject["provider"]}->{object_event["provider"]}'] += 1

        try:
            expected_id = relationship_id_for(
                edge["subject_event_id"], edge["object_event_id"], relation_type
            )
        except (AttributeError, TypeError, ValueError):
            expected_id = None
        if expected_id != edge_id:
            _finding(
                findings, code="edge_relationship_id_mismatch", severity="error",
                entity_type="edge", entity_id=edge_id,
                details="Edge ID does not match its directed endpoints and predicate.",
            )

        pointer = decision_by_id.get(edge["accepted_decision_id"])
        if pointer is None:
            _finding(
                findings, code="edge_decision_missing", severity="error",
                entity_type="edge", entity_id=edge_id,
                details="Accepted edge points to a missing review decision.",
            )
        else:
            expected_fields = (
                "relationship_id", "subject_event_id", "object_event_id",
                "subject_revision", "object_revision", "relation_type",
            )
            mismatched = [
                field for field in expected_fields
                if pointer[field] != edge[field]
            ]
            if pointer["decision"] != "accepted" or mismatched:
                _finding(
                    findings, code="edge_decision_projection_mismatch",
                    severity="error", entity_type="edge", entity_id=edge_id,
                    details="Accepted edge does not match its referenced accepted decision.",
                )
            tuple_key = (
                edge_id, edge["subject_revision"], edge["object_revision"],
                relation_type,
            )
            latest = latest_by_tuple.get(tuple_key)
            if (
                latest is None
                or latest["decision"] != "accepted"
                or latest["decision_id"] != edge["accepted_decision_id"]
            ):
                _finding(
                    findings, code="edge_not_latest_accepted_decision",
                    severity="error", entity_type="edge", entity_id=edge_id,
                    details="Edge does not point to the latest accepted action for its exact source revisions.",
                )
            if edge["evidence_json"] != pointer["evidence_json"]:
                _finding(
                    findings, code="edge_evidence_projection_mismatch",
                    severity="error", entity_type="edge", entity_id=edge_id,
                    details="Edge evidence differs from the evidence captured by its accepted decision.",
                )

        subject_revision = (
            subject["revision"] if subject is not None else None
        )
        object_revision = (
            object_event["revision"] if object_event is not None else None
        )
        if (
            subject_revision is None
            or object_revision is None
            or edge["subject_revision"] != subject_revision
            or edge["object_revision"] != object_revision
        ):
            stale_edges += 1
            _finding(
                findings, code="stale_accepted_edge", severity="warning",
                entity_type="edge", entity_id=edge_id,
                details="Accepted edge references source revisions older than the current event snapshots.",
            )
        else:
            current_edges += 1

        edge_by_tuple[
            (
                edge_id, edge["subject_revision"],
                edge["object_revision"], relation_type,
            )
        ] = edge

    for key, latest in latest_by_tuple.items():
        if latest["decision"] != "accepted":
            continue
        relationship_id, subject_revision, object_revision, relation_type = key
        subject = events.get(latest["subject_event_id"])
        object_event = events.get(latest["object_event_id"])
        if (
            subject is None or object_event is None
            or subject["revision"] != subject_revision
            or object_event["revision"] != object_revision
        ):
            continue
        edge = edge_by_tuple.get(key)
        if edge is None or edge["accepted_decision_id"] != latest["decision_id"]:
            _finding(
                findings, code="current_acceptance_missing_from_projection",
                severity="error", entity_type="decision",
                entity_id=latest["decision_id"],
                details="Latest accepted decision for current source revisions has no matching active graph edge.",
            )

    for (subject_id, object_id), predicates in pair_predicates.items():
        if len(predicates) > 1:
            _finding(
                findings, code="multiple_predicates_for_pair", severity="info",
                entity_type="event_pair", entity_id=f"{subject_id}->{object_id}",
                details="The directed event pair has multiple accepted predicates; this may be valid multi-faceted participation.",
            )

    reciprocal_pairs = 0
    for subject_id, object_id, relation_type in sorted(directed_pairs):
        if subject_id >= object_id:
            continue
        if (object_id, subject_id, relation_type) not in directed_pairs:
            continue
        reciprocal_pairs += 1
        severity = "info" if relation_type == "related_to" else "warning"
        code = (
            "reciprocal_symmetric_related_to"
            if relation_type == "related_to"
            else "reciprocal_directed_predicate"
        )
        _finding(
            findings, code=code, severity=severity,
            entity_type="event_pair",
            entity_id=f"{subject_id}<->{object_id}:{relation_type}",
            details=(
                "Both directions of the same predicate are accepted. "
                "This is a review signal, not proof of a contradiction."
            ),
        )

    missing_triggers = sorted(set(_REQUIRED_TRIGGERS) - trigger_names)
    for trigger in missing_triggers:
        _finding(
            findings, code="required_guard_trigger_missing", severity="error",
            entity_type="schema_trigger", entity_id=trigger,
            details="A required append-only or relationship-projection guard trigger is missing.",
        )

    foreign_key_violations = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "foreign_key_index": row[3],
        }
        for row in foreign_key_rows
    ]
    if foreign_key_violations:
        _finding(
            findings, code="foreign_key_violations", severity="error",
            entity_type="database", entity_id="sqlite",
            details=f"{len(foreign_key_violations)} foreign-key violation(s) were detected.",
        )
    if integrity != ["ok"]:
        _finding(
            findings, code="sqlite_integrity_failure", severity="error",
            entity_type="database", entity_id="sqlite",
            details="SQLite integrity_check did not return exactly 'ok'.",
        )

    findings.sort(
        key=lambda item: (
            {"error": 0, "warning": 1, "info": 2}[item["severity"]],
            item["code"],
            item["entity_id"],
        )
    )
    severity_counts = Counter(item["severity"] for item in findings)
    code_counts = Counter(item["code"] for item in findings)
    selected = findings[:limit]
    return {
        "status": "inconsistent" if severity_counts["error"] else "consistent",
        "review_status": "review_required" if severity_counts["warning"] else "clear",
        "mode": "read_only",
        "database_version": version,
        "scope": "relationship_decision_history_and_active_edge_projection",
        "events_total": len(events),
        "source_revisions_total": len(revisions),
        "decisions_total": len(decisions),
        "decisions_by_status": dict(sorted(decision_counts.items())),
        "accepted_edges_total": len(edges),
        "current_edges_total": current_edges,
        "stale_edges_total": stale_edges,
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "provider_pair_counts": dict(sorted(provider_pairs.items())),
        "multiple_predicate_pairs": sum(
            len(predicates) > 1 for predicates in pair_predicates.values()
        ),
        "reciprocal_same_predicate_pairs": reciprocal_pairs,
        "required_guard_triggers": len(_REQUIRED_TRIGGERS),
        "missing_guard_triggers": missing_triggers,
        "sqlite_integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_key_violations),
        "foreign_key_violations": foreign_key_violations[:limit],
        "findings_total": len(findings),
        "findings_by_severity": {
            severity: severity_counts.get(severity, 0)
            for severity in ("error", "warning", "info")
        },
        "findings_by_code": dict(sorted(code_counts.items())),
        "findings_returned": len(selected),
        "findings_truncated": len(findings) > limit,
        "findings": selected,
        "interpretation": (
            "Structural errors indicate disagreement between append-only decisions, "
            "source revision provenance and the active edge projection. Stale edges "
            "and reciprocal predicates are review signals; reciprocal predicates "
            "are not automatically classified as contradictions."
        ),
        "source_requests": 0,
        "database_rows_modified": 0,
        "canonical_identity_modified": False,
        "automatic_relationship_creation": False,
    }
