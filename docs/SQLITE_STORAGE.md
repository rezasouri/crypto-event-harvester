# Gate 5 — Incremental SQLite event storage

Two existing providers can now store **normalized event snapshots** in one local
SQLite database, while retaining full source-specific `--schema native` exports.

## Get started

```bash
python -m pip install -e '.[dev]'

# One listing fetch per command, same persistent database.
crypto-harvester scrape --provider icodrops --section upcoming \
  --db data/harvest.sqlite --schema unified --output data/upcoming.json
crypto-harvester scrape --provider cryptoevents \
  --db data/harvest.sqlite --schema unified --output data/conferences.json

# Repeat either command: unchanged data does not create new revisions.
crypto-harvester store --db data/harvest.sqlite --action status
crypto-harvester store --db data/harvest.sqlite --action list --provider icodrops --limit 20
crypto-harvester store --db data/harvest.sqlite --action history \
  --event-id icodrops:clix:tge-and-distribution
```

The `--db` option is opt-in. If omitted, normal scraping behaves exactly
as before. The `--limit` argument only limits the **output**, not records
persisted to the database; all successfully parsed listing records are synced.

A native JSON or CSV export combined with `--db` still stores the **unified**
schema internally. `store` always returns JSON, including current records,
observation metadata, and historic versions. The database uses only the
Python standard library `sqlite3`; no Redis, Docker, paid API, or cloud server.

## Tables

`events`: one latest normalized JSON document per provider-prefixed
`event_id` with SHA256, latest revision number, `first_seen_at_utc`,
`last_seen_at_utc`, and `updated_at_utc`.

`event_revisions`: immutable full JSON snapshots and content hashes,
keyed by `(event_id, revision)`. Foreign keys and a one-to-many relationship
are enforced. An index on provider supports filtered listings.

SQLite schema compatibility is tracked with `PRAGMA user_version`.
The current schema is version 3: version 1 creates the normalized event
store, version 2 adds canonical reconciliation, and version 3 adds the
append-only membership lifecycle audit log and integrity triggers. Existing
version 1 and 2 databases are upgraded additively; source event rows and
immutable revisions are preserved. Unsupported future versions fail closed.
Writes use WAL mode, a busy timeout, and atomic `BEGIN IMMEDIATE`
transactions.

## Reconciliation rules

| Incoming observation | Effect |
| --- | --- |
| Never seen before | Insert current event + revision 1 |
| Identical content, newer clock | Advance `last_seen_at_utc` only |
| Content changed, newer clock | Replace current event + append immutable revision N+1 |
| Older content/clock (or same clock with conflicting content) | Ignore; never roll back newer state |
| Event absent from partial/failing source page | **Do not delete** it |
| Duplicate identity in one batch | Reject batch before writing |

The content hash covers only normalized source fields. Observation timestamps
are stored **separately** so a repeat fetch doesn't falsely count as a change.
Observation timestamps are recorded as UTC instants; this does **not** imply
any event has a known event timezone or a midnight event timestamp.

All events within a provider batch are committed together. If any database
write fails, previous inserts/updates in that batch are rolled back.

## Explicit limitations

- Cross-provider semantic deduplication is **not** yet implemented. These are
  different record types (token events vs conferences) and an inferred title
  match would risk merging unrelated events.
- Identity currently follows provider event ID. If a source changes a slug or
  offering round label, this can result in a new ID; identity reconciliation
  is future work.
- This phase does not perform deletions, TTL pruning, backfilling, automatic
  scheduling, extraction retries, or persistent HTTP response caching.
- Separate `scrape` invocations are separate observations. If one provider
  succeeds and the next fails, the first provider's committed records remain.
- File-system export and database synchronization aren't a distributed
  transaction; a later file-write failure can occur after successful DB commit.
- `store` reads are bounded to at most 1000 latest events per call. Historical
  revisions are available for a requested event ID.
- The provider-dependent listing coverage limits from prior phases remain.
- **Coindar remains on the candidate list**, including workstation/browser
  tests; CI runner 403 errors are not a reason to remove it.

## Acceptance evidence

The offline test suite covers unchanged replay, changed revisions, stale
update rejection, multi-provider persistence, missing events, SQL transaction
rollback, UTC observation normalization, and CLI status/list/history.

A nonblocking live workflow runs two public listing fetches (ICO Drops and
CryptoEvents.global), persists both to the same SQLite file, replays the
stored records **offline** to verify idempotence, and uploads the database
and a machine-readable report. No extra network requests are needed for the
idempotence check.
