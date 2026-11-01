"""Temporary bounded DOM/script reconnaissance for CryptoEvents Load more.

Fetches homepage once; prints sanitized HTML attributes and relevant
inline JavaScript snippets, never cookies or credentials.
"""
import json
import re

import httpx
from bs4 import BeautifulSoup

url = "https://cryptoevents.global/"
r = httpx.get(url, follow_redirects=True, timeout=18)
soup = BeautifulSoup(r.text, "html.parser")
buttons = []
for node in soup.select("button, a, input[type=button]"):
    attrs = {k: v for k, v in node.attrs.items() if k not in ("style",)}
    combined = " ".join([node.get_text(" ", strip=True), json.dumps(attrs)])
    if re.search(r"load.?more|show.?more|more.?events", combined, re.I):
        buttons.append({"tag": node.name, "attrs": attrs, "text": node.get_text(" ", strip=True)[:130],
                        "parent": str(node.parent)[:1400]})
scripts = []
for s in soup.find_all("script"):
    if s.get("src"):
        scripts.append({"src": s.get("src", "")[:250]})
    elif s.string:
        code = s.string
        for m in list(re.finditer(r"load.more|load_more|ajax|pagination|event_blk", code, re.I))[:4]:
            scripts.append({"inline_match": code[max(0,m.start()-180):min(len(code),m.end()+380)][:570]})
print(json.dumps({
    "http_status": r.status_code, "html_bytes": len(r.content),
    "events": len(soup.select(".event_blk")), "load_more_buttons": buttons[:6],
    "script_hints": scripts[:40], "forms": [
        {"action":f.get("action"),"method":f.get("method"),"attrs":{k:v for k,v in f.attrs.items() if k!="style"}}
        for f in soup.select("form")[:3]
    ],
}, indent=2, ensure_ascii=False))
