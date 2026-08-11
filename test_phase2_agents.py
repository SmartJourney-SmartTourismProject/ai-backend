import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from app.core.state import TripState
from app.workflows.recommendation_agent import RecommendationAgent
from app.workflows.planning_agent import PlanningAgent

async def run_checkpoint():
    print("--- STARTING PHASE 2 CHECKPOINT ---")
    
    # 1. Create a hardcoded TripState (simulating what the Orchestrator would do)
    
    
    # 1. Create a hardcoded TripState (simulating what the Orchestrator would do)
    state = TripState(
        user_input="Plan a 2-day trip to Kandy for 2 people under $300.", # 👈 ADD THIS LINE
        user_id="test_user_1",
        destination="Kandy",
        duration_days=2,
        travelers=2,
        budget=300.0,
        interests=["nature", "history", "food"],
        weather={
            "condition": "Clear",
            "forecast": [
                {"date": "2026-08-20", "rain_probability": 0.1},
                {"date": "2026-08-21", "rain_probability": 0.1}
            ]
        }
    )
    # Adding disaster data manually as required by the build plan
    setattr(state, "disaster", {"safe": True, "active_events": []})

    # 2. Run Recommendation Agent
    print("\n=> 1. Running Recommendation Agent...")
    rec_agent = RecommendationAgent()
    rec_result = await rec_agent.execute(state)
    
    print(f"Result: {rec_result.success} - {rec_result.message}")
    print(f"Hotels found: {len(state.hotels)}")
    print(f"Restaurants found: {len(state.restaurants)}")
    print(f"Attractions found: {len(state.attractions)}")
    print(f"Events found: {len(state.events or [])}")
    
    if not rec_result.success:
        print("Stopping test due to Recommendation Agent failure.")
        print("Errors:", state.errors)
        return

    # 3. Run Planning Agent
    print("\n=> 2. Running Planning Agent...")
    plan_agent = PlanningAgent()
    plan_result = await plan_agent.execute(state)

    print(f"Result: {plan_result.success} - {plan_result.message}")
    print(f"Estimated Cost: ${state.estimated_cost}")
    print(f"Budget Notes: {state.final_response}")
    
    print("\n=> 3. Generated JSON Itinerary:")
    print(json.dumps(state.itinerary, indent=2))

    # 4. Final Verdict
    if plan_result.success and state.itinerary:
        print("\n[PASS] PHASE 2 CHECKPOINT PASSED! Both LLMs connected and parsed correctly.")
    else:
        print("\n[FAIL] PHASE 2 CHECKPOINT FAILED!")
        print("Errors:", state.errors)

if __name__ == "__main__":
    asyncio.run(run_checkpoint())