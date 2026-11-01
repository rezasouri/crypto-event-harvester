# Crypto Event Harvester

A **zero-paid-API, provider-independent** research pilot for scraping cryptocurrency event calendars.
Two implemented event providers (ICO Drops and CryptoEvents.global) support native/unified
JSON/CSV exports and incremental SQLite version history. Coindar remains a feasibility
candidate for normal workstation/browser access, not yet a normalized provider.

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
normalization/URL checks and offline first-page comparisons. Coindar link
evidence is **not** mislabeled as parsed event records. See the
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
