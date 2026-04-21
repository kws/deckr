"""Wire serialization for Node | SubGraphNode. Uses invariant native JSON serialization (dump_graph_to_dict / load_graph_from_dict)."""

from __future__ import annotations

from typing import Any

from invariant import dump_graph_to_dict, load_graph_from_dict


def graph_to_wire(graph: dict[str, Any], output: str) -> dict[str, Any]:
    """Serialize a graph dict (Node values) to a JSON-serializable wire format."""
    envelope = dump_graph_to_dict(graph)
    return {"graph": envelope, "output": output}


def graph_from_wire(wire: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Deserialize wire format to (graph dict, output). Returns None if wire is empty/invalid."""
    if not wire:
        return None
    output = wire.get("output", "output")
    raw = wire.get("graph")
    if raw is None:
        return None
    # Invariant envelope: format="invariant-graph" (per spec §2)
    if isinstance(raw, dict) and raw.get("format") == "invariant-graph":
        graph = load_graph_from_dict(raw)
        return graph, output
    # Legacy in-process: raw graph dict passed by reference (no format key)
    return raw, output
