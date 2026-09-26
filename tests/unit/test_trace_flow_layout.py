"""The Run Details transition map must draw the graphs that actually run.

``src/static/trace/traceFlowLayout.js`` is hand-laid-out, so it is compared with
the compiled graphs: every node and every arrow of ``build_graph()`` and
``build_dax_graph()`` (START drawn from ``pre_graph_setup``, the request pre-load
that runs before the graph). The arrows come from ``get_graph()``, whose branch
targets are the routing functions' ``Literal`` return types — so those types are
checked against the functions' actual ``return`` values too.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agent.langgraph_agent.graph import build_graph
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.agent.langgraph_agent_dax.graph import build_dax_graph
from src.agent.langgraph_agent_dax.prompt_loader import DaxPromptLoader

ROOT = Path(__file__).resolve().parents[2]
LAYOUT_JS = ROOT / "src/static/trace/traceFlowLayout.js"
GRAPH_MODULES = {
    "sql": ROOT / "src/agent/langgraph_agent/graph.py",
    "dax": ROOT / "src/agent/langgraph_agent_dax/graph.py",
}
PRE_GRAPH = "pre_graph_setup"


def _layouts() -> dict:
    script = (
        "const layouts = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify(layouts));"
    )
    out = subprocess.run(
        ["node", "-e", script, str(LAYOUT_JS)], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def _compiled(engine: str):
    if engine == "sql":
        return build_graph(
            llm=MagicMock(), router_llm=MagicMock(), sql_runner=MagicMock(), metadata_loader=MagicMock(),
            history_service=MagicMock(), prompt_loader=PromptLoader(), deployment_name="test",
        )
    return build_dax_graph(
        llm=MagicMock(), router_llm=MagicMock(), metadata_loader=MagicMock(), history_service=MagicMock(),
        prompt_loader=DaxPromptLoader(), deployment_name="test",
    )


def _graph_shape(engine: str) -> tuple[set[str], set[tuple[str, str]]]:
    drawable = _compiled(engine).get_graph()
    nodes = {node for node in drawable.nodes if node not in ("__start__", "__end__")} | {PRE_GRAPH}
    edges = {
        (PRE_GRAPH if edge.source == "__start__" else edge.source, edge.target)
        for edge in drawable.edges
        if edge.target != "__end__"
    }
    return nodes, edges


@pytest.fixture(scope="module")
def layouts() -> dict:
    return _layouts()


@pytest.mark.parametrize("engine", ["sql", "dax"])
def test_layout_draws_every_node_of_the_graph(layouts, engine):
    graph_nodes, _ = _graph_shape(engine)
    drawn = [node for column in layouts[engine]["columns"] for node in column["nodes"]]
    assert len(drawn) == len(set(drawn)), "a node is placed in two columns"
    assert set(drawn) == graph_nodes


@pytest.mark.parametrize("engine", ["sql", "dax"])
def test_layout_draws_exactly_the_graph_arrows(layouts, engine):
    _, graph_edges = _graph_shape(engine)
    drawn = [(edge[0], edge[1]) for edge in layouts[engine]["edges"]]
    assert len(drawn) == len(set(drawn)), "an arrow is listed twice"
    assert set(drawn) - graph_edges == set(), "arrows that do not exist in the graph"
    assert graph_edges - set(drawn) == set(), "graph arrows missing from the map"
    assert all(len(edge) == 3 and edge[2] for edge in layouts[engine]["edges"]), "every arrow needs a label"


@pytest.mark.parametrize("engine", ["sql", "dax"])
def test_optional_nodes_are_placed(layouts, engine):
    drawn = {node for column in layouts[engine]["columns"] for node in column["nodes"]}
    assert set(layouts[engine]["optional"]) <= drawn


def _string_returns(node: ast.AST) -> set[str]:
    """String targets a routing function can return (constants and ``a if c else b``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _string_returns(node.body) | _string_returns(node.orelse)
    raise AssertionError(f"routing return is not a string literal: {ast.dump(node)}")


def _literal_targets(annotation: ast.AST | None) -> set[str]:
    assert isinstance(annotation, ast.Subscript) and getattr(annotation.value, "id", None) == "Literal", (
        "routing functions must declare Literal[...] targets"
    )
    elements = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    return {element.value for element in elements}


@pytest.mark.parametrize("engine", ["sql", "dax"])
def test_routing_literals_match_their_return_values(engine):
    tree = ast.parse(GRAPH_MODULES[engine].read_text(encoding="utf-8"))
    routers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_route_from")
    ]
    assert routers, "no routing functions found"
    for router in routers:
        returned: set[str] = set()
        for child in ast.walk(router):
            if isinstance(child, ast.Return) and child.value is not None:
                returned |= _string_returns(child.value)
        assert returned == _literal_targets(router.returns), router.name
