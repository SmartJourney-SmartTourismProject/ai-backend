"""
Verify a bounded, quality-ranked slice of ingested listings per district.

Why this exists, and why it is not `verify_all_for_demo.py`:

`is_verified` is the human review gate between noisy OSM data and a real
traveller (backend/docs/BACKEND_PLAN.md §2). `verify_all_for_demo.py` flips
every row on, which its own docstring warns must not be done once NestJS's
admin panel exists - and it does exist now, with moderation endpoints and
tests. Bulk-verifying would silently bypass the control the admin panel is
there to provide.

This script keeps the gate meaningful while making the app usable: it
verifies at most TOP_N rows per (district, category), ranked by the quality
signals OSM actually carries, and it never touches a row a human already
decided on. Everything it does not pick stays unverified and waits for
review in the admin panel.

Ranking, best first. Note what is NOT used: `rating` is close to useless
here because it comes from Booking/Yelp enrichment, and only 5 of ~1,150
ingested OSM rows have one - ranking on it would verify almost nothing.

  +3  opening_hours present - the strongest signal in OSM that a POI is
      real, currently operating and actively maintained by a mapper
  +2  description present
  +1  has_public_transit - reachable without a car, which matters for a
      trip planner specifically
  +1  rating present - rare, but real when it is there

Rows whose name is missing, blank or shorter than 3 characters are never
picked: an unnamed node cannot be presented to a user.

    python -m app.data.verify_curated_subset            # apply
    python -m app.data.verify_curated_subset --dry-run  # show what would change
    python -m app.data.verify_curated_subset --top 5    # a tighter slice
"""
from __future__ import annotations

import argparse

from app.data.postgres_writer import get_connection

TOP_N = 10

# Ranked per (district, category); `qualified` filters out rows that could
# never be shown to a user regardless of rank.
_CANDIDATES_SQL = """
WITH qualified AS (
    SELECT l.id,
           l.district_id,
           l.category_id,
           l.name,
           (CASE WHEN l.opening_hours IS NOT NULL THEN 3 ELSE 0 END
          + CASE WHEN l.description   IS NOT NULL THEN 2 ELSE 0 END
          + CASE WHEN l.has_public_transit        THEN 1 ELSE 0 END
          + CASE WHEN l.rating        IS NOT NULL THEN 1 ELSE 0 END) AS score
    FROM travel_listing l
    WHERE l.is_verified = false
      AND l.is_active = true
      AND l.name IS NOT NULL
      AND length(trim(l.name)) >= 3
      AND l.latitude IS NOT NULL
      AND l.longitude IS NOT NULL
),
-- One branch per chain. OSM maps every outlet separately, so without this
-- a single chain swamps the slice: Colombo alone has Pizza Hut x27,
-- Barista x16 and Perera & Sons x16, and a "top 10 restaurants" that is
-- three Baristas is useless to a traveller. Keep the best-scoring branch
-- of each name and let the rest wait for review like anything else.
deduped AS (
    SELECT id, district_id, category_id, name, score,
           row_number() OVER (
               PARTITION BY district_id, category_id, lower(trim(name))
               ORDER BY score DESC, id ASC
           ) AS dup_rn
    FROM qualified
),
ranked AS (
    SELECT id, district_id, category_id, name, score,
           row_number() OVER (
               PARTITION BY district_id, category_id
               ORDER BY score DESC, name ASC
           ) AS rn
    FROM deduped
    WHERE dup_rn = 1
)
SELECT id, district_id, category_id, name, score
FROM ranked
WHERE rn <= %s
"""

_REPORT_SQL = """
SELECT d.name AS district, c.name AS category,
       count(*) FILTER (WHERE l.is_verified AND l.is_active) AS usable,
       count(*) AS total
FROM travel_listing l
JOIN district d ON d.id = l.district_id
JOIN category c ON c.id = l.category_id
GROUP BY d.name, c.name
HAVING count(*) FILTER (WHERE l.is_verified AND l.is_active) > 0
ORDER BY d.name, c.name
"""


def run(top_n: int = TOP_N, dry_run: bool = False) -> None:
    conn = get_connection()
    if conn is None:
        print("[FATAL] DATABASE_URL not configured or database unreachable.")
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_CANDIDATES_SQL, (top_n,))
            picked = cur.fetchall()
            if not picked:
                print("Nothing to verify - no unverified rows met the quality bar.")
                return

            print(f"Selected {len(picked)} row(s), up to {top_n} per district per category.")
            if dry_run:
                for _id, _d, _c, name, score in picked[:20]:
                    print(f"  score={score}  {name}")
                if len(picked) > 20:
                    print(f"  ... and {len(picked) - 20} more")
                print("\n[DRY RUN] Nothing written.")
                return

            # psycopg2 adapts a Python list of UUID-strings to text[], and
            # Postgres has no uuid = text operator, so the cast is required.
            cur.execute(
                "UPDATE travel_listing SET is_verified = true, updated_at = now() "
                "WHERE id = ANY(%s::uuid[])",
                ([str(row[0]) for row in picked],),
            )
            print(f"Verified {cur.rowcount} listing(s).")

            cur.execute(_REPORT_SQL)
            print("\nUsable catalogue now:")
            print(f"  {'district':<22} {'category':<12} {'usable':>6} / total")
            for district, category, usable, total in cur.fetchall():
                print(f"  {district:<22} {category:<12} {usable:>6} / {total}")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=TOP_N, help="max rows per district per category")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(args.top, args.dry_run)
