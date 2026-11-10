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

## Gate 22 — deterministic semantic relationship candidate discovery

Gate 22 adds a read-only discovery operation to the separate semantic
relationship graph. It does not change canonical event identity matching.

Run the report against the shared local database:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action candidates
    uv run crypto-harvester relationship --db data/harvest.sqlite --action candidates --min-score 0.70 --limit 50

### Candidate rules and evidence

- Only current source snapshots with explicit day or range dates are eligible.
- Candidate pairs must come from different providers and have overlapping
  calendar dates.
- The object must be a conference record; the subject is the other source
  record carrying the relationship assertion.
- Relationship cues are extracted from the subject description, title and
  source URL. Supported cues map to the existing predicates:
  participates_in, attends, speaks_at, sponsors, organizes and exhibits_at.
- A represented-by phrase is retained as cue mode evidence under
  participates_in; it is not silently upgraded into a speaking claim.
- When project metadata exists, the project name must occur near the cue.
  This helps avoid assigning a role mentioned for another organization to
  the subject project.
- Target-title matching is evaluated against each relation-cue snippet
  separately; generic event words, years, country/geographic terms and the
  target's own location are excluded from distinctive title anchors.
- When the target has a known location, at least one distinctive title anchor
  and the complete normalized target location must occur in the same cue
  snippet. When no target location is known, at least two distinctive title
  anchors must occur in that snippet.
- Description, title and source-URL cues are evaluated independently.
  Among candidates that pass the target-match gate for the same event pair,
  source-field priority is description, then title, then source URL. The
  strongest eligible field is selected before accepted/rejected review history
  is applied. Therefore, a reviewed description cue cannot disappear and
  expose a weaker URL predicate as a fallback. A URL can still supply target
  evidence when a generic description cue fails the target-match gate.
  Distinct predicates supported by the selected field are retained; repeated
  same-predicate cues are deduplicated.
- The deterministic score combines cue strength (40%), distinctive
  target-title coverage (35%), date overlap (15%) and location corroboration
  (10%). It is a ranking aid, not a probability or calibrated confidence
  estimate. The target-match gate runs before scoring.
- Candidates include a stable candidate ID, the relationship ID, exact source
  revisions, source URLs, cue snippets, matched distinctive title anchors,
  match basis and score components.
- Current accepted/rejected decisions for the same predicate, event pair and
  exact revisions are excluded after source-field priority is selected. A
  reviewed stronger-field cue suppresses weaker-field fallbacks for that pair.
  A retracted relationship may be surfaced for a fresh review.

### Safety and interpretation

The command opens SQLite in query-only mode. It does not create or accept
relationships, alter the graph, mutate source events or revisions, change
canonical memberships, or append review decisions. It makes no HTTP requests
and does not run schema migrations. All returned rows have
status=review_required and require a human decision through the existing
relationship accept/reject commands.

A candidate is not an identity match. In particular, a project participation
record and the conference record remain distinct source events connected only
by a directed semantic relationship after human approval. Scores and lexical
overlap cannot establish that a claim is true; reviewers should inspect the
source URL and cue snippet before accepting.


## Gate 23 — read-only relationship graph analytics and export

Gate 23 adds graph inspection on top of the accepted semantic relationships
from Gate 20. It does not infer new edges or alter the canonical identity
subsystem. All analytics and exports use only accepted relationships whose
subject and object revisions still match the current source snapshots. Stale
edges are counted and excluded from the active graph.

Summarize the accepted graph:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action analytics
    uv run crypto-harvester relationship --db data/harvest.sqlite --action analytics --limit 50

The report includes active node/edge counts, stale edges excluded, weakly
connected component sizes, directed in/out-degree rankings, predicate counts
and provider-pair counts. Weak components describe connectivity only; they do
not imply that a semantic predicate is transitive.

Inspect bounded paths around one source event:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action neighborhood \
      --event-id coindar:155698 --direction both --depth 2 --limit 100

Direction may be outgoing, incoming or both. Traversal depth is bounded to
1–3 and returned simple paths are capped by --limit. Each step preserves the
original edge direction and predicate. A path is a chain of accepted edges,
not a newly inferred relationship between its endpoints.

Export the active directed graph for external analysis or visualization:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action export \
      --format json --output artifacts/relationship-graph.json
    uv run crypto-harvester relationship --db data/harvest.sqlite --action export \
      --format graphml --output artifacts/relationship-graph.graphml

GraphML preserves source event IDs, provider, title, project/ticker metadata,
event dates, location, source URL, revisions and directed relationship
predicates. Reviewer evidence and free-form notes are intentionally omitted
from exports. GraphML node IDs are stable within one export; the original
provider-prefixed event ID is retained as node data.

All three operations open SQLite in query-only mode and require schema version
4 or newer without running migrations. They make no source requests and do not
modify source rows, revisions, canonical identities, graph edges or review
history. Stale relationships remain available through the existing status and
list commands for explicit human re-review.


## Gate 24 — relationship quality and retrospective evidence evaluation

Gate 24 evaluates the human-reviewed semantic relationship history without
changing it. It is a diagnostic report, not an automated acceptance or
rejection mechanism.

Run the report against an existing schema-v4 database:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action evaluate

Customize the deterministic evidence-score sweep and sample-size requirement:

    uv run crypto-harvester relationship --db data/harvest.sqlite --action evaluate \
      --thresholds 0.60 0.70 0.80 0.90 --min-reviewed-pairs 30 --limit 100

The report groups decisions by the exact directed relationship and source
revision pair, then uses the latest accepted/rejected human label for that
revision tuple. A retraction is a lifecycle action and is not reinterpreted as
a negative label. Historical event-revision payloads are used so later source
updates cannot silently rewrite the evidence that was reviewed.

For scoreable records, the current deterministic relationship-candidate rules
are replayed against the historical subject and object revisions. The report
includes score coverage, score distributions by human label, and threshold
metrics (precision, recall, F1, specificity and accuracy). Threshold metrics
use only reviewed relationships for which the current rules produce a score.
They do not estimate recall over undiscovered relationships.

The evidence audit reports snapshot consistency, source URL presence, reviewer
basis, review marker and reviewer note coverage. Evidence grades (complete,
partial, insufficient) describe recorded audit-field completeness only;
they do not indicate truth or probability. Findings include stale revisions,
missing evidence fields, absent candidate signals and inconsistencies between
review history and the current accepted-edge projection.

An evaluated status requires at least the configured number of reviewed
relationship-revision pairs, both accepted and rejected scoreable labels, and
at least 50% candidate-score coverage. Smaller or single-class datasets are explicitly marked
insufficient rather than presenting misleading performance claims.

The command opens SQLite in query-only mode, requires schema version 4 or
newer, and never migrates the database. It makes no source requests and does
not alter events, revisions, canonical identities, relationship edges or
decision history. Scores are deterministic ranking evidence, not calibrated
confidence or probabilities. Human decisions are not independent ground
truth, and the reviewed sample is selection-biased.
