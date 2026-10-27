"""Temporary DOM reconnaissance for crypto-events listing (no detail crawling)."""

from __future__ import annotations

import json
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

URL = "https://cryptoevents.global/"
response = httpx.get(URL, follow_redirects=True, timeout=25)
soup = BeautifulSoup(response.text, "html.parser")

def describe(node: Tag) -> dict:
    return {
        "tag": node.name,
        "classes": node.get("class", []),
        "id": node.get("id"),
        "other_attributes": {
            k: str(v)[:160] for k, v in node.attrs.items()
            if k not in {"class", "id", "style"} and not k.startswith("on")
        },
        "direct_children": [
            {"tag": n.name, "class": n.get("class", []), "text": n.get_text(" ", strip=True)[:90]}
            for n in node.find_all(recursive=False) if isinstance(n, Tag)
        ][:15],
        "text": node.get_text(" ", strip=True)[:260],
    }

report = {
    "http": response.status_code,
    "final_url": str(response.url),
    "title": soup.title.get_text(" ", strip=True) if soup.title else "",
    "html_bytes": len(response.content),
    "jsonld_count": len(soup.select('script[type="application/ld+json"]')),
    "time_elements": [describe(x) for x in soup.select("time")[:3]],
    "form_actions": [
        {"action": form.get("action"), "method": form.get("method"), "inputs": [
            {"name": x.get("name"), "type": x.get("type"), "placeholder": x.get("placeholder")}
            for x in form.select("input")[:12]
        ]} for form in soup.select("form")[:3]
    ],
    "load_more": [
        describe(n) for n in soup.find_all(
            lambda t: isinstance(t, Tag) and t.name in {"a", "button"}
            and "load more" in t.get_text(" ", strip=True).lower()
        )[:3]
    ],
}
for needle in ["Merge Madrid 2026", "The Bridge Conference 2026", "Devcon 2026 Mumbai"]:
    matches = soup.find_all(string=lambda s: bool(s and needle.lower() in s.lower()))
    report[needle] = []
    for match in matches[:2]:
        node = match.parent
        ancestors = []
        for _ in range(6):
            if not isinstance(node, Tag):
                break
            ancestors.append(describe(node))
            node = node.parent
        report[needle].append(ancestors)
report["detail_url_candidates"] = [
    {"url": urljoin(URL, a.get("href", "")), "text": a.get_text(" ", strip=True)[:70]}
    for a in soup.select("a[href]") if any(x in a.get("href", "") for x in ("merge-madrid", "devcon-2026"))
][:8]
print(json.dumps(report, indent=2, ensure_ascii=False))
