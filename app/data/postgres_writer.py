"""
Shared synchronous PostgreSQL connection + upsert helpers for the batch
data-refresh scripts (hotel/restaurant ingestion, events ingestion).

These scripts run outside the FastAPI event loop (triggered by APScheduler or
manually), so they use sync psycopg2 rather than db_tool.py's async asyncpg
pool.

Every function no-ops gracefully when DATABASE_URL isn't configured, so the
ingestion scripts can still be run and tested without a live database.
"""
from __future__ import annotations
import logging
from typing import Any, Iterable, Optional, Union

from app.config.settings import settings

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False
    psycopg2 = None


def get_connection():
    """
    Opens a new psycopg2 connection, or returns None if the database isn't
    configured/reachable. Callers are responsible for closing it - these are
    short-lived batch scripts, so a pool would be overkill.
    """
    if not _PSYCOPG2_AVAILABLE or not settings.database_url:
        logger.warning("Database not configured; ingestion will run without persisting rows.")
        return None
    try:
        return psycopg2.connect(settings.database_url)
    except Exception as e:
        logger.error(f"Could not connect to the database: {e}")
        return None


def _fetch_id_map(table: str) -> dict[str, str]:
    """Shared helper for the district/category name -> id lookups."""
    conn = get_connection()
    if conn is None:
        return {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"SELECT name, id FROM {table}")  # noqa: S608 - table is a literal, not user input
            return {name: str(row_id) for name, row_id in cur.fetchall()}
    except Exception as e:
        logger.error(f"Failed to load {table} map: {e}")
        return {}
    finally:
        conn.close()


def get_district_id_map() -> dict[str, str]:
    """Returns {district_name: district_id}. {} if unavailable."""
    return _fetch_id_map("district")


def get_category_id_map() -> dict[str, str]:
    """Returns {category_name: category_id}. {} if unavailable."""
    return _fetch_id_map("category")


def to_point_wkt(lat: float, lon: float) -> str:
    """
    Converts lat/lon into the WKT text PostGIS accepts for a
    geography(Point,4326) column on insert (e.g. "POINT(80.63 7.29)" -
    note WKT order is (lon, lat), not (lat, lon)).

    Pass the owning column's name in upsert_rows(geo_columns=...) so the value
    gets wrapped in ST_GeogFromText() rather than bound as a plain string.
    """
    return f"POINT({lon} {lat})"


_column_cache: dict[tuple[str, str], bool] = {}


def has_column(table: str, column: str) -> bool:
    """
    Whether `column` exists on `table` (cached per process). Used to let
    ingestion scripts include optional columns (e.g. price_per_night) only
    once a migration has actually added them, instead of erroring.
    """
    key = (table, column)
    if key in _column_cache:
        return _column_cache[key]

    conn = get_connection()
    if conn is None:
        _column_cache[key] = False
        return False

    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                (table, column),
            )
            _column_cache[key] = cur.fetchone() is not None
    except Exception as e:
        logger.warning(f"Column probe for {table}.{column} failed: {e}")
        _column_cache[key] = False
    finally:
        conn.close()

    return _column_cache[key]


def upsert_rows(
    table: str,
    rows: list[dict[str, Any]],
    on_conflict: Union[str, Iterable[str]],
    geo_columns: Iterable[str] = (),
) -> int:
    """
    Upsert rows into `table`, matching on `on_conflict` - a single column
    name ("external_ref") or an iterable of columns for a compound unique
    constraint (("source", "external_ref"), matching travel_listing's real
    UNIQUE (source, external_ref)). Returns the number of rows written.
    No-ops (returns 0) if the database isn't configured, so ingestion can
    still be run/tested without one.

    `geo_columns` names the columns holding PostGIS WKT (from to_point_wkt).
    Those bind through ST_GeogFromText() instead of as plain text - Postgres
    will not implicitly cast a string into a geography column, so omitting a
    geography column here fails the insert. Plain list/array columns (e.g.
    tags text[]) need no special handling - psycopg2 adapts a Python list to
    a Postgres array automatically.

    All rows must share the same keys (the ingestion scripts build them from a
    fixed template, so they do).
    """
    if not rows:
        return 0

    conn = get_connection()
    if conn is None:
        return 0

    conflict_cols = [on_conflict] if isinstance(on_conflict, str) else list(on_conflict)
    geo = set(geo_columns)
    columns = list(rows[0].keys())
    # ST_GeogFromText() for geography columns, plain %s for everything else.
    placeholders = ", ".join(
        f"ST_GeogFromText(%s)" if col in geo else "%s" for col in columns
    )
    updates = ", ".join(
        f"{col} = EXCLUDED.{col}" for col in columns if col not in conflict_cols
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_cols)}) DO UPDATE SET {updates}"
    )

    try:
        with conn, conn.cursor() as cur:
            cur.executemany(sql, [tuple(row[col] for col in columns) for row in rows])
        return len(rows)
    except Exception as e:
        logger.error(f"Upsert into '{table}' failed: {e}")
        return 0
    finally:
        conn.close()
