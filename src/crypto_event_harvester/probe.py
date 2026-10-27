"""Inspect publicly accessible calendar pages before writing a site-specific parser."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "CryptoEventHarvester-Pilot/0.1 (research; single-page probe)"
CHALLENGE_MARKERS = (
    "checking your browser",
    "verify you are human",
    "performing security verification",
    "captcha",
    "access denied",
)


@dataclass(frozen=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    engine: str
    status_code: int | None
    html: str


def fetch_page(
    url: str, *, engine: str = "http", timeout_seconds: float = 30, wait_for: str | None = None
) -> PageSnapshot:
    """Fetch one page; browser dependencies are installed only when explicitly selected."""
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
        raise ValueError("--url must be an absolute HTTP(S) URL")
    if timeout_seconds <= 0:
        raise ValueError("--timeout must be positive")
    if engine == "http":
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = client.get(url)
            return PageSnapshot(url, str(response.url), engine, response.status_code, response.text)
    if engine == "playwright":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Install the playwright extra and run 'playwright install chromium'") from exc
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=int(timeout_seconds * 1000))
                return PageSnapshot(
                    url, page.url, engine, response.status if response else None, page.content()
                )
            finally:
                browser.close()
    if engine == "selenium":
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as ec
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError("Install the selenium extra and configure Chrome/Chromedriver") from exc
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={USER_AGENT}")
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(timeout_seconds)
            driver.get(url)
            if wait_for:
                WebDriverWait(driver, timeout_seconds).until(
                    ec.presence_of_element_located((By.CSS_SELECTOR, wait_for))
                )
            return PageSnapshot(url, driver.current_url, engine, None, driver.page_source)
        finally:
            driver.quit()
    raise ValueError(f"Unknown engine: {engine}")


def summarize_page(snapshot: PageSnapshot, *, selector: str | None = None) -> dict:
    """Return diagnostic metadata; do not interpret challenge pages as event data."""
    soup = BeautifulSoup(snapshot.html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    visible_text = soup.get_text(" ", strip=True).lower()
    if snapshot.status_code == 429:
        issue = "rate_limited"
    elif snapshot.status_code in {401, 403}:
        issue = "access_restricted"
    elif any(marker in (title + " " + visible_text[:5000]).lower() for marker in CHALLENGE_MARKERS):
        issue = "security_challenge"
    else:
        issue = None
    return {
        "requested_url": snapshot.requested_url,
        "final_url": snapshot.final_url,
        "engine": snapshot.engine,
        "status_code": snapshot.status_code,
        "title": title,
        "html_bytes": len(snapshot.html.encode("utf-8")),
        "selector": selector,
        "selector_matches": len(soup.select(selector)) if selector else None,
        "issue": issue,
    }
