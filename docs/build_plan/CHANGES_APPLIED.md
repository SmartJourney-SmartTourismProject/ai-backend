# Changes Applied on `temp-shaluka`

This documents the fixes actually applied to this branch, following up on
[TEMP_SHALUKA_NEXT_STEPS.md](TEMP_SHALUKA_NEXT_STEPS.md)'s Part 1. Only fixes to code that already
existed on this branch were made — no new files (`disaster_tool.py`, the clarification/ask-back
branch, real tests, etc. from Part 2) were started, per instruction.

Files touched: `app/core/orchestrator.py`, `app/config/settings.py`, `app/tools/location_tool.py`,
`app/utils/slot_filling.py`.

---

## 1. `_respond_node` no longer hides a working itinerary behind an advisory error

**File:** `app/core/orchestrator.py`

Previously, any single entry in `state.errors` — even an advisory one like `location_unresolved`
(couldn't figure out where the traveler is starting from) — made `final_response` say "Sorry, I
ran into an issue," even when a real itinerary had been built.

Now `_respond_node` splits `state.errors` into hard failures and soft/advisory notes (currently
just `location_unresolved`, listed in `_SOFT_ERROR_PREFIXES`), and only blocks the response when
there's a hard failure **and** no itinerary was produced. Advisory notes are appended to a
successful response instead of replacing it:

```
Here's your trip plan for Kandy: 1 day(s) planned, estimated cost 500.0.

Note: location_unresolved: need to ask user for start location
```

Verified with `test_orchestrator.py`'s three scripted cases — all three now show a real itinerary
in `final_response` with the location note attached, instead of the itinerary being discarded.

---

## 2. `_context_node` no longer skips weather on the most common path

**File:** `app/core/orchestrator.py`

Previously, weather was only fetched when **both** `state.start_location` and `state.trip_dates`
were set. Since `trip_dates` only gets set when a calendar is connected (`state.user_id` present),
any request without a connected calendar — the common case — skipped weather entirely.

Now the `trip_dates` requirement is gone: if there's no calendar, it defaults to today +
`duration_days` (or 1 day) so weather still gets checked for some dates instead of being skipped.

**Deliberately not fixed here** (documented instead, in a comment in the code): this node still
uses `state.start_location` (where the traveler currently is) rather than the destination's
coordinates. Fixing that needs a destination → lat/lon lookup that doesn't exist on this branch —
that's `app/data/sri_lanka_districts.py` on Member B's side, or an equivalent built here — and
wiring in `disaster_tool.py` once it exists. Both are Part 2 items, intentionally not started.

---

## 3. Stray docstring removed from `app/config/settings.py`

**File:** `app/config/settings.py`

A comment block describing `location_tool.py`'s design decisions (the `ipapi.co` vs `ip-api.com`
tradeoff, why `client_gps`/`client_ip` are plain params, etc.) was sitting at the bottom of
`settings.py`, unrelated to anything in that file. Deleted it from `settings.py` and folded a
short version of the same rationale into `resolve_start_location`'s own docstring in
`app/tools/location_tool.py`, where it actually applies — nothing was lost, it just moved to the
right file.

---

## 4. `slot_filling.py` now asks for singular, lowercase interest tags

**File:** `app/utils/slot_filling.py`

The LLM extraction had no guidance on interest-tag *format*, so a message like "interested in
beaches" could extract `"beaches"` (plural) while downstream candidate data is tagged with the
singular `"beach"` — an exact-match lookup against that data would then silently return nothing,
even though matching listings exist. Added explicit formatting guidance in both the field
description and the system prompt:

```python
interests: List[str] = Field(
    default_factory=list,
    description=(
        "List of travel interests/activity types mentioned, as short, "
        "singular, lowercase tags (e.g. 'beach' not 'beaches', 'hike' not "
        "'hiking trips'). Empty list if none mentioned."
    ),
)
```

```
When listing interests, use short singular lowercase tags (e.g. "beach", "hike", "culture") -
not plurals or full phrases.
```

**Not changed:** the `model="gemini-3.6-flash"` hardcoded in `fill_slots()`. The original
suggestion was to read it from `settings.llm_model` instead, matching a pattern used elsewhere in
the project — but that field doesn't exist in this branch's `Settings` class, and nothing else on
this branch has the same hardcoded-model inconsistency to fix it against. Adding a new settings
field for a one-file consistency fix would be scope creep beyond what was asked here.

---

## Verification

| Check | Result |
|---|---|
| `app/core/orchestrator.py` imports and compiles the graph | Pass |
| `test_policy_guard.py` | 10/11 (unchanged from before — the one miss is a pre-existing blocklist gap, untouched by these changes) |
| `test_calendar_tool.py` | 4/4 (unchanged) |
| `test_orchestrator.py` (3 scripted cases, real Gemini calls) | All 3 now produce a real itinerary in `final_response` with the location note appended, instead of "Sorry, I ran into an issue" |
| `grep` for the removed docstring / stray content in `settings.py` | Clean |

## Not done (by design — see TEMP_SHALUKA_NEXT_STEPS.md Part 2)

- `app/tools/disaster_tool.py` — still doesn't exist.
- Destination-vs-origin coordinates in `_context_node` — needs a district lookup that isn't on
  this branch.
- The clarification/ask-back branch, §2 defaulting rules, OAuth CSRF fix, token persistence,
  weather/disaster caching, and converting `test_*.py` to real `pytest` — all still pending.
