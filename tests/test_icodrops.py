from pathlib import Path

import pytest

from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.providers.icodrops import (
    ICODropsError,
    LISTINGS,
    _date_value,
    fetch_listing,
    parse_listing,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_upcoming_real_row_structure_with_unknown_date():
    html = (FIXTURES / "icodrops_upcoming.html").read_text()
    events = parse_listing(html)
    assert len(events) == 2
    clix, space = events
    assert clix.event_id == "icodrops:clix:tge-and-distribution"
    assert clix.project == "CLIX"
    assert clix.ticker == "CLIX"
    assert clix.status == "upcoming"
    assert clix.round == "TGE and Distribution"
    assert clix.category == "Lending"
    assert clix.event_date is None
    assert clix.date_text == "Upcoming"
    assert clix.date_precision == "unknown"
    assert clix.pre_valuation == "$10 M"
    assert clix.total_raised is None
    assert space.project == "Spaceway Token"
    assert space.ticker == "SPWAY"
    assert space.event_date == "2026-11-11"
    assert space.date_precision == "day"


def test_ended_date_and_round():
    event = parse_listing((FIXTURES / "icodrops_ended.html").read_text(), section="ended")[0]
    assert event.project == "Jumper"
    assert event.round == "ICM on Legion"
    assert event.event_date == "2026-09-29"
    assert event.date_role == "listed"
    assert event.total_raised == "$3 M"
    assert event.source_url == "https://icodrops.com/jumper/"


@pytest.mark.parametrize(
    ("raw", "date", "precision", "role"),
    [
        ("Upcoming", None, "unknown", "unknown"),
        ("3d left", None, "unknown", "unknown"),
        ("from Q1, 2024", None, "quarter", "start"),
        ("from Jul 24, 2026", "2026-07-24", "day", "start"),
        ("Sep 29, 2026", "2026-09-29", "day", "listed"),
        ("Mar 40, 2026", None, "unknown", "unknown"),
    ],
)
def test_do_not_fabricate_dates(raw, date, precision, role):
    assert _date_value(raw) == (date, precision, role)


def test_deduplicates_same_round():
    html = (FIXTURES / "icodrops_ended.html").read_text()
    doubled = html.replace("</ul>", html.split("<li", 1)[1].split("</li>")[0].join([]) + "</ul>")
    # Insert a second row with identical project/round before the list closes.
    start = html.index("<li")
    end = html.index("</li>") + len("</li>")
    doubled = html.replace("</ul>", html[start:end] + "</ul>")
    assert len(parse_listing(doubled, section="ended")) == 1


def test_missing_event_table_is_failure():
    with pytest.raises(ICODropsError, match="table"):
        parse_listing("<html><body>Just a moment...</body></html>")


def test_empty_valid_table_does_not_claim_scrape_success():
    with pytest.raises(ICODropsError, match="no valid"):
        parse_listing("<ul id='table-list'><li class='Tbl-Row'></li></ul>")


def test_rejects_foreign_or_unexpected_links():
    html = (FIXTURES / "icodrops_upcoming.html").read_text().replace(
        'href="/clix/"', 'href="https://other.example/clix/"'
    )
    events = parse_listing(html)
    assert len(events) == 1
    assert events[0].project == "Spaceway Token"


@pytest.mark.parametrize(("http_status", "content"), [(403, "Forbidden"), (429, "Too many requests")])
def test_fetch_does_not_parse_blocked_html(monkeypatch, http_status, content):
    monkeypatch.setattr(
        "crypto_event_harvester.providers.icodrops.fetch_page",
        lambda url, **kw: PageSnapshot(url, url, "http", http_status, content),
    )
    with pytest.raises(ICODropsError, match="unavailable"):
        fetch_listing("upcoming")


def test_fetch_one_listing(monkeypatch):
    calls = []
    html = (FIXTURES / "icodrops_ended.html").read_text()

    def fake_fetch(url, **kwargs):
        calls.append(url)
        return PageSnapshot(url, url, "http", 200, html)

    monkeypatch.setattr("crypto_event_harvester.providers.icodrops.fetch_page", fake_fetch)
    events = fetch_listing("ended")
    assert len(events) == 1
    assert calls == [LISTINGS["ended"]]
