# Gate 4 — Unified Event Schema v1 (Normalization)

**Purpose:** make two independently scraped sources usable by the same consumer,
without changing provider-specific parsing or creating dates, time zones, or prices
that were not present on the source page.

## API and CLI

```bash
python -m pip install -e '.[dev]'

# Existing native output remains the default:
crypto-harvester scrape --provider icodrops --section ended --output ended-native.json

# Provider-independent normalized JSON/CSV:
crypto-harvester scrape --provider icodrops --section ended \
  --schema unified --output ended-unified.json
crypto-harvester scrape --provider cryptoevents --schema unified \
  --output conferences-unified.json
crypto-harvester scrape --provider cryptoevents --schema unified --format csv \
  --output conferences-unified.csv
```

Python code can call `normalize_event(event)` or `normalize_events(events)` from
`crypto_event_harvester.normalization` with parsed native provider dataclasses.
Unsupported provider types and duplicated normalized IDs fail explicitly.

## Common fields (same names, order and types from both providers)

| Field | Meaning |
| --- | --- |
| `schema_version` | `1.0`, version of the output contract |
| `event_id` | Stable within the provider's current native ID scheme, prefixed by provider |
| `provider`, `source_event_id` | Provenance and provider-local identity |
| `source_url`, `listing_url` | Original event page and listing page URLs |
| `event_type` | `token_event` or `conference`; `token_event` includes TGE/distributions, not just ICO sales |
| `title`, `project`, `ticker` | Name and optional project/token metadata |
| `category`, `subtype`, `status` | Source category, round label and source listing status |
| `location`, `description` | Conference fields when present |
| `date_text` | Verbatim human-readable date/status shown by source |
| `start_date`, `end_date` | Explicit ISO `YYYY-MM-DD` calendar dates or null |
| `date_precision` | `day`, `range`, `quarter` or `unknown` |
| `date_role` | `listed`, `start` or `unknown` |
| `timezone`, `start_at_utc`, `end_at_utc` | **Null**, because neither source supplies verified instants and offsets |
| `total_raised`, `pre_valuation` | Original displayed values, left as text; no currency assumptions |
| `featured` | Whether conference was a featured card; null for ICO Drops |

### Source mapping

ICO Drops maps its `event_date` to both start and end on day-precision events;
`Upcoming` stays null, and a quarter indication is retained as raw text and
`date_precision=quarter`, *not* a fabricated first-of-quarter date.
The `subtype` retains the token offering/round name, and `status` retains the
listing section (`upcoming`, `active`, `ended`).

CryptoEvents.global maps actual start/end date ranges, `location`, and
`description`. Its status `listed` means only that it is present on the
source calendar; it does not claim that the event has started or ended.

**Date-only is not a timestamp.** Neither `UTC midnight` nor the user's local
timezone is a sound inference from calendar dates. UTC instant and timezone fields
are intentionally null; later sources with explicit clock times can extend the
schema in a separate versioned review.

## Guarantees and limitations

- Source-specific `--schema native` JSON/CSV keeps its existing exact structure.
- `--schema unified` adds the versioned normalized contract with an identical
  flat CSV header for both providers.
- Validation checks identity, date precision, ISO dates, ordered date ranges, and
  absence of invented UTC timestamps; normalization never fetches extra pages.
- Existing native event IDs remain provider-derived; **cross-provider entity
  resolution** and changed-source-ID reconciliation are *not* part of this gate.
- Price/valuation strings are not converted to numeric currency values.
- No persistence or change history yet (next roadmap gate).
- Coindar remains on the shortlist for testing on a workstation/browser; CI
  network restrictions on that source are not grounds to discard it.

## Verification

Run `pytest -q` and `ruff check .`. The live unified export workflow
`Unified provider export (nonblocking)` fetches one current listing page per
provider, checks shared field layouts and calendar date semantics, then publishes
the resulting two JSON files as a workflow artifact. All live counts are
observations, not historical-completeness claims.
