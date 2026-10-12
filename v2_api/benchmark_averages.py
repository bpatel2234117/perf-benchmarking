"""Per-major-build, per-scenario throughput averages (build_major_averages).

Aggregates the `benchmarks` table into averaged throughput rows so charts can show
the mean across a major build instead of cherry-picking the best run. This module
NEVER modifies the `benchmarks` table — it only upserts into `build_major_averages`.

Major build = the first dot-segment of `builds.version` (e.g. "254" from "254.6",
"254.10"). A test scenario = (data_source_type, file_set_type, scan_type, is_acl,
is_feature_flags).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Single INSERT ... SELECT ... GROUP BY ... ON CONFLICT upsert. All grouping keys are
# COALESCEd to non-NULL constants so the UNIQUE constraint (and thus ON CONFLICT) is
# well-defined — Postgres treats NULLs as distinct in unique indexes, which would
# otherwise let duplicate rows accumulate.
_RECALC_TEMPLATE = (
    """
    INSERT INTO build_major_averages (
        major_build, data_source_type, file_set_type, scan_type,
        is_acl, is_feature_flags, run_count,
        avg_total_scan_hours,
        avg_throughput_cluster_gb_hr, avg_throughput_scanner_gb_hr, avg_throughput_thread_gb_hr,
        avg_throughput_cluster_files_hr, avg_throughput_scanner_files_hr, avg_throughput_thread_files_hr,
        min_throughput_cluster_gb_hr, max_throughput_cluster_gb_hr, stddev_throughput_cluster_gb_hr,
        last_calculated_at
    )
    SELECT
        SPLIT_PART(bv.version, '.', 1)        AS major_build,
        COALESCE(dst.name, 'Unknown')         AS data_source_type,
        COALESCE(fst.name, 'Unknown')         AS file_set_type,
        COALESCE(b.scan_type, 'unknown')      AS scan_type,
        COALESCE(cfg.is_acl, FALSE)           AS is_acl,
        COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
        COUNT(b.id)                           AS run_count,
        AVG(b.total_scan_hours),
        AVG(b.throughput_cluster_gb_hr),
        AVG(b.throughput_scanner_gb_hr),
        AVG(b.throughput_thread_gb_hr),
        AVG(b.throughput_cluster_files_hr),
        AVG(b.throughput_scanner_files_hr),
        AVG(b.throughput_thread_files_hr),
        MIN(b.throughput_cluster_gb_hr),
        MAX(b.throughput_cluster_gb_hr),
        STDDEV_SAMP(b.throughput_cluster_gb_hr),
        CURRENT_TIMESTAMP
    FROM benchmark_runs b
    JOIN builds bv ON bv.id = b.build_id
    LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
    LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
    LEFT JOIN configurations cfg ON cfg.id = b.config_id
    WHERE COALESCE(b.is_active, TRUE) = TRUE
      AND bv.version ~ '^[0-9]'
      AND b.throughput_cluster_gb_hr IS NOT NULL
      -- Throughput is only comparable on the standard 5-scanner / 50-thread rig.
      -- Runs explicitly recorded on a different config (e.g. 5/25) are kept in
      -- benchmark_runs but excluded here; throughput does not scale linearly with
      -- threads, so they must not mix into 5/50 averages. NULL config = auto run = 5/50.
      AND (b.config_id IS NULL OR (cfg.scanner_count = 5 AND cfg.thread_count = 50))
      {major_filter}
    GROUP BY
        SPLIT_PART(bv.version, '.', 1),
        COALESCE(dst.name, 'Unknown'),
        COALESCE(fst.name, 'Unknown'),
        COALESCE(b.scan_type, 'unknown'),
        COALESCE(cfg.is_acl, FALSE),
        COALESCE(cfg.is_feature_flags, FALSE)
    ON CONFLICT (major_build, data_source_type, file_set_type, scan_type, is_acl, is_feature_flags)
    DO UPDATE SET
        run_count                       = EXCLUDED.run_count,
        avg_total_scan_hours            = EXCLUDED.avg_total_scan_hours,
        avg_throughput_cluster_gb_hr    = EXCLUDED.avg_throughput_cluster_gb_hr,
        avg_throughput_scanner_gb_hr    = EXCLUDED.avg_throughput_scanner_gb_hr,
        avg_throughput_thread_gb_hr     = EXCLUDED.avg_throughput_thread_gb_hr,
        avg_throughput_cluster_files_hr = EXCLUDED.avg_throughput_cluster_files_hr,
        avg_throughput_scanner_files_hr = EXCLUDED.avg_throughput_scanner_files_hr,
        avg_throughput_thread_files_hr  = EXCLUDED.avg_throughput_thread_files_hr,
        min_throughput_cluster_gb_hr    = EXCLUDED.min_throughput_cluster_gb_hr,
        max_throughput_cluster_gb_hr    = EXCLUDED.max_throughput_cluster_gb_hr,
        stddev_throughput_cluster_gb_hr = EXCLUDED.stddev_throughput_cluster_gb_hr,
        last_calculated_at              = CURRENT_TIMESTAMP
    """
)


def recalculate_build_major_averages(engine: Engine, major_build: Optional[str] = None) -> int:
    """Upsert averages into build_major_averages. Returns rows affected.

    - major_build=None → recompute ALL (major_build, scenario) groups. Use for the
      one-time/startup full backfill ("recompute whatever we have today").
    - major_build="254" → recompute only that major build's groups. Use when a benchmark
      is added (manual save / workflow finish) so we touch just the affected build, not
      the whole table.

    Never touches the benchmarks/benchmark_runs source rows.
    """
    params: dict = {}
    if major_build is not None and str(major_build).strip():
        major_filter = "AND SPLIT_PART(bv.version, '.', 1) = :major_build"
        params["major_build"] = str(major_build).strip()
    else:
        major_filter = ""
    sql = text(_RECALC_TEMPLATE.format(major_filter=major_filter))
    with engine.begin() as conn:
        result = conn.execute(sql, params)
        return result.rowcount or 0
