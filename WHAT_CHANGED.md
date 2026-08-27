# What Changed on This Branch

This document explains, in plain terms, what was removed from this branch, what will take its
place, and what Member A still needs to build so everything connects properly.

---

## 1. Why anything was deleted

This branch (Member B's work) is only supposed to contain:
- the database tool (`db_tool.py`)
- the Recommendation Agent and Planner Agent
- the data pipeline that fills the database (Overpass, events, etc.)
- the RAG (search) system
- the scheduler that refreshes data automatically

But by accident, this branch also had a **second copy** of Member A's part of the project — her
router/orchestrator, her policy checker, her calendar tool, and her weather/disaster tool — just
built independently and put in different files. Having two different versions of the same thing
would cause conflicts when the two branches are combined, so the copies were removed.

---

## 2. Files that were deleted

| File deleted | What it did |
|---|---|
| `app/workflows/orchestrator.py` | Ran the whole trip-planning process step by step (the "brain" that calls every other tool in order) |
| `app/workflows/policy_agent.py` | Checked if a request was allowed (blocked destinations, budget limits, etc.) |
| `app/workflows/calendar_agent.py` | Looked at the user's calendar to suggest free travel dates |
| `app/workflows/context_agent.py` | Got the weather forecast and any disaster alerts (floods, earthquakes, etc.) for the destination |
| `app/utils/slot_filling.py` | Used AI to pull details like destination, budget, and number of travelers out of a plain sentence |
| `test_orchestrator.py` | A test file, only used to test the deleted orchestrator |
| `test_agents.py` | A test file, only used to test the deleted policy/calendar/weather-disaster files |

None of these were needed by Member B's actual work (Recommendation Agent, Planner Agent, database
tool, data pipeline). That was checked carefully before removing anything.

---

## 3. What replaces each deleted file

Member A is already building her own, real versions of these — just under different file names.
Here is the exact swap:

| Deleted file (old, on this branch) | Replaced by (Member A's real file) |
|---|---|
| `app/workflows/orchestrator.py` | `app/core/orchestrator.py` |
| `app/workflows/policy_agent.py` | `app/utils/policy_guard.py` |
| `app/workflows/calendar_agent.py` | `app/tools/calendar_tool.py` |
| `app/workflows/context_agent.py` | Split into **two** files: `app/tools/weather_tool.py` and `app/tools/disaster_tool.py` |
| `app/utils/slot_filling.py` | Same name and same location — `app/utils/slot_filling.py` (Member A already has this exact file, so this one really was just a duplicate) |

Also, Member A has a few files that never existed on this branch at all, but will need to be added
once the two branches are combined:

| New file coming from Member A | What it does |
|---|---|
| `app/tools/location_tool.py` | Figures out where the user is starting their trip from (GPS, then IP address, then just asks) |
| `app/api/trip.py` | The actual web endpoint (`POST /trip-plan`) that a phone/website app would call |
| `app/api/google_oauth.py` | Lets a user connect their Google Calendar |

---

## 4. One thing Member A hasn't built yet

`app/tools/disaster_tool.py` (the earthquake/flood/storm warning checker) does **not exist yet**,
even on Member A's own branch. It's the one piece nobody has built so far.

The good news: the deleted `app/workflows/context_agent.py` already had a working version of this
logic (it just also did the weather part in the same file, which needs to be split apart). It can
still be recovered if needed with this command:

```
git show ce5eb4f:app/workflows/context_agent.py
```

That gives Member A a working reference for the earthquake (USGS), wildfire/storm (NASA EONET),
and flood/cyclone (GDACS) checks she still needs to write in her own `disaster_tool.py`.

---

## 5. What still needs to happen for everything to connect

1. Add Member A's real files (`app/core/orchestrator.py`, `app/tools/*.py`,
   `app/utils/policy_guard.py`, `app/utils/slot_filling.py`, `app/api/*.py`) into this project.
2. Write the missing `app/tools/disaster_tool.py`.
3. Make sure Member A's orchestrator calls Member B's `RecommendationAgent` and `PlanningAgent`
   at the right step — the code for those two agents doesn't need to change at all, they're
   already written to expect exactly what Member A's tools will hand them (destination, weather,
   disaster info, etc.).
4. Merge the two versions of `app/config/settings.py` — right now each branch added different
   settings (API keys, feature flags) to it, and both sets are needed.
5. Update the shared `.env` file so it has all the API keys both people's code needs, not just one
   side's.

Once those five things are done, `main.py`'s temporary shortcut (currently calling the
Recommendation Agent directly, skipping all of Member A's steps) can be replaced with the real,
full pipeline: check policy → find location → check calendar → get weather/disaster → recommend →
plan → respond.
