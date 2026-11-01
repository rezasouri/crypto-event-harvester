"""Bounded, low-request Coindar calendar and past-event pagination.

Only follows a visible, same-host numbered Next page link, carrying forward
the same search filters. No guessing page URLs, bypass or event-detail GETs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .probe import fetch_page, summarize_page
from .providers.coindar import (
    BASE_URL,
    LISTING_URL,
    CoindarError,
    CoindarEvent,
    parse_listing,
)

MAX_PAGES = 5
SEARCH_PATH = "/en/search"
ALLOWED_SEARCH_KEYS = frozenset({
    "page", "text", "start", "cats", "im", "rs", "fav", "coins", "cap_from",
    "cap_to", "vol_from", "vol_to", "ex", "sort", "order",
})


@dataclass(frozen=True)
class CoindarPageResult:
    events: list[CoindarEvent]
    pages_fetched: int
    extra_requests: int
    stop_reason: str


def past_search_url(start_date: str | None = None) -> str:
    """Build the documented website's *public HTML* Past Events filter.

    This is the same sort/order link observed in the public 'Past Events' tab,
    never the token-gated Coindar API. A fixed start_date makes backfills
    reproducible across days and avoids inferred archive completeness.
    """
    if start_date is None:
        day = datetime.now(UTC).date()
    else:
        try:
            day = date.fromisoformat(start_date)
        except ValueError as exc:
            raise ValueError("--start-date must be YYYY-MM-DD") from exc
    query = [
        ("page", "1"), ("text", ""), ("start", day.isoformat()),
        ("cats", ""), ("im", ""), ("rs", "0"), ("fav", "0"), ("coins", ""),
        ("cap_from", "0"), ("cap_to", "9999999999999"),
        ("vol_from", "0"), ("vol_to", "9999999999999"),
        ("ex", ""), ("sort", "3"), ("order", "1"),
    ]
    return f"{BASE_URL}{SEARCH_PATH}?{urlencode(query)}"


def _search_params(url: str) -> dict[str, str] | None:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"www.coindar.org", "coindar.org"}
            or parts.port not in (None, 443)
            or parts.path != SEARCH_PATH
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        items = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None
    keys = [k for k, _ in items]
    if len(keys) != len(set(keys)) or not set(keys) <= ALLOWED_SEARCH_KEYS:
        return None
    params = dict(items)
    if not {"page", "start", "sort", "order"}.issubset(params):
        return None
    if not params["page"].isdigit() or not params["start"]:
        return None
    return params


def next_page_url(html: str, *, current_url: str, page_number: int) -> str | None:
    """Select only the visibly linked next page of the same filter window.

    On /en homepage, the first search link is the transition to page 2.
    Subsequent pages must keep *all* filter parameters, except 'page'.
    This prevents accidentally selecting unrelated sort/Recently Added links.
    """
    if page_number < 1:
        raise ValueError("page_number must be positive")
    current = urlsplit(current_url)
    if current.path not in {"/en", SEARCH_PATH}:
        raise ValueError("Coindar pagination source must be an event listing")
    current_params = _search_params(current_url) if current.path == SEARCH_PATH else None
    if current.path == SEARCH_PATH and current_params is None:
        raise ValueError("Invalid current Coindar search URL")

    want = page_number + 1
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]"):
        # A numbered pagination control is authoritative; a filter/sort
        # switch with page=2 is not.
        text = anchor.get_text(" ", strip=True)
        if text != str(want) and "next" not in str(anchor.get("rel", "")).lower():
            continue
        target = urljoin(current_url, anchor["href"])
        params = _search_params(target)
        if params is None or int(params["page"]) != want:
            continue
        if current_params is not None:
            prior = {key: val for key, val in current_params.items() if key != "page"}
            after = {key: val for key, val in params.items() if key != "page"}
            if prior != after:
                continue
        return target
    return None


def fetch_coindar_pages(
    *, section: str = "upcoming", max_pages: int = 1,
    engine: str = "http", timeout_seconds: float = 20,
    start_date: str | None = None,
) -> CoindarPageResult:
    """One initial listing GET + at most max_pages-1 visible pagination GETs.

    A failed subsequent page aborts the whole command, rather than storing
    an incomplete page range as if it were a successful full run.
    """
    if section not in {"upcoming", "ended"}:
        raise ValueError("Coindar supports upcoming or ended (Past Events)")
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"--pages must be between 1 and {MAX_PAGES}")
    if timeout_seconds <= 0:
        raise ValueError("--timeout must be positive")
    if start_date is not None and section != "ended":
        raise ValueError("--start-date is only supported for Coindar ended")
    if max_pages > 1 and engine != "http":
        raise ValueError("Multi-page Coindar scraping requires --engine http")

    url = LISTING_URL if section == "upcoming" else past_search_url(start_date)
    events: dict[str, CoindarEvent] = {}
    visited: set[str] = set()
    reason = "page_limit"
    fetched = 0
    for page in range(1, max_pages + 1):
        if url in visited:
            reason = "repeated_page_url"
            break
        visited.add(url)
        snapshot = fetch_page(url, engine=engine, timeout_seconds=timeout_seconds)
        info = summarize_page(snapshot)
        if info["issue"] or (snapshot.status_code is not None and snapshot.status_code >= 400):
            raise CoindarError(
                f"Coindar page {page} unavailable: {info['issue'] or snapshot.status_code}"
            )
        landed = urlsplit(snapshot.final_url)
        if landed.scheme != "https" or landed.hostname not in {
            "www.coindar.org", "coindar.org",
        } or landed.path not in {"/en", SEARCH_PATH}:
            raise CoindarError("Coindar listing redirected outside the expected public calendar")
        if page > 1 and _search_params(snapshot.final_url) is None:
            raise CoindarError("Coindar pagination redirect changed the search URL")
        parsed = parse_listing(snapshot.html)
        fetched += 1
        gained = 0
        for event in parsed:
            if event.event_id not in events:
                events[event.event_id] = event
                gained += 1
        if gained == 0:
            reason = "no_new_ids"
            break
        if page == max_pages:
            break
        candidate = next_page_url(
            snapshot.html, current_url=snapshot.final_url, page_number=page
        )
        if candidate is None:
            reason = "no_next_link"
            break
        url = candidate

    return CoindarPageResult(
        events=list(events.values()),
        pages_fetched=fetched,
        extra_requests=max(0, fetched - 1),
        stop_reason=reason,
    )
