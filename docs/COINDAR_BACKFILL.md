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


## Corrected historical date semantics and existing checkpoints

The first live workstation test of the October 1–November 1, 2026 range
found 80 distinct event IDs over four pages, but none passed the local
date-range filter. Inspection of the public Coindar **Past Events** link
shows why: `start=2026-11-01` returns October 31 and earlier.
The public `start` argument is an **exclusive upper date cutoff**,
not a lower bound.

The corrected search starts at **one day after `--to-date`** and
moves backward through numbered page links. For `--from-date 2026-10-01`
and `--to-date 2026-11-01`, the source request therefore uses
`start=2026-11-02`; the inclusive lower and upper range bounds
remain enforced against parsed event dates locally.

Existing checkpoints created before this correction were initialized with
the wrong search anchor. For safety, a new nullable `anchor_date`
column identifies these obsolete checkpoints. Their `status` includes
`requires_reset: true` and the next `run` refuses to continue them,
instead of silently skipping events. **Reset deletes only the checkpoint
and its seen-ID set; normalized event rows and revisions are preserved.**

```bash
# Use these exact dates when repairing the original local test window.
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action status

uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action reset

uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --pages 2
```

Per-run results additionally report counts of dated events falling
before/after the requested interval, unknown-date records, and the
minimum/maximum observed day. These are useful for diagnosing whether
a zero-insert scan resulted from the source response, date extraction
or persistent deduplication. Coarse month/quarter dates are not
automatically assigned a made-up day and remain excluded.

The website's page ordering and full historical completeness still require
further observation. The newly validated upper-cutoff behavior corrects
the start of the search, but does **not** prove all older events will
appear on a finite number of pages.


## Unknown-date record retention (post-live correction)

A live two-page scan returned 40 distinct source IDs, but only 26 records
had exact calendar dates; those 26 were **already stored** by the prior
Coindar scrape (reported as unchanged). The other 14 had no exact day.
Previously their IDs advanced the cursor while their full native payloads
were discarded. The checkpoint is now capture-versioned (**version 2**):
older checkpoints require explicit reset and re-scan so those records
cannot silently be skipped again.

New table `coindar_backfill_undated` retains native public listing
metadata for date-unknown records, **separately** from normalized events.
These records do **not** count as in-window events and never overwrite
previously stored date-verified normalized records. The undated cache is
independent of the per-window cursor and survives `--action reset`.
If a future successful in-window observation supplies an exact day for
the same event, the obsolete undated cache entry is removed.

Inspection requires **zero network requests**:

```bash
uv run crypto-harvester backfill --db data/harvest.sqlite \
  --from-date 2026-10-01 --to-date 2026-11-01 --action undated \
  --limit 20 --output artifacts/coindar-undated.json
```

The status report now provides `undated_saved` and `capture_version`.
The `undated` field in a run describes observed unknown-date records;
`undated_saved` counts quarantined distinct IDs for the window.
A one-time explicit checkpoint reset is necessary to recover raw
metadata from already-skipped pages. **This intentionally costs at most
the bounded re-scan request budget, not unlimited retries.**
