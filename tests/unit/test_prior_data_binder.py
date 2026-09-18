"""prior_data_binder: values from a stored result become a verified IN filter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.langgraph_agent.nodes.binder import build_binding, make_prior_data_binder
from src.agent.langgraph_agent.nodes.validation import make_sqlglot_validate
from src.agent.langgraph_agent.prompt_loader import PromptLoader
from src.api.result_cache import result_cache

PROMPTS = PromptLoader()

PRIOR = {
    "id": "qid-prev", "natural_language_query": "products and prices",
    "generated_sql": "SELECT productkey, product, price FROM p",
    "result_artifact": {"columns": ["productkey", "product", "price"], "row_count": 4},
    "snapshot_status": "stored",
}


def _llm(content):
    llm = MagicMock()
    llm.generate = AsyncMock(return_value={"content": content, "finish_reason": "stop", "usage": {"total_tokens": 10}})
    return llm


def _state(connection, **over):
    result_cache.put(
        user_id="u1", connection=connection, query_id="qid-prev",
        dataset={"columns": ["productkey", "product", "price"],
                 "rows": [{"productkey": 1, "product": "Bike", "price": 1200},
                          {"productkey": 2, "product": "Helmet", "price": 80},
                          {"productkey": 3, "product": "Lock", "price": 25},
                          {"productkey": 4, "product": "Light", "price": 20}]},
    )
    base = {
        "question": "show me the sales of the 2 most expensive products from the previous answer",
        "conversation_history": [PRIOR], "prior_refs": ["T1"],
        "user_id": "u1", "source_key": connection, "session_id": "s1",
        "metadata_bundle": {"columns": "factsales.productkey - Type: int\ndimproduct.productkey - Type: int"},
        "table_columns": {"factsales": ["productkey", "salesamount"], "dimproduct": ["productkey", "name"]},
        "filter_plan": {"filters": [], "invalid_filters": []}, "resolved_filters": [],
        "llm_call_count": 0, "llm_latency_ms": 0, "token_usage": {},
    }
    base.update(over)
    return base


TOP2 = 'SELECT "productkey" FROM insights_mem_t1 ORDER BY "price" DESC LIMIT 2'


@pytest.mark.asyncio
async def test_binds_extracted_values_as_resolved_in_filter(snapshot_engine):
    snapshot_engine.script(TOP2, rows=[{"productkey": 1}, {"productkey": 2}])
    llm = _llm(json.dumps({"extract_sql": TOP2, "bind": {"table": "factsales", "column": "productkey"},
                           "reason": "top 2 by price"}))
    node = make_prior_data_binder(llm, PROMPTS, engine=snapshot_engine, max_values=100)
    out = await node(_state("c-bind"))
    binding = out["prior_bindings"][0]
    assert binding["target"] == "factsales.productkey" and binding["op"] == "in"
    assert binding["value"] == [1, 2] and binding["resolved"] is True and binding["ref"] == "T1"
    assert out["filter_plan"]["filters"][-1] is binding and out["resolved_filters"][-1] is binding
    assert out["llm_call_count"] == 1
    assert out["memory_telemetry"]["binder_values"] == 2
    assert snapshot_engine.calls == [TOP2]
    # The model saw schema + sample only, fenced, under the prefixed table name.
    prompt = llm.generate.await_args.kwargs["messages"][0]["content"]
    assert "table insights_mem_t1" in prompt and "<<<BEGIN_UNTRUSTED_DATA>>>" in prompt
    assert "factsales.productkey - Type: int" in prompt


def test_bound_filter_is_enforced_by_sqlglot_validate():
    binding = build_binding(handle="T1", table="factsales", column="productkey", values=[1, 2])
    validate = make_sqlglot_validate(enabled=True)
    base = {"known_tables": ["factsales"], "table_columns": {"factsales": ["productkey", "salesamount"]},
            "filter_plan": {"filters": [binding]}}
    ok = validate({**base, "generated_sql": "SELECT SUM(salesamount) FROM factsales WHERE productkey IN (1, 2)"})
    assert ok["sqlglot_error"] is None
    widened = validate({**base, "generated_sql": "SELECT SUM(salesamount) FROM factsales WHERE productkey IN (1, 2, 3)"})
    assert "did not preserve the verified filter" in widened["sqlglot_error"]
    dropped = validate({**base, "generated_sql": "SELECT SUM(salesamount) FROM factsales"})
    assert "did not preserve the verified filter" in dropped["sqlglot_error"]


def test_single_value_binds_as_equals():
    binding = build_binding(handle="T1", table="factsales", column="productkey", values=[7, 7])
    assert binding["op"] == "equals" and binding["value"] == 7


@pytest.mark.asyncio
async def test_too_many_values_asks_the_user_to_narrow(snapshot_engine):
    snapshot_engine.script('SELECT "productkey" FROM insights_mem_t1', rows=[{"productkey": i} for i in (1, 2, 3, 4)])
    llm = _llm('{"extract_sql": "SELECT \\"productkey\\" FROM insights_mem_t1", "bind": {"table": "factsales", "column": "productkey"}}')
    node = make_prior_data_binder(llm, PROMPTS, engine=snapshot_engine, max_values=2)
    out = await node(_state("c-cap"))
    assert out["filter_clarification_required"] is True
    assert "4 values" in out["clarification"] and "limit is 2" in out["clarification"]
    assert "prior_bindings" not in out


@pytest.mark.asyncio
async def test_unknown_bind_target_or_skip_leaves_composition_to_the_sql_model(snapshot_engine):
    snapshot_engine.script('SELECT "productkey" FROM insights_mem_t1 LIMIT 2', rows=[{"productkey": 1}, {"productkey": 2}])
    llm = _llm('{"extract_sql": "SELECT \\"productkey\\" FROM insights_mem_t1 LIMIT 2", "bind": {"table": "nope", "column": "x"}}')
    out = await make_prior_data_binder(llm, PROMPTS, engine=snapshot_engine)(_state("c-unknown"))
    assert "prior_bindings" not in out and out["llm_call_count"] == 1
    llm = _llm('{"skip": true, "reason": "only context"}')
    out = await make_prior_data_binder(llm, PROMPTS, engine=snapshot_engine)(_state("c-skip"))
    assert "prior_bindings" not in out


@pytest.mark.asyncio
async def test_extraction_outside_the_memory_tables_is_refused(snapshot_engine):
    llm = _llm('{"extract_sql": "SELECT id FROM insights_conversation_sessions", "bind": {"table": "factsales", "column": "productkey"}}')
    out = await make_prior_data_binder(llm, PROMPTS, engine=snapshot_engine)(_state("c-refused"))
    assert "prior_bindings" not in out   # validation refused it; nothing was bound


@pytest.mark.asyncio
async def test_no_refs_is_a_no_op_and_text_turns_are_not_queryable():
    llm = _llm("{}")
    assert await make_prior_data_binder(llm, PROMPTS)(_state("c-norefs", prior_refs=[])) == {}
    state = _state("c-text")
    state["conversation_history"] = [{"id": "qid-text", "natural_language_query": "hello", "result_kind": "text"}]
    out = await make_prior_data_binder(llm, PROMPTS)(state)
    assert "prior_bindings" not in out
    llm.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_database_the_binder_steps_aside():
    """Default engine (no metadata pool) → extraction cannot run → SQL model composes."""
    llm = _llm('{"extract_sql": "SELECT \\"productkey\\" FROM insights_mem_t1 LIMIT 2", "bind": {"table": "factsales", "column": "productkey"}}')
    out = await make_prior_data_binder(llm, PROMPTS)(_state("c-nodb"))
    assert "prior_bindings" not in out and out["llm_call_count"] == 1


@pytest.mark.asyncio
async def test_rows_missing_from_cheap_tiers_are_rerun_only_after_the_model_asks(snapshot_engine):
    from src.agent.prior_results import PriorResultStore

    rerun = AsyncMock(return_value={"columns": ["productkey", "price"],
                                    "rows": [{"productkey": 9, "price": 5}, {"productkey": 8, "price": 4}]})
    store = PriorResultStore(rerun=rerun)
    state = _state("c-rerun")
    state["conversation_history"] = [{**PRIOR, "id": "qid-not-cached"}]

    # Model declines → no source query is ever issued.
    llm = _llm('{"skip": true, "reason": "context only"}')
    out = await make_prior_data_binder(llm, PROMPTS, store=store, engine=snapshot_engine)(state)
    assert "prior_bindings" not in out and out["memory_telemetry"]["binder_data_sources"] == {"T1": "on_demand"}
    rerun.assert_not_awaited()
    prompt = llm.generate.await_args.kwargs["messages"][0]["content"]
    assert "loaded on demand" in prompt and "columns: productkey, product, price" in prompt

    # Model wants values → the prior SQL is re-run at the source, then extracted.
    top1 = 'SELECT "productkey" FROM insights_mem_t1 ORDER BY "price" DESC LIMIT 1'
    snapshot_engine.script(top1, rows=[{"productkey": 9}])
    llm = _llm(json.dumps({"extract_sql": top1, "bind": {"table": "factsales", "column": "productkey"}}))
    out = await make_prior_data_binder(llm, PROMPTS, store=store, engine=snapshot_engine)(state)
    rerun.assert_awaited_once()
    assert out["prior_bindings"][0]["value"] == 9 and out["prior_bindings"][0]["op"] == "equals"
    assert out["memory_telemetry"]["binder_data_sources"] == {"T1": "rerun"}


@pytest.mark.asyncio
async def test_reentry_reapplies_existing_bindings_without_llm():
    llm = _llm("{}")
    binding = build_binding(handle="T1", table="factsales", column="productkey", values=[1, 2])
    # filter_planner re-ran on a catalog refresh and produced a fresh plan.
    out = await make_prior_data_binder(llm, PROMPTS)(_state(
        "c-reentry", prior_bindings=[binding], filter_plan={"filters": [], "invalid_filters": []}, resolved_filters=[],
    ))
    assert out["filter_plan"]["filters"] == [binding] and out["resolved_filters"] == [binding]
    llm.generate.assert_not_awaited()
