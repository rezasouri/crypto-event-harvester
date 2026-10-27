# Crypto Event Harvester

A **zero-paid-API, provider-independent** research pilot for scraping cryptocurrency event calendars.
It aims to make event extraction measurable before committing to a production architecture.
The first stage includes a one-page diagnostic probe; it **does not** claim to scrape crypto events yet.

## Pilot workflow

See [the staged roadmap](docs/PILOT_ROADMAP.md). Each gate ships in a separate pull request and
is merged only after owner approval.

Initial candidates: [Coindar](https://www.coindar.org/en), [ICO Drops](https://icodrops.com/),
[CryptoEvents.global](https://cryptoevents.global/). Their extraction feasibility is not yet verified.

## Setup (Python 3.11+)

```bash
python -m pip install -e '.[dev]'
crypto-harvester probe --url 'https://example.org/'
pytest -q
ruff check .
```

Alternatively, use `uv sync --extra dev` and prefix commands with `uv run`.

### Inspect a page and count candidate DOM nodes

```bash
crypto-harvester probe \
  --url 'https://example.org/' \
  --selector 'article' \
  --save-html '.debug/example.html'
```

By default this makes **one HTTP request**, parses returned HTML and prints JSON diagnostics
(URL, status, title, HTML size, selector matches, likely access issue). It does **not** fetch
individual event pages and does not bypass CAPTCHA or rate limits. Saved HTML is local/debug only.

### JavaScript-rendered pages (optional)

```bash
python -m pip install -e '.[playwright]'
playwright install chromium
crypto-harvester probe --url 'https://example.org/' --engine playwright --wait-for 'article'
```

Selenium is supported as an alternative: install with `pip install -e '.[selenium]'` and have
Chrome/Chromedriver available. Browser probes load extra assets and may cause more network
requests than the HTTP engine. `--wait-for` applies only to browser engines.

## Design principles

- One provider per source; shared normalization and persistence come in later gates.
- No mandatory authentication, subscriptions or paid API calls.
- Keep requests low; record unavailable, rate-limited and security-challenge pages rather than
  mistaking them for valid calendar content.
- No invented times: event date precision and revision history will be explicit in future gates.
- Tests run offline using mocked responses; live source probing is a separate activity.

## Project status

**Gate 0 — Foundation:** developer CLI, fetch strategies and offline tests. Site-specific
providers, normalized event records and storage are planned for subsequent reviewed pull requests.
