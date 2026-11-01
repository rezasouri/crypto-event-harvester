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
