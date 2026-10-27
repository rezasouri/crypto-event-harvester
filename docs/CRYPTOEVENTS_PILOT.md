# Gate 3 — CryptoEvents.global real event listing pilot

## Proven HTML shape

A public GitHub Actions runner fetched https://cryptoevents.global/ with HTTP 200
on October 27, 2026. Event summaries are in \`div.event_blk\`; children include
\`div.event_date\`, \`p.event_name\`, \`p.event_location\`,
\`div.event_short_description\` and a \`Show more\` link to the detail page.
No browser execution is needed for the currently visible first page.

Source HTML can contain duplicate cards inside the \`featured_event\` region.
The parser deduplicates by normalized same-host detail URL and prefers a regular
listing card when a featured duplicate exists.

## Run it

\`\`\`bash
pip install -e '.[dev]'
crypto-harvester scrape --provider cryptoevents --output artifacts/conferences.json
crypto-harvester scrape --provider cryptoevents --format csv \
  --output artifacts/conferences.csv
crypto-harvester scrape --provider cryptoevents --input-html \
  tests/fixtures/cryptoevents_home.html
\`\`\`

The provider makes one HTTP GET to the visible calendar listing. HTTP is the
default, but the shared \`--engine playwright\` and \`--engine selenium\` options
remain available. It does not open each event detail page.

## Date semantics

- Explicit single day: \`Oct 27 2026\` -> start_date/end_date \`2026-10-27\`.
- Explicit same-year range: \`Oct 27-29 2026\` -> 2026-10-27 to 2026-10-29.
- Ambiguous, invalid, or unknown dates: \`start_date=null\`, \`end_date=null\`,
  and the original \`date_text\` preserved.
- **No assumed time-of-day or time zone**: this page lists dates, not reliable UTC timestamps.

## Boundaries

This stage only covers the **first visible listing page**, before dynamic
\`Load more\`. Pagination, full historical coverage, and detailed sessions
require separate verification. The event schema is provider-specific until
the planned normalization gate. A local Coindar/browser evaluation is still
planned: a 403 response from GitHub-hosted IPs is not grounds to discard it.

This pilot treats site unavailability, 403/429, or zero parseable cards as
explicit failures rather than successes. No paid API or mandatory key.
