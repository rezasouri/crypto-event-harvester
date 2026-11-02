"""SQLite-backed incremental event storage with auditable revisions.

A source event is identified by its provider-prefixed event_id. Repeated
identical observations only advance last_seen_at_utc, whereas changed
observations append an immutable revision. Entire batches are transactional.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .normalization import UnifiedEvent

DB_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_provider ON events(provider);
CREATE TABLE IF NOT EXISTS event_revisions (
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    observed_at_utc TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (event_id, revision)
);
"""

_RECONCILIATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_events (
    canonical_event_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_event_members (
    canonical_event_id TEXT NOT NULL REFERENCES canonical_events(canonical_event_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    source_revision_at_link INTEGER NOT NULL CHECK (source_revision_at_link >= 1),
    linked_at_utc TEXT NOT NULL,
    PRIMARY KEY (canonical_event_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_canonical_members_canonical
    ON canonical_event_members(canonical_event_id);
CREATE TRIGGER IF NOT EXISTS trg_canonical_member_provider
BEFORE INSERT ON canonical_event_members
WHEN EXISTS (
    SELECT 1 FROM canonical_event_members old
    JOIN events old_event ON old_event.event_id = old.event_id
    JOIN events new_event ON new_event.event_id = NEW.event_id
    WHERE old.canonical_event_id = NEW.canonical_event_id
      AND old_event.provider = new_event.provider
)
BEGIN
    SELECT RAISE(ABORT, 'canonical identity already has a member from this provider');
END;
CREATE TABLE IF NOT EXISTS reconciliation_decisions (
    decision_id TEXT PRIMARY KEY,
    left_event_id TEXT NOT NULL,
    right_event_id TEXT NOT NULL,
    left_revision INTEGER NOT NULL CHECK (left_revision >= 1),
    right_revision INTEGER NOT NULL CHECK (right_revision >= 1),
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    strategy TEXT NOT NULL CHECK (strategy IN ('exact', 'similarity')),
    evidence_json TEXT NOT NULL,
    canonical_event_id TEXT REFERENCES canonical_events(canonical_event_id),
    note TEXT,
    decided_at_utc TEXT NOT NULL,
    CHECK (left_event_id < right_event_id),
    CHECK ((decision = 'accepted' AND canonical_event_id IS NOT NULL)
        OR (decision = 'rejected' AND canonical_event_id IS NULL)),
    FOREIGN KEY (left_event_id, left_revision) REFERENCES event_revisions(event_id, revision),
    FOREIGN KEY (right_event_id, right_revision) REFERENCES event_revisions(event_id, revision),
    UNIQUE (left_event_id, right_event_id, left_revision, right_revision, decision, strategy)
);
CREATE INDEX IF NOT EXISTS ix_reconciliation_decisions_left
    ON reconciliation_decisions(left_event_id, decided_at_utc);
CREATE INDEX IF NOT EXISTS ix_reconciliation_decisions_right
    ON reconciliation_decisions(right_event_id, decided_at_utc);
CREATE TRIGGER IF NOT EXISTS trg_reconciliation_decisions_no_update
BEFORE UPDATE ON reconciliation_decisions
BEGIN
    SELECT RAISE(ABORT, 'reconciliation decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_reconciliation_decisions_no_delete
BEFORE DELETE ON reconciliation_decisions
BEGIN
    SELECT RAISE(ABORT, 'reconciliation decisions are append-only');
END;
"""


_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_membership_actions (
    action_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('unlinked', 'split')),
    canonical_event_id TEXT NOT NULL REFERENCES canonical_events(canonical_event_id),
    target_canonical_event_id TEXT REFERENCES canonical_events(canonical_event_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    source_revision_at_link INTEGER NOT NULL CHECK (source_revision_at_link >= 1),
    current_source_revision INTEGER NOT NULL CHECK (current_source_revision >= 1),
    linked_at_utc TEXT NOT NULL,
    note TEXT,
    acted_at_utc TEXT NOT NULL,
    CHECK (
        (action = 'unlinked' AND target_canonical_event_id IS NULL)
        OR
        (action = 'split' AND target_canonical_event_id IS NOT NULL
            AND target_canonical_event_id <> canonical_event_id)
    )
);
CREATE INDEX IF NOT EXISTS ix_membership_actions_event
    ON canonical_membership_actions(event_id, acted_at_utc);
CREATE INDEX IF NOT EXISTS ix_membership_actions_canonical
    ON canonical_membership_actions(canonical_event_id, acted_at_utc);
CREATE INDEX IF NOT EXISTS ix_membership_actions_operation
    ON canonical_membership_actions(operation_id);
CREATE TRIGGER IF NOT EXISTS trg_canonical_member_provider_update
BEFORE UPDATE OF canonical_event_id ON canonical_event_members
WHEN EXISTS (
    SELECT 1 FROM canonical_event_members old
    JOIN events old_event ON old_event.event_id = old.event_id
    JOIN events new_event ON new_event.event_id = NEW.event_id
    WHERE old.canonical_event_id = NEW.canonical_event_id
      AND old.event_id <> OLD.event_id
      AND old_event.provider = new_event.provider
)
BEGIN
    SELECT RAISE(ABORT, 'canonical identity already has a member from this provider');
END;
CREATE TRIGGER IF NOT EXISTS trg_canonical_member_delete_audited
BEFORE DELETE ON canonical_event_members
WHEN NOT EXISTS (
    SELECT 1 FROM canonical_membership_actions a
    WHERE a.action = 'unlinked'
      AND a.canonical_event_id = OLD.canonical_event_id
      AND a.event_id = OLD.event_id
      AND a.source_revision_at_link = OLD.source_revision_at_link
      AND a.linked_at_utc = OLD.linked_at_utc
)
BEGIN
    SELECT RAISE(ABORT, 'canonical membership removal requires an audit action');
END;
CREATE TRIGGER IF NOT EXISTS trg_canonical_member_move_audited
BEFORE UPDATE OF canonical_event_id ON canonical_event_members
WHEN NEW.canonical_event_id <> OLD.canonical_event_id
 AND NOT EXISTS (
    SELECT 1 FROM canonical_membership_actions a
    WHERE a.action = 'split'
      AND a.canonical_event_id = OLD.canonical_event_id
      AND a.target_canonical_event_id = NEW.canonical_event_id
      AND a.event_id = OLD.event_id
      AND a.source_revision_at_link = OLD.source_revision_at_link
      AND a.linked_at_utc = OLD.linked_at_utc
)
BEGIN
    SELECT RAISE(ABORT, 'canonical membership move requires a split audit action');
END;
CREATE TRIGGER IF NOT EXISTS trg_membership_action_matches_current_member
BEFORE INSERT ON canonical_membership_actions
WHEN NOT EXISTS (
    SELECT 1 FROM canonical_event_members m
    JOIN events e USING(event_id)
    WHERE m.canonical_event_id = NEW.canonical_event_id
      AND m.event_id = NEW.event_id
      AND m.source_revision_at_link = NEW.source_revision_at_link
      AND m.linked_at_utc = NEW.linked_at_utc
      AND e.revision = NEW.current_source_revision
)
BEGIN
    SELECT RAISE(ABORT, 'membership audit action must match a current member snapshot');
END;
CREATE TRIGGER IF NOT EXISTS trg_membership_actions_no_update
BEFORE UPDATE ON canonical_membership_actions
BEGIN
    SELECT RAISE(ABORT, 'canonical membership actions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_membership_actions_no_delete
BEFORE DELETE ON canonical_membership_actions
BEGIN
    SELECT RAISE(ABORT, 'canonical membership actions are append-only');
END;
"""

@dataclass(frozen=True, slots=True)
class SyncResult:
    inserted: int
    updated: int
    unchanged: int
    stale_skipped: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged + self.stale_skipped

    def to_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "stale_skipped": self.stale_skipped,
            "total": self.total,
        }


def _observed_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat(timespec="microseconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("observed_at_utc must be an ISO8601 timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at_utc requires an explicit timezone offset")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if not create and not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if create:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, DB_VERSION):
            conn.close()
            raise RuntimeError(f"unsupported event database version: {version}")
        if version == 0:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + _SCHEMA + _RECONCILIATION_SCHEMA + _LIFECYCLE_SCHEMA
                + f"\nPRAGMA user_version = {DB_VERSION};\nCOMMIT;"
            )
        elif version == 1:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + _RECONCILIATION_SCHEMA + _LIFECYCLE_SCHEMA
                + f"\nPRAGMA user_version = {DB_VERSION};\nCOMMIT;"
            )
        elif version == 2:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + _LIFECYCLE_SCHEMA
                + f"\nPRAGMA user_version = {DB_VERSION};\nCOMMIT;"
            )
    return conn


def sync_events(
    database_path: str | Path,
    events: list[UnifiedEvent],
    *,
    observed_at_utc: str | None = None,
) -> SyncResult:
    """Upsert one atomic snapshot; old observations can never roll back new data.

    Missing records are not deleted: a short/failed scrape is not evidence that
    an event disappeared from its source.
    """
    timestamp = _observed_at(observed_at_utc)
    ids = [event.event_id for event in events]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate event_id in incoming snapshot")
    if not all(isinstance(event, UnifiedEvent) for event in events):
        raise TypeError("sync_events requires validated UnifiedEvent records")
    conn = _connect(Path(database_path), create=True)
    try:
        inserted = updated = unchanged = stale = 0
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for event in events:
                payload = json.dumps(
                    event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                row = conn.execute(
                    "SELECT payload_sha256, revision, last_seen_at_utc "
                    "FROM events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO events (event_id, provider, source_event_id, payload_json, "
                        "payload_sha256, revision, first_seen_at_utc, last_seen_at_utc, updated_at_utc) "
                        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            event.event_id, event.provider, event.source_event_id, payload,
                            digest, timestamp, timestamp, timestamp,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO event_revisions (event_id, revision, observed_at_utc, "
                        "payload_sha256, payload_json) VALUES (?, 1, ?, ?, ?)",
                        (event.event_id, timestamp, digest, payload),
                    )
                    inserted += 1
                    continue
                if timestamp < row["last_seen_at_utc"] or (
                    timestamp == row["last_seen_at_utc"] and digest != row["payload_sha256"]
                ):
                    stale += 1
                    continue
                if digest == row["payload_sha256"]:
                    if timestamp > row["last_seen_at_utc"]:
                        conn.execute(
                            "UPDATE events SET last_seen_at_utc = ? WHERE event_id = ?",
                            (timestamp, event.event_id),
                        )
                    unchanged += 1
                    continue
                revision = row["revision"] + 1
                conn.execute(
                    "UPDATE events SET provider = ?, source_event_id = ?, payload_json = ?, "
                    "payload_sha256 = ?, revision = ?, last_seen_at_utc = ?, updated_at_utc = ? "
                    "WHERE event_id = ?",
                    (
                        event.provider, event.source_event_id, payload, digest, revision,
                        timestamp, timestamp, event.event_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO event_revisions (event_id, revision, observed_at_utc, "
                    "payload_sha256, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (event.event_id, revision, timestamp, digest, payload),
                )
                updated += 1
        return SyncResult(inserted, updated, unchanged, stale)
    finally:
        conn.close()


def store_status(database_path: str | Path) -> dict[str, object]:
    """Return counts without fetching event payloads."""
    conn = _connect(Path(database_path), create=False)
    try:
        totals = conn.execute(
            "SELECT COUNT(*) AS events, COALESCE(SUM(revision), 0) AS revisions, "
            "MIN(first_seen_at_utc) AS earliest_seen, MAX(last_seen_at_utc) AS latest_seen "
            "FROM events"
        ).fetchone()
        providers = conn.execute(
            "SELECT provider, COUNT(*) AS events FROM events GROUP BY provider ORDER BY provider"
        ).fetchall()
        database_version = conn.execute("PRAGMA user_version").fetchone()[0]
        return {
            "database_version": database_version,
            "events": totals["events"],
            "revisions": totals["revisions"],
            "earliest_seen_at_utc": totals["earliest_seen"],
            "latest_seen_at_utc": totals["latest_seen"],
            "providers": {row["provider"]: row["events"] for row in providers},
        }
    finally:
        conn.close()


def list_events(
    database_path: str | Path, *, provider: str | None = None, limit: int = 100
) -> list[dict]:
    """Read latest snapshots in deterministic event ID order."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    conn = _connect(Path(database_path), create=False)
    try:
        query = (
            "SELECT event_id, revision, first_seen_at_utc, last_seen_at_utc, updated_at_utc, "
            "payload_json FROM events"
        )
        if provider:
            rows = conn.execute(
                query + " WHERE provider = ? ORDER BY event_id LIMIT ?", (provider, limit)
            ).fetchall()
        else:
            rows = conn.execute(query + " ORDER BY event_id LIMIT ?", (limit,)).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "revision": row["revision"],
                "first_seen_at_utc": row["first_seen_at_utc"],
                "last_seen_at_utc": row["last_seen_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
                "event": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def event_history(database_path: str | Path, event_id: str) -> list[dict]:
    """Return all immutable source-observation revisions for one event."""
    if not event_id.strip():
        raise ValueError("event_id is required")
    conn = _connect(Path(database_path), create=False)
    try:
        rows = conn.execute(
            "SELECT revision, observed_at_utc, payload_sha256, payload_json "
            "FROM event_revisions WHERE event_id = ? ORDER BY revision",
            (event_id,),
        ).fetchall()
        return [
            {
                "revision": row["revision"],
                "observed_at_utc": row["observed_at_utc"],
                "payload_sha256": row["payload_sha256"],
                "event": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
