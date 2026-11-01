# Gate 8 — Coindar public HTML provider

## Scope

This provider parses the public Coindar calendar listing at
https://www.coindar.org/en. It does not use Coindar API 2.0, which requires
an access token, and does not fetch individual event pages.

The public listing exposes event links, coin names/tickers, category labels,
captions, short descriptions and calendar-day group headings. The parser uses
same-host URL path patterns and nearest card-like ancestors rather than relying
on generated CSS class names.

## Commands

Capture a single local snapshot:

    uv run crypto-harvester probe --url https://www.coindar.org/en \
      --engine http --save-html .debug/coindar.html

Parse the saved HTML without network access:

    uv run crypto-harvester scrape --provider coindar --input-html .debug/coindar.html \
      --schema unified --min-events 10 --db data/harvest.sqlite \
      --output artifacts/coindar.json

    uv run crypto-harvester audit --source coindar --input-html .debug/coindar.html \
      --strict --min-records 10 --output artifacts/coindar-audit.json

A live workstation fetch is also supported:

    uv run crypto-harvester scrape --provider coindar --engine http \
      --schema unified --output artifacts/coindar.json

The input-html mode is recommended for repeatable tests and environments
where the source is reachable only from a workstation. Raw snapshots remain
local and should not be committed.

## Native event fields

| Field | Meaning |
| --- | --- |
| event_id | coindar:<numeric-event-id>, taken from the public event URL |
| source_url | Canonical HTTPS event link on www.coindar.org |
| title | Event caption |
| coin, ticker | Coin name and visible ticker, when present |
| category | Visible Coindar category tag |
| description | Listing summary, if present |
| date_text, event_date | Visible day-group label and ISO calendar date |
| date_precision, date_role | Exact day or unknown; listed for an explicit day-group date |
| added_text | Relative publication label, retained only in native output |

Unknown month/quarter labels are not converted to an arbitrary first day.
The listing date is a calendar day, not a timestamp; timezone,
start_at_utc and end_at_utc remain null in Unified Schema v1.
Location, source reliability and importance are not guessed.

## Request budget and access behavior

- One listing fetch per live run.
- Zero event-detail fetches.
- Zero paid API calls and no API token.
- No pagination or past-event traversal in this gate.
- HTTP 403/429 and challenge pages are explicit failures; no bypass,
  credential reuse, retries or proxy rotation is implemented.

GitHub-hosted runners previously received HTTP 403 from Coindar while a
normal workstation could access the page. CI therefore tests the parser
offline and does not require a live Coindar request.

## Fixture status and acceptance caveat

tests/fixtures/coindar_listing_sample.html is a representative synthetic
fixture, not a raw captured Coindar page. It exercises the public URL
conventions and semantic card structure seen in the rendered listing.
Offline tests verify parsing, canonical numeric IDs, deduplication,
same-host URL validation, date grouping, unknown dates, normalized output
and SQLite storage.

Before merging for production use, capture a fresh local HTML page and run
the commands above. If its DOM differs, retain that snapshot privately and
adjust the parser selectors/ancestor logic from the observed markup. Do not
commit session data, cookies or challenge tokens.
