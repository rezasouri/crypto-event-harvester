"""Read-only diagnosis of matching coverage and possibly related source records.

Related source records are *not* identity matches: a project's conference
participation is distinct from the conference's own event record.
"""

from __future__ import annotations

import heapq
import json
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .cross_provider_candidates import (
    GENERIC_TITLES,
    MIN_TITLE_LENGTH,
    _canonical,
    _date_overlap,
    _entity_evidence,
    _identity_key,
    _meaningful_tokens,
    _similarity_evidence,
    _title_similarity,
)
from .storage import _connect

_FIELDS = ("project", "ticker", "description", "location", "category")


def _rejection_reason(left: dict, right: dict, min_score: float) -> str:
    """Report the first blocking condition in the existing strict matcher."""
    left_title, right_title = _canonical(left.get("title")), _canonical(right.get("title"))
    if (
        len(left_title) < MIN_TITLE_LENGTH
        or len(right_title) < MIN_TITLE_LENGTH
        or left_title in GENERIC_TITLES
        or right_title in GENERIC_TITLES
    ):
        return "short_or_generic_title"

    title_score, _, _ = _title_similarity(left.get("title"), right.get("title"))
    if title_score == 0:
        return "no_meaningful_title_tokens"

    entity = _entity_evidence(left, right)
    if entity is None:
        return "project_ticker_or_event_type_conflict"

    entity_match, _ = entity
    if not entity_match and (
        len(_meaningful_tokens(left.get("title"))) < 2
        or len(_meaningful_tokens(right.get("title"))) < 2
    ):
        return "insufficient_meaningful_tokens"

    date_result = _date_overlap(left, right)
    if date_result is None:
        return "invalid_date_overlap"

    date_score, _ = date_result
    if entity_match:
        if title_score < 0.70:
            return "title_below_entity_threshold"
        score = 0.65 * title_score + 0.15 * date_score + 0.20
    else:
        if title_score < 0.90:
            return "title_below_title_only_threshold"
        score = 0.82 * title_score + 0.18 * date_score
    return "below_min_score" if score < min_score else "passes_strict_matching"


def _field_present(value: object) -> bool:
    return value is not None and value != "" and value != []


def _relationship_hint(
    left: dict,
    right: dict,
    *,
    title_similarity: float,
    character_similarity: float,
    token_jaccard: float,
) -> dict:
    descriptions = [
        item.get("description") or ""
        for item in (left, right)
        if item.get("provider") == "coindar"
    ]
    participation_terms = (
        "participat", "attend", "exhibit", "sponsor",
        "speaking at", "speaker at", "will join",
    )
    possible_participation = any(
        any(term in _canonical(description) for term in participation_terms)
        for description in descriptions
    )

    location = next(
        (
            _canonical(item.get("location"))
            for item in (left, right)
            if item.get("location")
        ),
        "",
    )
    other_text = " ".join(
        _canonical(item.get("title")) + " " + _canonical(item.get("description"))
        for item in (left, right)
        if not item.get("location")
    )
    location_mentioned = bool(
        location and len(location) >= 3 and f" {location} " in f" {other_text} "
    )

    if possible_participation:
        relation_hint = "possible_participation_mention"
    else:
        relation_hint = "lexically_related_only"

    return {
        "relation_hint": relation_hint,
        "not_an_identity_decision": True,
        "left": {
            field: left.get(field)
            for field in ("event_id", "provider", "title", "start_date", "end_date", "source_url")
        },
        "right": {
            field: right.get(field)
            for field in ("event_id", "provider", "title", "start_date", "end_date", "source_url")
        },
        "title_similarity": title_similarity,
        "character_similarity": character_similarity,
        "token_jaccard": token_jaccard,
        "location_mentioned": location_mentioned,
        "participation_language_detected": possible_participation,
        "date_relation": _date_overlap(left, right)[1],
    }


def diagnose_matching_coverage(
    database_path: str | Path,
    *,
    limit: int = 20,
    min_title_similarity: float = 0.55,
    strict_min_score: float = 0.80,
) -> dict:
    """Compare dated cross-provider pairs without changing matcher rules or SQLite."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not all(
        math.isfinite(value) and 0 <= value <= 1
        for value in (min_title_similarity, strict_min_score)
    ):
        raise ValueError("similarity scores must be finite values between 0 and 1")

    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    conn = _connect(path, create=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT event_id, provider, revision, payload_json "
            "FROM events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    provider_stats: dict[str, dict] = {}
    by_provider: dict[str, list[dict]] = defaultdict(list)
    eligible: list[dict] = []
    exact_eligible = 0
    invalid_dated_records = 0
    for row in rows:
        item = json.loads(row["payload_json"])
        if item.get("event_id") != row["event_id"] or item.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
        provider = row["provider"]
        by_provider[provider].append(item)
        if _identity_key(item) is not None:
            exact_eligible += 1
        if item.get("date_precision") not in {"day", "range"}:
            continue
        try:
            start = date.fromisoformat(item["start_date"])
            end = date.fromisoformat(item["end_date"])
        except (KeyError, TypeError, ValueError):
            invalid_dated_records += 1
            continue
        if end < start:
            invalid_dated_records += 1
            continue
        eligible.append(item)

    for provider in sorted(by_provider):
        records = by_provider[provider]
        provider_stats[provider] = {
            "event_count": len(records),
            "date_precision_counts": dict(
                sorted(Counter(item.get("date_precision") or "unknown" for item in records).items())
            ),
            "event_type_counts": dict(
                sorted(Counter(item.get("event_type") or "unknown" for item in records).items())
            ),
            "metadata_present_counts": {
                field: sum(_field_present(item.get(field)) for item in records)
                for field in _FIELDS
            },
        }

    eligible.sort(key=lambda item: (
        item["start_date"], item["end_date"], item["event_id"],
    ))
    reasons: Counter[str] = Counter()
    top: list[tuple[float, float, int, dict]] = []
    examined_pairs = 0
    strict_candidates = 0
    relation_hint_total = 0
    sequence = 0
    for index, left in enumerate(eligible):
        left_end = date.fromisoformat(left["end_date"])
        for right in eligible[index + 1:]:
            if date.fromisoformat(right["start_date"]) > left_end:
                break
            if left["provider"] == right["provider"]:
                continue
            examined_pairs += 1
            reason = _rejection_reason(left, right, strict_min_score)
            reasons[reason] += 1
            evidence = _similarity_evidence(left, right)
            if evidence is not None and evidence["score"] >= strict_min_score:
                strict_candidates += 1

            title_score, character_score, token_score = _title_similarity(
                left.get("title"), right.get("title")
            )
            shared_tokens = (
                _meaningful_tokens(left.get("title"))
                & _meaningful_tokens(right.get("title"))
            )
            if title_score < min_title_similarity or len(shared_tokens) < 2:
                continue
            relation_hint_total += 1
            hint = _relationship_hint(
                left,
                right,
                title_similarity=title_score,
                character_similarity=character_score,
                token_jaccard=token_score,
            )
            hint["strict_matcher_reason"] = reason
            rank = (title_score, token_score, -sequence, hint)
            sequence += 1
            if len(top) < limit:
                heapq.heappush(top, rank)
            elif rank[:3] > top[0][:3]:
                heapq.heapreplace(top, rank)

    related_hints = [
        row[3] for row in sorted(top, key=lambda row: (-row[0], -row[1], -row[2]))
    ]
    return {
        "status": "diagnosed",
        "scope": "current_dated_cross_provider_overlap_pairs",
        "events_total": len(rows),
        "eligible_with_explicit_calendar_dates": len(eligible),
        "eligible_for_exact_identity_key": exact_eligible,
        "invalid_dated_records": invalid_dated_records,
        "provider_metadata": provider_stats,
        "overlapping_cross_provider_pairs_examined": examined_pairs,
        "strict_similarity_min_score": strict_min_score,
        "strict_match_candidates_before_prior_decision_exclusions": strict_candidates,
        "strict_matcher_reason_counts": dict(sorted(reasons.items())),
        "related_title_min_similarity": min_title_similarity,
        "possibly_related_pairs_total": relation_hint_total,
        "possibly_related_pairs_returned": len(related_hints),
        "possibly_related_pairs": related_hints,
        "source_requests": 0,
        "source_events_modified": 0,
        "database_rows_modified": 0,
        "automatic_merge_allowed": False,
        "limitations": [
            "Date overlap and lexical resemblance are not evidence of identical events.",
            "A project participating in a conference is not the conference itself.",
            "Related-pair hints cannot be accepted or rejected through canonical review import.",
            "This report is not a labeled dataset or an estimate of matcher precision or recall.",
            "Only current events with explicit day or range dates enter pair comparisons.",
        ],
    }
