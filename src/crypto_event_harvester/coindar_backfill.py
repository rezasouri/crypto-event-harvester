"""Checkpointed, bounded Coindar historical discovery.

Every successful page is synchronized to the existing SQLite event store
before advancing the page cursor. A process interruption can repeat at most
the last successful page, with idempotent event revision reconciliation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .coindar_pagination import (
    MAX_PAGES,
    _search_params,
    next_page_url,
    past_search_url,
)
from .normalization import normalize_events
from .probe import fetch_page, summarize_page
from .providers.coindar import CoindarError, parse_listing
from .storage import _connect, sync_events

_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS coindar_backfill_windows (
    id INTEGER PRIMARY KEY,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    next_url TEXT,
    next_page INTEGER NOT NULL CHECK (next_page >= 1),
    pages_completed INTEGER NOT NULL CHECK (pages_completed >= 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'exhausted', 'stopped')),
    stop_reason TEXT,
    UNIQUE(window_start, window_end)
);
CREATE TABLE IF NOT EXISTS coindar_backfill_seen (
    window_id INTEGER NOT NULL REFERENCES coindar_backfill_windows(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    in_window INTEGER NOT NULL CHECK (in_window IN (0, 1)),
    PRIMARY KEY(window_id, event_id)
);
"""


@dataclass(frozen=True, slots=True)
class BackfillResult:
    window_start: str
    window_end: str
    state: str
    stop_reason: str | None
    pages_this_run: int
    total_pages: int
    next_page: int | None
    newly_discovered_ids: int
    inserted: int
    updated: int
    unchanged: int
    stale_skipped: int

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _dates(window_start: str, window_end: str) -> tuple[str, str]:
    try:
        first, last = date.fromisoformat(window_start), date.fromisoformat(window_end)
    except ValueError as exc:
        raise ValueError("--from-date and --to-date must be YYYY-MM-DD") from exc
    if first > last:
        raise ValueError("--from-date must not be after --to-date")
    if first.isoformat() != window_start or last.isoformat() != window_end:
        raise ValueError("--from-date and --to-date must be canonical YYYY-MM-DD")
    return window_start, window_end


def _open_db(path: str | Path, *, create: bool) -> sqlite3.Connection:
    conn = _connect(Path(path), create=create)
    try:
        if create:
            with conn:
                conn.executescript(_ARCHIVE_SCHEMA)
        return conn
    except Exception:
        conn.close()
        raise


def _window(conn: sqlite3.Connection, first: str, last: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM coindar_backfill_windows WHERE window_start = ? AND window_end = ?",
        (first, last),
    ).fetchone()


def _checked_next_url(url: str, *, first: str, page: int) -> None:
    params = _search_params(url)
    baseline = _search_params(past_search_url(first))
    if (
        params is None or baseline is None
        or int(params["page"]) != page
        or {k: v for k, v in params.items() if k != "page"}
        != {k: v for k, v in baseline.items() if k != "page"}
    ):
        raise ValueError("Stored checkpoint URL does not match the historical filter window")


def backfill_status(
    database_path: str | Path, *, from_date: str, to_date: str
) -> dict:
    """Read the checkpoint without fetching any website or creating a database."""
    first, last = _dates(from_date, to_date)
    conn = _open_db(database_path, create=False)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='coindar_backfill_windows'"
        ).fetchone()
        if not exists:
            return {"window_start": first, "window_end": last, "state": "not_started"}
        row = _window(conn, first, last)
        if row is None:
            return {"window_start": first, "window_end": last, "state": "not_started"}
        stats = conn.execute(
            "SELECT COUNT(*) AS seen, COALESCE(SUM(in_window), 0) AS kept "
            "FROM coindar_backfill_seen WHERE window_id = ?", (row["id"],)
        ).fetchone()
        return {
            "window_start": first, "window_end": last,
            "state": row["state"], "stop_reason": row["stop_reason"],
            "pages_completed": row["pages_completed"],
            "next_page": row["next_page"] if row["state"] == "active" else None,
            "next_url": row["next_url"] if row["state"] == "active" else None,
            "unique_ids_seen": stats["seen"], "unique_in_window": stats["kept"],
        }
    finally:
        conn.close()


def reset_backfill(database_path: str | Path, *, from_date: str, to_date: str) -> bool:
    """Forget only a range cursor/seen-set, leaving historic event data untouched."""
    first, last = _dates(from_date, to_date)
    conn = _open_db(database_path, create=False)
    try:
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='coindar_backfill_windows'"
            ).fetchone()
            if not exists:
                return False
            row = _window(conn, first, last)
            if row is None:
                return False
            conn.execute("DELETE FROM coindar_backfill_windows WHERE id = ?", (row["id"],))
            return True
    finally:
        conn.close()


def run_backfill(
    database_path: str | Path, *, from_date: str, to_date: str,
    pages: int = 2, timeout_seconds: float = 20,
) -> BackfillResult:
    """Fetch at most pages listing HTML pages, with durable per-page progress.

    This is bounded discovery, not a proof that every historic event in the
    date interval was enumerated. The source date filter's semantics and
    ordering may change over time.
    """
    first, last = _dates(from_date, to_date)
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError(f"--pages must be between 1 and {MAX_PAGES}")
    if timeout_seconds <= 0:
        raise ValueError("--timeout must be positive")
    conn = _open_db(database_path, create=True)
    done, added, inserted, updated, unchanged, stale = 0, 0, 0, 0, 0, 0
    try:
        with conn:
            row = _window(conn, first, last)
            if row is None:
                conn.execute(
                    "INSERT INTO coindar_backfill_windows "
                    "(window_start, window_end, next_url, next_page, pages_completed, state) "
                    "VALUES (?, ?, ?, 1, 0, 'active')",
                    (first, last, past_search_url(first)),
                )
                row = _window(conn, first, last)
        if row["state"] != "active":
            return BackfillResult(
                first, last, row["state"], row["stop_reason"], 0,
                row["pages_completed"], None, 0, 0, 0, 0, 0,
            )

        for _ in range(pages):
            row = _window(conn, first, last)
            if row["state"] != "active":
                break
            current_url, page_number = row["next_url"], row["next_page"]
            _checked_next_url(current_url, first=first, page=page_number)
            snapshot = fetch_page(current_url, engine="http", timeout_seconds=timeout_seconds)
            info = summarize_page(snapshot)
            if info["issue"] or (snapshot.status_code is not None and snapshot.status_code >= 400):
                raise CoindarError(
                    f"Coindar historical page {page_number} unavailable: "
                    f"{info['issue'] or snapshot.status_code}"
                )
            if urlsplit(snapshot.final_url).scheme != "https":
                raise CoindarError("Coindar historical page redirected away from HTTPS")
            _checked_next_url(snapshot.final_url, first=first, page=page_number)
            parsed = parse_listing(snapshot.html)
            next_link = next_page_url(
                snapshot.html, current_url=snapshot.final_url, page_number=page_number
            )
            if next_link is not None:
                _checked_next_url(next_link, first=first, page=page_number + 1)
            known = {
                item["event_id"] for item in conn.execute(
                    "SELECT event_id FROM coindar_backfill_seen WHERE window_id = ?",
                    (row["id"],),
                )
            }
            new = [event for event in parsed if event.event_id not in known]
            in_window = [
                event for event in parsed
                if event.event_date is not None and first <= event.event_date <= last
            ]
            # Persist events before checkpoint. A crash between commits will
            # repeat one page rather than skip uncommitted event payloads.
            result = sync_events(database_path, normalize_events(in_window))
            inserted += result.inserted
            updated += result.updated
            unchanged += result.unchanged
            stale += result.stale_skipped
            added += len(new)
            done += 1
            if not new and page_number > 1:
                state, reason = "stopped", "no_new_ids"
            elif next_link is None:
                state, reason = "exhausted", "no_next_link"
            else:
                state, reason = "active", None
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO coindar_backfill_seen "
                    "(window_id, event_id, in_window) VALUES (?, ?, ?)",
                    [
                        (row["id"], event.event_id, int(
                            event.event_date is not None
                            and first <= event.event_date <= last
                        ))
                        for event in parsed
                    ],
                )
                conn.execute(
                    "UPDATE coindar_backfill_windows SET next_url = ?, next_page = ?, "
                    "pages_completed = pages_completed + 1, state = ?, stop_reason = ? "
                    "WHERE id = ?",
                    (next_link if state == "active" else None,
                     page_number + 1, state, reason, row["id"]),
                )
            if state != "active":
                break

        row = _window(conn, first, last)
        return BackfillResult(
            first, last, row["state"], row["stop_reason"], done,
            row["pages_completed"],
            row["next_page"] if row["state"] == "active" else None,
            added, inserted, updated, unchanged, stale,
        )
    finally:
        conn.close()
