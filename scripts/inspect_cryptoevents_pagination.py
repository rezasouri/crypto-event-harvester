"""Temporary bounded JS + markup pagination reconnaissance."""
import json
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

base = "https://cryptoevents.global/"
with httpx.Client(timeout=20, follow_redirects=True) as client:
    response = client.get(base)
    soup = BeautifulSoup(response.text, "html.parser")
    elements = []
    for node in soup.find_all(True):
        if not node.attrs:
            continue
        fields = str({k: v for k, v in node.attrs.items() if k in {"id", "class", "data-page", "data-offset", "data-action", "data-url", "data-posts", "href"}})
        if re.search(r"load.?more|load_more|pagination|loadmore", fields, re.I):
            elements.append({"tag": node.name, "attrs": fields[:350], "outer": str(node)[:1300]})
    script_details = []
    for suffix in ["/wp-content/themes/cryptoevents/js/actions.js?v=1.0.4",
                   "/wp-content/themes/cryptoevents/js/common.js?v=1.0.26"]:
        url = urljoin(base, suffix)
        js = client.get(url)
        code = js.text
        snippets = []
        for match in list(re.finditer(
            r"load.?more|load_more|loadmore|ajax|pagination|offset|page.?num|post_per_page|event_blk",
            code, re.I
        ))[:25]:
            snippets.append(code[max(0, match.start()-240):min(len(code), match.end()+540)])
        script_details.append({"script": suffix, "status": js.status_code, "bytes": len(js.content),
                               "snippets": snippets[:25]})
print(json.dumps({"status": response.status_code, "pagination_elements": elements[:12],
                  "scripts": script_details}, indent=2, ensure_ascii=False))
