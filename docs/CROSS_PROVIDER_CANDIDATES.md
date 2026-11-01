# Gate 13 — cross-provider event candidate links (review-only)

## Motivation

The shared SQLite store keeps source identities separate:
`icodrops:<source-id>`, `coindar:<source-id>`, and
`cryptoevents:<source-id>`. These provider-local identifiers are never
rewritten by this gate. The same underlying event might appear on more than
one calendar, but merging records based solely on a ticker, topic, or date
would create false matches.

Gate 13 adds a **conservative, read-only, offline candidate report**.
It does not alter `events`, `event_revisions`, backfill checkpoints,
source observations or the Unified Schema.

## Usage

```bash
uv run crypto-harvester candidates --db data/harvest.sqlite --limit 100 \
  --output artifacts/cross-provider-candidates.json

# Human-readable JSON printed to stdout:
uv run crypto-harvester candidates --db data/harvest.sqlite
```

No site requests, API tokens, LLM calls or embeddings. Run it after
collecting from at least two providers into the **same** SQLite database.

## Candidate rules

- Two stored event IDs must have **different providers**.
- Normalized titles must be identical after Unicode NFKC normalization,
  case folding and punctuation/whitespace normalization.
- Both events must have explicit day or date-range values, and their
  **start dates and end dates must match exactly**.
- Generic labels like `Listing`, `Launch` and `Airdrop` and titles
  with fewer than ten normalized characters are excluded.
- If both sources provide a project name, conflicting names veto the
  candidate. If both give a ticker, conflicting symbols also veto it.
- Missing project/ticker values are **neutral**, not positive evidence.
- Source IDs, source URLs, revision numbers, raw titles and dates are
  included for manual review.

This deliberately misses some true duplicates (e.g. imprecise dates,
different title wording, changed dates, abbreviated project names). This
tradeoff avoids casually merging different events that happen on one day.
Even a matching pair is **not** verified truth: the result is marked
`match_type=review_required`, never auto-merged.

## Output and scale

The JSON summary reports `examined`, `eligible_with_exact_calendar_dates`,
`candidates_total`, and `returned`. `--limit` caps just the returned
pairs (1–1000); all stored current snapshots are evaluated and the
untruncated total is preserved. Results have deterministic sorting.
Source records and immutable revisions are unaffected.

SQLite event snapshots are read directly; this report does not include
the separate Coindar coarse-date quarantine, because those records
lack confirmed exact calendar dates. Candidate matching uses the newest
stored event version, not every historical revision.

## Verification

Offline unit tests cover positive cross-provider matches, explicit
conflicts, same-provider rejection, false matches from coarse/unknown dates,
day/range differences, idempotent source histories, truncation and CLI
export errors. The new operation is intentionally **zero network** and
does not need a source-accessible runner. A zero-candidate live report
can be a correct and expected outcome.
