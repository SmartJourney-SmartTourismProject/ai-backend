"""
Shared asyncpg connection pool for the async request path (db_tool.py,
calendar_tool.py).

get_pool() itself still returns None (not an exception) when DATABASE_URL
isn't configured or the database can't be reached - that's what keeps
importing this module safe with no database at all (a fresh clone, the
test suite, etc). What changed (decision D5, docs/master_plan/DATA_PLATFORM.md
§9): callers no longer fall back to mock data on a None pool. db_tool.py's
_require_pool() turns None into a raised DataUnavailable - the database
being unreachable is now a real, surfaced failure, not a silent substitution
with placeholder data. calendar_tool.py still has its own local-file
fallback (a smaller, still-open item - see PROJECT_MASTER_PLAN.md §5 Phase 7).

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
    shouldn't permanently prevent recovery. The 5s connect timeout bounds
    the cost of retrying while the database is genuinely down, and recovery
    is automatic once it's back.
    """
    global _pool

    if not _ASYNCPG_AVAILABLE:
        return None
    if not settings.database_url:
        # Not configured at all - expected on a fresh clone. Debug, not a
        # warning, so it doesn't cry wolf during normal standalone use.
        logger.debug("DATABASE_URL not set; db_tool calls will raise DataUnavailable.")
        return None

    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                settings.database_url, min_size=1, max_size=5, timeout=5,
            )
        except Exception as e:
            # Configured but unreachable - that IS worth a warning, since it
            # otherwise looks identical to "no database configured" while
            # actually being a real outage a DataUnavailable should surface.
            logger.warning(f"DATABASE_URL is set but the database is unreachable: {e}")
            return None

    return _pool


async def close_pool() -> None:
    """Close the pool on app shutdown. Safe to call when none was created."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
