# Gate 2: ICO Drops actual listing scraper

## Source and verified DOM structure

Public sections: https://icodrops.com/category/upcoming-ico/,
https://icodrops.com/category/active-ico/,
https://icodrops.com/category/ended-ico/.

Live GitHub-hosted HTML inspection on 2026-10-27 confirmed a table of
`ul#table-list > li.Tbl-Row` elements, with independent cells:
`.Tbl-Row__item--project`, `--round`, `--raised`, `--pre-valuation`,
`--categories`, `--date`. The project detail URL is in
`.Tbl-Row__item--avt a[href]`. This does not depend on JavaScript rendering.

## Usage

```bash
python -m pip install -e '.[dev]'
crypto-harvester scrape --provider icodrops --section upcoming --output upcoming.json
crypto-harvester scrape --provider icodrops --section ended --format csv --output ended.csv
crypto-harvester scrape --provider icodrops --section all --output all.json
crypto-harvester scrape --section ended --input-html tests/fixtures/icodrops_ended.html
pytest -q
```

One GET per requested listing section, without visiting detail pages.
`--section all` requests three pages and deduplicates by the project+round
identifier. The program fails if selectors break or no valid records remain.
`--min-events 20` is available as an explicit live feasibility gate.

## Data semantics and current limitations

- Records represent **ICO Drops listed rounds**, not every activity on the project page.
- `event_id` is derived from project URL slug plus the round label; if that label
  changes, the ID can change. A future revision store will improve matching.
- `date_text` preserves exactly what the listing displays.
- `event_date` contains YYYY-MM-DD only if an explicit calendar day is supplied.
- `Upcoming`, `3d left` and similar relative dates remain null; quarter dates
  are recorded as precision `quarter` without inventing a day.
- `from Jul 24, 2026` gets `date_role=start` to differentiate from plain dates.
- `status` is the requested listing section. This is **not** an authoritative
  claim about the current lifecycle state of a specific individual round.
- Ticker extraction from the project cell is heuristic; verify sample cases before
  using for financial decision-making.
- Listing pages may be limited/paginated. Historical completeness and detail
  fields are **not yet guaranteed**.
- Raw source snapshots are not included in Git; small structural HTML fixtures
  back the offline tests.

## Scope

Coindar remains on the shortlist: HTTP 403 on a GitHub runner is not proof the
site cannot be scraped from a regular workstation. Its HTTP/browser viability
must be tested in that environment in a subsequent PR.
