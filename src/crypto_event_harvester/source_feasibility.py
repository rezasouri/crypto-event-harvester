"""Repeatable, low-request source reconnaissance (not an event scraper)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .probe import PageSnapshot, fetch_page, summarize_page


@dataclass(frozen=True)
class SourceSpec:
    key: str
    listing_url: str
    description: str
    expected_text: str
    path_pattern: str
    excluded_slugs: frozenset[str] = frozenset()


SOURCES: dict[str, SourceSpec] = {
    "coindar": SourceSpec(
        key="coindar",
        listing_url="https://www.coindar.org/en",
        description="General cryptocurrency event calendar",
        expected_text="cryptocurrency calendar",
        path_pattern=r"^/en/event/[^/?#]+/?$",
    ),
    "icodrops": SourceSpec(
        key="icodrops",
        listing_url="https://icodrops.com/category/upcoming-ico/",
        description="Token launches and ICO/IDO/IEO projects",
        expected_text="upcoming",
        path_pattern=r"^/[a-zA-Z0-9][a-zA-Z0-9-]*/?$",
        excluded_slugs=frozenset({
            "about", "advertising", "legal", "contact", "privacy-policy",
            "terms", "category", "investors", "ico-stats", "funding-rounds",
            "points-farming",
        }),
    ),
    "cryptoevents": SourceSpec(
        key="cryptoevents",
        listing_url="https://cryptoevents.global/",
        description="Bitcoin and blockchain conferences",
        expected_text="blockchain",
        path_pattern=r"^/[a-zA-Z0-9][a-zA-Z0-9-]*/?$",
        excluded_slugs=frozenset({
            "about", "contact", "conferences", "privacy-policy", "web3-events",
            "newsroom", "blog", "submit-event", "announcements", "orange-pilled",
            "bitcoin-blockchain-conferences-in-north-america",
            "bitcoin-blockchain-conferences-in-europe",
            "bitcoin-blockchain-conferences-asia-apac-africa",
            "orange-pilled-bitcoin-events",
            "meetups-workshops-networking-crypto-web3-events",
        }),
    ),
}


def candidate_event_links(html: str, *, spec: SourceSpec, max_samples: int = 5) -> tuple[int, list[dict]]:
    """Count plausible event/detail links; this is not a validated event count."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(spec.listing_url).hostname
    pattern = re.compile(spec.path_pattern, re.IGNORECASE)
    found: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        url = urljoin(spec.listing_url, anchor.get("href", ""))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != host:
            continue
        if not pattern.fullmatch(parts.path):
            continue
        slug = parts.path.strip("/").rsplit("/", 1)[-1].lower()
        if slug in spec.excluded_slugs:
            continue
        label = anchor.get_text(" ", strip=True)
        # Image-only links still count, but are not usable title evidence.
        if not label:
            image = anchor.find("img", alt=True)
            label = image.get("alt", "").strip() if image else ""
        canonical = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if canonical not in found:
            found[canonical] = label[:120]
    samples = [{"url": url, "anchor_text": label} for url, label in list(found.items())[:max_samples]]
    return len(found), samples


def inspect_snapshot(spec: SourceSpec, snapshot: PageSnapshot) -> dict:
    """Classify evidence conservatively; never equate links to scraped events."""
    base = summarize_page(snapshot)
    count, samples = candidate_event_links(snapshot.html, spec=spec)
    soup = BeautifulSoup(snapshot.html, "html.parser")
    visible = soup.get_text(" ", strip=True).lower()
    expected = spec.expected_text.lower() in visible
    if base["issue"]:
        status = "blocked"
    elif snapshot.status_code is not None and snapshot.status_code >= 400:
        status = "http_error"
    elif expected and count:
        status = "listing_evidence"
    else:
        status = "needs_review"
    return {
        "source": spec.key,
        "description": spec.description,
        "status": status,
        "requested_url": snapshot.requested_url,
        "final_url": snapshot.final_url,
        "engine": snapshot.engine,
        "http_status": snapshot.status_code,
        "page_title": base["title"],
        "html_bytes": base["html_bytes"],
        "html_sha256": hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
        "access_issue": base["issue"],
        "expected_text_found": expected,
        "candidate_detail_link_count": count,
        "candidate_detail_link_samples": samples,
        "verified_event_count": None,
    }


def probe_source(
    spec: SourceSpec, *, engine: str = "http", timeout_seconds: float = 20,
    save_html_dir: Path | None = None,
) -> dict:
    """Probe one listing page; auto may try Playwright only for unrendered public HTML."""
    if engine not in {"http", "playwright", "selenium", "auto"}:
        raise ValueError(f"Unsupported source probe engine: {engine}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    selected = "http" if engine == "auto" else engine
    started = perf_counter()
    attempts = 0
    try:
        attempts += 1
        snapshot = fetch_page(spec.listing_url, engine=selected, timeout_seconds=timeout_seconds)
        report = inspect_snapshot(spec, snapshot)
        if engine == "auto" and report["status"] == "needs_review":
            try:
                attempts += 1
                rendered = fetch_page(spec.listing_url, engine="playwright", timeout_seconds=timeout_seconds)
                browser_report = inspect_snapshot(spec, rendered)
                if browser_report["status"] == "listing_evidence":
                    report, snapshot = browser_report, rendered
                else:
                    report["browser_fallback_status"] = browser_report["status"]
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                report["browser_fallback_error"] = f"{type(exc).__name__}: {exc}"
        if save_html_dir is not None:
            save_html_dir.mkdir(parents=True, exist_ok=True)
            (save_html_dir / f"{spec.key}.html").write_text(snapshot.html, encoding="utf-8")
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        report = {
            "source": spec.key,
            "description": spec.description,
            "status": "fetch_error",
            "requested_url": spec.listing_url,
            "engine": selected,
            "error": f"{type(exc).__name__}: {exc}",
            "verified_event_count": None,
        }
    report["fetch_attempts"] = attempts
    report["duration_seconds"] = round(perf_counter() - started, 3)
    report["observed_at_utc"] = datetime.now(UTC).isoformat()
    return report


def probe_sources(
    names: list[str], *, engine: str = "http", timeout_seconds: float = 20,
    save_html_dir: Path | None = None,
) -> dict:
    """Produce a JSON-serializable report of source status and request budget."""
    results = [
        probe_source(
            SOURCES[name], engine=engine, timeout_seconds=timeout_seconds, save_html_dir=save_html_dir
        )
        for name in names
    ]
    return {
        "method": "single_listing_page_probe",
        "method_note": "Candidate links are not verified scraped events; no detail pages were requested.",
        "paid_api_calls": 0,
        "total_fetch_attempts": sum(item["fetch_attempts"] for item in results),
        "sources": results,
    }
