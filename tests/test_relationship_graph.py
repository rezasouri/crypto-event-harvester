"""Offline tests for read-only semantic relationship graph analytics and export."""

import json
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_event_harvester.cli import main
from crypto_event_harvester.normalization import normalize_event
from crypto_event_harvester.providers.coindar import parse_listing as parse_coindar
from crypto_event_harvester.providers.cryptoevents import parse_listing as parse_cryptoevents
from crypto_event_harvester.relationship_graph import (
    _open_readonly,
    export_relationship_graph,
    relationship_graph_analytics,
    relationship_neighborhood,
)
from crypto_event_harvester.relationships import review_relationship
from crypto_event_harvester.storage import sync_events

FIXTURES = Path(__file__).parent / "fixtures"
GRAPHML_NS = "{http://graphml.graphdrawing.org/xmlns}"


def _seed(database_path):
    coindar = normalize_event(
        parse_coindar(
            (FIXTURES / "coindar_listing_sample.html").read_text(encoding="utf-8")
        )[0]
    )
    crypto = normalize_event(
        parse_cryptoevents(
            (FIXTURES / "cryptoevents_home.html").read_text(encoding="utf-8")
        )[0]
    )
    subject = replace(
        coindar,
        title="Verified Emeralds at India Blockchain Week",
        project="Verified Emeralds",
        ticker="VEREM",
        description="Verified Emeralds will participate in India Blockchain Week in Mumbai.",
        start_date="2026-11-01",
        end_date="2026-11-01",
        date_text="2026-11-01",
        date_precision="day",
        period_key=None,
    )
    conference = replace(
        crypto,
        title="India Blockchain Week 2026 Conference",
        description="India Blockchain Week is a conference in Mumbai.",
        location="Mumbai",
        start_date="2026-11-01",
        end_date="2026-11-02",
        date_text="Nov 1-2, 2026",
        date_precision="range",
        period_key=None,
    )
    sync_events(
        database_path,
        [subject, conference],
        observed_at_utc="2026-11-01T10:00:00+00:00",
    )
    return subject, conference


def _clone(event, source_event_id, *, title, project=None, location=None):
    return replace(
        event,
        event_id=f"{event.provider}:{source_event_id}",
        source_event_id=source_event_id,
        source_url=f"https://example.test/{source_event_id}",
        listing_url=f"https://example.test/{source_event_id}",
        title=title,
        project=project,
        ticker=None,
        location=location,
        description=f"Description for {title}",
    )


def _accept(db, subject, object_event, relation_type):
    return review_relationship(
        db,
        subject_event_id=subject.event_id,
        object_event_id=object_event.event_id,
        relation_type=relation_type,
        decision="accepted",
        expected_subject_revision=1,
        expected_object_revision=1,
        evidence={"review": "offline-test"},
    )


def _seed_graph(database_path):
    subject, conference = _seed(database_path)
    second_subject = _clone(
        subject,
        "graph-subject-2",
        title="Second Project at India Blockchain Week",
        project="Second Project",
    )
    second_conference = _clone(
        conference,
        "graph-conference-2",
        title="Second Blockchain Conference",
        project=None,
        location="Singapore",
    )
    sync_events(
        database_path,
        [second_subject, second_conference],
        observed_at_utc="2026-11-01T10:05:00+00:00",
    )
    _accept(database_path, subject, conference, "participates_in")
    _accept(database_path, second_subject, conference, "sponsors")
    _accept(database_path, conference, second_conference, "related_to")
    _accept(database_path, subject, second_conference, "attends")
    return subject, conference, second_subject, second_conference


def test_graph_analytics_reports_directed_degrees_and_weak_components(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference, second_subject, second_conference = _seed_graph(db)

    report = relationship_graph_analytics(db, limit=10)

    assert report["status"] == "analyzed"
    assert report["mode"] == "read_only"
    assert report["scope"] == "accepted_edges_with_current_source_revisions"
    assert report["nodes_total"] == 4
    assert report["edges_total"] == 4
    assert report["stale_edges_excluded"] == 0
    assert report["weakly_connected_components"] == 1
    assert report["component_sizes"] == [4]
    assert report["relation_type_counts"] == {
        "attends": 1,
        "participates_in": 1,
        "related_to": 1,
        "sponsors": 1,
    }
    assert report["provider_pair_counts"] == {
        "coindar->cryptoevents": 3,
        "cryptoevents->cryptoevents": 1,
    }
    assert report["top_targets"][0]["event_id"] == conference.event_id
    assert report["top_targets"][0]["relationship_count"] == 2
    assert report["top_targets"][0]["distinct_neighbors"] == 2
    assert report["top_subjects"][0]["event_id"] == subject.event_id
    assert report["top_subjects"][0]["relationship_count"] == 2
    assert report["database_rows_modified"] == 0
    assert report["source_requests"] == 0
    assert report["canonical_identity_modified"] is False
    assert second_subject.event_id != subject.event_id
    assert second_conference.event_id != conference.event_id


def test_neighborhood_returns_bounded_paths_without_composing_predicates(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference, _, second_conference = _seed_graph(db)

    report = relationship_neighborhood(
        db,
        event_id=subject.event_id,
        depth=2,
        direction="outgoing",
    )

    assert report["status"] == "complete"
    assert report["start_event_id"] == subject.event_id
    assert report["direction"] == "outgoing"
    assert report["max_depth"] == 2
    assert report["paths_returned"] == 3
    assert {node["event_id"] for node in report["nodes"]} == {
        subject.event_id,
        conference.event_id,
        second_conference.event_id,
    }
    assert any(
        path["event_ids"] == [
            subject.event_id,
            conference.event_id,
            second_conference.event_id,
        ]
        for path in report["paths"]
    )
    indirect = next(path for path in report["paths"] if path["depth"] == 2)
    assert [step["relation_type"] for step in indirect["steps"]] == [
        "participates_in",
        "related_to",
    ]
    assert indirect["steps"][0]["traversal"] == "forward"
    assert "not composed" in report["interpretation"]
    assert report["database_rows_modified"] == 0


def test_neighborhood_supports_incoming_direction_and_path_limit(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference, second_subject, _ = _seed_graph(db)

    incoming = relationship_neighborhood(
        db,
        event_id=conference.event_id,
        depth=2,
        direction="incoming",
    )
    assert incoming["paths_returned"] == 2
    assert {path["event_ids"][-1] for path in incoming["paths"]} == {
        subject.event_id,
        second_subject.event_id,
    }
    assert all(path["steps"][0]["traversal"] == "reverse" for path in incoming["paths"])

    bounded = relationship_neighborhood(
        db,
        event_id=subject.event_id,
        depth=2,
        direction="outgoing",
        limit=1,
    )
    assert bounded["status"] == "truncated"
    assert bounded["paths_returned"] == 1


def test_neighborhood_includes_an_existing_isolated_start_node(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, _ = _seed(db)

    report = relationship_neighborhood(
        db,
        event_id=subject.event_id,
        depth=1,
        direction="both",
    )

    assert report["paths"] == []
    assert report["edges"] == []
    assert report["nodes"] == [
        {
            "event_id": subject.event_id,
            "provider": subject.provider,
            "revision": 1,
            "title": subject.title,
            "project": subject.project,
            "ticker": subject.ticker,
            "event_type": subject.event_type,
            "category": subject.category,
            "location": subject.location,
            "start_date": subject.start_date,
            "end_date": subject.end_date,
            "date_precision": subject.date_precision,
            "source_url": subject.source_url,
            "depth": 0,
        }
    ]


def test_stale_edges_are_excluded_from_analytics_and_exports(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, _, _, _ = _seed_graph(db)
    updated_subject = replace(
        subject,
        description="Updated source description; relationship must be reviewed again.",
    )
    sync_events(
        db,
        [updated_subject],
        observed_at_utc="2026-11-01T11:00:00+00:00",
    )

    report = relationship_graph_analytics(db)
    exported = export_relationship_graph(db, format="json")

    assert report["edges_total"] == 2
    assert report["stale_edges_excluded"] == 2
    assert report["nodes_total"] == 3
    assert exported["stale_edges_excluded"] == 2
    assert len(exported["edges"]) == 2
    assert all(
        edge["subject_revision"] == 1 or edge["object_revision"] == 1
        for edge in exported["edges"]
    )


def test_json_and_graphml_exports_preserve_direction_and_metadata(tmp_path):
    db = tmp_path / "events.sqlite"
    subject, conference, _, _ = _seed_graph(db)

    exported = export_relationship_graph(db, format="json")
    assert exported["format"] == "json"
    assert len(exported["nodes"]) == 4
    assert len(exported["edges"]) == 4
    assert any(
        edge["subject_event_id"] == subject.event_id
        and edge["object_event_id"] == conference.event_id
        and edge["relation_type"] == "participates_in"
        for edge in exported["edges"]
    )

    graphml = export_relationship_graph(db, format="graphml")
    root = ET.fromstring(graphml)
    graph = root.find(f"{GRAPHML_NS}graph")
    assert graph is not None
    assert graph.attrib["edgedefault"] == "directed"
    assert len(graph.findall(f"{GRAPHML_NS}node")) == 4
    assert len(graph.findall(f"{GRAPHML_NS}edge")) == 4
    node_ids = {
        node.attrib["id"] for node in graph.findall(f"{GRAPHML_NS}node")
    }
    assert all(
        edge.attrib["source"] in node_ids and edge.attrib["target"] in node_ids
        for edge in graph.findall(f"{GRAPHML_NS}edge")
    )
    assert root.find(f"{GRAPHML_NS}key[@id='e_relation_type']") is not None


def test_graph_commands_are_query_only_and_do_not_migrate_legacy_databases(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_graph(db)

    conn = _open_readonly(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM event_relationships")
    conn.close()

    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="never migrates"):
        relationship_graph_analytics(legacy)

    conn = sqlite3.connect(legacy)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() == [("events",)]
    conn.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit must be between 1 and 1000"),
        ({"depth": 0}, "depth must be between 1 and 3"),
        ({"depth": 4}, "depth must be between 1 and 3"),
        ({"direction": "sideways"}, "direction must be outgoing, incoming or both"),
    ],
)
def test_neighborhood_rejects_invalid_bounds_and_direction(tmp_path, kwargs, message):
    db = tmp_path / "events.sqlite"
    subject, _ = _seed(db)
    with pytest.raises(ValueError, match=message):
        relationship_neighborhood(db, event_id=subject.event_id, **kwargs)


def test_graph_export_rejects_unknown_format(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed(db)
    with pytest.raises(ValueError, match="format must be json or graphml"):
        export_relationship_graph(db, format="csv")


def test_relationship_graph_cli_analytics_neighborhood_and_graphml_export(
    tmp_path, capsys
):
    db = tmp_path / "events.sqlite"
    subject, _, _, _ = _seed_graph(db)

    assert main([
        "relationship", "--db", str(db), "--action", "analytics",
    ]) == 0
    analytics = json.loads(capsys.readouterr().out)
    assert analytics["edges_total"] == 4

    assert main([
        "relationship", "--db", str(db), "--action", "neighborhood",
        "--event-id", subject.event_id, "--depth", "2", "--direction", "outgoing",
    ]) == 0
    neighborhood = json.loads(capsys.readouterr().out)
    assert neighborhood["start_event_id"] == subject.event_id
    assert neighborhood["paths_returned"] == 3

    output = tmp_path / "relationship-graph.graphml"
    assert main([
        "relationship", "--db", str(db), "--action", "export",
        "--format", "graphml", "--output", str(output),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "exported"
    assert output.is_file()
    assert ET.parse(output).getroot().find(f"{GRAPHML_NS}graph") is not None
