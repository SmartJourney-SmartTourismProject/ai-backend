# app/core/llm_config.py
"""
The LLM provider chain and API keys, as an admin set them (Admin > AI models)
with .env as the fallback.

NestJS writes two tables (backend/db/migrations/0017_llm_settings.sql):
  app_setting['llm_provider_chain'] - JSON array of "<provider>:<model>"
  llm_provider_key                  - per-provider key, AES-256-GCM encrypted
This module reads them into a snapshot that app/core/llm.py builds clients
from. Nothing saved in the DB means exactly the old behaviour: the chain is
settings.llm_provider_chain and each key is the .env one.

Reads happen off the request path: once at startup, every
settings.llm_config_refresh_s seconds, and immediately when NestJS calls
/internal/llm/reload after a save. get_llm() stays synchronous and only ever
reads the in-memory snapshot. `version` bumps on every real change, which is
what invalidates get_llm()'s client cache.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config.settings import settings
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

PROVIDERS = ("gemini", "groq", "openai", "anthropic")
CHAIN_SETTING_KEY = "llm_provider_chain"


@dataclass(frozen=True)
class DbOverrides:
    """What an admin saved. Empty = nothing saved, use .env for everything.
    Only the DB side is snapshotted; .env values are read live from
    `settings` (so tests that monkeypatch settings keep working)."""
    chain: tuple[str, ...] = ()
    keys: dict[str, str] = field(default_factory=dict)
    version: int = 0


_db = DbOverrides()
_refresh_task: asyncio.Task | None = None
# Called after the DB config actually changes - llm.py registers
# get_llm.cache_clear here so the next call builds clients from the new config.
_on_change: list[Callable[[], None]] = []


def on_change(callback: Callable[[], None]) -> None:
    _on_change.append(callback)


def _env_chain() -> tuple[str, ...]:
    return tuple(s.strip() for s in settings.llm_provider_chain.split(",") if s.strip())


def _env_keys() -> dict[str, str]:
    return {
        "gemini": settings.gemini_api_key,
        "groq": settings.groq_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }


def chain() -> tuple[str, ...]:
    """The provider chain in effect: the admin's, else LLM_PROVIDER_CHAIN."""
    return _db.chain or _env_chain()


def chain_source() -> str:
    return "db" if _db.chain else "env"


def key_for(provider: str) -> str:
    """The API key in effect for a provider: the admin's, else .env, else ""."""
    return _db.keys.get(provider) or _env_keys().get(provider, "") or ""


def key_source(provider: str) -> str:
    if _db.keys.get(provider):
        return "db"
    return "env" if _env_keys().get(provider) else "none"


def version() -> int:
    return _db.version


def decrypt(blob: str) -> str:
    """Inverse of NestJS's encrypt (admin-llm.service.ts): base64 of
    iv(12) | ciphertext | tag(16). AESGCM wants ciphertext||tag, which is
    exactly what follows the iv."""
    if not settings.settings_encryption_key:
        raise ValueError("SETTINGS_ENCRYPTION_KEY is not set")
    key = base64.b64decode(settings.settings_encryption_key)
    raw = base64.b64decode(blob)
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def _parse_chain(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if isinstance(item, str) and ":" in item and item.split(":", 1)[0] in PROVIDERS:
            out.append(item.strip())
    return tuple(out)


async def reload() -> None:
    """Re-read the DB. Never raises: a DB outage or a bad row keeps the
    previous overrides rather than taking planning down."""
    global _db
    db_chain: tuple[str, ...] = ()
    db_keys: dict[str, str] = {}
    try:
        pool = await get_pool()
        if pool is None:
            return
        row = await pool.fetchrow("SELECT value FROM app_setting WHERE key = $1", CHAIN_SETTING_KEY)
        if row is not None:
            db_chain = _parse_chain(row["value"])
        for r in await pool.fetch("SELECT provider, encrypted_key FROM llm_provider_key"):
            try:
                db_keys[r["provider"]] = decrypt(r["encrypted_key"])
            except Exception as e:  # wrong/missing master key, corrupt row
                logger.warning(f"llm_config: could not decrypt the saved {r['provider']} key: {e}")
    except Exception as e:
        logger.warning(f"llm_config: reload failed, keeping the current config: {e}")
        return

    if (db_chain, db_keys) != (_db.chain, _db.keys):
        _db = DbOverrides(chain=db_chain, keys=db_keys, version=_db.version + 1)
        logger.info(f"llm_config: chain now {list(chain())} (source={chain_source()})")
        for callback in _on_change:
            callback()


async def _refresh_loop() -> None:
    while True:
        await reload()
        await asyncio.sleep(settings.llm_config_refresh_s)


def start_refresh() -> None:
    global _refresh_task
    if _refresh_task is None:
        _refresh_task = asyncio.get_event_loop().create_task(_refresh_loop())


def stop_refresh() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None
