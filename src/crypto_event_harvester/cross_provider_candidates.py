"""Conservative, zero-request cross-provider event candidate discovery.

This module reports *candidates for human review*; it never merges source
identities, modifies verified events or rewrites their revision histories.
"""
from __future__ import annotations

import heapq
import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from .storage import _connect

MIN_TITLE_LENGTH = 10
GENERIC_TITLE_TERMS = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or",
    "the", "to", "with", "will", "be", "is", "are",
    "token", "tokens", "listing", "listed", "launch", "airdrop",
    "partnership", "announcement", "mainnet", "staking", "release",
    "upgrade", "ama", "webinar", "conference", "event", "events",
    "crypto", "cryptocurrency", "deadline", "ends", "ending", "starts",
    "starting", "opens", "opening", "closes", "closing", "unlock",
    "unlocks", "burn", "vote", "voting", "snapshot", "proposal",
})
GENERIC_TITLES = frozenset({
    "listing", "token listing", "launch", "token launch",
    "airdrop", "partnership", "announcement", "mainnet",
    "mainnet launch", "staking", "release", "upgrade",
    "ama", "webinar", "conference", "cryptocurrency conference",
})


def _canonical(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(
        char if char.isalnum() else " " for char in normalized
    ).split())


def _identity_key(item: dict) -> tuple[str, str, str] | None:
    title = _canonical(item.get("title"))
    start, end = item.get("start_date"), item.get("end_date")
    if len(title) < MIN_TITLE_LENGTH or title in GENERIC_TITLES:
        return None
    if not start or not end or not isinstance(start, str) or not isinstance(end, str):
        # Month/quarter-only labels are deliberately *not* assigned a day.
        return None
    if item.get("date_precision") not in {"day", "range"}:
        return None
    return title, start, end


def _compatible(left: dict, right: dict) -> bool:
    if left.get("provider") == right.get("provider"):
        return False
    # Explicitly conflicting projects or tickers veto a candidate. Missing
    # metadata is neutral; it cannot be treated as supporting agreement.
    for key in ("project", "ticker"):
        a = _canonical(left.get(key))
        b = _canonical(right.get(key))
        if a and b and a != b:
            return False
    return True


def _evidence(item: dict, revision: int) -> dict:
    return {
        "event_id": item["event_id"],
        "provider": item["provider"],
        "title": item["title"],
        "start_date": item["start_date"],
        "end_date": item["end_date"],
        "project": item.get("project"),
        "ticker": item.get("ticker"),
        "source_url": item["source_url"],
        "revision": revision,
    }


@dataclass(frozen=True)
class MatchResult:
    examined: int
    eligible: int
    candidates_total: int
    candidates: list[dict]

    def to_dict(self) -> dict:
        return {
            "mode": "review_only",
            "examined": self.examined,
            "eligible_with_exact_calendar_dates": self.eligible,
            "candidates_total": self.candidates_total,
            "returned": len(self.candidates),
            "candidates": self.candidates,
            "source_events_modified": 0,
            "source_requests": 0,
        }


def match_candidates(database_path: str | Path, *, limit: int = 100) -> MatchResult:
    """Deterministically compare current stored source snapshots only.

    Exact day/range equality and a conservatively canonicalized title are
    mandatory. These are *not* ground-truth confirmations of event identity.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000")
    conn = _connect(Path(database_path), create=False)
    try:
        rows = conn.execute(
            "SELECT event_id, provider, revision, payload_json "
            "FROM events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple[str, str, str], list[tuple[dict, int]]] = defaultdict(list)
    eligible = 0
    for row in rows:
        item = json.loads(row["payload_json"])
        # Reject malformed rows instead of trusting a JSON label alone.
        if item.get("event_id") != row["event_id"] or item.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
        key = _identity_key(item)
        if key is None:
            continue
        eligible += 1
        groups[key].append((item, row["revision"]))

    candidates: list[dict] = []
    total = 0
    for (title, start, end), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        # Stored event order is stable (SQL ORDER BY event_id).
        for i, (left, rev_left) in enumerate(group):
            for right, rev_right in group[i + 1:]:
                if not _compatible(left, right):
                    continue
                total += 1
                if len(candidates) < limit:
                    candidates.append({
                        "match_type": "review_required",
                        "evidence": {
                            "canonical_title": title,
                            "exact_start_date": start,
                            "exact_end_date": end,
                            "nonconflicting_metadata_only": True,
                        },
                        "left": _evidence(left, rev_left),
                        "right": _evidence(right, rev_right),
                    })
    return MatchResult(len(rows), eligible, total, candidates)


@dataclass(frozen=True)
class SimilarityResult:
    examined: int
    eligible: int
    overlapping_pairs_examined: int
    candidates_total: int
    candidates: list[dict]
    min_score: float

    def to_dict(self) -> dict:
        return {
            "mode": "review_only",
            "strategy": "similarity",
            "examined": self.examined,
            "eligible_with_explicit_calendar_dates": self.eligible,
            "overlapping_cross_provider_pairs_examined": self.overlapping_pairs_examined,
            "minimum_score": self.min_score,
            "candidates_total": self.candidates_total,
            "returned": len(self.candidates),
            "candidates": self.candidates,
            "source_events_modified": 0,
            "source_requests": 0,
        }


def _meaningful_tokens(value: str | None) -> set[str]:
    return {
        token for token in _canonical(value).split()
        if token not in GENERIC_TITLE_TERMS and len(token) > 1
    }


def _title_similarity(left: str | None, right: str | None) -> tuple[float, float, float]:
    left_tokens = _meaningful_tokens(left)
    right_tokens = _meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0, 0.0, 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    token_score = intersection / union
    left_sorted = " ".join(sorted(left_tokens))
    right_sorted = " ".join(sorted(right_tokens))
    character_score = SequenceMatcher(
        None, left_sorted, right_sorted, autojunk=False
    ).ratio()
    combined = 0.55 * character_score + 0.45 * token_score
    return round(combined, 4), round(character_score, 4), round(token_score, 4)


def _date_overlap(left: dict, right: dict) -> tuple[float, str] | None:
    try:
        left_start = date.fromisoformat(left["start_date"])
        left_end = date.fromisoformat(left["end_date"])
        right_start = date.fromisoformat(right["start_date"])
        right_end = date.fromisoformat(right["end_date"])
    except (KeyError, TypeError, ValueError):
        return None
    if left_end < right_start or right_end < left_start:
        return None
    if left_start == right_start and left_end == right_end:
        return 1.0, "exact_range"
    if left_start == right_start:
        return 0.97, "same_start"
    if left_end == right_end:
        return 0.95, "same_end"
    if left_start <= right_start and left_end >= right_end:
        return 0.90, "left_contains_right"
    if right_start <= left_start and right_end >= left_end:
        return 0.90, "right_contains_left"
    return 0.82, "partial_overlap"


def _entity_evidence(left: dict, right: dict) -> tuple[bool, list[str]] | None:
    """Return None for hard conflicts; missing metadata is neutral."""
    matches = []
    for key in ("project", "ticker"):
        a = _canonical(left.get(key))
        b = _canonical(right.get(key))
        if a and b and a != b:
            return None
        if a and b and a == b:
            matches.append(key)
    left_type, right_type = left.get("event_type"), right.get("event_type")
    if (
        left_type in {"conference", "token_event"}
        and right_type in {"conference", "token_event"}
        and left_type != right_type
    ):
        return None
    return bool(matches), matches


def _similarity_evidence(left: dict, right: dict) -> dict | None:
    if left.get("provider") == right.get("provider"):
        return None
    if left.get("date_precision") not in {"day", "range"}:
        return None
    if right.get("date_precision") not in {"day", "range"}:
        return None
    left_title = _canonical(left.get("title"))
    right_title = _canonical(right.get("title"))
    if (
        len(left_title) < MIN_TITLE_LENGTH
        or len(right_title) < MIN_TITLE_LENGTH
        or left_title in GENERIC_TITLES
        or right_title in GENERIC_TITLES
    ):
        return None
    title_score, character_score, token_score = _title_similarity(
        left.get("title"), right.get("title")
    )
    if not title_score:
        return None
    entity = _entity_evidence(left, right)
    if entity is None:
        return None
    entity_match, entity_matches = entity
    # Without corroborating project/ticker metadata, require a very strong
    # title match and at least two meaningful words on both sides.
    left_tokens = _meaningful_tokens(left.get("title"))
    right_tokens = _meaningful_tokens(right.get("title"))
    if not entity_match and (len(left_tokens) < 2 or len(right_tokens) < 2):
        return None
    date_result = _date_overlap(left, right)
    if date_result is None:
        return None
    date_score, date_relation = date_result
    if entity_match:
        if title_score < 0.70:
            return None
        score = 0.65 * title_score + 0.15 * date_score + 0.20
    else:
        if title_score < 0.90:
            return None
        score = 0.82 * title_score + 0.18 * date_score
    return {
        "score": round(score, 4),
        "title_similarity": title_score,
        "character_similarity": character_score,
        "token_jaccard": token_score,
        "date_similarity": date_score,
        "date_relation": date_relation,
        "project_match": "project" in entity_matches if left.get("project") and right.get("project") else None,
        "ticker_match": "ticker" in entity_matches if left.get("ticker") and right.get("ticker") else None,
        "shared_entity_fields": entity_matches,
        "threshold_path": "entity_corroborated" if entity_match else "title_only_strict",
    }


def match_similar_candidates(
    database_path: str | Path, *, limit: int = 100, min_score: float = 0.80
) -> SimilarityResult:
    """Find fuzzy cross-provider candidates from current snapshots, offline.

    Date intervals must overlap. Explicit project/ticker conflicts and
    conference-vs-token-event conflicts veto a pair. Without a shared
    project/ticker, a high title similarity is mandatory. Every result is
    review-only; no source identity or SQLite row is modified.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000")
    if not 0.0 <= min_score <= 1.0:
        raise ValueError("--min-score must be between 0 and 1")
    conn = _connect(Path(database_path), create=False)
    try:
        rows = conn.execute(
            "SELECT event_id, provider, revision, payload_json "
            "FROM events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    eligible_items: list[tuple[dict, int]] = []
    for row in rows:
        item = json.loads(row["payload_json"])
        if item.get("event_id") != row["event_id"] or item.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
        if item.get("date_precision") not in {"day", "range"}:
            continue
        try:
            date.fromisoformat(item["start_date"])
            date.fromisoformat(item["end_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if item["start_date"] > item["end_date"]:
            raise ValueError("stored event has an inverted calendar date range")
        eligible_items.append((item, row["revision"]))

    eligible_items.sort(key=lambda pair: (
        pair[0]["start_date"], pair[0]["end_date"], pair[0]["event_id"]
    ))
    candidate_heap: list[tuple[float, int, dict]] = []
    total = 0
    pairs_examined = 0
    pair_order = 0
    for index, (left, left_revision) in enumerate(eligible_items):
        left_end = date.fromisoformat(left["end_date"])
        for right, right_revision in eligible_items[index + 1:]:
            right_start = date.fromisoformat(right["start_date"])
            if right_start > left_end:
                break
            if left["provider"] == right["provider"]:
                continue
            pairs_examined += 1
            evidence = _similarity_evidence(left, right)
            if evidence is None or evidence["score"] < min_score:
                continue
            total += 1
            candidate = {
                "match_type": "review_required",
                "confidence_band": (
                    "high" if evidence["score"] >= 0.92
                    and evidence["title_similarity"] >= 0.90
                    else "medium"
                ),
                "evidence": evidence,
                "left": _evidence(left, left_revision),
                "right": _evidence(right, right_revision),
            }
            # Keep the highest-scoring bounded result set without retaining
            # every pair in memory. Earlier pairs win exact score ties.
            heap_item = (evidence["score"], -pair_order, candidate)
            pair_order += 1
            if len(candidate_heap) < limit:
                heapq.heappush(candidate_heap, heap_item)
            elif heap_item[:2] > candidate_heap[0][:2]:
                heapq.heapreplace(candidate_heap, heap_item)

    candidates = [item[2] for item in candidate_heap]
    candidates.sort(key=lambda item: (
        -item["evidence"]["score"],
        item["left"]["event_id"],
        item["right"]["event_id"],
    ))
    return SimilarityResult(
        examined=len(rows),
        eligible=len(eligible_items),
        overlapping_pairs_examined=pairs_examined,
        candidates_total=total,
        candidates=candidates,
        min_score=min_score,
    )
