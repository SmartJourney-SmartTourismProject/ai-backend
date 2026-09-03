Phase 1 — done

TripState (app/core/state.py) — added user_id, start_location, trip_dates, disaster, and events (the AI-output counterpart to candidate_events).

settings.py — added the four new API key fields, gave empty-string defaults to everything except gemini_api_key so you're not blocked on signups.

.env.example — added the four missing key placeholders; flagged GOOGLE_MAPS_API_KEY as possibly a leftover, worth confirming with the team.

db_tool.py mock (Member B's file, but you worked through it together) — filled in the missing get_transit_info, check_pickme_coverage, get_user_profile, plus brought the listing/event dict shapes up to the full §4 contract.