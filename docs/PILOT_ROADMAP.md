# Scraping Feasibility Pilot — implementation gates

This repository is a **zero-paid-API technical pilot**, not a trading signal system.
The first release targets crypto *events* (not price candles). Browser automation is an
optional fetch strategy; each site's provider stays independent.

## Delivery rule

Every stage is proposed in an isolated branch and pull request. Do **not** merge until
reviewed by the repository owner. Advance to the next stage only after approval/merge.

| Gate | Scope | Acceptance evidence |
| --- | --- | --- |
| 0 — Foundation | Python package, optional browser engines, reproducible offline CI, one-page probe | Tests and lint pass, CLI probes an accessible page |
| 1 — Source feasibility | Probe Coindar, ICO Drops, CryptoEvents.global; inspect HTML, DOM, pagination, history | `SOURCE_FEASIBILITY.md` with reproducible URLs, methods, observed limitations, request counts |
| 2 — Provider pilots | Independent parsers for ICO Drops, CryptoEvents.global and local Coindar HTML | Offline fixtures, normalized event records, source URLs; live access reported per environment |
| 3 — Normalization | Common schema, UTC/tz/unknown date precision, source IDs | Validation tests, no fabricated timestamps |
| 4 — Incremental storage | Dedup, revisions, resume, SQLite + CSV/JSON exports | Idempotence tests and before/after snapshots |
| 5 — Evaluation | Repeated trials, parser regression fixtures, quality checks | `PILOT_RESULTS.md` and source comparison |

## Pilot source shortlist

- https://www.coindar.org/en — broad crypto event calendar
- https://icodrops.com/ — token sale/launch events
- https://cryptoevents.global/ — blockchain conferences/events

These are **candidates**, not yet confirmed working or permitted for collection.
Gate 1 will record whether each can be accessed via direct HTTP and whether JS rendering
is required. Never claim an event was successfully scraped until verified from live content.

## Engineering boundaries

- No paid APIs or mandatory API keys.
- Prefer one page for multiple events over per-event navigation.
- Use HTTP first, Playwright when DOM rendering requires JS, Selenium if there is a concrete need.
- Keep debug snapshots local; do not commit fetched content or session credentials.
- Record 403/429/challenge pages as failures; do not add bypass or evasion machinery.
- Gate 0 intentionally does **not** implement any site-specific scraper or persistent database.


## Gate 8 status — Coindar HTML parser

The first Coindar provider implementation is available in
docs/COINDAR_PILOT.md. It parses one public listing page from saved HTML or
a normal HTTP/browser fetch, with no API token and no event-detail requests.
A representative offline fixture and parser/normalization/SQLite tests are
included. A fresh local snapshot from a workstation where Coindar is accessible
is still required to confirm the parser against the current raw DOM. Pagination
and historical backfill are explicitly out of scope for this gate.


## Gate 12 status — checkpointed CryptoEvents pagination

The one-shot Gate 7 paginator remains capped at five pages per invocation
and is still useful for a single quick export. Gate 12 adds a separate
cross-run cursor for repeatable collection: a frozen UTC-day filter,
per-page SQLite synchronization, resume from the next AJAX offset, a
five-request per-run cap and a 100-page total crawl cap. Checkpoints can be
inspected or reset without deleting event rows or revisions.

Acceptance evidence: offline interruption/replay tests, cursor-resume tests,
idempotent SQLite synchronization, request-budget validation, and a live
workstation run of the homepage plus at least one AJAX page. See
[Gate 12 implementation](CRYPTOEVENTS_BACKFILL.md).


## Gate 13 status — cross-provider candidate identity review

A read-only offline `candidates` command scans the latest normalized
SQLite event snapshots to surface **possible** cross-provider duplicates
with strict title/date equality and no explicit project/ticker conflict.
No automatic source identity merge, revision rewriting or added collection
requests. See [review-only candidate matching](CROSS_PROVIDER_CANDIDATES.md).


## Gate 14 status — scored cross-provider candidate matching

The exact matcher remains the default. An opt-in lexical similarity strategy
uses date-overlap blocking, token and character similarity, entity-conflict
vetoes, stricter thresholds when project/ticker corroboration is absent, and
score-ranked review candidates. Scores are not probabilities and never
trigger automatic merges. It reads the current SQLite snapshots only; no
network requests or paid services are involved. See
[CROSS_PROVIDER_CANDIDATES.md](CROSS_PROVIDER_CANDIDATES.md).


## Gate 15 status — manual cross-provider reconciliation

SQLite schema version 2 adds canonical identity memberships and an
append-only human decision history. The `reconcile` command offers status,
pending, list, history, accept and reject actions. Decisions are revalidated
against current database snapshots and revisions inside a transaction.
Conflicting canonical identities are never merged, and provider-specific
events, IDs and revision histories remain unchanged. See
[manual reconciliation](CROSS_PROVIDER_CANDIDATES.md).


Gate 15.1 status — reconciliation integrity

The integrity follow-up blocks opposite decisions for the same source revision
pair, refreshes canonical membership revision markers after re-acceptance,
filters already-linked current pairs from pending review, and surfaces stale
memberships in status/list output. SQLite status reports the actual
`user_version`. Offline regression tests cover these cases.

## Gate 16 status — canonical identity lifecycle

Adds transactional `unlink` and `split` operations for correcting manually
accepted canonical identities. Every membership removal or move requires an
append-only audit row, enforced by SQLite triggers. Unlink retains the
canonical identity even when it becomes empty; split moves a proper subset
to a new identity and leaves at least one source member. The
`membership-history` action exposes the lifecycle audit trail. Source event
snapshots and immutable revisions remain untouched. SQLite schema version 2
migrates additively to version 3.

## Gate 17 status — retrospective matching-quality evaluation

Adds a read-only `reconcile --action evaluate` report over human-reviewed
source-event revision pairs. It evaluates exact matching and sweeps
similarity thresholds using the immutable event revisions referenced by
the append-only decision log. Metrics are explicitly limited to reviewed
pairs; unreviewed pairs have no labels, so population recall is not claimed.
A configurable minimum sample size controls the report status, and an empty
decision history returns `no_reviewed_pairs` rather than fabricated metrics.
No network requests, database writes, schema migration or automatic merge
are introduced.

## Gate 18 status — human review dataset workflow

Adds a bounded, read-only CSV export of pending cross-provider candidates and
a dry-run-first import path for human accept/reject/unsure labels. The batch
captures deterministic candidate IDs and exact source revision numbers.
Import revalidates the strategy and evidence and checks expected revisions
inside the write transaction, so stale exports cannot be applied to changed
source snapshots. Duplicate candidate IDs and contradictory labels fail
before writes; per-row conflicts and stale rows are reported explicitly.
No new schema, source requests, paid APIs or automatic merges are introduced.
Imported labels feed the Gate 17 retrospective quality evaluator. See
[CROSS_PROVIDER_CANDIDATES.md](CROSS_PROVIDER_CANDIDATES.md).

## Gate 19 status — coverage diagnosis and relation hints

Introduce read-only matching diagnostics before modifying matching thresholds.
Compare metadata availability by provider and classify the first strict-matching
blocker for each overlapping cross-provider date pair. Report potentially
related records separately from canonical identity candidates, highlighting
conference participation/mention signals and city corroboration. Preserve
existing strict identity decisions, Gate 18 import eligibility, append-only
history and source tables. No new schema, network access or auto-merging.

## Gate 20 status — human-reviewed semantic relationship graph

Add a separate directed graph for semantic relationships between source event
records. The graph supports participation, sponsorship, speaking, organizing,
attendance, exhibition and generic related-to predicates. Canonical
same-event identity remains exclusively in the existing reconciliation
subsystem to avoid duplicate sources of truth. Every relationship decision
captures immutable source revision references, metadata evidence and an
append-only decision history. Accepted edges are a current projection;
retractions are audited and stale source snapshots require re-review. No
automatic edge creation, source requests or source-row mutation. SQLite
schema version 3 migrates additively to version 4.

## Gate 22 status — deterministic semantic relationship candidate discovery

Adds a read-only relationship candidate report over current, dated
cross-provider event snapshots. A candidate requires an explicit relationship
cue in the subject record, a conference target, overlapping calendar dates,
and distinctive target-title anchors found in the same relation-cue snippet.
Generic event terms, years, country/geographic terms and the target location
are excluded from anchors.
When the target location is known, the complete location must also occur in
that snippet; without a known location, at least two distinctive anchors are
required. Description, title and URL cues are evaluated independently;
among candidates passing the target gate for the same event pair, description
has priority over title, which has priority over URL. Source-field priority is
selected before accepted/rejected review filtering, so a reviewed stronger
cue cannot fall back to a weaker URL predicate. Repeated same-predicate cues
are deduplicated. Project-name proximity is used to
avoid attributing a role mentioned for a different organization to the subject
project.

The report ranks candidates with an explainable deterministic evidence score;
the score is not a probability. It includes exact source revisions, cue
location and snippet, matched distinctive target-title anchors, target-match
basis, date relation and location corroboration. Current accepted/rejected decisions for the same relationship
and exact revisions are not suggested again. Retracted decisions remain
visible for a fresh human review.

No relationship is created automatically. No canonical identity, source
event, revision history or decision history is modified. The command makes no
network requests and does not migrate older databases. See the Gate 22 section
in CROSS_PROVIDER_CANDIDATES.md.


## Gate 23 status — read-only relationship graph analytics

Adds query-only graph analytics over human-accepted semantic relationships.
Only edges whose exact subject and object revisions match current source
snapshots enter the active graph; stale edges are counted and excluded.
Reports directed degree rankings, predicate/provider-pair counts and weak
connectivity components. Bounded neighborhood traversal exposes simple paths
of up to three edges while preserving each edge's direction and predicate;
paths are connectivity evidence only and do not imply transitive semantics.
JSON and directed GraphML exports support downstream analysis and
visualization. Reviewer notes/evidence are excluded from exports. No schema
migration, source request, event/revision mutation, canonical identity change,
relationship creation or review-history mutation is performed.
