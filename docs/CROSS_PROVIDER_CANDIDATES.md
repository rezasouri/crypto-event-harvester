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


## Gate 14 — scored lexical similarity (opt-in)

Gate 14 adds an opt-in similarity strategy while keeping the existing
`exact` strategy as the CLI default for backwards compatibility:

```bash
uv run crypto-harvester candidates --db data/harvest.sqlite \
  --strategy similarity --min-score 0.80 --limit 100 \
  --output artifacts/cross-provider-similarity.json
```

### Candidate generation and safeguards

- Only different-provider events with explicit `day` or `range` calendar
  dates are eligible. Date intervals must overlap; month, quarter and
  unknown dates are never converted into guessed days.
- Event pairs are generated from overlapping date windows, not by comparing
  every record to every other record. Each provider's records are kept
  separate; same-provider pairs are ignored.
- A title score combines token-set Jaccard similarity (45%) and
  `SequenceMatcher` similarity on sorted meaningful words (55%). Punctuation,
  case, Unicode compatibility forms and word order are normalized.
- Generic event terms (for example `launch`, `listing`, `conference`,
  `upgrade`, `airdrop`) are excluded from the meaningful title tokens.
- Explicitly different project names or tickers veto a pair. A
  `conference` versus `token_event` type conflict also vetoes it.
- If project or ticker agrees explicitly, title similarity must still be at
  least 0.70. Without shared entity metadata, title similarity must be at
  least 0.90 and both titles must contain at least two meaningful tokens.
- Date overlap contributes a bounded score (exact interval, same start/end,
  containment or partial overlap). The default minimum combined score is
  0.80; `--min-score` can be set from 0 to 1.
- Candidates are sorted by score; `--limit` retains the highest-scoring
  pairs without holding the entire result set in memory.

### Interpretation

The score is a deterministic **lexical/evidence score**, not a probability
and not an ML confidence estimate. It does not understand synonyms or event
semantics. Even the `high` band is only a prioritization hint:
all outputs remain `review_required`; there is no automatic merge,
canonical identity assignment or source-row mutation. No HTTP requests,
paid API calls, embeddings or model inference are used.

The report includes component scores, date-overlap relation, matching
project/ticker fields, threshold path, both provider IDs, URLs and revision
numbers. A zero-candidate result remains valid when source calendars have
different coverage.
