# Gate 10 — Coindar historical windows with durable checkpoints

## Purpose

Discover Coindar's Past Events through the **public HTML search interface**
without refetching the same pages at every invocation. One user-defined
`--from-date/--to-date` pair owns one SQLite-backed cursor, URL, page counter
and seen-event ID set. This is an *incremental discovery checkpoint*, not a
claim of complete historical archive coverage.

Each command fetches at most 1–5 public HTML pages. The next invocation
continues at the exact next URL discovered from the last successfully parsed
HTML page, with the same filters. It does not reconstruct arbitrary page URLs.

## Commands

```bash
# One window, two HTML listing requests maximum per invocation:
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --pages 2 \
  --output artifacts/backfill-october.json

# Repeat: continue from its saved next-page URL (no page-one request):
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --pages 2

# Zero-request status:
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action status

# Explicitly restart that same window; existing event history is preserved:
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action reset
```

The existing `scrape --provider coindar --section ended --pages N`
remains available and unchanged as a one-shot, non-checkpointed collection.

## Checkpoint tables (same SQLite database)

- `coindar_backfill_windows`: one row per exact date range, with current
  `next_url`, sequential page number, number of completed pages, state and
  stop reason. Two overlapping date ranges are independent checkpoints.
- `coindar_backfill_seen`: unique source event IDs observed in each
  window, with whether the event's explicit calendar date was within it.
  Unknown dates remain excluded from range membership.
- Existing `events` and `event_revisions`: all in-range, date-known
  events are reconciled using the project's standard versioned, idempotent
  upserts; no new event schema or external state store is introduced.

**Checkpoint ordering:** fetch → validate URL/response → parse listing →
sync normalized in-range events → advance checkpoint and seen set. A failure
before sync leaves no changed cursor; a crash after sync but before cursor
commit may refetch **one** page, which the SQLite event reconciliation safely
treats as unchanged. Already committed pages are not refetched on a normal
resume. No assumption of an atomic transaction across network + SQLite.

## Request efficiency and safety

- At most `--pages` source listing GET attempts per invocation, maximum 5.
- No public Coindar detail page fetch, paid/API-token call or browser
  automation. No retry loops or challenge bypass.
- Stops at a missing next-page link (`exhausted/no_next_link`) or a page
  adding no unseen source IDs (`stopped/no_new_ids`).
- A terminal checkpoint performs **zero** network requests until explicitly
  reset.
- Rejects an unexpected URL, cross-host redirect, query-parameter drift,
  invalid stored cursor or unsupported page number.
- An HTTP timeout/403/429 or parser error preserves the last **committed**
  page and cursor; a later invocation retries only the pending page.
- `--action reset` deletes just the requested window's checkpoint and seen
  set, **not** previously stored event records or revision history.

## Important accuracy limits

The site's `start` query is a public Past Events search filter. It is
**not** independently verified as a server-side inclusive date range.
`--to-date` is therefore a **client-side inclusion filter**: dates outside
the inclusive interval are excluded from SQLite writes and from in-window
counts, but the source may still return those cards, which use up the page
budget. Unknown date records cannot be certified as inside a window.

Search results may reorder between runs; a stored cursor saves requests but
does not guarantee a perfect snapshot of a continuously changing archive.
Reset and re-scan a window if comprehensive freshness is more important than
avoiding requests. Never infer that absent cards were deleted; the persistent
event store intentionally preserves them.

GitHub-hosted runners cannot reliably fetch Coindar (prior HTTP 403). The
offline tests cover resume, date-window isolation, SQL durability, HTTP
failure, tampered URLs, terminal no-request replay and CLI. A real multi-run
workstation check is required before claiming live historical completeness.
