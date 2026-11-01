"""First-page pilot quality audit, pagination evidence and offline run comparison.

The audit deliberately separates validated provider records from link-only
Coindar feasibility evidence. It never treats pagination hints as a complete crawl.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .normalization import normalize_events
from .probe import PageSnapshot, fetch_page, summarize_page
from .providers import cryptoevents, icodrops
from .source_feasibility import SOURCES, candidate_event_links

SUPPORTED_SOURCES = ("icodrops", "cryptoevents", "coindar")


def _digest(data: dict) -> str:
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _pagination_evidence(html: str, *, listing_url: str) -> dict:
    """Inspect current HTML only. No pagination requests are made."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    host = urlsplit(listing_url).hostname
    for anchor in soup.select(
        'a[rel~="next"], a.next, a.page-numbers, .pagination a[href], '
        'a[href*="page="], a[href*="paged="]'
    ):
        value = anchor.get("href")
        if not value:
            continue
        url = urljoin(listing_url, value)
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or parts.hostname != host:
            continue
        if url not in found:
            found.append(url)
    load_more = bool(soup.select("button.load_more, a.load_more")) or any(
        "load more" in button.get_text(" ", strip=True).lower()
        for button in soup.select("button")
    )
    if load_more:
        mechanism = "dynamic_load_more_visible"
    elif found:
        mechanism = "pagination_links_visible"
    else:
        mechanism = "not_observed_on_first_page"
    return {
        "mechanism": mechanism,
        "load_more_visible": load_more,
        "next_page_url_samples": found[:5],
        "pagination_followed": False,
        "historical_coverage_verified": False,
    }


def audit_snapshot(source: str, snapshot: PageSnapshot) -> dict:
    """Evaluate a fetched or locally saved listing page without extra requests."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown audit source: {source}")

    spec = SOURCES[source]
    meta = summarize_page(snapshot)
    report: dict = {
        "source": source,
        "requested_url": snapshot.requested_url,
        "final_url": snapshot.final_url,
        "engine": snapshot.engine,
        "http_status": snapshot.status_code,
        "html_bytes": meta["html_bytes"],
        "html_sha256": hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
        "access_issue": meta["issue"],
        "page_title": meta["title"],
        "listing_fetch_attempts": 0 if snapshot.engine == "saved_html" else 1,
        "browser_asset_requests_measured": False,
        "paid_api_calls": 0,
        "pagination": _pagination_evidence(snapshot.html, listing_url=spec.listing_url),
        "records_are_manually_verified": False,
        "manual_verification_sample_size": 0,
        "parsed_record_count": 0,
        "event_ids": [],
        "event_fingerprints": {},
    }
    if meta["issue"]:
        report["status"] = "blocked"
        return report
    if snapshot.status_code is not None and snapshot.status_code >= 400:
        report["status"] = "http_error"
        return report

    if source == "coindar":
        count, samples = candidate_event_links(snapshot.html, spec=spec)
        report.update(
            status="candidate_links_only" if count else "needs_review",
            candidate_detail_link_count=count,
            candidate_detail_link_samples=samples,
            coindar_provider_implemented=False,
            evaluation_level="feasibility_only",
        )
        return report

    try:
        native = (
            icodrops.parse_listing(snapshot.html, section="upcoming")
            if source == "icodrops"
            else cryptoevents.parse_listing(snapshot.html)
        )
        records = normalize_events(native)
    except (icodrops.ICODropsError, cryptoevents.CryptoEventsError, ValueError, TypeError) as exc:
        report["status"] = "parser_error"
        report["parser_error"] = f"{type(exc).__name__}: {exc}"
        return report

    expected_host = urlsplit(spec.listing_url).hostname
    wrong_urls = [
        event.event_id for event in records
        if urlsplit(event.source_url).hostname != expected_host
        or urlsplit(event.source_url).scheme != "https"
    ]
    precise = sum(event.date_precision in {"day", "range"} for event in records)
    known_location = sum(bool(event.location) for event in records)
    known_description = sum(bool(event.description) for event in records)
    report.update(
        status="quality_failure" if wrong_urls else "parsed_listing",
        evaluation_level="structural_fields_only",
        parsed_record_count=len(records),
        unique_event_id_count=len({event.event_id for event in records}),
        explicit_calendar_date_count=precise,
        unknown_or_quarter_date_count=len(records) - precise,
        location_present_count=known_location,
        description_present_count=known_description,
        invalid_source_url_event_ids=wrong_urls,
        event_ids=[event.event_id for event in records],
        event_fingerprints={event.event_id: _digest(event.to_dict()) for event in records},
        sample_records=[event.to_dict() for event in records[:2]],
    )
    return report


def audit_source(
    source: str, *, engine: str = "http", timeout_seconds: float = 20,
    input_html: Path | None = None, save_html: Path | None = None,
) -> dict:
    """Audit one page; use saved HTML for Coindar workstation/browser triage."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown audit source: {source}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    spec = SOURCES[source]
    try:
        if input_html is not None:
            snapshot = PageSnapshot(
                spec.listing_url, spec.listing_url, "saved_html", None,
                input_html.read_text(encoding="utf-8"),
            )
        else:
            snapshot = fetch_page(
                spec.listing_url, engine=engine, timeout_seconds=timeout_seconds
            )
        if save_html is not None:
            save_html.parent.mkdir(parents=True, exist_ok=True)
            save_html.write_text(snapshot.html, encoding="utf-8")
        report = audit_snapshot(source, snapshot)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        report = {
            "source": source,
            "status": "fetch_error",
            "engine": "saved_html" if input_html else engine,
            "requested_url": spec.listing_url,
            "error": f"{type(exc).__name__}: {exc}",
            "listing_fetch_attempts": 0 if input_html else 1,
            "paid_api_calls": 0,
            "parsed_record_count": 0,
            "event_ids": [],
            "event_fingerprints": {},
        }
    report["observed_at_utc"] = datetime.now(UTC).isoformat()
    return report


def compare_audits(previous: dict, current: dict) -> dict:
    """Compare two reports offline; requires genuine parsed records."""
    if previous.get("source") != current.get("source"):
        raise ValueError("audit sources differ")
    if previous.get("status") != "parsed_listing" or current.get("status") != "parsed_listing":
        raise ValueError("comparison requires two successful parsed_listing reports")
    left = previous.get("event_fingerprints")
    right = current.get("event_fingerprints")
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ValueError("reports must include full event_fingerprints")
    before, after = set(left), set(right)
    common = before & after
    changed = sorted(event_id for event_id in common if left[event_id] != right[event_id])
    return {
        "source": current["source"],
        "previous_record_count": len(before),
        "current_record_count": len(after),
        "retained_count": len(common),
        "added_count": len(after - before),
        "missing_from_latest_listing_count": len(before - after),
        "changed_content_count": len(changed),
        "added_event_ids": sorted(after - before),
        "missing_from_latest_listing_event_ids": sorted(before - after),
        "changed_event_ids": changed,
        "note": "Missing from a listing does not imply deletion from persistent storage.",
    }
