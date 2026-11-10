"""Developer-facing commands for the pilot."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
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
    scrape.add_argument("--provider", choices=("icodrops", "cryptoevents", "coindar"), default="icodrops")
    scrape.add_argument("--section", choices=("upcoming", "active", "ended", "all"), default="upcoming")
    scrape.add_argument("--engine", choices=("http", "playwright", "selenium"), default="http")
    scrape.add_argument("--timeout", type=float, default=20)
    scrape.add_argument("--format", choices=("json", "csv"), default="json")
    scrape.add_argument("--schema", choices=("native", "unified"), default="native")
    scrape.add_argument("--output", type=Path, help="Save output; otherwise print to stdout")
    scrape.add_argument("--input-html", type=Path, help="Parse saved HTML offline (one section only)")
    scrape.add_argument("--limit", type=int, default=None, help="Limit exported rows; not fetch count")
    scrape.add_argument("--min-events", type=int, default=0, help="Fail if fewer events were extracted")
    scrape.add_argument("--db", type=Path, help="Incrementally persist normalized events in SQLite")
    scrape.add_argument(
        "--pages", type=int, default=1,
        help="CryptoEvents/Coindar: up to five listing pages (default: one)",
    )
    scrape.add_argument(
        "--start-date", help="Coindar Past Events only: fixed YYYY-MM-DD filter anchor",
    )

    audit = subcommands.add_parser("audit", help="First-page quality and coverage evidence")
    audit.add_argument("--source", choices=("icodrops", "cryptoevents", "coindar"), required=True)
    audit.add_argument("--engine", choices=("http", "playwright", "selenium"), default="http")
    audit.add_argument("--timeout", type=float, default=20)
    audit.add_argument("--input-html", type=Path, help="Use a saved HTML page with no requests")
    audit.add_argument("--save-html", type=Path, help="Save a local snapshot (not committed)")
    audit.add_argument("--output", type=Path, help="Write the full JSON audit report")
    audit.add_argument("--compare-to", type=Path, help="Compare against an earlier JSON report offline")
    audit.add_argument("--min-records", type=int, default=0)
    audit.add_argument("--strict", action="store_true", help="Return nonzero on insufficient evidence")

    backfill = subcommands.add_parser(
        "backfill", help="Checkpointed Coindar Past Events discovery over a date window"
    )
    backfill.add_argument("--db", required=True, type=Path)
    backfill.add_argument("--from-date", required=True, help="Inclusive YYYY-MM-DD")
    backfill.add_argument("--to-date", required=True, help="Inclusive YYYY-MM-DD")
    backfill.add_argument(
        "--action", choices=("run", "status", "reset", "undated", "enrich"), default="run"
    )
    backfill.add_argument(
        "--pages", type=int, default=2, help="Maximum listing pages per invocation (1–5)"
    )
    backfill.add_argument("--timeout", type=float, default=20)
    backfill.add_argument(
        "--limit", type=int, default=100,
        help="Maximum undated records to list (1–1000)",
    )
    backfill.add_argument("--output", type=Path, help="Optional JSON result path")

    cryptoevents_backfill = subcommands.add_parser(
        "cryptoevents-backfill",
        help="Checkpointed, bounded CryptoEvents.global listing pagination",
    )
    cryptoevents_backfill.add_argument("--db", required=True, type=Path)
    cryptoevents_backfill.add_argument(
        "--start-date", help="Frozen UTC start-day filter YYYY-MM-DD (default: current UTC day)",
    )
    cryptoevents_backfill.add_argument(
        "--action", choices=("run", "status", "reset"), default="run",
    )
    cryptoevents_backfill.add_argument(
        "--pages", type=int, default=2,
        help="Maximum listing requests in this invocation (1–5)",
    )
    cryptoevents_backfill.add_argument("--timeout", type=float, default=20)
    cryptoevents_backfill.add_argument("--output", type=Path, help="Optional JSON result path")

    candidates = subcommands.add_parser(
        "candidates", help="Offline cross-provider duplicate candidates for human review"
    )
    candidates.add_argument("--db", required=True, type=Path)
    candidates.add_argument("--limit", type=int, default=100, help="Max returned pairs (1–1000)")
    candidates.add_argument(
        "--strategy", choices=("exact", "similarity"), default="exact",
        help="Exact title/date matching or scored fuzzy matching (default: exact)",
    )
    candidates.add_argument(
        "--min-score", type=float, default=0.80,
        help="Minimum similarity score for --strategy similarity (0–1)",
    )
    candidates.add_argument("--output", type=Path, help="Optional JSON report path")

    reconcile = subcommands.add_parser(
        "reconcile", help="Review cross-provider candidate identities"
    )
    reconcile.add_argument("--db", required=True, type=Path)
    reconcile.add_argument(
        "--action",
        choices=(
            "status", "pending", "list", "history", "membership-history", "evaluate",
            "review-export", "review-import", "diagnose-coverage",
            "accept", "reject", "unlink", "split",
        ),
        required=True,
    )
    reconcile.add_argument("--strategy", choices=("exact", "similarity"))
    reconcile.add_argument("--min-score", type=float, default=0.80)
    reconcile.add_argument(
        "--min-title-similarity",
        type=float,
        default=0.55,
        help="Minimum lexical similarity for read-only related-record hints",
    )
    reconcile.add_argument("--left-event-id")
    reconcile.add_argument("--right-event-id")
    reconcile.add_argument("--event-id", help="Source event ID for history or unlink")
    reconcile.add_argument("--canonical-event-id", help="Canonical identity ID for unlink, split or history")
    reconcile.add_argument(
        "--event-ids", nargs="+", help="One or more current member event IDs to move during split"
    )
    reconcile.add_argument("--note")
    reconcile.add_argument("--limit", type=int, default=100)
    reconcile.add_argument(
        "--input",
        type=Path,
        help="CSV review batch to validate or import (review-import)",
    )
    reconcile.add_argument(
        "--output",
        type=Path,
        help="CSV path for the human review batch (review-export)",
    )
    reconcile.add_argument(
        "--apply",
        action="store_true",
        help="Persist review-import decisions; without this flag import is a dry-run",
    )
    reconcile.add_argument(
        "--thresholds",
        nargs="+",
        type=float,
        default=[0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
        help="Similarity thresholds for retrospective evaluation (0–1)",
    )
    reconcile.add_argument(
        "--min-reviewed-pairs",
        type=int,
        default=30,
        help="Minimum reviewed pair revisions required for an evaluation status of evaluated",
    )

    relationship = subcommands.add_parser(
        "relationship",
        help="Review, analyze and export directed semantic relationships between source events",
    )
    relationship.add_argument("--db", required=True, type=Path)
    relationship.add_argument(
        "--action",
        choices=(
            "status", "list", "history", "candidates", "analytics",
            "neighborhood", "export", "accept", "reject", "retract",
        ),
        required=True,
    )
    relationship.add_argument("--subject-event-id")
    relationship.add_argument("--object-event-id")
    relationship.add_argument("--relationship-id")
    relationship.add_argument(
        "--relation-type",
        choices=(
            "participates_in", "sponsors", "speaks_at", "organizes",
            "attends", "exhibits_at", "related_to",
        ),
    )
    relationship.add_argument("--subject-revision", type=int)
    relationship.add_argument("--object-revision", type=int)
    relationship.add_argument(
        "--evidence-json",
        help="Optional JSON object containing reviewer-supplied evidence",
    )
    relationship.add_argument(
        "--event-id",
        help="Source event ID for history or neighborhood inspection",
    )
    relationship.add_argument("--note")
    relationship.add_argument("--limit", type=int, default=100)
    relationship.add_argument("--output", type=Path, help="Optional JSON or GraphML output path")
    relationship.add_argument(
        "--format",
        choices=("json", "graphml"),
        default="json",
        help="Graph export format (applies only to --action export)",
    )
    relationship.add_argument("--depth", type=int, default=2)
    relationship.add_argument(
        "--direction",
        choices=("outgoing", "incoming", "both"),
        default="both",
        help="Traversal direction for --action neighborhood",
    )
    relationship.add_argument(
        "--min-score",
        type=float,
        default=0.60,
        help="Minimum deterministic evidence score for review-only candidate discovery",
    )

    store = subcommands.add_parser("store", help="Inspect SQLite event snapshots and history")
    store.add_argument("--db", required=True, type=Path, help="Existing SQLite database path")
    store.add_argument("--action", choices=("status", "list", "history"), default="status")
    store.add_argument("--provider", help="Optional provider filter for list")
    store.add_argument("--event-id", help="Event ID for history")
    store.add_argument("--limit", type=int, default=100, help="Maximum listed events (1 to 1000)")
    return parser


def _scrape(args) -> int:
    from . import coindar_pagination, pagination
    from .normalization import SCHEMA_VERSION, UnifiedEvent, normalize_events
    from .providers import coindar, cryptoevents, icodrops
    from .storage import sync_events

    if args.input_html and args.section == "all":
        print("--input-html requires a single section", file=sys.stderr)
        return 2
    if args.provider == "coindar" and args.section not in {"upcoming", "ended"}:
        print("coindar supports only --section upcoming or ended", file=sys.stderr)
        return 2
    if args.start_date and (args.provider != "coindar" or args.section != "ended"):
        print("--start-date requires --provider coindar --section ended", file=sys.stderr)
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
    if not 1 <= args.pages <= pagination.MAX_PAGES:
        print(f"--pages must be between 1 and {pagination.MAX_PAGES}", file=sys.stderr)
        return 2
    if args.pages > 1 and args.provider == "icodrops":
        print("--pages > 1 is supported only for cryptoevents and coindar", file=sys.stderr)
        return 2
    if args.pages > 1 and (args.input_html or args.engine != "http"):
        print("--pages > 1 requires a live HTTP source, not --input-html or a browser", file=sys.stderr)
        return 2

    sections = ("upcoming", "active", "ended") if args.section == "all" else (args.section,)
    event_type = {
        "coindar": coindar.CoindarEvent,
        "cryptoevents": cryptoevents.CryptoEventsEvent,
        "icodrops": icodrops.ICODropsEvent,
    }[args.provider]
    try:
        unique = {}
        for section in sections:
            if args.provider == "cryptoevents":
                if args.input_html:
                    events = cryptoevents.parse_listing(args.input_html.read_text(encoding="utf-8"))
                elif args.pages > 1:
                    page_result = pagination.fetch_paginated_listing(
                        max_pages=args.pages, engine=args.engine, timeout_seconds=args.timeout
                    )
                    events = page_result.events
                    print(
                        json.dumps({
                            "pagination": {
                                "pages_fetched": page_result.pages_fetched,
                                "ajax_requests": page_result.ajax_requests,
                                "stop_reason": page_result.stop_reason,
                            }
                        }),
                        file=sys.stderr,
                    )
                else:
                    events = cryptoevents.fetch_listing(
                        engine=args.engine, timeout_seconds=args.timeout
                    )
            elif args.provider == "coindar":
                if args.input_html:
                    if args.start_date:
                        from datetime import date

                        try:
                            date.fromisoformat(args.start_date)
                        except ValueError as exc:
                            raise ValueError("--start-date must be YYYY-MM-DD") from exc
                    events = coindar.parse_listing(args.input_html.read_text(encoding="utf-8"))
                else:
                    batch = coindar_pagination.fetch_coindar_pages(
                        section=section, max_pages=args.pages, engine=args.engine,
                        timeout_seconds=args.timeout, start_date=args.start_date,
                    )
                    events = batch.events
                    if args.pages > 1:
                        print(json.dumps({"coindar_pagination": {
                            "pages_fetched": batch.pages_fetched,
                            "extra_requests": batch.extra_requests,
                            "stop_reason": batch.stop_reason,
                            "section": section,
                        }}), file=sys.stderr)
            else:
                events = (
                    icodrops.parse_listing(args.input_html.read_text(encoding="utf-8"), section=section)
                    if args.input_html
                    else icodrops.fetch_listing(section, engine=args.engine, timeout_seconds=args.timeout)
                )
            for event in events:
                unique[event.event_id] = event
        all_events = list(unique.values())
        if len(all_events) < args.min_events:
            raise RuntimeError(f"Expected at least {args.min_events} events, got {len(all_events)}")
        if args.schema == "unified":
            records = normalize_events(all_events)
            event_type = UnifiedEvent
        else:
            records = all_events
        selected = records[:args.limit] if args.limit is not None else records
        if args.format == "json":
            payload = {
                "provider": args.provider,
                "sections": list(sections),
                "extracted_count": len(all_events),
                "exported_count": len(selected),
                "events": [event.to_dict() for event in selected],
            }
            if args.schema == "unified":
                payload["schema"] = "unified"
                payload["schema_version"] = SCHEMA_VERSION
            output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(event_type.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(event.to_dict() for event in selected)
            output = buffer.getvalue()
        if args.db:
            normalized = records if args.schema == "unified" else normalize_events(all_events)
            result = sync_events(args.db, normalized)
            print(json.dumps({"database": str(args.db), "sync": result.to_dict()}), file=sys.stderr)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
            print(json.dumps({"output": str(args.output), "events": len(selected)}, ensure_ascii=False))
        else:
            print(output, end="")
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError, httpx.HTTPError) as exc:
        print(f"scrape failed: {exc}", file=sys.stderr)
        return 2


def _cryptoevents_backfill(args) -> int:
    from .cryptoevents_backfill import crawl_status, reset_crawl, run_crawl

    try:
        if args.action == "status":
            payload = crawl_status(args.db, start_date=args.start_date)
        elif args.action == "reset":
            payload = {
                "checkpoint_reset": reset_crawl(args.db, start_date=args.start_date),
                "event_rows_preserved": True,
            }
        else:
            payload = run_crawl(
                args.db,
                start_date=args.start_date,
                pages=args.pages,
                timeout_seconds=args.timeout,
            ).to_dict()
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        print(output, end="")
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError, httpx.HTTPError) as exc:
        print(f"cryptoevents-backfill failed: {exc}", file=sys.stderr)
        return 2


def _candidates(args) -> int:
    from .cross_provider_candidates import match_candidates, match_similar_candidates

    try:
        if args.strategy == "similarity":
            report = match_similar_candidates(
                args.db, limit=args.limit, min_score=args.min_score
            ).to_dict()
        else:
            report = match_candidates(args.db, limit=args.limit).to_dict()
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
            summary = {
                "output": str(args.output),
                "candidates_total": report["candidates_total"],
                "returned": report["returned"],
            }
            if "strategy" in report:
                summary["strategy"] = report["strategy"]
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(output, end="")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(f"candidates failed: {exc}", file=sys.stderr)
        return 2


def _store(args) -> int:
    from .storage import event_history, list_events, store_status

    try:
        if args.action == "status":
            result = store_status(args.db)
        elif args.action == "list":
            result = list_events(args.db, provider=args.provider, limit=args.limit)
        else:
            if not args.event_id:
                print("store --action history requires --event-id", file=sys.stderr)
                return 2
            result = event_history(args.db, args.event_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(f"store failed: {exc}", file=sys.stderr)
        return 2


def _reconcile(args) -> int:
    from .matching_coverage import diagnose_matching_coverage
    from .matching_evaluation import evaluate_reconciliation
    from .reconciliation import (
        canonical_list,
        decision_history,
        membership_history,
        pending_candidates,
        reconcile_pair,
        reconciliation_status,
        split_canonical_identity,
        unlink_member,
    )
    from .review_dataset import export_review_dataset, import_review_dataset

    try:
        if args.action in {"accept", "reject"}:
            if not args.left_event_id or not args.right_event_id or not args.strategy:
                raise ValueError("accept/reject require both event IDs and an explicit strategy")
            result = reconcile_pair(
                args.db, left_event_id=args.left_event_id, right_event_id=args.right_event_id,
                strategy=args.strategy, decision="accepted" if args.action == "accept" else "rejected",
                min_score=args.min_score, note=args.note,
            )
        elif args.action == "status":
            result = reconciliation_status(args.db)
        elif args.action == "pending":
            result = pending_candidates(
                args.db, strategy=args.strategy or "similarity",
                min_score=args.min_score, limit=args.limit,
            )
        elif args.action == "list":
            result = canonical_list(args.db, limit=args.limit)
        elif args.action == "history":
            result = decision_history(args.db, event_id=args.event_id, limit=args.limit)
        elif args.action == "evaluate":
            result = evaluate_reconciliation(
                args.db,
                thresholds=args.thresholds,
                min_reviewed_pairs=args.min_reviewed_pairs,
            )
        elif args.action == "diagnose-coverage":
            if args.input or args.output or args.apply:
                raise ValueError("diagnose-coverage does not accept --input, --output or --apply")
            result = diagnose_matching_coverage(
                args.db,
                limit=args.limit,
                min_title_similarity=args.min_title_similarity,
                strict_min_score=args.min_score,
            )
        elif args.action == "review-export":
            if not args.output:
                raise ValueError("review-export requires --output")
            if args.apply or args.input:
                raise ValueError("review-export does not accept --apply or --input")
            result = export_review_dataset(
                args.db,
                args.output,
                strategy=args.strategy or "similarity",
                min_score=args.min_score,
                limit=args.limit,
            )
        elif args.action == "review-import":
            if not args.input:
                raise ValueError("review-import requires --input")
            if args.output or args.strategy:
                raise ValueError("review-import accepts --input and optional --apply only")
            result = import_review_dataset(args.db, args.input, apply=args.apply)
        elif args.action == "membership-history":
            result = membership_history(
                args.db,
                event_id=args.event_id,
                canonical_event_id=args.canonical_event_id,
                limit=args.limit,
            )
        elif args.action == "unlink":
            if not args.canonical_event_id or not args.event_id:
                raise ValueError("unlink requires --canonical-event-id and --event-id")
            result = unlink_member(
                args.db,
                canonical_event_id=args.canonical_event_id,
                event_id=args.event_id,
                note=args.note,
            )
        else:
            if not args.canonical_event_id or not args.event_ids:
                raise ValueError("split requires --canonical-event-id and one or more --event-ids")
            result = split_canonical_identity(
                args.db,
                canonical_event_id=args.canonical_event_id,
                event_ids=args.event_ids,
                note=args.note,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, csv.Error, sqlite3.DatabaseError) as exc:
        message = str(exc)
        print(
            json.dumps(
                {
                    "status": "conflict" if message.startswith("conflict:") else "error",
                    "action": args.action,
                    "error": message,
                    "conflict": message if message.startswith("conflict:") else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


def _relationship(args) -> int:
    from .relationship_candidates import discover_relationship_candidates
    from .relationship_graph import (
        export_relationship_graph,
        relationship_graph_analytics,
        relationship_neighborhood,
    )
    from .relationships import (
        list_relationships,
        relationship_history,
        relationship_status,
        retract_relationship,
        review_relationship,
    )

    try:
        if args.format != "json" and args.action != "export":
            raise ValueError("--format applies only to --action export")
        if args.action == "candidates":
            result = discover_relationship_candidates(
                args.db, limit=args.limit, min_score=args.min_score
            )
        elif args.action == "status":
            result = relationship_status(args.db)
        elif args.action == "list":
            result = list_relationships(
                args.db,
                subject_event_id=args.subject_event_id,
                object_event_id=args.object_event_id,
                relation_type=args.relation_type,
                limit=args.limit,
            )
        elif args.action == "history":
            result = relationship_history(
                args.db,
                relationship_id=args.relationship_id,
                event_id=args.event_id,
                limit=args.limit,
            )
        elif args.action == "analytics":
            result = relationship_graph_analytics(args.db, limit=args.limit)
        elif args.action == "neighborhood":
            if not args.event_id:
                raise ValueError("neighborhood requires --event-id")
            result = relationship_neighborhood(
                args.db,
                event_id=args.event_id,
                depth=args.depth,
                direction=args.direction,
                limit=args.limit,
            )
        elif args.action == "export":
            result = export_relationship_graph(args.db, format=args.format)
            if isinstance(result, str):
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(result, encoding="utf-8")
                    print(
                        json.dumps(
                            {
                                "status": "exported",
                                "format": args.format,
                                "output": str(args.output),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    return 0
                print(result, end="")
                return 0
        elif args.action == "retract":
            if not args.relationship_id:
                raise ValueError("retract requires --relationship-id")
            result = retract_relationship(
                args.db, relationship_id=args.relationship_id, note=args.note
            )
        else:
            if (
                not args.subject_event_id
                or not args.object_event_id
                or not args.relation_type
            ):
                raise ValueError(
                    "accept/reject require subject ID, object ID and relation type"
                )
            evidence = None
            if args.evidence_json is not None:
                evidence = json.loads(args.evidence_json)
                if not isinstance(evidence, dict):
                    raise ValueError("--evidence-json must decode to a JSON object")
            result = review_relationship(
                args.db,
                subject_event_id=args.subject_event_id,
                object_event_id=args.object_event_id,
                relation_type=args.relation_type,
                decision="accepted" if args.action == "accept" else "rejected",
                expected_subject_revision=args.subject_revision,
                expected_object_revision=args.object_revision,
                evidence=evidence,
                note=args.note,
            )

        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            print(
                json.dumps(
                    {
                        "status": "written",
                        "action": args.action,
                        "output": str(args.output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(rendered, end="")
        return 0
    except (
        OSError, ValueError, RuntimeError, json.JSONDecodeError, sqlite3.DatabaseError
    ) as exc:
        message = str(exc)
        print(
            json.dumps(
                {
                    "status": "conflict" if message.startswith("conflict:") else "error",
                    "action": args.action,
                    "error": message,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


def _audit(args) -> int:
    from .pilot_evaluation import audit_source, compare_audits

    if args.min_records < 0:
        print("--min-records cannot be negative", file=sys.stderr)
        return 2
    try:
        report = audit_source(
            args.source, engine=args.engine, timeout_seconds=args.timeout,
            input_html=args.input_html, save_html=args.save_html,
        )
        if args.compare_to:
            previous = json.loads(args.compare_to.read_text(encoding="utf-8"))
            report["comparison"] = compare_audits(previous, report)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            print(json.dumps({"output": str(args.output), "status": report["status"]}))
        else:
            print(rendered, end="")
        if not args.strict:
            return 0
        valid = report["status"] == "parsed_listing"
        count = report.get("parsed_record_count", 0)
        return 0 if valid and count >= args.min_records else 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2


def _backfill(args) -> int:
    from .coindar_backfill import (
        backfill_status,
        backfill_undated,
        enrich_undated,
        reset_backfill,
        run_backfill,
    )

    try:
        if args.action == "status":
            payload = backfill_status(
                args.db, from_date=args.from_date, to_date=args.to_date
            )
        elif args.action == "undated":
            payload = backfill_undated(
                args.db, from_date=args.from_date, to_date=args.to_date,
                limit=args.limit,
            )
        elif args.action == "enrich":
            payload = enrich_undated(
                args.db, from_date=args.from_date, to_date=args.to_date,
            )
        elif args.action == "reset":
            payload = {
                "checkpoint_reset": reset_backfill(
                    args.db, from_date=args.from_date, to_date=args.to_date
                ),
                "event_rows_preserved": True,
                "undated_records_preserved": True,
            }
        else:
            result = run_backfill(
                args.db, from_date=args.from_date, to_date=args.to_date,
                pages=args.pages, timeout_seconds=args.timeout,
            )
            payload = result.to_dict()
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        print(output, end="")
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError, httpx.HTTPError) as exc:
        print(f"backfill failed: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scrape":
        return _scrape(args)
    if args.command == "audit":
        return _audit(args)
    if args.command == "cryptoevents-backfill":
        return _cryptoevents_backfill(args)
    if args.command == "candidates":
        return _candidates(args)
    if args.command == "store":
        return _store(args)
    if args.command == "reconcile":
        return _reconcile(args)
    if args.command == "relationship":
        return _relationship(args)
    if args.command == "backfill":
        return _backfill(args)
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
