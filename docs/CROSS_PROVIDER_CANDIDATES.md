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

## Gate 15 — manual reconciliation

Candidate reports remain proposals for a human. Gate 15 adds a persistent
canonical identity and membership layer without changing the provider event
IDs or selecting/combining source fields. A canonical identity is only a
stable link between independently accessible source events; source payloads
and immutable revisions remain authoritative and separate.

The first write to a version 1 database applies additive migration version 2.
It creates `canonical_events`, `canonical_event_members`, and the
append-only `reconciliation_decisions` audit log. Existing event rows,
revision history, timestamps and unrelated tables are preserved. A member
can belong to only one canonical identity, and a canonical identity cannot
contain two events from the same provider.

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action status
uv run crypto-harvester reconcile --db data/harvest.sqlite --action pending \
  --strategy similarity --min-score 0.80 --limit 100
uv run crypto-harvester reconcile --db data/harvest.sqlite --action accept \
  --left-event-id icodrops:123 --right-event-id coindar:456 --strategy similarity
uv run crypto-harvester reconcile --db data/harvest.sqlite --action reject \
  --left-event-id icodrops:123 --right-event-id coindar:456 --strategy similarity \
  --note "Different events"
uv run crypto-harvester reconcile --db data/harvest.sqlite --action list --limit 100
uv run crypto-harvester reconcile --db data/harvest.sqlite --action history --event-id coindar:456
```

Accept and reject re-read both current snapshots, recalculate the selected
strategy's evidence, capture current revision numbers and write in one
immediate SQLite transaction. Old exported reports alone cannot authorize a
decision. Repeated identical decisions for the same pair, revisions,
strategy and outcome are idempotent. Opposite outcomes for the same event
pair and revision pair are conflicts, even when the requested matching
strategy differs. A later source revision can be reviewed again if the pair
remains eligible. Re-accepting it refreshes each membership's
`source_revision_at_link` to the reviewed revision without creating a
duplicate membership.

Pending candidates omit pairs already represented by the same canonical
identity while both members' linked revisions are current. If either source
event gains a revision, the pair can reappear for review. The canonical
`list` action reports both `source_revision_at_link` and `current_revision`,
plus `membership_stale`; `status` reports the number of stale memberships.
A rejected decision remains audit-only and does not detach an existing
canonical membership. If both events are already in different identities,
or adding an event would duplicate a provider in an identity, the transaction
fails with an explicit conflict; identities are never automatically merged.
All decision evidence is retained for history.

Reconciliation never rewrites or deletes source events or revisions and does
not create synthetic merged source events. The canonical layer contains no
copied event payload and does not resolve source-data conflicts.

The report includes component scores, date-overlap relation, matching
project/ticker fields, threshold path, both provider IDs, URLs and revision
numbers. A zero-candidate result remains valid when source calendars have
different coverage.
