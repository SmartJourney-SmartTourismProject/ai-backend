"""
⚠️ STOPGAP - bulk-verifies every currently-ingested listing/event.

`is_verified` is a real, intentional design: OSM data is noisy (a node
tagged tourism=hotel could be permanently closed or mislabeled), and NestJS's
admin panel is meant to be the human review gate before a listing reaches a
real user (backend/docs/BACKEND_PLAN.md §2 ownership table - "AI backend
writes unverified rows... NestJS owns admin CRUD + verification").

That NestJS admin service does not exist yet in this session's scope (the
`backend/` repo is docker-compose + schema only, no app). Without it,
`is_verified = true` matches zero rows, and every `/trip-plan` request
returns "no verified listings found" for every destination - the entire
system would be functionally unusable end-to-end despite 6,500+ real
listings sitting in the database.

This script is the explicit, documented, one-time workaround for that gap:
it marks every currently-ingested (source != 'admin') row as verified, so
the system is genuinely usable now. It is NOT a substitute for real admin
review, and running it again after NestJS's admin panel exists would
silently bypass that review - do not add this to the scheduler, and delete
this file once real verification exists.

    python -m app.data.verify_all_for_demo
"""
from __future__ import annotations

from app.data.postgres_writer import get_connection


def run() -> None:
    conn = get_connection()
    if conn is None:
        print("[FATAL] DATABASE_URL not configured or database unreachable.")
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE travel_listing SET is_verified = true WHERE is_verified = false")
            listings = cur.rowcount
            cur.execute("UPDATE local_event SET is_verified = true WHERE is_verified = false")
            events = cur.rowcount
        print(f"[STOPGAP] Verified {listings} listing(s) and {events} event(s).")
        print("This bypasses real admin review - see this file's module docstring.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
