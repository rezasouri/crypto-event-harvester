"""Coindar bounded pagination: URL policy, historical window and request budgets."""

from urllib.parse import parse_qs, urlsplit

import pytest

from crypto_event_harvester import coindar_pagination as pages
from crypto_event_harvester.probe import PageSnapshot

BASE = "https://www.coindar.org/en"
FIXED = "2026-11-01"
UPCOMING_PAGE2 = (
    "/en/search?page=2&text=&start=2026-11-01&cats=&im=&rs=0&fav=0"
    "&coins=&cap_from=0&cap_to=9999999999999&vol_from=0"
    "&vol_to=9999999999999&ex=&sort=0&order=0"
)


def _html(extra_link=None, *, offset=0):
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "coindar_listing_sample.html"
    content = fixture.read_text(encoding="utf-8")
    if offset:
        for number in ("155001", "155002", "155003", "155004"):
            content = content.replace(number, str(int(number) + offset))
    if extra_link:
        content = content.replace(
            "</main>", f"<nav class='pagination'><a href='{extra_link}'>"
            f"{parse_qs(urlsplit(extra_link).query)['page'][0]}</a></nav></main>",
        )
    return content


def _fake_fetch(pages_html: list[str], *, expected=None):
    called = []

    def fetch(url, *, engine, timeout_seconds):
        called.append(url)
        if expected is not None:
            assert url == expected[len(called) - 1]
        assert engine == "http"
        assert len(called) <= len(pages_html)
        return PageSnapshot(url, url, engine, 200, pages_html[len(called) - 1])

    return fetch, called


def test_known_past_link_is_website_html_not_api():
    url = pages.past_search_url(FIXED)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.scheme == "https"
    assert parts.netloc == "www.coindar.org"
    assert parts.path == "/en/search"
    assert query["sort"] == ["3"] and query["order"] == ["1"]
    assert query["start"] == [FIXED] and query["page"] == ["1"]
    assert "access_token" not in parts.query


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "yesterday"])
def test_invalid_historical_start_date_fails_before_io(bad, monkeypatch):
    monkeypatch.setattr(pages, "fetch_page", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError, match="start-date"):
        pages.fetch_coindar_pages(section="ended", start_date=bad)


def test_homepage_discovers_numbered_next_link_without_guessing():
    page = pages.next_page_url(
        _html(UPCOMING_PAGE2), current_url=BASE, page_number=1
    )
    assert page == "https://www.coindar.org" + UPCOMING_PAGE2


def test_page_one_sort_navigation_is_not_mistaken_for_page_two():
    html = (
        "<a href='/en/search?page=1&sort=3&order=1&start=2026-11-01'>2</a>"
        "<a href='https://other.example/en/search?page=2&sort=0&order=0&start=2026-11-01'>2</a>"
        "<a href='/en/search?page=2&sort=0&order=0&start=2026-11-01'>2</a>"
    )
    assert pages.next_page_url(html, current_url=BASE, page_number=1) == (
        "https://www.coindar.org/en/search?page=2&sort=0&order=0&start=2026-11-01"
    )


def test_filter_switch_is_rejected_between_pages():
    current = pages.past_search_url(FIXED).replace("page=1", "page=2")
    alternate = current.replace("page=2", "page=3").replace("sort=3", "sort=0")
    html = f"<a href='{alternate}'>3</a>"
    assert pages.next_page_url(html, current_url=current, page_number=2) is None


@pytest.mark.parametrize("bad", [
    "https://other.example/en/search?page=2&start=2026-11-01&sort=0&order=0",
    "http://www.coindar.org/en/search?page=2&start=2026-11-01&sort=0&order=0",
    "https://www.coindar.org:9999/en/search?page=2&start=2026-11-01&sort=0&order=0",
    "https://www.coindar.org/en/search?page=2&page=3&start=2026-11-01&sort=0&order=0",
    "https://www.coindar.org/en/event/something-999?page=2&start=2026-11-01&sort=0&order=0",
    "https://www.coindar.org/en/search?page=2&start=2026-11-01&sort=0&order=0&access_token=secret",
])
def test_unsafe_or_unrelated_next_links_are_rejected(bad):
    assert pages.next_page_url(
        f"<a href='{bad}'>2</a>", current_url=BASE, page_number=1
    ) is None


def test_two_upcoming_pages_one_get_each_no_detail_fetch(monkeypatch):
    first = _html(UPCOMING_PAGE2)
    second = _html(offset=1000)
    fetch, calls = _fake_fetch([first, second])
    monkeypatch.setattr(pages, "fetch_page", fetch)
    outcome = pages.fetch_coindar_pages(max_pages=2)
    assert len(outcome.events) == 8
    assert len({e.event_id for e in outcome.events}) == 8
    assert outcome.pages_fetched == 2
    assert outcome.extra_requests == 1
    assert outcome.stop_reason == "page_limit"
    assert calls == [BASE, "https://www.coindar.org" + UPCOMING_PAGE2]


def test_zero_new_ids_stops_early(monkeypatch):
    fetch, calls = _fake_fetch([_html(UPCOMING_PAGE2), _html(UPCOMING_PAGE2)])
    monkeypatch.setattr(pages, "fetch_page", fetch)
    outcome = pages.fetch_coindar_pages(max_pages=5)
    assert outcome.stop_reason == "no_new_ids"
    assert len(outcome.events) == 4
    assert outcome.extra_requests == 1
    assert len(calls) == 2


def test_no_link_stops_with_one_get(monkeypatch):
    content = _html().replace('<a href="/en?page=2">2</a>', "")
    fetch, calls = _fake_fetch([content])
    monkeypatch.setattr(pages, "fetch_page", fetch)
    outcome = pages.fetch_coindar_pages(max_pages=5)
    assert outcome.stop_reason == "no_next_link"
    assert outcome.extra_requests == 0
    assert len(calls) == 1


def test_past_search_pagination_uses_exact_fixed_filter(monkeypatch):
    page1 = pages.past_search_url(FIXED)
    page2 = page1.replace("page=1", "page=2")
    fetch, calls = _fake_fetch(
        [_html(page2), _html(offset=1000)],
        expected=[page1, page2],
    )
    monkeypatch.setattr(pages, "fetch_page", fetch)
    result = pages.fetch_coindar_pages(
        section="ended", start_date=FIXED, max_pages=2
    )
    assert len(result.events) == 8
    assert calls == [page1, page2]


@pytest.mark.parametrize("max_pages", [0, 6])
def test_page_budget_enforced_without_io(monkeypatch, max_pages):
    monkeypatch.setattr(pages, "fetch_page", lambda *a, **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="pages"):
        pages.fetch_coindar_pages(max_pages=max_pages)


def test_browser_engine_rejected_for_multipage(monkeypatch):
    monkeypatch.setattr(pages, "fetch_page", lambda *a, **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="engine http"):
        pages.fetch_coindar_pages(max_pages=2, engine="playwright")


def test_broken_second_page_aborts_not_partial_success(monkeypatch):
    fetch, calls = _fake_fetch([
        _html(UPCOMING_PAGE2), "<html><title>Broken layout</title></html>"
    ])
    monkeypatch.setattr(pages, "fetch_page", fetch)
    with pytest.raises(RuntimeError, match="event links missing"):
        pages.fetch_coindar_pages(max_pages=2)
    assert len(calls) == 2


def test_403_second_page_is_not_retried(monkeypatch):
    calls = []

    def fetch(url, *, engine, timeout_seconds):
        calls.append(url)
        return PageSnapshot(url, url, engine, 200 if len(calls) == 1 else 403,
                            _html(UPCOMING_PAGE2) if len(calls) == 1 else "")

    monkeypatch.setattr(pages, "fetch_page", fetch)
    with pytest.raises(RuntimeError, match="unavailable"):
        pages.fetch_coindar_pages(max_pages=3)
    assert len(calls) == 2


def test_search_page_redirect_offsite_rejected(monkeypatch):
    def fetch(url, *, engine, timeout_seconds):
        return PageSnapshot(url, "https://other.example/en/search", engine, 200, _html())

    monkeypatch.setattr(pages, "fetch_page", fetch)
    with pytest.raises(RuntimeError, match="redirected"):
        pages.fetch_coindar_pages()
