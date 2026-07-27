"""Phase 2 security tests: server-owned egress (SSRF) + per-action scope checks.

Pure-logic: the origin allowlist / HTTPS / private-IP checks are exercised without
opening real sockets by stubbing DNS resolution.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest


def _stub_dns(monkeypatch, ip: str):
    from src.connectors import egress

    def _getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", _getaddrinfo)


class TestEgressBuffering:
    """Regression: a gzip/deflate-encoded response must round-trip through the
    stream-buffer-and-rebuild path. ``aiter_bytes()`` already decodes the body,
    so the rebuilt ``httpx.Response`` must NOT keep the ``Content-Encoding``
    header — otherwise the constructor's eager ``read()`` re-runs the gzip
    decoder over already-decompressed bytes and raises ``DecodingError``
    ("incorrect header check"). This is exactly what Power BI's executeQueries
    (which always gzips) hit."""

    @pytest.mark.asyncio
    async def test_gzip_response_roundtrips(self, monkeypatch):
        from src.connectors import egress

        _stub_dns(monkeypatch, "93.184.216.34")
        payload = {"results": [{"tables": [{"rows": [{"[probe]": 1}]}]}]}
        gz = gzip.compress(json.dumps(payload).encode())

        def _handler(request: httpx.Request) -> httpx.Response:
            # Simulate a real wire response: raw gzip body + Content-Encoding.
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
                content=gz,
                request=request,
            )

        real_client = httpx.AsyncClient

        def _client_with_mock(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(egress.httpx, "AsyncClient", _client_with_mock)

        resp = await egress.request(
            "POST",
            "https://api.powerbi.com/x/executeQueries",
            allowed_origins=("https://api.powerbi.com",),
            json={"queries": []},
        )
        # The bug raised DecodingError before we reached here; assert the body is
        # readable and the stale content-coding header was stripped.
        assert resp.status_code == 200
        assert resp.json() == payload
        assert "content-encoding" not in {k.lower() for k in resp.headers}


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
