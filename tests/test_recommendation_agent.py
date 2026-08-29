# tests/test_recommendation_agent.py
# No real Gemini calls - the LLM is mocked so this tests OUR payload
# construction and state-assignment logic, not Google's model. Uses the
# real db_tool mock data (Kandy) and real rag_service, which both already
# have their own fail-open fallbacks (faiss/sentence-transformers aren't
# installed in this environment, and rag_service degrades to the
# unfiltered candidate list in that case - see recommendation_agent.py's
# _retrieve_or_fallback).

import json
from unittest.mock import MagicMock, AsyncMock

import app.workflows.recommendation_agent as recommendation_agent_module
from app.workflows.recommendation_agent import RecommendationAgent
from app.core.state import TripState


def _payload_section(prompt: str) -> str:
    """Extracts just the JSON candidate-data blob from the full prompt,
    since the static system-prompt text itself mentions field names like
    "previous_itinerary" when explaining the rule for using them."""
    return prompt.split("Candidate Data:\n", 1)[1].split("\n\nReturn valid JSON", 1)[0]


def _patch_llm(monkeypatch, response_dict: dict):
    mock_response = MagicMock()
    mock_response.content = json.dumps(response_dict)
    mock_llm_instance = MagicMock()
    mock_llm_instance.ainvoke = AsyncMock(return_value=mock_response)
    monkeypatch.setattr(
        recommendation_agent_module, "ChatGoogleGenerativeAI",
        MagicMock(return_value=mock_llm_instance),
    )
    return mock_llm_instance


_FULL_RESPONSE = {
    "hotels": [{"id": "h1", "name": "Hilltop Kandy Residency", "reason": "central"}],
    "restaurants": [],
    "attractions": [{"id": "a1", "name": "Temple of the Sacred Tooth Relic", "reason": "iconic"}],
    "events": [],
    "itinerary": [
        {"day": 1, "items": [
            {"time": "09:00", "type": "attraction", "name": "Temple of the Sacred Tooth Relic",
             "notes": "...", "lat": 7.2936, "lon": 80.6413},
        ]},
    ],
    "estimated_cost": 150.0,
    "budget_notes": "Fits comfortably within budget.",
}


async def test_missing_destination_returns_failure(monkeypatch):
    # __init__ builds the LLM client unconditionally (before the
    # destination check even runs), so it needs mocking here too even
    # though this test never expects a call to reach it.
    _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(user_input="Plan a trip")
    result = await RecommendationAgent().execute(state)

    assert result.success is False
    assert any("destination" in e.lower() for e in state.errors)


async def test_unknown_destination_returns_failure(monkeypatch):
    _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(user_input="Plan a trip", destination="Nowhereville")

    result = await RecommendationAgent().execute(state)

    assert result.success is False
    assert any("no verified listings" in e.lower() for e in state.errors)


async def test_successful_call_sets_state_including_lat_lon(monkeypatch):
    _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(user_input="Plan a trip to Kandy", destination="Kandy", budget=200.0)

    result = await RecommendationAgent().execute(state)

    assert result.success is True
    assert state.estimated_cost == 150.0
    assert len(state.itinerary) == 1
    item = state.itinerary[0]["items"][0]
    assert item["lat"] == 7.2936
    assert item["lon"] == 80.6413


async def test_budget_notes_is_set_and_final_response_is_untouched(monkeypatch):
    # Regression test: this agent used to overwrite state.final_response
    # with budget_notes, which _respond_node then always clobbered right
    # after - silently discarding it on every request. It must go on its
    # own field instead.
    _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(user_input="Plan a trip to Kandy", destination="Kandy")

    await RecommendationAgent().execute(state)

    assert state.budget_notes == "Fits comfortably within budget."
    assert state.final_response is None


async def test_traveler_request_is_always_passed_to_the_llm(monkeypatch):
    # Regression test: special requirements in the raw message (dietary,
    # accessibility, pace) don't map to any structured slot, so the LLM
    # must see the original text on every call, not just follow-ups.
    mock_llm = _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(
        user_input="Plan a trip to Kandy, vegetarian only, can't walk far",
        destination="Kandy",
    )

    await RecommendationAgent().execute(state)

    sent_prompt = mock_llm.ainvoke.call_args[0][0]
    assert "vegetarian only, can't walk far" in sent_prompt


async def test_followup_includes_previous_itinerary_and_modification_request(monkeypatch):
    mock_llm = _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(
        user_input="Make it cheaper",
        destination="Kandy",
        is_followup=True,
        itinerary=[{"day": 1, "items": [{"name": "Old Hotel"}]}],
    )

    await RecommendationAgent().execute(state)

    payload = _payload_section(mock_llm.ainvoke.call_args[0][0])
    assert "previous_itinerary" in payload
    assert "Old Hotel" in payload
    assert "modification_request" in payload
    assert "Make it cheaper" in payload


async def test_followup_without_prior_itinerary_omits_modification_fields(monkeypatch):
    # is_followup alone isn't enough - there must be an actual previous
    # itinerary to refine, otherwise this is really a first turn.
    mock_llm = _patch_llm(monkeypatch, _FULL_RESPONSE)
    state = TripState(user_input="Plan a trip to Kandy", destination="Kandy", is_followup=True)

    await RecommendationAgent().execute(state)

    payload = _payload_section(mock_llm.ainvoke.call_args[0][0])
    assert "previous_itinerary" not in payload
    assert "modification_request" not in payload


async def test_llm_failure_returns_failure_not_raises(monkeypatch):
    mock_llm_instance = MagicMock()
    mock_llm_instance.ainvoke = AsyncMock(side_effect=RuntimeError("API down"))
    monkeypatch.setattr(
        recommendation_agent_module, "ChatGoogleGenerativeAI",
        MagicMock(return_value=mock_llm_instance),
    )
    state = TripState(user_input="Plan a trip to Kandy", destination="Kandy")

    result = await RecommendationAgent().execute(state)

    assert result.success is False
    assert any("API down" in e for e in state.errors)
