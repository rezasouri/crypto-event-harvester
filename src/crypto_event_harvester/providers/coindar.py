"""Coindar public HTML calendar parser.

This provider uses only the public listing HTML. It never calls the token-gated
Coindar API and never visits individual event pages.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..date_precision import coarse_period
from ..probe import fetch_page, summarize_page

BASE_URL = "https://www.coindar.org"
LISTING_URL = f"{BASE_URL}/en"
EVENT_PATH = re.compile(r"^/en/event/(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*?)-(?P<id>\d+)/?$", re.I)
COIN_PATH = re.compile(r"^/en/coin/[A-Za-z0-9][A-Za-z0-9-]*/?$", re.I)
TAG_PATH = re.compile(r"^/en/tag/[A-Za-z0-9][A-Za-z0-9-]*/?$", re.I)
DATE_LABEL = re.compile(
    r"^(?P<date>(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4})(?:\s+UTC)?$",
    re.I,
)
COARSE_DATE_LABEL = re.compile(
    r"^(?:Q[1-4]\s+\d{4}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})(?:\s+UTC)?$",
    re.I,
)
ADDED_LABEL = re.compile(r"^(?:Added\s+.+|Yesterday|Today)$", re.I)
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{1,11}$")


class CoindarError(RuntimeError):
    """Coindar is unavailable or its public event-list structure changed."""


@dataclass(frozen=True)
class CoindarEvent:
    event_id: str
    source: str
    source_url: str
    listing_url: str
    title: str
    coin: str | None
    ticker: str | None
    category: str | None
    description: str | None
    date_text: str
    event_date: str | None
    date_precision: str
    date_role: str
    added_text: str | None
    period_key: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _text(node: Tag | None) -> str:
    return " ".join(node.stripped_strings) if node else ""


def _anchor_label(anchor: Tag) -> str:
    label = _text(anchor)
    if label:
        return label
    image = anchor.find("img", alt=True)
    return image.get("alt", "").strip() if image else ""


def _event_identity(href: object) -> tuple[str, str] | None:
    """Return (numeric source ID, canonical event URL) for a same-host link."""
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        url = urljoin(LISTING_URL + "/", href.strip())
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"www.coindar.org", "coindar.org"}
            or parts.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    match = EVENT_PATH.fullmatch(parts.path)
    if match is None:
        return None
    event_id = match["id"]
    return event_id, f"{BASE_URL}/en/event/{match['slug']}-{event_id}"


def _matching_links(container: Tag, pattern: re.Pattern) -> list[Tag]:
    matches = []
    for anchor in container.select("a[href]"):
        parts = urlsplit(urljoin(LISTING_URL + "/", anchor.get("href", "")))
        if parts.hostname in {"www.coindar.org", "coindar.org"} and pattern.fullmatch(parts.path):
            matches.append(anchor)
    return matches


def _event_container(anchor: Tag, event_id: str) -> Tag | None:
    """Find the nearest card-like ancestor containing this event and coin/tag links.

    Stable public URL paths are preferred over brittle generated CSS classes.
    A page-wide wrapper containing several event IDs is never accepted as a card.
    """
    fallback = None
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
            continue
        event_ids = {
            identity[0]
            for link in parent.select("a[href]")
            if (identity := _event_identity(link.get("href"))) is not None
        }
        if event_ids != {event_id}:
            continue
        coin_links = _matching_links(parent, COIN_PATH)
        tag_links = _matching_links(parent, TAG_PATH)
        text = _text(parent)
        if coin_links or tag_links:
            return parent
        if fallback is None and len(text) >= len(_anchor_label(anchor)) + 20:
            fallback = parent
    return fallback


def _parse_day(value: str) -> tuple[str | None, str, str]:
    match = DATE_LABEL.fullmatch(value.strip())
    if match is None:
        return None, value.strip(), "unknown"
    label = match["date"]
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(label, fmt).date()
            return parsed.isoformat(), value.strip(), "day"
        except ValueError:
            continue
    return None, value.strip(), "unknown"


def _date_for_event(anchor: Tag, container: Tag) -> tuple[str | None, str, str]:
    for node in container.select("[data-date], time[datetime], time"):
        value = node.get("data-date") or node.get("datetime") or _text(node)
        parsed, label, precision = _parse_day(str(value))
        if parsed:
            return parsed, label, precision

    # Upcoming events are grouped under a visible UTC calendar-day heading.
    # Use the closest preceding exact date label; never infer from relative text.
    for previous in container.find_all_previous(string=True):
        value = " ".join(str(previous).split())
        if DATE_LABEL.fullmatch(value):
            parsed, label, precision = _parse_day(value)
            if parsed:
                return parsed, label, precision
        if COARSE_DATE_LABEL.fullmatch(value):
            precision, _key = coarse_period(value)
            return None, value, precision
    return None, "Unknown", "unknown"


def _coin_and_ticker(container: Tag) -> tuple[str | None, str | None]:
    links = _matching_links(container, COIN_PATH)
    anchor = next((item for item in links if _anchor_label(item)), None)
    if anchor is None:
        return None, None
    coin = _anchor_label(anchor)
    parent_text = _text(anchor.parent) if isinstance(anchor.parent, Tag) else coin
    remainder = parent_text.removeprefix(coin).strip()
    candidates = remainder.split()
    ticker = candidates[-1].strip("()[]") if candidates else None
    if ticker and not TICKER.fullmatch(ticker):
        ticker = None
    return coin, ticker


def _category(container: Tag) -> str | None:
    links = _matching_links(container, TAG_PATH)
    return next((_anchor_label(link) for link in links if _anchor_label(link)), None)


def _description(
    container: Tag, *, title: str, coin: str | None, category: str | None,
    date_text: str, added_text: str | None,
) -> str | None:
    for selector in ("p", "[class*='description']", "[class*='summary']"):
        for node in container.select(selector):
            value = _text(node)
            if (
                len(value) >= 20 and value not in {title, coin, category, date_text, added_text}
                and not ADDED_LABEL.fullmatch(value)
            ):
                return value[:4000]

    ignored = {title, coin, category, date_text, added_text}
    fragments = []
    for node in container.find_all(string=True):
        value = " ".join(str(node).split())
        if not value or value in ignored or ADDED_LABEL.fullmatch(value):
            continue
        if value.isdigit() or len(value) < 18:
            continue
        if value not in fragments:
            fragments.append(value)
    result = " ".join(fragments).strip()
    return result[:4000] or None


def _quality(event: CoindarEvent) -> int:
    return sum((
        bool(event.event_date), bool(event.coin), bool(event.ticker),
        bool(event.category), bool(event.description), bool(event.added_text),
    ))


def parse_listing(html: str) -> list[CoindarEvent]:
    """Parse one public Coindar calendar page without following event links."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("a[href]")
    if not any(_event_identity(anchor.get("href")) for anchor in anchors):
        raise CoindarError("Coindar event links missing; access may be blocked or the page changed")

    records: dict[str, CoindarEvent] = {}
    for anchor in anchors:
        identity = _event_identity(anchor.get("href"))
        if identity is None:
            continue
        source_id, source_url = identity
        container = _event_container(anchor, source_id)
        if container is None:
            continue
        title = _anchor_label(anchor)
        if not title:
            continue

        coin, ticker = _coin_and_ticker(container)
        category = _category(container)
        event_date, date_text, precision = _date_for_event(anchor, container)
        added_text = next(
            (
                " ".join(str(item).split())
                for item in container.find_all(string=True)
                if ADDED_LABEL.fullmatch(" ".join(str(item).split()))
            ),
            None,
        )
        description = _description(
            container, title=title, coin=coin, category=category,
            date_text=date_text, added_text=added_text,
        )
        # The homepage also embeds small Recently Added / Trending teaser cards.
        # They link to real events but do not carry the calendar card's category
        # or description. Keep them as link-discovery hints, not parsed events.
        if category is None and description is None:
            continue
        event = CoindarEvent(
            event_id=f"coindar:{source_id}",
            source="coindar",
            source_url=source_url,
            listing_url=LISTING_URL,
            title=title,
            coin=coin,
            ticker=ticker,
            category=category,
            description=description,
            date_text=date_text,
            event_date=event_date,
            date_precision=precision,
            date_role="listed" if precision != "unknown" else "unknown",
            added_text=added_text,
            period_key=coarse_period(date_text)[1]
            if precision in {"month", "quarter"} else None,
        )
        previous = records.get(event.event_id)
        if previous is None or _quality(event) > _quality(previous):
            records[event.event_id] = event

    if not records:
        raise CoindarError(
            "Coindar event links were found, but no event cards matched the public card structure"
        )
    return list(records.values())


def fetch_listing(*, engine: str = "http", timeout_seconds: float = 20) -> list[CoindarEvent]:
    """Fetch exactly one public listing page; never call the token-gated API."""
    snapshot = fetch_page(LISTING_URL, engine=engine, timeout_seconds=timeout_seconds)
    report = summarize_page(snapshot)
    if report["issue"]:
        raise CoindarError(f"Coindar listing unavailable: {report['issue']}")
    if snapshot.status_code is not None and snapshot.status_code >= 400:
        raise CoindarError(f"Coindar listing HTTP {snapshot.status_code}")
    return parse_listing(snapshot.html)
