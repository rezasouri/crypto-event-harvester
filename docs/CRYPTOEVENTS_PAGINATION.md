# Gate 7 — Bounded pagination of CryptoEvents.global

## Verified public site behavior (November 1, 2026)

The homepage includes 20 calendar cards plus featured copies. Its `Load more`
button calls the website's first-party theme endpoint:

- `POST https://cryptoevents.global/wp-content/themes/cryptoevents/ajax/filters.php`
- Form keys: `action=index`, `offset=20`, `postperpage=20`,
  `categories[]=16`, `index_filters[start_date]=<UTC-day-start>`.
- A **single bounded POST** returned a JSON list of 20 event objects in the
  live GitHub-hosted diagnostic, including links, title, excerpt, city,
  explicit timezone offset label, start and end timestamps.

The endpoint is a website-internal HTTP interface, **not a paid API**.
This implementation intentionally avoids browser automation for additional
pages. It does not require or use an API key.

## Commands

```bash
# Existing behavior: first page only, one listing GET.
crypto-harvester scrape --provider cryptoevents --schema unified \
  --output artifacts/first.json

# First page + one additional batch = maximum 1 GET + 1 POST (without redirects).
crypto-harvester scrape --provider cryptoevents --pages 2 \
  --schema unified --output artifacts/two-pages.json

# Persist all fetched event records with idempotent SQLite version history.
crypto-harvester scrape --provider cryptoevents --pages 3 \
  --schema unified --db data/harvest.sqlite --output artifacts/more.json
```

- The `--pages` value is **1 to 5**. Default is 1 for backward compatibility.
- For 2 pages, network plan is **1 homepage GET + 1 paginated POST**;
  up to 5 pages means at most 1 GET + 4 POSTs, excluding HTTP redirects.
- Each POST matches the current website's `Load more` form parameters.
- Each batch is deduplicated by canonical source event ID. A repeated or empty
  response stops the loop; a batch shorter than 20 ends pagination.
- A 403/429, invalid JSON or unexpected object response is an explicit
  failure. No challenge bypass, retries or unlimited crawl.
- More than one page is currently HTTP-only. Existing Playwright/Selenium
  first-page options and `--input-html` remain supported for one page.
- `--db` persists **all** collected records, even when `--limit` limits
  the exported rows. A repeat scan is idempotent in SQLite.

## Time and data integrity

The JSON page contains Unix timestamps plus explicit `(UTC±HH:MM)`
labels. Date-only start/end values are computed using that explicit offset,
not the machine's timezone. If an event's offset is missing/invalid,
calendar dates remain unknown rather than guessed.

Like the first-page provider, event times are **not** stored in
`start_at_utc` or `end_at_utc` under the unified schema: this remains
a date-precision pilot. Title and description HTML entities are decoded
and markup removed. Only valid same-host event links are accepted.

## Explicit limitations

- The site's filter defaults to the current UTC day; event ordering/content
  can change between runs or at a calendar boundary.
- This is **bounded forward pagination**, not complete historical backfill.
- Persisting to SQLite makes repeated scans idempotent but is **not a
  durable page cursor or cross-run resume**. Cursor persistence can be added
  as a separate reviewed gate when needed.
- Only the CryptoEvents.global provider is paginated here; Coindar remains
  a separate locally accessible source to investigate with captured HTML,
  and ICO Drops uses its own Upcoming/Active/Ended listing sections.
- Zero paid APIs; no claim of verified full-history completeness or
  manual field-level accuracy.

## Verification

Offline tests mock the HTTP posts and cover date boundaries, timezone
offsets, malformed responses, same-host URL checks, duplicate IDs, stop
conditions, request bounds and CLI/SQLite integration.

A dedicated GitHub Actions workflow makes one real listing GET and one
real pagination POST, verifies at least 30 unique normalized records, and
uploads the database, JSON records and a report as artifacts.
