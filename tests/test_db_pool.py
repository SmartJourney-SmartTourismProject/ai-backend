# tests/test_db_pool.py
# No real database - asyncpg.create_pool and pgvector.asyncpg.register_vector
# are faked. Covers the pgvector codec registration added for the RAG
# knowledge base (app/rag/retrieve.py needs vector columns to bind as plain
# Python lists), and that it must never take the whole pool down.

from unittest.mock import AsyncMock, MagicMock, patch

import app.utils.db_pool as db_pool


async def test_register_codecs_calls_pgvector_register_vector(monkeypatch):
    fake_register = AsyncMock()
    with patch.dict("sys.modules", {"pgvector.asyncpg": MagicMock(register_vector=fake_register)}):
        await db_pool._register_codecs(MagicMock())
    fake_register.assert_awaited_once()


async def test_register_codecs_swallows_failure_when_vector_extension_is_missing(monkeypatch):
    # Migration 0013 not applied yet -> pgvector has nothing to register
    # against - this must not raise, or every OTHER query on this shared
    # pool (listings, events, calendar) would break too.
    failing_register = AsyncMock(side_effect=RuntimeError("type 'vector' does not exist"))
    with patch.dict("sys.modules", {"pgvector.asyncpg": MagicMock(register_vector=failing_register)}):
        await db_pool._register_codecs(MagicMock())   # must not raise


async def test_get_pool_passes_register_codecs_as_the_init_hook(monkeypatch):
    monkeypatch.setattr(db_pool, "_pool", None)
    monkeypatch.setattr(db_pool, "_ASYNCPG_AVAILABLE", True)
    monkeypatch.setattr(db_pool.settings, "database_url", "postgresql://x")

    fake_create_pool = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

    await db_pool.get_pool()

    assert fake_create_pool.call_args.kwargs["init"] is db_pool._register_codecs


async def test_get_pool_still_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(db_pool, "_pool", None)
    monkeypatch.setattr(db_pool.settings, "database_url", "")
    assert await db_pool.get_pool() is None
