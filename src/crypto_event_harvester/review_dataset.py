"""Human-review CSV export/import for cross-provider matching evaluation.

Exports are read-only. Imports revalidate immutable source revision numbers and
recompute matching evidence before writing append-only reconciliation decisions.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

from .cross_provider_candidates import match_candidates, match_similar_candidates
from .reconciliation import reconcile_pair, validate_reconciliation_pair
from .storage import _connect

CSV_FIELDS = (
    "candidate_id",
    "strategy",
    "min_score",
    "left_event_id",
    "left_revision",
    "left_provider",
    "left_title",
    "left_start_date",
    "left_end_date",
    "left_project",
    "left_ticker",
    "left_source_url",
    "right_event_id",
    "right_revision",
    "right_provider",
    "right_title",
    "right_start_date",
    "right_end_date",
    "right_project",
    "right_ticker",
    "right_source_url",
    "match_score",
    "title_similarity",
    "date_relation",
    "confidence_band",
    "evidence_json",
    "review_assessment_json",
    "review_decision",
    "review_note",
)

_REQUIRED_IMPORT_FIELDS = frozenset(
    {
        "candidate_id",
        "strategy",
        "min_score",
        "left_event_id",
        "left_revision",
        "right_event_id",
        "right_revision",
        "review_decision",
        "review_note",
    }
)
_MAX_BATCH_ROWS = 1000


def _pair_key(
    left_event_id: str,
    left_revision: int,
    right_event_id: str,
    right_revision: int,
) -> tuple[str, int, str, int]:
    pair = sorted(
        (
            (left_event_id, left_revision),
            (right_event_id, right_revision),
        )
    )
    return pair[0][0], pair[0][1], pair[1][0], pair[1][1]


def _candidate_id(
    left_event_id: str,
    left_revision: int,
    right_event_id: str,
    right_revision: int,
    *,
    strategy: str,
    min_score: float,
) -> str:
    pair = _pair_key(left_event_id, left_revision, right_event_id, right_revision)
    payload = {
        "pair": pair,
        "strategy": strategy,
        "min_score": format(min_score, ".8f") if strategy == "similarity" else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "review:" + hashlib.sha256(encoded).hexdigest()[:20]


def _excluded_pairs(database_path: str | Path) -> set[tuple[str, int, str, int]]:
    """Read existing decisions and current canonical links without migrating."""
    conn = _connect(Path(database_path), create=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        excluded: set[tuple[str, int, str, int]] = set()
        if "reconciliation_decisions" in tables:
            decisions = conn.execute(
                """
                SELECT left_event_id, left_revision, right_event_id, right_revision
                FROM reconciliation_decisions
                """
            ).fetchall()
            excluded.update(
                _pair_key(
                    row["left_event_id"],
                    row["left_revision"],
                    row["right_event_id"],
                    row["right_revision"],
                )
                for row in decisions
            )
        if {"canonical_event_members", "events"} <= tables:
            linked = conn.execute(
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
            for row in linked:
                if (
                    row["left_linked_revision"] == row["left_revision"]
                    and row["right_linked_revision"] == row["right_revision"]
                ):
                    excluded.add(
                        _pair_key(
                            row["left_event_id"],
                            row["left_revision"],
                            row["right_event_id"],
                            row["right_revision"],
                        )
                    )
        return excluded
    finally:
        conn.close()


def _candidate_report(
    database_path: str | Path,
    *,
    strategy: str,
    min_score: float,
    limit: int,
) -> dict:
    if strategy not in {"exact", "similarity"}:
        raise ValueError("strategy must be exact or similarity")
    if not 1 <= limit <= _MAX_BATCH_ROWS:
        raise ValueError(f"limit must be between 1 and {_MAX_BATCH_ROWS}")
    if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
        raise ValueError("min-score must be a finite value between 0 and 1")
    excluded = _excluded_pairs(database_path)
    result = (
        match_candidates(database_path, limit=limit, excluded_pairs=excluded).to_dict()
        if strategy == "exact"
        else match_similar_candidates(
            database_path,
            limit=limit,
            min_score=min_score,
            excluded_pairs=excluded,
        ).to_dict()
    )
    result["strategy"] = strategy
    result["min_score"] = min_score if strategy == "similarity" else None
    return result


def export_review_dataset(
    database_path: str | Path,
    output_path: str | Path,
    *,
    strategy: str = "similarity",
    min_score: float = 0.80,
    limit: int = 100,
) -> dict:
    """Export pending candidate pairs to an editable UTF-8 CSV without DB writes."""
    report = _candidate_report(
        database_path,
        strategy=strategy,
        min_score=min_score,
        limit=limit,
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for candidate in report["candidates"]:
            left, right = candidate["left"], candidate["right"]
            evidence = candidate.get("evidence", {})
            assessment = candidate.get("review_assessment", {})
            row = {
                "candidate_id": _candidate_id(
                    left["event_id"],
                    int(left["revision"]),
                    right["event_id"],
                    int(right["revision"]),
                    strategy=strategy,
                    min_score=min_score,
                ),
                "strategy": strategy,
                "min_score": (
                    format(min_score, ".8f") if strategy == "similarity" else ""
                ),
                "match_score": evidence.get("score", ""),
                "title_similarity": evidence.get("title_similarity", ""),
                "date_relation": evidence.get("date_relation", ""),
                "confidence_band": candidate.get("confidence_band", ""),
                "evidence_json": json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "review_assessment_json": json.dumps(
                    assessment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "review_decision": "",
                "review_note": "",
            }
            for side, item in (("left", left), ("right", right)):
                for field in (
                    "event_id",
                    "revision",
                    "provider",
                    "title",
                    "start_date",
                    "end_date",
                    "project",
                    "ticker",
                    "source_url",
                ):
                    row[f"{side}_{field}"] = item.get(field, "")
            writer.writerow(row)

    return {
        "status": "exported",
        "output": str(path),
        "strategy": strategy,
        "min_score": min_score if strategy == "similarity" else None,
        "candidates_total": report["candidates_total"],
        "exported_count": len(report["candidates"]),
        "truncated": report["candidates_total"] > len(report["candidates"]),
        "source_requests": 0,
        "source_events_modified": 0,
        "database_rows_modified": 0,
        "review_decisions_required": True,
        "automatic_merge_allowed": False,
    }


def _normalize_decision(value: str | None) -> str | None:
    normalized = (value or "").strip().casefold()
    if normalized in {"", "unsure", "uncertain", "skip", "pending"}:
        return None
    if normalized in {"accept", "accepted"}:
        return "accepted"
    if normalized in {"reject", "rejected"}:
        return "rejected"
    raise ValueError("review_decision must be accept, reject, unsure or blank")


def _load_review_rows(input_path: str | Path) -> list[dict]:
    path = Path(input_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        missing = sorted(_REQUIRED_IMPORT_FIELDS - headers)
        if missing:
            raise ValueError(f"review CSV is missing required columns: {', '.join(missing)}")
        rows = []
        seen_ids: set[str] = set()
        pair_labels: dict[tuple[str, int, str, int], str] = {}
        for line_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ValueError(f"review CSV row {line_number} has more values than its header")
            candidate_id = (raw.get("candidate_id") or "").strip()
            left_id = (raw.get("left_event_id") or "").strip()
            right_id = (raw.get("right_event_id") or "").strip()
            if not candidate_id or not left_id or not right_id:
                raise ValueError(f"review CSV row {line_number} is missing candidate or event IDs")
            if candidate_id in seen_ids:
                raise ValueError(f"duplicate candidate_id in review CSV: {candidate_id}")
            seen_ids.add(candidate_id)
            strategy = (raw.get("strategy") or "").strip().casefold()
            if strategy not in {"exact", "similarity"}:
                raise ValueError(f"review CSV row {line_number} has an invalid strategy")
            try:
                left_revision = int(raw.get("left_revision", ""))
                right_revision = int(raw.get("right_revision", ""))
                if left_revision < 1 or right_revision < 1:
                    raise ValueError
                min_score_text = (raw.get("min_score") or "").strip()
                min_score = float(min_score_text) if min_score_text else 0.80
            except (TypeError, ValueError) as exc:
                raise ValueError(f"review CSV row {line_number} has invalid revisions or min_score") from exc
            if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
                raise ValueError(f"review CSV row {line_number} has invalid min_score")
            expected_id = _candidate_id(
                left_id,
                left_revision,
                right_id,
                right_revision,
                strategy=strategy,
                min_score=min_score,
            )
            if candidate_id != expected_id:
                raise ValueError(f"review CSV row {line_number} candidate_id does not match its pair")
            decision = _normalize_decision(raw.get("review_decision"))
            pair_key = _pair_key(left_id, left_revision, right_id, right_revision)
            if decision is not None:
                previous = pair_labels.get(pair_key)
                if previous is not None and previous != decision:
                    raise ValueError(
                        f"review CSV contains conflicting labels for pair {pair_key}"
                    )
                pair_labels[pair_key] = decision
            rows.append(
                {
                    "line_number": line_number,
                    "candidate_id": candidate_id,
                    "left_event_id": left_id,
                    "right_event_id": right_id,
                    "left_revision": left_revision,
                    "right_revision": right_revision,
                    "expected_revisions": {
                        left_id: left_revision,
                        right_id: right_revision,
                    },
                    "strategy": strategy,
                    "min_score": min_score,
                    "decision": decision,
                    "note": (raw.get("review_note") or "").strip() or None,
                }
            )
            if len(rows) > _MAX_BATCH_ROWS:
                raise ValueError(f"review CSV exceeds the {_MAX_BATCH_ROWS}-row limit")
    return rows


def import_review_dataset(
    database_path: str | Path,
    input_path: str | Path,
    *,
    apply: bool = False,
) -> dict:
    """Validate or apply human labels; dry-run is the default and never migrates."""
    rows = _load_review_rows(input_path)
    results = []
    validated = 0
    applied = 0
    skipped = 0
    idempotent = 0
    stale = 0
    conflicts = 0
    errors = 0

    for row in rows:
        base = {
            "line_number": row["line_number"],
            "candidate_id": row["candidate_id"],
            "decision": row["decision"],
        }
        if row["decision"] is None:
            skipped += 1
            results.append({**base, "status": "skipped_unreviewed"})
            continue
        try:
            if apply:
                result = reconcile_pair(
                    database_path,
                    left_event_id=row["left_event_id"],
                    right_event_id=row["right_event_id"],
                    strategy=row["strategy"],
                    decision=row["decision"],
                    min_score=row["min_score"],
                    note=row["note"],
                    expected_revisions=row["expected_revisions"],
                )
                applied += 1
                idempotent += int(result["idempotent"])
                results.append(
                    {
                        **base,
                        "status": "applied",
                        "decision_id": result["decision_id"],
                        "idempotent": result["idempotent"],
                    }
                )
            else:
                validation = validate_reconciliation_pair(
                    database_path,
                    left_event_id=row["left_event_id"],
                    right_event_id=row["right_event_id"],
                    strategy=row["strategy"],
                    min_score=row["min_score"],
                    expected_revisions=row["expected_revisions"],
                )
                validated += 1
                results.append(
                    {
                        **base,
                        "status": "validated",
                        "current_revisions": validation["source_revisions"],
                        "evidence": validation["evidence"],
                    }
                )
        except (OSError, ValueError, RuntimeError) as exc:
            message = str(exc)
            if message.startswith("stale review candidate:"):
                stale += 1
                status = "stale"
            elif message.startswith("conflict:"):
                conflicts += 1
                status = "conflict"
            else:
                errors += 1
                status = "error"
            results.append({**base, "status": status, "error": message})

    failed = stale + conflicts + errors
    return {
        "status": (
            "completed_with_errors" if failed and apply
            else "validation_errors" if failed
            else "applied" if apply
            else "validated"
        ),
        "mode": "apply" if apply else "dry_run",
        "rows_total": len(rows),
        "validated_count": validated,
        "applied_count": applied,
        "idempotent_count": idempotent,
        "skipped_unreviewed_count": skipped,
        "stale_count": stale,
        "conflict_count": conflicts,
        "error_count": errors,
        "source_requests": 0,
        "source_events_modified": 0,
        "automatic_merge_allowed": False,
        "rows": results,
    }
