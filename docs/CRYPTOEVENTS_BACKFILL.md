# Gate 12 — checkpointed CryptoEvents.global collection

## Purpose

Gate 7 added a bounded one-shot paginator. If the process stopped, a later
run fetched the homepage again and restarted the AJAX offsets. Gate 12 adds
a durable per-UTC-day checkpoint so a later invocation resumes at the next
offset without repeating the homepage request.

This is a public website listing workflow. It uses the existing homepage
GET and the site's observed public `filters.php` listing POST; it does not
use a paid API, an API key, event-detail pages, browser automation, retries
or request-limit evasion.

## Commands

Use one fixed UTC date for the whole crawl:

```bash
# Initial run: at most two listing requests (homepage GET + one AJAX POST).
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --pages 2

# Resume: continues from the stored next offset; no homepage GET.
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --pages 2

# Inspect current progress without network requests.
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --action status

# Reset this cursor/seen set only. Event rows and revisions survive.
uv run crypto-harvester cryptoevents-backfill --db data/harvest.sqlite \
  --start-date 2026-11-01 --action reset
```

If `--start-date` is omitted, the current UTC date is selected. For a
multi-run crawl, always pass the same explicit date on every invocation;
the stored epoch is frozen at the initial run and reused on resume.

## Request and page bounds

- Each invocation accepts `--pages 1..5` (default 2).
- The first request is one homepage GET; subsequent requests are AJAX POSTs
  at offsets 20, 40, 60, ... .
- A resumed invocation does not refetch the homepage.
- Each crawl is hard-capped at 100 total pages (at most 2,000 raw listing
  entries before deduplication). The cap is per UTC-day crawl.
- Empty responses, a page with no new event IDs, or a raw response shorter
  than 20 items mark the crawl complete.
- HTTP errors, malformed JSON and unexpected payload shapes fail explicitly;
  no retry is made and the cursor does not advance.

The UTC day is the same `index_filters[start_date]` anchor used by the
existing paginator. It is frozen in SQLite so a crawl resumed after midnight
does not silently switch its server-side filter.

## Persistence and crash behavior

Each page is parsed, normalized and synchronized to the existing SQLite
event/revision store before the checkpoint is advanced. The checkpoint then
records the next offset, total successful pages, unique IDs seen, state and
stop reason. If the process stops after event sync but before cursor update,
the same page may be requested once more on resume; the event store's
content hashes and revision logic make that replay idempotent. A failed
fetch or parse never advances the cursor.

The per-crawl seen-ID set prevents a repeated AJAX batch from being mistaken
for new coverage. The existing event table is shared with ICO Drops and
Coindar, but cursor/seen state is provider-specific.

## CLI result fields

The run command reports:
- `pages_this_run`, `total_pages`
- `requests.homepage_gets`, `requests.ajax_posts`, `requests.total`
- `new_ids_this_run`, `unique_ids_seen`
- SQLite sync counts: inserted, updated, unchanged, stale_skipped
- `next_offset`, `state`, and `stop_reason`

`status` and `reset` make no network requests. Reset removes only the
requested UTC-day checkpoint and its seen-ID set; normalized event rows and
revision history remain intact.

## Acceptance limits

A successful offline test suite proves parser, cursor and persistence logic,
not that the live site will always return the same page contents. Run the
command from a workstation where CryptoEvents.global is accessible and share
the JSON output. The observed request budget and number of unique events
should be reviewed before treating historical coverage as adequate.
