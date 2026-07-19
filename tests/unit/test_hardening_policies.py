import pytest

from src.agent.langgraph_agent.graph import _route_from_eval
from src.agent.langgraph_agent.nodes.catalog import make_catalog_lookup
from src.agent.langgraph_agent.nodes.feedback import make_feedback_classifier
from src.agent.langgraph_agent.nodes.validation import make_dlp_check
from src.connectors.databricks import DatabricksSqlRunner
from src.connectors.trino import TrinoSqlRunner


def test_column_entitlement_blocks_unapproved_role_before_execution():
    check = make_dlp_check(True)

    outcome = check(
        {
            "generated_sql": "SELECT salary FROM employees",
            "database_type": "postgres",
            "table_columns": {"employees": ["id", "salary"]},
            "column_entitlements": {
                "salary": {
                    "classification": "confidential",
                    "allowed_roles": ["admin"],
                }
            },
            "user_context": {"user_role": "viewer"},
        }
    )

    assert outcome["dlp_blocked"] is True
    assert "entitlement" in outcome["governance_error"]


def test_column_entitlement_allows_approved_role():
    check = make_dlp_check(True)

    outcome = check(
        {
            "generated_sql": "SELECT salary FROM employees",
            "database_type": "postgres",
            "table_columns": {"employees": ["id", "salary"]},
            "column_entitlements": {
                "salary": {
                    "classification": "confidential",
                    "allowed_roles": ["admin"],
                }
            },
            "user_context": {"user_role": "admin"},
        }
    )

    assert outcome == {"dlp_blocked": False, "governance_error": None}


def test_column_entitlement_remains_enforced_when_keyword_dlp_is_disabled():
    check = make_dlp_check(False)
    outcome = check(
        {
            "generated_sql": "SELECT salary FROM employees",
            "database_type": "postgres",
            "table_columns": {"employees": ["id", "salary"]},
            "column_entitlements": {
                "salary": {
                    "classification": "confidential",
                    "allowed_roles": ["admin"],
                }
            },
            "user_context": {"user_role": "viewer"},
        }
    )
    assert outcome["dlp_blocked"] is True
    assert "entitlement" in outcome["governance_error"]


def test_star_query_expands_hidden_governed_columns_for_entitlement_check():
    check = make_dlp_check(True)

    outcome = check(
        {
            "generated_sql": "SELECT * FROM employees",
            "database_type": "postgres",
            # The prompt/catalog deliberately hides salary.
            "table_columns": {"employees": ["id", "name"]},
            "governed_columns_by_table": {"employees": ["salary"]},
            "column_entitlements": {
                "salary": {
                    "classification": "confidential",
                    "allowed_roles": ["admin"],
                }
            },
            "user_context": {"user_role": "viewer"},
        }
    )

    assert outcome["dlp_blocked"] is True
    assert "salary" in outcome["governance_error"]


def test_raw_dlp_fallback_still_enforces_column_entitlements():
    check = make_dlp_check(True)
    outcome = check(
        {
            "generated_sql": "SELEC salary FROM employees",
            "database_type": "postgres",
            "table_columns": {},
            "column_entitlements": {
                "salary": {
                    "classification": "confidential",
                    "allowed_roles": ["admin"],
                }
            },
            "user_context": {"user_role": "viewer"},
        }
    )
    assert outcome["dlp_blocked"] is True
    assert "entitlement" in outcome["governance_error"]


def test_semantic_mismatch_never_automatically_reexecutes_sql():
    assert _route_from_eval({"eval_result": {"answers_intent": False}}) == "response_formatter"


class _MetadataLoader:
    def __init__(self):
        self.load_all_calls = 0

    async def load_all(self, _source_key):
        self.load_all_calls += 1
        raise AssertionError("fresh preload must be reused")

    async def load_column_entitlements(self, _source_key):
        return {}


@pytest.mark.asyncio
async def test_catalog_lookup_reuses_fresh_preloaded_bundle():
    loader = _MetadataLoader()
    lookup = make_catalog_lookup(loader)
    bundle = {
        "tables": "- employees",
        "columns": "- employees.id - Type: integer",
    }

    outcome = await lookup(
        {
            "source_key": "source",
            "metadata_bundle": bundle,
            "catalog_preloaded": True,
        }
    )

    assert loader.load_all_calls == 0
    assert outcome["metadata_bundle"] == bundle
    assert outcome["catalog_cache"] == "hit"


def test_schema_link_miss_widens_once_only_when_prompt_was_pruned():
    classifier = make_feedback_classifier(max_retries=3)
    state = {
        "retry_count": 0,
        "sqlglot_error": "Table 'orders' not found in catalog.",
        "structured_prompt": {"schema_pruned": True},
        "schema_link_widened": False,
    }
    first = classifier(state)
    assert first["schema_link_widened"] is True

    state.update(first)
    second = classifier(state)
    assert "schema_link_widened" not in second


class _CancelableCursor:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner",
    [
        DatabricksSqlRunner(
            source_key="source", host="warehouse.example", http_path="/sql/1", access_token="token"
        ),
        TrinoSqlRunner(source_key="source", host="trino.example", username="user"),
    ],
)
async def test_warehouse_timeout_adapter_requests_native_cursor_cancellation(runner):
    cursor = _CancelableCursor()
    unrelated_cursor = _CancelableCursor()

    await runner._cancel_cursor(cursor)
    await runner.close()

    assert cursor.cancelled is True
    assert unrelated_cursor.cancelled is False
