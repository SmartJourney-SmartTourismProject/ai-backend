# tests/test_llm_config.py
# Admin-managed LLM config (app/core/llm_config.py), the provider branches it
# feeds in app/core/llm.py, and the internal routes in app/api/admin_llm.py.
# No database and no network: a fake pool stands in for asyncpg.

import json

import pytest
from fastapi import HTTPException

import app.core.llm_config as llm_config
from app.api import admin_llm
from app.config.settings import settings
from app.core.llm import _build, get_llm

# Produced by Node's crypto (aes-256-gcm), exactly as NestJS's
# admin-llm.service.ts encrypts: key = 32 bytes of 0x07, iv = 12 bytes of 0x03,
# plaintext "sk-test-1234". Proves the two sides agree on the blob format.
NODE_KEY = "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
NODE_BLOB = "AwMDAwMDAwMDAwMDVpWOdz9bKm9LcnBoW//rzqqQkqZpHdSFSyUFJg=="


class _FakePool:
    def __init__(self, chain=None, keys=None):
        self.chain = chain
        self.keys = keys or []

    async def fetchrow(self, _sql, _key):
        return None if self.chain is None else {"value": json.dumps(self.chain)}

    async def fetch(self, _sql):
        return [{"provider": p, "encrypted_key": k} for p, k in self.keys]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(llm_config, "_db", llm_config.DbOverrides())
    monkeypatch.setattr(settings, "settings_encryption_key", NODE_KEY)
    get_llm.cache_clear()
    yield
    get_llm.cache_clear()


def _use_pool(monkeypatch, pool):
    async def fake_get_pool():
        return pool
    monkeypatch.setattr(llm_config, "get_pool", fake_get_pool)


def test_decrypts_a_blob_written_by_node():
    assert llm_config.decrypt(NODE_BLOB) == "sk-test-1234"


def test_without_db_overrides_everything_comes_from_env(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert llm_config.chain() == tuple(settings.llm_provider_chain.split(","))
    assert llm_config.chain_source() == "env"
    assert llm_config.key_source("gemini") == "env"     # conftest placeholder
    assert llm_config.key_source("openai") == "none"


async def test_reload_applies_the_admin_chain_and_keys(monkeypatch):
    _use_pool(monkeypatch, _FakePool(
        chain=["anthropic:claude-haiku-4-5", "gemini:gemini-3.5-flash-lite"],
        keys=[("anthropic", NODE_BLOB)],
    ))
    await llm_config.reload()
    assert llm_config.chain() == ("anthropic:claude-haiku-4-5", "gemini:gemini-3.5-flash-lite")
    assert llm_config.chain_source() == "db"
    assert llm_config.key_for("anthropic") == "sk-test-1234"
    assert llm_config.key_source("anthropic") == "db"
    assert llm_config.version() == 1


async def test_a_change_rebuilds_cached_clients_and_no_change_keeps_them(monkeypatch):
    before = get_llm("respond")
    _use_pool(monkeypatch, _FakePool(chain=["groq:openai/gpt-oss-120b"]))
    await llm_config.reload()
    after = get_llm("respond")
    assert after is not before
    assert type(after).__name__ == "ChatGroq"           # single model, no fallbacks
    await llm_config.reload()                           # same DB state again
    assert get_llm("respond") is after
    assert llm_config.version() == 1


async def test_unknown_providers_in_the_saved_chain_are_ignored(monkeypatch):
    _use_pool(monkeypatch, _FakePool(chain=["bogus:x", "groq:openai/gpt-oss-120b"]))
    await llm_config.reload()
    assert llm_config.chain() == ("groq:openai/gpt-oss-120b",)


async def test_an_undecryptable_key_falls_back_to_env(monkeypatch):
    _use_pool(monkeypatch, _FakePool(keys=[("groq", "not-a-real-blob")]))
    await llm_config.reload()
    assert llm_config.key_source("groq") == "env"


async def test_a_db_outage_keeps_the_previous_config(monkeypatch):
    _use_pool(monkeypatch, _FakePool(chain=["groq:openai/gpt-oss-120b"]))
    await llm_config.reload()

    async def broken_pool():
        raise RuntimeError("db down")
    monkeypatch.setattr(llm_config, "get_pool", broken_pool)
    await llm_config.reload()
    assert llm_config.chain() == ("groq:openai/gpt-oss-120b",)


def test_claude_haiku_keeps_sampling_params(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-placeholder")
    model = _build("anthropic:claude-haiku-4-5", "slots", None)
    assert model.temperature == settings.llm_temperature
    assert model.max_tokens == 512


def test_thinking_claude_models_drop_sampling_params_and_get_more_room(monkeypatch):
    # Sonnet/Opus 5.5 400 on temperature/top_p/top_k and think on every call.
    monkeypatch.setattr(settings, "anthropic_api_key", "test-placeholder")
    model = _build("anthropic:claude-sonnet-5-5", "slots", None)
    assert model.temperature is None
    assert model.max_tokens >= 4096


def test_openai_branch_builds_a_chat_openai(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-placeholder")
    model = _build("openai:some-model", "plan", 0.2)
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "some-model"


@pytest.mark.parametrize("message,expected", [
    ("429 RESOURCE_EXHAUSTED quota exceeded", "quota"),
    ("Error code: 401 - invalid x-api-key", "auth"),
    ("model: not_found_error", "not_found"),
    ("connection reset", "error"),
])
def test_test_results_are_bucketed_for_the_admin(message, expected):
    assert admin_llm._classify(Exception(message)) == expected


def test_internal_routes_need_the_shared_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    with pytest.raises(HTTPException) as disabled:
        admin_llm._require_token("anything")
    assert disabled.value.status_code == 503

    monkeypatch.setattr(settings, "internal_api_token", "s3cret")
    with pytest.raises(HTTPException) as wrong:
        admin_llm._require_token("nope")
    assert wrong.value.status_code == 401
    admin_llm._require_token("s3cret")   # no raise
