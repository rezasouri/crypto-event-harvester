"""CryptoEvents.global public calendar listing scraper (one HTML GET per listing)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..probe import fetch_page, summarize_page

BASE_URL = "https://cryptoevents.global/"
LISTING_URL = BASE_URL

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_PATTERN = re.compile(
    r"^(?P<month>[A-Za-z]{3,9})\s+(?P<start>\d{1,2})"
    r"(?:\s*[-–—]\s*(?:(?P<end_month>[A-Za-z]{3,9})\s+)?(?P<end>\d{1,2}))?"
    r",?\s+(?P<year>\d{4})$"
)


class CryptoEventsError(RuntimeError):
    """A failed or structurally invalid CryptoEvents listing."""


@dataclass(frozen=True)
class CryptoEventsEvent:
    event_id: str
    source: str
    source_url: str
    listing_url: str
    title: str
    category: str
    location: str | None
    description: str | None
    date_text: str
    start_date: str | None
    end_date: str | None
    date_precision: str
    featured: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _month(value: str | None) -> int | None:
    return MONTHS.get(value[:3].lower()) if value else None


def parse_event_dates(value: str) -> tuple[str | None, str | None, str]:
    """Parse explicitly shown day/day-range, without inventing timezone or clock time."""
    match = DATE_PATTERN.fullmatch(" ".join(value.split()))
    if not match:
        return None, None, "unknown"
    start_month = _month(match["month"])
    end_month = _month(match["end_month"]) if match["end_month"] else start_month
    if start_month is None or end_month is None:
        return None, None, "unknown"
    year = int(match["year"])
    try:
        start = date(year, start_month, int(match["start"]))
        if match["end"] is None:
            return start.isoformat(), start.isoformat(), "day"
        end = date(year, end_month, int(match["end"]))
    except ValueError:
        return None, None, "unknown"
    # Cross-year dates are ambiguous when the listing supplies only one year.
    if end < start:
        return None, None, "unknown"
    return start.isoformat(), end.isoformat(), "range"


def _event_url(card) -> str | None:
    anchors = card.select("a[href]")
    selected = next(
        (a for a in anchors if "show more" in _text(a).lower()),
        anchors[-1] if anchors else None,
    )
    if selected is None:
        return None
    full_url = urljoin(BASE_URL, selected.get("href", ""))
    parts = urlsplit(full_url)
    if parts.scheme != "https" or parts.hostname != "cryptoevents.global":
        return None
    if parts.port not in (None, 443):
        return None
    slug = parts.path.strip("/")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", slug):
        return None
    return f"{BASE_URL}{slug}/"


def parse_listing(html: str) -> list[CryptoEventsEvent]:
    """Parse genuine event cards, excluding site navigation and announcement cards."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.event_blk")
    if not cards:
        raise CryptoEventsError("CryptoEvents event cards missing; source DOM may have changed")
    results: dict[str, CryptoEventsEvent] = {}
    for card in cards:
        title = _text(card.select_one(".event_name"))
        date_text = _text(card.select_one(".event_date"))
        url = _event_url(card)
        if not title or not date_text or not url:
            continue
        start, end, precision = parse_event_dates(date_text)
        slug = urlsplit(url).path.strip("/")
        description_node = card.select_one(".event_short_description")
        if description_node is not None:
            # Do not include CTA text in the event synopsis.
            for link in description_node.select("a"):
                if "show more" in _text(link).lower():
                    link.decompose()
        event = CryptoEventsEvent(
            event_id=f"cryptoevents:{slug}",
            source="cryptoevents",
            source_url=url,
            listing_url=LISTING_URL,
            title=title,
            category="conference",
            location=_text(card.select_one(".event_location")) or None,
            description=_text(description_node) or None,
            date_text=date_text,
            start_date=start,
            end_date=end,
            date_precision=precision,
            featured="featured_event" in card.get("class", []),
        )
        previous = results.get(event.event_id)
        # Prefer regular listing card over featured duplicate; it represents the
        # normal listing page and may contain updated data.
        if previous is None or (previous.featured and not event.featured):
            results[event.event_id] = event
    if not results:
        raise CryptoEventsError("CryptoEvents cards yielded no valid event records")
    return list(results.values())


def fetch_listing(*, engine: str = "http", timeout_seconds: float = 20) -> list[CryptoEventsEvent]:
    """Download one public listing page; no individual event pages or paid API."""
    snapshot = fetch_page(LISTING_URL, engine=engine, timeout_seconds=timeout_seconds)
    report = summarize_page(snapshot)
    if report["issue"]:
        raise CryptoEventsError(f"CryptoEvents listing unavailable: {report['issue']}")
    if snapshot.status_code is not None and snapshot.status_code >= 400:
        raise CryptoEventsError(f"CryptoEvents listing HTTP {snapshot.status_code}")
    return parse_listing(snapshot.html)
