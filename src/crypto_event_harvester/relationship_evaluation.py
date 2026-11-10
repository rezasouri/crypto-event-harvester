"""Retrospective quality evaluation for human-reviewed semantic relationships.

The deterministic candidate score is recomputed against immutable source
revisions. It is ranking evidence, not a probability or calibrated confidence.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .cross_provider_candidates import _date_overlap
from .relationship_candidates import (
    _candidate,
    _extract_relation_cues,
    _is_conference,
    _target_match,
)
from .storage import DB_VERSION

DEFAULT_THRESHOLDS = (0.60, 0.70, 0.80, 0.90)
_EVALUATION_SCOPE = "latest_human_labels_per_relationship_and_source_revision"


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _metrics(labels: list[bool], predictions: list[bool]) -> dict:
    paired = list(zip(labels, predictions, strict=True))
    true_positive = sum(label and prediction for label, prediction in paired)
    false_positive = sum(not label and prediction for label, prediction in paired)
    true_negative = sum(not label and not prediction for label, prediction in paired)
    false_negative = sum(label and not prediction for label, prediction in paired)
    total = len(labels)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = (
        _ratio(
            2 * true_positive,
            2 * true_positive + false_positive + false_negative,
        )
        if true_positive or false_positive or false_negative
        else None
    )
    return {
        "sample_size": total,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "specificity": _ratio(true_negative, true_negative + false_positive),
        "accuracy": _ratio(true_positive + true_negative, total),
    }


def _open_readonly(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < DB_VERSION:
        conn.close()
        raise ValueError(
            f"relationship evaluation requires database schema version "
            f"{DB_VERSION} or newer; found {version}. This command never migrates databases."
        )
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "events",
        "event_revisions",
        "event_relationship_decisions",
        "event_relationships",
    }
    missing = required - tables
    if missing:
        conn.close()
        raise ValueError(
            "database is missing relationship evaluation tables: "
            + ", ".join(sorted(missing))
        )
    return conn


def _valid_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _has_text(mapping: dict, keys: tuple[str, ...]) -> bool:
    return any(
        isinstance(mapping.get(key), str) and bool(mapping[key].strip())
        for key in keys
    )


def _evidence_quality(
    row: sqlite3.Row,
    subject: dict,
    object_event: dict,
) -> tuple[dict, list[str]]:
    issues: list[str] = []
    try:
        evidence = json.loads(row["evidence_json"])
    except (TypeError, json.JSONDecodeError):
        evidence = {}
        issues.append("invalid_evidence_json")
    if not isinstance(evidence, dict):
        evidence = {}
        issues.append("evidence_not_object")

    snapshot_checks = []
    for name, expected in (("subject", subject), ("object", object_event)):
        snapshot = evidence.get(name)
        consistent = (
            isinstance(snapshot, dict)
            and snapshot.get("event_id") == expected.get("event_id")
            and snapshot.get("provider") == expected.get("provider")
            and snapshot.get("revision") == row[f"{name}_revision"]
            and snapshot.get("source_url") == expected.get("source_url")
        )
        snapshot_checks.append(consistent)
    snapshot_consistent = all(snapshot_checks)
    if not snapshot_consistent:
        issues.append("evidence_snapshot_mismatch")

    subject_url = _valid_http_url(subject.get("source_url"))
    object_url = _valid_http_url(object_event.get("source_url"))
    if not subject_url:
        issues.append("missing_subject_source_url")
    if not object_url:
        issues.append("missing_object_source_url")

    reviewer_evidence = evidence.get("reviewer_evidence")
    if not isinstance(reviewer_evidence, dict):
        reviewer_evidence = {}
    basis_present = _has_text(
        reviewer_evidence, ("basis", "rationale", "claim")
    )
    review_marker_present = _has_text(
        reviewer_evidence, ("review", "reviewer", "reviewed_by")
    )
    note_present = (
        isinstance(row["note"], str) and bool(row["note"].strip())
    )
    if not basis_present:
        issues.append("missing_reviewer_basis")
    if not review_marker_present:
        issues.append("missing_review_marker")
    if not note_present:
        issues.append("missing_reviewer_note")

    if (
        snapshot_consistent
        and subject_url
        and object_url
        and basis_present
        and review_marker_present
        and note_present
    ):
        grade = "complete"
    elif snapshot_consistent and basis_present and (subject_url or object_url):
        grade = "partial"
    else:
        grade = "insufficient"

    return (
        {
            "grade": grade,
            "snapshot_consistent": snapshot_consistent,
            "subject_source_url_present": subject_url,
            "object_source_url_present": object_url,
            "reviewer_basis_present": basis_present,
            "review_marker_present": review_marker_present,
            "reviewer_note_present": note_present,
        },
        issues,
    )


def _candidate_score(
    subject: dict,
    subject_revision: int,
    object_event: dict,
    object_revision: int,
    relation_type: str,
) -> dict | None:
    """Recompute the current deterministic candidate evidence for one old revision pair."""
    if (
        subject.get("date_precision") not in {"day", "range"}
        or object_event.get("date_precision") not in {"day", "range"}
        or _is_conference(subject)
        or not _is_conference(object_event)
    ):
        return None
    date_evidence = _date_overlap(subject, object_event)
    if date_evidence is None:
        return None

    best_by_field: dict[str, dict] = {}
    for cue in _extract_relation_cues(subject):
        if cue["relation_type"] != relation_type:
            continue
        target = _target_match(object_event, cue)
        if target is None:
            continue
        candidate = _candidate(
            subject,
            subject_revision,
            object_event,
            object_revision,
            cue,
            target,
            date_evidence,
            0.0,
            None,
        )
        if candidate is None:
            continue
        previous = best_by_field.get(cue["field"])
        if previous is None or candidate["score"] > previous["score"]:
            best_by_field[cue["field"]] = candidate

    for field in ("description", "title", "source_url"):
        if field in best_by_field:
            candidate = best_by_field[field]
            return {
                "score": candidate["score"],
                "score_kind": candidate["score_kind"],
                "score_components": candidate["score_components"],
                "cue_field": candidate["evidence"]["relation_cue"]["field"],
                "date_relation": candidate["evidence"]["date_relation"],
            }
    return None


def evaluate_relationship_quality(
    database_path: str | Path,
    *,
    thresholds: list[float] | tuple[float, ...] = DEFAULT_THRESHOLDS,
    min_reviewed_pairs: int = 30,
    limit: int = 100,
) -> dict:
    """Evaluate relationship review evidence and retrospective candidate ranking.

    Metrics use the latest accepted/rejected label for each exact relationship
    and source-revision tuple. Retractions are lifecycle actions, not negative
    labels. All reads are query-only; no schema migration is performed.
    """
    if min_reviewed_pairs < 1:
        raise ValueError("min_reviewed_pairs must be at least 1")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    normalized_thresholds = sorted({float(value) for value in thresholds})
    if not normalized_thresholds:
        raise ValueError("at least one threshold is required")
    if any(
        not math.isfinite(value) or value < 0.0 or value > 1.0
        for value in normalized_thresholds
    ):
        raise ValueError("thresholds must be finite values between 0 and 1")

    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute(
            """
            SELECT
                d.rowid AS sequence,
                d.decision_id,
                d.relationship_id,
                d.subject_event_id,
                d.object_event_id,
                d.subject_revision,
                d.object_revision,
                d.relation_type,
                d.decision,
                d.evidence_json,
                d.supersedes_decision_id,
                d.note,
                d.decided_at_utc,
                sr.payload_json AS subject_payload_json,
                orv.payload_json AS object_payload_json,
                se.revision AS subject_current_revision,
                oe.revision AS object_current_revision
            FROM event_relationship_decisions AS d
            JOIN event_revisions AS sr
              ON sr.event_id = d.subject_event_id
             AND sr.revision = d.subject_revision
            JOIN event_revisions AS orv
              ON orv.event_id = d.object_event_id
             AND orv.revision = d.object_revision
            LEFT JOIN events AS se ON se.event_id = d.subject_event_id
            LEFT JOIN events AS oe ON oe.event_id = d.object_event_id
            ORDER BY d.rowid
            """
        ).fetchall()
        edge_rows = conn.execute(
            """
            SELECT
                relationship_id,
                subject_event_id,
                object_event_id,
                subject_revision,
                object_revision,
                relation_type,
                accepted_decision_id
            FROM event_relationships
            """
        ).fetchall()
    finally:
        conn.close()

    decisions_by_status = Counter(row["decision"] for row in rows)
    grouped: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = (
            row["relationship_id"],
            row["subject_event_id"],
            row["subject_revision"],
            row["object_event_id"],
            row["object_revision"],
            row["relation_type"],
        )
        grouped[key].append(row)

    edges = {row["relationship_id"]: row for row in edge_rows}
    reviewed: list[dict] = []
    audit_issue_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    score_by_label: dict[str, list[float]] = {
        "accepted": [],
        "rejected": [],
    }
    relation_counts: Counter[str] = Counter()
    provider_pair_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()

    for key, history in grouped.items():
        review_rows = [
            row for row in history if row["decision"] in {"accepted", "rejected"}
        ]
        if not review_rows:
            continue
        label_row = review_rows[-1]
        latest_action = history[-1]
        subject = json.loads(label_row["subject_payload_json"])
        object_event = json.loads(label_row["object_payload_json"])
        if not isinstance(subject, dict) or not isinstance(object_event, dict):
            raise ValueError("relationship decision revision payload must be a JSON object")
        if (
            subject.get("event_id") != label_row["subject_event_id"]
            or object_event.get("event_id") != label_row["object_event_id"]
        ):
            raise ValueError("relationship decision revision has inconsistent event identity")

        quality, issues = _evidence_quality(label_row, subject, object_event)
        edge = edges.get(label_row["relationship_id"])
        subject_current = label_row["subject_current_revision"]
        object_current = label_row["object_current_revision"]
        stale = (
            subject_current != label_row["subject_revision"]
            or object_current != label_row["object_revision"]
        )
        if stale:
            issues.append("stale_source_revision")

        edge_matches_revision = (
            edge is not None
            and edge["subject_revision"] == label_row["subject_revision"]
            and edge["object_revision"] == label_row["object_revision"]
        )
        if edge is not None and not edge_matches_revision:
            projection = "superseded_by_newer_revision"
        elif latest_action["decision"] == "accepted":
            if edge is None and not stale:
                issues.append("accepted_edge_missing")
                projection = "missing"
            elif edge_matches_revision and edge["accepted_decision_id"] != latest_action["decision_id"]:
                issues.append("active_edge_decision_mismatch")
                projection = "mismatch"
            elif stale:
                projection = "stale"
            else:
                projection = "active"
        elif latest_action["decision"] == "retracted":
            if edge_matches_revision:
                issues.append("retracted_edge_still_active")
                projection = "mismatch"
            else:
                projection = "retracted"
        else:
            if edge_matches_revision:
                issues.append("rejected_decision_has_active_edge")
                projection = "mismatch"
            else:
                projection = "rejected"

        candidate = _candidate_score(
            subject,
            label_row["subject_revision"],
            object_event,
            label_row["object_revision"],
            label_row["relation_type"],
        )
        if candidate is None:
            issues.append("no_current_candidate_signal")

        label = label_row["decision"]
        quality_counts[quality["grade"]] += 1
        relation_counts[label_row["relation_type"]] += 1
        providers = f'{subject.get("provider")}->{object_event.get("provider")}'
        provider_pair_counts[providers] += 1
        for name, value in quality.items():
            if name == "grade":
                continue
            coverage_counts[name] += int(value)
        if candidate is not None:
            score_by_label[label].append(candidate["score"])

        audit_issue_counts.update(issues)
        reviewed.append(
            {
                "decision_id": label_row["decision_id"],
                "relationship_id": label_row["relationship_id"],
                "subject_event_id": label_row["subject_event_id"],
                "object_event_id": label_row["object_event_id"],
                "subject_revision": label_row["subject_revision"],
                "object_revision": label_row["object_revision"],
                "relation_type": label_row["relation_type"],
                "label": label,
                "latest_action": latest_action["decision"],
                "current_projection": projection,
                "stale": stale,
                "candidate_evidence": candidate,
                "evidence_quality": quality,
                "issues": sorted(set(issues)),
                "decided_at_utc": label_row["decided_at_utc"],
            }
        )

    labels = [item["label"] == "accepted" for item in reviewed if item["candidate_evidence"]]
    scores = [
        item["candidate_evidence"]["score"]
        for item in reviewed
        if item["candidate_evidence"] is not None
    ]
    threshold_sweep = []
    for threshold in normalized_thresholds:
        predictions = [score >= threshold for score in scores]
        threshold_sweep.append(
            {"min_score": threshold, **_metrics(labels, predictions)}
        )

    reviewed_count = len(reviewed)
    accepted_count = sum(item["label"] == "accepted" for item in reviewed)
    rejected_count = sum(item["label"] == "rejected" for item in reviewed)
    scoreable_count = len(scores)
    if reviewed_count == 0:
        status = "no_reviewed_relationships"
    elif reviewed_count < min_reviewed_pairs:
        status = "insufficient_reviewed_sample"
    elif not accepted_count or not rejected_count:
        status = "insufficient_class_diversity"
    elif scoreable_count < min_reviewed_pairs:
        status = "insufficient_score_coverage"
    else:
        status = "evaluated"

    reviewed.sort(
        key=lambda item: (item["decided_at_utc"], item["relationship_id"])
    )
    findings = [item for item in reviewed if item["issues"]]
    findings.sort(
        key=lambda item: (
            -len(item["issues"]),
            item["decided_at_utc"],
            item["relationship_id"],
        )
    )
    coverage = {
        name: _ratio(count, reviewed_count)
        for name, count in sorted(coverage_counts.items())
    }
    score_summary = {}
    for label, values in score_by_label.items():
        score_summary[label] = {
            "count": len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }

    return {
        "status": status,
        "mode": "read_only",
        "database_version": version,
        "evaluation_scope": _EVALUATION_SCOPE,
        "decision_record_count": len(rows),
        "decision_records_by_status": dict(sorted(decisions_by_status.items())),
        "reviewed_relationship_revision_count": reviewed_count,
        "accepted_label_count": accepted_count,
        "rejected_label_count": rejected_count,
        "retracted_action_count": decisions_by_status["retracted"],
        "minimum_reviewed_pairs_for_evaluation": min_reviewed_pairs,
        "candidate_score_coverage": _ratio(scoreable_count, reviewed_count),
        "candidate_score_summary_by_label": score_summary,
        "candidate_threshold_sweep": threshold_sweep,
        "evidence_quality_counts": dict(sorted(quality_counts.items())),
        "evidence_coverage": coverage,
        "audit_issue_counts": dict(sorted(audit_issue_counts.items())),
        "reviewed_by_relation_type": dict(sorted(relation_counts.items())),
        "provider_pair_counts": dict(sorted(provider_pair_counts.items())),
        "reviewed_relationships": reviewed[:limit],
        "reviewed_relationships_returned": min(reviewed_count, limit),
        "findings_total": len(findings),
        "findings": findings[:limit],
        "score_kind": "deterministic_evidence_ranking_not_probability",
        "evaluation_ready": status == "evaluated",
        "source_requests": 0,
        "database_rows_modified": 0,
        "source_events_modified": 0,
        "canonical_identity_modified": False,
        "limitations": [
            "Reviewed relationships are selected by prior candidate generation or manual review and are not a random sample.",
            "Human decisions are labels for retrospective comparison, not independently verified ground truth.",
            "Candidate scores are deterministic ranking evidence, not probabilities or calibrated confidence.",
            "The current candidate rules are applied retrospectively to immutable historical source revisions.",
            "Threshold metrics use only reviewed relationships for which the current candidate rules produce a score.",
            "Evidence completeness measures recorded audit fields; it does not prove that a reviewer claim is true.",
            "Retractions are lifecycle actions and are not treated as negative labels.",
        ],
    }
