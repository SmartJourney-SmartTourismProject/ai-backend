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
    
    if result.success:
        state.completed_steps.append("policy_agent")
    else:
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
    
    # TODO: Implement actual location resolution (GPS, IP fallback)
    # For now, just mark as complete and continue
    state.completed_steps.append("location_resolver")
    return state


async def calendar_check_node(state: TripState) -> TripState:
    """
    Check user's calendar for free dates if trip_dates not provided.
    """
    from app.workflows.calendar_agent import CalendarAgent
    
    agent = CalendarAgent()
    result = await agent.execute(state)
    
    if result.success:
        state.completed_steps.append("calendar_agent")
    else:
        state.errors.append(f"Calendar Agent failed: {result.message}")
    
    return state


async def weather_check_node(state: TripState) -> TripState:
    """
    Fetch weather forecast for destination and trip dates.
    """
    from app.workflows.weather_agent import WeatherAgent
    
    agent = WeatherAgent()
    result = await agent.execute(state)
    
    if result.success:
        state.completed_steps.append("weather_agent")
    else:
        state.errors.append(f"Weather Agent failed: {result.message}")
    
    return state


async def disaster_check_node(state: TripState) -> TripState:
    """
    Fetch disaster alerts (earthquakes, floods, etc.) for destination.
    """
    from app.workflows.disaster_agent import DisasterAgent
    
    agent = DisasterAgent()
    result = await agent.execute(state)
    
    if result.success:
        state.completed_steps.append("disaster_agent")
    else:
        state.errors.append(f"Disaster Agent failed: {result.message}")
    
    return state


def route_to_recommendation(state: TripState) -> str:
    """
    After weather/disaster parallel execution, route to recommendation agent.
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
    5. weather_check_node (parallel with disaster_check_node)
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
    graph.add_node("weather_check_node", weather_check_node)
    graph.add_node("disaster_check_node", disaster_check_node)
    graph.add_node("recommendation_agent", recommendation_agent_node)
    graph.add_node("planning_agent", planning_agent_node)
    graph.add_node("finalize_response", finalize_response)
    graph.add_node("error", finalize_response)  # Error handler
    
    # Add edges - sequential flow
    graph.add_edge("validate_input", "policy_check_node")
    graph.add_edge("policy_check_node", "location_resolver_node")
    graph.add_edge("location_resolver_node", "calendar_check_node")
    graph.add_edge("calendar_check_node", "weather_check_node")
    graph.add_edge("calendar_check_node", "disaster_check_node")
    
    # After weather and disaster complete, go to recommendation
    graph.add_edge("weather_check_node", "recommendation_agent")
    graph.add_edge("disaster_check_node", "recommendation_agent")
    
    # Then planning and finalize
    graph.add_edge("recommendation_agent", "planning_agent")
    graph.add_edge("planning_agent", "finalize_response")
    
    # Set entry point
    graph.set_entry_point("validate_input")
    
    # Set finish point
    graph.set_finish_point("finalize_response")
    
    # Add conditional routing for validation errors
    graph.add_conditional_edges(
        "validate_input",
        lambda s: "error" if len(s.errors) > 0 else "policy_check_node",
        {"error": "error", "policy_check_node": "policy_check_node"}
    )
    
    # Add normal edges from validate_input
    graph.add_edge("validate_input", "policy_check_node")
    
    return graph


# Create the compiled graph
def get_orchestrator_graph():
    """Get or compile the orchestrator graph."""
    graph_builder = build_orchestrator_graph()
    return graph_builder.compile()
