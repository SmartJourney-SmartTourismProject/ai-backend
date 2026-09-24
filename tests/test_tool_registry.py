# tests/test_tool_registry.py
# app/tools/registry.py wraps existing tool functions (already tested in
# their own files) as LangChain StructuredTools - this only checks the
# WRAPPING: the right tools exist, args schemas validate what they should,
# and each tool's underlying call actually reaches the real function
# (mocked here, same as every other tool test in this suite).
#
# DATA_TOOLS/build_planning_tools are id-based now (B1,
# AI_BACKEND_OPTIMIZATION_PLAN.md) - score_candidates/estimate_costs/
# build_day_plan take listing_ids and resolve them against a per-request
# item_store, instead of the model re-emitting full candidate dicts.

from unittest.mock import AsyncMock

import app.tools.registry as registry


def test_data_tools_are_the_two_named_after_demoting_events_and_travel_matrix():
    # db_search_events was dropped (AI_BACKEND_OPTIMIZATION_PLAN.md C5) -
    # local_event has 0 rows, so this was a tool that could only ever waste
    # a ReAct step on an always-empty result. travel_matrix dropped too
    # (B2) - its result was never converted into a usable matrix anyway.
    tools, _item_store = registry.build_data_tools()
    names = {t.name for t in tools}
    assert names == {"db_search_listings", "score_candidates"}


def test_planning_tools_are_the_three_named_after_dropping_travel_matrix():
    names = {t.name for t in registry.build_planning_tools({})}
    assert names == {"estimate_costs", "build_day_plan", "check_budget"}


def test_no_agent_sees_more_than_six_tools():
    # AGENT_ARCHITECTURE.md §4's own stated design constraint.
    tools, _item_store = registry.build_data_tools()
    assert len(tools) <= 6
    assert len(registry.build_planning_tools({})) <= 6


async def test_db_search_listings_tool_never_raises_even_on_data_unavailable(monkeypatch):
    from app.tools.db_tool import DataUnavailable
    monkeypatch.setattr(registry, "search_listings_by_district", AsyncMock(side_effect=DataUnavailable("db down")))
    tools, _item_store = registry.build_data_tools()
    tool = next(t for t in tools if t.name == "db_search_listings")

    result = await tool.ainvoke({"district_id": "d1", "category": "hotel"})
    assert result["items"] == []
    assert "error" in result


async def test_db_search_listings_populates_the_item_store_by_id(monkeypatch):
    monkeypatch.setattr(
        registry, "search_listings_by_district",
        AsyncMock(return_value={"items": [{"id": "a", "name": "A"}], "total": 1, "truncated": False}),
    )
    tools, item_store = registry.build_data_tools()
    tool = next(t for t in tools if t.name == "db_search_listings")

    await tool.ainvoke({"district_id": "d1", "category": "hotel"})
    assert item_store["a"]["name"] == "A"


def test_score_candidates_tool_resolves_ids_and_is_deterministic():
    tools, item_store = registry.build_data_tools()
    item_store["a"] = {"id": "a", "name": "A", "lat": 7.29, "lon": 80.63, "tags": ["culture"], "rating": 4.5, "rating_count": 100}
    item_store["b"] = {"id": "b", "name": "B", "lat": 7.30, "lon": 80.64, "tags": ["nature"], "rating": 4.0, "rating_count": 50}
    tool = next(t for t in tools if t.name == "score_candidates")

    args = {"listing_ids": ["a", "b"], "interests": ["culture"], "anchor": {"lat": 7.29, "lon": 80.63}, "category": "attraction"}
    result1 = tool.invoke(args)
    result2 = tool.invoke(args)
    assert result1 == result2
    assert result1["ranked"][0]["listing_id"] == "a"   # matches the stated interest, ranked first


def test_score_candidates_tool_silently_drops_an_unknown_id():
    tools, item_store = registry.build_data_tools()
    item_store["a"] = {"id": "a", "name": "A", "lat": 7.29, "lon": 80.63, "tags": [], "rating": None, "rating_count": 0}
    tool = next(t for t in tools if t.name == "score_candidates")

    result = tool.invoke({"listing_ids": ["a", "never-observed"], "anchor": {"lat": 7.29, "lon": 80.63}, "category": "attraction"})
    assert len(result["ranked"]) == 1
    assert result["ranked"][0]["listing_id"] == "a"


def test_build_day_plan_tool_delegates_to_real_itinerary_module():
    item_store = {"a": {"id": "a", "name": "Temple", "lat": 7.30, "lon": 80.64, "currency": "LKR"}}
    tools = registry.build_planning_tools({}, item_store)
    tool = next(t for t in tools if t.name == "build_day_plan")
    result = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "attraction_ids": ["a"],
    })
    assert result["items"][0]["listing_id"] == "a"
    assert result["day_cost"] == 0.0   # no cost_reference rows given - never assumed nonzero


def test_estimate_costs_tool_resolves_ids_against_the_item_store():
    item_store = {"h1": {"id": "h1", "name": "Hotel", "price_level": 2}}
    tools = registry.build_planning_tools({}, item_store)
    tool = next(t for t in tools if t.name == "estimate_costs")

    result = tool.invoke({"listing_ids": ["h1"], "category": "hotel"})
    assert "h1" in result["per_item"]


# ---- restaurants are auto-resolved, not model-assigned per day (Part 2 extension) --
# Live-found: the model had no geographic signal when picking restaurant_ids per
# day, so a real run assigned a Badulla restaurant to an all-Ella day. build_day_plan
# no longer takes restaurant_ids at all - it picks from every restaurant in the
# item_store itself, by real proximity, same as the deterministic fallback planner.

def _build_day_plan_args(day=1, anchor=None):
    return {"day": day, "date": "2026-10-01", "anchor": anchor or {"lat": 7.29, "lon": 80.63}}


def test_build_day_plan_tool_takes_no_restaurant_ids_argument():
    from app.tools.registry import _BuildDayPlanArgs
    assert "restaurant_ids" not in _BuildDayPlanArgs.model_fields


def test_build_day_plan_tool_auto_picks_the_nearest_restaurant():
    # build_day_plan always requests both lunch and dinner (DayConstraints'
    # own defaults - there's no include_lunch/include_dinner arg on this
    # tool), so two DIFFERENT nearby restaurants are needed to prove the far
    # one is skipped rather than forced into use.
    near_1 = {"id": "r-near-1", "name": "Near 1", "category": "restaurant", "lat": 7.291, "lon": 80.631, "currency": "LKR"}
    near_2 = {"id": "r-near-2", "name": "Near 2", "category": "restaurant", "lat": 7.292, "lon": 80.632, "currency": "LKR"}
    far = {"id": "r-far", "name": "Far", "category": "restaurant", "lat": 8.5, "lon": 81.5, "currency": "LKR"}
    item_store = {"r-near-1": near_1, "r-near-2": near_2, "r-far": far}
    tools = registry.build_planning_tools({}, item_store)
    tool = next(t for t in tools if t.name == "build_day_plan")

    result = tool.invoke(_build_day_plan_args())

    restaurant_ids = [i["listing_id"] for i in result["items"] if i["type"] == "restaurant"]
    assert set(restaurant_ids) == {"r-near-1", "r-near-2"}
    assert "r-far" not in restaurant_ids   # not close enough to be picked when nearer ones exist


def test_build_day_plan_tool_never_repeats_a_restaurant_across_days():
    only_one = {"id": "r1", "name": "Only Restaurant", "category": "restaurant",
                "lat": 7.291, "lon": 80.631, "currency": "LKR"}
    item_store = {"r1": only_one}
    tools = registry.build_planning_tools({}, item_store)
    tool = next(t for t in tools if t.name == "build_day_plan")

    day1 = tool.invoke(_build_day_plan_args(day=1))
    day1_restaurant_ids = {i["listing_id"] for i in day1["items"] if i["type"] == "restaurant"}
    assert day1_restaurant_ids == {"r1"}   # the only candidate - correctly used

    day2 = tool.invoke(_build_day_plan_args(day=2))
    # Exhausted (day 1 already used it) - reused rather than serving day 2
    # with no meal at all, same "degrade, don't omit" convention used
    # throughout this codebase.
    day2_restaurant_ids = {i["listing_id"] for i in day2["items"] if i["type"] == "restaurant"}
    assert day2_restaurant_ids == {"r1"}
