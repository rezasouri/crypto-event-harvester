"""Temporary diagnosis of the published Load more handler and its API response."""
import json

import httpx

BASE = "https://cryptoevents.global/"
SCRIPT = BASE + "wp-content/themes/cryptoevents/js/actions.js?v=1.0.4"
with httpx.Client(timeout=20, follow_redirects=True) as client:
    res = client.get(SCRIPT)
    js = res.text
    start = js.find("$(document).on('click', '.load_more'")
    print(json.dumps({"js_status": res.status_code, "click_handler": js[start:start+2800],
      "builder": js[:6200]}, ensure_ascii=False, indent=2))
