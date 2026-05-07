"""
PostgreSQL compatibility helpers for v2_api.

All v2_api Python files should import from this module to ensure
MySQL/Postgres compatibility.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def is_postgresql(engine: Engine) -> bool:
    """Check if engine is PostgreSQL."""
    try:
        return engine.dialect.name == "postgresql"
    except Exception:
        return False


def sql_auto_increment(engine: Engine) -> str:
    """Return AUTO_INCREMENT or SERIAL based on dialect."""
    return "SERIAL" if is_postgresql(engine) else "INT AUTO_INCREMENT"


def sql_datetime(engine: Engine) -> str:
    """Return TIMESTAMP or DATETIME based on dialect."""
    return "TIMESTAMP" if is_postgresql(engine) else "DATETIME"


def sql_longtext(engine: Engine) -> str:
    """Return TEXT or LONGTEXT based on dialect."""
    return "TEXT" if is_postgresql(engine) else "LONGTEXT"


def sql_bool_true(engine: Engine) -> str:
    """Return TRUE or 1 based on dialect."""
    return "TRUE" if is_postgresql(engine) else "1"


def sql_bool_false(engine: Engine) -> str:
    """Return FALSE or 0 based on dialect."""
    return "FALSE" if is_postgresql(engine) else "0"


def sql_on_duplicate_key_update(engine: Engine, updates: str) -> str:
    """
    Return ON CONFLICT ... DO UPDATE or ON DUPLICATE KEY UPDATE based on dialect.
    
    Args:
        engine: SQLAlchemy engine
        updates: Update clause (e.g., "col1=VALUES(col1), col2=VALUES(col2)")
    
    Returns:
        Complete upsert clause for the dialect
    
    Example:
        # MySQL: ON DUPLICATE KEY UPDATE col1=VALUES(col1)
        # Postgres: ON CONFLICT DO UPDATE SET col1=EXCLUDED.col1
    """
    if is_postgresql(engine):
        # Convert MySQL VALUES() to Postgres EXCLUDED
        pg_updates = updates.replace("VALUES(", "EXCLUDED.")
        pg_updates = pg_updates.replace(")", "")
        return f"ON CONFLICT DO UPDATE SET {pg_updates}"
    else:
        return f"ON DUPLICATE KEY UPDATE {updates}"


def sql_upsert_heartbeat(engine: Engine, table: str = "tracker_heartbeat") -> str:
    """Return complete upsert SQL for heartbeat table."""
    if is_postgresql(engine):
        return f"""
            INSERT INTO {table} (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
            VALUES (:n, :s, :f, :st, :m)
            ON CONFLICT(name) DO UPDATE SET
                last_started_at_utc = COALESCE(EXCLUDED.last_started_at_utc, {table}.last_started_at_utc),
                last_finished_at_utc = COALESCE(EXCLUDED.last_finished_at_utc, {table}.last_finished_at_utc),
                last_status = COALESCE(EXCLUDED.last_status, {table}.last_status),
                last_message = COALESCE(EXCLUDED.last_message, {table}.last_message)
        """
    else:
        return f"""
            INSERT INTO {table} (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
            VALUES (:n, :s, :f, :st, :m)
            ON DUPLICATE KEY UPDATE
                last_started_at_utc = COALESCE(VALUES(last_started_at_utc), last_started_at_utc),
                last_finished_at_utc = COALESCE(VALUES(last_finished_at_utc), last_finished_at_utc),
                last_status = COALESCE(VALUES(last_status), last_status),
                last_message = COALESCE(VALUES(last_message), last_message)
        """


def sql_current_timestamp(engine: Engine) -> str:
    """Return CURRENT_TIMESTAMP or NOW() based on dialect."""
    return "CURRENT_TIMESTAMP" if is_postgresql(engine) else "CURRENT_TIMESTAMP"


def sql_on_update_current_timestamp(engine: Engine) -> str:
    """Return ON UPDATE CURRENT_TIMESTAMP for MySQL, empty for Postgres (use trigger instead)."""
    return "" if is_postgresql(engine) else "ON UPDATE CURRENT_TIMESTAMP"
