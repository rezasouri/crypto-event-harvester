"""Inspect event-listing DOM containers on a GitHub runner (diagnostic only)."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

TARGETS = (
    ("upcoming", "https://icodrops.com/category/upcoming-ico/", "/clix/"),
    ("ended", "https://icodrops.com/category/ended-ico/", "/jumper/"),
)


def compact(node):
    return {
        "tag": getattr(node, "name", ""),
        "class": node.get("class", []) if hasattr(node, "get") else [],
        "attrs": {k: str(v)[:120] for k, v in node.attrs.items()
                  if k not in {"class", "style", "onclick", "onmouseover"}},
        "text": node.get_text(" ", strip=True)[:300],
        "children": [
            {"tag": c.name, "class": c.get("class", []), "text": c.get_text(" ", strip=True)[:100]}
            for c in node.find_all(recursive=False) if getattr(c, "name", None)
        ][:18],
    }


for name, url, suffix in TARGETS:
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        response = client.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    anchors = [a for a in soup.select("a[href]") if urlparse(a.get("href", "")).path == suffix]
    out = {"source": name, "status": response.status_code, "matches": len(anchors)}
    if anchors:
        node = anchors[0]
        parents = []
        for i in range(7):
            if node is None or not hasattr(node, "get_text"):
                break
            parents.append(compact(node))
            node = node.parent
        out["ancestor_chain"] = parents
    print(json.dumps(out, ensure_ascii=False, indent=2))
