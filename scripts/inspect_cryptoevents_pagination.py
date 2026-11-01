"""One bounded POST matching the public homepage Load more request."""
import json
from datetime import UTC, datetime

import httpx

url = "https://cryptoevents.global/wp-content/themes/cryptoevents/ajax/filters.php"
today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
body = [
    ("action", "index"), ("offset", "20"), ("postperpage", "20"),
    ("categories[]", "16"), ("index_filters[start_date]", str(int(today.timestamp()))),
]
try:
    with httpx.Client(timeout=25, follow_redirects=True, headers={
        "Referer": "https://cryptoevents.global/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }) as client:
        r = client.post(url, data=body)
    try:
        doc = r.json()
    except ValueError:
        doc = None
    if isinstance(doc, list):
        samples = [{k:(str(v)[:250] if k in {"excerpt", "title", "timezone"} else str(v)[:150])
                     for k,v in p.items()} for p in doc[:2] if isinstance(p, dict)]
        output = {"http":r.status_code,"content_type":r.headers.get("content-type"),
                  "items":len(doc),"sample":samples}
    else:
        output = {"http":r.status_code, "type":type(doc).__name__,
                  "keys":list(doc)[:30] if isinstance(doc,dict) else [],
                  "body_preview":r.text[:350]}
    print(json.dumps(output,indent=2,ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"error":str(exc)[:250],"type":type(exc).__name__}))
