#!/usr/bin/env python3
"""
Generate the email-friendly benchmark HTML report from Postgres.

Powers the React UI: POST /api/benchmark/generate-report (/tools/generate-report).

This intentionally reuses `generate_benchmark_email_report.py` for all layout,
colors, charts, cards, and section rendering. Only the database connection and
benchmark row retrieval are Postgres-specific.

Usage:
  python scripts/generate_benchmark_email_report_pg.py --structured --scanner-performance --test-env --out reports/latest_major_best.html
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import bindparam, create_engine, text

repo = Path(__file__).resolve().parent.parent
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(repo))
sys.path.insert(0, str(scripts_dir))

import generate_benchmark_email_report as base  # noqa: E402
from v2_api.db_config_pg import DB_CONNECTION_STRING  # noqa: E402


def _db_has_column(engine, table: str, col: str) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = ANY (current_schemas(false))
                      AND table_name = :table
                      AND column_name = :col
                    LIMIT 1
                    """
                ),
                {"table": table, "col": col},
            ).fetchone()
            return row is not None
    except Exception:
        return False


base._db_has_column = _db_has_column


def _sql_to_dataframe(engine, statement, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    Run SQL via SQLAlchemy and build a DataFrame.

    Avoids pd.read_sql(), which breaks in some Docker images when passed text()
    + Engine (pandas treats the query as non-SQLAlchemy and raises TypeError).
    """
    with engine.connect() as conn:
        result = conn.execute(statement, params or {})
        columns = list(result.keys())
        rows = result.fetchall()
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def load_benchmarks(engine, *, environment_id: Optional[int]) -> pd.DataFrame:
    has_b_release = _db_has_column(engine, "builds", "release_milestone")
    has_b_is_release = _db_has_column(engine, "benchmark_runs", "is_release")
    if has_b_is_release and has_b_release:
        # MySQL used builds.release_milestone; PG may also set benchmarks.is_release per row.
        release_expr = """
            (
                COALESCE(b.is_release, FALSE)
                OR (
                    bv.release_milestone IS NOT NULL
                    AND TRIM(COALESCE(bv.release_milestone::text, '')) NOT IN ('', '0', 'false', 'False')
                )
            ) AS is_release
        """
    elif has_b_is_release:
        release_expr = "COALESCE(b.is_release, FALSE) AS is_release"
    elif has_b_release:
        release_expr = """
            (
                bv.release_milestone IS NOT NULL
                AND TRIM(COALESCE(bv.release_milestone::text, '')) NOT IN ('', '0', 'false', 'False')
            ) AS is_release
        """
    else:
        release_expr = "FALSE AS is_release"
    updated_at_expr = "b.updated_at" if _db_has_column(engine, "benchmark_runs", "updated_at") else "NULL AS updated_at"
    scanner_files_expr = "b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr" if _db_has_column(engine, "benchmark_runs", "throughput_scanner_files_hr") else "NULL::double precision AS throughput_scanner_files_hr"
    thread_files_expr = "b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr" if _db_has_column(engine, "benchmark_runs", "throughput_thread_files_hr") else "NULL::double precision AS throughput_thread_files_hr"
    scanner_gb_expr = "b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr" if _db_has_column(engine, "benchmark_runs", "throughput_scanner_gb_hr") else "NULL::double precision AS throughput_scanner_gb_hr"
    thread_gb_expr = "b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr" if _db_has_column(engine, "benchmark_runs", "throughput_thread_gb_hr") else "NULL::double precision AS throughput_thread_gb_hr"
    query = """
        SELECT
            b.id,
            bv.version AS build_version,
            COALESCE(b.scan_id, '') AS scan_id,
            COALESCE(b.scan_type, '') AS scan_type,
            COALESCE(dst.name, '') AS data_source_type,
            COALESCE(fst.name, '') AS file_set_type,
            COALESCE(cfg.is_acl, FALSE) AS is_acl,
            COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
            COALESCE(cfg.feature_flags, '') AS feature_flags,
            b.total_scan_hours::double precision AS total_scan_hours,
            b.throughput_cluster_files_hr::double precision AS throughput_cluster_files_hr,
            b.throughput_cluster_gb_hr::double precision AS throughput_cluster_gb_hr,
            {scanner_files_expr},
            {thread_files_expr},
            {scanner_gb_expr},
            {thread_gb_expr},
            COALESCE(fst.total_files, 0) AS total_files,
            COALESCE(si.total_size_gb, fst.total_size_gb) AS total_size_gb,
            {updated_at_expr},
            sp.environment_id,
            COALESCE(sp.scan_status, '') AS scan_status,
            COALESCE(sp.scan_name, '') AS scan_name,
            COALESCE(si.total_classification, b.total_classification, 0) AS total_classification,
            COALESCE(si.new_attributes_count, b.total_catalog_items, 0) AS new_attributes_count,
            COALESCE(si.removed_attributes_count, 0) AS removed_attributes_count,
            COALESCE(NULLIF(si.objects_scanned, 0), NULLIF(fst.total_files, 0), NULLIF(b.total_catalog_items, 0), 0) AS objects_scanned,
            {release_expr}
        FROM benchmark_runs b
        LEFT JOIN builds bv ON bv.id = b.build_id
        LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
        LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
        LEFT JOIN configurations cfg ON cfg.id = b.config_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        WHERE COALESCE(b.is_active, TRUE) = TRUE
    """.format(
        release_expr=release_expr,
        updated_at_expr=updated_at_expr,
        scanner_files_expr=scanner_files_expr,
        thread_files_expr=thread_files_expr,
        scanner_gb_expr=scanner_gb_expr,
        thread_gb_expr=thread_gb_expr,
    )
    params: Dict[str, Any] = {}
    if environment_id is not None:
        query += " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)
    df = _sql_to_dataframe(engine, text(query), params)
    return base.normalize_benchmark_dataframe(df)


def load_trend_benchmark_rows(
    engine,
    *,
    environment_id: Optional[int] = None,
    max_builds: int = 600,
) -> pd.DataFrame:
    """
    Benchmark rows for trend charts — same scope as GET /api/trends/small-files
    (Small Files and/or structured relational runs, newest builds first).
    """
    small_files_fst = """(
        fst.name ILIKE '%Small Files%'
        OR fst.name ILIKE '%Small File%'
    )"""
    small_files_fst0 = """(
        fst0.name ILIKE '%Small Files%'
        OR fst0.name ILIKE '%Small File%'
    )"""
    structured_expr = """(
        (
            dst.name IS NOT NULL AND (
                LOWER(TRIM(dst.name::text)) LIKE '%postgres%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%mssql%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%mysql%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%sql server%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%microsoft sql%'
                OR LOWER(TRIM(dst.name::text)) LIKE 'rdb-%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%oracle%'
            )
        )
        OR COALESCE(cfg.is_feature_flags, FALSE)
        OR (
            LOWER(COALESCE(cfg.feature_flags, '')) LIKE '%structured%'
            AND LOWER(COALESCE(cfg.feature_flags, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(b.scenario_type, '')) LIKE '%structured%'
            AND LOWER(COALESCE(b.scenario_type, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(fst.name, '')) LIKE '%structured%'
            AND LOWER(COALESCE(fst.name, '')) NOT LIKE '%unstructured%'
        )
        OR LOWER(TRIM(COALESCE(fst.data_type, ''))) IN ('structured', 'structure')
    )"""
    structured_expr0 = structured_expr.replace("dst.", "dst0.").replace("cfg.", "cfg0.").replace("fst.", "fst0.").replace("b.scenario_type", "b0.scenario_type")

    has_b_release = _db_has_column(engine, "builds", "release_milestone")
    has_b_is_release = _db_has_column(engine, "benchmark_runs", "is_release")
    if has_b_is_release and has_b_release:
        release_expr = """
            (
                COALESCE(b.is_release, FALSE)
                OR (
                    bv.release_milestone IS NOT NULL
                    AND TRIM(COALESCE(bv.release_milestone::text, '')) NOT IN ('', '0', 'false', 'False')
                )
            ) AS is_release
        """
    elif has_b_is_release:
        release_expr = "COALESCE(b.is_release, FALSE) AS is_release"
    elif has_b_release:
        release_expr = """
            (
                bv.release_milestone IS NOT NULL
                AND TRIM(COALESCE(bv.release_milestone::text, '')) NOT IN ('', '0', 'false', 'False')
            ) AS is_release
        """
    else:
        release_expr = "FALSE AS is_release"

    scanner_files_expr = (
        "b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr"
        if _db_has_column(engine, "benchmark_runs", "throughput_scanner_files_hr")
        else "NULL::double precision AS throughput_scanner_files_hr"
    )
    thread_files_expr = (
        "b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr"
        if _db_has_column(engine, "benchmark_runs", "throughput_thread_files_hr")
        else "NULL::double precision AS throughput_thread_files_hr"
    )
    scanner_gb_expr = (
        "b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr"
        if _db_has_column(engine, "benchmark_runs", "throughput_scanner_gb_hr")
        else "NULL::double precision AS throughput_scanner_gb_hr"
    )
    thread_gb_expr = (
        "b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr"
        if _db_has_column(engine, "benchmark_runs", "throughput_thread_gb_hr")
        else "NULL::double precision AS throughput_thread_gb_hr"
    )

    env_filter = ""
    params: Dict[str, Any] = {"max_builds": int(max_builds)}
    if environment_id is not None:
        env_filter = " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)

    query = f"""
        WITH recent_build_ids AS (
            SELECT bv.id AS build_pk
            FROM builds bv
            WHERE EXISTS (
                SELECT 1
                FROM benchmark_runs b0
                LEFT JOIN data_source_types dst0 ON b0.data_source_type_id = dst0.id
                LEFT JOIN file_set_types fst0 ON b0.file_set_type_id = fst0.id
                LEFT JOIN configurations cfg0 ON b0.config_id = cfg0.id
                WHERE b0.build_id = bv.id
                  AND ({small_files_fst0} OR {structured_expr0})
                  AND COALESCE(b0.is_active, TRUE) = TRUE
            )
            ORDER BY
              COALESCE(NULLIF(SUBSTRING(bv.version FROM '^([0-9]{{3}})'), '')::INTEGER, 0) DESC,
              COALESCE(NULLIF(SUBSTRING(bv.version FROM '^[0-9]{{3}}[.\\-]([0-9]+)'), '')::INTEGER, 0) DESC,
              bv.id DESC
            LIMIT :max_builds
        )
        SELECT
            b.id,
            bv.version AS build_version,
            COALESCE(b.scan_id, '') AS scan_id,
            COALESCE(b.scan_type, '') AS scan_type,
            COALESCE(dst.name, '') AS data_source_type,
            COALESCE(fst.name, '') AS file_set_type,
            COALESCE(cfg.is_acl, FALSE) AS is_acl,
            COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
            COALESCE(cfg.feature_flags, '') AS feature_flags,
            b.total_scan_hours::double precision AS total_scan_hours,
            b.throughput_cluster_files_hr::double precision AS throughput_cluster_files_hr,
            b.throughput_cluster_gb_hr::double precision AS throughput_cluster_gb_hr,
            {scanner_files_expr},
            {thread_files_expr},
            {scanner_gb_expr},
            {thread_gb_expr},
            COALESCE(sp.scan_name, '') AS scan_name,
            {release_expr}
        FROM benchmark_runs b
        INNER JOIN recent_build_ids rbi ON b.build_id = rbi.build_pk
        LEFT JOIN builds bv ON bv.id = b.build_id
        LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
        LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
        LEFT JOIN configurations cfg ON cfg.id = b.config_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        WHERE fst.name IS NOT NULL
          AND ({small_files_fst} OR {structured_expr})
          AND COALESCE(b.is_active, TRUE) = TRUE
          {env_filter}
        ORDER BY b.id DESC
    """
    df = _sql_to_dataframe(engine, text(query), params)

    # Match the Dashboard's default base trend scenario, on the RAW DB flags
    # (cfg.is_acl / cfg.is_feature_flags as selected above) BEFORE normalization
    # canonicalizes them. The Dashboard (/api/trends/small-files) treats ACL and
    # Structured Discovery as separate scenarios and averages only base runs; the
    # report must do the same so the two charts agree. CSV's feature-flag is
    # inherent and never counts as Structured Discovery. OLS is dropped downstream
    # by base._filter_dashboard_trend_rows.
    if not df.empty:
        is_acl = (
            df["is_acl"].fillna(False).astype(bool)
            if "is_acl" in df.columns
            else pd.Series(False, index=df.index)
        )
        is_ff = (
            df["is_feature_flags"].fillna(False).astype(bool)
            if "is_feature_flags" in df.columns
            else pd.Series(False, index=df.index)
        )
        fst_lower = (
            df["file_set_type"].astype(str).str.lower()
            if "file_set_type" in df.columns
            else pd.Series("", index=df.index)
        )
        struct_disc = is_ff & ~fst_lower.str.contains("csv", na=False)
        df = df[~is_acl & ~struct_disc].copy()

    return base.normalize_benchmark_dataframe(df)


def load_available_builds(engine, *, environment_id: Optional[int]) -> List[Tuple[str, int]]:
    query = """
        SELECT bv.version AS build_version, COUNT(*) AS cnt
        FROM benchmark_runs b
        LEFT JOIN builds bv ON bv.id = b.build_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        WHERE COALESCE(b.is_active, TRUE) = TRUE
    """
    params: Dict[str, Any] = {}
    if environment_id is not None:
        query += " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)
    query += """
        GROUP BY bv.version
        ORDER BY MAX(b.id) DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return [(str(r[0]), int(r[1] or 0)) for r in rows]


def _pg_timestamp_expr(expr: str) -> str:
    return f"NULLIF({expr}::text, '')::timestamp"


def load_scanner_performance_for_build(engine, current_build: str, environment_id: Optional[int]) -> Dict[str, Any]:
    """Postgres-safe scanner-performance loader that preserves the original renderer's return shape."""
    if not current_build:
        return {"available": False, "reason": "No current build."}

    b_has_scan_start = _db_has_column(engine, "benchmark_runs", "scan_start_time")
    b_has_scan_end = _db_has_column(engine, "benchmark_runs", "scan_end_time")
    sp_has_completed_at = _db_has_column(engine, "scan_parents", "completed_at")
    sp_has_started_at = _db_has_column(engine, "scan_parents", "started_at")
    sp_has_last_seen = _db_has_column(engine, "scan_parents", "last_seen_at")

    start_parts: List[str] = []
    if sp_has_started_at:
        start_parts.append(_pg_timestamp_expr("sp.started_at"))
    if b_has_scan_start:
        start_parts.append("b.scan_start_time")
    start_expr = "COALESCE(" + ", ".join(start_parts) + ")" if start_parts else "NULL::timestamp"

    end_parts: List[str] = []
    if sp_has_completed_at:
        end_parts.append(_pg_timestamp_expr("sp.completed_at"))
    if b_has_scan_end:
        end_parts.append("b.scan_end_time")
    if sp_has_last_seen:
        end_parts.append("sp.last_seen_at")
    end_parts.extend(start_parts)
    end_expr = "COALESCE(" + ", ".join(end_parts) + ")" if end_parts else "NULL::timestamp"

    params: Dict[str, Any] = {"build": str(current_build).strip()}
    env_filter = ""
    if environment_id is not None:
        env_filter = " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)

    try:
        with engine.connect() as conn:
            scan_rows = conn.execute(
                text(f"""
                    SELECT DISTINCT sp.environment_id, b.scan_id, {start_expr} AS started_at, {end_expr} AS ended_at
                    FROM benchmark_runs b
                    LEFT JOIN builds bv ON bv.id = b.build_id
                    LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
                    WHERE bv.version = :build
                      AND sp.environment_id IS NOT NULL
                      AND TRIM(COALESCE(b.scan_id, '')) <> ''
                      {env_filter}
                """),
                params,
            ).fetchall()
    except Exception as exc:
        return {"available": False, "reason": f"Could not load build scan windows: {exc}"}

    env_windows: Dict[int, List[Tuple[datetime, datetime]]] = {}
    for row in scan_rows:
        start_dt = pd.to_datetime(row[2], errors="coerce")
        end_dt = pd.to_datetime(row[3], errors="coerce")
        if pd.isna(start_dt):
            continue
        if pd.isna(end_dt) or end_dt < start_dt:
            end_dt = start_dt
        try:
            eid = int(row[0])
        except Exception:
            continue
        env_windows.setdefault(eid, []).append((start_dt.to_pydatetime(), end_dt.to_pydatetime()))

    if not env_windows:
        return {"available": False, "reason": f"No benchmark scan windows found for build {current_build}."}

    return _build_perf_from_env_windows(
        engine,
        env_windows,
        section_title="Scanner Performance During Build",
        scope_desc=None,
    )


def _build_perf_from_env_windows(
    engine,
    env_windows: Dict[int, List[Tuple[datetime, datetime]]],
    *,
    section_title: Optional[str] = None,
    scope_desc: Optional[str] = None,
) -> Dict[str, Any]:
    """Given per-environment scan windows, build the scanner-performance render payload.

    Shared by the build-scoped and last-workflow-scoped loaders so both produce the
    identical per-env chart/summary shape the base renderer expects.
    """
    if not env_windows:
        return {"available": False, "reason": "No scan windows to load scanner performance for."}

    has_cpu_pct = _db_has_column(engine, "scanner_individual_performance", "cpu_pct")
    cpu_expr = "cpu_pct" if has_cpu_pct else "system_load_average AS cpu_pct"

    all_env_ids = tuple(sorted(env_windows.keys()))
    global_start = min(w[0] for wins in env_windows.values() for w in wins)
    global_end = max(w[1] for wins in env_windows.values() for w in wins)
    if global_end <= global_start:
        global_end = global_start

    try:
        group_df_all = _sql_to_dataframe(
            engine,
            text("""
                SELECT environment_id, captured_at_utc, group_name,
                       running_scanners, running_threads, scanner_threads,
                       physical_memory_mb, free_memory_mb, avg_scan_speed
                FROM scanner_group_performance
                WHERE environment_id IN :env_ids
                  AND captured_at_utc BETWEEN :start_dt AND :end_dt
                ORDER BY captured_at_utc ASC
            """).bindparams(bindparam("env_ids", expanding=True)),
            {"env_ids": list(all_env_ids), "start_dt": global_start, "end_dt": global_end},
        )
    except Exception:
        group_df_all = pd.DataFrame()

    try:
        individual_df_all = _sql_to_dataframe(
            engine,
            text(f"""
                SELECT environment_id, captured_at_utc, scanner_id, group_name,
                       {cpu_expr}, system_load_average, physical_memory_mb, free_memory_mb
                FROM scanner_individual_performance
                WHERE environment_id IN :env_ids
                  AND captured_at_utc BETWEEN :start_dt AND :end_dt
                ORDER BY captured_at_utc ASC
            """).bindparams(bindparam("env_ids", expanding=True)),
            {"env_ids": list(all_env_ids), "start_dt": global_start, "end_dt": global_end},
        )
    except Exception:
        individual_df_all = pd.DataFrame()

    per_env: List[Dict[str, Any]] = []
    for eid, windows in sorted(env_windows.items()):
        env_start = min(w[0] for w in windows)
        env_end = max(w[1] for w in windows)
        group_df = base._filter_rows_to_windows(
            group_df_all[group_df_all["environment_id"] == eid].copy() if not group_df_all.empty else pd.DataFrame(),
            windows,
        )
        individual_df = base._filter_rows_to_windows(
            individual_df_all[individual_df_all["environment_id"] == eid].copy() if not individual_df_all.empty else pd.DataFrame(),
            windows,
        )

        group_ts = pd.DataFrame()
        group_by_ts = pd.DataFrame()
        group_summary = pd.DataFrame()
        if not group_df.empty:
            group_df = group_df.copy()
            group_df["captured_at_utc"] = pd.to_datetime(group_df["captured_at_utc"], errors="coerce")
            for col in ("running_scanners", "running_threads", "scanner_threads", "physical_memory_mb", "free_memory_mb", "avg_scan_speed"):
                group_df[col] = pd.to_numeric(group_df.get(col), errors="coerce").fillna(0.0)
            group_df["used_memory_mb"] = (group_df["physical_memory_mb"] - group_df["free_memory_mb"]).clip(lower=0)
            group_df["group_name"] = group_df["group_name"].fillna("").astype(str).str.strip().replace({"": "default"})
            group_df = group_df.dropna(subset=["captured_at_utc"]).drop_duplicates(["environment_id", "captured_at_utc", "group_name"])
            group_by_ts = (
                group_df.groupby(["captured_at_utc", "group_name"], as_index=False)
                .agg(
                    running_scanners=("running_scanners", "sum"),
                    running_threads=("running_threads", "sum"),
                    used_memory_mb=("used_memory_mb", "sum"),
                    physical_memory_mb=("physical_memory_mb", "max"),
                    avg_scan_speed=("avg_scan_speed", "mean"),
                )
                .sort_values(["group_name", "captured_at_utc"])
            )
            group_by_ts["avg_scan_speed_kib_s"] = pd.to_numeric(group_by_ts["avg_scan_speed"], errors="coerce").fillna(0.0) / 1024.0
            group_ts = (
                group_by_ts.groupby("captured_at_utc", as_index=False)
                .agg(
                    running_scanners=("running_scanners", "sum"),
                    running_threads=("running_threads", "sum"),
                    used_memory_mb=("used_memory_mb", "sum"),
                    physical_memory_mb=("physical_memory_mb", "max"),
                    avg_scan_speed=("avg_scan_speed", "mean"),
                )
                .sort_values("captured_at_utc")
            )
            group_ts["avg_scan_speed_kib_s"] = pd.to_numeric(group_ts["avg_scan_speed"], errors="coerce").fillna(0.0) / 1024.0
            group_summary = (
                group_by_ts.groupby("group_name", as_index=False)
                .agg(
                    avg_speed_kib_s=("avg_scan_speed_kib_s", "mean"),
                    avg_memory_mb=("used_memory_mb", "mean"),
                    peak_memory_mb=("used_memory_mb", "max"),
                    peak_scanners=("running_scanners", "max"),
                    avg_threads=("running_threads", "mean"),
                )
                .sort_values("group_name")
            )

        cpu_ts = pd.DataFrame()
        cpu_by_group = pd.DataFrame()
        if not individual_df.empty:
            individual_df = individual_df.copy()
            individual_df["captured_at_utc"] = pd.to_datetime(individual_df["captured_at_utc"], errors="coerce")
            individual_df["cpu_pct"] = pd.to_numeric(individual_df.get("cpu_pct"), errors="coerce")
            if individual_df["cpu_pct"].isna().all() and "system_load_average" in individual_df.columns:
                individual_df["cpu_pct"] = pd.to_numeric(individual_df["system_load_average"], errors="coerce")
            individual_df["group_name"] = individual_df.get("group_name", "").fillna("").astype(str).str.strip().replace({"": "default"})
            individual_df = individual_df.dropna(subset=["captured_at_utc"]).dropna(subset=["cpu_pct"])
            if not individual_df.empty:
                cpu_by_group = (
                    individual_df.groupby(["captured_at_utc", "group_name"], as_index=False)["cpu_pct"]
                    .mean()
                    .rename(columns={"cpu_pct": "avg_cpu_pct"})
                    .sort_values(["group_name", "captured_at_utc"])
                )
                cpu_ts = (
                    individual_df.groupby("captured_at_utc", as_index=False)["cpu_pct"]
                    .mean()
                    .rename(columns={"cpu_pct": "avg_cpu_pct"})
                    .sort_values("captured_at_utc")
                )

        per_env.append({
            "env_id": eid,
            "start_dt": env_start,
            "end_dt": env_end,
            "scan_count": len(windows),
            "group_ts": group_ts,
            "group_by_ts": group_by_ts,
            "group_summary": group_summary,
            "cpu_ts": cpu_ts,
            "cpu_by_group": cpu_by_group,
        })

    if not per_env:
        return {"available": False, "reason": "No scanner performance rows found during the scan windows."}

    return {
        "available": True,
        "per_env": per_env,
        "total_scan_count": sum(e["scan_count"] for e in per_env),
        "env_ids": [e["env_id"] for e in per_env],
        "section_title": section_title,
        "scope_desc": scope_desc,
    }


def load_scanner_performance_for_last_workflow(
    engine, current_build: str, environment_id: Optional[int]
) -> Dict[str, Any]:
    """Scanner performance scoped to the most recent workflow run.

    Picks the latest workflow_run whose steps target `current_build`; if none match,
    falls back to the latest workflow_run overall. Builds per-environment scan windows
    from that run's steps (scan_parents timing preferred, workflow_steps timing as
    fallback) so the charts reflect the last sprint's scans across all its envs.
    """
    ws_has_started = _db_has_column(engine, "workflow_steps", "started_at")
    ws_has_finished = _db_has_column(engine, "workflow_steps", "finished_at")
    sp_has_started = _db_has_column(engine, "scan_parents", "started_at")
    sp_has_completed = _db_has_column(engine, "scan_parents", "completed_at")
    sp_has_last_seen = _db_has_column(engine, "scan_parents", "last_seen_at")

    start_parts: List[str] = []
    if sp_has_started:
        start_parts.append(_pg_timestamp_expr("sp.started_at"))
    if ws_has_started:
        start_parts.append("ws.started_at")
    start_expr = "COALESCE(" + ", ".join(start_parts) + ")" if start_parts else "NULL::timestamp"

    end_parts: List[str] = []
    if sp_has_completed:
        end_parts.append(_pg_timestamp_expr("sp.completed_at"))
    if ws_has_finished:
        end_parts.append("ws.finished_at")
    if sp_has_last_seen:
        end_parts.append("sp.last_seen_at")
    end_parts.extend(start_parts)
    end_expr = "COALESCE(" + ", ".join(end_parts) + ")" if end_parts else "NULL::timestamp"

    build = str(current_build or "").strip()

    try:
        with engine.connect() as conn:
            run_id: Optional[int] = None
            if build:
                row = conn.execute(
                    text("""
                        SELECT wr.id
                        FROM workflow_runs wr
                        WHERE EXISTS (
                            SELECT 1 FROM workflow_steps ws
                            WHERE ws.run_id = wr.id
                              AND TRIM(COALESCE(ws.build_version, '')) = :build
                        )
                        ORDER BY COALESCE(wr.started_at, wr.created_at) DESC, wr.id DESC
                        LIMIT 1
                    """),
                    {"build": build},
                ).fetchone()
                if row is not None:
                    run_id = int(row[0])
            if run_id is None:
                row = conn.execute(
                    text("""
                        SELECT id FROM workflow_runs
                        ORDER BY COALESCE(started_at, created_at) DESC, id DESC
                        LIMIT 1
                    """)
                ).fetchone()
                if row is not None:
                    run_id = int(row[0])

            if run_id is None:
                return {"available": False, "reason": "No workflow runs found."}

            params: Dict[str, Any] = {"rid": run_id}
            env_filter = ""
            if environment_id is not None:
                env_filter = " AND ws.environment_id = :eid"
                params["eid"] = int(environment_id)

            scan_rows = conn.execute(
                text(f"""
                    SELECT DISTINCT ws.environment_id, ws.scan_id,
                           {start_expr} AS started_at, {end_expr} AS ended_at
                    FROM workflow_steps ws
                    LEFT JOIN scan_parents sp ON sp.scan_id = ws.scan_id
                    WHERE ws.run_id = :rid
                      AND ws.environment_id IS NOT NULL
                      AND TRIM(COALESCE(ws.scan_id, '')) <> ''
                      {env_filter}
                """),
                params,
            ).fetchall()
    except Exception as exc:
        return {"available": False, "reason": f"Could not load last workflow scan windows: {exc}"}

    env_windows: Dict[int, List[Tuple[datetime, datetime]]] = {}
    for row in scan_rows:
        start_dt = pd.to_datetime(row[2], errors="coerce")
        end_dt = pd.to_datetime(row[3], errors="coerce")
        if pd.isna(start_dt):
            continue
        if pd.isna(end_dt) or end_dt < start_dt:
            end_dt = start_dt
        try:
            eid = int(row[0])
        except Exception:
            continue
        env_windows.setdefault(eid, []).append((start_dt.to_pydatetime(), end_dt.to_pydatetime()))

    if not env_windows:
        return {"available": False, "reason": f"No completed scans found for the last workflow run (id {run_id})."}

    return _build_perf_from_env_windows(
        engine,
        env_windows,
        section_title="Scanner Performance — Last Workflow Run",
        scope_desc=f"last workflow run (id {run_id})",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the original benchmark email HTML report from Postgres.")
    ap.add_argument("--list-builds", action="store_true", help="List available build versions and benchmark counts, then exit")
    ap.add_argument("--current-build", help="Optional current build version. If omitted, the latest available build is used.")
    ap.add_argument("--previous-build", help="Optional previous / baseline build version. If omitted, the previous available build is used.")
    ap.add_argument("--environment-id", type=int, help="Optional environment_id filter")
    ap.add_argument("--structured", action="store_true", help="(Legacy) Include all structured DS when --structured-ds is omitted")
    ap.add_argument("--unstructured-ds", default=None, metavar="LIST", help="Comma-separated unstructured DS (default: S3,SMB). Options: S3,SMB")
    ap.add_argument("--structured-ds", default=None, metavar="LIST", help="Comma-separated structured DS (default: none, or all with --structured). Options: MSSQL,Postgres")
    ap.add_argument("--scanner-performance", action="store_true", help="Include scanner-group CPU, memory, and speed during current-build benchmark scans")
    ap.add_argument(
        "--scanner-scope",
        choices=["last", "all"],
        default="last",
        help="Scanner-performance window scope: 'last' = the most recent workflow run for the current build (all its envs); 'all' = every benchmark scan of the current build. Default: last.",
    )
    ap.add_argument("--trends", action="store_true", help="Include Major Release Benchmark Trends section showing throughput across all releases")
    ap.add_argument("--errors", action="store_true", help="Include Observed Issues / Errors / RCA section")
    ap.add_argument("--test-env", action="store_true", help="Include Test Environment section")
    ap.add_argument("--test-env-environments", default="Two large-scale environments", metavar="DESC")
    ap.add_argument("--test-env-scanners", default="5", metavar="N")
    ap.add_argument("--test-env-threads-per-scanner", default="10", metavar="N")
    ap.add_argument("--test-env-scan-setting-threads", default="50", metavar="N")
    ap.add_argument("--test-env-ds-lite-mode", default="True", metavar="BOOL")
    ap.add_argument("--test-env-scanner-jobs-thresholds", default="false", metavar="BOOL")
    ap.add_argument("--test-env-file-count", default="1 M", metavar="COUNT")
    ap.add_argument("--test-env-file-type", default="txt", metavar="EXT")
    ap.add_argument("--email-safe", action="store_true",
                    help="Emit a fixed narrow-width, Gmail/Outlook-safe layout suitable for pasting into an email body")
    ap.add_argument("--email-width", type=int, default=base.EMAIL_SAFE_WIDTH, metavar="PX",
                    help=f"Container width in px for --email-safe (default: {base.EMAIL_SAFE_WIDTH})")
    ap.add_argument("--out", help="Output HTML file path")
    args = ap.parse_args(argv)

    engine = create_engine(DB_CONNECTION_STRING, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        msg = str(exc).split("\n")[0]
        print(f"ERROR: Cannot connect to the Postgres benchmark database.\n  {msg}", file=sys.stderr)
        print("  Check Postgres_* / DB_NAME in .env and network reachability.", file=sys.stderr)
        return 1

    available_builds = load_available_builds(engine, environment_id=args.environment_id)
    if args.list_builds:
        for build_version, cnt in available_builds:
            print(f"{build_version}\t{cnt}")
        return 0

    auto_current, auto_previous = base.pick_default_builds(available_builds)
    current_major, previous_major = base._pick_latest_two_major_lines(available_builds)
    current_build = str(args.current_build or auto_current or "").strip()
    previous_build = str(args.previous_build or auto_previous or "").strip() or None
    if not current_build:
        ap.error("No build data found in Postgres. Use --list-builds to inspect available versions.")

    df = load_benchmarks(engine, environment_id=args.environment_id)
    trend_df = load_trend_benchmark_rows(engine, environment_id=args.environment_id)
    if not current_major:
        ap.error("No major-version build lines were found in Postgres.")

    cur_df = base.best_rows_for_major_line(df, current_major)
    prev_df = base.best_rows_for_major_line(df, previous_major) if previous_major else pd.DataFrame(columns=df.columns)
    current_build_df = df[df["build_version"].astype(str) == current_build].copy()
    classifier_detail = base.load_classifier_detail(engine, current_build, previous_build, args.environment_id)
    error_items = (
        base.load_error_rca(engine, [s for s in cur_df.get("scan_id", pd.Series(dtype=str)).tolist() if str(s or "").strip()])
        if args.errors
        else []
    )
    if args.scanner_performance:
        if args.scanner_scope == "last":
            scanner_perf = load_scanner_performance_for_last_workflow(engine, current_build, args.environment_id)
            # If no workflow run has captured scanner data yet, fall back to the
            # build-wide windows so the section still renders something useful.
            if not scanner_perf.get("available"):
                scanner_perf = load_scanner_performance_for_build(engine, current_build, args.environment_id)
        else:
            scanner_perf = load_scanner_performance_for_build(engine, current_build, args.environment_id)
    else:
        scanner_perf = {}
    test_env_config = {
        "environments": args.test_env_environments,
        "scanners": args.test_env_scanners,
        "threads_per_scanner": args.test_env_threads_per_scanner,
        "scan_setting_threads": args.test_env_scan_setting_threads,
        "ds_lite_mode": args.test_env_ds_lite_mode,
        "scanner_jobs_thresholds": args.test_env_scanner_jobs_thresholds,
        "file_count": args.test_env_file_count,
        "file_type": args.test_env_file_type,
    } if args.test_env else None

    ds_scope = base.resolve_ds_scope(
        args.unstructured_ds,
        args.structured_ds,
        include_structured_legacy=bool(args.structured),
    )
    if not ds_scope:
        ap.error("No data sources selected. Pick at least one unstructured or structured DS.")

    report_html = base.build_html_report(
        current_build,
        previous_build,
        current_major,
        previous_major,
        args.environment_id,
        df,
        cur_df,
        prev_df,
        current_build_df,
        classifier_detail,
        error_items,
        scanner_perf,
        ds_scope,
        args.scanner_performance,
        args.errors,
        args.test_env,
        test_env_config,
        engine,
        include_trends=args.trends,
        trend_df=trend_df,
        email_safe=args.email_safe,
        email_width=args.email_width,
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_html, encoding="utf-8")
        print(str(out_path))
    else:
        print(report_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
