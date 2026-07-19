"""Phase 4 tests: Slack + Jira providers and their server-owned action policies.

Live OAuth/execute paths hit external hosts (integration-only); here we test the
pure logic: authorize-URL construction, external-account binding + destination
allowlists, and the action-policy validators.
"""

from __future__ import annotations

import pytest

from src.connectors.providers import get_provider
from src.connectors.providers.base import TokenResult


class TestProviderRegistration:
    def test_slack_and_jira_registered(self):
        slack = get_provider("slack")
        jira = get_provider("jira")
        assert slack is not None and slack.auth_kind == "oauth"
        assert jira is not None and jira.auth_kind == "oauth"
        assert slack.allowed_origins == ("https://slack.com",)
        assert "https://api.atlassian.com" in jira.allowed_origins


class TestSlackProvider:
    def test_authorize_url_uses_user_scope(self):
        slack = get_provider("slack")
        url = slack.authorize_url(
            config={"client_id": "cid"},
            manifest={"scopes": ["chat:write"]},
            redirect_uri="https://app/cb",
            state="st",
            code_challenge="ch",
            nonce="n",
        )
        assert url.startswith("https://slack.com/oauth/v2/authorize?")
        assert "user_scope=chat%3Awrite" in url
        assert "client_id=cid" in url
        assert "code_challenge_method=S256" in url

    def test_to_result_maps_user_token(self):
        slack = get_provider("slack")
        tok = slack._to_result({
            "ok": True,
            "authed_user": {"id": "U1", "access_token": "xoxp-1", "scope": "chat:write"},
            "team": {"id": "T1", "name": "Acme"},
        })
        assert tok.access_token == "xoxp-1"
        assert tok.claims["team_id"] == "T1"

    def test_to_result_raises_on_error(self):
        slack = get_provider("slack")
        from src.connectors.oauth import OAuthError

        with pytest.raises(OAuthError):
            slack._to_result({"ok": False, "error": "invalid_code"})

    def test_validate_and_bind_enforces_workspace_allowlist(self):
        slack = get_provider("slack")
        tok = TokenResult(access_token="xoxp", claims={"team_id": "T1", "team_name": "Acme", "user_id": "U1"})
        # Matching workspace -> ok
        acct = slack.validate_and_bind(
            tok, config={"allowed_team_id": "T1"}, expected_nonce="", expected_tenant="", expected_object_id=""
        )
        assert acct["tenant_id"] == "T1"
        # Wrong workspace -> rejected
        with pytest.raises(ValueError):
            slack.validate_and_bind(
                tok, config={"allowed_team_id": "T2"}, expected_nonce="", expected_tenant="", expected_object_id=""
            )


class TestJiraProvider:
    def test_authorize_url_atlassian(self):
        jira = get_provider("jira")
        url = jira.authorize_url(
            config={"client_id": "jc"},
            manifest={"scopes": ["read:jira-work", "write:jira-work", "offline_access"]},
            redirect_uri="https://app/cb",
            state="st",
            code_challenge="ch",
            nonce="n",
        )
        assert url.startswith("https://auth.atlassian.com/authorize?")
        assert "audience=api.atlassian.com" in url
        assert "prompt=consent" in url
        assert "write%3Ajira-work" in url

    def test_validate_and_bind_requires_configured_cloud_id(self):
        jira = get_provider("jira")
        tok = TokenResult(
            access_token="at",
            claims={"cloud_ids": ["cid1"], "sites": [{"id": "cid1", "url": "https://x.atlassian.net"}]},
        )
        acct = jira.validate_and_bind(
            tok, config={"allowed_cloud_id": "cid1"}, expected_nonce="", expected_tenant="", expected_object_id=""
        )
        assert acct["tenant_id"] == "cid1"
        with pytest.raises(ValueError):
            jira.validate_and_bind(
                tok, config={"allowed_cloud_id": "other"}, expected_nonce="", expected_tenant="", expected_object_id=""
            )

    def test_cloud_id_required_for_execute_config(self):
        jira = get_provider("jira")
        with pytest.raises(ValueError):
            jira._cloud_id({})


class TestSlackJiraPolicies:
    def test_slack_policy_shape(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("slack-message", "post_message")
        assert p is not None
        assert p.auth_kind == "oauth"
        assert p.requires_snapshot is True
        assert p.required_scopes == ("chat:write",)

    def test_slack_channel_allowlist(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("slack-message", "post_message")
        # allowlisted channel ok
        out = p.validate({"channel": "#data", "note": "hi"}, {"allowed_channels": ["#data"]}, None)
        assert out["channel"] == "#data"
        # non-allowlisted rejected
        with pytest.raises(ActionPolicyError):
            p.validate({"channel": "#random"}, {"allowed_channels": ["#data"]}, None)
        # no allowlist -> any channel allowed
        assert p.validate({"channel": "#anything"}, {}, None)["channel"] == "#anything"

    def test_slack_requires_channel(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("slack-message", "post_message")
        with pytest.raises(ActionPolicyError):
            p.validate({"note": "no channel"}, {}, None)

    def test_jira_policy_allowlists(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("jira-issue", "create_issue")
        assert p.required_scopes == ("write:jira-work",)
        ok = p.validate(
            {"project_key": "abc", "issue_type": "Task", "summary": "s"},
            {"allowed_projects": ["ABC"], "allowed_issue_types": ["Task"]},
            None,
        )
        assert ok["project_key"] == "ABC"  # normalized upper
        # bad project
        with pytest.raises(ActionPolicyError):
            p.validate(
                {"project_key": "ZZZ", "issue_type": "Task", "summary": "s"},
                {"allowed_projects": ["ABC"]}, None,
            )
        # bad issue type
        with pytest.raises(ActionPolicyError):
            p.validate(
                {"project_key": "ABC", "issue_type": "Epic", "summary": "s"},
                {"allowed_issue_types": ["Task"]}, None,
            )

    def test_jira_requires_summary(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("jira-issue", "create_issue")
        with pytest.raises(ActionPolicyError):
            p.validate({"project_key": "ABC", "issue_type": "Task"}, {}, None)
