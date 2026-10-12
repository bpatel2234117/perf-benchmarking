#!/usr/bin/env python3
"""
Build a simpler major-line HTML report from Postgres (benchmarks + auto_scan_benchmarks).

NOT used by the UI at /tools/generate-report — that page runs
`generate_benchmark_email_report_pg.py` (full email-style report with charts,
scenario matrix, classifiers, optional scanner-performance / test-env sections).

This script is for CLI / ops:
- find the latest major build line with active benchmark rows, unless --major-line is provided
- pick the best result per scenario inside that major line for chart/summary use
- keep all runs in a detail table for supporting report data

Usage (repo root):
  python scripts/workflow_run_scan_report_pg.py
  python scripts/workflow_run_scan_report_pg.py --major-line 252
  python scripts/workflow_run_scan_report_pg.py --out reports/latest_major_best.html
"""
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from v2_api.db_config_pg import DB_CONNECTION_STRING  # noqa: E402


def _major_line(v: Any) -> str:
    s = str(v or "").strip()
    return s.split(".", 1)[0].strip() if s else ""


def _build_sort_key(v: Any) -> Tuple[int, int, str]:
    s = str(v or "").strip()
    if not s:
        return (0, 0, "")
    parts = s.split(".")
    try:
        major = int(parts[0])
    except Exception:
        major = 0
    try:
        minor = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        minor = 0
    return (major, minor, s)


def _safe_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _fmt_num(v: Any, decimals: int = 2) -> str:
    x = _safe_float(v)
    if x is None:
        return ""
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:,.{decimals}f}"


def _boolish(v: Any) -> bool:
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _scenario_label(row: pd.Series) -> str:
    scan_type = str(row.get("scan_type") or "").strip().lower()
    is_acl = _boolish(row.get("is_acl"))
    is_ff = _boolish(row.get("is_feature_flags"))
    has_ols = "ols" in scan_type
    if has_ols and is_ff:
        return "OLS + Structured FF"
    if has_ols:
        return "OLS Scan"
    if is_acl and is_ff:
        return "ACL + Structured FF"
    if is_acl:
        return "ACL On"
    if is_ff:
        return "Structured FF"
    return "Base Full Scan"


def load_benchmarks(engine) -> pd.DataFrame:
    query = """
        WITH progress_latest AS (
            SELECT DISTINCT ON (scan_id)
                scan_id,
                COALESCE(scan_state, '') AS latest_state
            FROM scan_progress
            ORDER BY scan_id, COALESCE(captured_at_utc, "timestamp") DESC NULLS LAST, id DESC
        )
        SELECT
            b.id,
            b.source AS source_table,
            COALESCE(bv.version, b.build_version, '') AS build_version,
            COALESCE(dst.name, b.data_source_type, '') AS data_source_type,
            COALESCE(fst.name, b.file_set_type, '') AS file_set_type,
            COALESCE(b.scenario_type, '') AS scenario_type,
            COALESCE(b.scan_type, '') AS scan_type,
            COALESCE(cfg.is_acl, FALSE) AS is_acl,
            COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
            COALESCE(cfg.feature_flags, '') AS feature_flags,
            b.total_scan_hours::double precision AS total_scan_hours,
            b.throughput_cluster_gb_hr::double precision AS throughput_cluster_gb_hr,
            b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr,
            b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr,
            b.throughput_cluster_files_hr::double precision AS throughput_cluster_files_hr,
            b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr,
            b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr,
            COALESCE(si.objects_scanned, b.objects_scanned, b.total_catalog_items, fst.total_files, 0)::bigint AS objects_scanned,
            COALESCE(si.total_size_gb, b.total_size_gb, fst.total_size_gb, 0)::double precision AS total_size_gb,
            COALESCE(si.total_classification, b.total_classification, 0)::bigint AS total_classification,
            COALESCE(si.new_attributes_count, b.new_attributes, 0)::bigint AS new_attributes,
            COALESCE(si.removed_attributes_count, b.removed_attributes, 0)::bigint AS removed_attributes,
            b.scan_id,
            COALESCE(b.environment_id, sp.environment_id) AS environment_id,
            te.env_name,
            COALESCE(sp.scan_status, '') AS scan_status,
            pl.latest_state,
            b.run_date::text AS run_date
        FROM benchmark_runs b
        LEFT JOIN builds bv ON bv.id = b.build_id
        LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
        LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
        LEFT JOIN configurations cfg ON cfg.id = b.config_id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        LEFT JOIN test_environments te ON te.id = COALESCE(b.environment_id, sp.environment_id)
        LEFT JOIN progress_latest pl ON pl.scan_id = b.scan_id
        WHERE COALESCE(b.is_active, TRUE) = TRUE
    """
    with engine.connect() as conn:
        result = conn.execute(text(query))
        cols = list(result.keys())
        rows = result.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


def pick_latest_major(df: pd.DataFrame) -> str:
    majors = sorted(
        {_major_line(v) for v in df.get("build_version", pd.Series(dtype=str)).tolist() if _major_line(v)},
        key=_build_sort_key,
        reverse=True,
    )
    return majors[0] if majors else ""


def select_best_run(group: pd.DataFrame) -> pd.Series:
    """Average the runs in this scenario group (mean throughput / scan-hours / volume).

    Reports use the averaged-per-major-build figure rather than a single cherry-picked
    run, matching the build_major_averages table and the trend charts.
    """
    row = group.iloc[0].copy()
    numeric_cols = (
        "throughput_cluster_gb_hr", "throughput_scanner_gb_hr", "throughput_thread_gb_hr",
        "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr",
        "total_scan_hours", "objects_scanned", "total_size_gb", "total_classification",
        "new_attributes", "removed_attributes",
    )
    for col in numeric_cols:
        if col in group.columns:
            vals = pd.to_numeric(group[col], errors="coerce")
            if vals.notna().any():
                row[col] = vals.mean()
    if "scan_name" in row.index:
        row["scan_name"] = f"Average of {len(group)} run(s)"
    return row


def best_rows_for_major(df: pd.DataFrame, major_line: str) -> pd.DataFrame:
    work = df[df["build_version"].astype(str).map(_major_line) == str(major_line).strip()].copy()
    if work.empty:
        return pd.DataFrame(columns=df.columns)
    work["scenario_label"] = work.apply(_scenario_label, axis=1)
    work["norm_ds"] = work["data_source_type"].astype(str).str.strip()
    rows: List[pd.Series] = []
    for _, grp in work.groupby(["norm_ds", "file_set_type", "scenario_label"], dropna=False):
        rows.append(select_best_run(grp))
    return pd.DataFrame(rows).reset_index(drop=True) if rows else pd.DataFrame(columns=df.columns)


def md_table(df: pd.DataFrame, columns: Sequence[str]) -> str:
    if df.empty:
        return "_No rows._"
    use = df[[c for c in columns if c in df.columns]].copy()
    lines = [
        "| " + " | ".join(use.columns) + " |",
        "| " + " | ".join("---" for _ in use.columns) + " |",
    ]
    for _, row in use.iterrows():
        cells = []
        for c in use.columns:
            v = row[c]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                cells.append("")
            elif c in {
                "throughput_cluster_gb_hr",
                "throughput_scanner_gb_hr",
                "throughput_thread_gb_hr",
                "total_scan_hours",
                "total_size_gb",
            }:
                cells.append(_fmt_num(v, 2))
            elif c in {
                "throughput_cluster_files_hr",
                "throughput_scanner_files_hr",
                "throughput_thread_files_hr",
                "objects_scanned",
                "total_classification",
                "new_attributes",
                "removed_attributes",
            }:
                cells.append(_fmt_num(v, 0))
            else:
                cells.append(str(v).replace("|", "\\|")[:220])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def html_table(df: pd.DataFrame, columns: Sequence[str]) -> str:
    if df.empty:
        return "<div class='empty'>No rows.</div>"
    use = df[[c for c in columns if c in df.columns]].copy()
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in use.columns)
    body_rows: List[str] = []
    for _, row in use.iterrows():
        cells = []
        for c in use.columns:
            v = row[c]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                text_value = ""
            elif c in {
                "throughput_cluster_gb_hr",
                "throughput_scanner_gb_hr",
                "throughput_thread_gb_hr",
                "total_scan_hours",
                "total_size_gb",
            }:
                text_value = _fmt_num(v, 2)
            elif c in {
                "throughput_cluster_files_hr",
                "throughput_scanner_files_hr",
                "throughput_thread_files_hr",
                "objects_scanned",
                "total_classification",
                "new_attributes",
                "removed_attributes",
            }:
                text_value = _fmt_num(v, 0)
            else:
                text_value = str(v)[:240]
            cells.append(f"<td>{html.escape(text_value)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def bar_chart_html(df: pd.DataFrame, value_col: str, title: str, unit: str) -> str:
    if df.empty or value_col not in df.columns:
        return "<div class='empty'>No chart data.</div>"
    work = df.copy()
    work["scenario_label"] = work.apply(_scenario_label, axis=1)
    work["_value"] = pd.to_numeric(work[value_col], errors="coerce")
    work = work[work["_value"].notna()].sort_values("_value", ascending=False)
    if work.empty:
        return "<div class='empty'>No chart data.</div>"
    max_val = float(work["_value"].max()) or 1.0
    rows = []
    for _, r in work.iterrows():
        label = f"{r.get('data_source_type')} / {r.get('file_set_type')} / {r.get('scenario_label')}"
        width = max(2.0, min(100.0, float(r["_value"]) / max_val * 100.0))
        rows.append(
            "<div class='bar-row'>"
            f"<div class='bar-label'>{html.escape(label)}</div>"
            "<div class='bar-track'>"
            f"<div class='bar-fill' style='width:{width:.1f}%'></div>"
            "</div>"
            f"<div class='bar-value'>{html.escape(_fmt_num(r['_value'], 2 if unit == 'GB/hr' else 0))} {html.escape(unit)}</div>"
            "</div>"
        )
    return f"<section class='card'><h2>{html.escape(title)}</h2>{''.join(rows)}</section>"


def build_html_report(df: pd.DataFrame, best_df: pd.DataFrame, major_line: str) -> str:
    if df.empty:
        body = "<section class='card'><p>No active benchmark rows found in Postgres.</p></section>"
        title = "Benchmark report"
    else:
        full_major = df[df["build_version"].astype(str).map(_major_line) == major_line].copy()
        builds = sorted(full_major["build_version"].astype(str).unique().tolist(), key=_build_sort_key)
        best_df = best_df.copy()
        best_df["scenario_label"] = best_df.apply(_scenario_label, axis=1)
        best_df["sort_ds"] = best_df["data_source_type"].astype(str).str.lower()
        best_df["sort_file_set"] = best_df["file_set_type"].astype(str).str.lower()
        best_df = best_df.sort_values(["sort_ds", "sort_file_set", "scenario_label", "build_version"])

        all_runs_df = full_major.copy()
        all_runs_df["scenario_label"] = all_runs_df.apply(_scenario_label, axis=1)
        all_runs_df["sort_ds"] = all_runs_df["data_source_type"].astype(str).str.lower()
        all_runs_df["sort_file_set"] = all_runs_df["file_set_type"].astype(str).str.lower()
        all_runs_df["sort_build"] = all_runs_df["build_version"].map(_build_sort_key)
        all_runs_df = all_runs_df.sort_values(
            ["sort_ds", "sort_file_set", "scenario_label", "sort_build", "id"],
            ascending=[True, True, True, True, True],
        )

        scenario_counts = (
            all_runs_df.groupby(["data_source_type", "file_set_type", "scenario_label"], dropna=False)
            .size()
            .reset_index(name="run_count")
        )
        repeated = scenario_counts[scenario_counts["run_count"] > 1].copy()
        repeated = repeated.sort_values(["data_source_type", "file_set_type", "scenario_label"])

        best_cols = [
            "data_source_type",
            "file_set_type",
            "scenario_label",
            "build_version",
            "source_table",
            "throughput_cluster_gb_hr",
            "throughput_cluster_files_hr",
            "throughput_scanner_gb_hr",
            "throughput_thread_gb_hr",
            "total_scan_hours",
            "objects_scanned",
            "total_classification",
            "scan_id",
            "scan_status",
            "latest_state",
            "env_name",
        ]
        title = f"Best benchmark results for {major_line}.x"
        body = f"""
        <section class="hero">
          <div>
            <h1>{html.escape(title)}</h1>
            <p>Best-per-scenario rows feed charts and headline comparisons. All active runs remain in the detail table.</p>
          </div>
          <div class="metrics">
            <div><span>{html.escape(', '.join(builds) if builds else 'none')}</span><label>Builds included</label></div>
            <div><span>{len(full_major):,}</span><label>Active rows</label></div>
            <div><span>{len(best_df):,}</span><label>Best rows</label></div>
          </div>
        </section>
        <div class="grid">
          {bar_chart_html(best_df, "throughput_cluster_files_hr", "Best File Throughput", "files/hr")}
          {bar_chart_html(best_df, "throughput_cluster_gb_hr", "Best Data Throughput", "GB/hr")}
        </div>
        <section class="card">
          <h2>Best Result Per Scenario</h2>
          <p class="note">Deduped for graphs and headline comparisons. If a scenario has multiple runs, only the best run is shown here.</p>
          {html_table(best_df, best_cols)}
        </section>
        <section class="card">
          <h2>All Runs Detail</h2>
          <p class="note">Every active run in the selected major line is kept here, including duplicate S3/SMB runs.</p>
          {html_table(all_runs_df, best_cols)}
        </section>
        <section class="card">
          <h2>Repeated Scenario Runs</h2>
          <p class="note">These scenarios have more than one active run. Charts use the best row; detail keeps all rows.</p>
          {html_table(repeated, ["data_source_type", "file_set_type", "scenario_label", "run_count"])}
        </section>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f5f7fb; color: #172033; font-family: Arial, Helvetica, sans-serif; }}
    .page {{ max-width: 1320px; margin: 0 auto; padding: 28px; }}
    .hero {{ background: #13233f; color: white; padding: 26px 30px; display: flex; justify-content: space-between; gap: 24px; align-items: flex-end; border-radius: 8px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    h2 {{ margin: 0 0 12px; font-size: 20px; color: #172033; }}
    p {{ margin: 0; line-height: 1.45; }}
    .metrics {{ display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
    .metrics div {{ min-width: 135px; background: rgba(255,255,255,0.11); padding: 12px 14px; border: 1px solid rgba(255,255,255,0.18); border-radius: 6px; }}
    .metrics span {{ display: block; font-size: 22px; font-weight: 700; }}
    .metrics label {{ display: block; margin-top: 4px; font-size: 12px; color: #cbd6e8; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 18px; }}
    .card {{ background: white; border: 1px solid #dfe6f1; border-radius: 8px; padding: 18px; margin-top: 18px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04); overflow-x: auto; }}
    .note {{ color: #5b6475; margin-bottom: 14px; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ background: #edf2f8; color: #26344d; text-align: left; font-weight: 700; padding: 9px 10px; border-bottom: 1px solid #d8e1ee; white-space: nowrap; }}
    td {{ padding: 8px 10px; border-bottom: 1px solid #edf1f6; vertical-align: top; white-space: nowrap; }}
    tr:nth-child(even) td {{ background: #fafcff; }}
    .bar-row {{ display: grid; grid-template-columns: minmax(210px, 1.2fr) 2fr 110px; gap: 10px; align-items: center; margin: 10px 0; font-size: 12px; }}
    .bar-label {{ color: #2d374b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-track {{ background: #e7edf5; height: 12px; border-radius: 999px; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: #2563eb; border-radius: 999px; }}
    .bar-value {{ text-align: right; color: #172033; font-variant-numeric: tabular-nums; }}
    .empty {{ color: #6b7280; font-style: italic; }}
    @media (max-width: 900px) {{
      .hero {{ display: block; }}
      .metrics {{ justify-content: flex-start; margin-top: 18px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: 1fr; }}
      .bar-value {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    {body}
  </main>
</body>
</html>
"""


def build_markdown(df: pd.DataFrame, best_df: pd.DataFrame, major_line: str) -> str:
    lines: List[str] = []
    lines.append(f"# Best benchmark results for {major_line}.x")
    lines.append("")
    if df.empty:
        lines.append("_No active benchmark rows found in Postgres._")
        return "\n".join(lines)

    full_major = df[df["build_version"].astype(str).map(_major_line) == major_line].copy()
    builds = sorted(full_major["build_version"].astype(str).unique().tolist(), key=_build_sort_key)
    lines.append("## Scope")
    lines.append("")
    lines.append(f"- **Major line:** {major_line}.x")
    lines.append(f"- **Builds included:** {', '.join(builds) if builds else 'none'}")
    lines.append(f"- **Active rows in major:** {len(full_major)}")
    lines.append(f"- **Best scenario rows:** {len(best_df)}")
    lines.append("")

    if best_df.empty:
        lines.append("_No benchmark rows found for the selected major line._")
        return "\n".join(lines)

    best_df = best_df.copy()
    best_df["scenario_label"] = best_df.apply(_scenario_label, axis=1)
    best_df["sort_ds"] = best_df["data_source_type"].astype(str).str.lower()
    best_df["sort_file_set"] = best_df["file_set_type"].astype(str).str.lower()
    best_df = best_df.sort_values(["sort_ds", "sort_file_set", "scenario_label", "build_version"])

    lines.append("## Best Result Per Scenario")
    lines.append("")
    lines.append("_Use this deduplicated section for charts and headline comparisons. If a scenario has multiple runs, only the best run is shown here._")
    lines.append("")
    lines.append(
        md_table(
            best_df,
            [
                "data_source_type",
                "file_set_type",
                "scenario_label",
                "build_version",
                "source_table",
                "throughput_cluster_gb_hr",
                "throughput_cluster_files_hr",
                "throughput_scanner_gb_hr",
                "throughput_thread_gb_hr",
                "total_scan_hours",
                "objects_scanned",
                "total_classification",
                "scan_id",
                "scan_status",
                "latest_state",
                "env_name",
            ],
        )
    )
    lines.append("")

    all_runs_df = full_major.copy()
    all_runs_df["scenario_label"] = all_runs_df.apply(_scenario_label, axis=1)
    all_runs_df["sort_ds"] = all_runs_df["data_source_type"].astype(str).str.lower()
    all_runs_df["sort_file_set"] = all_runs_df["file_set_type"].astype(str).str.lower()
    all_runs_df["sort_build"] = all_runs_df["build_version"].map(_build_sort_key)
    all_runs_df = all_runs_df.sort_values(
        ["sort_ds", "sort_file_set", "scenario_label", "sort_build", "id"],
        ascending=[True, True, True, True, True],
    )

    lines.append("## All Runs Detail")
    lines.append("")
    lines.append("_Use this section for supporting data. It keeps every active run in the selected major line, including duplicate runs for the same DS/file-set/scenario._")
    lines.append("")
    lines.append(
        md_table(
            all_runs_df,
            [
                "data_source_type",
                "file_set_type",
                "scenario_label",
                "build_version",
                "source_table",
                "throughput_cluster_gb_hr",
                "throughput_cluster_files_hr",
                "throughput_scanner_gb_hr",
                "throughput_thread_gb_hr",
                "total_scan_hours",
                "objects_scanned",
                "total_classification",
                "scan_id",
                "scan_status",
                "latest_state",
                "env_name",
            ],
        )
    )
    lines.append("")

    scenario_counts = (
        all_runs_df.groupby(["data_source_type", "file_set_type", "scenario_label"], dropna=False)
        .size()
        .reset_index(name="run_count")
    )
    repeated = scenario_counts[scenario_counts["run_count"] > 1].copy()
    if not repeated.empty:
        repeated = repeated.sort_values(["data_source_type", "file_set_type", "scenario_label"])
        lines.append("## Repeated Scenario Runs")
        lines.append("")
        lines.append("_These scenarios have more than one active run in the selected major line. The best section uses one row from each; the all-runs section keeps them all._")
        lines.append("")
        lines.append(md_table(repeated, ["data_source_type", "file_set_type", "scenario_label", "run_count"]))
        lines.append("")

    tp = pd.to_numeric(best_df["throughput_cluster_files_hr"], errors="coerce")
    gb = pd.to_numeric(best_df["throughput_cluster_gb_hr"], errors="coerce")
    lines.append("## Observations")
    lines.append("")
    if tp.notna().any():
        r = best_df.loc[tp.idxmax()]
        lines.append(
            f"- Highest file throughput: **{_fmt_num(r.get('throughput_cluster_files_hr'), 0)} files/hr** "
            f"for `{r.get('data_source_type')}` / `{r.get('file_set_type')}` / `{_scenario_label(r)}` "
            f"from build **{r.get('build_version')}**."
        )
    if gb.notna().any():
        r = best_df.loc[gb.idxmax()]
        lines.append(
            f"- Highest data throughput: **{_fmt_num(r.get('throughput_cluster_gb_hr'), 2)} GB/hr** "
            f"for `{r.get('data_source_type')}` / `{r.get('file_set_type')}` / `{_scenario_label(r)}` "
            f"from build **{r.get('build_version')}**."
        )
    lines.append("- Selection rule: highest `throughput_cluster_gb_hr`, then highest `throughput_cluster_files_hr`, then shortest `total_scan_hours`.")
    lines.append("- Data source: Postgres `benchmarks` plus auto-only `auto_scan_benchmarks`, enriched with `scan_insights`, `scan_parents`, and latest `scan_progress` state.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Best benchmark HTML performance report from Postgres latest major line")
    ap.add_argument("--major-line", default="", help="Major build line to report, e.g. 252. Defaults to latest active major.")
    ap.add_argument("--out", type=str, default="", help="write HTML to this path")
    args = ap.parse_args()

    engine = create_engine(DB_CONNECTION_STRING, connect_args={"connect_timeout": 10})
    try:
        df = load_benchmarks(engine)
    except OperationalError as exc:
        print(
            "Could not connect to Postgres (check Postgres_* / DB_NAME in .env and network access). "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1

    major_line = str(args.major_line or "").strip() or pick_latest_major(df)
    if not major_line:
        print("No active benchmark major line found in Postgres.", file=sys.stderr)
        return 1

    best_df = best_rows_for_major(df, major_line)
    report_html = build_html_report(df, best_df, major_line)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_html, encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
    print(report_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
