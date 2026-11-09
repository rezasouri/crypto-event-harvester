"""Read-only discovery of reviewable semantic event relationships.

Unlike canonical identity matching, this module looks for an explicit
relationship assertion in one source record that refers to a dated conference
record. Scores rank evidence for human review; they are not probabilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cross_provider_candidates import _date_overlap, _meaningful_tokens
from .relationships import relationship_id_for
from .storage import _connect

_CUE_PATTERNS = (
    (
        "speaks_at",
        "speaker",
        re.compile(
            r"\b(?:speak(?:s|ing)?|spoke|speaker|speakers|keynote(?:s)?)\b"
            r"(?:\s+\w+){0,4}\s+\b(?:at|during|for)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sponsors",
        "sponsor",
        re.compile(
            r"\b(?:sponsor|sponsors|sponsored|sponsoring|sponsorship)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "organizes",
        "organizer",
        re.compile(
            r"\b(?:organize|organizes|organized|organizing|organise|organises|organised|organising|organiser|organisers|organizer|organizers)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exhibits_at",
        "exhibitor",
        re.compile(
            r"\b(?:exhibit|exhibits|exhibited|exhibiting|exhibitor|exhibitors|booth)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "attends",
        "attendee",
        re.compile(
            r"\b(?:attend|attends|attended|attending|attendance)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "participates_in",
        "represented_by",
        re.compile(r"\brepresented\s+by\b", re.IGNORECASE),
    ),
    (
        "participates_in",
        "participant",
        re.compile(
            r"\b(?:participate|participates|participated|participating|participation)\b"
            r"|\b(?:take|takes|taking|took)\s+part\s+in\b",
            re.IGNORECASE,
        ),
    ),
)

_RELATION_WORDS = frozenset(
    {
        "attend", "attended", "attendee", "attending", "attendance",
        "booth", "exhibit", "exhibited", "exhibiting", "exhibitor", "exhibitors",
        "keynote", "keynotes", "organise", "organised", "organiser", "organisers",
        "organize", "organized", "organizer", "organizers", "organizing",
        "participate", "participated", "participates", "participating", "participation",
        "part", "represented", "representing", "speaker", "speakers", "speaking",
        "sponsor", "sponsored", "sponsors", "sponsoring", "sponsorship",
        "speak", "spoke", "take", "takes", "taking", "took",
    }
)

_FIELD_STRENGTH = {
    "description": 1.0,
    "title": 0.95,
    "source_url": 0.90,
}


_RELATION_TARGET_GENERIC_TERMS = frozenset(
    {
        "annual",
        "community",
        "conference",
        "convention",
        "day",
        "days",
        "edition",
        "event",
        "events",
        "expo",
        "exhibition",
        "festival",
        "forum",
        "global",
        "international",
        "meetup",
        "meeting",
        "summit",
        "week",
        "weeks",
        "workshop",
        "workshops",
        "world",
        "x",
    }
)


def _url_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        path = unquote(urlsplit(value).path)
    except ValueError:
        path = value
    return path.replace("-", " ").replace("_", " ")


def _is_conference(item: dict) -> bool:
    values = (
        str(item.get("event_type") or "").casefold(),
        str(item.get("category") or "").casefold(),
    )
    return any("conference" in value for value in values)


def _extract_relation_cues(item: dict) -> list[dict]:
    """Extract explicit, locally attributed cues from each source field."""
    project_tokens = _meaningful_tokens(item.get("project"))
    fields = (
        ("description", str(item.get("description") or "")),
        ("title", str(item.get("title") or "")),
        ("source_url", _url_text(item.get("source_url"))),
    )
    best_by_relation_and_field: dict[tuple[str, str], dict] = {}

    for field, text in fields:
        if not text:
            continue
        for relation_type, mode, pattern in _CUE_PATTERNS:
            for match in pattern.finditer(text):
                start, end = match.span()
                window = text[max(0, start - 72): min(len(text), end + 72)]
                if project_tokens:
                    local_project_tokens = _meaningful_tokens(window) & project_tokens
                    required = (
                        1
                        if len(project_tokens) == 1
                        else max(2, math.ceil(len(project_tokens) * 0.5))
                    )
                    if len(local_project_tokens) < required:
                        continue
                    attribution = "project_name_near_cue"
                    attribution_strength = 1.0
                else:
                    attribution = "implicit_record_subject"
                    attribution_strength = 0.75

                cue = {
                    "relation_type": relation_type,
                    "mode": mode,
                    "field": field,
                    "phrase": match.group(0),
                    "snippet": text[max(0, start - 72): min(len(text), end + 96)].strip(),
                    "actor_attribution": attribution,
                    "cue_strength": round(
                        _FIELD_STRENGTH[field] * attribution_strength, 4
                    ),
                }
                key = (relation_type, field)
                previous = best_by_relation_and_field.get(key)
                if previous is None or cue["cue_strength"] > previous["cue_strength"]:
                    best_by_relation_and_field[key] = cue

    return [
        best_by_relation_and_field[key]
        for key in sorted(best_by_relation_and_field)
    ]

def _target_match(object_event: dict, cue: dict) -> dict | None:
    """Match distinctive conference-title anchors inside the relation cue itself.

    Generic event words, years and the target's own location are not considered
    title anchors. When the target has a known location, at least one distinctive
    title anchor and the full location must occur in the cue snippet. Without a
    known location, two distinctive title anchors are required.
    """
    title_tokens = _meaningful_tokens(object_event.get("title"))
    location_tokens = _meaningful_tokens(object_event.get("location"))
    anchor_tokens = {
        token
        for token in title_tokens - _RELATION_TARGET_GENERIC_TERMS - location_tokens
        if not token.isdigit()
    }
    if not anchor_tokens:
        return None

    cue_text = str(cue.get("snippet") or "")
    cue_tokens = _meaningful_tokens(cue_text) - _RELATION_WORDS
    shared = sorted(anchor_tokens & cue_tokens)
    location_mentioned = bool(location_tokens) and location_tokens <= cue_tokens

    if location_tokens:
        if not location_mentioned or not shared:
            return None
        match_basis = "distinctive_title_anchor_and_location"
    else:
        if len(shared) < 2:
            return None
        match_basis = "multiple_distinctive_title_anchors"

    coverage = len(shared) / len(anchor_tokens)
    return {
        "shared_tokens": shared,
        "target_title_tokens": sorted(title_tokens),
        "target_anchor_tokens": sorted(anchor_tokens),
        "target_title_coverage": round(coverage, 4),
        "location_mentioned": location_mentioned,
        "match_basis": match_basis,
        "match_scope": "relation_cue_snippet",
    }

def _snapshot(item: dict, revision: int) -> dict:
    return {
        "event_id": item["event_id"],
        "provider": item["provider"],
        "revision": revision,
        "title": item.get("title"),
        "source_url": item.get("source_url"),
        "start_date": item.get("start_date"),
        "end_date": item.get("end_date"),
        "date_precision": item.get("date_precision"),
        "event_type": item.get("event_type"),
        "category": item.get("category"),
        "project": item.get("project"),
        "ticker": item.get("ticker"),
        "location": item.get("location"),
        "description": item.get("description"),
    }


def _candidate(
    subject: dict,
    subject_revision: int,
    object_event: dict,
    object_revision: int,
    cue: dict,
    target: dict,
    date_evidence: tuple[float, str],
    min_score: float,
    previous_decision: str | None,
) -> dict | None:
    date_score, date_relation = date_evidence
    location_mentioned = target["location_mentioned"]
    components = {
        "cue_strength": cue["cue_strength"],
        "target_title_coverage": target["target_title_coverage"],
        "date_overlap": date_score,
        "location_mentioned": location_mentioned,
    }
    score = (
        0.40 * components["cue_strength"]
        + 0.35 * components["target_title_coverage"]
        + 0.15 * components["date_overlap"]
        + 0.10 * float(location_mentioned)
    )
    score = round(score, 4)
    if score < min_score:
        return None

    relation_type = cue["relation_type"]
    relationship_id = relationship_id_for(
        subject["event_id"], object_event["event_id"], relation_type
    )
    raw_id = "\0".join(
        (
            subject["event_id"],
            str(subject_revision),
            object_event["event_id"],
            str(object_revision),
            relation_type,
        )
    ).encode("utf-8")
    result = {
        "candidate_id": (
            "relationship-candidate:" + hashlib.sha256(raw_id).hexdigest()[:32]
        ),
        "relationship_id": relationship_id,
        "status": "review_required",
        "relation_type": relation_type,
        "score": score,
        "score_kind": "deterministic_evidence_score_not_probability",
        "score_components": components,
        "subject": _snapshot(subject, subject_revision),
        "object": _snapshot(object_event, object_revision),
        "evidence": {
            "relation_cue": cue,
            "target_match": target,
            "date_relation": date_relation,
            "not_an_identity_decision": True,
        },
        "not_an_identity_decision": True,
    }
    if previous_decision == "retracted":
        result["previous_decision"] = "retracted"
    return result


def discover_relationship_candidates(
    database_path: str | Path,
    *,
    limit: int = 100,
    min_score: float = 0.60,
) -> dict:
    """Discover explicit, date-overlapping project-to-conference relation candidates.

    The current event snapshots are read in query-only mode. Exact source
    revisions are attached to every candidate. Existing accepted/rejected
    decisions for the same pair, predicate and revisions are not re-suggested.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000")
    if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
        raise ValueError("--min-score must be finite and between 0 and 1")

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

        reviewed: dict[tuple[str, int, str, int, str], str] = {}
        decision_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='event_relationship_decisions'"
        ).fetchone()
        if decision_table is not None:
            decisions = conn.execute(
                "SELECT subject_event_id, subject_revision, object_event_id, "
                "object_revision, relation_type, decision "
                "FROM event_relationship_decisions ORDER BY rowid"
            ).fetchall()
            for row in decisions:
                key = (
                    row["subject_event_id"],
                    row["subject_revision"],
                    row["object_event_id"],
                    row["object_revision"],
                    row["relation_type"],
                )
                reviewed[key] = row["decision"]
    finally:
        conn.close()

    eligible: list[tuple[dict, int]] = []
    invalid_dated_records = 0
    for row in rows:
        item = json.loads(row["payload_json"])
        if item.get("event_id") != row["event_id"] or item.get("provider") != row["provider"]:
            raise ValueError("stored event identity disagrees with SQLite provenance")
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
        eligible.append((item, row["revision"]))

    eligible.sort(
        key=lambda pair: (
            pair[0]["start_date"],
            pair[0]["end_date"],
            pair[0]["event_id"],
        )
    )
    conference_count = sum(_is_conference(item) for item, _ in eligible)
    examined_pairs = 0
    reviewed_excluded = 0
    candidates_by_relationship_id: dict[str, dict] = {}
    reviewed_excluded_keys: set[tuple[str, int, str, int, str]] = set()

    for index, (left, left_revision) in enumerate(eligible):
        left_end = date.fromisoformat(left["end_date"])
        for right, right_revision in eligible[index + 1:]:
            if date.fromisoformat(right["start_date"]) > left_end:
                break
            if left["provider"] == right["provider"]:
                continue
            examined_pairs += 1

            orientations = (
                (left, left_revision, right, right_revision),
                (right, right_revision, left, left_revision),
            )
            for subject, subject_revision, object_event, object_revision in orientations:
                if _is_conference(subject) or not _is_conference(object_event):
                    continue
                date_evidence = _date_overlap(subject, object_event)
                if date_evidence is None:
                    continue
                cues = _extract_relation_cues(subject)
                if not cues:
                    continue
                for cue in cues:
                    target = _target_match(object_event, cue)
                    if target is None:
                        continue
                    key = (
                        subject["event_id"],
                        subject_revision,
                        object_event["event_id"],
                        object_revision,
                        cue["relation_type"],
                    )
                    previous_decision = reviewed.get(key)
                    candidate = _candidate(
                        subject,
                        subject_revision,
                        object_event,
                        object_revision,
                        cue,
                        target,
                        date_evidence,
                        min_score,
                        previous_decision if previous_decision == "retracted" else None,
                    )
                    if candidate is None:
                        continue
                    if previous_decision in {"accepted", "rejected"}:
                        reviewed_excluded_keys.add(key)
                        continue
                    previous_candidate = candidates_by_relationship_id.get(
                        candidate["relationship_id"]
                    )
                    if (
                        previous_candidate is None
                        or candidate["score"] > previous_candidate["score"]
                    ):
                        candidates_by_relationship_id[candidate["relationship_id"]] = candidate

    reviewed_excluded = len(reviewed_excluded_keys)
    candidates = list(candidates_by_relationship_id.values())
    candidates.sort(
        key=lambda item: (
            -item["score"],
            item["subject"]["event_id"],
            item["object"]["event_id"],
            item["relation_type"],
        )
    )
    relation_counts = Counter(item["relation_type"] for item in candidates)
    return {
        "status": "diagnosed",
        "mode": "review_only",
        "scope": "dated_cross_provider_project_to_conference_pairs",
        "examined": len(rows),
        "eligible_with_explicit_calendar_dates": len(eligible),
        "eligible_conference_records": conference_count,
        "invalid_dated_records": invalid_dated_records,
        "overlapping_cross_provider_pairs_examined": examined_pairs,
        "minimum_score": min_score,
        "candidates_total": len(candidates),
        "returned": min(len(candidates), limit),
        "reviewed_candidates_excluded": reviewed_excluded,
        "candidates_by_relation_type": dict(sorted(relation_counts.items())),
        "candidates": candidates[:limit],
        "source_requests": 0,
        "source_events_modified": 0,
        "database_rows_modified": 0,
        "canonical_identity_modified": False,
        "automatic_relationship_creation": False,
        "limitations": [
            "Scores are deterministic evidence-ranking values, not probabilities.",
            "Only explicit relation language and a dated conference target are considered.",
            "The target must match distinctive title anchors inside the relation-cue snippet; "
            "known target locations must also match.",
            "A candidate is not a verified relationship; human review is required.",
            "Only current source snapshots with explicit day or range dates are compared.",
        ],
    }
