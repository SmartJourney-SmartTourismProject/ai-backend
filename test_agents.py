import asyncio
import sys
sys.path.insert(0, '.')

async def test_agents():
    from app.workflows.context_agent import ContextAgent
    from app.workflows.policy_agent import PolicyAgent
    from app.workflows.calendar_agent import CalendarAgent
    from app.core.state import TripState
    from app.core.result import AgentResult
    
    state = TripState(
        user_input='Plan a 3-day trip to Ella',
        destination='Ella',
        duration_days=3,
        budget=800,
        travelers=2,
        interests=['hiking'],
    )
    
    print('Testing ContextAgent (weather + disaster, merged)...')
    ctx = ContextAgent()
    result = await ctx.execute(state)
    wkeys = list(state.weather.keys()) if state.weather else None
    alert_count = len(state.disaster.get('alerts', [])) if state.disaster else 0
    print('  Context: success=' + str(result.success) + ', weather_keys=' + str(wkeys) + ', alerts=' + str(alert_count))
    
    print('Testing PolicyAgent...')
    pa = PolicyAgent()
    result = await pa.execute(state)
    print('  Policy: success=' + str(result.success))
    
    print('Testing CalendarAgent...')
    ca = CalendarAgent()
    result = await ca.execute(state)
    print('  Calendar: success=' + str(result.success) + ', dates=' + str(state.trip_dates))
    
    print('All agents working!')

asyncio.run(test_agents())