"""ICO Drops HTML listing scraper: one request per listing section, no paid API."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..probe import fetch_page, summarize_page

BASE_URL = "https://icodrops.com"
LISTINGS = {
    "upcoming": f"{BASE_URL}/category/upcoming-ico/",
    "active": f"{BASE_URL}/category/active-ico/",
    "ended": f"{BASE_URL}/category/ended-ico/",
}
MISSING = {"", "—", "-", "–", "n/a", "none"}
DATE_RE = re.compile(r"^(?:from\s+)?([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})$", re.I)
QUARTER_RE = re.compile(r"^(?:from\s+)?Q[1-4][,\s]+\d{4}$", re.I)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{1,13}$")


class ICODropsError(RuntimeError):
    """An inaccessible or structurally invalid ICO Drops listing."""


@dataclass(frozen=True)
class ICODropsEvent:
    event_id: str
    source: str
    source_url: str
    listing_url: str
    section: str
    title: str
    project: str
    ticker: str | None
    round: str
    category: str | None
    status: str
    date_text: str
    event_date: str | None
    date_precision: str
    date_role: str
    total_raised: str | None
    pre_valuation: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _text(node) -> str:
    return " ".join(node.stripped_strings) if node else ""


def _cell(row, suffix: str) -> str:
    return _text(row.select_one(f".Tbl-Row__item--{suffix}"))


def _optional(text: str) -> str | None:
    return text if text.strip().lower() not in MISSING else None


def _date_value(label: str) -> tuple[str | None, str, str]:
    """Never turn 'Upcoming', '3d left', or Q1 into an invented calendar day."""
    match = DATE_RE.fullmatch(label.strip())
    if match:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                date = datetime.strptime(match.group(1), fmt).date()
                return date.isoformat(), "day", "start" if label.lower().startswith("from ") else "listed"
            except ValueError:
                continue
    if QUARTER_RE.fullmatch(label.strip()):
        return None, "quarter", "start" if label.lower().startswith("from ") else "listed"
    return None, "unknown", "unknown"


def _project_name_and_ticker(label: str) -> tuple[str, str | None]:
    pieces = label.split()
    if len(pieces) >= 2 and TICKER_RE.fullmatch(pieces[-1]):
        return " ".join(pieces[:-1]), pieces[-1]
    return label, None


def _event_url(row) -> str | None:
    anchor = row.select_one(".Tbl-Row__item--avt a[href]") or row.select_one(
        ".Tbl-Row__item--project a[href]"
    )
    if anchor is None:
        return None
    url = urljoin(BASE_URL, anchor.get("href", ""))
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"icodrops.com", "www.icodrops.com"}:
        return None
    slug = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", slug):
        return None
    return f"{BASE_URL}/{slug}/"


def parse_listing(html: str, *, section: str = "upcoming") -> list[ICODropsEvent]:
    """Extract actual listing *rows*, not guessed event links from the whole page."""
    if section not in LISTINGS:
        raise ValueError(f"Unsupported ICO Drops section: {section}")
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("ul#table-list > li.Tbl-Row")
    if not rows:
        raise ICODropsError("ICO Drops event table was not found; page or selectors may have changed")
    records: dict[str, ICODropsEvent] = {}
    for row in rows:
        url = _event_url(row)
        project_label = _cell(row, "project")
        round_name = _cell(row, "round")
        if not url or not project_label or not round_name:
            continue
        project, ticker = _project_name_and_ticker(project_label)
        if not project:
            continue
        date_text = _cell(row, "date")
        event_date, precision, date_role = _date_value(date_text)
        category = _optional(_cell(row, "categories"))
        slug = urlsplit(url).path.strip("/")
        round_slug = re.sub(r"[^a-z0-9]+", "-", round_name.lower()).strip("-")
        if not round_slug:
            continue
        event_id = f"icodrops:{slug}:{round_slug}"
        records[event_id] = ICODropsEvent(
            event_id=event_id,
            source="icodrops",
            source_url=url,
            listing_url=LISTINGS[section],
            section=section,
            title=f"{project} — {round_name}",
            project=project,
            ticker=ticker,
            round=round_name,
            category=category,
            status=section,
            date_text=date_text,
            event_date=event_date,
            date_precision=precision,
            date_role=date_role,
            total_raised=_optional(_cell(row, "raised")),
            pre_valuation=_optional(_cell(row, "pre-valuation")),
        )
    if not records:
        raise ICODropsError("ICO Drops table contained no valid event records")
    return list(records.values())


def fetch_listing(
    section: str = "upcoming", *, engine: str = "http", timeout_seconds: float = 20
) -> list[ICODropsEvent]:
    """Fetch one public page, then parse its event rows. Browser support is optional."""
    if section not in LISTINGS:
        raise ValueError(f"Unsupported ICO Drops section: {section}")
    snapshot = fetch_page(LISTINGS[section], engine=engine, timeout_seconds=timeout_seconds)
    result = summarize_page(snapshot)
    if result["issue"]:
        raise ICODropsError(f"ICO Drops listing unavailable: {result['issue']}")
    if snapshot.status_code is not None and snapshot.status_code >= 400:
        raise ICODropsError(f"ICO Drops returned HTTP {snapshot.status_code}")
    return parse_listing(snapshot.html, section=section)
