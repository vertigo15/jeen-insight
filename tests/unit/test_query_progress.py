from __future__ import annotations

import pytest

from src.agent.langgraph_agent.graph import _timed as sql_timed
from src.agent.langgraph_agent_dax.graph import _timed as dax_timed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper", "node"),
    [
        (sql_timed, "execute_query"),
        (dax_timed, "pbi_execute_query"),
    ],
)
async def test_timed_nodes_emit_started_and_finished(wrapper, node):
    events = []

    async def work(_state):
        return {"answer": "ok"}

    result = await wrapper(node, work)({"progress_callback": events.append})

    assert [event["status"] for event in events] == [
        "node_started",
        "node_finished",
    ]
    assert all(event["node"] == node for event in events)
    assert events[-1]["elapsed_ms"] >= 0
    assert result["trace"][0]["node"] == node


@pytest.mark.asyncio
async def test_timed_node_emits_failure_without_sensitive_state():
    events = []

    async def fail(_state):
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        await sql_timed("execute_query", fail)(
            {
                "question": "secret question",
                "generated_sql": "SELECT secret",
                "progress_callback": events.append,
            }
        )

    assert [event["status"] for event in events] == [
        "node_started",
        "node_failed",
    ]
    assert "question" not in events[-1]
    assert "sql" not in events[-1]
