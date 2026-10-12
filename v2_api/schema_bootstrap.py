"""Apply idempotent Postgres schema patches on API startup (safe to re-run)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from v2_api.db_config_pg import BACKFILL_SQL, COLUMN_ADDS, DATA_SEEDS, DDL_STATEMENTS

_APPLIED = False

# Tables introduced after the original init_db bootstrap. init_db creates all
# DDL_STATEMENTS, but app startup only runs this migration path — so newly added
# tables must be created here too (CREATE TABLE IF NOT EXISTS is idempotent).
_NEW_TABLES = ("build_major_averages", "benchmark_runs")

# Indexes for the Logs page search/filter. Leading-wildcard ILIKE ('%term%') over the
# large error_context / ai_analysis text columns can't use a btree index and forces a
# full-table scan — the cause of multi-minute /api/logs/errors searches. pg_trgm GIN
# indexes make those substring searches index-backed (Postgres BitmapOrs them for the
# multi-column OR). Each statement is run independently so a missing-extension privilege
# error on one host doesn't abort the rest of startup.
_INDEX_STATEMENTS = (
    # Manual triage notes on a log error — when set, the analyzer skips the Ollama call
    # for that error signature (see ai_log_fetcher reuse-by-hash logic).
    "ALTER TABLE scan_log_errors ADD COLUMN IF NOT EXISTS manual_triage TEXT",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS idx_sle_error_context_trgm ON scan_log_errors USING gin (error_context gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_sle_ai_analysis_trgm ON scan_log_errors USING gin (ai_analysis gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_sle_scan_id_trgm ON scan_log_errors USING gin (scan_id gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_sle_log_source ON scan_log_errors (log_source)",
    "CREATE INDEX IF NOT EXISTS idx_sle_created_at ON scan_log_errors (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sle_log_timestamp ON scan_log_errors (log_timestamp DESC)",
    # error_hash lookups: dedup check + reuse-prior-analysis (skip Ollama for duplicates)
    "CREATE INDEX IF NOT EXISTS idx_sle_error_hash ON scan_log_errors (error_hash)",
)


def apply_schema_migrations(engine: Engine, *, force: bool = False, run_backfill: bool = True) -> None:
    global _APPLIED
    if _APPLIED and not force:
        return
    with engine.begin() as conn:
        for table_name in _NEW_TABLES:
            ddl = DDL_STATEMENTS.get(table_name)
            if ddl:
                conn.execute(text(ddl))
        for stmt in COLUMN_ADDS:
            conn.execute(text(stmt))
    if run_backfill:
        with engine.begin() as conn:
            for stmt in BACKFILL_SQL + DATA_SEEDS:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
    # Indexes run each in their own transaction so one failure (e.g. extension privilege)
    # can't roll back the others. CREATE INDEX IF NOT EXISTS is a no-op once built.
    for stmt in _INDEX_STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass
    _APPLIED = True
