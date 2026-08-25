import asyncio
import sys
sys.path.insert(0, '.')
from app.workflows.orchestrator import get_orchestrator_graph
from app.core.state import TripState

async def test():
    state = TripState(
        user_input='Plan a 3-day trip to Ella for hiking and local food',
        destination='Ella',
        duration_days=3,
        budget=800,
        travelers=2,
        interests=['hiking', 'nature', 'local cuisine'],
        travel_style='adventure'
    )
    
    graph = get_orchestrator_graph()
    print('Graph compiled successfully')
    
    result = await graph.ainvoke(state)
    print('Graph execution completed')
    
    if hasattr(result, 'errors'):
        print('Errors:', result.errors)
    if hasattr(result, 'completed_steps'):
        print('Completed steps:', result.completed_steps)
    if hasattr(result, 'final_response'):
        print('Final response:', result.final_response[:500] if result.final_response else 'None')
    if hasattr(result, 'itinerary'):
        print('Itinerary items:', len(result.itinerary))
    if hasattr(result, 'recommendations'):
        print('Recommendations:', len(result.recommendations))
    if hasattr(result, 'weather'):
        print('Weather:', 'present' if result.weather else 'none')
    if hasattr(result, 'disaster'):
        print('Disaster:', 'present' if result.disaster else 'none')

asyncio.run(test())
print('Orchestrator test done!')