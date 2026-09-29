"""
The Recommendation agent (AGENT_ARCHITECTURE.md §3.3) - decides what to
query, then explains what score_candidates chose. It never orders anything
itself; score_candidates (app/core/scoring.py's rank(), pure and
deterministic) is the only legal source of an ordering.
"""
from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.base_agent import BaseAgent
from app.core.llm import get_llm
from app.core.react import ReActConfig, TraceStep, run_react
from app.core.result import AgentResult
from app.core.state import TripState
from app.models.schemas import RecommendationOutput
from app.prompts import get_prompt
from app.prompts._base import enforce_max_input_chars
from app.prompts.recommendation_prompt import RECOMMENDATION_FINALIZE_SYSTEM
from app.tools.registry import build_data_tools

logger = logging.getLogger(__name__)

_LISTING_CATEGORIES = ("hotel", "restaurant", "attraction")


def _observed_items(trace: list[TraceStep]) -> dict[str, dict]:
    """Every item this run's db_search_* observations actually returned,
    keyed by id - the exact set app/core/output_validator.py's L1
    referential check needs (so a listing_id the model invents is caught
    rather than trusted), and what the planner agent needs for full item
    data (lat/lon/tags/price_level) that RecommendationOutput.Selection
    itself doesn't carry."""
    items: dict[str, dict] = {}
    for pool in _observed_pools(trace).values():
        for item in pool:
            if "id" in item:
                items[str(item["id"])] = item
    return items


def _observed_pools(trace: list[TraceStep]) -> dict[str, list[dict]]:
    """The same observations as _observed_items, bucketed by category into
    the raw candidate-pool shape app/core/fallback.py's build_plan() needs
    (state.candidate_pools) - db_search_listings' own `category` arg is the
    source of truth for which bucket an item belongs to (the item dict
    itself carries no category field), db_search_events always means
    "event". De-duplicates by id within a category, since the agent may
    call db_search_listings for the same category more than once (e.g.
    widening tags) and a repeat observation is either an identical cache
    hit or a genuinely wider result - either way, one entry per id."""
    pools: dict[str, dict[str, dict]] = {c: {} for c in (*_LISTING_CATEGORIES, "event")}
    for step in trace:
        for call in step.tool_calls:
            if call.error:
                continue
            if call.tool == "db_search_listings":
                category = call.args.get("category")
                if category not in pools:
                    continue
            elif call.tool == "db_search_events":
                category = "event"
            else:
                continue
            for item in (call.observation or {}).get("items") or []:
                if "id" in item:
                    pools[category][str(item["id"])] = item
    return {category: list(items.values()) for category, items in pools.items()}


def _drop_unobserved(selections: list, observed: dict[str, dict]) -> tuple[list, list[str]]:
    """L1 referential check (mirrors app/core/output_validator.py's
    validate_referential), applied HERE at the recommendation agent
    boundary rather than only downstream on the planner's final output.
    Before this, a listing_id the model invented - never returned by a real
    db_search_* observation - passed straight through: _flat() below merges
    it with `observed.get(s.listing_id, {})`, which is `{}` for an
    unobserved id, so the item reaches state.attractions/etc with NO real
    data at all (no lat/lon, no tags, no price_level) and flows straight
    into the planner agent as a legitimate candidate. That's a hallucinated
    selection turning into a broken plan two agents downstream, with
    nothing checking it in between until the planner's OWN output hits L1 -
    by which point a whole ReAct planning run has already been spent
    reasoning about a listing that was never real."""
    kept, dropped_ids = [], []
    for s in selections:
        if s.listing_id in observed:
            kept.append(s)
        else:
            dropped_ids.append(s.listing_id)
    return kept, dropped_ids


class RecommendationAgent(BaseAgent):
    name = "recommendation"

    async def execute(self, state: TripState) -> AgentResult:
        ctx = state.trip_context or {}
        spec = get_prompt("recommendation")
        human = json.dumps({
            "trip_context": ctx,
            "interests": state.interests,
            "must_avoid": state.must_avoid,
            "budget": state.budget,
            "duration_days": state.duration_days,
            "raw_message": state.user_input,
        })
        human = enforce_max_input_chars(spec, human)
        messages = [SystemMessage(content=spec.system), HumanMessage(content=human)]
        tools, _item_store = build_data_tools()

        try:
            result = await run_react(
                llm=get_llm("recommend"), tools=tools, messages=messages,
                output_schema=RecommendationOutput, config=ReActConfig(),
                finalize_system=RECOMMENDATION_FINALIZE_SYSTEM,
            )
        except Exception as e:
            # Catches ReActError (run_react's own failure) AND anything
            # get_llm() itself can raise (no provider configured) - see
            # planner_agent.py's identical fix for why a bare
            # `except ReActError` was a real crash-the-request bug (Phase 8,
            # scenario 11). getattr(..., "trace", []) rather than e.trace
            # directly: a plain Exception (e.g. from get_llm()) has no
            # .trace attribute, only a real ReActError does.
            logger.warning(f"RecommendationAgent failed: {e}")
            state.errors.append(f"recommendation_failed: {e}")
            # The structured RecommendationOutput failed, but the loop may
            # well have made real, successful db_search_* calls before that
            # final call failed - ReActError carries the trace precisely so
            # this isn't thrown away. Populated here so _fallback_node has
            # real DB candidates to rank from scratch instead of an empty
            # pool (the actual bug this fixed - see TODO.md).
            trace = getattr(e, "trace", [])
            observed = _observed_items(trace)
            state.candidate_listing_ids = sorted(observed.keys())
            state.candidate_items = observed
            state.candidate_pools = _observed_pools(trace)
            return AgentResult(success=False, error=str(e))

        output: RecommendationOutput = result.output
        observed = _observed_items(result.trace)
        state.recommendation_output = output.model_dump()
        state.candidate_listing_ids = sorted(observed.keys())
        state.candidate_items = observed
        state.candidate_pools = _observed_pools(result.trace)

        # Backward-compatible flat view for _respond_node / any API consumer
        # that still reads state.hotels/restaurants/attractions/events -
        # same shape those fields have always carried (id + reason), not a
        # new contract. Merged with the full candidate dict (lat/lon/tags/
        # price_level/name/currency) when the id was actually observed, so
        # the planner agent gets real item data, not just id+reason.
        def _flat(selections):
            out = []
            for s in selections:
                full = observed.get(s.listing_id, {})
                out.append({
                    **full, "id": s.listing_id, "category": s.category,
                    "rank": s.rank, "score": s.score, "reason": s.reason,
                })
            return out

        all_dropped_ids: list[str] = []

        def _validated(selections):
            kept, dropped_ids = _drop_unobserved(selections, observed)
            all_dropped_ids.extend(dropped_ids)
            return _flat(kept)

        state.hotels = _validated(output.hotels)
        state.restaurants = _validated(output.restaurants)
        state.attractions = _validated(output.attractions)
        state.events = _validated(output.events)
        state.recommendations = state.hotels + state.restaurants + state.attractions + state.events

        if all_dropped_ids:
            logger.warning(f"RecommendationAgent: dropped {len(all_dropped_ids)} hallucinated selection(s): {all_dropped_ids}")
            state.errors.append(
                f"recommendation_hallucinated_drop: {len(all_dropped_ids)} selection(s) referenced "
                f"a listing_id no db_search_* call ever observed and were dropped: {all_dropped_ids}"
            )

        state.react_traces["recommendation"] = {
            "steps_used": result.steps_used, "tools_used": result.tools_used,
            "stopped_by": result.stopped_by,
        }
        return AgentResult(
            success=True,
            message=f"{len(state.recommendations)} selection(s), {len(output.dropped)} dropped",
        )
