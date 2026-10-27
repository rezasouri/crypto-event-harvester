from unittest.mock import Mock

import pytest

from crypto_event_harvester.probe import PageSnapshot, fetch_page, summarize_page


def test_summarize_html_with_selector():
    snapshot = PageSnapshot(
        "https://example.test/calendar",
        "https://example.test/calendar",
        "http",
        200,
        "<html><title>Calendar</title><body><article class='event'>A</article>"
        "<article class='event'>B</article></body></html>",
    )
    result = summarize_page(snapshot, selector="article.event")
    assert result["title"] == "Calendar"
    assert result["selector_matches"] == 2
    assert result["issue"] is None


@pytest.mark.parametrize(
    ("status", "html", "expected"),
    [
        (429, "<h1>Too many requests</h1>", "rate_limited"),
        (403, "<h1>Forbidden</h1>", "access_restricted"),
        (200, "<h1>Checking your browser</h1>", "security_challenge"),
    ],
)
def test_identify_access_issues(status, html, expected):
    snapshot = PageSnapshot("https://example.test", "https://example.test", "http", status, html)
    assert summarize_page(snapshot)["issue"] == expected


def test_fetch_http_follow_redirects_and_keep_status(monkeypatch):
    response = Mock(status_code=200, text="<title>Calendar</title>", url="https://example.test/ok")
    client = Mock()
    client.get.return_value = response
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    created = []

    def client_factory(**kwargs):
        created.append(kwargs)
        return client

    monkeypatch.setattr("crypto_event_harvester.probe.httpx.Client", client_factory)
    snapshot = fetch_page("https://example.test/calendar")
    assert snapshot.final_url == "https://example.test/ok"
    assert snapshot.status_code == 200
    assert created[0]["follow_redirects"] is True
    client.get.assert_called_once_with("https://example.test/calendar")


@pytest.mark.parametrize("url", ["", "file:///tmp/example.html", "javascript:alert(1)"])
def test_reject_non_http_urls(url):
    with pytest.raises(ValueError, match="HTTP"):
        fetch_page(url)


def test_reject_non_positive_timeout():
    with pytest.raises(ValueError, match="timeout"):
        fetch_page("https://example.test", timeout_seconds=0)


def test_reject_unknown_engine():
    with pytest.raises(ValueError, match="engine"):
        fetch_page("https://example.test", engine="unsupported")
