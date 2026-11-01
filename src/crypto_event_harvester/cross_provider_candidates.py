"""Conservative, zero-request cross-provider event candidate discovery.

This module reports *candidates for human review*; it never merges source
identities, modifies verified events or rewrites their revision histories.
"""
from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .storage import _connect

MIN_TITLE_LENGTH = 10
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
