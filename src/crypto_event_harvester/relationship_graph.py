"""Read-only analytics, bounded traversal and export for accepted event relationships."""

from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path

from .storage import DB_VERSION

_GRAPH_SCOPE = "accepted_edges_with_current_source_revisions"
_NODE_FIELDS = (
    "event_id",
    "provider",
    "title",
    "project",
    "ticker",
    "event_type",
    "category",
    "location",
    "start_date",
    "end_date",
    "date_precision",
    "source_url",
)


def _open_readonly(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"event database does not exist: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < DB_VERSION:
        conn.close()
        raise ValueError(
            f"relationship graph analytics requires database schema version "
            f"{DB_VERSION} or newer; found {version}. This command never migrates databases."
        )
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = {"events", "event_relationships"} - tables
    if missing:
        conn.close()
        raise ValueError(
            "database is missing relationship graph tables: " + ", ".join(sorted(missing))
        )
    return conn


def _node_from_row(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError(f"event payload is not a JSON object: {row['event_id']}")
    return {
        "event_id": row["event_id"],
        "provider": row["provider"],
        "revision": row["revision"],
        **{field: payload.get(field) for field in _NODE_FIELDS if field != "event_id"},
    }


def _load_graph(conn: sqlite3.Connection) -> tuple[dict[str, dict], list[dict], int]:
    rows = conn.execute(
        """
        SELECT
            r.relationship_id,
            r.subject_event_id,
            r.object_event_id,
            r.relation_type,
            r.subject_revision,
            r.object_revision,
            r.accepted_decision_id,
            r.created_at_utc,
            r.updated_at_utc,
            s.provider AS subject_provider,
            s.revision AS subject_current_revision,
            s.payload_json AS subject_payload_json,
            o.provider AS object_provider,
            o.revision AS object_current_revision,
            o.payload_json AS object_payload_json
        FROM event_relationships AS r
        JOIN events AS s ON s.event_id = r.subject_event_id
        JOIN events AS o ON o.event_id = r.object_event_id
        ORDER BY r.relationship_id
        """
    ).fetchall()

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stale_edges = 0
    for row in rows:
        is_stale = (
            row["subject_revision"] != row["subject_current_revision"]
            or row["object_revision"] != row["object_current_revision"]
        )
        if is_stale:
            stale_edges += 1
            continue

        subject = _node_from_row(
            {
                "event_id": row["subject_event_id"],
                "provider": row["subject_provider"],
                "revision": row["subject_current_revision"],
                "payload_json": row["subject_payload_json"],
            }
        )
        object_event = _node_from_row(
            {
                "event_id": row["object_event_id"],
                "provider": row["object_provider"],
                "revision": row["object_current_revision"],
                "payload_json": row["object_payload_json"],
            }
        )
        nodes[subject["event_id"]] = subject
        nodes[object_event["event_id"]] = object_event
        edges.append(
            {
                "relationship_id": row["relationship_id"],
                "subject_event_id": row["subject_event_id"],
                "object_event_id": row["object_event_id"],
                "relation_type": row["relation_type"],
                "subject_revision": row["subject_revision"],
                "object_revision": row["object_revision"],
                "accepted_decision_id": row["accepted_decision_id"],
                "created_at_utc": row["created_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
                "subject_provider": row["subject_provider"],
                "object_provider": row["object_provider"],
            }
        )
    return nodes, edges, stale_edges


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _components(nodes: dict[str, dict], edges: list[dict]) -> list[int]:
    parent = {event_id: event_id for event_id in nodes}

    def find(event_id: str) -> str:
        root = event_id
        while parent[root] != root:
            root = parent[root]
        while parent[event_id] != event_id:
            next_id = parent[event_id]
            parent[event_id] = root
            event_id = next_id
        return root

    for edge in edges:
        left = find(edge["subject_event_id"])
        right = find(edge["object_event_id"])
        if left != right:
            parent[right] = left

    sizes = Counter(find(event_id) for event_id in nodes)
    return sorted(sizes.values(), reverse=True)


def _ranked_nodes(
    nodes: dict[str, dict],
    edges: list[dict],
    *,
    incoming: bool,
    limit: int,
) -> list[dict]:
    counts: Counter[str] = Counter()
    neighbors: dict[str, set[str]] = defaultdict(set)
    relation_types: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        event_id = edge["object_event_id"] if incoming else edge["subject_event_id"]
        neighbor_id = edge["subject_event_id"] if incoming else edge["object_event_id"]
        counts[event_id] += 1
        neighbors[event_id].add(neighbor_id)
        relation_types[event_id].add(edge["relation_type"])

    result = []
    for event_id, count in counts.items():
        result.append(
            {
                **nodes[event_id],
                "relationship_count": count,
                "distinct_neighbors": len(neighbors[event_id]),
                "relation_types": sorted(relation_types[event_id]),
            }
        )
    result.sort(
        key=lambda item: (
            -item["relationship_count"],
            item.get("title") or "",
            item["event_id"],
        )
    )
    return result[:limit]


def relationship_graph_analytics(
    database_path: str | Path,
    *,
    limit: int = 20,
) -> dict:
    """Summarize accepted, non-stale graph edges without modifying the database."""
    _validate_limit(limit)
    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        nodes, edges, stale_edges = _load_graph(conn)
    finally:
        conn.close()

    relation_counts = Counter(edge["relation_type"] for edge in edges)
    provider_pairs = Counter(
        f'{edge["subject_provider"]}->{edge["object_provider"]}' for edge in edges
    )
    component_sizes = _components(nodes, edges)
    return {
        "status": "analyzed" if edges else "empty_graph",
        "mode": "read_only",
        "database_version": version,
        "scope": _GRAPH_SCOPE,
        "nodes_total": len(nodes),
        "edges_total": len(edges),
        "stale_edges_excluded": stale_edges,
        "weakly_connected_components": len(component_sizes),
        "component_sizes": component_sizes,
        "largest_component_size": component_sizes[0] if component_sizes else 0,
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "provider_pair_counts": dict(sorted(provider_pairs.items())),
        "top_targets": _ranked_nodes(nodes, edges, incoming=True, limit=limit),
        "top_subjects": _ranked_nodes(nodes, edges, incoming=False, limit=limit),
        "interpretation": (
            "Connectivity and degree describe accepted direct edges only; "
            "they do not establish transitive semantic relationships."
        ),
        "database_rows_modified": 0,
        "source_requests": 0,
        "canonical_identity_modified": False,
    }


def relationship_neighborhood(
    database_path: str | Path,
    *,
    event_id: str,
    depth: int = 2,
    direction: str = "both",
    limit: int = 100,
) -> dict:
    """Return bounded simple paths around one event using accepted, current edges."""
    if not event_id:
        raise ValueError("event_id is required")
    if not 1 <= depth <= 3:
        raise ValueError("depth must be between 1 and 3")
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("direction must be outgoing, incoming or both")
    _validate_limit(limit)

    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        start_row = conn.execute(
            "SELECT event_id, provider, revision, payload_json FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if start_row is None:
            raise ValueError(f"unknown source event ID: {event_id}")
        nodes, edges, stale_edges = _load_graph(conn)
        start_node = _node_from_row(start_row)
    finally:
        conn.close()

    nodes.setdefault(event_id, start_node)
    adjacency: dict[str, list[tuple[str, dict, str]]] = defaultdict(list)
    for edge in edges:
        if direction in {"outgoing", "both"}:
            adjacency[edge["subject_event_id"]].append(
                (edge["object_event_id"], edge, "forward")
            )
        if direction in {"incoming", "both"}:
            adjacency[edge["object_event_id"]].append(
                (edge["subject_event_id"], edge, "reverse")
            )
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: (item[0], item[1]["relationship_id"], item[2]))

    queue = deque([(event_id, [event_id], [])])
    paths: list[dict] = []
    minimum_depth = {event_id: 0}
    used_edges: dict[str, dict] = {}
    truncated = False
    while queue and not truncated:
        current, path_nodes, path_steps = queue.popleft()
        if len(path_steps) >= depth:
            continue
        for neighbor_id, edge, traversal in adjacency.get(current, []):
            if neighbor_id in path_nodes:
                continue
            if len(paths) >= limit:
                truncated = True
                break
            step = {
                "relationship_id": edge["relationship_id"],
                "relation_type": edge["relation_type"],
                "from_event_id": current,
                "to_event_id": neighbor_id,
                "edge_subject_event_id": edge["subject_event_id"],
                "edge_object_event_id": edge["object_event_id"],
                "traversal": traversal,
            }
            next_nodes = [*path_nodes, neighbor_id]
            next_steps = [*path_steps, step]
            paths.append(
                {
                    "depth": len(next_steps),
                    "event_ids": next_nodes,
                    "steps": next_steps,
                }
            )
            minimum_depth[neighbor_id] = min(
                minimum_depth.get(neighbor_id, len(next_steps)), len(next_steps)
            )
            used_edges[edge["relationship_id"]] = edge
            queue.append((neighbor_id, next_nodes, next_steps))

    rendered_nodes = [
        {**nodes[node_id], "depth": node_depth}
        for node_id, node_depth in sorted(
            minimum_depth.items(), key=lambda item: (item[1], item[0])
        )
    ]
    return {
        "status": "truncated" if truncated else "complete",
        "mode": "read_only",
        "database_version": version,
        "scope": _GRAPH_SCOPE,
        "start_event_id": event_id,
        "direction": direction,
        "max_depth": depth,
        "nodes": rendered_nodes,
        "edges": [used_edges[key] for key in sorted(used_edges)],
        "paths": paths,
        "paths_returned": len(paths),
        "stale_edges_excluded": stale_edges,
        "interpretation": (
            "Paths show graph connectivity only. Predicates are not composed, "
            "and no transitive semantic relationship is inferred."
        ),
        "database_rows_modified": 0,
        "source_requests": 0,
        "canonical_identity_modified": False,
    }


def _graphml(nodes: dict[str, dict], edges: list[dict], stale_edges: int) -> str:
    namespace = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", namespace)

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    root = ET.Element(tag("graphml"))
    node_keys = (
        ("n_event_id", "event_id", "string"),
        ("n_provider", "provider", "string"),
        ("n_title", "title", "string"),
        ("n_project", "project", "string"),
        ("n_ticker", "ticker", "string"),
        ("n_event_type", "event_type", "string"),
        ("n_category", "category", "string"),
        ("n_location", "location", "string"),
        ("n_start_date", "start_date", "string"),
        ("n_end_date", "end_date", "string"),
        ("n_source_url", "source_url", "string"),
        ("n_revision", "revision", "int"),
    )
    edge_keys = (
        ("e_relationship_id", "relationship_id", "string"),
        ("e_relation_type", "relation_type", "string"),
        ("e_subject_revision", "subject_revision", "int"),
        ("e_object_revision", "object_revision", "int"),
        ("e_created_at_utc", "created_at_utc", "string"),
    )
    for key_id, name, value_type in (*node_keys, *edge_keys):
        target = "node" if key_id.startswith("n_") else "edge"
        ET.SubElement(
            root,
            tag("key"),
            {
                "id": key_id,
                "for": target,
                "attr.name": name,
                "attr.type": value_type,
            },
        )
    ET.SubElement(
        root,
        tag("key"),
        {
            "id": "g_stale_edges_excluded",
            "for": "graph",
            "attr.name": "stale_edges_excluded",
            "attr.type": "int",
        },
    )
    graph = ET.SubElement(
        root,
        tag("graph"),
        {"id": "crypto-event-relationships", "edgedefault": "directed"},
    )
    ET.SubElement(graph, tag("data"), {"key": "g_stale_edges_excluded"}).text = str(
        stale_edges
    )

    node_ids = {
        event_id: f"n{index}"
        for index, event_id in enumerate(sorted(nodes))
    }
    node_key_map = {field: key for key, field, _ in node_keys}
    for event_id in sorted(nodes):
        node = ET.SubElement(graph, tag("node"), {"id": node_ids[event_id]})
        for field, value in nodes[event_id].items():
            key = node_key_map.get(field)
            if key is not None and value is not None:
                ET.SubElement(node, tag("data"), {"key": key}).text = str(value)

    edge_key_map = {field: key for key, field, _ in edge_keys}
    for index, edge in enumerate(edges):
        edge_element = ET.SubElement(
            graph,
            tag("edge"),
            {
                "id": f"e{index}",
                "source": node_ids[edge["subject_event_id"]],
                "target": node_ids[edge["object_event_id"]],
            },
        )
        for field, value in edge.items():
            key = edge_key_map.get(field)
            if key is not None and value is not None:
                ET.SubElement(edge_element, tag("data"), {"key": key}).text = str(value)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"


def export_relationship_graph(
    database_path: str | Path,
    *,
    format: str = "json",
) -> dict | str:
    """Export accepted, current graph edges as JSON data or directed GraphML."""
    if format not in {"json", "graphml"}:
        raise ValueError("format must be json or graphml")
    conn = _open_readonly(database_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        nodes, edges, stale_edges = _load_graph(conn)
    finally:
        conn.close()

    if format == "graphml":
        return _graphml(nodes, edges, stale_edges)
    return {
        "status": "exported",
        "mode": "read_only",
        "format": "json",
        "database_version": version,
        "scope": _GRAPH_SCOPE,
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": edges,
        "stale_edges_excluded": stale_edges,
        "database_rows_modified": 0,
        "source_requests": 0,
        "canonical_identity_modified": False,
    }
