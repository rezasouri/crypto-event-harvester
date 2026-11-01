"""Bounded HTTP pagination of CryptoEvents.global's public Load-more listing.

The homepage already contains the first 20 calendar entries, plus featured
cards. Subsequent batches are provided by the site's public filters.php form
endpoint with category 16 and a UTC-midnight start-date filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .providers.cryptoevents import BASE_URL, CryptoEventsError, CryptoEventsEvent, fetch_listing

PAGE_SIZE = 20
MAX_PAGES = 5
AJAX_URL = BASE_URL + "wp-content/themes/cryptoevents/ajax/filters.php"
UTC_OFFSET = re.compile(r"\(UTC(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})\)")


@dataclass(frozen=True)
class PageResult:
    events: list[CryptoEventsEvent]
    pages_fetched: int
    ajax_requests: int
    stop_reason: str


def _clean(value: object) -> str:
    """Clean public display text without preserving markup or HTML entities."""
    if not isinstance(value, str):
        return ""
    return unescape(BeautifulSoup(value, "html.parser").get_text(" ", strip=True)).strip()


def _parse_timezone(value: object) -> timezone | None:
    """Use only an explicit UTC offset in the source payload; never guess a zone."""
    match = UTC_OFFSET.search(value) if isinstance(value, str) else None
    if match is None:
        return None
    hours, minutes = int(match["hours"]), int(match["minutes"])
    if hours > 14 or minutes >= 60 or (hours == 14 and minutes):
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    if match["sign"] == "-":
        delta = -delta
    return timezone(delta)


def _local_date(value: object, zone: timezone | None) -> str | None:
    if zone is None:
        return None
    try:
        timestamp = int(str(value))
        if not 1_000_000_000 <= timestamp <= 4_102_444_800:
            return None
        return datetime.fromtimestamp(timestamp, UTC).astimezone(zone).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_ajax_records(data: object) -> list[CryptoEventsEvent]:
    """Convert one public AJAX JSON array into existing native event records.

    Reject a different API shape rather than silently returning an empty crawl.
    """
    if not isinstance(data, list):
        raise CryptoEventsError("unexpected CryptoEvents AJAX response (expected a list)")
    records: dict[str, CryptoEventsEvent] = {}
    for post in data:
        if not isinstance(post, dict):
            continue
        url = post.get("link")
        if not isinstance(url, str):
            continue
        parts = urlsplit(url)
        slug = parts.path.strip("/")
        if (
            parts.scheme != "https"
            or parts.hostname != "cryptoevents.global"
            or parts.port not in (None, 443)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", slug)
        ):
            continue
        title = _clean(post.get("title"))
        if not title:
            continue
        zone = _parse_timezone(post.get("timezone"))
        start = _local_date(post.get("start_date"), zone)
        end = _local_date(post.get("end_date"), zone)
        if start is None or end is None or end < start:
            start = end = None
            precision = "unknown"
            label = "Date not verified"
        elif start == end:
            precision = "day"
            label = start
        else:
            precision = "range"
            label = f"{start} – {end}"
        event = CryptoEventsEvent(
            event_id=f"cryptoevents:{slug}",
            source="cryptoevents",
            source_url=f"{BASE_URL}{slug}/",
            listing_url=BASE_URL,
            title=title,
            category="conference",
            location=_clean(post.get("city")) or None,
            description=_clean(post.get("excerpt")) or None,
            date_text=label,
            start_date=start,
            end_date=end,
            date_precision=precision,
            featured=False,
        )
        records[event.event_id] = event
    return list(records.values())


def fetch_ajax_page(
    offset: int, *, start_day_utc: int, timeout_seconds: float = 20
) -> tuple[list[CryptoEventsEvent], int]:
    """Return parsed records and the unfiltered raw count; no retries."""
    if offset < PAGE_SIZE or offset % PAGE_SIZE:
        raise ValueError("offset must be a positive page-size multiple")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    response = httpx.post(
        AJAX_URL,
        data={
            "action": "index",
            "offset": str(offset),
            "postperpage": str(PAGE_SIZE),
            "categories[]": "16",
            "index_filters[start_date]": str(start_day_utc),
        },
        headers={
            "Referer": BASE_URL,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
        follow_redirects=False,
        timeout=timeout_seconds,
    )
    if response.status_code != 200:
        raise CryptoEventsError(f"CryptoEvents pagination HTTP {response.status_code}")
    try:
        raw = response.json()
    except ValueError as exc:
        raise CryptoEventsError("CryptoEvents pagination did not return JSON") from exc
    records = parse_ajax_records(raw)
    return records, len(raw)


def fetch_paginated_listing(
    *, max_pages: int = 1, timeout_seconds: float = 20, engine: str = "http",
    start_day_utc: int | None = None,
) -> PageResult:
    """First listing plus at most max_pages-1 AJAX pages, deduplicated by event ID.

    Once fetched, parsed events can be synced to the existing SQLite store;
    repeated runs are idempotent, but this is not a persistent cursor.
    """
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
    if max_pages > 1 and engine != "http":
        raise ValueError("multi-page scraping uses HTTP only; use --pages 1 for browser engines")
    first = fetch_listing(engine=engine, timeout_seconds=timeout_seconds)
    found = {event.event_id: event for event in first}
    if max_pages == 1:
        return PageResult(list(found.values()), 1, 0, "page_limit")

    start = start_day_utc
    if start is None:
        now = datetime.now(UTC)
        start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    pages, requests, reason = 1, 0, "page_limit"
    for page in range(1, max_pages):
        offset = page * PAGE_SIZE
        new_batch, raw_count = fetch_ajax_page(
            offset, start_day_utc=start, timeout_seconds=timeout_seconds
        )
        requests += 1
        pages += 1
        new_records = 0
        for event in new_batch:
            previous = found.get(event.event_id)
            if previous is None:
                found[event.event_id] = event
                new_records += 1
            elif previous.featured and not event.featured:
                found[event.event_id] = event
        if not new_batch:
            reason = "empty_page"
            break
        if new_records == 0:
            reason = "no_new_ids"
            break
        if raw_count < PAGE_SIZE:
            reason = "partial_page"
            break
    return PageResult(list(found.values()), pages, requests, reason)
