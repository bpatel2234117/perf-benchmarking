"""
Benchmark database URL resolution for tracker, ai_log_fetcher, workflow tooling, and Streamlit `dashboard.py`.

When TRACKER_USE_POSTGRES (or BENCHMARK_USE_POSTGRES) is enabled, uses the same
PostgreSQL URL as the v2 API (`v2_api.db_config_pg`), driven by Postgres_* and DB_NAME in `.env`.

Override explicitly:
  TRACKER_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
"""
from __future__ import annotations

import os


def resolve_benchmark_database_url() -> str:
    for key in ("TRACKER_DATABASE_URL", "BENCHMARK_DATABASE_URL"):
        v = (os.getenv(key) or "").strip()
        if v:
            return v
    flag = (os.getenv("TRACKER_USE_POSTGRES") or os.getenv("BENCHMARK_USE_POSTGRES") or "").strip().lower()
    if flag in ("1", "true", "yes", "on", "postgres", "postgresql", "pg"):
        try:
            from v2_api.db_config_pg import DB_CONNECTION_STRING

            return DB_CONNECTION_STRING
        except Exception:
            pass
    from db_config import DB_CONNECTION_STRING as mysql_cs

    return mysql_cs


def is_postgresql_engine(engine) -> bool:
    try:
        return engine.dialect.name == "postgresql"
    except Exception:
        return False
