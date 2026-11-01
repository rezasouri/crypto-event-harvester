"""Pure offline audit, no external network calls."""

from pathlib import Path

import pytest

from crypto_event_harvester.pilot_evaluation import (
    _pagination_evidence,
    audit_snapshot,
    audit_source,
    compare_audits,
)
from crypto_event_harvester.probe import PageSnapshot
from crypto_event_harvester.source_feasibility import SOURCES

FIXTURES = Path(__file__).parent / "fixtures"


def _snapshot(source: str, html: str, *, status: int | None = 200, engine: str = "http"):
    url = SOURCES[source].listing_url
    return PageSnapshot(url, url, engine, status, html)


def test_icodrops_counts_actual_rows_not_navigation_links():
    html = (FIXTURES / "icodrops_upcoming.html").read_text()
    audit = audit_snapshot("icodrops", _snapshot("icodrops", html))
    assert audit["status"] == "parsed_listing"
    assert audit["parsed_record_count"] == audit["unique_event_id_count"] == 2
    assert audit["explicit_calendar_date_count"] == 1
    assert audit["unknown_or_quarter_date_count"] == 1
    assert audit["location_present_count"] == 0
    assert audit["manual_verification_sample_size"] == 0
    assert audit["records_are_manually_verified"] is False
    assert audit["evaluation_level"] == "structural_fields_only"
    assert len(audit["event_fingerprints"]) == 2
    assert len(audit["sample_records"]) == 2
    assert audit["listing_fetch_attempts"] == 1
    assert audit["paid_api_calls"] == 0


def test_cryptoevents_dedups_featured_and_preserves_date_range():
    html = (FIXTURES / "cryptoevents_home.html").read_text()
    audit = audit_snapshot("cryptoevents", _snapshot("cryptoevents", html))
    assert audit["status"] == "parsed_listing"
    assert audit["parsed_record_count"] == 4
    assert audit["unique_event_id_count"] == 4
    assert audit["explicit_calendar_date_count"] == 3
    assert audit["unknown_or_quarter_date_count"] == 1
    assert audit["location_present_count"] == 4
    assert audit["sample_records"][0]["start_date"] == "2026-10-27"
    assert audit["sample_records"][0]["end_date"] == "2026-10-29"


def test_coindar_is_retained_as_link_only_feasibility_candidate():
    html = (FIXTURES / "coindar_listing_sample.html").read_text()
    report = audit_snapshot("coindar", _snapshot("coindar", html))
    assert report["status"] == "candidate_links_only"
    assert report["candidate_detail_link_count"] == 2
    assert report["parsed_record_count"] == 0
    assert report["coindar_provider_implemented"] is False
    assert report["event_ids"] == []
    assert report["pagination"]["mechanism"] == "pagination_links_visible"
    assert report["pagination"]["next_page_url_samples"] == ["https://www.coindar.org/en?page=2"]
    assert report["pagination"]["historical_coverage_verified"] is False


def test_saved_html_creates_no_network_request(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("network was attempted")

    monkeypatch.setattr("crypto_event_harvester.pilot_evaluation.fetch_page", fail)
    report = audit_source("coindar", input_html=FIXTURES / "coindar_listing_sample.html")
    assert report["engine"] == "saved_html"
    assert report["listing_fetch_attempts"] == 0
    assert report["status"] == "candidate_links_only"


def test_live_fetch_is_one_listing_request_with_no_details(monkeypatch, tmp_path):
    called = []
    html = (FIXTURES / "icodrops_upcoming.html").read_text()

    def fetch(url, *, engine, timeout_seconds):
        called.append((url, engine))
        return _snapshot("icodrops", html)

    monkeypatch.setattr("crypto_event_harvester.pilot_evaluation.fetch_page", fetch)
    snapshot_path = tmp_path / "raw" / "upcoming.html"
    report = audit_source("icodrops", engine="http", save_html=snapshot_path)
    assert report["status"] == "parsed_listing"
    assert called == [(SOURCES["icodrops"].listing_url, "http")]
    assert snapshot_path.read_text() == html


@pytest.mark.parametrize("status", [403, 429])
def test_block_pages_never_claim_extracted_records(status):
    html = (FIXTURES / "icodrops_upcoming.html").read_text()
    report = audit_snapshot("icodrops", _snapshot("icodrops", html, status=status))
    assert report["status"] == "blocked"
    assert report["parsed_record_count"] == 0
    assert report["event_ids"] == []


def test_parser_failure_is_explicit_and_does_not_invent_events():
    html = "<html><body><div>Nothing to parse</div></body></html>"
    report = audit_snapshot("cryptoevents", _snapshot("cryptoevents", html))
    assert report["status"] == "parser_error"
    assert report["parsed_record_count"] == 0
    assert "parser_error" in report


def test_access_failure_from_runner_not_a_permanent_source_rejection(monkeypatch):
    def denied(url, *, engine, timeout_seconds):
        return _snapshot("coindar", "<title>Access denied</title>", status=403)

    monkeypatch.setattr("crypto_event_harvester.pilot_evaluation.fetch_page", denied)
    report = audit_source("coindar")
    assert report["status"] == "blocked"
    assert report["listing_fetch_attempts"] == 1
    assert report["source"] == "coindar"


def test_load_more_detection_is_not_claimed_pagination_coverage():
    html = "<div class='event_blk'></div><button class='load_more'>Load more</button>"
    report = _pagination_evidence(html, listing_url=SOURCES["cryptoevents"].listing_url)
    assert report["load_more_visible"] is True
    assert report["mechanism"] == "dynamic_load_more_visible"
    assert report["pagination_followed"] is False
    assert report["historical_coverage_verified"] is False


def test_pagination_links_must_share_source_host():
    html = (
        "<div class='pagination'>"
        "<a href='https://bad.example/page/2'>2</a>"
        "<a href='?page=2'>Next</a></div>"
    )
    report = _pagination_evidence(html, listing_url=SOURCES["icodrops"].listing_url)
    assert len(report["next_page_url_samples"]) == 1
    assert report["next_page_url_samples"][0].startswith("https://icodrops.com/")


def test_same_snapshot_compare_is_stable_without_new_requests():
    html = (FIXTURES / "icodrops_upcoming.html").read_text()
    report = audit_snapshot("icodrops", _snapshot("icodrops", html))
    comparison = compare_audits(report, report)
    assert comparison["retained_count"] == 2
    assert comparison["added_count"] == 0
    assert comparison["changed_content_count"] == 0
    assert comparison["missing_from_latest_listing_count"] == 0


def test_compare_identifies_changed_content_and_missing_rows():
    html = (FIXTURES / "cryptoevents_home.html").read_text()
    updated = html.replace("MERGE Madrid 2026: institutional crypto and Web3.",
                           "MERGE Madrid 2026: updated conference details.")
    updated = updated.replace('href="/unscheduled-conference/"',
                              'href="https://external.example/unscheduled-conference/"')
    before = audit_snapshot("cryptoevents", _snapshot("cryptoevents", html))
    after = audit_snapshot("cryptoevents", _snapshot("cryptoevents", updated))
    comparison = compare_audits(before, after)
    assert comparison["retained_count"] == 3
    assert comparison["missing_from_latest_listing_count"] == 1
    assert comparison["changed_content_count"] == 1
    assert comparison["changed_event_ids"] == ["cryptoevents:merge-madrid-2026"]
    assert comparison["added_count"] == 0


def test_compare_rejects_nonmatching_sources_and_unverified_link_reports():
    html = (FIXTURES / "icodrops_upcoming.html").read_text()
    before = audit_snapshot("icodrops", _snapshot("icodrops", html))
    other = audit_snapshot(
        "coindar", _snapshot("coindar", (FIXTURES / "coindar_listing_sample.html").read_text())
    )
    with pytest.raises(ValueError, match="sources differ"):
        compare_audits(before, other)
    with pytest.raises(ValueError, match="parsed_listing"):
        compare_audits(other, other)


def test_invalid_timeout_and_source_are_explicit_errors():
    with pytest.raises(ValueError, match="unknown audit source"):
        audit_source("unknown")
    with pytest.raises(ValueError, match="positive"):
        audit_source("coindar", timeout_seconds=0)
