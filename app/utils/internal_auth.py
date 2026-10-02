# app/utils/internal_auth.py
"""
Shared-secret guard for routes only the NestJS backend should call
(/trip-plan, /api/admin/sync/*). NestJS sends INTERNAL_API_TOKEN as
X-Internal-Token; this compares it in constant time.

Unlike /internal/llm/* (disabled outright when no token is set), these
routes stay open when INTERNAL_API_TOKEN is empty, so a fresh clone, the
test suite and local curl demos keep working - set the token in both .env
files for any deployed environment.
"""
import hmac
import logging
from typing import Optional

from fastapi import Header, HTTPException

from app.config.settings import settings

logger = logging.getLogger(__name__)
_warned = False


def require_internal_token(x_internal_token: Optional[str] = Header(default=None)) -> None:
    global _warned
    expected = settings.internal_api_token
    if not expected:
        if not _warned:
            logger.warning("INTERNAL_API_TOKEN is not set - protected AI-backend routes are open")
            _warned = True
        return
    if not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(401, "bad internal token")
