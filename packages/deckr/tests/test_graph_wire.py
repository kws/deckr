"""Tests for graph_wire: invariant-native serialization of key image graphs."""

from deckr.plugin.graph_wire import graph_from_wire, graph_to_wire


def _make_minimal_graph():
    """Create a minimal graph for round-trip and envelope tests (no controller dependency)."""
    from invariant import Node

    return {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (0, 0, 0, 255),
            },
            deps=["canvas"],
        ),
    }


def test_graph_to_wire_round_trip():
    """Round-trip preserves graph structure."""
    graph = _make_minimal_graph()
    output = "bg"
    wire = graph_to_wire(graph, output)
    parsed = graph_from_wire(wire)
    assert parsed is not None
    result_graph, result_output = parsed
    assert result_output == output
    # Invariant uses sorted keys (spec §6); compare sets, not order
    assert set(result_graph.keys()) == set(graph.keys())


def test_graph_to_wire_envelope_structure():
    """graph_to_wire output has invariant envelope format and version."""
    graph = _make_minimal_graph()
    output = "bg"
    wire = graph_to_wire(graph, output)
    assert wire["graph"]["format"] == "invariant-graph"
    assert wire["graph"]["version"] == 1
    assert "graph" in wire["graph"]
    assert wire["output"] == output


def test_graph_from_wire_in_process_fallback():
    """Raw graph dict without format key is returned as-is (in-process path)."""
    from invariant import Node

    raw_graph = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (0, 0, 0, 255),
            },
            deps=["canvas"],
        ),
    }
    wire = {"graph": raw_graph, "output": "bg"}
    parsed = graph_from_wire(wire)
    assert parsed is not None
    graph, output = parsed
    assert output == "bg"
    assert graph is raw_graph
    assert list(graph.keys()) == ["bg"]


def test_graph_from_wire_empty_returns_none():
    """Empty wire returns None."""
    assert graph_from_wire({}) is None


def test_graph_from_wire_none_returns_none():
    """None/falsy wire returns None."""
    assert graph_from_wire(None) is None  # type: ignore[arg-type]


def test_graph_from_wire_missing_graph_returns_none():
    """Wire with no graph key returns None."""
    assert graph_from_wire({"output": "x"}) is None
