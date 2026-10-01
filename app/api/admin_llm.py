# app/api/admin_llm.py
"""
Internal routes behind Admin > AI models. Only NestJS calls these - it does
the admin auth (Keycloak role) and forwards with X-Internal-Token, which must
equal INTERNAL_API_TOKEN. With no token configured the routes are disabled
(503) rather than open, since /status and /test describe the provider setup.
"""
from __future__ import annotations

import asyncio
import hmac
import time

from fastapi import APIRouter, Header, HTTPException

from app.config.settings import settings
from app.core import llm_config
from app.core.llm import _build, _has_key_for

router = APIRouter(prefix="/internal/llm", tags=["internal"])

TEST_TIMEOUT_S = 25.0


def _require_token(token: str | None) -> None:
    if not settings.internal_api_token:
        raise HTTPException(503, "INTERNAL_API_TOKEN is not configured on the AI backend")
    if not token or not hmac.compare_digest(token, settings.internal_api_token):
        raise HTTPException(401, "bad internal token")


@router.post("/reload")
async def reload_config(x_internal_token: str | None = Header(default=None)):
    _require_token(x_internal_token)
    await llm_config.reload()
    return _status()


@router.get("/status")
async def status(x_internal_token: str | None = Header(default=None)):
    _require_token(x_internal_token)
    return _status()


def _status() -> dict:
    return {
        "chain": list(llm_config.chain()),
        "chain_source": llm_config.chain_source(),
        "keys": {p: llm_config.key_source(p) for p in llm_config.PROVIDERS},
    }


def _classify(error: Exception) -> str:
    """Bucket a provider error into what an admin can act on. Providers
    disagree on shapes (status_code attrs, gRPC-style names, plain text), so
    this reads both the status code and the message."""
    code = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    text = str(error).lower()
    if code == 429 or "429" in text or "resource_exhausted" in text or "rate limit" in text or "quota" in text:
        return "quota"
    if code in (401, 403) or any(s in text for s in ("api key", "api_key", "api-key", "authentication", "permission_denied", "unauthorized")):
        return "auth"
    if code == 404 or "not_found" in text or "not found" in text or "does not exist" in text:
        return "not_found"
    return "error"


async def _test_one(spec: str) -> dict:
    result = {"spec": spec}
    if not _has_key_for(spec):
        return {**result, "status": "no_key", "message": "No API key for this provider", "latency_ms": None}
    started = time.perf_counter()
    try:
        model = _build(spec, "respond", None)
        await asyncio.wait_for(model.ainvoke("Reply with the single word OK."), TEST_TIMEOUT_S)
        status = "ok"
        message = "Working"
    except asyncio.TimeoutError:
        status, message = "error", f"No reply within {TEST_TIMEOUT_S:.0f}s"
    except Exception as e:
        status, message = _classify(e), str(e).replace("\n", " ")[:300]
    return {**result, "status": status, "message": message, "latency_ms": round((time.perf_counter() - started) * 1000)}


@router.post("/test")
async def test_chain(x_internal_token: str | None = Header(default=None)):
    """One tiny call per model in the chain, in parallel. Spends one request
    of each provider's quota - it's a button, not a health check loop."""
    _require_token(x_internal_token)
    results = await asyncio.gather(*(_test_one(spec) for spec in llm_config.chain()))
    return {"results": list(results)}
