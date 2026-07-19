"""Phase 5 tests: Tavily read tool, action policy, artifact fencing/hashing, and
the response-only continuation message shape (tools disabled)."""

from __future__ import annotations

import json

import pytest

from src.connectors.providers import get_provider


class TestTavilyProvider:
    def test_registered_as_api_key(self):
        t = get_provider("tavily")
        assert t is not None
        assert t.auth_kind == "api_key"
        assert t.allowed_origins == ("https://api.tavily.com",)

    def test_authorize_url_unsupported(self):
        t = get_provider("tavily")
        with pytest.raises(ValueError):
            t.authorize_url()


class TestTavilyPolicy:
    def test_policy_is_read_no_snapshot_no_grant(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("tavily-web-search", "web_search")
        assert p is not None
        assert p.auth_kind == "api_key"
        assert p.is_read is True
        assert p.requires_snapshot is False
        assert p.requires_grant is False
        assert p.required_scopes == ()

    def test_validator_requires_query_and_caps_results(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("tavily-web-search", "web_search")
        out = p.validate({"query": "what is duckdb", "max_results": 100}, {}, None)
        assert out["query"] == "what is duckdb"
        assert out["max_results"] == 10  # capped
        assert p.validate({"query": "x"}, {}, None)["max_results"] == 5  # default
        with pytest.raises(ActionPolicyError):
            p.validate({"query": ""}, {}, None)


class TestArtifactHelpers:
    def test_integrity_hash_deterministic(self):
        from src.connectors.tool_result_service import integrity_hash

        a = {"results": [{"url": "u", "content": "c"}], "query": "q"}
        b = {"query": "q", "results": [{"content": "c", "url": "u"}]}
        assert integrity_hash(a) == integrity_hash(b)  # canonicalized

    def test_fence_wraps_and_marks_untrusted(self):
        from src.connectors.tool_result_service import fence_tool_data

        fenced = fence_tool_data({"results": ["hello"]})
        assert "TOOL_DATA" in fenced
        assert "END_TOOL_DATA" in fenced
        assert "untrusted" in fenced.lower()
        assert "hello" in fenced

    def test_fence_truncates_large_payload(self):
        from src.connectors.tool_result_service import fence_tool_data

        big = {"content": "x" * 100000}
        fenced = fence_tool_data(big, max_bytes=500)
        assert "truncated" in fenced
        # The fenced string stays close to the cap (plus fence markers).
        assert len(fenced.encode("utf-8")) < 500 + 400


class TestContinuationMessages:
    def test_messages_disable_tools_and_fence_data(self):
        from src.agent.read_continuation import build_continuation_messages
        from src.connectors.tool_result_service import fence_tool_data

        fenced = fence_tool_data({"results": ["r1"]})
        msgs = build_continuation_messages("who won?", fenced)
        assert msgs[0]["role"] == "system"
        assert "no tools" in msgs[0]["content"].lower() or "tools are available" in msgs[0]["content"].lower()
        assert "untrusted" in msgs[0]["content"].lower()
        assert "who won?" in msgs[1]["content"]
        assert "TOOL_DATA" in msgs[1]["content"]
