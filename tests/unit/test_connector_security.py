"""Security regression tests for the per-user connector platform.

These cover the fail-closed invariants introduced during the security hardening
pass. They are pure-logic tests (no DB / no network): every check exercises a
server-side decision that must hold regardless of what a browser or LLM sends.

Grouped by finding id:
  c2  — signing-secret strength + internal token integrity
  h1  — OAuth id_token claim validation (validate_and_bind)
  h2  — bounded group-membership freshness
  h3  — single-tenant isolation
  h4  — MCP bearer token fail-closed without a KEK
  m3  — outbound recipient policy (external / allowlist / syntax)
  m4  — keyed (HMAC) recipient redaction in the audit log
  m5  — strong-KEK enforcement + envelope encryption AEAD
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

# A real 32-byte key, base64-encoded (what production must supply).
_STRONG_KEY_B64 = base64.b64encode(b"\x11" * 32).decode("ascii")
_STRONG_KEY_B64_ALT = base64.b64encode(b"\x22" * 32).decode("ascii")


@pytest.fixture
def prod_mode(monkeypatch):
    """Force production posture: dev bypass off, no ambient secrets.

    JEEN_DEV_MODE now defaults to true (POC/portable), so hardened behaviour must
    be requested explicitly by setting it to false.
    """
    for var in (
        "INTERNAL_API_SECRET",
        "FLASK_SECRET_KEY",
        "AUTH_SECRET",
        "APP_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEEN_DEV_MODE", "false")
    yield


# ── c2: internal token integrity + secret strength ──────────────────────────

class TestInternalTokenIntegrity:
    def _configure(self, monkeypatch, secret: str = "s3cret-with-lots-of-entropy-xyz"):
        monkeypatch.delenv("JEEN_DEV_MODE", raising=False)
        monkeypatch.setenv("INTERNAL_API_SECRET", secret)

    def test_roundtrip_preserves_all_claims(self, monkeypatch):
        from src.security import internal_auth as ia

        self._configure(monkeypatch)
        token = ia.issue_internal_token(
            {
                "user_id": "42",
                "role": "admin",
                "name": "Ada",
                "email": "ada@corp.com",
                "tenant_id": "tenant-1",
                "object_id": "oid-1",
                "groups": ["g1", "g2"],
                "groups_complete": True,
                "auth_provider": "entra",
                "auth_time": 1_700_000_000,
            }
        )
        p = ia.verify_internal_token(token)
        assert p.user_id == "42"
        assert p.role == "admin"
        assert p.tenant_id == "tenant-1"
        assert p.object_id == "oid-1"
        assert p.groups == ("g1", "g2")
        assert p.auth_time == 1_700_000_000
        assert p.is_entra and p.is_admin

    def test_audience_mismatch_rejected(self, monkeypatch):
        from src.security import internal_auth as ia

        self._configure(monkeypatch)
        token = ia.issue_internal_token({"user_id": "1"}, audience="some-other-aud")
        with pytest.raises(ia.PrincipalError):
            ia.verify_internal_token(token, audience=ia.AUDIENCE_API)

    def test_tampered_token_rejected(self, monkeypatch):
        from src.security import internal_auth as ia

        self._configure(monkeypatch)
        token = ia.issue_internal_token({"user_id": "1"})
        tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
        with pytest.raises(ia.PrincipalError):
            ia.verify_internal_token(tampered)

    def test_default_subject_rejected(self, monkeypatch):
        from src.security import internal_auth as ia

        self._configure(monkeypatch)
        token = ia.issue_internal_token({"user_id": "default"})
        with pytest.raises(ia.PrincipalError):
            ia.verify_internal_token(token)

    def test_foreign_secret_cannot_forge(self, monkeypatch):
        from src.security import internal_auth as ia

        # Attacker signs with a *different* secret; our verifier must reject.
        self._configure(monkeypatch, secret="attacker-guessed-key-000000000000")
        forged = ia.issue_internal_token({"user_id": "1", "role": "admin"})
        self._configure(monkeypatch, secret="real-production-secret-aaaaaaaaaa")
        with pytest.raises(ia.PrincipalError):
            ia.verify_internal_token(forged)


class TestSecretStrength:
    def test_weak_fallback_rejected_in_prod(self, monkeypatch, prod_mode):
        from src.security import internal_auth as ia

        monkeypatch.setenv("FLASK_SECRET_KEY", "jeen-insights-dev-only-insecure-secret")
        with pytest.raises(ia.InternalAuthConfigError):
            ia.assert_configured()

    def test_missing_secret_rejected_in_prod(self, monkeypatch, prod_mode):
        from src.security import internal_auth as ia

        with pytest.raises(ia.InternalAuthConfigError):
            ia.assert_configured()

    def test_dev_mode_allows_fallback(self, monkeypatch, prod_mode):
        from src.security import internal_auth as ia

        monkeypatch.setenv("JEEN_DEV_MODE", "true")
        # No secret configured, but dev mode tolerates the insecure dev key.
        ia.assert_configured()
        secrets = ia._load_secrets()
        assert secrets and secrets[0][1] == ia._DEV_SECRET

    def test_strong_secret_accepted_in_prod(self, monkeypatch, prod_mode):
        from src.security import internal_auth as ia

        monkeypatch.setenv("INTERNAL_API_SECRET", "a-strong-random-48char-value-000000000000000000")
        ia.assert_configured()


# ── m5: strong KEK + envelope AEAD ──────────────────────────────────────────

class TestCryptoKekStrength:
    def test_decode_strong_key_accepts_base64_32(self):
        from src.security import crypto

        assert crypto._decode_strong_key(_STRONG_KEY_B64) == b"\x11" * 32

    def test_decode_strong_key_accepts_hex_32(self):
        from src.security import crypto

        assert crypto._decode_strong_key(("aa" * 32)) == b"\xaa" * 32

    def test_decode_strong_key_rejects_passphrase(self):
        from src.security import crypto

        assert crypto._decode_strong_key("hunter2") is None
        assert crypto._decode_strong_key("short") is None

    def test_crypto_unavailable_for_weak_key_in_prod(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", "weak-passphrase")
        assert crypto.crypto_available() is False

    def test_crypto_available_for_strong_key(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        assert crypto.crypto_available() is True

    def test_assert_kek_valid_raises_for_weak_in_prod(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", "weak-passphrase")
        with pytest.raises(crypto.CryptoError):
            crypto.assert_kek_valid()

    def test_assert_kek_valid_ok_when_unset(self, monkeypatch, prod_mode):
        from src.security import crypto

        # Unset key = feature disabled/fail-closed elsewhere, but startup is fine.
        crypto.assert_kek_valid()

    def test_dev_mode_derives_key_from_passphrase(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("JEEN_DEV_MODE", "true")
        monkeypatch.setenv("APP_ENCRYPTION_KEY", "weak-passphrase")
        assert crypto.crypto_available() is True


class TestEnvelopeEncryption:
    def test_roundtrip_with_aad(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        blob = crypto.encrypt("top-secret-token", aad="mcp_server:7:bearer")
        assert "top-secret-token" not in blob.ciphertext  # never plaintext at rest
        assert crypto.decrypt(blob, aad="mcp_server:7:bearer") == "top-secret-token"

    def test_wrong_aad_fails(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        blob = crypto.encrypt("x", aad="mcp_server:7:bearer")
        with pytest.raises(crypto.CryptoError):
            crypto.decrypt(blob, aad="mcp_server:8:bearer")  # cannot move rows

    def test_tampered_ciphertext_fails(self, monkeypatch, prod_mode):
        from src.security import crypto

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        blob = crypto.encrypt("x", aad="a")
        bad = crypto.EncryptedBlob(
            algo=blob.algo,
            kek_id=blob.kek_id,
            ciphertext=base64.b64encode(b"\x00" * 32).decode(),
            nonce=blob.nonce,
            wrapped_dek=blob.wrapped_dek,
            dek_nonce=blob.dek_nonce,
        )
        with pytest.raises(crypto.CryptoError):
            crypto.decrypt(bad, aad="a")

    def test_encrypt_without_kek_fails_closed(self, monkeypatch, prod_mode):
        from src.security import crypto

        with pytest.raises(crypto.CryptoError):
            crypto.encrypt("x", aad="a")


# ── h4: MCP bearer token fail-closed ────────────────────────────────────────

class TestMcpTokenFailClosed:
    def test_token_without_kek_raises(self, monkeypatch, prod_mode):
        from src.metadata import mcp_server_service as svc

        with pytest.raises(svc.McpTokenError):
            svc._require_kek_for_token("a-bearer-token")

    def test_token_with_kek_ok(self, monkeypatch, prod_mode):
        from src.metadata import mcp_server_service as svc

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        svc._require_kek_for_token("a-bearer-token")  # no raise

    def test_no_token_is_always_ok(self, monkeypatch, prod_mode):
        from src.metadata import mcp_server_service as svc

        svc._require_kek_for_token(None)
        svc._require_kek_for_token("")

    def test_dev_mode_allows_plaintext_token(self, monkeypatch):
        # POC/portable default: a token without a KEK is allowed (stored plaintext)
        # so the shared DB stays readable by every copy of the app.
        from src.metadata import mcp_server_service as svc

        monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("JEEN_DEV_MODE", "true")
        svc._require_kek_for_token("a-bearer-token")  # no raise

    def test_dev_mode_is_the_default(self, monkeypatch):
        # With nothing configured at all, we default to the portable POC posture.
        from src.metadata import mcp_server_service as svc

        monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("JEEN_DEV_MODE", raising=False)
        assert svc._dev_mode() is True
        svc._require_kek_for_token("a-bearer-token")  # no raise


# ── h3: single-tenant isolation ─────────────────────────────────────────────

class TestTenantIsolation:
    def _principal(self, tenant_id: str):
        from src.security.internal_auth import Principal

        return Principal(user_id="1", tenant_id=tenant_id, object_id="oid-1")

    def test_foreign_tenant_rejected(self, monkeypatch):
        from src.api import dependencies as deps

        monkeypatch.setattr(deps, "_configured_tenant", lambda: "home-tenant")
        with pytest.raises(HTTPException) as exc:
            deps.resolve_tenant_id(self._principal("foreign-tenant"))
        assert exc.value.status_code == 403

    def test_matching_tenant_ok(self, monkeypatch):
        from src.api import dependencies as deps

        monkeypatch.setattr(deps, "_configured_tenant", lambda: "home-tenant")
        assert deps.resolve_tenant_id(self._principal("home-tenant")) == "home-tenant"

    def test_empty_principal_tenant_falls_back_to_configured(self, monkeypatch):
        from src.api import dependencies as deps

        monkeypatch.setattr(deps, "_configured_tenant", lambda: "home-tenant")
        assert deps.resolve_tenant_id(self._principal("")) == "home-tenant"


# ── h2: bounded group-membership freshness ──────────────────────────────────

class TestMembershipFreshness:
    def _svc(self):
        from src.connectors.identity_service import IdentityService

        return IdentityService(pool=None)  # _is_fresh does not touch the pool

    def _sync(self, *, age_seconds: int, complete: bool = True):
        return {
            "synced_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
            "complete": complete,
            "source": "token",
        }

    def test_recent_complete_is_fresh(self, monkeypatch):
        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "900")
        assert self._svc()._is_fresh(self._sync(age_seconds=10)) is True

    def test_stale_is_not_fresh(self, monkeypatch):
        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "900")
        # older than TTL -> access must be revoked
        assert self._svc()._is_fresh(self._sync(age_seconds=1000)) is False

    def test_incomplete_is_not_fresh(self, monkeypatch):
        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "900")
        assert self._svc()._is_fresh(self._sync(age_seconds=10, complete=False)) is False

    def test_missing_sync_is_not_fresh(self):
        assert self._svc()._is_fresh(None) is False

    def test_ttl_is_configurable(self, monkeypatch):
        from src.connectors import identity_service as isvc

        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "120")
        assert isvc._membership_ttl_seconds() == 120

    def test_ttl_invalid_falls_back_to_default(self, monkeypatch):
        from src.connectors import identity_service as isvc

        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "not-a-number")
        assert isvc._membership_ttl_seconds() == isvc.MEMBERSHIP_MAX_AGE_SECONDS

    def test_ttl_floor_is_enforced(self, monkeypatch):
        from src.connectors import identity_service as isvc

        monkeypatch.setenv("CONNECTOR_GROUP_MEMBERSHIP_TTL_SECONDS", "1")
        assert isvc._membership_ttl_seconds() == 60


# ── h1: OAuth id_token claim validation ─────────────────────────────────────

class TestValidateAndBind:
    CLIENT_ID = "client-abc"
    TENANT = "tenant-1"
    OID = "user-oid-1"
    NONCE = "nonce-xyz"

    def _config(self):
        return {"client_id": self.CLIENT_ID, "tenant_id": self.TENANT}

    def _token(self, **overrides):
        from src.connectors.providers.base import TokenResult

        claims = {
            "aud": self.CLIENT_ID,
            "tid": self.TENANT,
            "iss": f"https://login.microsoftonline.com/{self.TENANT}/v2.0",
            "nonce": self.NONCE,
            "exp": time.time() + 3600,
            "oid": self.OID,
            "preferred_username": "user@corp.com",
        }
        claims.update(overrides.get("claims", {}))
        return TokenResult(
            access_token="at",
            id_token=overrides.get("id_token", "raw.jwt.value"),
            claims=claims,
        )

    def _adapter(self):
        from src.connectors.providers.graph_mail import GraphMailAdapter

        return GraphMailAdapter()

    def _bind(self, token):
        return self._adapter().validate_and_bind(
            token,
            config=self._config(),
            expected_nonce=self.NONCE,
            expected_tenant=self.TENANT,
            expected_object_id=self.OID,
        )

    def test_valid_token_binds(self):
        bound = self._bind(self._token())
        assert bound["tenant_id"] == self.TENANT
        assert bound["object_id"] == self.OID
        assert bound["upn"] == "user@corp.com"

    def test_missing_id_token_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(id_token=""))

    def test_audience_mismatch_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"aud": "someone-else"}))

    def test_tenant_mismatch_rejected(self):
        tok = self._token(
            claims={
                "tid": "other-tenant",
                "iss": "https://login.microsoftonline.com/other-tenant/v2.0",
            }
        )
        with pytest.raises(ValueError):
            self._bind(tok)

    def test_issuer_mismatch_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"iss": "https://evil.example.com/x/v2.0"}))

    def test_nonce_mismatch_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"nonce": "different-nonce"}))

    def test_expired_token_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"exp": time.time() - 3600}))

    def test_object_id_mismatch_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"oid": "attacker-oid"}))

    def test_missing_object_id_rejected(self):
        with pytest.raises(ValueError):
            self._bind(self._token(claims={"oid": ""}))


# ── m3: recipient policy ────────────────────────────────────────────────────

class TestRecipientPolicy:
    def test_internal_only_rejects_external(self):
        from src.connectors.recipients import validate_recipients

        res = validate_recipients(
            ["a@corp.com", "b@external.com"], sender_domain="corp.com"
        )
        assert res.valid == ["a@corp.com"]
        assert res.rejected == ["b@external.com"]
        assert res.ok is False  # a rejected recipient blocks the batch

    def test_allow_external_flags_but_permits(self):
        from src.connectors.recipients import validate_recipients

        res = validate_recipients(
            ["a@corp.com", "b@external.com"],
            sender_domain="corp.com",
            allow_external=True,
        )
        assert set(res.valid) == {"a@corp.com", "b@external.com"}
        assert res.external == ["b@external.com"]

    def test_allowlist_permits_named_domains(self):
        from src.connectors.recipients import validate_recipients

        res = validate_recipients(
            ["a@corp.com", "b@partner.com", "c@evil.com"],
            sender_domain="corp.com",
            allowlist=["partner.com"],
        )
        assert set(res.valid) == {"a@corp.com", "b@partner.com"}
        assert res.rejected == ["c@evil.com"]

    def test_invalid_syntax_flagged(self):
        from src.connectors.recipients import validate_recipients

        res = validate_recipients(
            ["not-an-email", "also bad@x", "@nolocal.com"], sender_domain="corp.com"
        )
        assert "not-an-email" in res.invalid
        assert res.ok is False

    def test_dedup_and_over_limit(self):
        from src.connectors.recipients import MAX_RECIPIENTS, validate_recipients

        raw = ["dup@corp.com", "dup@corp.com"] + [
            f"u{i}@corp.com" for i in range(MAX_RECIPIENTS)
        ]
        res = validate_recipients(raw, sender_domain="corp.com")
        # Duplicates collapse; anything beyond the cap is surfaced as invalid.
        assert res.valid.count("dup@corp.com") == 1
        assert len(res.invalid) >= 1


# ── p1: typed server-owned action policy ────────────────────────────────────

class TestActionPolicy:
    def test_unknown_action_fails_closed(self):
        from src.connectors.action_policy import get_action_policy

        assert get_action_policy("microsoft-graph-mail", "delete_everything") is None
        assert get_action_policy("some-other-connector", "send_email") is None

    def test_send_email_policy_shape(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        assert p is not None
        # These authorization facts come from the server, never a manifest/LLM arg.
        assert p.auth_kind == "oauth"
        assert p.requires_snapshot is True
        assert p.requires_grant is True

    def _grant(self, sender="me@corp.com"):
        return {"external_account": sender, "status": "active"}

    def test_validator_requires_subject(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        with pytest.raises(ActionPolicyError):
            p.validate({"recipients": ["a@corp.com"], "subject": " "}, {}, self._grant())

    def test_validator_requires_recipients(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        with pytest.raises(ActionPolicyError):
            p.validate({"recipients": [], "subject": "Hi"}, {}, self._grant())

    def test_validator_rejects_external_by_default(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        with pytest.raises(ActionPolicyError):
            p.validate(
                {"recipients": ["a@corp.com", "b@external.com"], "subject": "Hi"},
                {},
                self._grant(),
            )

    def test_validator_allows_external_when_config_permits(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        out = p.validate(
            {"recipients": ["a@corp.com", "b@external.com"], "subject": "Hi", "note": "n"},
            {"allow_external_recipients": True},
            self._grant(),
        )
        assert set(out["recipients"]) == {"a@corp.com", "b@external.com"}
        assert out["_external"] == ["b@external.com"]
        assert out["subject"] == "Hi"

    def test_validator_allowlist_permits_named_domain(self):
        from src.connectors.action_policy import get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        out = p.validate(
            {"recipients": ["a@corp.com", "b@partner.com"], "subject": "Hi"},
            {"recipient_domain_allowlist": ["partner.com"]},
            self._grant(),
        )
        assert set(out["recipients"]) == {"a@corp.com", "b@partner.com"}

    def test_validator_rejects_invalid_syntax(self):
        from src.connectors.action_policy import ActionPolicyError, get_action_policy

        p = get_action_policy("microsoft-graph-mail", "send_email")
        with pytest.raises(ActionPolicyError):
            p.validate({"recipients": ["not-an-email"], "subject": "Hi"}, {}, self._grant())


# ── p1: approval hash binds the exact approved payload + immutable context ───

class TestApprovalHash:
    def _args(self, **over):
        base = dict(
            action="send_email",
            connector_id="c1",
            connector_version_id="v1",
            snapshot_id="s1",
            snapshot_hash="deadbeef",
            params={"recipients": ["a@corp.com"], "subject": "Hi"},
        )
        base.update(over)
        return base

    def test_deterministic(self):
        from src.connectors.action_gate import _approval_hash

        assert _approval_hash(**self._args()) == _approval_hash(**self._args())

    def test_internal_keys_do_not_change_hash(self):
        from src.connectors.action_gate import _approval_hash

        base = _approval_hash(**self._args())
        withint = _approval_hash(
            **self._args(params={"recipients": ["a@corp.com"], "subject": "Hi", "_external": ["x@y.com"]})
        )
        assert base == withint  # underscore-prefixed keys are excluded

    def test_params_change_hash(self):
        from src.connectors.action_gate import _approval_hash

        a = _approval_hash(**self._args())
        b = _approval_hash(**self._args(params={"recipients": ["a@corp.com"], "subject": "Bye"}))
        assert a != b

    def test_version_and_snapshot_change_hash(self):
        from src.connectors.action_gate import _approval_hash

        base = _approval_hash(**self._args())
        assert base != _approval_hash(**self._args(connector_version_id="v2"))
        assert base != _approval_hash(**self._args(snapshot_hash="cafebabe"))


# ── p1: agent-tools master switch enforced inside the gate + fail-closed ─────

class _StubRegistry:
    async def get_connector(self, connector_id):
        return None  # forces a 404 once the flag check has passed


class TestAgentToolsGate:
    def _gate(self):
        from src.connectors.action_gate import ActionGate

        return ActionGate(
            pool=None, registry=_StubRegistry(), grants=None,
            snapshots=None, identities=None, audit=None,
        )

    async def test_agent_origin_blocked_when_flag_off(self, monkeypatch):
        from src.connectors import action_gate as ag

        async def _off(*a, **k):
            return False

        monkeypatch.setattr(ag, "get_connectors_enabled", _off)
        monkeypatch.setattr(ag, "get_agent_tools_enabled", _off)

        with pytest.raises(ag.ActionError) as exc:
            await self._gate().propose(
                owner_user_id="1", identity_id="i1", connector_id="c1",
                action="send_email", snapshot_id="s1", origin="agent",
            )
        assert exc.value.status_code == 403

    async def test_user_origin_skips_agent_flag(self, monkeypatch):
        from src.connectors import action_gate as ag

        async def _off(*a, **k):
            return False

        # Even with both switches off, a USER-origin proposal is not blocked by the
        # agent-tools gate here; it proceeds and fails later (stub connector -> 404).
        monkeypatch.setattr(ag, "get_connectors_enabled", _off)
        monkeypatch.setattr(ag, "get_agent_tools_enabled", _off)

        with pytest.raises(ag.ActionError) as exc:
            await self._gate().propose(
                owner_user_id="1", identity_id="i1", connector_id="c1",
                action="send_email", snapshot_id="s1", origin="user",
            )
        assert exc.value.status_code == 404  # got past the flag gate

    async def test_agent_tools_flag_fails_closed_without_pool(self, monkeypatch):
        import src.metadata as metadata
        from src.security import app_flags

        app_flags.invalidate_cache()

        async def _boom():
            raise RuntimeError("no pool")

        monkeypatch.setattr(metadata, "get_metadata_pool", _boom)
        assert await app_flags.get_agent_tools_enabled(use_cache=False) is False


# ── m4: keyed recipient redaction in the audit log ──────────────────────────

class TestAuditRedaction:
    def test_domain_only_without_key(self, monkeypatch, prod_mode):
        from src.connectors.audit_service import redact_recipient

        red = redact_recipient("alice@example.com")
        assert red == {"domain": "example.com"}  # no hash when no key configured

    def test_keyed_hash_present_with_key(self, monkeypatch, prod_mode):
        from src.connectors.audit_service import redact_recipient

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        red = redact_recipient("alice@example.com")
        assert red["domain"] == "example.com"
        assert len(red["hash"]) == 16
        # The local part must never appear in the redacted record.
        assert "alice" not in str(red)

    def test_hash_is_keyed_not_plain_sha(self, monkeypatch, prod_mode):
        from src.connectors.audit_service import redact_recipient

        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64)
        h1 = redact_recipient("alice@example.com")["hash"]
        monkeypatch.setenv("APP_ENCRYPTION_KEY", _STRONG_KEY_B64_ALT)
        h2 = redact_recipient("alice@example.com")["hash"]
        # Same address, different server key -> different digest (proves keying).
        assert h1 != h2
