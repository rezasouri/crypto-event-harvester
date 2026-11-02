"""Retrospective evaluation of human-reviewed cross-provider event pairs.

Metrics are deliberately scoped to reviewed source-revision pairs. They are
not estimates of recall over all possible event pairs or the full calendar.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .cross_provider_candidates import _compatible, _identity_key, _similarity_evidence
from .storage import _connect

DEFAULT_THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _metrics(labels: list[bool], predictions: list[bool]) -> dict:
    true_positive = sum(label and prediction for label, prediction in zip(labels, predictions))
    false_positive = sum(not label and prediction for label, prediction in zip(labels, predictions))
    true_negative = sum(not label and not prediction for label, prediction in zip(labels, predictions))
    false_negative = sum(label and not prediction for label, prediction in zip(labels, predictions))
    total = len(labels)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = (
        _ratio(2 * true_positive, 2 * true_positive + false_positive + false_negative)
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


def evaluate_reconciliation(
    database_path: str | Path,
    *,
    thresholds: list[float] | tuple[float, ...] = DEFAULT_THRESHOLDS,
    min_reviewed_pairs: int = 30,
) -> dict:
    """Evaluate exact and similarity matching against immutable human decisions.

    The evaluation reads the exact source revisions captured by each decision,
    not the latest event snapshots. A pair/revision combination is counted
    once even if it was reviewed with more than one strategy.
    """
    if min_reviewed_pairs < 1:
        raise ValueError("min-reviewed-pairs must be at least 1")
    normalized_thresholds = sorted({float(value) for value in thresholds})
    if not normalized_thresholds:
        raise ValueError("at least one similarity threshold is required")
    if any(
        not math.isfinite(value) or value < 0.0 or value > 1.0
        for value in normalized_thresholds
    ):
        raise ValueError("similarity thresholds must be finite values between 0 and 1")

    conn = _connect(Path(database_path), create=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        database_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "reconciliation_decisions" not in tables:
            if database_version >= 2:
                raise RuntimeError(
                    "database schema declares reconciliation support but its decision table is missing"
                )
            rows = []
        else:
            rows = conn.execute(
                """
                SELECT
                    d.left_event_id,
                    d.right_event_id,
                    d.left_revision,
                    d.right_revision,
                    d.decision,
                    d.strategy,
                    left_revision.payload_json AS left_payload_json,
                    right_revision.payload_json AS right_payload_json
                FROM reconciliation_decisions AS d
                JOIN event_revisions AS left_revision
                  ON left_revision.event_id = d.left_event_id
                 AND left_revision.revision = d.left_revision
                JOIN event_revisions AS right_revision
                  ON right_revision.event_id = d.right_event_id
                 AND right_revision.revision = d.right_revision
                ORDER BY
                    d.left_event_id,
                    d.left_revision,
                    d.right_event_id,
                    d.right_revision,
                    d.decided_at_utc,
                    d.decision_id
                """
            ).fetchall()
    finally:
        conn.close()

    reviewed: dict[tuple[str, int, str, int], dict] = {}
    strategy_counts = {"exact": 0, "similarity": 0}
    for row in rows:
        pair_key = (
            row["left_event_id"],
            row["left_revision"],
            row["right_event_id"],
            row["right_revision"],
        )
        label = row["decision"] == "accepted"
        item = reviewed.get(pair_key)
        if item is None:
            left = json.loads(row["left_payload_json"])
            right = json.loads(row["right_payload_json"])
            if left.get("event_id") != row["left_event_id"]:
                raise ValueError("left decision revision has inconsistent event identity")
            if right.get("event_id") != row["right_event_id"]:
                raise ValueError("right decision revision has inconsistent event identity")
            if left.get("provider") == right.get("provider"):
                raise ValueError("reviewed pair contains events from the same provider")
            item = {
                "label": label,
                "left": left,
                "right": right,
                "strategies": set(),
            }
            reviewed[pair_key] = item
        elif item["label"] != label:
            raise ValueError("conflicting human labels exist for one event revision pair")
        item["strategies"].add(row["strategy"])
        strategy_counts[row["strategy"]] += 1

    labels: list[bool] = []
    exact_predictions: list[bool] = []
    similarity_scores: list[float | None] = []
    provider_pair_counts: dict[str, int] = {}
    for item in reviewed.values():
        left, right = item["left"], item["right"]
        left_key = _identity_key(left)
        right_key = _identity_key(right)
        exact_predictions.append(
            left_key is not None and left_key == right_key and _compatible(left, right)
        )
        evidence = _similarity_evidence(left, right)
        similarity_scores.append(None if evidence is None else float(evidence["score"]))
        labels.append(item["label"])
        providers = sorted((left["provider"], right["provider"]))
        provider_pair = "|".join(providers)
        provider_pair_counts[provider_pair] = provider_pair_counts.get(provider_pair, 0) + 1

    threshold_sweep = []
    for threshold in normalized_thresholds:
        predictions = [
            score is not None and score >= threshold for score in similarity_scores
        ]
        threshold_sweep.append(
            {"min_score": threshold, **_metrics(labels, predictions)}
        )

    reviewed_count = len(reviewed)
    accepted_count = sum(labels)
    rejected_count = reviewed_count - accepted_count
    scoreable_count = sum(score is not None for score in similarity_scores)
    if reviewed_count == 0:
        status = "no_reviewed_pairs"
    elif reviewed_count < min_reviewed_pairs:
        status = "insufficient_reviewed_sample"
    else:
        status = "evaluated"

    return {
        "status": status,
        "database_version": database_version,
        "evaluation_scope": "historically_reviewed_event_revision_pairs_only",
        "reviewed_pair_count": reviewed_count,
        "accepted_label_count": accepted_count,
        "rejected_label_count": rejected_count,
        "decision_record_count": len(rows),
        "decision_records_by_strategy": strategy_counts,
        "provider_pair_counts": dict(sorted(provider_pair_counts.items())),
        "minimum_reviewed_pairs_for_evaluation": min_reviewed_pairs,
        "similarity_score_coverage": _ratio(scoreable_count, reviewed_count),
        "exact_strategy": _metrics(labels, exact_predictions),
        "similarity_threshold_sweep": threshold_sweep,
        "source_requests": 0,
        "source_events_modified": 0,
        "database_rows_modified": 0,
        "limitations": [
            "Reviewed pairs are selected by prior candidate generation and are not a random sample.",
            "Recall is measured only within reviewed event-revision pairs; unreviewed pairs have no labels.",
            "Similarity scores are ranking evidence, not probabilities or calibrated confidence.",
            "Human decisions are treated as labels; this report does not measure reviewer agreement.",
        ],
    }
