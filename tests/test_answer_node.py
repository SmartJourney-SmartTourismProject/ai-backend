# tests/test_answer_node.py
# _answer_node in isolation - retrieval, the LLM call, and citation
# parsing are all faked (each has its own dedicated tests: test_rag_
# retrieve.py, test_embeddings.py). Full-graph routing for intent
# "question"/"both" is covered in test_orchestrator.py-style fashion here
# too, reusing that file's _patch_agents harness.

from unittest.mock import AsyncMock, MagicMock

import app.core.orchestrator as orchestrator_module
from app.core.orchestrator import _answer_node, _cited_passage_indices, orchestrator
from app.core.state import TripState
from app.models.schemas import AnswerOutput
from app.rag.retrieve import Passage

from tests.test_orchestrator import _FakePlannerAgent, _FakeRecommendationAgent, _patch_agents


def _passage(id_="p1", title="Temple dress code", section="Footwear", content="Remove your shoes.",
            url="https://x", vector_score=0.8):
    return Passage(id=id_, content=content, section=section, title=title, url=url,
                   license="internal", last_verified=None, vector_score=vector_score, rrf_score=0.02)


def _patch_llm(monkeypatch, answer_text: str):
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=AnswerOutput(answer=answer_text))
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    monkeypatch.setattr(orchestrator_module, "get_llm", MagicMock(return_value=mock_llm))


# ---- _cited_passage_indices ------------------------------------------------

def test_cited_passage_indices_deduped_and_ordered():
    assert _cited_passage_indices("A [2] and B [1], also [2] again.", passage_count=3) == [2, 1]


def test_cited_passage_indices_drops_out_of_range():
    assert _cited_passage_indices("See [1] and [9].", passage_count=2) == [1]


def test_cited_passage_indices_no_brackets_is_empty():
    assert _cited_passage_indices("No citations here.", passage_count=3) == []


def test_cited_passage_indices_handles_a_grouped_citation_list():
    # Live-observed 2026-09-30: the model wrote "[1, 2, 4]" despite the
    # prompt asking for separate brackets per citation.
    assert _cited_passage_indices("Cover up [1, 2, 4].", passage_count=4) == [1, 2, 4]


def test_cited_passage_indices_grouped_list_still_drops_out_of_range_and_dedupes():
    assert _cited_passage_indices("See [1, 1, 9, 2].", passage_count=2) == [1, 2]


# ---- _answer_node -----------------------------------------------------------

async def test_no_passages_returns_a_plain_refusal_with_no_llm_call(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=[]))
    mock_llm = MagicMock()
    monkeypatch.setattr(orchestrator_module, "get_llm", mock_llm)

    state = TripState(user_input="do I need a visa?", question="do I need a visa?", intent="question")
    result = await _answer_node(state)

    assert result.answer == "I don't have reliable information on that."
    mock_llm.assert_not_called()


async def test_weak_match_below_threshold_refuses(monkeypatch):
    monkeypatch.setattr(orchestrator_module.settings, "rag_min_score", 0.55)
    weak = _passage(vector_score=0.2)
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=[weak]))
    mock_llm = MagicMock()
    monkeypatch.setattr(orchestrator_module, "get_llm", mock_llm)

    state = TripState(user_input="best nightclub in Paris", question="best nightclub in Paris", intent="question")
    result = await _answer_node(state)

    assert result.answer == "I don't have reliable information on that."
    mock_llm.assert_not_called()


async def test_confident_match_calls_the_llm_and_records_cited_sources(monkeypatch):
    passages = [_passage(id_="p1", title="Temple dress code", vector_score=0.9),
               _passage(id_="p2", title="Emergency numbers", vector_score=0.6)]
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=passages))
    _patch_llm(monkeypatch, "Cover your shoulders and knees [1].")

    state = TripState(user_input="what should I wear at a temple?",
                      question="what should I wear at a temple?", intent="question")
    result = await _answer_node(state)

    assert result.answer == "Cover your shoulders and knees [1]."
    assert result.sources == [{"title": "Temple dress code", "url": "https://x", "section": "Footwear", "license": "internal"}]


async def test_uncited_answer_falls_back_to_the_top_passage_as_its_source(monkeypatch):
    passages = [_passage(id_="p1", title="Top hit", vector_score=0.9)]
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=passages))
    _patch_llm(monkeypatch, "A confident-sounding answer with no citation marker at all.")

    state = TripState(user_input="q", question="q", intent="question")
    result = await _answer_node(state)

    # Never an uncited answer with zero sources shown, per the eval target.
    assert result.sources == [{"title": "Top hit", "url": "https://x", "section": "Footwear", "license": "internal"}]


async def test_llm_failure_falls_back_to_the_top_passage_verbatim(monkeypatch):
    passages = [_passage(id_="p1", content="Watch your bag on buses.", vector_score=0.9)]
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=passages))
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(side_effect=RuntimeError("provider down"))
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_structured
    monkeypatch.setattr(orchestrator_module, "get_llm", MagicMock(return_value=mock_llm))

    state = TripState(user_input="q", question="q", intent="question")
    result = await _answer_node(state)

    assert result.answer == "Watch your bag on buses. [1]"
    assert result.sources[0]["title"] == passages[0].title


async def test_retrieval_failure_degrades_to_refusal_not_an_exception(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(side_effect=RuntimeError("db down")))
    state = TripState(user_input="q", question="q", intent="question")
    result = await _answer_node(state)
    assert result.answer == "I don't have reliable information on that."


async def test_district_is_resolved_from_trip_context_when_present(monkeypatch):
    retrieve_mock = AsyncMock(return_value=[_passage(vector_score=0.9)])
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", retrieve_mock)
    resolve_mock = AsyncMock()   # must NOT be called - trip_context already has a district_id
    monkeypatch.setattr(orchestrator_module, "resolve_place", resolve_mock)
    _patch_llm(monkeypatch, "answer [1]")

    state = TripState(user_input="q", question="q", intent="both",
                      trip_context={"district_id": "d-kandy"})
    await _answer_node(state)

    resolve_mock.assert_not_called()
    assert retrieve_mock.call_args.kwargs["district_id"] == "d-kandy"


async def test_district_falls_back_to_resolving_the_named_destination(monkeypatch):
    retrieve_mock = AsyncMock(return_value=[_passage(vector_score=0.9)])
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", retrieve_mock)
    monkeypatch.setattr(orchestrator_module, "resolve_place",
                        AsyncMock(return_value={"district_id": "d-galle"}))
    _patch_llm(monkeypatch, "answer [1]")

    state = TripState(user_input="is it safe in Galle?", question="is it safe in Galle?",
                      intent="question", destination="Galle")
    await _answer_node(state)

    assert retrieve_mock.call_args.kwargs["district_id"] == "d-galle"


async def test_no_destination_searches_every_district(monkeypatch):
    retrieve_mock = AsyncMock(return_value=[_passage(vector_score=0.9)])
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", retrieve_mock)
    _patch_llm(monkeypatch, "answer [1]")

    state = TripState(user_input="do I need a visa?", question="do I need a visa?", intent="question")
    await _answer_node(state)

    assert retrieve_mock.call_args.kwargs["district_id"] is None


# ---- full-graph routing ------------------------------------------------

async def _question_visa(state):
    state.intent = "question"
    state.question = "do I need a visa?"
    return state


async def _question_water(state):
    state.intent = "question"
    state.question = "is tap water safe?"
    return state


async def test_pure_question_routes_straight_to_answer_never_touching_the_planner(monkeypatch):
    _patch_agents(monkeypatch, fill_slots_fn=_question_visa)
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=[_passage(vector_score=0.9)]))
    _patch_llm(monkeypatch, "Yes, most visitors need an ETA [1].")

    state = TripState(user_input="do I need a visa?")
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == ["validate", "policy", "slot_fill", "answer", "respond"]
    assert result["final_response"] == "Yes, most visitors need an ETA [1]."
    assert result["itinerary"] == []   # never touched orchestrate/recommend/plan


async def test_pure_question_never_asks_which_destination(monkeypatch):
    # The exact bug this feature had to avoid: a first-turn question with no
    # destination must not fall into the "which destination?" clarification
    # slot_filling.py asks a plain plan request for.
    _patch_agents(monkeypatch, fill_slots_fn=_question_water)
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=[]))

    state = TripState(user_input="is tap water safe?")
    result = await orchestrator.ainvoke(state)

    assert result["final_response"] != "Which destination would you like to visit?"
    assert result["final_response"] == "I don't have reliable information on that."


async def test_both_intent_runs_the_full_plan_pipeline_and_appends_the_answer(monkeypatch):
    async def _both(state):
        state.destination = "Kandy"
        state.duration_days = 2
        state.intent = "both"
        state.question = "any scams to watch out for?"
        return state

    _patch_agents(monkeypatch, fill_slots_fn=_both)
    monkeypatch.setattr(orchestrator_module, "retrieve_passages", AsyncMock(return_value=[_passage(vector_score=0.9)]))
    _patch_llm(monkeypatch, "Agree tuk-tuk fares up front [1].")

    state = TripState(user_input="plan 2 days in Kandy, any scams?")
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "plan", "verify", "answer", "respond",
    ]
    assert "Here's your trip plan for Kandy" in result["final_response"]
    assert "Agree tuk-tuk fares up front [1]." in result["final_response"]
    assert len(result["itinerary"]) > 0   # the plan half wasn't dropped
