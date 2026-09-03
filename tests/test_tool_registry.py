# tests/test_tool_registry.py
# app/tools/registry.py wraps existing tool functions (already tested in
# their own files) as LangChain StructuredTools - this only checks the
# WRAPPING: the right tools exist, args schemas validate what they should,
# and each tool's underlying call actually reaches the real function
# (mocked here, same as every other tool test in this suite).

from unittest.mock import AsyncMock

import pytest

import app.tools.registry as registry


def test_context_tools_are_the_six_named_in_agent_architecture():
    names = {t.name for t in registry.CONTEXT_TOOLS}
    assert names == {
        "resolve_place", "resolve_district", "resolve_start_location",
        "get_calendar_free_days", "get_weather", "get_disaster_info",
    }


def test_data_tools_are_the_four_named_in_agent_architecture():
    names = {t.name for t in registry.DATA_TOOLS}
    assert names == {"db_search_listings", "db_search_events", "travel_matrix", "score_candidates"}


def test_planning_tools_are_the_four_named_in_agent_architecture():
    names = {t.name for t in registry.build_planning_tools({})}
    assert names == {"estimate_costs", "build_day_plan", "check_budget", "travel_matrix"}


def test_no_agent_sees_more_than_six_tools():
    # AGENT_ARCHITECTURE.md §4's own stated design constraint.
    assert len(registry.CONTEXT_TOOLS) <= 6
    assert len(registry.DATA_TOOLS) <= 6
    assert len(registry.build_planning_tools({})) <= 6


async def test_resolve_place_tool_reaches_real_function(monkeypatch):
    monkeypatch.setattr(registry, "resolve_place", AsyncMock(return_value={"name": "Kandy", "lat": 7.29, "lon": 80.63}))
    tool = next(t for t in registry.CONTEXT_TOOLS if t.name == "resolve_place")

    result = await tool.ainvoke({"name": "Kandy"})
    assert result["name"] == "Kandy"


async def test_resolve_place_tool_reports_failure_as_a_dict_not_none(monkeypatch):
    monkeypatch.setattr(registry, "resolve_place", AsyncMock(return_value=None))
    tool = next(t for t in registry.CONTEXT_TOOLS if t.name == "resolve_place")

    result = await tool.ainvoke({"name": "Nowhereville"})
    assert "error" in result   # never a bare None - the LLM needs something to react to


async def test_db_search_listings_tool_never_raises_even_on_data_unavailable(monkeypatch):
    from app.tools.db_tool import DataUnavailable
    monkeypatch.setattr(registry, "search_listings_by_district", AsyncMock(side_effect=DataUnavailable("db down")))
    tool = next(t for t in registry.DATA_TOOLS if t.name == "db_search_listings")

    result = await tool.ainvoke({"district_id": "d1", "category": "hotel"})
    assert result["items"] == []
    assert "error" in result


def test_score_candidates_tool_is_sync_and_deterministic():
    tool = next(t for t in registry.DATA_TOOLS if t.name == "score_candidates")
    candidates = [
        {"id": "a", "name": "A", "lat": 7.29, "lon": 80.63, "tags": ["culture"], "rating": 4.5, "rating_count": 100},
        {"id": "b", "name": "B", "lat": 7.30, "lon": 80.64, "tags": ["nature"], "rating": 4.0, "rating_count": 50},
    ]
    result1 = tool.invoke({"candidates": candidates, "interests": ["culture"], "anchor": {"lat": 7.29, "lon": 80.63}, "category": "attraction"})
    result2 = tool.invoke({"candidates": candidates, "interests": ["culture"], "anchor": {"lat": 7.29, "lon": 80.63}, "category": "attraction"})
    assert result1 == result2
    assert result1["ranked"][0]["listing_id"] == "a"   # matches the stated interest, ranked first


def test_build_day_plan_tool_delegates_to_real_itinerary_module():
    tools = registry.build_planning_tools({})
    tool = next(t for t in tools if t.name == "build_day_plan")
    result = tool.invoke({
        "day": 1, "date": "2026-10-01", "anchor": {"lat": 7.29, "lon": 80.63},
        "attractions": [{"id": "a", "name": "Temple", "lat": 7.30, "lon": 80.64, "currency": "LKR"}],
    })
    assert result["items"][0]["listing_id"] == "a"
    assert result["day_cost"] == 0.0   # no cost_lookup given - never assumed nonzero
