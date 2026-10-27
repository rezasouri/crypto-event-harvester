"""Developer-facing commands for the pilot."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import httpx

from .probe import fetch_page, summarize_page


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crypto-harvester", description="Crypto event scraping pilot")
    subcommands = parser.add_subparsers(dest="command", required=True)
    probe = subcommands.add_parser("probe", help="Inspect a single public page without scraping events")
    probe.add_argument("--url", required=True, help="Absolute HTTP(S) page URL")
    probe.add_argument("--engine", choices=("http", "playwright", "selenium"), default="http")
    probe.add_argument("--selector", help="CSS selector whose matches will be counted")
    probe.add_argument("--wait-for", help="CSS selector to wait for when using a browser")
    probe.add_argument("--timeout", type=float, default=30, help="Request/browser timeout in seconds")
    probe.add_argument("--save-html", type=Path, help="Optional file path to save the inspected HTML")

    sources = subcommands.add_parser("sources", help="Probe three known crypto event listing sources")
    sources.add_argument("--source", choices=("all", "coindar", "icodrops", "cryptoevents"), default="all")
    sources.add_argument("--engine", choices=("http", "playwright", "selenium", "auto"), default="http")
    sources.add_argument("--timeout", type=float, default=20)
    sources.add_argument("--json-out", type=Path, help="Save machine-readable source diagnostics")
    sources.add_argument("--save-html-dir", type=Path, help="Save raw HTML locally, never to Git")
    sources.add_argument(
        "--strict", action="store_true", help="Return nonzero if listing evidence is absent"
    )
    scrape = subcommands.add_parser("scrape", help="Extract real crypto event listing rows")
    scrape.add_argument("--provider", choices=("icodrops", "cryptoevents"), default="icodrops")
    scrape.add_argument("--section", choices=("upcoming", "active", "ended", "all"), default="upcoming")
    scrape.add_argument("--engine", choices=("http", "playwright", "selenium"), default="http")
    scrape.add_argument("--timeout", type=float, default=20)
    scrape.add_argument("--format", choices=("json", "csv"), default="json")
    scrape.add_argument("--output", type=Path, help="Save output; otherwise print to stdout")
    scrape.add_argument("--input-html", type=Path, help="Parse saved HTML offline (one section only)")
    scrape.add_argument("--limit", type=int, default=None, help="Limit exported rows; not fetch count")
    scrape.add_argument("--min-events", type=int, default=0, help="Fail if fewer events were extracted")
    return parser


def _scrape(args) -> int:
    from .providers.cryptoevents import (
        CryptoEventsEvent,
        fetch_listing as fetch_cryptoevents,
        parse_listing as parse_cryptoevents,
    )
    from .providers.icodrops import (
        ICODropsEvent,
        fetch_listing as fetch_icodrops,
        parse_listing as parse_icodrops,
    )

    if args.input_html and args.section == "all":
        print("--input-html requires a single section", file=sys.stderr)
        return 2
    if args.provider == "cryptoevents" and args.section != "upcoming":
        print("cryptoevents currently supports only --section upcoming", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 2
    if args.min_events < 0:
        print("--min-events cannot be negative", file=sys.stderr)
        return 2

    sections = ("upcoming", "active", "ended") if args.section == "all" else (args.section,)
    event_type = CryptoEventsEvent if args.provider == "cryptoevents" else ICODropsEvent
    try:
        unique = {}
        for section in sections:
            if args.provider == "cryptoevents":
                events = (
                    parse_cryptoevents(args.input_html.read_text(encoding="utf-8"))
                    if args.input_html
                    else fetch_cryptoevents(engine=args.engine, timeout_seconds=args.timeout)
                )
            else:
                events = (
                    parse_icodrops(args.input_html.read_text(encoding="utf-8"), section=section)
                    if args.input_html
                    else fetch_icodrops(section, engine=args.engine, timeout_seconds=args.timeout)
                )
            for event in events:
                unique[event.event_id] = event
        all_events = list(unique.values())
        if len(all_events) < args.min_events:
            raise RuntimeError(f"Expected at least {args.min_events} events, got {len(all_events)}")
        selected = all_events[:args.limit] if args.limit is not None else all_events
        if args.format == "json":
            payload = {
                "provider": args.provider,
                "sections": list(sections),
                "extracted_count": len(all_events),
                "exported_count": len(selected),
                "events": [event.to_dict() for event in selected],
            }
            output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(event_type.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(event.to_dict() for event in selected)
            output = buffer.getvalue()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
            print(json.dumps({"output": str(args.output), "events": len(selected)}, ensure_ascii=False))
        else:
            print(output, end="")
        return 0
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"scrape failed: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scrape":
        return _scrape(args)
    if args.command == "sources":
        from .source_feasibility import SOURCES, probe_sources

        names = list(SOURCES) if args.source == "all" else [args.source]
        report = probe_sources(
            names, engine=args.engine, timeout_seconds=args.timeout, save_html_dir=args.save_html_dir
        )
        output = json.dumps(report, indent=2, ensure_ascii=False)
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(output + "\n", encoding="utf-8")
        print(output)
        has_missing = any(item["status"] != "listing_evidence" for item in report["sources"])
        return 1 if args.strict and has_missing else 0
    try:
        snapshot = fetch_page(
            args.url, engine=args.engine, timeout_seconds=args.timeout, wait_for=args.wait_for
        )
        if args.save_html:
            args.save_html.parent.mkdir(parents=True, exist_ok=True)
            args.save_html.write_text(snapshot.html, encoding="utf-8")
        summary = summarize_page(snapshot, selector=args.selector)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
