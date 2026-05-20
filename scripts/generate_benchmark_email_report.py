#!/usr/bin/env python3
"""
Generate an email-friendly HTML benchmark report.

Initial focus:
- inline HTML suitable for email body
- visual style inspired by the reference PDF structure
- benchmark content for:
  - S3 / SMB build-by-build comparison
  - current build cross-DS comparison (S3 / SMB, optionally MSSQL / Postgres)
  - total classifier details
  - observed errors / RCA notes

Examples:
  python scripts/generate_benchmark_email_report.py \
    --current-build 249.1 \
    --previous-build 248.1 \
    --out reports/email_report_249_1.html

  python scripts/generate_benchmark_email_report.py \
    --current-build 249.1 \
    --previous-build 248.1 \
    --environment-id 4
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import os
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

def _default_cache_base() -> Path:
    for key in ("TMPDIR", "TEMP", "TMP"):
        v = os.getenv(key)
        if v:
            return Path(v)
    return Path(tempfile.gettempdir())

_CACHE_ROOT = _default_cache_base() / "scan_benchmark_report_cache"
_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_CACHE_ROOT / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(_CACHE_ROOT / "xdg"))

import matplotlib
import pandas as pd
from sqlalchemy import bindparam, create_engine, text

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from db_config import DB_CONNECTION_STRING  # noqa: E402
from benchmark_scenario_normalizer import (  # noqa: E402
    classify_scenario,
    normalize_benchmark_dataframe,
    normalize_ds_name,
)


UNSTRUCTURED_DS_ORDER = ["S3", "SMB"]
STRUCTURED_DS_ORDER = ["MSSQL", "Postgres"]
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402


def _db_has_column(engine, table: str, col: str) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SHOW COLUMNS FROM {table} LIKE :col"), {"col": col}).fetchone()
            return row is not None
    except Exception:
        return False


def _safe_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _fmt_num(v: Any, decimals: int = 1) -> str:
    x = _safe_float(v)
    if x is None:
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:,.{decimals}f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def _pct_delta(cur: Any, prev: Any) -> Optional[float]:
    c = _safe_float(cur)
    p = _safe_float(prev)
    if c is None or p is None or abs(p) < 1e-9:
        return None
    return ((c - p) / p) * 100.0


def _median_text(vals: Sequence[float]) -> str:
    arr = [float(x) for x in vals if x is not None]
    if not arr:
        return "—"
    return f"{statistics.median(arr):.1f}%"


def _build_chart_img(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def load_benchmarks(engine, *, environment_id: Optional[int]) -> pd.DataFrame:
    has_b_updated_at = _db_has_column(engine, "benchmarks", "updated_at")
    has_scanner_gb = _db_has_column(engine, "benchmarks", "throughput_scanner_gb_hr")
    has_thread_gb = _db_has_column(engine, "benchmarks", "throughput_thread_gb_hr")
    has_scanner_files = _db_has_column(engine, "benchmarks", "throughput_scanner_files_hr")
    has_thread_files = _db_has_column(engine, "benchmarks", "throughput_thread_files_hr")
    has_is_release = _db_has_column(engine, "builds", "release_milestone")
    updated_at_expr = "b.updated_at" if has_b_updated_at else "NULL AS updated_at"
    scanner_gb_expr = "b.throughput_scanner_gb_hr" if has_scanner_gb else "NULL AS throughput_scanner_gb_hr"
    thread_gb_expr = "b.throughput_thread_gb_hr" if has_thread_gb else "NULL AS throughput_thread_gb_hr"
    scanner_files_expr = "b.throughput_scanner_files_hr" if has_scanner_files else "NULL AS throughput_scanner_files_hr"
    thread_files_expr = "b.throughput_thread_files_hr" if has_thread_files else "NULL AS throughput_thread_files_hr"
    is_release_expr = "COALESCE(bu.release_milestone, 0) AS is_release" if has_is_release else "0 AS is_release"
    query = """
        SELECT
            b.id,
            bu.version AS build_version,
            COALESCE(b.scan_id, '') AS scan_id,
            COALESCE(b.scan_type, '') AS scan_type,
            COALESCE(b.data_source_type, '') AS data_source_type,
            COALESCE(b.file_set_type, '') AS file_set_type,
            COALESCE(conf.is_acl, 0) AS is_acl,
            COALESCE(conf.is_feature_flags, 0) AS is_feature_flags,
            COALESCE(conf.feature_flags, '') AS feature_flags,
            b.total_scan_hours,
            b.throughput_cluster_files_hr,
            b.throughput_cluster_gb_hr,
            {scanner_files_expr},
            {thread_files_expr},
            {scanner_gb_expr},
            {thread_gb_expr},
            b.total_files,
            b.total_size_gb,
            {updated_at_expr},
            sp.environment_id,
            COALESCE(sp.scan_status, '') AS scan_status,
            COALESCE(sp.scan_name, '') AS scan_name,
            COALESCE(si.total_classification, 0) AS total_classification,
            COALESCE(si.new_attributes_count, 0) AS new_attributes_count,
            COALESCE(si.removed_attributes_count, 0) AS removed_attributes_count,
            COALESCE(NULLIF(si.objects_scanned, 0), NULLIF(b.total_files, 0), 0) AS objects_scanned,
            {is_release_expr}
        FROM benchmarks b
        JOIN builds bu ON bu.id = b.build_id
        JOIN configurations conf ON conf.id = b.config_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        WHERE COALESCE(b.is_active, 1) = 1
    """.format(
        updated_at_expr=updated_at_expr,
        scanner_files_expr=scanner_files_expr,
        thread_files_expr=thread_files_expr,
        scanner_gb_expr=scanner_gb_expr,
        thread_gb_expr=thread_gb_expr,
        is_release_expr=is_release_expr,
    )
    params: Dict[str, Any] = {}
    if environment_id is not None:
        query += " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn, params=params)
    return normalize_benchmark_dataframe(df)


def load_available_builds(engine, *, environment_id: Optional[int]) -> List[Tuple[str, int]]:
    query = """
        SELECT bu.version AS build_version, COUNT(*) AS cnt
        FROM benchmarks b
        JOIN builds bu ON bu.id = b.build_id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        WHERE COALESCE(b.is_active, 1) = 1
    """
    params: Dict[str, Any] = {}
    if environment_id is not None:
        query += " AND sp.environment_id = :eid"
        params["eid"] = int(environment_id)
    query += """
        GROUP BY bu.version
        ORDER BY bu.version DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return [(str(r[0]), int(r[1] or 0)) for r in rows]


def pick_default_builds(available: Sequence[Tuple[str, int]]) -> Tuple[Optional[str], Optional[str]]:
    if not available:
        return None, None
    builds = [str(v) for v, cnt in available if str(v or "").strip()]
    if not builds:
        return None, None
    builds_sorted = sorted(builds, key=_build_sort_key, reverse=True)
    current = builds_sorted[0] if len(builds_sorted) >= 1 else None
    previous = builds_sorted[1] if len(builds_sorted) >= 2 else None
    return current, previous


def select_best_run(group: pd.DataFrame) -> pd.Series:
    if group.empty:
        raise ValueError("empty group")
    # Reset index to guarantee uniqueness — without this, idxmax()/idxmin() can return a
    # label shared by multiple rows (after filters/concats), causing .loc to return a
    # DataFrame instead of a Series and silently picking the wrong row.
    g = group.reset_index(drop=True)
    tp = pd.to_numeric(g["throughput_cluster_gb_hr"], errors="coerce")
    if tp.notna().any():
        return g.loc[tp.idxmax()]
    hrs = pd.to_numeric(g["total_scan_hours"], errors="coerce")
    if hrs.notna().any():
        return g.loc[hrs.idxmin()]
    return g.iloc[0]


def best_rows_for_build(df: pd.DataFrame, build_version: str) -> pd.DataFrame:
    part = df[df["build_version"].astype(str) == str(build_version).strip()].copy()
    if part.empty:
        return pd.DataFrame(columns=df.columns)
    rows: List[pd.Series] = []
    for _, grp in part.groupby(
        ["data_source_type", "scan_type", "file_set_type", "is_acl", "is_feature_flags"],
        dropna=False,
    ):
        rows.append(select_best_run(grp))
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=df.columns)


_STRUCTURED_DS = {"postgres", "mssql", "mysql", "oracle"}

def _is_structured_ds(ds_name: str) -> bool:
    return str(ds_name or "").lower().strip() in _STRUCTURED_DS

def _files_unit(ds_name: str) -> str:
    """Return 'tables' for structured DS, 'files' for everything else."""
    return "tables" if _is_structured_ds(ds_name) else "files"

def _files_label(ds_name: str, per_hour: bool = True) -> str:
    unit = _files_unit(ds_name)
    return f"{unit.capitalize()}/hour" if per_hour else unit.capitalize()


def _major_line(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    return s.split(".", 1)[0].strip()


def _build_sort_key(v: Any) -> Tuple[int, int, str]:
    s = str(v or "").strip()
    if not s:
        return (0, 0, s)
    parts = s.split(".")
    try:
        major = int(parts[0])
    except Exception:
        return (0, 0, s)
    try:
        minor = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        minor = 0
    return (major, minor, s)


def _pick_latest_two_major_lines(available: Sequence[Tuple[str, int]]) -> Tuple[Optional[str], Optional[str]]:
    majors = sorted({_major_line(v) for v, _ in available if _major_line(v)}, key=_build_sort_key, reverse=True)
    current_major = majors[0] if len(majors) >= 1 else None
    previous_major = majors[1] if len(majors) >= 2 else None
    return current_major, previous_major


def filter_major_line(df: pd.DataFrame, major_line: Optional[str]) -> pd.DataFrame:
    if df is None or df.empty or not major_line or "build_version" not in df.columns:
        return pd.DataFrame(columns=df.columns if df is not None else [])
    return df[df["build_version"].astype(str).map(_major_line) == str(major_line).strip()].copy()


def best_rows_for_major_line(df: pd.DataFrame, major_line: Optional[str]) -> pd.DataFrame:
    part = filter_major_line(df, major_line)
    if part.empty:
        return pd.DataFrame(columns=df.columns)
    rows: List[pd.Series] = []
    for _, grp in part.groupby(
        ["data_source_type", "scan_type", "file_set_type", "is_acl", "is_feature_flags"],
        dropna=False,
    ):
        rows.append(select_best_run(grp))
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=df.columns)


def _release_trend_chart_html(df: pd.DataFrame, ds_name: str, value_col: str, title: str, y_label: str) -> str:
    """Trend chart showing best Base (Full Scan) result per major version across ALL builds.
    X-axis labelled by major version (e.g. '252'). Multiple sub-versions → best throughput wins."""
    req = {"build_version", "data_source_type", value_col, "file_set_type"}
    if df is None or df.empty or not req.issubset(set(df.columns)):
        return "<div class='muted'>No trend data available.</div>"

    work = df.copy()
    work["norm_ds"] = work["data_source_type"].map(normalize_ds_name)
    work = work[work["norm_ds"] == ds_name].copy()
    if work.empty:
        return f"<div class='muted'>No {html.escape(ds_name)} data available.</div>"

    # Prefer Small Files; fall back to all if not present
    small = work[work["file_set_type"].astype(str).str.contains("Small Files", case=False, na=False)]
    if not small.empty:
        work = small.copy()

    # Full Scan baseline: no ACL, no feature flags, no OLS.
    if "is_acl" in work.columns:
        work = work[pd.to_numeric(work["is_acl"], errors="coerce").fillna(0).astype(int) == 0]
    if "is_feature_flags" in work.columns:
        work = work[pd.to_numeric(work["is_feature_flags"], errors="coerce").fillna(0).astype(int) == 0]
    if "scan_type" in work.columns:
        work = work[~work["scan_type"].astype(str).str.lower().str.contains("ols", na=False)]
    if work.empty:
        return f"<div class='muted'>No {html.escape(ds_name)} Full Scan data available.</div>"

    work["major_label"] = work["build_version"].astype(str).map(_major_line)
    rows = []
    for _, grp in work.groupby("major_label", dropna=False):
        rows.append(select_best_run(grp))
    plot_df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=work.columns)
    if plot_df.empty or value_col not in plot_df.columns:
        return "<div class='muted'>No release trend data available.</div>"

    plot_df["sort_key"] = plot_df["major_label"].map(lambda v: _build_sort_key(v))
    plot_df = plot_df.sort_values("sort_key")
    # Keep only the last 8 major release lines to avoid old/miscategorized entries (e.g. 249)
    _MAX_RELEASE_MAJORS = 8
    if len(plot_df) > _MAX_RELEASE_MAJORS:
        plot_df = plot_df.iloc[-_MAX_RELEASE_MAJORS:].copy()
    x_vals = plot_df["major_label"].astype(str).tolist()

    if value_col == "throughput_cluster_gb_hr":
        series_defs = [
            ("Cluster GB/hr", "throughput_cluster_gb_hr", "#8B5CF6", 3.4, 8),
            ("Per Scanner", "throughput_scanner_gb_hr", "#6366F1", 2.0, 5),
        ]
    else:
        _unit = _files_unit(ds_name).capitalize()
        series_defs = [
            (f"Cluster {_unit}/hr", "throughput_cluster_files_hr", "#10B981", 3.4, 8),
            ("Per Scanner", "throughput_scanner_files_hr", "#34D399", 2.0, 5),
        ]

    fig, ax = plt.subplots(figsize=(7.4, 3.6))
    plotted_any = False
    for label, col_name, color, lw, ms in series_defs:
        if col_name not in plot_df.columns:
            continue
        y_vals = pd.to_numeric(plot_df[col_name], errors="coerce")
        if y_vals.notna().sum() == 0:
            continue
        plotted_any = True
        ax.plot(x_vals, y_vals, marker="o", linewidth=lw, markersize=ms,
                color=color, markerfacecolor=color, markeredgewidth=0, label=label)
        # Annotate each point with its value
        for xi, yi in zip(x_vals, y_vals):
            if pd.notna(yi):
                ax.annotate(f"{yi:.1f}", (xi, yi), textcoords="offset points",
                            xytext=(0, 8), ha="center", fontsize=8, color=color)

    if not plotted_any:
        return "<div class='muted'>No release trend data available.</div>"

    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=14, color="#1f2937")
    ax.set_ylabel(y_label, fontsize=10, color="#4b5563", labelpad=10)
    ax.set_xlabel("Major Release", fontsize=10, color="#6b7280", labelpad=10)
    ax.grid(True, axis="y", color="#dbe5f1", linewidth=1.0, alpha=1.0)
    ax.grid(False, axis="x")
    ax.set_axisbelow(True)
    ax.set_facecolor("#ffffff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#d1d5db")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.tick_params(axis="x", labelrotation=0, labelsize=10, colors="#1f2937", pad=6)
    ax.tick_params(axis="y", labelsize=9, colors="#4b5563")
    ax.margins(x=0.12)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, ncol=2)
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.18, top=0.86)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def build_major_release_section(full_df: pd.DataFrame, include_structured: bool) -> str:
    """Card showing throughput trend across release builds only (is_release=1).
    Multiple sub-versions within the same major → best throughput wins."""
    if full_df is None or full_df.empty:
        return ""

    # Only include rows explicitly marked as release builds
    if "is_release" in full_df.columns:
        release_df = full_df[pd.to_numeric(full_df["is_release"], errors="coerce").fillna(0).astype(int) == 1].copy()
    else:
        release_df = full_df.copy()

    if release_df.empty:
        return ""

    ds_list = UNSTRUCTURED_DS_ORDER + (STRUCTURED_DS_ORDER if include_structured else [])
    panels = []
    for ds in ds_list:
        _fl = _files_label(ds)
        _fu = _files_unit(ds)
        gb_chart  = _release_trend_chart_html(release_df, ds, "throughput_cluster_gb_hr",
                                               f"{ds} — Data Throughput (GB/hr)", "GB/hour")
        fil_chart = _release_trend_chart_html(release_df, ds, "throughput_cluster_files_hr",
                                               f"{ds} — {_fu.capitalize()} Throughput ({_fu}/hr)", _fl)
        panels.append(f"""
        <div class="trend-panel">
          <div class="trend-panel-head">{html.escape(ds)}</div>
          <table style="width: 100%; border-collapse: collapse;">
            <tr>
              <td style="width: 50%; padding-right: 7px;">{gb_chart}</td>
              <td style="width: 50%; padding-left: 7px;">{fil_chart}</td>
            </tr>
          </table>
        </div>
        """)

    return f"""
    <div class="card card-release">
      <div class="section-title">📦 Major Release Benchmark Trends</div>
      <div class="section-subtitle">
        Release builds only (marked as Release Milestone). Best Base Full Scan result per major version.
        Multiple sub-versions in the same major → highest throughput is shown.
      </div>
      <div class="chart-band">
        {''.join(panels)}
      </div>
    </div>
    """


def _trend_chart_html(df: pd.DataFrame, ds_name: str, value_col: str, title: str, y_label: str) -> str:
    req = {"build_version", "data_source_type", value_col, "file_set_type"}
    if df is None or df.empty or not req.issubset(set(df.columns)):
        return "<div class='muted'>No trend data available.</div>"

    work = df.copy()
    work["norm_ds"] = work["data_source_type"].map(normalize_ds_name)
    work = work[work["norm_ds"] == ds_name].copy()
    small_file_work = work[work["file_set_type"].astype(str).str.contains("Small Files", case=False, na=False)].copy()
    if not small_file_work.empty:
        work = small_file_work
    # Base Full Scan only: no ACL, no FF, no OLS — keeps comparison consistent across builds.
    if "is_acl" in work.columns:
        work = work[pd.to_numeric(work["is_acl"], errors="coerce").fillna(0).astype(int) == 0]
    if "is_feature_flags" in work.columns:
        work = work[pd.to_numeric(work["is_feature_flags"], errors="coerce").fillna(0).astype(int) == 0]
    if "scan_type" in work.columns:
        work = work[~work["scan_type"].astype(str).str.lower().str.contains("ols", na=False)]
    if work.empty:
        return f"<div class='muted'>No {html.escape(ds_name)} Base Full Scan data available.</div>"

    # Group by major version (e.g. "253" covers "253.5" and "253.11") and pick best per major.
    work = work.copy()
    work["_major"] = work["build_version"].astype(str).map(_major_line)
    rows = []
    for _, grp in work.groupby(["_major"], dropna=False):
        rows.append(select_best_run(grp))
    plot_df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=work.columns)
    if plot_df.empty or value_col not in plot_df.columns:
        return "<div class='muted'>No trend data available.</div>"

    plot_df["_major"] = plot_df["build_version"].astype(str).map(_major_line)
    plot_df["sort_key"] = plot_df["_major"].map(_build_sort_key)
    plot_df = plot_df.sort_values("sort_key")
    fig, ax = plt.subplots(figsize=(7.4, 3.6))
    x_vals = plot_df["_major"].astype(str).tolist()
    if value_col == "throughput_cluster_gb_hr":
        series_defs = [
            ("Cluster", "throughput_cluster_gb_hr", "#8B5CF6", 3.4, 7),
            ("Per Scanner", "throughput_scanner_gb_hr", "#6366F1", 2.2, 5),
            ("Per Thread", "throughput_thread_gb_hr", "#A78BFA", 2.0, 5),
        ]
    else:
        _unit2 = _files_unit(ds_name).capitalize()
        series_defs = [
            (f"Cluster {_unit2}/hr", "throughput_cluster_files_hr", "#10B981", 3.4, 7),
            ("Per Scanner", "throughput_scanner_files_hr", "#34D399", 2.2, 5),
            ("Per Thread", "throughput_thread_files_hr", "#6EE7B7", 2.0, 5),
        ]

    plotted_any = False
    for label, col_name, color, line_width, marker_size in series_defs:
        if col_name not in plot_df.columns:
            continue
        y_vals = pd.to_numeric(plot_df[col_name], errors="coerce")
        if y_vals.notna().sum() == 0:
            continue
        plotted_any = True
        ax.plot(
            x_vals,
            y_vals,
            marker="o",
            linewidth=line_width,
            markersize=marker_size,
            color=color,
            markerfacecolor=color,
            markeredgewidth=0,
            label=label,
        )

    if not plotted_any:
        return "<div class='muted'>No trend data available.</div>"

    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=14, color="#1f2937")
    ax.set_ylabel(y_label, fontsize=10, color="#4b5563", labelpad=10)
    ax.set_xlabel("Build Version", fontsize=10, color="#6b7280", labelpad=10)
    ax.grid(True, axis="y", color="#dbe5f1", linewidth=1.0, alpha=1.0)
    ax.grid(False, axis="x")
    ax.set_axisbelow(True)
    ax.set_facecolor("#ffffff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#d1d5db")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.spines["left"].set_linewidth(1.0)
    ax.spines["bottom"].set_linewidth(1.0)
    ax.tick_params(axis="x", labelrotation=32, labelsize=9, colors="#4b5563", pad=6)
    ax.tick_params(axis="y", labelsize=9, colors="#4b5563")
    ax.margins(x=0.04)
    ax.legend(loc="upper right", frameon=False, fontsize=8.5, ncol=3, handlelength=2.2, columnspacing=1.2)
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.28, top=0.88)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def _build_ds_trend_panel(full_df: pd.DataFrame, ds_name: str) -> str:
    _fl = _files_label(ds_name)
    _fu = _files_unit(ds_name)
    return f"""
    <div class="trend-panel">
      <div class="trend-panel-head">{html.escape(ds_name)} Trends</div>
      <table style="width: 100%; border-collapse: collapse;">
        <tr>
          <td style="width: 50%; padding-right: 7px;">{_trend_chart_html(full_df, ds_name, "throughput_cluster_gb_hr", "Data Throughput Trend (GB/hr)", "GB/hour")}</td>
          <td style="width: 50%; padding-left: 7px;">{_trend_chart_html(full_df, ds_name, "throughput_cluster_files_hr", f"{_fu.capitalize()} Throughput Trend ({_fu}/hr)", _fl)}</td>
        </tr>
      </table>
    </div>
    """


def _cross_ds_bar_chart_html(cur_df: pd.DataFrame, value_col: str, title: str, y_label: str, ds_order: Sequence[str]) -> str:
    req = {"data_source_type", value_col}
    if cur_df is None or cur_df.empty or not req.issubset(set(cur_df.columns)):
        return "<div class='muted'>No current-build DS chart data available.</div>"

    work = cur_df.copy()
    work["norm_ds"] = work["data_source_type"].map(normalize_ds_name)
    ds_filter = set(ds_order)
    work = work[work["norm_ds"].isin(ds_filter)].copy()
    if work.empty:
        return "<div class='muted'>No current-build DS chart data available.</div>"

    best_rows = []
    for ds in ds_order:
        part = work[work["norm_ds"] == ds].copy()
        if part.empty:
            continue
        best_rows.append(select_best_run(part))
    plot_df = pd.DataFrame(best_rows) if best_rows else pd.DataFrame(columns=work.columns)
    if plot_df.empty:
        return "<div class='muted'>No current-build DS chart data available.</div>"

    plot_df["norm_ds"] = plot_df["data_source_type"].map(normalize_ds_name)
    plot_df = plot_df.sort_values(by="norm_ds", key=lambda s: s.map({k: i for i, k in enumerate(ds_order)}))

    fig, ax = plt.subplots(figsize=(8.6, 3.4))
    vals = pd.to_numeric(plot_df[value_col], errors="coerce").fillna(0.0)
    palette = {"S3": "#2563eb", "SMB": "#dc2626", "MSSQL": "#7c3aed", "Postgres": "#0f766e"}
    colors = [palette.get(str(ds), "#4b5563") for ds in plot_df["norm_ds"].astype(str)]
    bars = ax.bar(plot_df["norm_ds"].astype(str), vals, color=colors, width=0.62)
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel(y_label, fontsize=10)
    ax.grid(True, axis="y", alpha=0.18)
    ax.set_facecolor("#fbfdff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="x", labelrotation=0, labelsize=9)
    ax.tick_params(axis="y", labelsize=9)
    for bar, val in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), _fmt_num(val, 1), ha="center", va="bottom", fontsize=8)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def _build_trend_group(title: str, subtitle: str, full_df: pd.DataFrame, ds_order: Sequence[str], card_class: str) -> str:
    panels = "".join(_build_ds_trend_panel(full_df, ds_name) for ds_name in ds_order)
    return f"""
    <div class="card {html.escape(card_class)}">
      <div class="section-title">{html.escape(title)}</div>
      <div class="section-subtitle">{subtitle}</div>
      <div class="chart-band">
        {panels}
      </div>
    </div>
    """


def build_build_comparison_section(cur_df: pd.DataFrame, prev_df: pd.DataFrame, full_df: pd.DataFrame, current_major: str, previous_major: Optional[str], include_structured: bool) -> str:
    required_cols = {"data_source_type", "scan_type", "file_set_type", "is_acl", "is_feature_flags"}
    if cur_df is None or cur_df.empty or not required_cols.issubset(set(cur_df.columns)):
        return """
        <div class="card">
          <div class="section-title">Unstructured DS Trend Comparison</div>
          <div class="section-subtitle">Trend comparison by major version line.</div>
          <div class="muted">No benchmark rows were found for the selected current build.</div>
        </div>
        """

    subtitle = f"Last {len(set(full_df['build_version'].astype(str)))} builds across all major lines."
    sections = [
        _build_trend_group("Unstructured DS Trend Comparison", subtitle, full_df, UNSTRUCTURED_DS_ORDER, "card-warm")
    ]
    if include_structured:
        sections.append(
            _build_trend_group("Structured DS Trend Comparison", subtitle, full_df, STRUCTURED_DS_ORDER, "card-mint")
        )
    return "".join(sections)


def _ols_vs_base_bar_chart_html(cur_df: pd.DataFrame, value_col: str, title: str, y_label: str, ds_order: Sequence[str]) -> str:
    """Grouped bar chart: Base (Full Scan) vs OLS Scan, one group per DS."""
    req = {"data_source_type", value_col, "scan_type", "is_feature_flags"}
    if cur_df is None or cur_df.empty or not req.issubset(set(cur_df.columns)):
        return "<div class='muted'>No Full Scan vs OLS data available.</div>"

    work = cur_df.copy()
    work["norm_ds"] = work["data_source_type"].map(normalize_ds_name)
    work["scenario_label"] = work.apply(_scenario_label, axis=1)
    work = work[work["norm_ds"].isin(set(ds_order))].copy()
    if work.empty:
        return "<div class='muted'>No Full Scan vs OLS data available.</div>"

    scenarios = ["Base (Full Scan)", "OLS Scan"]
    ds_list = [ds for ds in ds_order if ds in work["norm_ds"].values]
    if not ds_list:
        return "<div class='muted'>No Full Scan vs OLS data available.</div>"

    # Best value per (ds, scenario)
    data: Dict[str, Dict[str, float]] = {scen: {} for scen in scenarios}
    for ds in ds_list:
        for scen in scenarios:
            part = work[(work["norm_ds"] == ds) & (work["scenario_label"] == scen)]
            if not part.empty:
                best = pd.to_numeric(part[value_col], errors="coerce").max()
                if pd.notna(best):
                    data[scen][ds] = float(best)

    # Only render if at least one OLS value exists
    has_ols = any(data["OLS Scan"])
    if not has_ols:
        return "<div class='muted'>No OLS Scan data available for comparison.</div>"

    x = list(range(len(ds_list)))
    width = 0.38
    palette = {
        "Base (Full Scan)": "#2563eb",
        "OLS Scan": "#7c3aed",
    }
    fig, ax = plt.subplots(figsize=(8.6, 3.4))
    for i, (scen, offset) in enumerate(zip(scenarios, [-width / 2, width / 2])):
        vals = [data[scen].get(ds, 0.0) for ds in ds_list]
        bars = ax.bar([xi + offset for xi in x], vals, width=width,
                      color=palette[scen], label=scen)
        for bar, val in zip(bars, vals):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        _fmt_num(val, 1), ha="center", va="bottom", fontsize=8)
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel(y_label, fontsize=10)
    ax.set_xticks(x)
    ax.set_xticklabels(ds_list, fontsize=9)
    ax.grid(True, axis="y", alpha=0.18)
    ax.set_facecolor("#fbfdff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="x", labelrotation=0, labelsize=9)
    ax.tick_params(axis="y", labelsize=9)
    ax.legend(loc="upper right", frameon=False, fontsize=8.5)
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.14, top=0.88)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def _build_cross_ds_group(title: str, subtitle: str, current_build_df: pd.DataFrame, ds_order: Sequence[str], card_class: str, show_ols_comparison: bool = False) -> str:
    _all_structured = bool(ds_order) and all(_is_structured_ds(ds) for ds in ds_order)
    _f_unit = "Tables" if _all_structured else "Files"
    _f_label = f"{_f_unit}/hr"
    ols_chart = ""
    if show_ols_comparison:
        ols_chart = f"""
        <div style="display:flex; align-items:center; margin: 20px 0 12px 0; gap: 12px;">
          <hr style="flex:1; border:none; border-top: 1.5px solid #e5e7eb; margin:0;" />
          <span style="font-size:13px; font-weight:700; color:#6b7280; white-space:nowrap; letter-spacing:0.04em; text-transform:uppercase;">
            Full Scan vs OLS Scan Comparison
          </span>
          <hr style="flex:1; border:none; border-top: 1.5px solid #e5e7eb; margin:0;" />
        </div>
        <table style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="width: 50%; padding-right: 7px;">{_ols_vs_base_bar_chart_html(current_build_df, "throughput_cluster_gb_hr", "Full Scan vs OLS — GB/hr", "GB/hr", ds_order)}</td>
            <td style="width: 50%; padding-left: 7px;">{_ols_vs_base_bar_chart_html(current_build_df, "throughput_cluster_files_hr", f"Full Scan vs OLS — {_f_label}", _f_label, ds_order)}</td>
          </tr>
        </table>
        """
    return f"""
    <div class="card {html.escape(card_class)}">
      <div class="section-title">{html.escape(title)}</div>
      <div class="section-subtitle">{subtitle}</div>
      <div class="chart-band">
        <table style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="width: 50%; padding-right: 7px;">{_cross_ds_bar_chart_html(current_build_df, "throughput_cluster_gb_hr", "Data Throughput By DS", "GB/hr", ds_order)}</td>
            <td style="width: 50%; padding-left: 7px;">{_cross_ds_bar_chart_html(current_build_df, "throughput_cluster_files_hr", f"{_f_unit} Throughput By DS", _f_label, ds_order)}</td>
          </tr>
        </table>
        {ols_chart}
      </div>
    </div>
    """


def build_cross_ds_section(current_build_df: pd.DataFrame, current_build: str, include_structured: bool) -> str:
    required_cols = {"data_source_type", "scan_type", "file_set_type", "is_acl"}
    if current_build_df is None or current_build_df.empty or not required_cols.issubset(set(current_build_df.columns)):
        return """
        <div class="card">
          <div class="section-title">Current Build Unstructured DS Comparison</div>
          <div class="section-subtitle">S3 and SMB side-by-side for the selected current build.</div>
          <div class="muted">No cross-DS benchmark rows were found for the selected current build.</div>
        </div>
        """
    subtitle = f"Best available result inside build <strong>{html.escape(current_build)}</strong>."
    sections = [
        _build_cross_ds_group("Current Build Unstructured DS Comparison", subtitle, current_build_df, UNSTRUCTURED_DS_ORDER, "card-sky", show_ols_comparison=True)
    ]
    if include_structured:
        sections.append(
            _build_cross_ds_group("Current Build Structured DS Comparison", subtitle, current_build_df, STRUCTURED_DS_ORDER, "card-mint")
        )
    return "".join(sections)


def _scenario_label(row: pd.Series) -> str:
    return classify_scenario(row)


def build_scenario_matrix_section(
    current_build_df: pd.DataFrame,
    current_build: str,
    include_structured: bool,
    cur_major_df: Optional[pd.DataFrame] = None,
    prev_major_df: Optional[pd.DataFrame] = None,
    current_major: Optional[str] = None,
    previous_major: Optional[str] = None,
) -> str:
    # Use best-of-major-line data when available; fall back to single-build data
    work_df = cur_major_df if (cur_major_df is not None and not cur_major_df.empty) else current_build_df
    label = f"{current_major}.x (best)" if current_major and cur_major_df is not None and not cur_major_df.empty else current_build

    if work_df is None or work_df.empty or "data_source_type" not in work_df.columns:
        return f"""
        <div class="card">
          <div class="section-title">Scenario Matrix</div>
          <div class="muted">No runs found for {html.escape(label)}.</div>
        </div>
        """

    work = work_df.copy()
    work["norm_ds"] = work.get("matrix_data_source_type", work["data_source_type"]).map(normalize_ds_name)
    if "matrix_file_set_type" in work.columns:
        work["file_set_type"] = work["matrix_file_set_type"]
    ds_scope = UNSTRUCTURED_DS_ORDER + (STRUCTURED_DS_ORDER if include_structured else [])
    work = work[work["norm_ds"].isin(ds_scope)].copy()
    if work.empty:
        scope_text = "S3 / SMB / MSSQL / Postgres" if include_structured else "S3 / SMB"
        return f"""
        <div class="card">
          <div class="section-title">Scenario Matrix</div>
          <div class="muted">No {html.escape(scope_text)} rows found for {html.escape(label)}.</div>
        </div>
        """
    work["scenario_label"] = work.apply(_scenario_label, axis=1)
    work["sort_hours"] = pd.to_numeric(work.get("total_scan_hours"), errors="coerce")
    work["sort_gb"] = pd.to_numeric(work.get("throughput_cluster_gb_hr"), errors="coerce")

    # Deduplicate: keep best throughput row per (ds, file_set, scenario_label).
    # This prevents duplicate rows when two scan_type values map to the same scenario
    # (e.g. "ols_scan" and "ols" both → "OLS Scan").
    dedup_rows: List[pd.Series] = []
    for _, grp in work.groupby(["norm_ds", "file_set_type", "scenario_label"], dropna=False):
        dedup_rows.append(select_best_run(grp))
    work = pd.DataFrame(dedup_rows).reset_index(drop=True) if dedup_rows else work

    # Build prev-major lookup: (norm_ds, file_set_type, scenario_label) → best GB/hr
    prev_best: Dict[Tuple[str, str, str], float] = {}
    if prev_major_df is not None and not prev_major_df.empty:
        pm = prev_major_df.copy()
        pm["norm_ds"] = pm.get("matrix_data_source_type", pm["data_source_type"]).map(normalize_ds_name)
        if "matrix_file_set_type" in pm.columns:
            pm["file_set_type"] = pm["matrix_file_set_type"]
        pm["scenario_label"] = pm.apply(_scenario_label, axis=1)
        # Deduplicate prev major the same way
        pm_dedup: List[pd.Series] = []
        for _, grp in pm.groupby(["norm_ds", "file_set_type", "scenario_label"], dropna=False):
            pm_dedup.append(select_best_run(grp))
        pm = pd.DataFrame(pm_dedup).reset_index(drop=True) if pm_dedup else pm
        for _, r in pm.iterrows():
            tp = _safe_float(r.get("throughput_cluster_gb_hr"))
            if tp and tp > 0:
                key = (
                    str(r.get("norm_ds") or "").strip(),
                    str(r.get("file_set_type") or "").strip(),
                    str(r.get("scenario_label") or "").strip(),
                )
                prev_best[key] = max(prev_best.get(key, 0.0), tp)

    show_prev_col = bool(prev_best)
    prev_col_header = f"vs {previous_major}.x" if previous_major else "vs Prev Major"

    scenario_order = {
        "Base (Full Scan)": 0,
        "ACL On": 1,
        "Structured FF": 2,
        "ACL + Structured FF": 3,
        "OLS Scan": 4,
        "ACL + OLS Scan": 5,
        "OLS + Structured FF": 6,
        "ACL + OLS + Structured FF": 7,
    }
    # File set order: Small Files first, then Large Files, then CSV, then anything else
    _FILE_SET_ORDER = {"small": 0, "large": 1, "csv": 2}

    def _file_set_rank(fs: str) -> int:
        s = str(fs or "").lower()
        if "small" in s:
            return 0
        if "large" in s:
            return 1
        if "csv" in s:
            return 2
        return 3

    cards: List[str] = []
    ds_order = {k: i for i, k in enumerate(ds_scope)}
    grouped = list(work.groupby(["norm_ds", "file_set_type"], dropna=False))
    grouped.sort(key=lambda item: (ds_order.get(item[0][0], 999), _file_set_rank(str(item[0][1] or "")), str(item[0][1] or "")))
    for (ds, file_set), part in grouped:
        if not ds:
            continue
        part = part.copy()
        part["scenario_rank"] = part["scenario_label"].map(lambda s: scenario_order.get(str(s), 99))
        part = part.sort_values(["scenario_rank", "sort_hours", "sort_gb"], ascending=[True, True, False])

        base_candidates = part[part["scenario_label"] == "Base (Full Scan)"]
        base_row_series = select_best_run(base_candidates) if not base_candidates.empty else None
        base_gb = _safe_float(base_row_series.get("throughput_cluster_gb_hr")) if base_row_series is not None else None
        rows_html = []
        for _, row in part.iterrows():
            cur_gb = _safe_float(row.get("throughput_cluster_gb_hr"))
            scen = str(row.get("scenario_label") or "")

            # vs Base (within current major)
            vs_base = _pct_delta(cur_gb, base_gb) if base_gb not in (None, 0) and scen != "Base (Full Scan)" else None
            vs_base_text = _fmt_pct(vs_base) if scen != "Base (Full Scan)" else ""
            vs_base_cls = "pos" if vs_base is not None and vs_base >= 0 else ("neg" if vs_base is not None else "")

            # vs Prev Major
            pm_key = (str(ds).strip(), str(file_set or "").strip(), scen.strip())
            pm_tp = prev_best.get(pm_key)
            vs_prev = _pct_delta(cur_gb, pm_tp) if pm_tp else None
            vs_prev_text = _fmt_pct(vs_prev) if vs_prev is not None else ("N/A" if show_prev_col else "")
            vs_prev_cls = "pos" if vs_prev is not None and vs_prev >= 0 else ("neg" if vs_prev is not None else "")

            prev_cell = f"<td class='{vs_prev_cls}'>{html.escape(vs_prev_text)}</td>" if show_prev_col else ""
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(scen or '—')}</td>"
                f"<td>{_fmt_num(row.get('throughput_cluster_gb_hr'), 2)}</td>"
                f"<td>{_fmt_num(row.get('throughput_cluster_files_hr'), 0)}</td>"
                f"<td>{_fmt_num(row.get('total_scan_hours'), 2)}</td>"
                f"<td>{_fmt_num(row.get('objects_scanned'), 0)}</td>"
                f"<td class='{vs_base_cls}'>{html.escape(vs_base_text) if vs_base_text else ''}</td>"
                f"{prev_cell}"
                "</tr>"
            )
        prev_th = f"<th>{html.escape(prev_col_header)}</th>" if show_prev_col else ""
        cards.append(
            f"""
            <details class="matrix-card" open>
              <summary class="matrix-summary">{html.escape(ds)} — {html.escape(str(file_set or '—'))} ({len(part)} scenario(s))</summary>
              <table class="report-table">
                <thead>
                  <tr>
                    <th>Scenario</th><th>GB/hr</th><th>{"Tables/hr" if _is_structured_ds(ds) else "Files/hr"}</th><th>Scan Hrs</th><th>Objects</th><th>vs Base</th>{prev_th}
                  </tr>
                </thead>
                <tbody>
                  {''.join(rows_html)}
                </tbody>
              </table>
            </details>
            """
        )
    if not cards:
        return f"""
        <div class="card">
          <div class="section-title">Scenario Matrix</div>
          <div class="muted">No {'S3 / SMB / MSSQL / Postgres' if include_structured else 'S3 / SMB'} rows found for {html.escape(label)}.</div>
        </div>
        """
    return f"""
    <div class="card card-mint">
      <div class="section-title">Scenario Matrix</div>
      <div class="section-subtitle">
        Best result per scenario across <strong>{html.escape(label)}</strong>.
        {f'Compared against best of <strong>{html.escape(previous_major)}.x</strong>.' if show_prev_col and previous_major else ''}
      </div>
      {''.join(cards)}
    </div>
    """


def load_classifier_detail(engine, current_build: str, previous_build: Optional[str], environment_id: Optional[int]) -> Dict[str, Any]:
    params: Dict[str, Any] = {"cur": str(current_build).strip()}
    env_filter = ""
    if environment_id is not None:
        env_filter = " AND bcm.environment_id = :eid"
        params["eid"] = int(environment_id)

    def _best_version_with_classifiers(conn, major: str, env_f: str, p: Dict) -> str:
        """Return the most recent build version in this major that has classifier mappings."""
        rows = conn.execute(
            text(f"""
                SELECT b.version
                FROM build_classifier_mappings bcm
                JOIN builds b ON b.id = bcm.build_id
                WHERE b.version LIKE :major_prefix {env_f}
                GROUP BY b.version
                ORDER BY MAX(bcm.id) DESC
                LIMIT 1
            """),
            {**p, "major_prefix": f"{major}.%"},
        ).fetchone()
        return str(rows[0]).strip() if rows else ""

    with engine.connect() as conn:
        cur_count = conn.execute(
            text(f"""
                SELECT COUNT(DISTINCT bcm.classifier_id)
                FROM build_classifier_mappings bcm
                JOIN builds b ON b.id = bcm.build_id
                WHERE b.version = :cur {env_filter}
            """),
            params,
        ).scalar() or 0

        # If no classifiers found for the exact build, fall back to the most recent
        # build in the same major line that has classifier data.
        effective_cur = str(current_build).strip()
        if int(cur_count) == 0:
            cur_major = str(current_build).split(".")[0]
            fallback = _best_version_with_classifiers(conn, cur_major, env_filter, params)
            if fallback and fallback != effective_cur:
                effective_cur = fallback
                params_fb = dict(params)
                params_fb["cur"] = effective_cur
                cur_count = conn.execute(
                    text(f"""
                        SELECT COUNT(DISTINCT bcm.classifier_id)
                        FROM build_classifier_mappings bcm
                        JOIN builds b ON b.id = bcm.build_id
                        WHERE b.version = :cur {env_filter}
                    """),
                    params_fb,
                ).scalar() or 0
                params = params_fb

        cur_names = [
            str(r[0]).strip()
            for r in conn.execute(
                text(f"""
                    SELECT DISTINCT c.name
                    FROM build_classifier_mappings bcm
                    JOIN builds b ON b.id = bcm.build_id
                    JOIN classifiers c ON c.id = bcm.classifier_id
                    WHERE b.version = :cur {env_filter}
                    ORDER BY c.name
                """),
                params,
            ).fetchall()
            if r and r[0]
        ]

        prev_names: List[str] = []
        prev_count = None
        if previous_build:
            params_prev = dict(params)
            params_prev["prev"] = str(previous_build).strip()
            prev_count = conn.execute(
                text(f"""
                    SELECT COUNT(DISTINCT bcm.classifier_id)
                    FROM build_classifier_mappings bcm
                    JOIN builds b ON b.id = bcm.build_id
                    WHERE b.version = :prev {env_filter}
                """),
                params_prev,
            ).scalar()
            # Same fallback for previous build
            if (prev_count or 0) == 0:
                prev_major = str(previous_build).split(".")[0]
                fallback_prev = _best_version_with_classifiers(conn, prev_major, env_filter, params_prev)
                if fallback_prev:
                    params_prev["prev"] = fallback_prev
                    prev_count = conn.execute(
                        text(f"""
                            SELECT COUNT(DISTINCT bcm.classifier_id)
                            FROM build_classifier_mappings bcm
                            JOIN builds b ON b.id = bcm.build_id
                            WHERE b.version = :prev {env_filter}
                        """),
                        params_prev,
                    ).scalar()
            prev_names = [
                str(r[0]).strip()
                for r in conn.execute(
                    text(f"""
                        SELECT DISTINCT c.name
                        FROM build_classifier_mappings bcm
                        JOIN builds b ON b.id = bcm.build_id
                        JOIN classifiers c ON c.id = bcm.classifier_id
                        WHERE b.version = :prev {env_filter}
                        ORDER BY c.name
                    """),
                    params_prev,
                ).fetchall()
                if r and r[0]
            ]

    cur_set = set(cur_names)
    prev_set = set(prev_names)
    return {
        "current_count": int(cur_count or 0),
        "previous_count": int(prev_count or 0) if prev_count is not None else None,
        "new_names": sorted(cur_set - prev_set),
        "missing_names": sorted(prev_set - cur_set),
        "sample_current": cur_names[:24],
        "effective_build": effective_cur,
    }


def build_classifier_section(detail: Dict[str, Any]) -> str:
    cur = detail.get("current_count")
    prev = detail.get("previous_count")
    new_names = detail.get("new_names") or []
    missing_names = detail.get("missing_names") or []
    cards = [
        f"<div style='font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;'>Current Build</div><div style='font-size: 24px; font-weight: 800; color: #111827;'>{cur:,}</div>",
        f"<div style='font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;'>Previous Build</div><div style='font-size: 24px; font-weight: 800; color: #111827;'>{prev:,}</div>" if prev is not None else "",
        f"<div style='font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;'>New Classifiers</div><div style='font-size: 24px; font-weight: 800; color: #16a34a;'>{len(new_names):,}</div>",
        f"<div style='font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;'>Missing vs Previous</div><div style='font-size: 24px; font-weight: 800; color: #dc2626;'>{len(missing_names):,}</div>",
    ]

    def _pill_list(items: Sequence[str], empty_text: str, cls_name: str) -> str:
        if not items:
            return f"<div class='muted'>{html.escape(empty_text)}</div>"
        pills = "".join(f"<span class='pill {cls_name}'>{html.escape(x)}</span>" for x in items[:24])
        extra = f"<div class='muted more-note'>+{len(items)-24} more</div>" if len(items) > 24 else ""
        return pills + extra

    return f"""
    <div class="card">
      <div class="section-title">Total Classifier Details</div>
      <table style="width: 100%; border-collapse: separate; border-spacing: 14px 0; margin-bottom: 18px; margin-left: -14px;">
        <tr>
          {''.join(f"<td style='padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #f8fafc; text-align: left; width: 25%;'>{c}</td>" for c in cards if c)}
        </tr>
      </table>
      <table style="width: 100%; border-collapse: separate; border-spacing: 20px 0; margin-left: -20px;">
        <tr>
          <td style="width: 50%; vertical-align: top;">
            <div class="subhead">New in current build</div>
            {_pill_list(new_names, 'No new classifier names vs previous build.', 'pill-pos')}
          </td>
          <td style="width: 50%; vertical-align: top;">
            <div class="subhead">Missing from current build</div>
            {_pill_list(missing_names, 'No missing classifier names vs previous build.', 'pill-neg')}
          </td>
        </tr>
      </table>
    </div>
    """


def load_error_rca(engine, scan_ids: Iterable[str]) -> List[Dict[str, Any]]:
    cleaned = [str(x).strip() for x in scan_ids if str(x or "").strip()]
    if not cleaned:
        return []
    has_created_at = _db_has_column(engine, "scan_log_errors", "created_at")
    has_log_timestamp = _db_has_column(engine, "scan_log_errors", "log_timestamp")
    time_expr = "created_at" if has_created_at else ("log_timestamp" if has_log_timestamp else "NULL")
    order_expr = "created_at DESC, id DESC" if has_created_at else ("log_timestamp DESC, id DESC" if has_log_timestamp else "id DESC")
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT scan_id, log_source, error_context, ai_analysis, {time_expr} AS created_at
                FROM scan_log_errors
                WHERE scan_id IN :ids
                ORDER BY {order_expr}
                LIMIT 20
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": tuple(cleaned)},
        ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "scan_id": str(row[0] or "").strip(),
                "log_source": str(row[1] or "").strip(),
                "error_context": str(row[2] or "").strip(),
                "ai_analysis": str(row[3] or "").strip(),
                "created_at": row[4],
            }
        )
    return out


def _extract_error_lines(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    keep: List[str] = []
    for line in txt.splitlines():
        s = str(line or "").strip()
        if not s:
            continue
        up = s.upper()
        if "[INFO]" in up or up.startswith("INFO") or " INFO " in up:
            continue
        if any(tok in up for tok in ["ERROR", "EXCEPTION", "FAILED", "TRACEBACK", "FATAL", "CRASH"]):
            keep.append(s)
    if keep:
        return " | ".join(keep[:3])
    return txt[:240]


def build_error_section(items: List[Dict[str, Any]]) -> str:
    if not items:
        return """
        <div class="card">
          <div class="section-title">Observed Issues / Errors / RCA</div>
          <div class="muted">No linked scan-log issues were found for the selected scans.</div>
        </div>
        """
    blocks = []
    for item in items[:8]:
        raw_analysis = str(item.get("ai_analysis") or "").strip()
        raw_context = _extract_error_lines(item.get("error_context") or "")
        if not raw_context:
            continue
        context = html.escape(raw_context[:360])
        blocks.append(
            f"""
            <div class="issue">
              <div class="issue-head">
                <span class="issue-scan">{html.escape(item.get('scan_id') or '')}</span>
                <span class="issue-source">{html.escape(item.get('log_source') or 'unknown')}</span>
              </div>
              <div class="issue-body"><strong>Error:</strong> {context}</div>
              {f"<div class='issue-body'><strong>RCA:</strong> {html.escape(raw_analysis)}</div>" if raw_analysis and 'AI Analysis failed' not in raw_analysis and 'AI analysis failed' not in raw_analysis else ""}
            </div>
            """
        )
    if not blocks:
        return """
        <div class="card">
          <div class="section-title">Observed Issues / Errors / RCA</div>
          <div class="muted">No error-level log lines were found for the selected scans.</div>
        </div>
        """
    return f"""
    <div class="card">
      <div class="section-title">Observed Issues / Errors / RCA</div>
      <div class="section-subtitle">Latest scan-linked log issues and stored AI analysis.</div>
      {''.join(blocks)}
    </div>
    """


def _filter_rows_to_windows(df: pd.DataFrame, windows: Sequence[Tuple[datetime, datetime]]) -> pd.DataFrame:
    if df is None or df.empty or "captured_at_utc" not in df.columns or not windows:
        return pd.DataFrame(columns=df.columns if df is not None else [])
    out = df.copy()
    out["captured_at_utc"] = pd.to_datetime(out["captured_at_utc"], errors="coerce", utc=True)
    out = out.dropna(subset=["captured_at_utc"])
    if out.empty:
        return out
    keep = pd.Series(False, index=out.index)
    for start_dt, end_dt in windows:
        s_dt = pd.to_datetime(start_dt, utc=True)
        e_dt = pd.to_datetime(end_dt, utc=True)
        keep = keep | ((out["captured_at_utc"] >= s_dt) & (out["captured_at_utc"] <= e_dt))
    return out[keep].copy()


def load_scanner_performance_for_build(engine, current_build: str, environment_id: Optional[int]) -> Dict[str, Any]:
    """
    Aggregate scanner performance group-wise during benchmark scans for one exact build.
    Returns per-environment data so the caller can render separate sections.
    """
    if not current_build:
        return {"available": False, "reason": "No current build."}

    b_has_scan_start = _db_has_column(engine, "benchmarks", "scan_start_time")
    b_has_scan_end = _db_has_column(engine, "benchmarks", "scan_end_time")
    sp_has_completed_at = _db_has_column(engine, "scan_parents", "completed_at")
    sp_has_started_at = _db_has_column(engine, "scan_parents", "started_at")
    sp_has_last_seen = _db_has_column(engine, "scan_parents", "last_seen_at")

    start_parts = []
    if sp_has_started_at:
        start_parts.append("sp.started_at")
    if b_has_scan_start:
        start_parts.append("b.scan_start_time")
    start_expr = "COALESCE(" + ", ".join(start_parts) + ")" if start_parts else "NULL"

    end_parts = []
    if sp_has_completed_at:
        end_parts.append("sp.completed_at")
    if b_has_scan_end:
        end_parts.append("b.scan_end_time")
    if sp_has_last_seen:
        end_parts.append("sp.last_seen_at")
    end_parts.extend(start_parts)
    end_expr = "COALESCE(" + ", ".join(end_parts) + ")" if end_parts else "NULL"

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
                    JOIN builds bu ON bu.id = b.build_id
                    LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
                    WHERE bu.version = :build
                      AND sp.environment_id IS NOT NULL
                      AND TRIM(COALESCE(b.scan_id, '')) <> ''
                      {env_filter}
                """),
                params,
            ).fetchall()
    except Exception as exc:
        return {"available": False, "reason": f"Could not load build scan windows: {exc}"}

    # Group windows by environment_id
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

    # Fetch data for all envs in one query, then split per env
    all_env_ids = tuple(sorted(env_windows.keys()))
    global_start = min(w[0] for wins in env_windows.values() for w in wins)
    global_end   = max(w[1] for wins in env_windows.values() for w in wins)
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

    # Build per-environment result dicts
    per_env: List[Dict[str, Any]] = []
    for eid, windows in sorted(env_windows.items()):
        env_start = min(w[0] for w in windows)
        env_end   = max(w[1] for w in windows)

        group_df = _filter_rows_to_windows(
            group_df_all[group_df_all["environment_id"] == eid].copy() if not group_df_all.empty else pd.DataFrame(),
            windows,
        )
        individual_df = _filter_rows_to_windows(
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
            # If cpu_pct column is all null, fall back to system_load_average if present
            if individual_df["cpu_pct"].isna().all() and "system_load_average" in individual_df.columns:
                individual_df["cpu_pct"] = pd.to_numeric(individual_df["system_load_average"], errors="coerce")
            individual_df["group_name"] = individual_df.get("group_name", "").fillna("").astype(str).str.strip().replace({"": "default"})
            # Keep rows where cpu_pct is a valid number (including 0.0 — don't drop zeros)
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


def _time_series_chart_html(df: pd.DataFrame, x_col: str, y_col: str, title: str, y_label: str, color: str, fill: bool = False) -> str:
    if df is None or df.empty or x_col not in df.columns or y_col not in df.columns:
        return "<div class='muted'>No chart data available.</div>"
    plot_df = df[[x_col, y_col]].copy()
    plot_df[x_col] = pd.to_datetime(plot_df[x_col], errors="coerce")
    plot_df[y_col] = pd.to_numeric(plot_df[y_col], errors="coerce")
    plot_df = plot_df.dropna(subset=[x_col, y_col]).sort_values(x_col)
    if plot_df.empty:
        return "<div class='muted'>No chart data available.</div>"

    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    ax.plot(plot_df[x_col], plot_df[y_col], marker="o", linewidth=2.4, markersize=4.5, color=color, markeredgewidth=0)
    if fill:
        ax.fill_between(plot_df[x_col], plot_df[y_col], color=color, alpha=0.14)
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left", pad=12, color="#1f2937")
    ax.set_ylabel(y_label, fontsize=10, color="#4b5563", labelpad=8)
    ax.grid(True, axis="y", color="#dbe5f1", linewidth=1.0)
    ax.grid(False, axis="x")
    ax.set_facecolor("#ffffff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#d1d5db")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.tick_params(axis="x", labelrotation=28, labelsize=8.5, colors="#4b5563")
    ax.tick_params(axis="y", labelsize=9, colors="#4b5563")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.28, top=0.86)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def _group_series_chart_html(df: pd.DataFrame, y_col: str, title: str, y_label: str) -> str:
    req = {"captured_at_utc", "group_name", y_col}
    if df is None or df.empty or not req.issubset(set(df.columns)):
        return "<div class='muted'>No group chart data available.</div>"
    plot_df = df[list(req)].copy()
    plot_df["captured_at_utc"] = pd.to_datetime(plot_df["captured_at_utc"], errors="coerce")
    plot_df[y_col] = pd.to_numeric(plot_df[y_col], errors="coerce")
    plot_df["group_name"] = plot_df["group_name"].fillna("default").astype(str)
    plot_df = plot_df.dropna(subset=["captured_at_utc", y_col]).sort_values("captured_at_utc")
    if plot_df.empty:
        return "<div class='muted'>No group chart data available.</div>"

    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed", "#f59e0b", "#0f766e"]
    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    for idx, (group_name, part) in enumerate(plot_df.groupby("group_name", sort=True)):
        ax.plot(
            part["captured_at_utc"],
            part[y_col],
            marker="o",
            linewidth=2.1,
            markersize=4,
            color=colors[idx % len(colors)],
            markeredgewidth=0,
            label=str(group_name),
        )
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left", pad=12, color="#1f2937")
    ax.set_ylabel(y_label, fontsize=10, color="#4b5563", labelpad=8)
    ax.grid(True, axis="y", color="#dbe5f1", linewidth=1.0)
    ax.grid(False, axis="x")
    ax.set_facecolor("#ffffff")
    fig.patch.set_facecolor("white")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#d1d5db")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.tick_params(axis="x", labelrotation=28, labelsize=8.5, colors="#4b5563")
    ax.tick_params(axis="y", labelsize=9, colors="#4b5563")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.legend(loc="upper right", frameon=False, fontsize=8.2, ncol=2)
    fig.subplots_adjust(left=0.12, right=0.98, bottom=0.28, top=0.86)
    img = _build_chart_img(fig)
    return f"<img class='chart-img' src='data:image/png;base64,{img}' alt='{html.escape(title)}' />"


def _build_scanner_env_section(env_data: Dict[str, Any], env_name: str = "") -> str:
    """Render scanner performance charts and stats for a single environment."""
    group_ts     = env_data.get("group_ts")     if isinstance(env_data.get("group_ts"),     pd.DataFrame) else pd.DataFrame()
    group_by_ts  = env_data.get("group_by_ts")  if isinstance(env_data.get("group_by_ts"),  pd.DataFrame) else pd.DataFrame()
    group_summary= env_data.get("group_summary") if isinstance(env_data.get("group_summary"),pd.DataFrame) else pd.DataFrame()
    cpu_ts       = env_data.get("cpu_ts")       if isinstance(env_data.get("cpu_ts"),       pd.DataFrame) else pd.DataFrame()
    cpu_by_group = env_data.get("cpu_by_group") if isinstance(env_data.get("cpu_by_group"), pd.DataFrame) else pd.DataFrame()

    start_txt = pd.to_datetime(env_data.get("start_dt"), errors="coerce")
    end_txt   = pd.to_datetime(env_data.get("end_dt"),   errors="coerce")
    start_label = start_txt.strftime("%Y-%m-%d %H:%M") if not pd.isna(start_txt) else "—"
    end_label   = end_txt.strftime("%Y-%m-%d %H:%M")   if not pd.isna(end_txt)   else "—"

    avg_speed    = _fmt_num(group_ts["avg_scan_speed_kib_s"].mean() if not group_ts.empty and "avg_scan_speed_kib_s" in group_ts.columns else None, 1)
    avg_used_mem = _fmt_num(group_ts["used_memory_mb"].mean()       if not group_ts.empty and "used_memory_mb"        in group_ts.columns else None, 0)
    max_scanners = _fmt_num(group_ts["running_scanners"].max()      if not group_ts.empty and "running_scanners"      in group_ts.columns else None, 0)
    avg_cpu_val  = cpu_ts["avg_cpu_pct"].mean() if not cpu_ts.empty and "avg_cpu_pct" in cpu_ts.columns else None
    avg_cpu_label = f"{_fmt_num(avg_cpu_val, 1)}%" if avg_cpu_val is not None else "—"

    stat_cards = f"""
    <table style="width: 100%; border-collapse: separate; border-spacing: 14px 0; margin-bottom: 18px;">
      <tr>
        <td style="padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #f8fafc; text-align: left;">
          <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;">Benchmark Scans</div>
          <div style="font-size: 24px; font-weight: 800; color: #111827;">{int(env_data.get('scan_count') or 0):,}</div>
        </td>
        <td style="padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #f8fafc; text-align: left;">
          <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;">Peak Scanners</div>
          <div style="font-size: 24px; font-weight: 800; color: #111827;">{max_scanners}</div>
        </td>
        <td style="padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #f8fafc; text-align: left;">
          <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;">Avg CPU</div>
          <div style="font-size: 24px; font-weight: 800; color: #111827;">{avg_cpu_label}</div>
        </td>
        <td style="padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #f8fafc; text-align: left;">
          <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;">Avg Memory</div>
          <div style="font-size: 24px; font-weight: 800; color: #111827;">{avg_used_mem} MB</div>
        </td>
      </tr>
    </table>
    """

    rows_html = []
    if not group_summary.empty:
        cpu_summary = pd.DataFrame()
        if not cpu_by_group.empty:
            cpu_summary = cpu_by_group.groupby("group_name", as_index=False).agg(avg_cpu_pct=("avg_cpu_pct", "mean"))
        show_summary = group_summary.merge(cpu_summary, on="group_name", how="left") if not cpu_summary.empty else group_summary.copy()
        for _, row in show_summary.iterrows():
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(str(row.get('group_name') or 'default'))}</td>"
                f"<td>{_fmt_num(row.get('avg_cpu_pct'), 1)}%</td>"
                f"<td>{_fmt_num(row.get('avg_memory_mb'), 0)}</td>"
                f"<td>{_fmt_num(row.get('peak_memory_mb'), 0)}</td>"
                f"<td>{_fmt_num(row.get('avg_speed_kib_s'), 1)}</td>"
                f"<td>{_fmt_num(row.get('peak_scanners'), 0)}</td>"
                f"<td>{_fmt_num(row.get('avg_threads'), 0)}</td>"
                "</tr>"
            )
    summary_table = ""
    if rows_html:
        summary_table = f"""
        <table class="report-table">
          <thead>
            <tr><th>Scanner Group</th><th>Avg CPU</th><th>Avg Mem MB</th><th>Peak Mem MB</th><th>Avg Speed KiB/s</th><th>Peak Scanners</th><th>Avg Threads</th></tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
        """

    display_name = env_name if env_name else f"Environment {env_data.get('env_id', '')}"
    return f"""
    <details class="env-perf-section" open>
      <summary class="env-perf-summary">
        <span class="env-perf-head">{html.escape(display_name)}</span>
        <span class="env-perf-range">{html.escape(start_label)} → {html.escape(end_label)}</span>
      </summary>
      <div class="env-perf-body">
        <div class="section-subtitle" style="margin-top:10px;">Avg scan speed: <strong>{avg_speed} KiB/s</strong></div>
        {stat_cards}
        {summary_table}
        <div class="chart-band">
          <table style="width: 100%; border-collapse: collapse;">
            <tr>
              <td style="width: 50%; padding-right: 7px; padding-bottom: 14px;">{_group_series_chart_html(cpu_by_group, "avg_cpu_pct", "Scanner CPU by Group", "CPU %")}</td>
              <td style="width: 50%; padding-left: 7px; padding-bottom: 14px;">{_group_series_chart_html(group_by_ts, "used_memory_mb", "Scanner Memory by Group", "Used MB")}</td>
            </tr>
            <tr>
              <td style="width: 50%; padding-right: 7px;">{_group_series_chart_html(group_by_ts, "avg_scan_speed_kib_s", "Average Scan Speed by Group", "KiB/s")}</td>
              <td style="width: 50%; padding-left: 7px;">{_time_series_chart_html(group_ts, "captured_at_utc", "running_scanners", "Running Scanners", "Count", "#2563eb")}</td>
            </tr>
          </table>
        </div>
      </div>
    </details>
    """


def load_env_names(engine, env_ids: List[int]) -> Dict[int, str]:
    """Return {env_id: env_name} for the given IDs."""
    if not env_ids:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, env_name FROM test_environments WHERE id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": tuple(env_ids)},
            ).fetchall()
        return {int(r[0]): str(r[1] or f"Env {r[0]}") for r in rows}
    except Exception:
        return {}


def build_scanner_performance_section(perf: Dict[str, Any], engine=None) -> str:
    if not perf or not perf.get("available"):
        reason = html.escape(str((perf or {}).get("reason") or "No scanner performance data available."))
        return f"""
        <div class="card card-sky">
          <div class="section-title">Scanner Performance During Build</div>
          <div class="muted">{reason}</div>
        </div>
        """

    per_env = perf.get("per_env") or []
    env_ids = [e["env_id"] for e in per_env]
    env_names = load_env_names(engine, env_ids) if engine else {}
    env_sections = "".join(
        _build_scanner_env_section(e, env_names.get(e["env_id"], ""))
        for e in per_env
    )
    total_scans = int(perf.get("total_scan_count") or 0)
    env_labels = ", ".join(env_names.get(eid, f"Env {eid}") for eid in env_ids)

    return f"""
    <div class="card card-sky">
      <div class="section-title">Scanner Performance During Build</div>
      <div class="section-subtitle">
        Scanner-group performance during {total_scans:,} benchmark scan(s) across
        environment(s): <strong>{html.escape(env_labels)}</strong>.
        Each environment is shown separately below.
      </div>
      {env_sections}
    </div>
    """


def build_overview_cards(current_major: str, previous_major: Optional[str], cur_df: pd.DataFrame, total_scans: int = 0) -> str:
    avg_files = pd.to_numeric(cur_df.get("throughput_cluster_files_hr"), errors="coerce").dropna() if cur_df is not None and "throughput_cluster_files_hr" in cur_df.columns else pd.Series(dtype=float)
    avg_gb = pd.to_numeric(cur_df.get("throughput_cluster_gb_hr"), errors="coerce").dropna() if cur_df is not None and "throughput_cluster_gb_hr" in cur_df.columns else pd.Series(dtype=float)
    ds_count = 0
    if cur_df is not None and not cur_df.empty and "data_source_type" in cur_df.columns:
        ds_count = int(cur_df["data_source_type"].map(normalize_ds_name).nunique())
    cards = [
        ("Current Line", f"{current_major}.x"),
        ("Comparison Line", f"{previous_major}.x" if previous_major else "—"),
        ("Data Sources", f"{ds_count:,}"),
        ("Total Scans", f"{total_scans:,}"),
        ("Avg Files/hr", _fmt_num(avg_files.mean() if not avg_files.empty else None, 0)),
        ("Avg GB/hr", _fmt_num(avg_gb.mean() if not avg_gb.empty else None, 2)),
    ]
    html_cards = []
    for label, value in cards:
        html_cards.append(
            f"<td style='padding: 14px 16px; border: 1px solid #e5e7eb; border-radius: 14px; background: #ffffff; min-width: 120px; text-align: left;'>"
            f"<div style='font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 6px;'>{html.escape(str(label))}</div>"
            f"<div style='font-size: 24px; font-weight: 800; color: #111827;'>{html.escape(str(value))}</div>"
            f"</td>"
        )
    return "<table style='width: 100%; border-collapse: separate; border-spacing: 14px 0; margin-top: 22px;'><tr>" + "".join(html_cards) + "</tr></table>"


def build_test_environment_section(include: bool, env_config: Optional[Dict[str, Any]] = None) -> str:
    """Render a Test Environment card as bullet-point statements.
    Values come from CLI args (env_config dict); all have sensible defaults.
    Only shown when --test-env flag is passed.
    """
    if not include:
        return ""

    cfg = env_config or {}
    environments        = str(cfg.get("environments")           or "Two large-scale environments")
    scanners            = str(cfg.get("scanners")               or "5")
    threads_per_scanner = str(cfg.get("threads_per_scanner")    or "10")
    scan_setting_threads= str(cfg.get("scan_setting_threads")   or "50")
    ds_lite_mode        = str(cfg.get("ds_lite_mode")           or "True")
    file_count          = str(cfg.get("file_count")             or "1 M")
    file_type           = str(cfg.get("file_type")              or "txt")
    extra_notes: List[str] = list(cfg.get("extra_notes") or [])

    bullets = [
        f"{environments}.",
        f"{scanners} Scanners, {threads_per_scanner} threads each.",
        f"{scan_setting_threads} Threads configured in Scan setting in DS.",
        f"DS_LITE_MODE_ENABLED {ds_lite_mode}.",
        f"{file_count} {file_type} files.",
    ]
    for note in extra_notes:
        bullets.append(str(note))

    items_html = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return f"""
    <div class="card card-env">
      <div class="section-title">🖥️ Test Environment</div>
      <div class="section-subtitle">Infrastructure and dataset configuration used for this benchmark run.</div>
      <ul class="overview-list">{items_html}</ul>
    </div>
    """


def build_html_report(
    current_build: str,
    previous_build: Optional[str],
    current_major: str,
    previous_major: Optional[str],
    environment_id: Optional[int],
    full_df: pd.DataFrame,
    cur_df: pd.DataFrame,
    prev_df: pd.DataFrame,
    current_build_df: pd.DataFrame,
    classifier_detail: Dict[str, Any],
    error_items: List[Dict[str, Any]],
    scanner_perf: Dict[str, Any],
    include_structured: bool,
    include_scanner_performance: bool,
    include_errors: bool,
    include_test_env: bool = False,
    test_env_config: Optional[Dict[str, Any]] = None,
    engine=None,
) -> str:
    build_comp_html = build_build_comparison_section(cur_df, prev_df, full_df, current_major, previous_major, include_structured)
    major_release_html = build_major_release_section(full_df, include_structured)
    # Use major-line best rows (cur_df) so data from all sub-versions (e.g. 253.5 + 253.11)
    # is considered — ensures OLS and Full Scan runs from any sub-version are included.
    cross_ds_html = build_cross_ds_section(cur_df, f"{current_major}.x (best)", include_structured)
    scenario_matrix_html = build_scenario_matrix_section(
        current_build_df,
        current_build,
        include_structured,
        cur_major_df=filter_major_line(full_df, current_major),    # raw, deduplicated inside
        prev_major_df=filter_major_line(full_df, previous_major),  # raw, deduplicated inside
        current_major=current_major,
        previous_major=previous_major,
    )
    scanner_perf_html = build_scanner_performance_section(scanner_perf, engine) if include_scanner_performance else ""
    classifier_html = build_classifier_section(classifier_detail)
    error_html = build_error_section(error_items) if include_errors else ""
    test_env_html = build_test_environment_section(include_test_env, test_env_config)
    # Total scan count across the major line for context
    total_scans_in_major = len(filter_major_line(full_df, current_major)) if full_df is not None and not full_df.empty else 0
    hero = build_overview_cards(current_major, previous_major, cur_df, total_scans_in_major)
    env_txt = f"Environment ID {environment_id}" if environment_id is not None else "All matching environments"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    summary_points = [
        f"Current report build is {current_build}; trend comparison is grouped by major line {current_major}.x versus {previous_major + '.x' if previous_major else 'the previous line'}.",
        "Unstructured DS charts show cluster, per-scanner, and per-thread throughput for S3 and SMB, including Full Scan vs OLS comparison.",
        f"Cross-DS charts compare the best available result across {current_major}.x (all sub-versions).",
        f"Scenario Matrix shows best result per scenario across {current_major}.x{f', compared against {previous_major}.x' if previous_major else ''}.",
    ]
    # Classifier highlights in Executive Summary
    new_names = classifier_detail.get("new_names") or []
    missing_names = classifier_detail.get("missing_names") or []
    cur_count = classifier_detail.get("current_count") or 0
    prev_count = classifier_detail.get("previous_count")
    if new_names:
        names_str = ", ".join(new_names[:5]) + (f" (+{len(new_names)-5} more)" if len(new_names) > 5 else "")
        summary_points.append(f"\u2728 {len(new_names)} new classifier(s) added in {current_major}.x: {names_str}.")
    elif prev_count is not None:
        summary_points.append(f"Total classifiers: {cur_count:,} (no change vs {previous_major}.x)." if previous_major else f"Total classifiers: {cur_count:,}.")
    if missing_names:
        names_str = ", ".join(missing_names[:5]) + (f" (+{len(missing_names)-5} more)" if len(missing_names) > 5 else "")
        summary_points.append(f"{len(missing_names)} classifier(s) removed vs previous: {names_str}.")
    if include_structured:
        summary_points.insert(2, "Structured DS sections are enabled and include MSSQL and Postgres.")
    if include_scanner_performance:
        summary_points.append(f"Scanner performance covers {total_scans_in_major:,} scan(s) across {current_major}.x, split per environment.")
    if include_errors:
        summary_points.append("Observed errors and AI RCA notes are included.")

    # Build summary HTML — classifier new/removed lines get colored treatment
    summary_html_parts = []
    for pt in summary_points:
        escaped = html.escape(pt)
        if pt.startswith("\u2728"):
            summary_html_parts.append(f"<li style='color:#16a34a;font-weight:600;'>{escaped}</li>")
        elif pt.startswith("1 classifier") or "removed" in pt:
            summary_html_parts.append(f"<li style='color:#dc2626;font-weight:600;'>{escaped}</li>")
        else:
            summary_html_parts.append(f"<li>{escaped}</li>")
    summary_html = "".join(summary_html_parts)
    comparison_line_text = f"{previous_major}.x" if previous_major else "—"
    no_data_banner = ""
    if cur_df is None or cur_df.empty:
        no_data_banner = f"""
        <div class="card warn-card">
          <div class="section-title">No Data For Current Major Line</div>
          <div class="section-subtitle">
            The current major line <strong>{html.escape(current_major)}.x</strong> returned zero benchmark rows after best-run filtering.
            Check available builds with <code>--list-builds</code> if this looks unexpected.
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Benchmark Report - {html.escape(current_build)}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background:
        radial-gradient(circle at top left, rgba(254, 240, 138, 0.28), transparent 28%),
        radial-gradient(circle at top right, rgba(125, 211, 252, 0.22), transparent 24%),
        linear-gradient(180deg, #fffaf0 0%, #f6f9ff 34%, #f8fafc 100%);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      color: #1f2937;
    }}
    .page {{
      max-width: 1120px;
      margin: 0 auto;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.96) 0%, rgba(255,255,255,0.99) 100%);
      border: 1px solid #e9e5d8;
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 20px 60px rgba(15, 23, 42, 0.08);
    }}
    .header {{
      padding: 32px 36px 24px 36px;
      background:
        linear-gradient(135deg, rgba(255,248,235,0.95) 0%, rgba(246,248,255,0.92) 52%, rgba(239,246,255,0.96) 100%);
      border-bottom: 1px solid #ece7db;
    }}
    .brand {{
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.03em;
      color: #4f46e5;
      margin-bottom: 12px;
    }}
    .title-row {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: flex-start;
    }}
    .title {{
      font-size: 34px;
      line-height: 1.1;
      font-weight: 800;
      color: #111827;
      margin: 0 0 8px 0;
    }}
    .meta {{
      font-size: 13px;
      color: #6b7280;
      line-height: 1.7;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }}
    .hero-stat {{
      background: #ffffff;
      border: 1px solid #e5e7eb;
      border-radius: 14px;
      padding: 14px 16px;
    }}
    .hero-stat .label, .mini-stat .label {{
      font-size: 12px;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      margin-bottom: 6px;
    }}
    .hero-stat .value, .mini-stat .value {{
      font-size: 24px;
      font-weight: 800;
      color: #111827;
    }}
    .hero-stat .value.pos, .mini-stat .value.pos, .pos {{
      color: #16a34a;
    }}
    .hero-stat .value.neg, .mini-stat .value.neg, .neg {{
      color: #dc2626;
    }}
    .muted {{
      color: #6b7280;
    }}
    .content {{
      padding: 28px 36px 36px 36px;
    }}
    .card {{
      border: 1px solid #e5e7eb;
      border-radius: 18px;
      padding: 22px 22px 18px 22px;
      margin-bottom: 22px;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(4px);
    }}
    .card-warm {{
      background: linear-gradient(180deg, rgba(255, 251, 235, 0.92) 0%, rgba(255,255,255,0.94) 100%);
      border-color: #f3dfb3;
    }}
    .card-sky {{
      background: linear-gradient(180deg, rgba(239, 246, 255, 0.92) 0%, rgba(255,255,255,0.95) 100%);
      border-color: #bfdbfe;
    }}
    .card-mint {{
      background: linear-gradient(180deg, rgba(236, 253, 245, 0.9) 0%, rgba(255,255,255,0.95) 100%);
      border-color: #a7f3d0;
    }}
    .warn-card {{
      background: #fff8e8;
      border-color: #fde68a;
    }}
    .section-title {{
      font-size: 22px;
      font-weight: 800;
      color: #1f2937;
      margin-bottom: 8px;
    }}
    .section-subtitle {{
      font-size: 14px;
      color: #6b7280;
      margin-bottom: 18px;
    }}
    .overview-list {{
      margin: 0;
      padding-left: 20px;
      color: #374151;
      line-height: 1.7;
    }}
    .report-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .report-table th {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid #dbe3ef;
      color: #374151;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .report-table td {{
      padding: 10px 8px;
      border-bottom: 1px solid #eef2f7;
      vertical-align: top;
    }}
    .matrix-card {{
      border: 1px solid #d8dee9;
      border-radius: 14px;
      margin-bottom: 16px;
      background: #ffffff;
      overflow: hidden;
    }}
    .matrix-summary {{
      list-style: none;
      cursor: pointer;
      padding: 14px 18px;
      font-size: 16px;
      font-weight: 700;
      color: #374151;
      background: #fafcff;
      border-bottom: 1px solid #e5e7eb;
    }}
    .matrix-summary::-webkit-details-marker {{
      display: none;
    }}
    .matrix-summary::before {{
      content: "▾";
      display: inline-block;
      margin-right: 10px;
      color: #4b5563;
      font-size: 14px;
      transform: translateY(-1px);
    }}
    details.matrix-card:not([open]) .matrix-summary::before {{
      content: "▸";
    }}
    .matrix-card .report-table {{
      margin: 16px;
      width: calc(100% - 32px);
    }}
    .mini-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .mini-stat {{
      background: #f8fafc;
      border-radius: 14px;
      padding: 14px 16px;
      border: 1px solid #e5e7eb;
    }}
    .split {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
    }}
    .subhead {{
      font-size: 16px;
      font-weight: 700;
      color: #374151;
      margin-bottom: 10px;
    }}
    .pill {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      margin: 0 8px 8px 0;
      border: 1px solid #dbe3ef;
      background: #f8fafc;
    }}
    .pill-pos {{
      color: #166534;
      background: #ecfdf5;
      border-color: #bbf7d0;
    }}
    .pill-neg {{
      color: #991b1b;
      background: #fef2f2;
      border-color: #fecaca;
    }}
    .more-note {{
      margin-top: 6px;
      font-size: 12px;
    }}
    .issue {{
      border: 1px solid #e5e7eb;
      border-radius: 14px;
      padding: 14px 16px;
      margin-bottom: 12px;
      background: #fafbff;
    }}
    .issue-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      font-size: 12px;
      font-weight: 700;
      color: #4b5563;
    }}
    .issue-source {{
      color: #4f46e5;
    }}
    .issue-body {{
      font-size: 13px;
      line-height: 1.6;
      color: #374151;
      margin-top: 6px;
    }}
    .chart-band {{
      background: linear-gradient(180deg, rgba(250, 252, 255, 0.96) 0%, rgba(245, 249, 255, 0.96) 100%);
      border: 1px solid #d7e6fb;
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .trend-panel {{
      background: #ffffff;
      border: 1px solid #d9e6fb;
      border-radius: 18px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }}
    .trend-panel:last-child {{
      margin-bottom: 0;
    }}
    .trend-panel-head {{
      font-size: 18px;
      font-weight: 800;
      color: #1f2937;
      margin-bottom: 12px;
    }}
    .trend-panel-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .chart-img {{
      width: 100%;
      display: block;
      border-radius: 12px;
      background: #ffffff;
      border: 1px solid #e5e7eb;
    }}
    .env-perf-section {{
      border: 1px solid #bfdbfe;
      border-radius: 14px;
      padding: 0;
      margin-bottom: 18px;
      background: rgba(255,255,255,0.7);
      overflow: hidden;
    }}
    .env-perf-section:last-child {{
      margin-bottom: 0;
    }}
    .env-perf-summary {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 18px;
      cursor: pointer;
      list-style: none;
      background: rgba(239,246,255,0.8);
      border-bottom: 1px solid #bfdbfe;
      border-radius: 14px;
      user-select: none;
    }}
    .env-perf-section[open] > .env-perf-summary {{
      border-radius: 14px 14px 0 0;
    }}
    .env-perf-summary::-webkit-details-marker {{ display: none; }}
    .env-perf-summary::before {{
      content: "▶";
      font-size: 11px;
      color: #2563eb;
      transition: transform 0.2s;
      flex-shrink: 0;
    }}
    .env-perf-section[open] > .env-perf-summary::before {{
      content: "▼";
    }}
    .env-perf-body {{
      padding: 18px 18px 14px 18px;
    }}
    .env-perf-head {{
      font-size: 17px;
      font-weight: 800;
      color: #1e40af;
    }}
    .env-perf-range {{
      font-size: 13px;
      font-weight: 400;
      color: #6b7280;
      margin-left: 4px;
    }}
    .card-env {{
      background: linear-gradient(180deg, rgba(255, 247, 237, 0.95) 0%, rgba(255,255,255,0.97) 100%);
      border-color: #fed7aa;
    }}
    .card-release {{
      background: linear-gradient(180deg, rgba(238, 242, 255, 0.95) 0%, rgba(255,255,255,0.97) 100%);
      border-color: #c7d2fe;
    }}
    .te-table {{
      max-width: 680px;
    }}
    .te-label {{
      font-weight: 600;
      color: #374151;
      width: 260px;
      white-space: nowrap;
    }}
    @media only screen and (max-width: 900px) {{
      .hero-grid, .mini-grid, .split, .chart-grid, .trend-panel-grid {{
        grid-template-columns: 1fr 1fr;
      }}
      .title-row {{
        display: block;
      }}
    }}
    @media only screen and (max-width: 640px) {{
      .trend-panel-grid, .chart-grid, .hero-grid, .mini-grid, .split {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="brand">BigID Benchmark Report</div>
      <div class="title-row">
        <div>
          <div class="title">Benchmark Executive Digest</div>
          <div class="meta">
            Generated: {html.escape(generated_at)}<br/>
            Scope: {html.escape(env_txt)}<br/>
            Current line: {html.escape(current_major)}.x<br/>
            Comparison line: {html.escape(comparison_line_text)}
          </div>
        </div>
      </div>
      {hero}
    </div>
    <div class="content">
      {no_data_banner}
      <div class="card card-warm">
        <div class="section-title">Executive Summary</div>
        <ul class="overview-list">{summary_html}</ul>
      </div>
      {test_env_html}
      {classifier_html}
      {major_release_html}
      {build_comp_html}
      {cross_ds_html}
      {scanner_perf_html}
      {scenario_matrix_html}
      {error_html}
    </div>
  </div>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate an email-friendly HTML benchmark report.")
    ap.add_argument("--list-builds", action="store_true", help="List available build versions and benchmark counts, then exit")
    ap.add_argument("--current-build", help="Optional current build version. If omitted, the latest available build is used.")
    ap.add_argument("--previous-build", help="Optional previous / baseline build version. If omitted, the previous available build is used.")
    ap.add_argument("--environment-id", type=int, help="Optional environment_id filter")
    ap.add_argument("--structured", action="store_true", help="Include structured DS sections for MSSQL and Postgres")
    ap.add_argument("--scanner-performance", action="store_true", help="Include scanner-group CPU, memory, and speed during current-build benchmark scans")
    ap.add_argument("--errors", action="store_true", help="Include Observed Issues / Errors / RCA section")
    ap.add_argument("--test-env", action="store_true", help="Include Test Environment section")
    ap.add_argument("--test-env-environments", default="Two large-scale environments",
                    metavar="DESC", help="Environment description (default: 'Two large-scale environments')")
    ap.add_argument("--test-env-scanners", default="5",
                    metavar="N", help="Number of scanners per environment (default: 5)")
    ap.add_argument("--test-env-threads-per-scanner", default="10",
                    metavar="N", help="Threads per scanner (default: 10)")
    ap.add_argument("--test-env-scan-setting-threads", default="50",
                    metavar="N", help="Threads configured in DS scan settings (default: 50)")
    ap.add_argument("--test-env-ds-lite-mode", default="True",
                    metavar="BOOL", help="DS_LITE_MODE_ENABLED value (default: True)")
    ap.add_argument("--test-env-file-count", default="1 M",
                    metavar="COUNT", help="Dataset file count (default: '1 M')")
    ap.add_argument("--test-env-file-type", default="txt",
                    metavar="EXT", help="Dataset file type (default: txt)")
    ap.add_argument("--out", help="Output HTML file path")
    args = ap.parse_args()

    engine = create_engine(
        DB_CONNECTION_STRING,
        connect_args={"connect_timeout": 10},
    )
    # Verify the connection is reachable before doing any work.
    try:
        with engine.connect() as _conn:
            _conn.execute(text("SELECT 1"))
    except Exception as exc:
        # Strip the full stack — just show a clear, actionable message.
        msg = str(exc).split("\n")[0]
        print(f"ERROR: Cannot connect to the benchmark database.\n  {msg}", file=sys.stderr)
        print("  Make sure you are on the correct network (VPN / on-prem) and the DB host is reachable.", file=sys.stderr)
        return 1

    available_builds = load_available_builds(engine, environment_id=args.environment_id)
    if args.list_builds:
        for build_version, cnt in available_builds:
            print(f"{build_version}\t{cnt}")
        return 0
    auto_current, auto_previous = pick_default_builds(available_builds)
    current_major, previous_major = _pick_latest_two_major_lines(available_builds)
    current_build = str(args.current_build or auto_current or "").strip()
    previous_build = str(args.previous_build or auto_previous or "").strip() or None
    if not current_build:
        ap.error("No build data found in the database. Use --list-builds to inspect available versions.")
    df = load_benchmarks(engine, environment_id=args.environment_id)
    if not current_major:
        ap.error("No major-version build lines were found in the database.")
    cur_df = best_rows_for_major_line(df, current_major)
    prev_df = best_rows_for_major_line(df, previous_major) if previous_major else pd.DataFrame(columns=df.columns)
    current_build_df = df[df["build_version"].astype(str) == current_build].copy()
    classifier_detail = load_classifier_detail(engine, current_build, previous_build, args.environment_id)
    error_items = load_error_rca(engine, [
        s for s in cur_df.get("scan_id", pd.Series(dtype=str)).tolist()
        if str(s or "").strip()
    ]) if args.errors else []
    scanner_perf = (
        load_scanner_performance_for_build(engine, current_build, args.environment_id)
        if args.scanner_performance
        else {}
    )
    test_env_config = {
        "environments":         args.test_env_environments,
        "scanners":             args.test_env_scanners,
        "threads_per_scanner":  args.test_env_threads_per_scanner,
        "scan_setting_threads": args.test_env_scan_setting_threads,
        "ds_lite_mode":         args.test_env_ds_lite_mode,
        "file_count":           args.test_env_file_count,
        "file_type":            args.test_env_file_type,
    } if args.test_env else None

    report_html = build_html_report(
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
