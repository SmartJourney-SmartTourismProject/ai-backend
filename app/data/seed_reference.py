"""
One-time (then manual-cadence) seed for reference tables the connectors and
scorer depend on: tag_vocabulary/tag_mapping (docs/master_plan/DATA_PLATFORM.md
§5.3) and cost_reference (§6). Neither is a per-district live-fetched
connector - both are small, curated, admin-editable CSVs checked into the
repo.

Run once, then whenever app/data/tag_mapping.csv or cost_reference.csv change:

    python -m app.data.seed_reference
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from app.data.postgres_writer import get_connection, get_district_id_map

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent

# Canonical interest tags with whether each is weather-sensitive (drives the
# "no outdoor items on a rainy day" validator rule, DETERMINISM_AND_VALIDATION.md §5).
_TAG_VOCABULARY = [
    ("stay", "Accommodation", False),
    ("sightseeing", "Sightseeing", False),
    ("views", "Scenic Views", True),
    ("culture", "Culture & Heritage", False),
    ("wildlife", "Wildlife", True),
    ("family", "Family-Friendly", False),
    ("beach", "Beach", True),
    ("nature", "Nature", True),
    ("hike", "Hiking", True),
    ("history", "History", False),
    ("food", "Food & Dining", False),
    ("local_food", "Local Cuisine", False),
]

_TAG_VOCAB_UPSERT = """
    INSERT INTO tag_vocabulary (tag, label, is_outdoor) VALUES (%s, %s, %s)
    ON CONFLICT (tag) DO UPDATE SET label = EXCLUDED.label, is_outdoor = EXCLUDED.is_outdoor
"""
_TAG_MAPPING_UPSERT = """
    INSERT INTO tag_mapping (source, source_key, tag) VALUES (%s, %s, %s)
    ON CONFLICT (source, source_key, tag) DO NOTHING
"""
_COST_REFERENCE_UPSERT = """
    INSERT INTO cost_reference (district_id, category, price_level, unit, typical_cost, currency, source_note)
    VALUES (%s, %s, %s, %s, %s, 'LKR', %s)
    ON CONFLICT (district_id, category, price_level, unit) DO UPDATE SET
        typical_cost = EXCLUDED.typical_cost, source_note = EXCLUDED.source_note, updated_at = now()
"""


def seed_tags(conn) -> tuple[int, int]:
    vocab_count = mapping_count = 0
    with conn, conn.cursor() as cur:
        for tag, label, is_outdoor in _TAG_VOCABULARY:
            cur.execute(_TAG_VOCAB_UPSERT, (tag, label, is_outdoor))
            vocab_count += 1

        mapping_path = _DATA_DIR / "tag_mapping.csv"
        with mapping_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cur.execute(_TAG_MAPPING_UPSERT, (row["source"], row["source_key"], row["tag"]))
                mapping_count += 1
    return vocab_count, mapping_count


def seed_costs(conn) -> int:
    district_ids = get_district_id_map()
    count = 0
    with conn, conn.cursor() as cur:
        cost_path = _DATA_DIR / "cost_reference.csv"
        with cost_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                district_name = row["district"].strip()
                district_id = district_ids.get(f"{district_name} District") if district_name else None
                if district_name and district_id is None:
                    logger.warning("cost_reference row for unknown district '%s' - storing as national", district_name)
                cur.execute(_COST_REFERENCE_UPSERT, (
                    district_id, row["category"], int(row["price_level"]), row["unit"],
                    float(row["typical_cost"]), row["source_note"] or None,
                ))
                count += 1
    return count


def run_seed() -> None:
    print("--- SEEDING tag_vocabulary, tag_mapping, cost_reference ---")
    conn = get_connection()
    if conn is None:
        print("[FATAL] DATABASE_URL not configured or database unreachable.")
        return
    try:
        vocab_count, mapping_count = seed_tags(conn)
        print(f"  tag_vocabulary: {vocab_count} tags")
        print(f"  tag_mapping: {mapping_count} source-key mappings")
        cost_count = seed_costs(conn)
        print(f"  cost_reference: {cost_count} rows")
    finally:
        conn.close()
    print("[SUCCESS] Reference data seeded.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_seed()
