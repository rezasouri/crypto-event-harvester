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
contain two events from the same provider. Gate 16 adds an additive migration
from schema version 2 to version 3 for the canonical membership lifecycle
audit log and its integrity triggers.

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


## Gate 16 — canonical identity lifecycle

Gate 16 adds controlled correction operations for manual canonical links.
They never rewrite or delete source event rows or immutable source revisions.

### Unlink one member

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action unlink \
  --canonical-event-id canonical:... --event-id coindar:456 \
  --note "Reviewed as a different event"
```

The operation removes only the selected membership and writes an immutable
`unlinked` audit action in the same transaction. The canonical identity is
retained even when its last member is unlinked; it appears in `list` with
`lifecycle_status=empty` and remains available to historical decisions.

### Split a proper subset into a new identity

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action split \
  --canonical-event-id canonical:... --event-ids coindar:456 cryptoevents:789 \
  --note "These source entries describe a separate event"
```

The selected IDs must be unique current members of the source identity.
The operation must move a proper subset, leaving at least one member in the
original identity. A new canonical identity is created, selected memberships
are moved without changing their linked revision or timestamp, and one
immutable `split` audit row is recorded per moved event. All changes commit
or roll back together.

### Audit and status

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action membership-history
uv run crypto-harvester reconcile --db data/harvest.sqlite --action membership-history \
  --canonical-event-id canonical:...
uv run crypto-harvester reconcile --db data/harvest.sqlite --action membership-history \
  --event-id coindar:456
uv run crypto-harvester reconcile --db data/harvest.sqlite --action status
uv run crypto-harvester reconcile --db data/harvest.sqlite --action list
```

The `canonical_membership_actions` table is append-only. SQLite triggers
reject direct membership deletion or movement unless a matching audit action
was inserted in the same transaction; audit rows themselves cannot be
updated or deleted. Status reports unlink/split action counts and retained
empty identities. Schema version 2 databases migrate additively to version 3,
preserving existing canonical memberships, decisions, source events and
revision history.

## Gate 17 — retrospective matching-quality evaluation

Gate 17 adds a read-only evaluation report over source-event revision pairs
that a human has already accepted or rejected. It evaluates the exact matcher
and sweeps configurable similarity thresholds without changing the matcher,
canonical identities, source snapshots or decision history.

Run the report:

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action evaluate
uv run crypto-harvester reconcile --db data/harvest.sqlite --action evaluate \
  --thresholds 0.75 0.80 0.85 0.90 0.95 --min-reviewed-pairs 30
```

The evaluator loads the immutable `event_revisions` payloads referenced by
each human decision. A later source update therefore cannot silently change
the evidence used for a historical evaluation. Repeated reviews of the same
event pair and revision pair are counted once; contradictory labels fail
closed.

The report includes reviewed-pair counts, accepted/rejected labels, exact
strategy metrics, similarity threshold confusion matrices, precision, recall,
F1, specificity, accuracy, provider-pair coverage and similarity-score
coverage. The default minimum of 30 reviewed pairs is a reporting status
guard, not a claim that 30 examples are statistically sufficient.

### Interpretation and limits

- Metrics apply only to historically reviewed event-revision pairs. They do
  **not** estimate recall over every possible or unreviewed cross-provider pair.
- Reviewed pairs are selected by earlier candidate-generation rules, so the
  sample is not random and may contain selection bias.
- Human accept/reject decisions are treated as labels; inter-reviewer
  agreement and independent source verification are not measured.
- Similarity scores are ranking evidence, not probabilities or calibrated
  confidence.
- With no human decisions, the command returns `status=no_reviewed_pairs`
  and null ratio metrics. It does not fabricate a benchmark.
- The operation is offline and read-only: zero source requests, no database
  writes, no automatic merges and no schema migration.

## Gate 18 — human review dataset workflow

Gate 18 turns the review-only candidate list into an auditable labeling loop
for the Gate 17 quality evaluator. It adds CSV export and import without a
new database table or schema migration.

### Export a review batch

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite \
  --action review-export --strategy similarity --min-score 0.80 \
  --limit 100 --output artifacts/reconciliation-review.csv
```

The UTF-8 CSV contains a deterministic candidate ID, both provider event IDs,
the exact source revision numbers, titles, dates, project/ticker metadata,
source URLs, score/evidence and blank `review_decision` / `review_note`
columns. Reviewers should fill `review_decision` with `accept`, `reject`,
or `unsure` and may add a short explanation in `review_note`. Blank and
unsure rows are skipped. Export is read-only, makes zero source requests and
does not migrate a version 1 database.

The export is a bounded candidate pool, not a random or population-
representative sample. `candidates_total` and `truncated` make any limit
visible. Similarity scores are prioritization evidence, not probabilities.

### Validate and import labels

First validate the edited CSV without writing anything:

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite \
  --action review-import --input artifacts/reconciliation-review.csv
```

Only after reviewing the dry-run report, explicitly persist decisions:

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite \
  --action review-import --input artifacts/reconciliation-review.csv --apply
```

Import checks the deterministic candidate ID, rejects duplicate IDs and
contradictory labels for the same source revision pair, and revalidates the
pair using the selected matching strategy. Most importantly, it checks both
exported revision numbers again inside the write transaction. If either
source event changed after export, that row is reported as stale and is not
applied. A conflict or stale row does not silently change its label; other
independent valid rows can still be processed and are reported individually.
Re-importing an already recorded identical decision is idempotent.

The CSV's descriptive fields are for reviewer context only. Import does not
trust edited titles, dates, provider labels, scores or evidence JSON: it
re-reads the authoritative current event snapshots and recomputes evidence.
The candidate ID detects accidental row/pair edits; it is not an authorization
or cryptographic signature.

Accepted rows use the existing manual reconciliation transaction and can
create/update canonical memberships. Rejected rows add audit-only decisions.
Neither outcome rewrites source event records or immutable revisions.
The apply operation may perform the existing additive schema migration when
run against an older database; dry-run validation does not migrate.

After importing enough labels, run the Gate 17 report:

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite --action evaluate
```

Metrics remain scoped to reviewed candidate pairs. Candidate-generation
selection bias and the absence of labels for unreviewed pairs still prevent
population-recall claims. Human review quality and reviewer agreement are
not inferred from the CSV itself.

## Gate 19 — coverage diagnosis and related-record hints

This gate intentionally does **not** loosen the strict identity matcher. The
pilot sources currently differ in scope: Coindar often records a project's
participation in an event while CryptoEvents describes the conference itself.
Such records can be related without being the same canonical event.

Run the read-only diagnosis:

```bash
uv run crypto-harvester reconcile --db data/harvest.sqlite \
  --action diagnose-coverage --limit 20 \
  --min-title-similarity 0.55 --min-score 0.80
```

The JSON report contains:

- Counts of source records, date precision, event types and populated metadata
  fields by provider.
- Number of dated cross-provider pairs whose date intervals overlap.
- First blocking reason from the existing strict matcher for each overlapping
  pair. These counters are *diagnostic*, not labeled outcomes.
- A separate ranked set of possible **relationships** based on shared title
  tokens, lexical title similarity, dates and optional location/participation
  language in Coindar descriptions or source URL slugs. The signal sources are
  reported for auditability. A title-related Coindar participation record may
  describe a distinct action from the CryptoEvents conference.
- Explicit `not_an_identity_decision` markers, zero requests and zero database
  modifications. No new tables, migrations, model calls or APIs.

The relationship hints are **not** eligible for the Gate 18 `review-import`
identity workflow. They cannot be used to automatically link, accept or reject
canonical identities. This prevents conflating *participates in* with *is the
same event as*. A separate independently reviewed relationship taxonomy and
label store would be needed before using those signals for training/evaluation.

Even when the diagnosis finds no strict candidates, this does not prove that
the full event population has no duplicates. To measure identity precision or
recall, collect independently verified positive and negative pairs from
sources with genuinely overlapping event taxonomies.

## Gate 20 — human-reviewed semantic event relationship graph

Gate 20 adds a relationship graph for directed semantic links between source
event records. It is deliberately separate from canonical identity:

- \`same_event\` is **not** a graph predicate. It remains managed exclusively by
  \`reconcile accept/reject\` and canonical memberships.
- \`participates_in\`, \`sponsors\`, \`speaks_at\`, \`organizes\`, \`attends\`,
  \`exhibits_at\` and \`related_to\` are directed semantic predicates.
- The subject is the source event record making or carrying the assertion; the
  object is the event record it refers to. For example, a Coindar project
  participation record can be the subject and a CryptoEvents conference record
  the object.
- Every accepted/rejected review records the exact subject/object source
  revisions and a snapshot of their relevant metadata plus reviewer evidence.
- Accepted edges form the current graph projection. Decision history is
  append-only; retraction removes an edge only after an audited retraction.
- Source event rows, immutable source revisions and canonical memberships are
  not rewritten. Edges whose source revisions have advanced are reported stale
  until explicitly re-reviewed.
- This gate creates no edges automatically and makes no network requests.

Inspect graph state (indented command examples):

    uv run crypto-harvester relationship --db data/harvest.sqlite --action status
    uv run crypto-harvester relationship --db data/harvest.sqlite --action list
    uv run crypto-harvester relationship --db data/harvest.sqlite --action history

Accept one manually verified directed relationship (replace IDs and revision
numbers with the exact source records you reviewed):

    uv run crypto-harvester relationship --db data/harvest.sqlite --action accept \
      --subject-event-id coindar:155698 \
      --object-event-id cryptoevents:india-blockchain-week-2026-conference \
      --relation-type participates_in \
      --subject-revision 1 --object-revision 1 \
      --evidence-json '{"basis":"Coindar source URL and description identify project participation"}' \
      --note "Manually verified participation mention"

Use \`--action reject\` with the same pair/predicate to record a negative
relationship label. Use \`--action retract --relationship-id relationship:...\`
to remove an accepted edge while preserving its audit trail. A rejected
decision does not create a graph edge. Repeated identical decisions are
idempotent; contradictory decisions for the same exact source revisions fail
closed. After a source revision changes, the old edge is marked stale and a
new review must explicitly target the new revisions.

Schema version 3 migrates additively to version 4. Existing source events,
revision history, canonical identities and reconciliation decisions are
preserved. The graph is a manually curated knowledge layer, not an inferred
ground-truth dataset or an automatic merge mechanism.
