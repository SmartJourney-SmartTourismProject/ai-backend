"""
LangGraph-based orchestrator workflow for multi-agent trip planning.
Coordinates all agents and manages state transitions.
"""
from typing import Any, Dict, List, Optional, Literal
from langgraph.graph import StateGraph
from app.core.state import TripState
from app.core.result import AgentResult


def validate_input(state: TripState) -> Dict[str, Any]:
    """
    Validate user input for completeness.
    Returns state updates.
    """
    updates = {}
    if not state.user_input or not state.user_input.strip():
        updates["errors"] = state.errors + ["No user input provided."]
    return updates


async def policy_check_node(state: TripState) -> TripState:
    """
    Apply policy guardrails (rate limiting, blocked destinations, etc.).
    """
    from app.workflows.policy_agent import PolicyAgent
    
    agent = PolicyAgent()
    result = await agent.execute(state)

    # PolicyAgent.execute already appends "policy_agent" to completed_steps.
    if not result.success:
        state.errors.append(f"Policy Agent failed: {result.message}")
    
    return state


async def slot_filling_node(state: TripState) -> TripState:
    """
    Fills destination/duration_days/budget/travelers/interests from the raw
    user_input text via the LLM, for requests that only sent free text (the
    chat UI never sends structured fields). Only fills gaps - never
    overwrites values already provided explicitly.
    """
    from app.utils.slot_filling import fill_missing_slots

    await fill_missing_slots(state)
    state.completed_steps.append("slot_filling")
    return state


async def location_resolver_node(state: TripState) -> TripState:
    """
    Resolve user's start location via GPS → IP fallback → ask.
    """
    # If already have location, skip
    if state.start_location:
        state.completed_steps.append("location_resolver")
        return state
    
    
    state.completed_steps.append("location_resolver")
    return state


async def calendar_check_node(state: TripState) -> TripState:
    """
    Check user's calendar for free dates if trip_dates not provided.
    """
    from app.workflows.calendar_agent import CalendarAgent
    
    agent = CalendarAgent()
    result = await agent.execute(state)

    # CalendarAgent.execute already appends "calendar_agent" to completed_steps.
    if not result.success:
        state.errors.append(f"Calendar Agent failed: {result.message}")
    
    return state


async def weather_disaster_node(state: TripState) -> TripState:
    """
    Fetch weather and disaster data via ContextAgent, which resolves the
    destination's coordinates once and fetches both concurrently internally
    (see context_agent.py - merged from what were previously separate
    WeatherAgent/DisasterAgent classes).

    TripState is a plain Pydantic model with no reducers on its fields, so
    two graph-level edges converging on the same downstream node (e.g. both
    weather_check_node and disaster_check_node -> recommendation_agent) is
    unsafe in LangGraph: it either runs the downstream node twice or raises
    a concurrent-update conflict when both branches complete in the same
    super-step. A single node calling one combined agent sidesteps that
    entirely -- there's only ever a single edge into recommendation_agent.
    """
    from app.workflows.context_agent import ContextAgent

    result = await ContextAgent().execute(state)

    # ContextAgent.execute already appends "weather_agent"/"disaster_agent"
    # to completed_steps.
    if not result.success:
        state.errors.append(f"Weather/Disaster Agent failed: {result.message}")

    return state


def route_to_recommendation(state: TripState) -> str:
    """
    After weather/disaster fetch completes, route to recommendation agent.
    """
    if not state.destination:
        state.errors.append("Destination is required for recommendations.")
        return "error"
    
    return "recommendation_agent"


async def recommendation_agent_node(state: TripState) -> TripState:
    """
    Call RecommendationAgent to curate hotels/restaurants/attractions/events
    AND build the day-by-day itinerary in one LLM call (merged with what was
    previously a separate PlanningAgent call - see recommendation_agent.py's
    docstring for why that merge is safe here: every input PlanningAgent
    needed, e.g. weather/disaster, is already on `state` by this point in
    the graph). PlanningAgent itself still exists standalone for cases that
    want to re-plan without re-curating (e.g. a "regenerate itinerary" action).
    """
    from app.workflows.recommendation_agent import RecommendationAgent

    agent = RecommendationAgent()
    result = await agent.execute(state)

    if result.success:
        state.completed_steps.append("recommendation_agent")
    else:
        state.errors.append(f"Recommendation Agent failed: {result.message}")

    return state


def finalize_response(state: TripState) -> TripState:
    """
    Format final response for user.

    state.errors mixes hard failures with advisory notices (PolicyAgent
    prefixes non-blocking notices with "[WARNING]" - e.g. a low budget, or
    collecting a start location - see policy_agent.py). Only genuine
    failures should mark the whole plan as failed; if a real itinerary was
    still produced, show it with the advisories attached as notes instead of
    discarding a successful plan over a benign warning.
    """
    hard_failures = [e for e in state.errors if not e.startswith("[WARNING]")]
    warnings = [e for e in state.errors if e.startswith("[WARNING]")]

    if hard_failures and not state.itinerary:
        state.final_response = f"Planning failed with errors: {'; '.join(hard_failures)}"
    elif state.itinerary:
        state.final_response = format_itinerary_response(state)
        notes = hard_failures + warnings
        if notes:
            state.final_response += f"\n\n**Notes:** {'; '.join(notes)}"
    else:
        state.final_response = "No itinerary could be generated."

    state.completed_steps.append("finalize")
    return state


def format_itinerary_response(state: TripState) -> str:
    """
    Format the final itinerary as a readable string.
    """
    lines = [
        f"**Trip to {state.destination} ({state.duration_days} days)**",
        "",
    ]
    
    if state.estimated_cost:
        lines.append(f"**Estimated Cost: ${state.estimated_cost:.2f}**")
        lines.append("")
    
    if state.itinerary:
        for day_plan in state.itinerary:
            day = day_plan.get("day", "?")
            date = day_plan.get("date", "?")
            lines.append(f"## Day {day} ({date})")
            
            items = day_plan.get("items", [])
            for item in items:
                time = item.get("time", "TBD")
                item_type = item.get("type", "activity")
                name = item.get("name", "TBD")
                notes = item.get("notes", "")
                
                line = f"- **{time}** {item_type.title()}: {name}"
                if notes:
                    line += f" ({notes})"
                lines.append(line)
            
            lines.append("")
    
    if state.final_response and "budget_notes" not in state.final_response:
        lines.append(f"**Notes:** {state.final_response}")
    
    return "\n".join(lines)


def build_orchestrator_graph() -> StateGraph:
    """
    Build the LangGraph state machine for trip planning orchestration.
    
    Flow:
    1. validate_input
    2. policy_check_node
    3. slot_filling_node (LLM extracts destination/duration/budget/etc. from free text)
    4. location_resolver_node
    5. calendar_check_node
    6. weather_disaster_node (weather + disaster fetched concurrently via asyncio.gather)
    7. recommendation_agent_node (curates candidates AND builds the itinerary
       in one LLM call - see recommendation_agent.py's docstring)
    8. finalize_response
    """
    graph = StateGraph(TripState)

    # Add nodes
    graph.add_node("validate_input", validate_input)
    graph.add_node("policy_check_node", policy_check_node)
    graph.add_node("slot_filling_node", slot_filling_node)
    graph.add_node("location_resolver_node", location_resolver_node)
    graph.add_node("calendar_check_node", calendar_check_node)
    graph.add_node("weather_disaster_node", weather_disaster_node)
    graph.add_node("recommendation_agent", recommendation_agent_node)
    graph.add_node("finalize_response", finalize_response)
    graph.add_node("error", finalize_response)  # Error handler

    # Add edges - sequential flow
    graph.add_edge("policy_check_node", "slot_filling_node")
    graph.add_edge("slot_filling_node", "location_resolver_node")
    graph.add_edge("location_resolver_node", "calendar_check_node")
    graph.add_edge("calendar_check_node", "weather_disaster_node")

    # recommendation_agent now produces the itinerary too - straight to finalize.
    graph.add_edge("recommendation_agent", "finalize_response")

    # Set entry point
    graph.set_entry_point("validate_input")

    # Set finish point
    graph.set_finish_point("finalize_response")

    # Route to policy_check_node on valid input, or straight to the error
    # handler on invalid input. This must be the *only* outgoing edge from
    # validate_input -- an additional unconditional add_edge alongside this
    # conditional edge would fire regardless of validation errors and let
    # the whole pipeline run even on invalid input.
    graph.add_conditional_edges(
        "validate_input",
        lambda s: "error" if len(s.errors) > 0 else "policy_check_node",
        {"error": "error", "policy_check_node": "policy_check_node"}
    )

    # weather_disaster_node runs weather + disaster concurrently internally,
    # then routes to recommendation only if a destination is present --
    # no converging edges into recommendation_agent.
    graph.add_conditional_edges(
        "weather_disaster_node",
        route_to_recommendation,
        {"recommendation_agent": "recommendation_agent", "error": "error"}
    )

    return graph


# Create the compiled graph
def get_orchestrator_graph():
    """Get or compile the orchestrator graph."""
    graph_builder = build_orchestrator_graph()
    return graph_builder.compile()
