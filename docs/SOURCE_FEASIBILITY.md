# Gate 1 — Source feasibility (crypto events)

Status: **initial page reconnaissance**; direct HTTP and browser results are measured separately.
No live fetched page is declared a verified event scrape in this gate.

## Shortlist and public page observations (2026-10-27)

| Source | Listing | Sample detail shape | Public listing content | History/pagination |
| --- | --- | --- | --- | --- |
| Coindar | https://www.coindar.org/en | `/en/event/<slug>-<id>` | coin, title, category, summary, date (UTC), added time | Past Events UI visible; exact paging params not yet verified |
| ICO Drops | https://icodrops.com/category/upcoming-ico/ | `/<project>/` | project, offering type, status, some dates | `/category/ended-ico/` and upcoming sections; paging not verified |
| CryptoEvents.global | https://cryptoevents.global/ | `/<conference-slug>/` | conference, date/date range, location, description | date-from/date-to UI; history not verified |

### Evidence for public page shapes

- Coindar listing: https://www.coindar.org/en; example event: https://www.coindar.org/en/event/sailout-royalty-to-release-computer-155795
- ICO Drops upcoming: https://icodrops.com/category/upcoming-ico/; example project: https://icodrops.com/1win-token/
- CryptoEvents.global listing: https://cryptoevents.global/; example conference: https://cryptoevents.global/merge-madrid-2026/

These page observations came from a web-rendered public page view, **not from the project's HTTPX
or Playwright runner**. Whether raw HTML contains the event list, whether a browser is needed,
and whether the sites respond on GitHub-hosted runners are answered by the live probe report.

## Reproduce from a developer machine

```bash
python -m pip install -e '.[dev]'
crypto-harvester sources --source all --engine http --json-out artifacts/sources-http.json

# If public HTML does not include candidate event links:
python -m pip install -e '.[playwright]'
playwright install chromium
crypto-harvester sources --source coindar --engine playwright \
    --json-out artifacts/coindar-browser.json

# Optional automatic HTTP -> Playwright fallback for pages missing listing evidence:
crypto-harvester sources --source all --engine auto --json-out artifacts/auto.json
```

A separate GitHub Actions job, `Live source feasibility (nonblocking)`, probes one listing page
per source via HTTP (three GET calls total; redirects and page assets may affect actual wire
requests). It uploads `crypto-source-probe-report` as a JSON artifact. No API keys, paid
subscriptions or event detail fetches are involved. Run outcomes may differ across hosts.

## Interpretation

- `listing_evidence`: page contained a recognizable text marker *and* plausible same-site detail
  links. **Not an event count or proof that each link is a real event.**
- `needs_review`: listing proof was insufficient; try browser rendering or inspect selectors.
- `blocked` / `http_error` / `fetch_error`: network or page restriction; do not report success.
- `candidate_detail_link_count`: URL-pattern match only, not normalized event records.
- `verified_event_count`: always null in this stage; parsing and field-level QA belong to Gate 2.

## Next acceptance gate

After live results are reviewed, the next pull request implements *one* real event parser at a
time using captured fixture HTML, verifies at least 20 real events when available, confirms
field correctness against the source pages, and documents pagination/history separately.


## First observed live GitHub Actions run

Run: https://github.com/rezasouri/crypto-event-harvester/actions/runs/40477402010
(2026-10-27 UTC, one HTTP listing probe per source; revision before navigation-link exclusions.)

| Source | Status | HTTP | Candidate links | HTML bytes |
| --- | --- | ---: | ---: | ---: |
| Coindar | blocked | 403 (challenge page) | 0 | 5507 |
| ICO Drops | listing_evidence | 200 | 52 | 230090 |
| CryptoEvents.global | listing_evidence | 200 | 37 | 91146 |

**Caution:** the initial candidate counts included navigation links (e.g., Points Farming
and regional category pages). Subsequent exclusions reduce false positives; a fresh CI run
on the updated commit is the source of truth for updated candidate-link counts. No normalized
events were extracted. Browser rendering for Coindar is not attempted against an access block.


## Verified GitHub-hosted HTTP run after corrections

Run: https://github.com/rezasouri/crypto-event-harvester/actions/runs/40477660745

| Source | Status | HTTP | Candidate links | Duration |
| --- | --- | ---: | ---: | ---: |
| Coindar | blocked (challenge page) | 403 | 0 | 0.38 s |
| ICO Drops | listing_evidence | 200 | 51 | 2.03 s |
| CryptoEvents.global | listing_evidence | 200 | 32 | 1.41 s |

No paid API and only three listing-page fetch attempts. Both successful HTTP sources returned
the content marker and candidate detail links without JavaScript browser rendering.
The CryptoEvents.global candidate links can still include site navigation or content pages
(for example, /press-releases/); Gate 2 must identify genuine event cards and validate fields.

**Gate 1 decision:** pursue ICO Drops and CryptoEvents.global as HTTP-first providers,
hold Coindar pending an accessible permitted route. No repeated requests or automatic
browser fallback were attempted on the blocked Coindar page.
