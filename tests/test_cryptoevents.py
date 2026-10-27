from pathlib import Path

import pytest

from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.providers.cryptoevents import (
    CryptoEventsError,
    LISTING_URL,
    fetch_listing,
    parse_event_dates,
    parse_listing,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cryptoevents_home.html"


def test_real_dom_cards_not_navigation_and_featured_dedup():
    events = parse_listing(FIXTURE.read_text())
    assert len(events) == 4
    madrid, bridge, devcon, unscheduled = events
    assert madrid.title == "Merge Madrid 2026"
    assert madrid.event_id == "cryptoevents:merge-madrid-2026"
    assert madrid.source_url == "https://cryptoevents.global/merge-madrid-2026/"
    assert madrid.start_date == "2026-10-27"
    assert madrid.end_date == "2026-10-29"
    assert madrid.date_precision == "range"
    assert madrid.featured is False
    assert madrid.location == "Madrid"
    assert madrid.description == "MERGE Madrid 2026: institutional crypto and Web3. Show more"
    assert bridge.start_date == bridge.end_date == "2026-10-27"
    assert bridge.date_precision == "day"
    assert bridge.location == "New York, NY"
    assert devcon.source_url == "https://cryptoevents.global/devcon-2026-mumbai/"
    assert unscheduled.start_date is None
    assert unscheduled.end_date is None
    assert unscheduled.date_text == "Coming soon"


@pytest.mark.parametrize(
    ("label", "start", "end", "precision"),
    [
        ("Oct 27-29 2026", "2026-10-27", "2026-10-29", "range"),
        ("Nov 3-6 2026", "2026-11-03", "2026-11-06", "range"),
        ("Oct 27 2026", "2026-10-27", "2026-10-27", "day"),
        ("Oct 27–29, 2026", "2026-10-27", "2026-10-29", "range"),
        ("Nov 29-Dec 2 2026", "2026-11-29", "2026-12-02", "range"),
        ("Dec 31-Jan 2 2027", None, None, "unknown"),
        ("Oct 29-27 2026", None, None, "unknown"),
        ("Feb 29 2025", None, None, "unknown"),
        ("Q1 2027", None, None, "unknown"),
        ("Coming soon", None, None, "unknown"),
        ("Oct 2026", None, None, "unknown"),
    ],
)
def test_parse_date_semantics(label, start, end, precision):
    assert parse_event_dates(label) == (start, end, precision)


def test_invalid_html_rejected():
    with pytest.raises(CryptoEventsError, match="cards missing"):
        parse_listing("<html><body>Consent page</body></html>")


def test_cards_without_required_fields_rejected():
    with pytest.raises(CryptoEventsError, match="no valid"):
        parse_listing("<div class='event_blk'><p class='event_name'>No date</p></div>")


def test_foreign_detail_url_ignored():
    html = FIXTURE.read_text().replace(
        'href="/merge-madrid-2026/"',
        'href="https://other.test/merge-madrid-2026/"',
    )
    events = parse_listing(html)
    assert len(events) == 3
    assert all("merge-madrid" not in x.event_id for x in events)


def test_fetch_listing_one_http_page(monkeypatch):
    requested = []
    html = FIXTURE.read_text()

    def fake_fetch(url, **kwargs):
        requested.append((url, kwargs))
        return PageSnapshot(url, url, "http", 200, html)

    monkeypatch.setattr("crypto_event_harvester.providers.cryptoevents.fetch_page", fake_fetch)
    events = fetch_listing()
    assert len(events) == 4
    assert len(requested) == 1
    assert requested[0][0] == LISTING_URL


@pytest.mark.parametrize("status", (403, 429, 500))
def test_do_not_treat_inaccessible_page_as_events(monkeypatch, status):
    monkeypatch.setattr(
        "crypto_event_harvester.providers.cryptoevents.fetch_page",
        lambda url, **kw: PageSnapshot(url, url, "http", status, FIXTURE.read_text()),
    )
    with pytest.raises(CryptoEventsError, match="unavailable|HTTP"):
        fetch_listing()
