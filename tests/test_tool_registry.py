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


# ---- day_context: server-side enforcement (Part 5, prompt-improvement pass) --
# build_day_plan's day/date/check-in/check-out/exclude_outdoor/items_target
# arguments are all facts the server already knows for certain once a
# DayContext is supplied - these prove they're corrected against ground
# truth rather than trusted from the model's own call, per _build_day_plan's
# own comment in app/tools/registry.py.

from datetime import date as date_cls

from app.core.planner_shared import DayContext

_ATTRACTION = {"id": "a1", "name": "Temple", "lat": 7.30, "lon": 80.64, "currency": "LKR", "tags": []}
_HOTEL = {"id": "h1", "name": "Hotel", "lat": 7.29, "lon": 80.63, "currency": "LKR"}


def test_day_context_derives_the_real_date_ignoring_the_models_own_arg():
    day_context = DayContext(start_date=date_cls(2026, 10, 1), duration_days=3)
    tools = registry.build_planning_tools({}, {"a1": _ATTRACTION}, day_context=day_context)
    tool = next(t for t in tools if t.name == "build_day_plan")

    result = tool.invoke({
        "day": 2, "date": "2099-01-01",   # deliberately wrong - should be ignored
        "anchor": {"lat": 7.29, "lon": 80.63}, "attraction_ids": ["a1"],
    })
    assert result["date"] == "2026-10-02"
    assert result["day"] == 2


def test_day_context_clamps_an_out_of_range_day_to_the_trip_length():
    day_context = DayContext(start_date=date_cls(2026, 10, 1), duration_days=3)
    tools = registry.build_planning_tools({}, {"a1": _ATTRACTION}, day_context=day_context)
    tool = next(t for t in tools if t.name == "build_day_plan")

    result = tool.invoke({
        "day": 7, "date": "2026-10-07",   # past the 3-day trip
        "anchor": {"lat": 7.29, "lon": 80.63}, "attraction_ids": ["a1"],
    })
    assert result["day"] == 3
    assert result["date"] == "2026-10-03"


def test_day_context_forces_checkin_on_day_one_and_checkout_on_the_last_day():
    day_context = DayContext(start_date=date_cls(2026, 10, 1), duration_days=2)
    tools = registry.build_planning_tools({}, {"h1": _HOTEL}, day_context=day_context)
    tool = next(t for t in tools if t.name == "build_day_plan")

    day1 = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "hotel_ids": ["h1"], "need_hotel_checkin": False, "need_hotel_checkout": True,   # both deliberately wrong
    })
    assert [i["type"] for i in day1["items"]] == ["hotel"]   # check-in forced on, check-out forced off

    day2 = tool.invoke({
        "day": 2, "date": "2026-10-02", "anchor": {"lat": 7.29, "lon": 80.63},
        "hotel_ids": ["h1"], "need_hotel_checkin": True, "need_hotel_checkout": False,   # both deliberately wrong
    })
    assert [i["type"] for i in day2["items"]] == ["hotel"]   # check-out forced on, check-in forced off (no duplicate)


def test_day_context_forces_exclude_outdoor_on_a_rainy_day_but_never_forces_it_off():
    day_context = DayContext(
        start_date=date_cls(2026, 10, 1), duration_days=1,
        per_day_rain_probability={"2026-10-01": 0.9},
    )
    outdoor_attraction = {**_ATTRACTION, "tags": ["hike"]}
    tools = registry.build_planning_tools(
        {}, {"a1": outdoor_attraction}, outdoor_tags=frozenset({"hike"}), day_context=day_context,
    )
    tool = next(t for t in tools if t.name == "build_day_plan")

    result = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "attraction_ids": ["a1"], "exclude_outdoor": False,   # the model says it's fine - overridden by real rain
    })
    # Rain forces the exclusion (the model's False is overridden), and since
    # that would leave the day empty, the rain fallback keeps the one outdoor
    # stop - clearly noted - rather than serving no sightseeing at all.
    from app.core.itinerary import RAIN_FALLBACK_NOTE
    [only] = result["items"]
    assert only["listing_id"] == "a1"
    assert RAIN_FALLBACK_NOTE in only["notes"]


def test_day_context_clamps_items_target_to_the_travelers_pace_but_not_below_it():
    day_context = DayContext(start_date=date_cls(2026, 10, 1), duration_days=1, expected_items_per_day=2)
    attractions = {f"a{i}": {"id": f"a{i}", "name": f"A{i}", "lat": 7.29 + i * 0.01, "lon": 80.63,
                             "currency": "LKR", "tags": []} for i in range(5)}
    tools = registry.build_planning_tools({}, attractions, day_context=day_context)
    tool = next(t for t in tools if t.name == "build_day_plan")

    over = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "attraction_ids": list(attractions), "items_target": 5,   # asks for more than the pace allows
    })
    assert len([i for i in over["items"] if i["type"] == "attraction"]) == 2

    under = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "attraction_ids": list(attractions), "items_target": 1,   # a genuine follow-up asking for fewer
    })
    assert len([i for i in under["items"] if i["type"] == "attraction"]) == 1


def test_no_day_context_keeps_the_old_behavior_of_trusting_the_models_args():
    tools = registry.build_planning_tools({}, {"a1": _ATTRACTION})   # no day_context, as every existing caller does
    tool = next(t for t in tools if t.name == "build_day_plan")

    result = tool.invoke({
        "day": 9, "date": "2099-01-01", "anchor": {"lat": 7.29, "lon": 80.63}, "attraction_ids": ["a1"],
    })
    assert result["day"] == 9
    assert result["date"] == "2099-01-01"


def test_build_day_plan_tops_up_a_day_with_nearby_unused_attractions_and_never_repeats_one():
    # Regression (live-found 2026-09-30): a day whose model-picked
    # attractions were far from the hotel came back with only the hotel
    # while nearer candidates sat unused in the pool; and the model could
    # name the same attraction for two different days.
    import asyncio
    from app.core.planner_shared import DayContext
    from app.tools.registry import build_planning_tools
    from datetime import date

    hotel = {"id": "h1", "name": "Hotel", "lat": 6.27, "lon": 81.26, "category": "hotel"}
    near = {"id": "near", "name": "Near Beach", "lat": 6.28, "lon": 81.27, "category": "attraction", "tags": []}
    far = {"id": "far", "name": "Far Ruins", "lat": 6.80, "lon": 81.90, "category": "attraction", "tags": []}
    store = {i["id"]: i for i in (hotel, near, far)}
    ctx = DayContext(start_date=date(2026, 10, 1), duration_days=2, per_day_rain_probability={},
                     expected_items_per_day=2, exclude_categories=[])
    tools = {t.name: t for t in build_planning_tools({}, store, frozenset(), "d1", ctx)}

    def build(day):
        return tools["build_day_plan"].func(
            day=day, date="2026-10-01", anchor={"lat": 6.27, "lon": 81.26}, hotel_ids=["h1"],
            attraction_ids=["far"], items_target=2, exclude_outdoor=False,
            need_hotel_checkin=day == 1, need_hotel_checkout=day == 2, prefer_price_level_max=None,
        )

    day1 = build(1)
    names1 = [i["name"] for i in day1["items"] if i["type"] == "attraction"]
    assert names1 == ["Near Beach"]          # the far pick was dropped, the near one topped up

    day2 = build(2)
    assert "Near Beach" not in [i["name"] for i in day2["items"]]   # never repeated on a later day
