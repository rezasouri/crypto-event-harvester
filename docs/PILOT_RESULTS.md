# Gate 6 — Pilot evaluation, coverage and workstation Coindar investigation

This stage turns the two working providers into a **measurable pilot** and adds
a reproducible **local** diagnostic for Coindar. It is not a claim of a full
historic archive, manual field-level accuracy, or production readiness.

## Audit one page with no extra detail requests

```bash
uv sync --extra dev

uv run crypto-harvester audit --source icodrops --min-records 20 --strict \
  --output artifacts/audit-icodrops.json
uv run crypto-harvester audit --source cryptoevents --min-records 10 --strict \
  --output artifacts/audit-cryptoevents.json
```

Each command fetches **one visible listing page via direct HTTP** by default
(+ any HTTP redirects). Browser engines may load additional resources; their
subrequests are not measured by the audit. The output distinguishes:

- Genuine **parsed and normalized** event records vs candidate HTML links.
- Unique source IDs, explicit date-only/date-range coverage, unknown dates,
  description and location completeness, and invalid source URLs.
- SHA256 HTML snapshot and individual normalized record fingerprints.
- Possible `Load more`, next-page links, and whether pagination/history
  was actually followed (always false in this stage).
- Access-block or parser failures (never labeled successful data extraction).
- A clear `records_are_manually_verified: false` marker: a valid parser alone
  **does not prove** 95% field-level accuracy against the source.

`--min-records` is a gate on genuine normalized records for implemented
providers and a gate on **candidate links only** for Coindar. The number of
physical HTTP requests may exceed the number of listing calls due to redirects
or browser assets, so the audit reports attempts rather than network totals.

## Coindar: keep as a source, test from your local machine

Coindar is **not rejected** because its page has returned HTTP 403 on
GitHub-hosted runners. Browsing the website on a normal computer and reaching
the data using GitHub Actions are separate feasibility questions.

On the workstation where you can open Coindar:

```bash
# First: a single ordinary HTTP GET.
uv run crypto-harvester audit --source coindar --engine http \
  --output artifacts/coindar-http.json --save-html .debug/coindar-http.html

# Optional: inspect JavaScript-rendered public page with a normal browser engine.
uv sync --extra dev --extra playwright
uv run playwright install chromium
uv run crypto-harvester audit --source coindar --engine playwright \
  --output artifacts/coindar-browser.json --save-html .debug/coindar-browser.html

# If you saved a publicly visible page as HTML in your browser:
uv run crypto-harvester audit --source coindar --input-html ~/Downloads/coindar.html \
  --output artifacts/coindar-saved.json
```

Coindar now has an offline HTML parser that extracts same-host event cards and normalizes
numeric event IDs, coin/ticker, category, title, description and visible day-group dates.
A representative fixture tests this structure. The current raw page could not be captured
from the GitHub runner (403), so a fresh local HTML snapshot must still be audited before
claiming field-level accuracy or production readiness.

If a live page returns 403/429, the audit records it as a limitation of
**that environment**. It does not attempt challenge bypass or persistent
retry loops. Saved HTML, logs, and page samples should stay local and are
covered by the repository's `.gitignore` paths.

## Compare repeated observations without scraping more

Save two JSON reports from the same provider at different times:

```bash
uv run crypto-harvester audit --source icodrops \
  --output artifacts/first.json
# At a later time, run the same source again:
uv run crypto-harvester audit --source icodrops \
  --compare-to artifacts/first.json --output artifacts/second.json
```

The comparison reports added, missing and content-changed event IDs.
**Missing from one first-page snapshot must not be interpreted as deletion**:
the source may reorder, paginate or replace old items. Normal SQLite
synchronization therefore intentionally preserves prior unseen IDs.

No statistical uptime/availability claim is made from one or two probes.
For a reliable stability claim, collect multiple independent live reports
over several dates and review the differences and rate-limiting behavior.

## Historical reach / pagination limits

| Source | First-page parser | Extra pages | Past history |
| --- | --- | --- | --- |
| ICO Drops | Implemented for section listing pages | Not traversed in this gate | Ended section works, archive completeness unverified |
| CryptoEvents.global | Implemented for homepage cards | A `Load more` control is visible; not clicked or reverse-engineered | No historic completeness verification |
| Coindar | Implemented for saved/local HTML; live DOM confirmation pending | Page links detected, not followed | Past-events interface visible; archive completeness unverified |

The new GitHub workflow evaluates the **two implemented providers** through
a single public listing fetch each and attaches JSON evidence. Coindar is
deliberately not counted as a GitHub-hosted failure or as a validated provider.

## Earlier measured evidence (not current-run guarantees)

- October 27, 2026: ICO Drops Upcoming 50, Ended 50 listing records.
- October 27, 2026: CryptoEvents.global 21 unique conference records.
- October 28, 2026: 71 normalized records stored in SQLite from two public
  listing pages; offline replay added 0 revisions.

Those counts were observed at the time; they may change as sites update.
For updated evidence, inspect the `pilot-quality-audit` workflow artifact.

## Acceptance and remaining work

Acceptance for this gate is passing offline tests and lint, plus live parsed
results with explicit source quality metrics and bounded request counts for
both supported providers. Requiring Coindar success on a GitHub runner is
**not** an acceptance condition. Manual accuracy sampling and real pagination
backfill remain work to be validated, rather than assertions in this report.

**Next logical work:** use actual local Coindar HTML to build the provider,
then implement bounded pagination and evaluate incremental historical reach.


## Verified live quality snapshot: 2026-11-01

The initial live quality run has now completed successfully:

- [GitHub Actions: two-source quality audit](https://github.com/rezasouri/crypto-event-harvester/actions/runs/41280030191)
- [Offline tests and Ruff](https://github.com/rezasouri/crypto-event-harvester/actions/runs/41280030081)

| Source | Parsed unique records | Explicit calendar dates | Unknown/quarter dates | Listing HTTP fetch attempts | HTML bytes | Paging evidence |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| ICO Drops Upcoming | 50 | 3 | 47 | 1 | 231416 | No pagination controls detected on first page |
| CryptoEvents.global | 21 | 21 | 0 | 1 | 90056 | Dynamic `Load more` visible |

All 71 records passed **structural** normalization and source-host identity
checks. **137 offline tests passed** and Ruff passed. Exactly two listing
fetch attempts were made by the quality workflow, and no paid API was used.
The normalized calendar dates have no inferred clock time or UTC offset.

**Key finding:** for ICO Drops Upcoming, only **3 of 50** visible rows
carried exact day-level dates. Consequently, this provider is valuable for
project/round discovery but not sufficient on its own for a fully dated
event calendar. Never invent a timestamp for the other 47 records.

### Decision for the next development round

| Area | Decision | Reason |
| --- | --- | --- |
| ICO Drops | Continue, with known date-coverage limitation | First-page HTTP extraction works; date coverage only 3/50 in this observation |
| CryptoEvents.global | Continue; prioritize bounded `Load more` pagination | 21/21 explicit date ranges on first page, but page coverage incomplete |
| Coindar | **Retain**; workstation HTML audit needed | Browser-accessible to project owner, GitHub Actions IP restrictions do not invalidate local access |

**Not yet accepted as proven:** ≥95% title/link accuracy or ≥90% date-field
accuracy against human-reviewed source ground truth, multi-day availability,
full historical reach, and a working local Coindar parser. These require
actual labeled samples or more than one independently observed live snapshot.
The existing tests confirm parser/structural behavior, not all those claims.

Next recommended reviewed PR after this evaluation: add a bounded,
resume-safe pagination adapter for CryptoEvents.global, or build Coindar
from an accessible locally captured HTML fixture if one is available.


## Gate 8 — Coindar HTML provider

The provider uses the public /en listing HTML, not the token-gated API.
The public API documentation requires an access token for API 2.0; this pilot
does not use that API or any paid service. The listing page itself exposes
event links, coin links, category tags, captions, summaries and date-group
headings. The parser performs no event-detail requests.

### Local validation

    uv run crypto-harvester probe --url https://www.coindar.org/en \
      --engine http --save-html .debug/coindar.html

    uv run crypto-harvester audit --source coindar --input-html .debug/coindar.html \
      --strict --min-records 10 --output artifacts/coindar-audit.json

    uv run crypto-harvester scrape --provider coindar --input-html .debug/coindar.html \
      --schema unified --db data/harvest.sqlite --output artifacts/coindar.json

The checked-in fixture is representative test HTML, not a raw page snapshot.
The parser is therefore tested for URL identity, card grouping, deduplication,
field extraction, exact-day handling, unknown dates, normalization and SQLite
integration. The live parser must be verified against a fresh local HTML snapshot
before its selectors/ancestor heuristics are treated as stable.

### Current scope and limits

- One listing page per run; pagination and past-event history are not followed.
- Numeric event ID in the public event URL is the provider-local stable key.
- Event dates are date-only from the visible UTC day heading; no time-of-day
  or timezone-aware instant is fabricated.
- Location, source reliability and importance are not inferred when absent.
- A 403/429/challenge page is reported as unavailable; no bypass is attempted.
- The GitHub runner's Coindar 403 is an environment-specific limitation, not
  evidence that a user's workstation cannot access the page.
