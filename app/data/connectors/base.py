"""
Shared connector protocol + district/tag lookup helpers, per
docs/master_plan/DATA_PLATFORM.md §5.1. Every connector implements
fetch/normalize/upsert and is driven by app/data/pipeline.py, never run
standalone in production (though each is still directly importable/runnable
for manual testing - see each connector's own __main__ block).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)


@dataclass
class District:
    id: str
    name: str          # "Kandy District" - matches OSM/Nominatim's own naming
    province: str
    lat: float
    lon: float
    osm_relation_id: Optional[int] = None


class Connector(Protocol):
    name: str
    cadence: Literal["daily", "weekly", "monthly", "quarterly", "manual"]
    requires_key: bool
    scope: Literal["per_district", "global"]

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]: ...
    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]: ...
    def upsert(self, rows: list[dict[str, Any]]) -> int: ...


def fetch_all_districts() -> list[District]:
    """Every seeded district, with centroid coordinates - what per-district
    connectors iterate over. Empty list (not an exception) if the database
    isn't reachable or Phase 1's seed hasn't run yet."""
    conn = get_connection()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, province, ST_Y(center::geometry), ST_X(center::geometry), "
                "osm_relation_id FROM district ORDER BY name"
            )
            return [
                District(id=str(r[0]), name=r[1], province=r[2], lat=r[3], lon=r[4], osm_relation_id=r[5])
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.error(f"fetch_all_districts failed: {e}")
        return []
    finally:
        conn.close()


def fetch_tag_mapping(source: str) -> dict[str, list[str]]:
    """{source_key: [tags]} for one source ("osm"), e.g.
    {"tourism=hotel": ["stay"], "amenity=restaurant": ["food"]}."""
    conn = get_connection()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT source_key, tag FROM tag_mapping WHERE source = %s", (source,))
            out: dict[str, list[str]] = {}
            for source_key, tag in cur.fetchall():
                out.setdefault(source_key, []).append(tag)
            return out
    except Exception as e:
        logger.error(f"fetch_tag_mapping failed: {e}")
        return {}
    finally:
        conn.close()


def fetch_category_id_map() -> dict[str, str]:
    conn = get_connection()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT name, id FROM category")
            return {name: str(cid) for name, cid in cur.fetchall()}
    except Exception as e:
        logger.error(f"fetch_category_id_map failed: {e}")
        return {}
    finally:
        conn.close()


def ensure_categories(names: list[str]) -> dict[str, str]:
    """Idempotently ensures each category name exists, then returns the
    full {name: id} map. category has no seed script of its own since it's
    exactly 3 fixed rows - simplest to guarantee here, on first connector run."""
    conn = get_connection()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            for name in names:
                cur.execute(
                    "INSERT INTO category (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
                )
        conn.commit()
    except Exception as e:
        logger.error(f"ensure_categories failed: {e}")
    finally:
        conn.close()
    return fetch_category_id_map()
