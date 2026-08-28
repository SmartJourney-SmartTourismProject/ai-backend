# tests/test_cache.py
import time

from app.utils.cache import cache_get, cache_set


def test_miss_on_unset_key():
    assert cache_get("nope") is None


def test_hit_after_set():
    cache_set("greeting", {"msg": "hello"}, ttl_seconds=5)
    assert cache_get("greeting") == {"msg": "hello"}


def test_entry_expires_after_ttl():
    cache_set("short_lived", {"msg": "bye"}, ttl_seconds=1)
    time.sleep(1.2)
    assert cache_get("short_lived") is None
