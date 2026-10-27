from pathlib import Path

import pytest

from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.source_feasibility import (
    SOURCES,
    candidate_event_links,
    inspect_snapshot,
    probe_source,
    probe_sources,
)


def snapshot(spec, html, status=200, engine="http"):
    return PageSnapshot(spec.listing_url, spec.listing_url, engine, status, html)


@pytest.mark.parametrize(
    ("name", "href", "marker"),
    [
        ("coindar", "/en/event/example-update-1234", "Cryptocurrency Calendar"),
        ("icodrops", "/my-new-project/", "Upcoming"),
        ("cryptoevents", "/merge-madrid-2026/", "blockchain"),
    ],
)
def test_detect_likely_event_detail_link(name, href, marker):
    spec = SOURCES[name]
    html = f"<h1>{marker}</h1><a href='{href}'>Sample Event</a>"
    result = inspect_snapshot(spec, snapshot(spec, html))
    assert result["status"] == "listing_evidence"
    assert result["candidate_detail_link_count"] == 1
    assert result["verified_event_count"] is None
    assert result["candidate_detail_link_samples"][0]["anchor_text"] == "Sample Event"


def test_deduplicates_same_event_link_and_ignores_other_hosts():
    spec = SOURCES["coindar"]
    html = ("<a href='/en/event/abc-123'>One</a>"
            "<a href='https://www.coindar.org/en/event/abc-123?track=1'>One again</a>"
            "<a href='https://bad.coindar.org/en/event/bad'>Off-site</a>"
            "<a href='/en/event/abc-456'>Two</a>")
    n, samples = candidate_event_links(html, spec=spec)
    assert n == 2
    assert len(samples) == 2


def test_excluded_navigation_not_counted():
    spec = SOURCES["cryptoevents"]
    count, _ = candidate_event_links("<a href='/about/'>About</a>", spec=spec)
    assert count == 0


@pytest.mark.parametrize("http_status", [403, 429])
def test_status_blocked_not_treated_as_event_evidence(http_status):
    spec = SOURCES["coindar"]
    html = "<h1>Cryptocurrency Calendar</h1><a href='/en/event/sale-123'>Sale</a>"
    result = inspect_snapshot(spec, snapshot(spec, html, http_status))
    assert result["status"] == "blocked"


def test_auto_fallback_only_for_missing_listing_evidence(monkeypatch):
    spec = SOURCES["coindar"]
    engines = []

    def fake_fetch(url, *, engine, timeout_seconds):
        engines.append(engine)
        if engine == "http":
            return snapshot(spec, "<h1>Cryptocurrency Calendar</h1>")
        return snapshot(
            spec,
            "<h1>Cryptocurrency Calendar</h1><a href='/en/event/update-123'>Update</a>",
            engine="playwright",
        )

    monkeypatch.setattr("crypto_event_harvester.source_feasibility.fetch_page", fake_fetch)
    report = probe_source(spec, engine="auto")
    assert report["engine"] == "playwright"
    assert report["fetch_attempts"] == 2
    assert engines == ["http", "playwright"]


def test_auto_does_not_bypass_blocked_http(monkeypatch):
    spec = SOURCES["coindar"]
    engines = []

    def fake_fetch(url, *, engine, timeout_seconds):
        engines.append(engine)
        return snapshot(spec, "<h1>Access denied</h1>", 403)

    monkeypatch.setattr("crypto_event_harvester.source_feasibility.fetch_page", fake_fetch)
    result = probe_source(spec, engine="auto")
    assert result["status"] == "blocked"
    assert engines == ["http"]


def test_error_on_one_source_does_not_stop_other_sources(monkeypatch):
    def fake_fetch(url, *, engine, timeout_seconds):
        if "coindar" in url:
            raise RuntimeError("temporary network failure")
        spec = next(spec for spec in SOURCES.values() if spec.listing_url == url)
        return snapshot(spec, f"<h1>{spec.expected_text}</h1>")

    monkeypatch.setattr("crypto_event_harvester.source_feasibility.fetch_page", fake_fetch)
    result = probe_sources(["coindar", "icodrops"])
    assert [s["status"] for s in result["sources"]] == ["fetch_error", "needs_review"]
    assert result["total_fetch_attempts"] == 2
    assert result["paid_api_calls"] == 0


def test_saved_html_is_opt_in(tmp_path: Path, monkeypatch):
    spec = SOURCES["icodrops"]
    monkeypatch.setattr(
        "crypto_event_harvester.source_feasibility.fetch_page",
        lambda url, *, engine, timeout_seconds: snapshot(spec, "<title>Upcoming</title>"),
    )
    probe_source(spec, save_html_dir=tmp_path)
    assert (tmp_path / "icodrops.html").read_text() == "<title>Upcoming</title>"


def test_rejects_unknown_engine():
    with pytest.raises(ValueError):
        probe_source(SOURCES["coindar"], engine="invalid")
