-- docs/db_migrations.sql
--
-- Schema additions needed by the AI backend's real (non-mock) Supabase
-- integration, written up for Member B to review and run against the
-- actual Supabase project - this repo has no direct DB access to apply
-- migrations itself, only the code that queries these tables once they
-- exist (app/tools/db_tool.py, app/tools/calendar_tool.py).
--
-- Every statement below is safe to run against an existing database:
-- CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS won't touch
-- anything that's already there.

-- ---------------------------------------------------------------------
-- 1. google_oauth_tokens
-- ---------------------------------------------------------------------
-- Requested in member_B.md. Backs app/tools/calendar_tool.py's
-- get_stored_credentials()/save_credentials() - previously an in-memory
-- dict, then an interim local JSON file (calendar_tokens.json), neither
-- of which survives a restart / works across multiple server instances.
-- One row per user; save_credentials() upserts on user_id.

CREATE TABLE IF NOT EXISTS google_oauth_tokens (
    user_id       uuid PRIMARY KEY REFERENCES "user"(id) ON DELETE CASCADE,
    access_token  text NOT NULL,
    refresh_token text,
    token_expiry  timestamptz,
    scope         text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 2. traveler_profile: two columns db_tool.get_user_profile() needs
-- ---------------------------------------------------------------------
-- The SAD's ER diagram (§9) already has traveler_profile with
-- preferred_language/preferred_currency/country_region/travel_style/
-- location_enabled/travel_interests. app/utils/slot_filling.py's §2
-- defaulting logic (destination-only request -> pull budget/home
-- location from the saved profile instead of re-asking) additionally
-- needs a default budget and a home base location, neither of which
-- exist in that diagram yet - adding them here rather than inventing a
-- separate table for two columns.

ALTER TABLE traveler_profile
    ADD COLUMN IF NOT EXISTS default_budget numeric,
    ADD COLUMN IF NOT EXISTS home_location  geography(Point, 4326);

-- Column mapping used by app/tools/db_tool.py's get_user_profile():
--   travel_interests -> profile["interests"]
--   travel_style      -> profile["travel_style"]
--   default_budget    -> profile["budget"]
--   home_location     -> profile["home_location"] = {"lat": ..., "lon": ...}
--                        (decoded from PostGIS EWKB the same way
--                        travel_listing.location already is)
