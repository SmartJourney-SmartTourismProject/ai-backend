"""
Shared asyncpg connection pool for the async request path (db_tool.py,
calendar_tool.py).

Fails open, like app/utils/cache.py: if DATABASE_URL isn't configured, or the
database can't be reached, get_pool() returns None and callers fall back to
their mock data / local-file paths rather than raising. The AI backend must
stay runnable with no database at all - that's what keeps the test suite
network-free and lets someone run this service from a fresh clone.

The batch ingest scripts do NOT use this - they run outside the event loop
via APScheduler and use psycopg2 instead (see app/data/postgres_writer.py).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)

try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False
    asyncpg = None

_pool = None


async def get_pool():
    """
    Lazily create and cache one pool per process. Returns None when the
    database is unavailable for any reason.

    Deliberately does NOT latch the failure permanently: a transient outage
    shouldn't silently pin this service to mock data for the rest of its
    life. The 5s connect timeout bounds the cost of retrying while the
    database is genuinely down, and recovery is automatic once it's back.
    """
    global _pool

    if not _ASYNCPG_AVAILABLE:
        return None
    if not settings.database_url:
        # Not configured at all - expected on a fresh clone. Debug, not a
        # warning, so it doesn't cry wolf during normal standalone use.
        logger.debug("DATABASE_URL not set; using mock/local fallbacks.")
        return None

    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                settings.database_url, min_size=1, max_size=5, timeout=5,
            )
        except Exception as e:
            # Configured but unreachable - that IS worth a warning, since it
            # otherwise looks identical to "no database configured" while
            # quietly serving mock data.
            logger.warning(f"DATABASE_URL is set but the database is unreachable: {e}")
            return None

    return _pool


async def close_pool() -> None:
    """Close the pool on app shutdown. Safe to call when none was created."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
