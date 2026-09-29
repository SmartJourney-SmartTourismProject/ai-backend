"""
Thin async Redis cache wrapper. Used to give "real-time" data sources (like
weather) a short TTL cache so repeat requests for the same location don't
re-hit the upstream API on every trip-planning call.

Fails open: if Redis isn't reachable or REDIS_URL isn't configured, get/set
are no-ops (cache miss every time) rather than raising - a missing cache
should degrade to "always fetch fresh", never break the request.
"""
from __future__ import annotations
import json
import logging
from typing import Any, Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    redis = None

_client = None
_connection_failed = False


def _get_client():
    global _client, _connection_failed
    if not _REDIS_AVAILABLE or not settings.redis_url or _connection_failed:
        return None
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def cache_get(key: str) -> Optional[Any]:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f"Redis GET failed for '{key}', treating as cache miss: {e}")
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value), ex=ttl_seconds)
    except Exception as e:
        logger.warning(f"Redis SET failed for '{key}', continuing without cache: {e}")
