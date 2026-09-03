"""
Diagnostic: tries the structured-output finalization call against EVERY
provider in LLM_PROVIDER_CHAIN individually, for a realistic
RecommendationOutput-shaped payload - unlike going through get_llm()'s
RunnableWithFallbacks (which only surfaces the FIRST provider's error when
all of them fail), this shows each provider's own result, so "did Groq
actually fail, or did it just not get a turn" stops being ambiguous.

See ai-backend/TODO.md's "Recommend/plan ReAct agents unreliable" entry for
the investigation this supports.

    python scripts/check_llm_chain_reliability.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from app.config.settings import settings  # noqa: E402
from app.models.schemas import RecommendationOutput  # noqa: E402
from app.prompts.recommendation_prompt import RECOMMENDATION_FINALIZE_SYSTEM  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402
from langchain_groq import ChatGroq  # noqa: E402


def _make_item(prefix: int, i: int, tags: list[str]) -> dict:
    return {
        "id": f"{prefix:01x}{i:07x}-0000-0000-0000-000000000000",
        "name": f"Place {prefix}{i}", "tags": tags,
        "lat": 6.87 + i * 0.001, "lon": 81.04 + i * 0.001,
        "rating": 4.0, "rating_count": 20, "price_level": 2,
    }


def _build_finalize_messages() -> list:
    obs = {"tool_observations": [
        {"tool": "db_search_listings", "args": {"category": "hotel"},
         "observation": {"items": [_make_item(1, i, ["stay"]) for i in range(10)]}, "error": None},
        {"tool": "db_search_listings", "args": {"category": "restaurant"},
         "observation": {"items": [_make_item(2, i, ["local"]) for i in range(15)]}, "error": None},
        {"tool": "db_search_listings", "args": {"category": "attraction"},
         "observation": {"items": [_make_item(3, i, ["culture", "nature"]) for i in range(15)]}, "error": None},
    ]}
    trip_ctx = json.dumps({
        "trip_context": {"destination_name": "Ella", "district_id": "d1", "lat": 6.87, "lon": 81.04},
        "interests": ["nature", "hike"], "budget": 40000.0, "duration_days": 2,
        "raw_message": "Plan a 2-day trip to Ella, nature and hiking",
    })
    nudge = ("No tools are available in this message - do not attempt any tool or function call here. "
             "Using only the tool observations above (if any), return the final structured object "
             "described in your instructions.")
    return [
        SystemMessage(content=RECOMMENDATION_FINALIZE_SYSTEM),
        HumanMessage(content=trip_ctx),
        HumanMessage(content=json.dumps(obs)),
        HumanMessage(content=nudge),
    ]


def _build_client(spec: str):
    provider, model = spec.split(":", 1)
    if provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=model, google_api_key=settings.gemini_api_key,
            temperature=0, max_output_tokens=2048, timeout=20, max_retries=0,
        )
    if provider == "groq":
        return ChatGroq(
            model_name=model, groq_api_key=settings.groq_api_key,
            temperature=0, max_tokens=2048, request_timeout=20, max_retries=0,
        )
    raise ValueError(f"unknown provider in chain: {spec}")


async def main() -> int:
    specs = [s.strip() for s in settings.llm_provider_chain.split(",") if s.strip()]
    messages = _build_finalize_messages()

    print(f"Provider chain: {specs}\n")
    any_ok = False
    for spec in specs:
        client = _build_client(spec)
        try:
            result: RecommendationOutput = await client.with_structured_output(RecommendationOutput).ainvoke(messages)
            print(f"{spec:35s} OK   hotels={len(result.hotels)} restaurants={len(result.restaurants)} "
                  f"attractions={len(result.attractions)}")
            any_ok = True
        except Exception as e:
            print(f"{spec:35s} FAIL {type(e).__name__}: {str(e)[:150]}")

    print(f"\n{'At least one provider succeeded.' if any_ok else 'ALL providers failed.'}")
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
