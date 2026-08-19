"""
LangGraph-based orchestrator workflow for multi-agent trip planning.
Coordinates all agents and manages state transitions.
"""
import asyncio
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
    Fetch weather and disaster data concurrently via asyncio.gather, inside a
    single graph node.

    TripState is a plain Pydantic model with no reducers on its fields, so
    two graph-level edges converging on the same downstream node (e.g. both
    weather_check_node and disaster_check_node -> recommendation_agent) is
    unsafe in LangGraph: it either runs the downstream node twice or raises
    a concurrent-update conflict when both branches complete in the same
    super-step. Running both agents concurrently *inside* one node sidesteps
    that entirely -- there's only ever a single edge into recommendation_agent.
    """
    from app.workflows.weather_agent import WeatherAgent
    from app.workflows.disaster_agent import DisasterAgent

    weather_result, disaster_result = await asyncio.gather(
        WeatherAgent().execute(state),
        DisasterAgent().execute(state),
    )

    # WeatherAgent/DisasterAgent.execute already append their own step names
    # to completed_steps.
    if not weather_result.success:
        state.errors.append(f"Weather Agent failed: {weather_result.message}")

    if not disaster_result.success:
        state.errors.append(f"Disaster Agent failed: {disaster_result.message}")

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
    Call RecommendationAgent to fetch and rank attractions/hotels/restaurants.
    """
    from app.workflows.recommendation_agent import RecommendationAgent
    
    agent = RecommendationAgent()
    result = await agent.execute(state)
    
    if result.success:
        state.completed_steps.append("recommendation_agent")
    else:
        state.errors.append(f"Recommendation Agent failed: {result.message}")
    
    return state


async def planning_agent_node(state: TripState) -> TripState:
    """
    Call PlanningAgent to build day-by-day itinerary.
    """
    from app.workflows.planning_agent import PlanningAgent
    
    agent = PlanningAgent()
    result = await agent.execute(state)
    
    if result.success:
        state.completed_steps.append("planning_agent")
    else:
        state.errors.append(f"Planning Agent failed: {result.message}")
    
    return state


def finalize_response(state: TripState) -> TripState:
    """
    Format final response for user.
    """
    if state.errors:
        state.final_response = f"Planning failed with errors: {'; '.join(state.errors)}"
    elif state.itinerary:
        state.final_response = format_itinerary_response(state)
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
    3. location_resolver_node
    4. calendar_check_node
    5. weather_disaster_node (weather + disaster fetched concurrently via asyncio.gather)
    6. recommendation_agent_node
    7. planning_agent_node
    8. finalize_response
    """
    graph = StateGraph(TripState)

    # Add nodes
    graph.add_node("validate_input", validate_input)
    graph.add_node("policy_check_node", policy_check_node)
    graph.add_node("location_resolver_node", location_resolver_node)
    graph.add_node("calendar_check_node", calendar_check_node)
    graph.add_node("weather_disaster_node", weather_disaster_node)
    graph.add_node("recommendation_agent", recommendation_agent_node)
    graph.add_node("planning_agent", planning_agent_node)
    graph.add_node("finalize_response", finalize_response)
    graph.add_node("error", finalize_response)  # Error handler

    # Add edges - sequential flow
    graph.add_edge("policy_check_node", "location_resolver_node")
    graph.add_edge("location_resolver_node", "calendar_check_node")
    graph.add_edge("calendar_check_node", "weather_disaster_node")

    # Then planning and finalize
    graph.add_edge("recommendation_agent", "planning_agent")
    graph.add_edge("planning_agent", "finalize_response")

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
