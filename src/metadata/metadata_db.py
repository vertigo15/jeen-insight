"""Async pool for the shared Jeen metadata database.

Mirrors `schema-modeler/lib/db/metadata-config.ts`. We use a singleton
asyncpg pool so the FastAPI app reuses connections across requests.
"""

from __future__ import annotations

import logging
import ssl as _ssl
from typing import Optional

import asyncpg

from src.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _build_ssl_context() -> Optional[_ssl.SSLContext]:
    """Build a permissive SSL context for Azure Postgres (CA bundle is implicit)."""
    if not settings.METADATA_DB_SSL:
        return None
    ctx = _ssl.create_default_context()
    # Azure Postgres uses publicly-trusted certs but we don't ship a CA bundle
    # in the container; mirror schema-modeler's `rejectUnauthorized: false`.
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


async def get_metadata_pool() -> asyncpg.Pool:
    """Return the singleton metadata pool, creating it on first call."""
    global _pool
    if _pool is None:
        ssl_ctx = _build_ssl_context()
        _pool = await asyncpg.create_pool(
            host=settings.METADATA_DB_HOST,
            port=settings.METADATA_DB_PORT,
            database=settings.METADATA_DB_NAME,
            user=settings.METADATA_DB_USER,
            password=settings.METADATA_DB_PASSWORD,
            ssl=ssl_ctx,
            min_size=2,
            max_size=10,
            command_timeout=30,
            # Every Insights object lives in `public` and is referenced
            # unqualified. The database is shared with other products that
            # keep same-named tables in their own schemas, so pin the search
            # path per session instead of trusting the role/database default.
            # (pg_catalog is always searched first implicitly; listing it
            # explicitly would make it the creation schema for unqualified DDL.)
            server_settings={"search_path": "public"},
        )
        logger.info(
            "✅ Metadata DB pool ready (%s:%s/%s)",
            settings.METADATA_DB_HOST,
            settings.METADATA_DB_PORT,
            settings.METADATA_DB_NAME,
        )
    return _pool


async def close_metadata_pool() -> None:
    """Close the singleton metadata pool (called on app shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("👋 Metadata DB pool closed")
