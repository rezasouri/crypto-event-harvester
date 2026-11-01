# Crypto Event Harvester

A **zero-paid-API, provider-independent** research pilot for scraping cryptocurrency event calendars.
Three independent providers (ICO Drops, CryptoEvents.global and Coindar) support native/unified
JSON/CSV exports and incremental SQLite version history. Coindar parsing is implemented for
saved/local HTML; live access remains environment-dependent and must be verified on the workstation.

## Pilot workflow

See [the staged roadmap](docs/PILOT_ROADMAP.md). Each gate ships in a separate pull request and
is merged only after owner approval.

Initial candidates: [Coindar](https://www.coindar.org/en), [ICO Drops](https://icodrops.com/),
[CryptoEvents.global](https://cryptoevents.global/). Their extraction feasibility is not yet verified.

## Setup (Python 3.11+)

```bash
python -m pip install -e '.[dev]'
crypto-harvester probe --url 'https://example.org/'
pytest -q
ruff check .
```

Alternatively, use `uv sync --extra dev` and prefix commands with `uv run`.

### Inspect a page and count candidate DOM nodes

```bash
crypto-harvester probe \
  --url 'https://example.org/' \
  --selector 'article' \
  --save-html '.debug/example.html'
```

By default this makes **one HTTP request**, parses returned HTML and prints JSON diagnostics
(URL, status, title, HTML size, selector matches, likely access issue). It does **not** fetch
individual event pages and does not bypass CAPTCHA or rate limits. Saved HTML is local/debug only.

### JavaScript-rendered pages (optional)

```bash
python -m pip install -e '.[playwright]'
playwright install chromium
crypto-harvester probe --url 'https://example.org/' --engine playwright --wait-for 'article'
```

Selenium is supported as an alternative: install with `pip install -e '.[selenium]'` and have
Chrome/Chromedriver available. Browser probes load extra assets and may cause more network
requests than the HTTP engine. `--wait-for` applies only to browser engines.

## Design principles

- One provider per source; shared normalization and persistence come in later gates.
- No mandatory authentication, subscriptions or paid API calls.
- Keep requests low; record unavailable, rate-limited and security-challenge pages rather than
  mistaking them for valid calendar content.
- No invented times: event date precision and revision history will be explicit in future gates.
- Tests run offline using mocked responses; live source probing is a separate activity.

## Project status

**Gate 0 — Foundation:** developer CLI, fetch strategies and offline tests. Site-specific
providers, normalized event records and storage are planned for subsequent reviewed pull requests.


## Gate 1: evaluate event sources (HTTP first)

```bash
crypto-harvester sources --source all --engine http --json-out artifacts/sources.json
# Optional browser rendering after installing the playwright extra:
crypto-harvester sources --source coindar --engine playwright
```

This command examines **one listing page per source**, detects candidate detail links and
reports access failures without treating candidates as confirmed extracted events.
See [source-feasibility methodology](docs/SOURCE_FEASIBILITY.md). A separate nonblocking
GitHub Actions workflow attaches live HTTP evidence to the PR.


## Gate 2: real ICO Drops listings

```bash
crypto-harvester scrape --provider icodrops --section upcoming --output upcoming.json
crypto-harvester scrape --provider icodrops --section ended --format csv --output ended.csv
```

The provider reads real ICO Drops HTML rows (not candidate navigation links).
Upcoming events without explicit dates remain undated. See
[the ICO Drops pilot details](docs/ICODROPS_PILOT.md). Coindar remains
a planned provider despite blocking GitHub Actions IPs.


## Gate 3: CryptoEvents.global conference listings

The second provider extracts real conference/event cards from HTML using direct HTTP;
no API key or paid service is needed:

```bash
crypto-harvester scrape --provider cryptoevents --output conferences.json
crypto-harvester scrape --provider cryptoevents --format csv --output conferences.csv
```

Conference dates are preserved as date-only values or ranges, with null for
ambiguous dates. Featured/regular duplicates are consolidated. Only the first
visible event listing is covered (Load more and historical crawling are future
work). See [Gate 3 details](docs/CRYPTOEVENTS_PILOT.md). The Coindar candidate
remains on the roadmap for local/browser-based trials.


## Gate 4: one normalized event contract

Both providers now support a versioned, provider-independent output format
without changing their native exports:

```bash
crypto-harvester scrape --provider icodrops --section ended --schema unified \
  --output tokens.json
crypto-harvester scrape --provider cryptoevents --schema unified \
  --output conferences.json
```

See [Unified schema v1](docs/UNIFIED_SCHEMA.md). Date-only or unknown event
dates **never** become inferred UTC timestamps; the two providers currently
have no verified event timezone. Coindar is still a planned candidate for
local/browser trials.


## Gate 5: incremental local SQLite storage

Opt into persistent snapshots, changed-field revision history, and
repeatable idempotent scraping without adding more source requests:

```bash
crypto-harvester scrape --provider icodrops --section upcoming \
  --schema unified --db data/harvest.sqlite
crypto-harvester scrape --provider cryptoevents \
  --schema unified --db data/harvest.sqlite
crypto-harvester store --db data/harvest.sqlite --action status
```

Repeated identical content does not create revisions. Changed source data
adds an immutable revision and older observations never overwrite newer ones.
Missing records are never deleted based on a short listing fetch. See
[SQLite incremental storage](docs/SQLITE_STORAGE.md). Coindar is still on
the shortlist for local/browser feasibility tests.


## Gate 6: first-page pilot evaluation and Coindar local diagnosis

```bash
crypto-harvester audit --source icodrops --min-records 20 --strict \
  --output artifacts/audit-icodrops.json
crypto-harvester audit --source cryptoevents --min-records 10 --strict \
  --output artifacts/audit-cryptoevents.json

# On a workstation where Coindar is accessible:
crypto-harvester audit --source coindar --engine http \
  --output artifacts/coindar-http.json
crypto-harvester audit --source coindar \
  --input-html ~/Downloads/coindar.html --output artifacts/coindar-offline.json
```

The audit reports parsed-record quality, optional pagination signals,
normalization/URL checks and offline first-page comparisons. Coindar is parsed
from saved HTML without event-detail requests. See the
[pilot evaluation report](docs/PILOT_RESULTS.md) for methodology and limits.


## Gate 7: bounded multi-page CryptoEvents collection

```bash
# First page (default, backwards-compatible)
crypto-harvester scrape --provider cryptoevents --schema unified

# First page + two additional public listing batches, at most three requests
crypto-harvester scrape --provider cryptoevents --pages 3 \
  --schema unified --db data/harvest.sqlite \
  --output artifacts/cryptoevents.json
```

The first page uses normal HTTP; each optional extra page uses the site's
public Load-more POST. Deduplication and early stops keep fetches bounded.
The cap is five pages, with no automatic retries or paid APIs.
See [pagination implementation and limits](docs/CRYPTOEVENTS_PAGINATION.md).
Coindar remains on the roadmap for a local-HTML-backed parser.


## Gate 8: Coindar public HTML provider

On a workstation where the public Coindar calendar is accessible, capture its
listing HTML once and use the independent parser:

    uv run crypto-harvester probe --url https://www.coindar.org/en \
      --engine http --save-html .debug/coindar.html

    uv run crypto-harvester scrape --provider coindar --input-html .debug/coindar.html \
      --schema unified --min-events 10 --db data/harvest.sqlite \
      --output artifacts/coindar.json

    uv run crypto-harvester audit --source coindar --input-html .debug/coindar.html \
      --strict --min-records 10 --output artifacts/coindar-audit.json

The parser extracts numeric source IDs, title, coin, ticker, category, description
and explicitly grouped calendar-day dates. Unknown month/quarter labels stay
unknown. It uses one listing page only; pagination and historical coverage remain
separate work. GitHub-hosted runners have previously received HTTP 403 for Coindar,
so live CI success is not a merge requirement. The representative offline fixture
covers parser behavior; validate against a fresh local snapshot before production use.


## Gate 9: bounded Coindar pagination and historical listing

The Coindar public listing now supports up to five linked HTML pages, with
no detail-page requests and no paid API. Historical records use the website's
Past Events search filter and can be persisted to the same SQLite database:

```bash
uv run crypto-harvester scrape --provider coindar --section upcoming \
  --pages 3 --schema unified --db data/harvest.sqlite \
  --output artifacts/coindar-upcoming.json

uv run crypto-harvester scrape --provider coindar --section ended \
  --start-date 2026-11-01 --pages 3 --schema unified \
  --db data/harvest.sqlite --output artifacts/coindar-past.json
```

See [Coindar bounded pagination](docs/COINDAR_PAGINATION.md).
The three-page mode requires live HTTP access from a machine where Coindar
can be opened; CI validates the same logic with offline fixtures.


## Gate 10: incremental, checkpointed Coindar historical discovery

Unlike one-shot Coindar pagination, `backfill` saves each successful public
HTML listing page to SQLite and resumes directly from the next visible link:

```bash
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --pages 2
# Resume with the same command: do not refetch already committed pages.
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --pages 2
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action status
```

See [Coindar historical checkpoints](docs/COINDAR_BACKFILL.md). The upper
date bound is a local filter, **not** a verified upstream server-side range.
Unknown dates are not invented or included in a historical window.


## Coindar month and quarter precision (Gate 11)

Coindar month-only records (for example `October 2026`) now retain
`date_precision=month` and `period_key=2026-10`; quarter-only labels
retain `quarter` and `YYYY-QN`. The Unified Schema is version 1.1, and
no exact day, UTC timestamp or timezone is fabricated.

You can update previously cached month-only records **without any web
requests** and inspect them locally:

```bash
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action enrich
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action undated --limit 20
```

See [Unified Schema](docs/UNIFIED_SCHEMA.md) and
[Coindar backfill](docs/COINDAR_BACKFILL.md).


## Gate 12: checkpointed CryptoEvents.global pagination

The ordinary `scrape --pages` command remains a bounded one-shot scan. For
cross-run continuation, use the separate checkpointed command:

```bash
# Start a crawl for a fixed UTC day; fetch at most two listing pages this run.
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --pages 2

# Resume from the saved offset; the homepage is not fetched again.
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --pages 2

# Inspect or reset only this day's checkpoint (event rows/revisions are preserved).
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --action status
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --action reset
```

The UTC start-day filter is frozen in SQLite and must be supplied consistently
when resuming. Each invocation makes at most five listing requests; a crawl
has a hard cap of 100 pages. Each page is synchronized to the shared event
store before the cursor advances, so a crash can repeat at most the last
page and idempotent storage prevents duplicate revisions. No event-detail
pages, paid APIs, retries or unbounded requests are used. See
[checkpointed CryptoEvents pagination](docs/CRYPTOEVENTS_BACKFILL.md).
