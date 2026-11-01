# Gate 9 — Coindar bounded pagination and Past Events collection

## Verified public HTML links

The public Coindar calendar exposes numbered pagination anchors pointing to
`/en/search?page=N&...&sort=0&order=0`. The public **Past Events** tab links
to the same search endpoint with `sort=3&order=1`, using a `start` date
filter. This is the publicly displayed website HTML, **not** Coindar API 2.0
and does not require an access token.

The crawler starts from the existing homepage for Upcoming, or a fixed
`/en/search` Past Events link. It follows **only the visibly linked next
numbered page** with the exact same non-page filter parameters.

## Example commands

```bash
# Default (unchanged): one listing page and no detail pages.
uv run crypto-harvester scrape --provider coindar --schema unified \
  --output artifacts/upcoming.json

# One homepage GET + at most two GETs for linked numbered pages.
uv run crypto-harvester scrape --provider coindar --section upcoming --pages 3 \
  --schema unified --db data/harvest.sqlite \
  --output artifacts/upcoming-pages.json

# Historical discovery: Past Events starts at one fixed filter date.
uv run crypto-harvester scrape --provider coindar --section ended \
  --start-date 2026-11-01 --pages 3 \
  --schema unified --db data/harvest.sqlite \
  --output artifacts/past-events.json

# Inspect the growing persistent store, not the old missing rows as deletions.
uv run crypto-harvester store --db data/harvest.sqlite --action status
```

The page bound is **1–5**, default 1. No auto-retries, proxy rotation,
challenge bypass, or event-detail requests. Physical HTTP requests could
include redirects; the implementation counts listing fetch attempts.

The `--start-date` option anchors the public past-results filter; it does
**not** imply historical events older than that date are fully covered.
Repeat the same fixed-date scans if tracking updates to a page window, and
keep the underlying SQLite revision history across successful runs.

## Early stop and data safety

- Only HTTPS links to `www.coindar.org` or `coindar.org` are allowed.
- Only numbered `/en/search` links for the next sequential page are followed.
- A non-page filter change (sort/order/start/coin/category) stops pagination.
- A page with no new event IDs terminates the crawl.
- Missing next-page links stop without requesting a guessed page number.
- Errors or access blocks on later pages **abort the batch**; a partial run
  is never falsely written as a successful complete snapshot in SQLite.
- IDs are deduplicated across pages before native/unified exports and storage.
- `--limit` changes export size only; all fetched events are stored in SQLite.
- No source-inferred UTC instants are introduced from calendar-only dates.

## Acceptance evidence and scope

The GitHub-hosted environment previously returned 403 from Coindar. As a
result **Coindar must not be rejected as a provider**, but it is incorrect
to require GitHub Actions to fetch the live site successfully. CI runs
offline fixture tests for request bounds, URL safety, stop behavior,
pagination/ended filter semantics and SQLite integration.

The earlier local workstation audit verified **20 unique dated Coindar
calendar events with descriptions on a real saved homepage**. This gate's
multi-page path still needs an **independent two- or three-page live
workstation run**. The GitHub-hosted 403 cannot be used to infer a result for
that workstation. There is no claim of complete site-history backfill,
durable page-cursor checkpointing or cross-provider semantic deduplication.
