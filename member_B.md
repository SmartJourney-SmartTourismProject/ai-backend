## Member B TODO: Google Calendar OAuth token storage

`calendar_tool.py` (Member A) needs a place to persist each user's Google
OAuth credentials (access token + refresh token) after they authorize.
Not in the current ER diagram / db_tool.py contract.

Needed: a new table, e.g. `google_oauth_tokens`, with at minimum:
- user_id (FK to User)
- access_token
- refresh_token
- token_expiry
- scope

And two functions added to db_tool.py (or wherever DB access lives),
matching this signature so calendar_tool.py doesn't need to change:

    async def get_stored_credentials(user_id: str) -> dict | None: ...
    async def save_credentials(user_id: str, creds: dict) -> None: ...

Until this exists, Member A is mocking these in-memory / returning None,
which makes get_free_days() fall through to [] (its designed no-calendar
fallback) — so nothing is blocked, but calendar integration won't
actually persist across sessions until this table exists.

## Member A note: Gemini model name updated

app/utils/slot_filling.py now uses "gemini-3.6-flash" instead of
"gemini-2.5-flash" — the 2.5 model returned a 404 (no longer available
to new API keys). If Recommendation/Planner agents reference
gemini-2.5-flash or another deprecated model name anywhere, worth
checking and updating those too.