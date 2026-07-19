"""Phase 2 security tests: server-owned egress (SSRF) + per-action scope checks.

Pure-logic: the origin allowlist / HTTPS / private-IP checks are exercised without
opening real sockets by stubbing DNS resolution.
"""

from __future__ import annotations

import pytest


def _stub_dns(monkeypatch, ip: str):
    from src.connectors import egress

    def _getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", _getaddrinfo)


class TestEgressAllowlist:
    def test_rejects_non_https(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "93.184.216.34")
        with pytest.raises(egress.EgressError):
            egress._assert_allowed("http://graph.microsoft.com/x", ["https://graph.microsoft.com"])

    def test_rejects_origin_not_in_allowlist(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "93.184.216.34")
        with pytest.raises(egress.EgressError):
            egress._assert_allowed("https://evil.example.com/x", ["https://graph.microsoft.com"])

    def test_allows_listed_origin_public_ip(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "93.184.216.34")
        # No raise.
        egress._assert_allowed(
            "https://graph.microsoft.com/v1.0/me/sendMail", ["https://graph.microsoft.com"]
        )

    def test_rejects_private_ip_even_when_allowlisted(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "127.0.0.1")
        with pytest.raises(egress.EgressError):
            egress._assert_allowed(
                "https://graph.microsoft.com/x", ["https://graph.microsoft.com"]
            )

    def test_rejects_link_local_metadata_ip(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "169.254.169.254")  # cloud metadata endpoint
        with pytest.raises(egress.EgressError):
            egress._assert_allowed(
                "https://graph.microsoft.com/x", ["https://graph.microsoft.com"]
            )


class TestScopeCheck:
    def test_no_required_scopes_always_ok(self):
        from src.connectors.action_policy import scopes_satisfied

        assert scopes_satisfied((), None) is True
        assert scopes_satisfied((), "anything") is True

    def test_missing_grant_scopes_cannot_verify_allows(self):
        from src.connectors.action_policy import scopes_satisfied

        assert scopes_satisfied(("Mail.Send",), None) is True

    def test_exact_scope_present(self):
        from src.connectors.action_policy import scopes_satisfied

        assert scopes_satisfied(("Mail.Send",), "openid Mail.Send offline_access") is True

    def test_fully_qualified_scope_matches_short_name(self):
        from src.connectors.action_policy import scopes_satisfied

        granted = "https://graph.microsoft.com/Mail.Send offline_access"
        assert scopes_satisfied(("Mail.Send",), granted) is True

    def test_missing_required_scope_fails(self):
        from src.connectors.action_policy import scopes_satisfied

        assert scopes_satisfied(("Mail.Send",), "openid profile offline_access") is False


class TestSendEmailPolicyScopes:
    def test_send_email_requires_mail_send(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        assert p.required_scopes == ("Mail.Send",)
        assert p.auth_kind == "oauth"
