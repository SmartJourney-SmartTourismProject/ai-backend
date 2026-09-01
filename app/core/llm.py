# app/core/llm.py
"""
The single construction point for every chat model in this codebase.

Before this module existed, ChatGoogleGenerativeAI(...) was constructed in
three different places (slot_filling.py, recommendation_agent.py,
planning_agent.py) with three different configurations - and two of them
didn't even pass an API key, relying on main.py's load_dotenv() having
already populated os.environ. One construction point removes that class of
bug and is what makes the provider chain below a config change rather than
a rewrite (decision D6b, docs/master_plan/DETERMINISM_AND_VALIDATION.md §2).

Provider chain: on 429 (quota)/503/timeout, LangChain's .with_fallbacks()
tries the next provider in LLM_PROVIDER_CHAIN, in order. Fallback here is
deliberately narrow - a model that returns a well-formed but WRONG answer
should go through the L3 repair path (structured-output validation), not a
different provider; switching providers there would just make the failure
less reproducible. Every provider in the default chain is free-tier, no
card required (docs/master_plan/API_SETUP.md).

.with_fallbacks() preserves .bind_tools() and .with_structured_output() on
the object it returns, so ReAct tool-calling and structured output survive
a provider switch transparently to callers.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from app.config.settings import settings

Purpose = Literal["slots", "orchestrator", "recommend", "plan", "respond"]

# Per-agent output token budgets (DETERMINISM_AND_VALIDATION.md §2 D6c call
# budget). Deliberately generous for recommend/plan, which carry the
# candidate payload and a multi-day itinerary; tight for slots/respond,
# which are single small structured objects / short text.
_TOKEN_BUDGET: dict[Purpose, int] = {
    "slots": 512,
    "orchestrator": 1024,
    "recommend": 2048,
    "plan": 3072,
    "respond": 512,
}


def _has_key_for(spec: str) -> bool:
    provider = spec.split(":", 1)[0]
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "groq":
        return bool(settings.groq_api_key)
    return False


def _build(spec: str, purpose: Purpose) -> BaseChatModel:
    """spec is "<provider>:<model>". Field names genuinely differ between
    the two client classes (verified against their pydantic model_fields,
    not assumed) - ChatGroq has no top_p and calls its timeout param
    request_timeout, for instance - so this branches per provider rather
    than sharing one kwargs dict."""
    provider, model = spec.split(":", 1)
    max_tokens = _TOKEN_BUDGET[purpose]

    if provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            top_k=settings.llm_top_k,
            max_output_tokens=max_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=0,  # retries are the chain's job, not one client's
        )
    if provider == "groq":
        return ChatGroq(
            model_name=model,
            groq_api_key=settings.groq_api_key,
            temperature=settings.llm_temperature,
            max_tokens=max_tokens,
            request_timeout=settings.llm_timeout_s,
            max_retries=0,
        )
    raise ValueError(f"unknown LLM provider in chain: '{provider}' (from spec '{spec}')")


@lru_cache(maxsize=16)
def get_llm(purpose: Purpose) -> BaseChatModel:
    """Returns the primary model for `purpose` with the rest of the
    provider chain attached as fallbacks. Cached per purpose - constructing
    a chat model is cheap but there is no reason to redo it per call."""
    specs = [s.strip() for s in settings.llm_provider_chain.split(",") if s.strip()]

    if purpose == "orchestrator" and settings.llm_model_orchestrator:
        specs = [f"gemini:{settings.llm_model_orchestrator}", *specs]

    usable = [s for s in specs if _has_key_for(s)]
    if not usable:
        raise RuntimeError(
            "No LLM provider has a configured API key. Set GEMINI_API_KEY "
            "(required) and optionally GROQ_API_KEY for failover - see "
            "docs/master_plan/API_SETUP.md."
        )

    primary, *rest = (_build(s, purpose) for s in usable)
    return primary.with_fallbacks(rest) if rest else primary
