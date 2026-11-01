"""Coindar HTML parser tests; fixtures are representative and offline."""
from pathlib import Path

import pytest

from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.providers.coindar import (
    BASE_URL,
    LISTING_URL,
    CoindarError,
    _event_identity,
    fetch_listing,
    parse_listing,
)

FIXTURE = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"


def _events():
    return parse_listing(FIXTURE.read_text(encoding="utf-8"))


def test_parses_event_cards_and_deduplicates_by_numeric_source_id():
    events = _events()
    assert len(events) == 4
    assert [event.event_id for event in events] == [
        "coindar:155001", "coindar:155002", "coindar:155003", "coindar:155004"
    ]


def test_extracts_coin_ticker_category_title_description_and_date():
    event = _events()[0]
    assert event.source == "coindar"
    assert event.source_url == "https://www.coindar.org/en/event/rotten-candy-hunt-155001"
    assert event.listing_url == "https://www.coindar.org/en"
    assert event.title == "Rotten Candy Hunt"
    assert event.coin == "Alien Worlds"
    assert event.ticker == "TLM"
    assert event.category == "Contest"
    assert event.description == "Alien Worlds' Rotten Candy Hunt will conclude on November 1st."
    assert event.date_text == "November 1, 2026 UTC"
    assert event.event_date == "2026-11-01"
    assert event.date_precision == "day"
    assert event.date_role == "listed"
    assert event.added_text == "Added 5 days ago"


def test_each_card_uses_its_nearest_preceding_day_heading():
    events = {event.event_id: event for event in _events()}
    assert events["coindar:155002"].event_date == "2026-11-01"
    assert events["coindar:155003"].event_date == "2026-11-02"


def test_unknown_quarter_heading_is_not_promoted_to_a_day():
    event = _events()[-1]
    assert event.event_id == "coindar:155004"
    assert event.date_text == "Q1 2027"
    assert event.event_date is None
    assert event.date_precision == "unknown"
    assert event.date_role == "unknown"


@pytest.mark.parametrize("url", [
    "https://www.coindar.org/en/event/sample-123?utm_source=mail",
    "https://coindar.org/en/event/sample-123#details",
    "/en/event/sample-123/",
])
def test_event_identity_canonicalizes_valid_public_urls(url):
    assert _event_identity(url) == ("123", "https://www.coindar.org/en/event/sample-123")


@pytest.mark.parametrize("url", [
    "http://www.coindar.org/en/event/sample-123",
    "https://coindar.org.evil.example/en/event/sample-123",
    "https://www.coindar.org:444/en/event/sample-123",
    "javascript:alert(1)",
    "/en/event/sample-no-id",
    "/en/coin/sample-123",
])
def test_event_identity_rejects_untrusted_or_non_event_urls(url):
    assert _event_identity(url) is None


def test_navigation_and_foreign_event_links_are_not_emitted():
    ids = {event.event_id for event in _events()}
    assert "coindar:999999" not in ids
    assert "coindar:155005" not in ids
    # Valid event URLs in overview teaser rails are not full calendar cards.
    assert "coindar:160001" not in ids
    assert "coindar:160002" not in ids


@pytest.mark.parametrize("html,message", [
    ("<html><body>Access denied</body></html>", "event links missing"),
    ("<a href='/en/event/unattached-123'>Unattached</a>", "no event cards matched"),
])
def test_structural_failures_are_explicit(html, message):
    with pytest.raises(CoindarError, match=message):
        parse_listing(html)


def test_live_fetch_requests_only_the_listing_page(monkeypatch):
    html = FIXTURE.read_text(encoding="utf-8")
    calls = []

    def fake_fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200, html)

    monkeypatch.setattr("crypto_event_harvester.providers.coindar.fetch_page", fake_fetch)
    events = fetch_listing(engine="http")
    assert len(events) == 4
    assert calls == [LISTING_URL]
    assert BASE_URL == "https://www.coindar.org"


def test_live_fetch_reports_http_403_without_retry_or_bypass(monkeypatch):
    calls = []

    def blocked(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 403, "<title>Just a moment...</title>")

    monkeypatch.setattr("crypto_event_harvester.providers.coindar.fetch_page", blocked)
    with pytest.raises(CoindarError, match="unavailable"):
        fetch_listing()
    assert calls == [LISTING_URL]


def test_page_overview_teasers_do_not_change_listing_count_or_quality():
    events = _events()
    assert len(events) == 4
    assert sum(event.date_precision == "day" for event in events) == 3
    assert all(event.category is not None for event in events)
