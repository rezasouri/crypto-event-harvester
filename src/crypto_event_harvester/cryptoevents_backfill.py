"""Durable, bounded CryptoEvents.global listing pagination.

A crawl is keyed by its frozen UTC start-day filter. Each successfully parsed
page is synchronized to the shared SQLite event store before its cursor moves.
An interruption can therefore repeat at most the last page; event sync is
idempotent and no homepage GET is repeated when resuming.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .normalization import normalize_events
from .pagination import MAX_PAGES, PAGE_SIZE, fetch_ajax_page
from .providers.cryptoevents import fetch_listing
from .storage import _connect, sync_events

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cryptoevents_crawl_runs (
    start_date_utc TEXT PRIMARY KEY,
    start_day_epoch INTEGER NOT NULL,
    next_offset INTEGER NOT NULL CHECK (next_offset >= 0),
    pages_completed INTEGER NOT NULL CHECK (pages_completed >= 0),
    unique_ids_seen INTEGER NOT NULL CHECK (unique_ids_seen >= 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'complete')),
    stop_reason TEXT,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cryptoevents_crawl_seen (
    start_date_utc TEXT NOT NULL REFERENCES cryptoevents_crawl_runs(start_date_utc)
        ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    PRIMARY KEY (start_date_utc, event_id)
);
"""


@dataclass(frozen=True)
class CrawlResult:
    start_date_utc: str
    state: str
    pages_this_run: int
    total_pages: int
    homepage_gets: int
    ajax_posts: int
    new_ids_this_run: int
    unique_ids_seen: int
    inserted: int
    updated: int
    unchanged: int
    stale_skipped: int
    next_offset: int
    stop_reason: str | None

    def to_dict(self) -> dict:
        return {
            "start_date_utc": self.start_date_utc,
            "state": self.state,
            "pages_this_run": self.pages_this_run,
            "total_pages": self.total_pages,
            "requests": {
                "homepage_gets": self.homepage_gets,
                "ajax_posts": self.ajax_posts,
                "total": self.homepage_gets + self.ajax_posts,
            },
            "new_ids_this_run": self.new_ids_this_run,
            "unique_ids_seen": self.unique_ids_seen,
            "sync": {
                "inserted": self.inserted,
                "updated": self.updated,
                "unchanged": self.unchanged,
                "stale_skipped": self.stale_skipped,
            },
            "next_offset": self.next_offset,
            "stop_reason": self.stop_reason,
        }


def _date_value(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--start-date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("--start-date must be YYYY-MM-DD")
    return parsed


def _epoch_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def _open_db(path: str | Path, *, create: bool) -> sqlite3.Connection:
    conn = _connect(Path(path), create=create)
    if create:
        with conn:
            conn.executescript(_SCHEMA)
    return conn


def crawl_status(database_path: str | Path, *, start_date: str | None = None) -> dict:
    day = _date_value(start_date).isoformat()
    conn = _open_db(database_path, create=False)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='cryptoevents_crawl_runs'"
        ).fetchone()
        if not exists:
            return {
                "start_date_utc": day,
                "state": "not_started",
                "pages_completed": 0,
                "next_offset": 0,
                "unique_ids_seen": 0,
                "stop_reason": None,
            }
        row = conn.execute(
            "SELECT start_day_epoch, next_offset, pages_completed, unique_ids_seen, "
            "state, stop_reason FROM cryptoevents_crawl_runs WHERE start_date_utc = ?",
            (day,),
        ).fetchone()
        if row is None:
            return {
                "start_date_utc": day,
                "state": "not_started",
                "pages_completed": 0,
                "next_offset": 0,
                "unique_ids_seen": 0,
                "stop_reason": None,
            }
        return {
            "start_date_utc": day,
            "start_day_epoch": row["start_day_epoch"],
            "state": row["state"],
            "pages_completed": row["pages_completed"],
            "next_offset": row["next_offset"],
            "unique_ids_seen": row["unique_ids_seen"],
            "stop_reason": row["stop_reason"],
        }
    finally:
        conn.close()


def reset_crawl(database_path: str | Path, *, start_date: str | None = None) -> bool:
    day = _date_value(start_date).isoformat()
    conn = _open_db(database_path, create=False)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='cryptoevents_crawl_runs'"
        ).fetchone()
        if not exists:
            return False
        with conn:
            cursor = conn.execute(
                "DELETE FROM cryptoevents_crawl_runs WHERE start_date_utc = ?", (day,)
            )
        return cursor.rowcount > 0
    finally:
        conn.close()


def run_crawl(
    database_path: str | Path,
    *,
    start_date: str | None = None,
    pages: int = 2,
    timeout_seconds: float = 20,
) -> CrawlResult:
    """Run at most pages listing requests, resuming from the saved offset."""
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError(f"--pages must be between 1 and {MAX_PAGES}")
    if timeout_seconds <= 0:
        raise ValueError("--timeout must be positive")
    day = _date_value(start_date)
    day_text = day.isoformat()
    epoch = _epoch_midnight(day)
    conn = _open_db(database_path, create=True)
    pages_this_run = homepage_gets = ajax_posts = new_this_run = 0
    inserted = updated = unchanged = stale = 0
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO cryptoevents_crawl_runs "
                "(start_date_utc, start_day_epoch, next_offset, pages_completed, "
                "unique_ids_seen, state, stop_reason, updated_at_utc) "
                "VALUES (?, ?, 0, 0, 0, 'active', NULL, ?)",
                (day_text, epoch, datetime.now(UTC).isoformat()),
            )
        while pages_this_run < pages:
            row = conn.execute(
                "SELECT start_day_epoch, next_offset, pages_completed, unique_ids_seen, "
                "state, stop_reason FROM cryptoevents_crawl_runs WHERE start_date_utc = ?",
                (day_text,),
            ).fetchone()
            if row["state"] == "complete":
                break

            offset = row["next_offset"]
            if offset == 0:
                events = fetch_listing(engine="http", timeout_seconds=timeout_seconds)
                raw_count = None
                next_offset = PAGE_SIZE
                homepage_gets += 1
            else:
                events, raw_count = fetch_ajax_page(
                    offset,
                    start_day_utc=row["start_day_epoch"],
                    timeout_seconds=timeout_seconds,
                )
                next_offset = offset + PAGE_SIZE
                ajax_posts += 1

            pages_this_run += 1
            existing = {
                item["event_id"]
                for item in conn.execute(
                    "SELECT event_id FROM cryptoevents_crawl_seen WHERE start_date_utc = ?",
                    (day_text,),
                ).fetchall()
            }
            page_ids = {event.event_id for event in events}
            fresh_ids = page_ids - existing

            # Persist normalized event rows first. If the process stops before
            # the cursor transaction below, this same page is safe to replay.
            synced = sync_events(database_path, normalize_events(events))
            inserted += synced.inserted
            updated += synced.updated
            unchanged += synced.unchanged
            stale += synced.stale_skipped

            stop_reason = None
            state = "active"
            if raw_count is not None:
                if raw_count == 0:
                    state, stop_reason = "complete", "empty_page"
                elif not fresh_ids:
                    state, stop_reason = "complete", "no_new_ids"
                elif raw_count < PAGE_SIZE:
                    state, stop_reason = "complete", "partial_page"

            now = datetime.now(UTC).isoformat()
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO cryptoevents_crawl_seen "
                    "(start_date_utc, event_id) VALUES (?, ?)",
                    [(day_text, event_id) for event_id in sorted(page_ids)],
                )
                conn.execute(
                    "UPDATE cryptoevents_crawl_runs SET next_offset = ?, "
                    "pages_completed = pages_completed + 1, unique_ids_seen = "
                    "(SELECT COUNT(*) FROM cryptoevents_crawl_seen WHERE start_date_utc = ?), "
                    "state = ?, stop_reason = ?, updated_at_utc = ? "
                    "WHERE start_date_utc = ?",
                    (next_offset, day_text, state, stop_reason, now, day_text),
                )
            new_this_run += len(fresh_ids)
            if state == "complete":
                break

        final = conn.execute(
            "SELECT next_offset, pages_completed, unique_ids_seen, state, stop_reason "
            "FROM cryptoevents_crawl_runs WHERE start_date_utc = ?",
            (day_text,),
        ).fetchone()
        return CrawlResult(
            start_date_utc=day_text,
            state=final["state"],
            pages_this_run=pages_this_run,
            total_pages=final["pages_completed"],
            homepage_gets=homepage_gets,
            ajax_posts=ajax_posts,
            new_ids_this_run=new_this_run,
            unique_ids_seen=final["unique_ids_seen"],
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            stale_skipped=stale,
            next_offset=final["next_offset"],
            stop_reason=final["stop_reason"],
        )
    finally:
        conn.close()
