"""Trino data-source runner."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Union

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorSyntaxError,
    QueryTimeout,
    SqlRunner,
)

# Schema Modeler writes ``authenticationMode`` on the shared connection row.
# These aliases all mean "present a client certificate instead of a password".
_MTLS_AUTH_MODES = frozenset({"mtls", "mutualtls", "mutual-tls", "certificate", "cert"})
# Same directory Schema Modeler mounts. A saved path outside it is refused so a
# connection row cannot point the API at an arbitrary readable file.
_MTLS_MOUNT_ENV = "TRINO_MTLS_MOUNT_PATH"
_DEFAULT_MTLS_MOUNT = "/mnt/trino-mtls"
_JWT_AUTH_MODES = frozenset({"jwt", "token"})
_BASIC_AUTH_MODES = frozenset({"basic", "password"})
_VERIFY_FALSE = frozenset({"false", "0", "no", "off", "f"})
_VERIFY_TRUE = frozenset({"true", "1", "yes", "on", "t"})


class TrinoSqlRunner(SqlRunner):
    """Trino runner backed by the synchronous ``trino`` DB-API client.

    The Trino Python client is synchronous, and DB-API connection/thread-safety
    varies by driver. To keep behavior deterministic, this runner opens a
    short-lived connection per operation inside a small dedicated executor
    instead of sharing a connection across worker threads.
    """

    database_type = "trino"
    sqlglot_dialect = "trino"
    # ``query_max_execution_time`` is a Trino session property enforced by the
    # coordinator, so the query is killed server-side, not just abandoned here.
    supports_server_side_timeout = True

    def __init__(
        self,
        *,
        source_key: Optional[str],
        host: str,
        port: int = 443,
        username: str,
        password: Optional[str] = None,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        http_scheme: str = "https",
        auth: Optional[str] = None,
        access_token: Optional[str] = None,
        client_cert_path: Optional[str] = None,
        client_key_path: Optional[str] = None,
        verify: Union[bool, str] = True,
        request_timeout: float = 30.0,
        max_workers: int = 4,
    ) -> None:
        self.source_key = source_key
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.catalog = catalog
        self.schema = schema
        self.http_scheme = (http_scheme or "https").strip().lower()
        self.auth = (auth or "").strip().lower()
        self.access_token = access_token
        self.client_cert_path = client_cert_path
        self.client_key_path = client_key_path
        self.verify = verify
        self.request_timeout = request_timeout
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"jeen-trino-{source_key or 'source'}",
        )

    async def initialize(self) -> None:
        # Connections are opened per query. This avoids sharing sync DB-API
        # connection objects across threads.
        return None

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _execute(
        self, sql: str, statement_timeout_ms: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        timeout = _timeout_seconds(statement_timeout_ms, self.request_timeout)
        session_properties = (
            {"query_max_execution_time": f"{max(1, int(round(timeout)))}s"}
            if statement_timeout_ms and statement_timeout_ms > 0 else None
        )
        try:
            return await asyncio.wait_for(
                self._run_blocking(
                    lambda cur: _fetch_rows(cur, sql), session_properties=session_properties
                ),
                # Give the coordinator a moment to enforce its own limit first.
                timeout=timeout + 1.0,
            )
        except asyncio.TimeoutError as exc:
            raise QueryTimeout("Trino query exceeded the configured timeout.") from exc

    async def list_tables(self) -> List[str]:
        if not self.schema:
            return []
        sql = (
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = {_sql_literal(self.schema)} "
            "AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_name"
        )
        try:
            _, rows = await self._run_blocking(lambda cur: _fetch_rows(cur, sql))
            return [str(row["table_name"]) for row in rows]
        except Exception:
            return []

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        schema, table = self._split_table_name(table_name)
        if not schema or not table:
            return []
        sql = (
            "SELECT column_name, data_type, is_nullable, NULL AS column_default "
            "FROM information_schema.columns "
            f"WHERE table_schema = {_sql_literal(schema)} "
            f"AND table_name = {_sql_literal(table)} "
            "ORDER BY ordinal_position"
        )
        try:
            _, rows = await self._run_blocking(lambda cur: _fetch_rows(cur, sql))
            return rows
        except Exception:
            return []

    async def _run_blocking(self, fn, session_properties: Optional[Dict[str, str]] = None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._with_cursor, fn, session_properties
        )

    def _with_cursor(self, fn, session_properties: Optional[Dict[str, str]] = None):
        try:
            conn = self._connect(session_properties)
            try:
                cur = conn.cursor()
                try:
                    return fn(cur)
                finally:
                    cur.close()
            finally:
                conn.close()
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _classify_trino_error(exc) from exc

    def _connect(self, session_properties: Optional[Dict[str, str]] = None):
        try:
            from trino import dbapi
            from trino.auth import (
                BasicAuthentication,
                CertificateAuthentication,
                JWTAuthentication,
            )
        except ImportError as exc:
            raise ConnectorConnectionError(
                "The 'trino' package is not installed."
            ) from exc

        kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.username,
            "catalog": self.catalog,
            "schema": self.schema,
            "http_scheme": self.http_scheme,
            "request_timeout": self.request_timeout,
            "session_properties": session_properties or None,
            "verify": self.verify,
        }
        if self.auth == "mtls":
            self._require_mtls_files()
            kwargs["auth"] = CertificateAuthentication(
                self.client_cert_path, self.client_key_path
            )
        elif self.auth == "jwt" or (self.access_token and self.auth != "basic"):
            kwargs["auth"] = JWTAuthentication(self.access_token or self.password or "")
        elif self.password:
            kwargs["auth"] = BasicAuthentication(self.username, self.password)
        return dbapi.connect(**{k: v for k, v in kwargs.items() if v is not None})

    def _require_mtls_files(self) -> None:
        if self.http_scheme != "https":
            raise ConnectorConnectionError("Trino mTLS requires https.")
        _require_readable_file(self.client_cert_path, "client certificate")
        _require_readable_file(self.client_key_path, "client private key")
        if isinstance(self.verify, str):
            _require_readable_file(self.verify, "CA certificate")

    def _split_table_name(self, table_name: str) -> tuple[Optional[str], str]:
        cleaned = (table_name or "").strip().strip('"')
        parts = [p.strip('"') for p in cleaned.split(".") if p.strip('"')]
        if len(parts) >= 3:
            return parts[-2], parts[-1]
        if len(parts) == 2:
            return parts[0], parts[1]
        return self.schema, parts[0] if parts else ""


def _fetch_rows(cur, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    cur.execute(sql)
    columns = [col[0] for col in (cur.description or [])]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return columns, rows


def _timeout_seconds(statement_timeout_ms: int, request_timeout: float) -> float:
    if statement_timeout_ms and statement_timeout_ms > 0:
        return max(1.0, statement_timeout_ms / 1000.0)
    return max(1.0, request_timeout)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def normalize_trino_auth(cfg: Dict[str, Any]) -> str:
    """Return ``basic``, ``jwt``, or ``mtls`` for a Schema Modeler connection row.

    An explicit ``authenticationMode`` wins. Otherwise a saved access token stays
    on JWT, and a certificate/key pair with no password is treated as mTLS.
    Password rows keep working when the mode field is absent.
    """
    raw = _first_str(
        cfg,
        "authenticationMode",
        "authentication_mode",
        "auth",
        "authType",
        "auth_type",
    )
    mode = (raw or "").strip().lower()
    if mode in _MTLS_AUTH_MODES:
        return "mtls"
    if mode in _JWT_AUTH_MODES:
        return "jwt"
    if mode in _BASIC_AUTH_MODES:
        return "basic"
    if _first_str(cfg, "accessToken", "access_token", "token"):
        return "jwt"
    cert = _first_str(cfg, "clientCertPath", "client_cert_path")
    key = _first_str(cfg, "clientKeyPath", "client_key_path")
    password = _first_str(cfg, "password")
    if cert and key and not password:
        return "mtls"
    return "basic"


def normalize_trino_verify(cfg: Dict[str, Any]) -> Union[bool, str]:
    """Server-certificate check passed to the Trino client.

    ``verify: false`` disables validation. A CA file path, either in
    ``caCertPath`` or in a legacy ``verify`` string, is passed through so
    ``requests`` trusts that bundle. Anything else keeps the default trust store.
    """
    ca = _first_str(cfg, "caCertPath", "ca_cert_path", "caCertificate")
    raw = cfg.get("verify")
    if isinstance(raw, str):
        token = raw.strip()
        lower = token.lower()
        if lower in _VERIFY_FALSE:
            return False
        if lower in _VERIFY_TRUE or token == "":
            return ca or True
        return token
    if raw is False or raw == 0:
        return False
    return ca or True


def trino_mtls_paths(cfg: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    return (
        _first_str(cfg, "clientCertPath", "client_cert_path"),
        _first_str(cfg, "clientKeyPath", "client_key_path"),
    )


def _first_str(cfg: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, str) and str(value).strip():
            return str(value).strip()
    return None


def mtls_mount_root() -> str:
    """Directory the API is allowed to read Trino client credentials from."""
    configured = os.environ.get(_MTLS_MOUNT_ENV) or _DEFAULT_MTLS_MOUNT
    return os.path.realpath(configured)


def _require_readable_file(path: Optional[str], label: str) -> None:
    if not path or not os.path.isabs(path):
        raise ConnectorConnectionError(f"Trino {label} must be an absolute file path.")
    root = mtls_mount_root()
    resolved = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ConnectorConnectionError(f"Trino {label} must be inside {root}.")
    if not os.path.isfile(resolved) or not os.access(resolved, os.R_OK):
        raise ConnectorConnectionError(f"Trino {label} is not a readable file: {path}")


def _classify_trino_error(exc: Exception) -> ConnectorError:
    msg = str(exc)
    lower = msg.lower()
    if "authentication" in lower or "unauthorized" in lower or "401" in lower:
        return ConnectorAuthError("Trino authentication failed.")
    if "access denied" in lower or "permission" in lower or "403" in lower:
        return ConnectorPermissionError(msg)
    if "timed out" in lower or "timeout" in lower:
        return QueryTimeout(msg)
    if (
        "certificate" in lower
        or "ssl" in lower
        or "tls" in lower
        or "connection" in lower
        or "host" in lower
        or "network" in lower
    ):
        return ConnectorConnectionError(msg)
    if "syntax" in lower or "mismatched input" in lower or "line " in lower:
        return ConnectorSyntaxError(msg)
    return ConnectorError(msg)
