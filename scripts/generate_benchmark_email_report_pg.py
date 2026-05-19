#!/usr/bin/env python3
"""
Generate the original email-friendly benchmark HTML report from Postgres.

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


def load_benchmarks(engine, *, environment_id: Optional[int]) -> pd.DataFrame:
    release_expr = "COALESCE(bv.release_milestone, 0) AS is_release" if _db_has_column(engine, "builds", "release_milestone") else "0 AS is_release"
    updated_at_expr = "b.updated_at" if _db_has_column(engine, "benchmarks", "updated_at") else "NULL AS updated_at"
    scanner_files_expr = "b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr" if _db_has_column(engine, "benchmarks", "throughput_scanner_files_hr") else "NULL::double precision AS throughput_scanner_files_hr"
    thread_files_expr = "b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr" if _db_has_column(engine, "benchmarks", "throughput_thread_files_hr") else "NULL::double precision AS throughput_thread_files_hr"
    scanner_gb_expr = "b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr" if _db_has_column(engine, "benchmarks", "throughput_scanner_gb_hr") else "NULL::double precision AS throughput_scanner_gb_hr"
    thread_gb_expr = "b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr" if _db_has_column(engine, "benchmarks", "throughput_thread_gb_hr") else "NULL::double precision AS throughput_thread_gb_hr"
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
            b.total_files,
            COALESCE(si.total_size_gb, b.total_size_gb, fst.total_size_gb) AS total_size_gb,
            {updated_at_expr},
            sp.environment_id,
            COALESCE(sp.scan_status, '') AS scan_status,
            COALESCE(si.total_classification, b.total_classification, 0) AS total_classification,
            COALESCE(si.new_attributes_count, b.total_catalog_items, 0) AS new_attributes_count,
            COALESCE(si.removed_attributes_count, b.removed_attributes, 0) AS removed_attributes_count,
            COALESCE(NULLIF(si.objects_scanned, 0), NULLIF(b.total_files, 0), NULLIF(b.total_catalog_items, 0), 0) AS objects_scanned,
            {release_expr}
        FROM benchmarks b
        JOIN builds bv ON bv.id = b.build_id
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
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def load_available_builds(engine, *, environment_id: Optional[int]) -> List[Tuple[str, int]]:
    query = """
        SELECT bv.version AS build_version, COUNT(*) AS cnt
        FROM benchmarks b
        JOIN builds bv ON bv.id = b.build_id
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

    b_has_scan_start = _db_has_column(engine, "benchmarks", "scan_start_time")
    b_has_scan_end = _db_has_column(engine, "benchmarks", "scan_end_time")
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
                    FROM benchmarks b
                    JOIN builds bv ON bv.id = b.build_id
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

    has_cpu_pct = _db_has_column(engine, "scanner_individual_performance", "cpu_pct")
    cpu_expr = "cpu_pct" if has_cpu_pct else "system_load_average AS cpu_pct"

    all_env_ids = tuple(sorted(env_windows.keys()))
    global_start = min(w[0] for wins in env_windows.values() for w in wins)
    global_end = max(w[1] for wins in env_windows.values() for w in wins)
    if global_end <= global_start:
        global_end = global_start

    try:
        with engine.connect() as conn:
            group_df_all = pd.read_sql(
                text("""
                    SELECT environment_id, captured_at_utc, group_name,
                           running_scanners, running_threads, scanner_threads,
                           physical_memory_mb, free_memory_mb, avg_scan_speed
                    FROM scanner_group_performance
                    WHERE environment_id IN :env_ids
                      AND captured_at_utc BETWEEN :start_dt AND :end_dt
                    ORDER BY captured_at_utc ASC
                """).bindparams(bindparam("env_ids", expanding=True)),
                conn,
                params={"env_ids": all_env_ids, "start_dt": global_start, "end_dt": global_end},
            )
    except Exception:
        group_df_all = pd.DataFrame()

    try:
        with engine.connect() as conn:
            individual_df_all = pd.read_sql(
                text(f"""
                    SELECT environment_id, captured_at_utc, scanner_id, group_name,
                           {cpu_expr}, system_load_average, physical_memory_mb, free_memory_mb
                    FROM scanner_individual_performance
                    WHERE environment_id IN :env_ids
                      AND captured_at_utc BETWEEN :start_dt AND :end_dt
                    ORDER BY captured_at_utc ASC
                """).bindparams(bindparam("env_ids", expanding=True)),
                conn,
                params={"env_ids": all_env_ids, "start_dt": global_start, "end_dt": global_end},
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
        return {"available": False, "reason": "No scanner performance rows found during this build's benchmark scan windows."}

    return {
        "available": True,
        "per_env": per_env,
        "total_scan_count": sum(e["scan_count"] for e in per_env),
        "env_ids": [e["env_id"] for e in per_env],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the original benchmark email HTML report from Postgres.")
    ap.add_argument("--list-builds", action="store_true", help="List available build versions and benchmark counts, then exit")
    ap.add_argument("--current-build", help="Optional current build version. If omitted, the latest available build is used.")
    ap.add_argument("--previous-build", help="Optional previous / baseline build version. If omitted, the previous available build is used.")
    ap.add_argument("--environment-id", type=int, help="Optional environment_id filter")
    ap.add_argument("--structured", action="store_true", help="Include structured DS sections for MSSQL and Postgres")
    ap.add_argument("--scanner-performance", action="store_true", help="Include scanner-group CPU, memory, and speed during current-build benchmark scans")
    ap.add_argument("--errors", action="store_true", help="Include Observed Issues / Errors / RCA section")
    ap.add_argument("--test-env", action="store_true", help="Include Test Environment section")
    ap.add_argument("--test-env-environments", default="Two large-scale environments", metavar="DESC")
    ap.add_argument("--test-env-scanners", default="5", metavar="N")
    ap.add_argument("--test-env-threads-per-scanner", default="10", metavar="N")
    ap.add_argument("--test-env-scan-setting-threads", default="50", metavar="N")
    ap.add_argument("--test-env-ds-lite-mode", default="True", metavar="BOOL")
    ap.add_argument("--test-env-file-count", default="1 M", metavar="COUNT")
    ap.add_argument("--test-env-file-type", default="txt", metavar="EXT")
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
    scanner_perf = load_scanner_performance_for_build(engine, current_build, args.environment_id) if args.scanner_performance else {}
    test_env_config = {
        "environments": args.test_env_environments,
        "scanners": args.test_env_scanners,
        "threads_per_scanner": args.test_env_threads_per_scanner,
        "scan_setting_threads": args.test_env_scan_setting_threads,
        "ds_lite_mode": args.test_env_ds_lite_mode,
        "file_count": args.test_env_file_count,
        "file_type": args.test_env_file_type,
    } if args.test_env else None

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
        args.structured,
        args.scanner_performance,
        args.errors,
        args.test_env,
        test_env_config,
        engine,
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
