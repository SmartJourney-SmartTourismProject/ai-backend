# Modifications — thisuri

This document records the correctness fixes made to `ai-backend` after reviewing the
LangGraph orchestrator and RAG implementation against how LangGraph 1.2.10 and standard
RAG/embedding practice actually behave (verified against the installed package source,
not just docs, and re-tested after each fix).

## 1. FAISS vector store — stored vectors were never normalized

**File:** [`app/rag/vector_store.py`](app/rag/vector_store.py)

**Problem:** `VectorStore` uses `faiss.IndexFlatIP` (inner product) and relies on all
vectors being L2-normalized so that inner product equals cosine similarity — the code's
own comment said as much. In practice:
- `_maybe_init_faiss()` normalized `self._vectors` *before* `add_vectors()` had extended
  it with the new batch, so it was always normalizing an **empty list** — dead code.
- The actual vectors passed to `faiss_index.add(...)` were added completely
  unnormalized.
- Query vectors *were* normalized in `_search_faiss()`.

Net effect: stored vectors carried their raw magnitude into the "cosine similarity"
score, silently biasing search results (e.g. toward longer text blobs with larger
embedding norms) instead of ranking by semantic similarity. Both `faiss-cpu` and
`sentence-transformers` are installed in this project's `.venv`, so this was the active
code path, not a dormant fallback.

**Fix:** `_maybe_init_faiss()` now only constructs the index. `add_vectors()` normalizes
the incoming batch with `faiss.normalize_L2()` immediately before `faiss_index.add()`,
matching the normalization already applied to queries.

**Verified:** indexed two short documents (a hiking blurb, a beach-resort blurb) and
queried `"hiking waterfall"` — after the fix, scores were `0.84` (hiking doc) vs. `0.25`
(resort doc), correctly discriminating relevant from irrelevant results.

## 2. LangGraph orchestrator — duplicate edge bypassed input validation

**File:** [`app/workflows/orchestrator.py`](app/workflows/orchestrator.py)

**Problem:** `build_orchestrator_graph()` added **both** an unconditional edge
`validate_input → policy_check_node` (twice, at two different points in the function)
**and** a conditional edge out of `validate_input` that was supposed to route to an
`"error"` node when `state.errors` was non-empty. LangGraph's `StateGraph` does not
reject a node having both a static edge and conditional edges to overlapping
destinations, so the unconditional edge fired regardless of validation errors. Result:
on invalid input (e.g. empty `user_input`), the graph still ran the *entire* pipeline —
policy check, calendar check, weather/disaster fetch, and two paid Gemini calls — instead
of short-circuiting to the error handler as the conditional edge was clearly meant to do.

**Fix:** Removed both unconditional `add_edge("validate_input", "policy_check_node")`
calls, leaving `add_conditional_edges("validate_input", ...)` as the *only* outgoing edge
from that node.

## 3. LangGraph orchestrator — destination-required routing was dead code

**File:** [`app/workflows/orchestrator.py`](app/workflows/orchestrator.py)

**Problem:** `route_to_recommendation()` was written to guard against running
`recommendation_agent` without a `destination`, but it was never registered — the graph
instead used a plain `add_edge("weather_disaster_node", "recommendation_agent")`, so the
guard function was unreachable and the check never actually ran at the graph level.

**Fix:** Replaced the plain edge with
`add_conditional_edges("weather_disaster_node", route_to_recommendation, {"recommendation_agent": ..., "error": ...})`,
so a missing destination now actually routes to the error handler instead of falling
through to `recommendation_agent` (which previously only caught this internally, after
already having done the weather/disaster work).

**Verified:** `build_orchestrator_graph().compile()` succeeds and exposes the expected
node set (`validate_input`, `policy_check_node`, `location_resolver_node`,
`calendar_check_node`, `weather_disaster_node`, `recommendation_agent`, `planning_agent`,
`finalize_response`, `error`).

## 4. RAG module was fully implemented but never used

**Files:** [`app/workflows/recommendation_agent.py`](app/workflows/recommendation_agent.py)

**Problem:** `rag_service` / `Retriever` / `VectorStore` / `embeddings.py` were fully
built but nothing in `app/workflows/*` ever imported or called them.
`RecommendationAgent` fetched candidates straight from `db_tool` and handed the entire
unfiltered list to Gemini in one call — there was no embedding or vector-search step
anywhere in the actual request path, contradicting the project's stated use of RAG and
the `TripState` field comments (`candidate_hotels` etc. were documented as "raw RAG/API
candidates, pre-ranking" but were never populated either).

**Fix:** `RecommendationAgent.execute()` now:
1. Fetches candidates from `db_tool` as before, and stores them in
   `state.candidate_hotels` / `candidate_restaurants` / `candidate_attractions` /
   `candidate_events` (previously unused `TripState` fields).
2. Indexes that candidate pool into `rag_service` per category
   (`rag_service.index_candidate_data`).
3. Builds a query from `state.interests` + `state.travel_style` and retrieves the
   top-`k` (6) most relevant candidates per category via
   `rag_service.retrieve_candidates` (falling back to the unfiltered list if retrieval
   returns nothing).
4. Passes the RAG-retrieved subset — not the raw full list — to the LLM for final
   curation/ranking, same as before.

Added a small `_retrieve_or_fallback()` helper to unwrap retriever hits
(`{"item": ..., "metadata": ..., "score": ...}`) back into plain item dicts.

**Verified:** indexed mock Ella hotels via `rag_service.index_candidate_data` and queried
`"luxury relaxation resort"` — results correctly ranked the luxury/relaxation-tagged
hotel highest.

## 5. Hardcoded / inconsistent LLM model names

**Files:** [`app/workflows/recommendation_agent.py`](app/workflows/recommendation_agent.py),
[`app/workflows/planning_agent.py`](app/workflows/planning_agent.py)

**Problem:** `app/config/settings.py` defines `llm_model` / `llm_temperature` for exactly
this purpose, but both agents hardcoded their own values instead of reading from
`settings`. `RecommendationAgent` hardcoded `"gemini-3.6-flash"` — not a real published
Gemini model name — while `PlanningAgent` separately hardcoded `"gemini-2.0-flash"`, so
the two agents could silently drift out of sync with each other and with configuration.

**Fix:** Both agents now build `ChatGoogleGenerativeAI(model=settings.llm_model, temperature=settings.llm_temperature)`.

## 6. Duplicate `completed_steps` entries

**File:** [`app/workflows/orchestrator.py`](app/workflows/orchestrator.py)

**Problem:** `PolicyAgent`, `WeatherAgent`, `DisasterAgent`, and `CalendarAgent` each
append their own name to `state.completed_steps` inside `execute()`. The corresponding
orchestrator nodes (`policy_check_node`, `weather_disaster_node`, `calendar_check_node`)
then appended the same name again on success, producing duplicate entries for those four
steps (while `recommendation_agent` / `planning_agent`, which don't self-report, had only
one entry each).

**Fix:** Removed the redundant appends from the orchestrator nodes; the agents' own
`completed_steps` bookkeeping is now the single source of truth. Orchestrator nodes only
append to `state.errors` on failure.
