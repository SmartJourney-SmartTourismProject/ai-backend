"""
Shared synchronous Supabase client + upsert helpers for the batch data-refresh
scripts (hotel/restaurant ingestion, events ingestion). These scripts run
outside the FastAPI event loop (triggered by APScheduler or manually), so they
use the plain sync `supabase` client rather than `db_tool.py`'s async one.
"""
from __future__ import annotations
import logging
from typing import Any, Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)

try:
    from supabase import create_client, Client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    create_client = None
    Client = None

_client: Optional["Client"] = None


def get_client() -> Optional["Client"]:
    """Lazily create and cache a single sync Supabase client for this process."""
    global _client
    if not _SUPABASE_AVAILABLE or not settings.supabase_url or not settings.supabase_key:
        logger.warning("Supabase not configured; ingestion will run without persisting rows.")
        return None
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_key)
    return _client


def get_district_id_map() -> dict[str, str]:
    """Returns {district_name: district_id} from Supabase's `district` table. {} if unavailable."""
    client = get_client()
    if client is None:
        return {}
    try:
        resp = client.table("district").select("id,name").execute()
        return {row["name"]: row["id"] for row in resp.data}
    except Exception as e:
        logger.error(f"Failed to load district map: {e}")
        return {}


def get_category_id_map() -> dict[str, str]:
    """Returns {category_name: category_id} from Supabase's `category` table. {} if unavailable."""
    client = get_client()
    if client is None:
        return {}
    try:
        resp = client.table("category").select("id,name").execute()
        return {row["name"]: row["id"] for row in resp.data}
    except Exception as e:
        logger.error(f"Failed to load category map: {e}")
        return {}


def to_point_wkt(lat: float, lon: float) -> str:
    """
    Converts lat/lon into the WKT text Postgres/PostGIS accepts for a
    geography(Point,4326) column on insert (e.g. "POINT(80.63 7.29)" -
    note WKT order is (lon, lat), not (lat, lon)).
    """
    return f"POINT({lon} {lat})"


_column_cache: dict[tuple[str, str], bool] = {}


def has_column(table: str, column: str) -> bool:
    """
    Cheap probe for whether `column` exists on `table` (cached per process).
    Used to let ingestion scripts include optional columns (e.g. price_per_night)
    only once a migration has actually added them, instead of erroring.
    """
    key = (table, column)
    if key in _column_cache:
        return _column_cache[key]

    client = get_client()
    if client is None:
        _column_cache[key] = False
        return False

    try:
        client.table(table).select(column).limit(1).execute()
        _column_cache[key] = True
    except Exception:
        _column_cache[key] = False
    return _column_cache[key]


def upsert_rows(table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
    """
    Upsert rows into `table`, matching on `on_conflict` (e.g. "external_ref").
    Returns the number of rows sent. No-ops (returns 0) if Supabase isn't configured,
    so ingestion scripts can still be run/tested without a live database.
    """
    if not rows:
        return 0

    client = get_client()
    if client is None:
        return 0

    try:
        client.table(table).upsert(rows, on_conflict=on_conflict).execute()
        return len(rows)
    except Exception as e:
        logger.error(f"Upsert into '{table}' failed: {e}")
        return 0
