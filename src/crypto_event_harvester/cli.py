"""Developer-facing commands for the pilot."""

from __future__ import annotations

import argparse
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
    sources.add_argument("--strict", action="store_true", help="Return nonzero if source listing evidence is absent")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        return 1 if args.strict and any(item["status"] != "listing_evidence" for item in report["sources"]) else 0
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
