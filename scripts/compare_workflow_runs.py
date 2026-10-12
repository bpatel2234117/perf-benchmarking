#!/usr/bin/env python3
"""
Compare specific steps from two workflow runs and emit a detailed HTML report.

Example:
    python scripts/compare_workflow_runs.py \
        --run-a 150 --run-b 152 --steps 4-7 \
        --out reports/run_150_vs_152_steps_4-7.html
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import re
import sys

import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from sqlalchemy import create_engine, text
from v2_api.db_config_pg import DB_CONNECTION_STRING


def parse_steps(s: str) -> List[int]:
    """Parse '4,5,6,7' or '4-7' into a sorted list of unique ints."""
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def parse_run_ids(s: str) -> List[int]:
    """Parse '150,151,152' or '150-152' into a sorted list of unique run IDs."""
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def load_steps(engine, run_a_ids: List[int], run_b_ids: List[int], steps: List[int]) -> pd.DataFrame:
    step_sql = ",".join(str(int(s)) for s in steps)
    run_ids = sorted(set(run_a_ids) | set(run_b_ids))
    run_ids_sql = ",".join(str(int(r)) for r in run_ids)
    sql = f"""
    WITH latest_progress AS (
        SELECT DISTINCT ON (scan_id)
            scan_id,
            COALESCE(scan_state, '') AS latest_state,
            completed_sub_scans,
            failed_sub_scans,
            total_sub_scans,
            completed_parts,
            failed_parts,
            total_parts_scanned,
            total_classifications,
            newly_added_classifications,
            false_removed_classifications,
            objects_scanned AS progress_objects_scanned,
            objects_failed,
            objects_with_pi AS progress_objects_with_pi
        FROM scan_progress
        ORDER BY scan_id, COALESCE(captured_at_utc, "timestamp") DESC NULLS LAST, id DESC
    ),
    scan_metrics AS (
        SELECT DISTINCT ON (scan_id)
            scan_id,
            total_scan_hours,
            throughput_cluster_gb_hr,
            throughput_scanner_gb_hr,
            throughput_thread_gb_hr,
            throughput_cluster_files_hr,
            throughput_scanner_files_hr,
            throughput_thread_files_hr,
            total_size_gb,
            total_files,
            total_folders,
            objects_scanned,
            objects_with_pi,
            objects_enumerated,
            total_classification,
            new_attributes,
            removed_attributes,
            scan_start_time,
            scan_end_time
        FROM (
            SELECT
                scan_id, total_scan_hours, throughput_cluster_gb_hr,
                throughput_scanner_gb_hr, throughput_thread_gb_hr,
                throughput_cluster_files_hr, throughput_scanner_files_hr,
                throughput_thread_files_hr,
                NULL::real AS total_size_gb,
                NULL::bigint AS total_files,
                NULL::bigint AS total_folders,
                total_catalog_items AS objects_scanned,
                NULL::bigint AS objects_with_pi,
                NULL::bigint AS objects_enumerated,
                total_classification,
                NULL::int AS new_attributes,
                NULL::int AS removed_attributes,
                scan_start_time, scan_end_time
            FROM benchmarks
            WHERE scan_id IS NOT NULL
            UNION ALL
            SELECT
                scan_id, total_scan_hours, throughput_cluster_gb_hr,
                NULL::real AS throughput_scanner_gb_hr,
                NULL::real AS throughput_thread_gb_hr,
                throughput_cluster_files_hr,
                NULL::real AS throughput_scanner_files_hr,
                NULL::real AS throughput_thread_files_hr,
                total_size_gb, total_files, total_folders,
                objects_scanned, objects_with_pi, objects_enumerated,
                total_classification, new_attributes, removed_attributes,
                scan_start_time, scan_end_time
            FROM auto_scan_benchmarks
            WHERE scan_id IS NOT NULL
            UNION ALL
            SELECT
                scan_id, total_scan_hours, throughput_cluster_gb_hr,
                throughput_scanner_gb_hr, throughput_thread_gb_hr,
                throughput_cluster_files_hr, throughput_scanner_files_hr,
                throughput_thread_files_hr, total_size_gb, total_files,
                total_folders, objects_scanned, objects_with_pi,
                objects_enumerated, total_classification, new_attributes,
                removed_attributes, scan_start_time, scan_end_time
            FROM benchmark_runs
            WHERE scan_id IS NOT NULL
        ) all_metrics
        ORDER BY scan_id, COALESCE(total_scan_hours, 0) DESC
    )
    SELECT
        s.id AS step_id,
        s.run_id,
        s.seq,
        wr.name AS run_name,
        wr.status AS run_status,
        wr.started_at AS run_started_at,
        wr.finished_at AS run_finished_at,
        te.env_name,
        s.environment_id,
        s.ds_kind,
        s.ds_name,
        s.scan_kind,
        s.file_set_type,
        s.is_acl,
        s.is_ocr,
        s.is_structured_ff,
        s.is_ols,
        s.is_lite,
        s.feature_flags,
        s.notes,
        s.scan_profile_name,
        s.build_version,
        s.scan_id,
        s.status AS step_status,
        s.last_error,
        s.started_at AS step_started_at,
        s.finished_at AS step_finished_at,
        s.scan_waiting_since,
        s.failover_count,
        m.total_scan_hours,
        m.throughput_cluster_gb_hr,
        m.throughput_scanner_gb_hr,
        m.throughput_thread_gb_hr,
        m.throughput_cluster_files_hr,
        m.throughput_scanner_files_hr,
        m.throughput_thread_files_hr,
        m.total_size_gb,
        m.total_files,
        m.total_folders,
        m.objects_scanned,
        m.objects_with_pi,
        m.objects_enumerated,
        m.total_classification,
        m.new_attributes,
        m.removed_attributes,
        m.scan_start_time,
        m.scan_end_time,
        si.total_classification AS insight_total_classification,
        si.objects_scanned AS insight_objects_scanned,
        si.total_size_gb AS insight_total_size_gb,
        si.new_attributes_count,
        si.removed_attributes_count,
        si.distinct_classification_attributes,
        si.classifier_attribute_names,
        sp.scan_status AS parent_scan_status,
        sp.scan_name AS parent_scan_name,
        sp.started_at AS parent_started_at,
        sp.completed_at AS parent_completed_at,
        lp.latest_state,
        lp.completed_sub_scans,
        lp.failed_sub_scans,
        lp.total_sub_scans,
        lp.completed_parts,
        lp.failed_parts,
        lp.total_parts_scanned,
        lp.total_classifications AS progress_total_classifications,
        lp.newly_added_classifications,
        lp.false_removed_classifications,
        lp.progress_objects_scanned,
        lp.objects_failed,
        lp.progress_objects_with_pi
    FROM workflow_steps s
    JOIN workflow_runs wr ON wr.id = s.run_id
    LEFT JOIN test_environments te ON te.id = s.environment_id
    LEFT JOIN scan_metrics m ON m.scan_id = s.scan_id
    LEFT JOIN scan_insights si ON si.scan_id = s.scan_id
    LEFT JOIN scan_parents sp ON sp.scan_id = s.scan_id
    LEFT JOIN latest_progress lp ON lp.scan_id = s.scan_id
    WHERE s.run_id IN ({run_ids_sql})
      AND s.seq IN ({step_sql})
    ORDER BY s.run_id, s.seq
    """
    return pd.read_sql(text(sql), engine)


def load_scan_progress(engine, scan_ids: List[str]) -> pd.DataFrame:
    """Load full scan_progress history for a set of scan IDs."""
    if not scan_ids:
        return pd.DataFrame()
    ids_sql = ",".join(f"'{sid}'" for sid in scan_ids if sid and str(sid).strip())
    if not ids_sql:
        return pd.DataFrame()
    sql = f"""
    SELECT
        scan_id,
        COALESCE(captured_at_utc, "timestamp") AS ts,
        completed_parts,
        failed_parts,
        total_parts_scanned,
        objects_scanned,
        objects_failed,
        objects_with_pi,
        total_classifications,
        newly_added_classifications,
        false_removed_classifications
    FROM scan_progress
    WHERE scan_id IN ({ids_sql})
    ORDER BY scan_id, ts
    """
    df = pd.read_sql(text(sql), engine)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df




def _coerce(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


def _safe_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        x = int(float(v))
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
        return f"{x:,.{decimals}f}"
    return f"{x:,.{decimals}f}"


def _fmt_int(v: Any) -> str:
    x = _safe_int(v)
    if x is None:
        return ""
    if abs(x) >= 1000:
        return f"{x:,}"
    return str(x)


def _fmt_ts(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, str):
        v = v.strip()
        if v.lower() in ("nat", "none", ""):
            return ""
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_hours(start: Any, end: Any) -> Optional[float]:
    s = pd.to_datetime(start, errors="coerce")
    e = pd.to_datetime(end, errors="coerce")
    if pd.isna(s) or pd.isna(e):
        return None
    return (e - s).total_seconds() / 3600.0


def _delta_pct(a: Any, b: Any) -> Tuple[str, str]:
    af = _safe_float(a)
    bf = _safe_float(b)
    if af is None or bf is None:
        return "", ""
    delta = bf - af
    if af == 0:
        return _fmt_num(delta), ""
    pct = delta / af * 100.0
    return _fmt_num(delta), f"{pct:+.1f}%"


def _h(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def _td_raw(s: str, cls: str = "") -> str:
    if cls:
        return f"<td class='{cls}'>{s}</td>"
    return f"<td>{s}</td>"


TEXT_FIELDS = [
    ("run_name", "Run Name"),
    ("run_status", "Run Status"),
    ("env_name", "Environment"),
    ("environment_id", "Env ID"),
    ("ds_kind", "DS Kind"),
    ("ds_name", "DS Name"),
    ("scan_kind", "Scan Kind"),
    ("file_set_type", "File Set"),
    ("is_acl", "ACL"),
    ("is_structured_ff", "Structured FF"),
    ("is_ols", "OLS"),
    ("is_lite", "Lite"),
    ("is_ocr", "OCR"),
    ("feature_flags", "Feature Flags"),
    ("notes", "Notes"),
    ("scan_profile_name", "Scan Profile"),
    ("build_version", "Build Version"),
    ("scan_id", "Scan ID"),
    ("step_status", "Step Status"),
    ("last_error", "Last Error"),
    ("parent_scan_status", "Parent Scan Status"),
    ("parent_scan_name", "Parent Scan Name"),
    ("latest_state", "Latest Scan State"),
    ("classifier_attribute_names", "Classifier Attributes"),
]

INT_METRICS = {
    "total_files", "total_folders", "objects_scanned", "objects_with_pi",
    "objects_enumerated", "total_classification", "new_attributes", "removed_attributes",
    "insight_total_classification", "insight_objects_scanned", "new_attributes_count",
    "removed_attributes_count", "distinct_classification_attributes",
    "completed_sub_scans", "failed_sub_scans", "total_sub_scans",
    "completed_parts", "failed_parts", "total_parts_scanned",
    "progress_total_classifications", "newly_added_classifications",
    "false_removed_classifications", "progress_objects_scanned", "objects_failed",
    "progress_objects_with_pi",
}

NUMERIC_METRICS = [
    ("total_scan_hours", "Total Scan Hours", "h"),
    ("throughput_cluster_gb_hr", "Cluster Throughput", "GB/hr"),
    ("throughput_scanner_gb_hr", "Scanner Throughput", "GB/hr"),
    ("throughput_thread_gb_hr", "Thread Throughput", "GB/hr"),
    ("throughput_cluster_files_hr", "Cluster File Throughput", "files/hr"),
    ("throughput_scanner_files_hr", "Scanner File Throughput", "files/hr"),
    ("throughput_thread_files_hr", "Thread File Throughput", "files/hr"),
    ("total_size_gb", "Total Size", "GB"),
    ("total_files", "Total Files", ""),
    ("total_folders", "Total Folders", ""),
    ("objects_scanned", "Objects Scanned", ""),
    ("objects_with_pi", "Objects w/ PI", ""),
    ("objects_enumerated", "Objects Enumerated", ""),
    ("total_classification", "Total Classifications", ""),
    ("new_attributes", "New Attributes", ""),
    ("removed_attributes", "Removed Attributes", ""),
    ("insight_total_classification", "Insights Total Classifications", ""),
    ("insight_objects_scanned", "Insights Objects Scanned", ""),
    ("insight_total_size_gb", "Insights Total Size", "GB"),
    ("new_attributes_count", "New Attributes (insights)", ""),
    ("removed_attributes_count", "Removed Attributes (insights)", ""),
    ("distinct_classification_attributes", "Distinct Classifiers", ""),
    ("completed_sub_scans", "Completed Sub-scans", ""),
    ("failed_sub_scans", "Failed Sub-scans", ""),
    ("total_sub_scans", "Total Sub-scans", ""),
    ("completed_parts", "Completed Parts", ""),
    ("failed_parts", "Failed Parts", ""),
    ("total_parts_scanned", "Total Parts Scanned", ""),
    ("progress_total_classifications", "Progress Classifications", ""),
    ("newly_added_classifications", "Newly Added Classifications", ""),
    ("false_removed_classifications", "False Removed Classifications", ""),
    ("progress_objects_scanned", "Progress Objects Scanned", ""),
    ("objects_failed", "Progress Objects Failed", ""),
    ("progress_objects_with_pi", "Progress Objects w/ PI", ""),
]


def _get(row: Optional[Dict[str, Any]], key: str) -> Any:
    if row is None:
        return None
    return row.get(key)


def _row_dict(df: pd.DataFrame, run_id: int, seq: int) -> Optional[Dict[str, Any]]:
    rows = df[(df["run_id"] == int(run_id)) & (df["seq"] == int(seq))]
    if rows.empty:
        return None
    return {k: _coerce(v) for k, v in rows.iloc[0].to_dict().items()}


def _text_value(row: Optional[Dict[str, Any]], key: str) -> str:
    if row is None:
        return ""
    return _h(row.get(key))


def _numeric_value(row: Optional[Dict[str, Any]], key: str) -> str:
    if row is None:
        return ""
    v = row.get(key)
    if key in INT_METRICS:
        return _fmt_int(v)
    return _fmt_num(v)


def _html_table(headers: Sequence[str], rows: Sequence[str]) -> str:
    head = "".join(f"<th>{_h(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _step_html(df: pd.DataFrame, run_a: int, run_b: int, seq: int,
               run_a_name: str, run_b_name: str) -> str:
    a = _row_dict(df, run_a, seq)
    b = _row_dict(df, run_b, seq)
    if a is None and b is None:
        return f"<div class='card'><h3>Step {seq}</h3><p class='missing'>No data found for either run.</p></div>"
    rows: List[str] = []
    a_start = _fmt_ts(_get(a, "step_started_at"))
    b_start = _fmt_ts(_get(b, "step_started_at"))
    a_end = _fmt_ts(_get(a, "step_finished_at"))
    b_end = _fmt_ts(_get(b, "step_finished_at"))
    a_dur = _elapsed_hours(_get(a, "step_started_at"), _get(a, "step_finished_at"))
    b_dur = _elapsed_hours(_get(b, "step_started_at"), _get(b, "step_finished_at"))
    dur_delta, dur_pct = _delta_pct(a_dur, b_dur)
    dur_cls = "diff" if a_dur is not None and b_dur is not None and a_dur != b_dur else ""
    rows.append(
        f"<tr>{_td_raw('Step Started')}{_td_raw(a_start)}{_td_raw(b_start)}{_td_raw('')}{_td_raw('')}</tr>"
    )
    rows.append(
        f"<tr>{_td_raw('Step Finished')}{_td_raw(a_end)}{_td_raw(b_end)}{_td_raw('')}{_td_raw('')}</tr>"
    )
    rows.append(
        f"<tr>{_td_raw('Step Duration')}{_td_raw((_fmt_num(a_dur, 2) + ' h') if a_dur is not None else '', 'right')}"
        f"{_td_raw((_fmt_num(b_dur, 2) + ' h') if b_dur is not None else '', 'right')}"
        f"{_td_raw((dur_delta + ' h') if dur_delta else '', 'right')}{_td_raw(dur_pct, 'right')}</tr>"
    )
    for key, label in TEXT_FIELDS:
        av = _text_value(a, key)
        bv = _text_value(b, key)
        cls = "diff" if av != bv else ""
        rows.append(
            f"<tr>{_td_raw(_h(label))}{_td_raw(av, cls)}{_td_raw(bv, cls)}{_td_raw('')}{_td_raw('')}</tr>"
        )
    for key, label, unit in NUMERIC_METRICS:
        av = _numeric_value(a, key)
        bv = _numeric_value(b, key)
        delta, pct = _delta_pct(_get(a, key), _get(b, key))
        cls = "diff" if av != "" and bv != "" and av != bv else ""
        full_label = f"{label} ({unit})" if unit else label
        rows.append(
            f"<tr>{_td_raw(_h(full_label))}{_td_raw(av, 'right')}{_td_raw(bv, 'right')}"
            f"{_td_raw(delta, 'right')}{_td_raw(pct, 'right')}</tr>"
        )
    table = _html_table(
        ["Metric", f"Run {run_a}: {_h(run_a_name)}", f"Run {run_b}: {_h(run_b_name)}", "Delta", "Change"],
        rows,
    )
    notes = ""
    if a is None:
        notes += f"<p class='missing'>Run {run_a} has no step {seq}.</p>"
    if b is None:
        notes += f"<p class='missing'>Run {run_b} has no step {seq}.</p>"
    return f"<div class='card'><h3>Step {seq}</h3>{notes}{table}</div>"


def _run_summary(df: pd.DataFrame, run_id: int) -> Tuple[str, str, str, str]:
    rows = df[df["run_id"] == int(run_id)]
    if rows.empty:
        return f"Run {run_id}", "unknown", "", ""
    name = str(rows.iloc[0].get("run_name") or f"Run {run_id}")
    status = str(rows.iloc[0].get("run_status") or "")
    started = _fmt_ts(rows.iloc[0].get("run_started_at"))
    finished = _fmt_ts(rows.iloc[0].get("run_finished_at"))
    return name, status, started, finished


def build_html_report(df: pd.DataFrame, run_a_ids: List[int], run_b_ids: List[int], steps: List[int]) -> str:
    run_a = run_a_ids[0]
    run_b = run_b_ids[0]
    a_name, a_status, a_start, a_end = _run_summary(df, run_a)
    b_name, b_status, b_start, b_end = _run_summary(df, run_b)
    title = f"Workflow Step Comparison: Run {run_a} vs Run {run_b}"
    summary_rows = [
        f"<tr>{_td_raw('Run')}{_td_raw(_h(a_name))}{_td_raw(_h(b_name))}</tr>",
        f"<tr>{_td_raw('Run ID')}{_td_raw(run_a)}{_td_raw(run_b)}</tr>",
        f"<tr>{_td_raw('Status')}{_td_raw(_h(a_status))}{_td_raw(_h(b_status))}</tr>",
        f"<tr>{_td_raw('Started')}{_td_raw(a_start)}{_td_raw(b_start)}</tr>",
        f"<tr>{_td_raw('Finished')}{_td_raw(a_end)}{_td_raw(b_end)}</tr>",
    ]
    summary = _html_table(["", f"Run {run_a}", f"Run {run_b}"], summary_rows)
    sections = []
    for seq in steps:
        sections.append(_step_html(df, run_a, run_b, seq, a_name, b_name))
    style = """
        body { font-family: Arial, Helvetica, sans-serif; margin: 20px; background: #f5f7fb; color: #172033; }
        .page { max-width: 1400px; margin: 0 auto; }
        .hero { background: #13233f; color: white; padding: 26px 30px; border-radius: 8px; margin-bottom: 24px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        h1 { margin: 0 0 8px; font-size: 30px; }
        h2 { margin: 0 0 12px; font-size: 20px; color: #172033; }
        h3 { margin: 0 0 12px; font-size: 18px; color: #13233f; }
        table { border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 14px; }
        th, td { border: 1px solid #d0d7e2; padding: 8px 10px; text-align: left; vertical-align: top; }
        th { background: #eef1f7; }
        tr:nth-child(even) { background: #f9fafc; }
        .right { text-align: right; }
        .diff { background: #fff3cd; }
        .regression { background: #f8d7da; }
        .missing { color: #6c757d; font-style: italic; }
        .foot { margin-top: 16px; font-size: 12px; color: #6c757d; }
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_h(title)}</title>
  <style>{style}</style>
</head>
<body>
<div class="page">
  <section class="hero">
    <h1>{_h(title)}</h1>
    <p>Steps compared: {', '.join(str(s) for s in steps)}</p>
  </section>
  <section class="card">
    <h2>Run Summary</h2>
    {summary}
  </section>
  {''.join(sections)}
  <p class="foot">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
</body>
</html>
"""


def _scan_label(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    ds_name = str(row.get("ds_name") or "").strip()
    if ds_name:
        # Strip run-specific prefixes and suffixes so labels read as scan names.
        ds_name = re.sub(r"\s+\(wf\s+\d+-\d+\)", "", ds_name)
        ds_name = re.sub(r"\s+Full Scan$", "", ds_name, flags=re.IGNORECASE)
        ds_name = re.sub(r"^(?:\d+(?:\.\d+)*(?:-\d+)*|staging|prod|test)\s+", "", ds_name)
        return ds_name.strip()
    ds_kind = str(row.get("ds_kind") or "").strip()
    file_set = str(row.get("file_set_type") or "").strip()
    if ds_kind and file_set:
        return f"{ds_kind.capitalize()} {file_set}"
    if ds_kind:
        return ds_kind.capitalize()
    return ""


def _simple_scan_label(row: Optional[Dict[str, Any]]) -> str:
    """Return a short scan-type label (e.g. S3, SMB, Postgres) without size suffixes."""
    if not row:
        return ""
    ds_name = str(row.get("ds_name") or "").strip()
    if ds_name:
        ds_name = re.sub(r"\s+\(wf\s+\d+-\d+\)", "", ds_name)
        ds_name = re.sub(r"\s+Full Scan$", "", ds_name, flags=re.IGNORECASE)
        ds_name = re.sub(r"^(?:\d+(?:\.\d+)*(?:-\d+)*|staging|prod|test)\s+", "", ds_name)
        first = ds_name.split()[0]
        if first.isupper():
            return first
        return first.capitalize()
    ds_kind = str(row.get("ds_kind") or "").strip()
    if ds_kind:
        if ds_kind.upper() in ("S3", "SMB", "NFS", "API"):
            return ds_kind.upper()
        return ds_kind.capitalize()
    return ""




def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _progress_chart_img(progress_df: pd.DataFrame, scan_id_a: Optional[str], scan_id_b: Optional[str],
                        label_a: str, label_b: str, title: str) -> str:
    """Side-by-side progress-over-time figure for a single scan (Run A vs Run B)."""
    df_a = progress_df[progress_df["scan_id"] == scan_id_a].copy() if scan_id_a else pd.DataFrame()
    df_b = progress_df[progress_df["scan_id"] == scan_id_b].copy() if scan_id_b else pd.DataFrame()

    fig, axes = plt.subplots(2, 3, figsize=(14, 7.5), sharex="col", sharey="col")
    fig.suptitle(title, fontsize=14, fontweight="bold", y=1.02)

    panels = [
        (
            "Parts",
            [
                ("completed_parts", "completed", "#22c55e"),
                ("failed_parts", "failed", "#ef4444"),
                ("total_parts_scanned", "total", "#3b82f6"),
            ],
        ),
        (
            "Objects",
            [
                ("objects_scanned", "scanned", "#a855f7"),
                ("objects_failed", "failed", "#ef4444"),
            ],
        ),
        (
            "Classifications",
            [
                ("total_classifications", "total", "#3b82f6"),
                ("false_removed_classifications", "falseRemoved", "#ef4444"),
                ("newly_added_classifications", "newlyAdded", "#a855f7"),
                ("objects_with_pi", "objectsWithPi", "#22c55e"),
            ],
        ),
    ]

    for col_idx, (panel_name, series) in enumerate(panels):
        for row_idx, (sub_df, run_label) in enumerate([(df_a, label_a), (df_b, label_b)]):
            ax = axes[row_idx, col_idx]
            ax.set_title(f"{run_label} – {panel_name}" if col_idx == 0 else panel_name, fontsize=10)
            ax.grid(True, linestyle="--", alpha=0.4)
            ax.set_ylim(bottom=0)

            if not sub_df.empty and not sub_df["ts"].isna().all():
                sub_df = sub_df.sort_values("ts")
                for col, sname, color in series:
                    if col in sub_df.columns and sub_df[col].notna().any():
                        ax.plot(sub_df["ts"], sub_df[col], label=sname, color=color, linewidth=1.5)
                ax.legend(loc="lower right", fontsize=7, framealpha=0.9)

            if row_idx == 1:
                ax.set_xlabel("Time", fontsize=9)
            if col_idx == 0:
                ax.set_ylabel(run_label, fontsize=9)

    fig.autofmt_xdate()
    for ax in axes.flat:
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)

    data = _fig_to_base64(fig)
    return f"<img src='data:image/png;base64,{data}' alt='{_h(title)}' />"


def _parts_progress_chart_img(progress_df: pd.DataFrame,
                               scan_ids_a: Union[Optional[str], List[str]],
                               scan_ids_b: Union[Optional[str], List[str]],
                               label_a: str, label_b: str, title: str) -> str:
    """Side-by-side parts progress chart.

    Each side may contain one or many scan IDs. When multiple scan IDs are
    provided, the progress curves are averaged by minute-since-start so that
    several runs can be compared as a single build group.
    """

    def _norm_ids(scan_ids):
        if scan_ids is None:
            return []
        if isinstance(scan_ids, str):
            return [scan_ids]
        return list(scan_ids)

    ids_a = _norm_ids(scan_ids_a)
    ids_b = _norm_ids(scan_ids_b)

    series = [
        ("completed_parts", "completed", "#22c55e"),
        ("failed_parts", "failed", "#ef4444"),
        ("total_parts_scanned", "total", "#3b82f6"),
    ]

    def _avg_side(scan_ids):
        frames = []
        for sid in scan_ids:
            sub = progress_df[progress_df["scan_id"] == sid]
            if sub.empty or sub["ts"].isna().all():
                continue
            sub = sub.sort_values("ts").copy()
            t0 = sub["ts"].min()
            sub["minutes"] = (sub["ts"] - t0).dt.total_seconds() / 60.0
            frames.append(sub)
        if not frames:
            return pd.DataFrame()
        max_min = max(df["minutes"].max() for df in frames)
        if pd.isna(max_min) or max_min <= 0:
            return pd.DataFrame()
        grid = np.arange(0, int(np.ceil(max_min)) + 1, 1.0)
        out = {"minutes": grid}
        for col, _, _ in series:
            interp_list = []
            for df in frames:
                minutes = df["minutes"].to_numpy(dtype=float)
                values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
                valid = ~np.isnan(minutes) & ~np.isnan(values)
                minutes = minutes[valid]
                values = values[valid]
                if len(minutes) == 0:
                    continue
                if not np.all(np.diff(minutes) >= 0):
                    order = np.argsort(minutes)
                    minutes = minutes[order]
                    values = values[order]
                interp = np.interp(
                    grid, minutes, values,
                    left=float(values[0]), right=float(values[-1])
                )
                interp_list.append(interp)
            if interp_list:
                out[col] = np.mean(interp_list, axis=0)
            else:
                out[col] = np.full(len(grid), np.nan)
        return pd.DataFrame(out)

    avg_a = _avg_side(ids_a)
    avg_b = _avg_side(ids_b)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.02)

    y_max = 0.0
    for avg_df in (avg_a, avg_b):
        if not avg_df.empty:
            for col, _, _ in series:
                if col in avg_df.columns and avg_df[col].notna().any():
                    y_max = max(y_max, float(avg_df[col].max()))

    for ax, avg_df, run_label in [(axes[0], avg_a, label_a), (axes[1], avg_b, label_b)]:
        ax.set_title(run_label, fontsize=11)
        ax.grid(True, linestyle="--", alpha=0.4)

        if not avg_df.empty:
            for col, sname, color in series:
                if col in avg_df.columns and avg_df[col].notna().any():
                    ax.plot(avg_df["minutes"], avg_df[col], label=sname, color=color, linewidth=1.5)
            ax.legend(loc="lower right", fontsize=8, framealpha=0.9)

        ax.set_xlabel("Minutes since start", fontsize=9)
        ax.set_ylabel("Parts", fontsize=9)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0, top=max(1.0, y_max * 1.05))

    for ax in axes.flat:
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)

    data = _fig_to_base64(fig)
    return f"<img src='data:image/png;base64,{data}' alt='{_h(title)}' />"



def build_concise_html_report(df: pd.DataFrame, run_a_ids: List[int], run_b_ids: List[int], steps: List[int],
                              label_a: Optional[str] = None, label_b: Optional[str] = None,
                              progress_df: Optional[pd.DataFrame] = None) -> str:
    run_a = run_a_ids[0]
    run_b = run_b_ids[0]
    a_name, a_status, a_start, a_end = _run_summary(df, run_a)
    b_name, b_status, b_start, b_end = _run_summary(df, run_b)
    a_label = label_a or f"Run {run_a}"
    b_label = label_b or f"Run {run_b}"
    a_dur = _elapsed_hours(
        df[df["run_id"] == run_a]["run_started_at"].iloc[0] if not df[df["run_id"] == run_a].empty else None,
        df[df["run_id"] == run_a]["run_finished_at"].iloc[0] if not df[df["run_id"] == run_a].empty else None,
    )
    b_dur = _elapsed_hours(
        df[df["run_id"] == run_b]["run_started_at"].iloc[0] if not df[df["run_id"] == run_b].empty else None,
        df[df["run_id"] == run_b]["run_finished_at"].iloc[0] if not df[df["run_id"] == run_b].empty else None,
    )
    title = f"Workflow Run Comparison: {_h(a_label)} vs {_h(b_label)}"

    summary_rows = [
        f"<tr><td>Run Label</td><td>{_h(a_label)}</td><td>{_h(b_label)}</td></tr>",
        f"<tr><td>Run Name</td><td>{_h(a_name)}</td><td>{_h(b_name)}</td></tr>",
        f"<tr><td>Run ID</td><td>{run_a}</td><td>{run_b}</td></tr>",
        f"<tr><td>Status</td><td>{_h(a_status)}</td><td>{_h(b_status)}</td></tr>",
        f"<tr><td>Environment</td><td>{_h(df[df['run_id']==run_a]['env_name'].iloc[0] if not df[df['run_id']==run_a].empty else '')}</td>"
        f"<td>{_h(df[df['run_id']==run_b]['env_name'].iloc[0] if not df[df['run_id']==run_b].empty else '')}</td></tr>",
        f"<tr><td>Build</td><td>{_h(df[df['run_id']==run_a]['build_version'].iloc[0] if not df[df['run_id']==run_a].empty else '')}</td>"
        f"<td>{_h(df[df['run_id']==run_b]['build_version'].iloc[0] if not df[df['run_id']==run_b].empty else '')}</td></tr>",
        f"<tr><td>Started</td><td>{a_start}</td><td>{b_start}</td></tr>",
        f"<tr><td>Finished</td><td>{a_end}</td><td>{b_end}</td></tr>",
        f"<tr><td>Duration</td><td>{(_fmt_num(a_dur, 2)+' h') if a_dur is not None else ''}</td>"
        f"<td>{(_fmt_num(b_dur, 2)+' h') if b_dur is not None else ''}</td></tr>",
    ]
    summary = _html_table(["", _h(a_label), _h(b_label)], summary_rows)

    step_labels = []
    a_durations, b_durations = [], []
    a_total_hours, b_total_hours = [], []
    a_cluster_gb, b_cluster_gb = [], []
    a_scanner_gb, b_scanner_gb = [], []
    a_thread_gb, b_thread_gb = [], []
    a_cluster_files, b_cluster_files = [], []
    a_scanner_files, b_scanner_files = [], []
    a_thread_files, b_thread_files = [], []
    a_objects, b_objects = [], []
    a_completed, b_completed = [], []
    a_failed, b_failed = [], []
    a_total_parts, b_total_parts = [], []
    a_classifications, b_classifications = [], []

    detail_rows = []
    for seq in steps:
        a = _row_dict(df, run_a, seq)
        b = _row_dict(df, run_b, seq)
        label = _scan_label(a) or _scan_label(b) or f"Step {seq}"
        step_labels.append(label)

        a_dur = _elapsed_hours(_get(a, "step_started_at"), _get(a, "step_finished_at"))
        b_dur = _elapsed_hours(_get(b, "step_started_at"), _get(b, "step_finished_at"))
        a_durations.append(a_dur)
        b_durations.append(b_dur)

        a_total_hours.append(_get(a, "total_scan_hours"))
        b_total_hours.append(_get(b, "total_scan_hours"))
        a_cluster_gb.append(_get(a, "throughput_cluster_gb_hr"))
        b_cluster_gb.append(_get(b, "throughput_cluster_gb_hr"))
        a_scanner_gb.append(_get(a, "throughput_scanner_gb_hr"))
        b_scanner_gb.append(_get(b, "throughput_scanner_gb_hr"))
        a_thread_gb.append(_get(a, "throughput_thread_gb_hr"))
        b_thread_gb.append(_get(b, "throughput_thread_gb_hr"))
        a_cluster_files.append(_get(a, "throughput_cluster_files_hr"))
        b_cluster_files.append(_get(b, "throughput_cluster_files_hr"))
        a_scanner_files.append(_get(a, "throughput_scanner_files_hr"))
        b_scanner_files.append(_get(b, "throughput_scanner_files_hr"))
        a_thread_files.append(_get(a, "throughput_thread_files_hr"))
        b_thread_files.append(_get(b, "throughput_thread_files_hr"))
        a_objects.append(_get(a, "progress_objects_scanned"))
        b_objects.append(_get(b, "progress_objects_scanned"))
        a_completed.append(_get(a, "completed_parts"))
        b_completed.append(_get(b, "completed_parts"))
        a_failed.append(_get(a, "failed_parts"))
        b_failed.append(_get(b, "failed_parts"))
        a_total_parts.append(_get(a, "total_parts_scanned"))
        b_total_parts.append(_get(b, "total_parts_scanned"))
        a_classifications.append(_get(a, "insight_total_classification"))
        b_classifications.append(_get(b, "insight_total_classification"))

        a_status = _text_value(a, "step_status")
        b_status = _text_value(b, "step_status")
        a_parent = _text_value(a, "parent_scan_status")
        b_parent = _text_value(b, "parent_scan_status")
        detail_rows.append(
            f"<tr>"
            f"<td>{seq}</td>"
            f"<td>{label}</td>"
            f"<td>{(_fmt_num(a_dur, 2)+' h') if a_dur is not None else ''}</td>"
            f"<td>{(_fmt_num(b_dur, 2)+' h') if b_dur is not None else ''}</td>"
            f"<td>{a_status}</td><td>{b_status}</td>"
            f"<td>{a_parent}</td><td>{b_parent}</td>"
            f"<td>{_fmt_num(_get(a, 'throughput_cluster_gb_hr'), 2)}</td>"
            f"<td>{_fmt_num(_get(b, 'throughput_cluster_gb_hr'), 2)}</td>"
            f"<td>{_fmt_int(_get(a, 'progress_objects_scanned'))}</td>"
            f"<td>{_fmt_int(_get(b, 'progress_objects_scanned'))}</td>"
            f"<td>{_fmt_int(_get(a, 'completed_parts'))}</td>"
            f"<td>{_fmt_int(_get(b, 'completed_parts'))}</td>"
            f"<td>{_fmt_int(_get(a, 'failed_parts'))}</td>"
            f"<td>{_fmt_int(_get(b, 'failed_parts'))}</td>"
            f"</tr>"
        )

    detail = _html_table(
        [
            "Step", "Scan", f"Dur {_h(a_label)}", f"Dur {_h(b_label)}",
            f"Step {_h(a_label)}", f"Step {_h(b_label)}",
            f"Parent {_h(a_label)}", f"Parent {_h(b_label)}",
            f"Clust GB/hr {_h(a_label)}", f"Clust GB/hr {_h(b_label)}",
            f"Obj {_h(a_label)}", f"Obj {_h(b_label)}",
            f"Comp {_h(a_label)}", f"Comp {_h(b_label)}",
            f"Fail {_h(a_label)}", f"Fail {_h(b_label)}",
        ],
        detail_rows,
    )

    charts = [
        _chart_html("Scan Duration (hours)", step_labels, a_durations, b_durations, a_label, b_label, "h"),
        _chart_html("Total Scan Hours", step_labels, a_total_hours, b_total_hours, a_label, b_label, "h"),
        _chart_html("Cluster Throughput (GB/hr)", step_labels, a_cluster_gb, b_cluster_gb, a_label, b_label, "GB/hr"),
        _chart_html("Scanner Throughput (GB/hr)", step_labels, a_scanner_gb, b_scanner_gb, a_label, b_label, "GB/hr"),
        _chart_html("Thread Throughput (GB/hr)", step_labels, a_thread_gb, b_thread_gb, a_label, b_label, "GB/hr"),
        _chart_html("Cluster Throughput (files/hr)", step_labels, a_cluster_files, b_cluster_files, a_label, b_label, "files/hr"),
        _chart_html("Scanner Throughput (files/hr)", step_labels, a_scanner_files, b_scanner_files, a_label, b_label, "files/hr"),
        _chart_html("Thread Throughput (files/hr)", step_labels, a_thread_files, b_thread_files, a_label, b_label, "files/hr"),
        _chart_html("Objects Scanned", step_labels, a_objects, b_objects, a_label, b_label, ""),
        _chart_html("Completed Parts", step_labels, a_completed, b_completed, a_label, b_label, ""),
        _chart_html("Failed Parts", step_labels, a_failed, b_failed, a_label, b_label, ""),
        _chart_html("Total Parts Scanned", step_labels, a_total_parts, b_total_parts, a_label, b_label, ""),
        _chart_html("Total Classifications (Insights)", step_labels, a_classifications, b_classifications, a_label, b_label, ""),
    ]

    progress_sections = []
    if progress_df is not None and not progress_df.empty:
        for seq in steps:
            a = _row_dict(df, run_a, seq)
            b = _row_dict(df, run_b, seq)
            scan_a = _get(a, "scan_id")
            scan_b = _get(b, "scan_id")
            if not scan_a and not scan_b:
                continue
            label = _scan_label(a) or _scan_label(b) or f"Step {seq}"
            img = _progress_chart_img(progress_df, scan_a, scan_b, a_label, b_label, label)
            progress_sections.append(
                f"<div class='card progress-card'><h3>Progress Over Time – {_h(label)}</h3>{img}</div>"
            )

    style = """
        body { font-family: Arial, Helvetica, sans-serif; margin: 20px; background: #f5f7fb; color: #172033; }
        .page { max-width: 1400px; margin: 0 auto; }
        .hero { background: #13233f; color: white; padding: 26px 30px; border-radius: 8px; margin-bottom: 24px; }
        .card, .chart-card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
        @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
        .progress-card { padding: 16px; }
        .progress-card img { max-width: 100%; height: auto; display: block; }
        h1 { margin: 0 0 8px; font-size: 30px; }
        h2 { margin: 0 0 12px; font-size: 20px; color: #172033; }
        h3 { margin: 0 0 12px; font-size: 16px; color: #13233f; }
        table { border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 13px; }
        th, td { border: 1px solid #d0d7e2; padding: 6px 8px; text-align: left; vertical-align: top; }
        th { background: #eef1f7; }
        tr:nth-child(even) { background: #f9fafc; }
        .right { text-align: right; }
        .bar-row { margin-bottom: 16px; }
        .bar-label { font-weight: bold; margin-bottom: 4px; font-size: 13px; }
        .bar-tracks { display: flex; align-items: center; gap: 4px; height: 24px; background: #eef1f7; border-radius: 4px; overflow: hidden; }
        .bar-a-fill { height: 100%; background: #4a90d9; }
        .bar-b-fill { height: 100%; background: #e08e79; }
        .bar-values { font-size: 12px; color: #555; margin-top: 3px; }
        .bar-values .a { color: #4a90d9; font-weight: bold; }
        .bar-values .b { color: #e08e79; font-weight: bold; }
        .foot { margin-top: 16px; font-size: 12px; color: #6c757d; }
        .missing { color: #6c757d; font-style: italic; }
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_h(title)}</title>
  <style>{style}</style>
</head>
<body>
<div class="page">
  <section class="hero">
    <h1>{_h(title)}</h1>
    <p>Steps compared: {', '.join(str(s) for s in steps)}</p>
  </section>
  <section class="card">
    <h2>Run Summary</h2>
    {summary}
  </section>
  <section class="card">
    <h2>Charts</h2>
    <div class="grid">
      {''.join(charts)}
    </div>
  </section>
  {''.join(progress_sections)}
  <p class="foot">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
</body>
</html>
"""


def build_simple_html_report(df: pd.DataFrame, run_a_ids: List[int], run_b_ids: List[int], steps: List[int],
                               label_a: Optional[str] = None, label_b: Optional[str] = None,
                               progress_df: Optional[pd.DataFrame] = None) -> str:
    """Minimal report showing average scan duration across one or many runs per side."""
    a_count = len(run_a_ids)
    b_count = len(run_b_ids)
    a_label = label_a or f"Run {run_a_ids[0]}"
    b_label = label_b or f"Run {run_b_ids[0]}"
    a_count_str = f" ({a_count} runs)" if a_count > 1 else ""
    b_count_str = f" ({b_count} runs)" if b_count > 1 else ""
    title = f"Scan Duration: {a_label}{a_count_str} vs {b_label}{b_count_str}"

    table_rows = []
    progress_sections = []
    total_a: Optional[float] = 0.0
    total_b: Optional[float] = 0.0

    def _avg_duration(rows: pd.DataFrame) -> Optional[float]:
        durs = [
            _elapsed_hours(row.step_started_at, row.step_finished_at)
            for row in rows.itertuples(index=False)
            if hasattr(row, "step_started_at") and hasattr(row, "step_finished_at")
        ]
        durs = [d for d in durs if d is not None]
        return sum(durs) / len(durs) if durs else None

    def _status_str(rows: pd.DataFrame) -> str:
        statuses = rows["parent_scan_status"].dropna().astype(str).unique().tolist()
        if not statuses:
            statuses = rows["step_status"].dropna().astype(str).unique().tolist()
        return ", ".join(sorted(set(statuses))) if statuses else ""

    scan_names: List[str] = []
    for seq in steps:
        a_rows = df[(df["run_id"].isin(run_a_ids)) & (df["seq"] == seq)]
        b_rows = df[(df["run_id"].isin(run_b_ids)) & (df["seq"] == seq)]
        simple_name = ""
        if not a_rows.empty:
            simple_name = _simple_scan_label(a_rows.iloc[0].to_dict())
        if not simple_name and not b_rows.empty:
            simple_name = _simple_scan_label(b_rows.iloc[0].to_dict())
        if not simple_name:
            simple_name = f"Step {seq}"
        scan_names.append(simple_name)

        a_dur = _avg_duration(a_rows)
        b_dur = _avg_duration(b_rows)

        a_str = f"{a_dur:.2f} h" if a_dur is not None else ""
        b_str = f"{b_dur:.2f} h" if b_dur is not None else ""

        if a_dur is not None and b_dur is not None:
            diff = b_dur - a_dur
            pct = (diff / a_dur * 100.0) if a_dur else 0.0
            diff_str = f"{diff:+.2f} h ({pct:+.1f}%)"
        else:
            diff_str = ""

        a_status = _status_str(a_rows)
        b_status = _status_str(b_rows)

        table_rows.append(
            f"<tr>"
            f"<td>{_h(simple_name)}</td>"
            f"<td class='right'>{_h(a_str)}</td>"
            f"<td class='right'>{_h(a_status)}</td>"
            f"<td class='right'>{_h(b_str)}</td>"
            f"<td class='right'>{_h(b_status)}</td>"
            f"<td class='right'>{_h(diff_str)}</td>"
            f"</tr>"
        )

        if progress_df is not None and not progress_df.empty:
            scan_a_ids = [str(s) for s in a_rows["scan_id"].dropna().unique()]
            scan_b_ids = [str(s) for s in b_rows["scan_id"].dropna().unique()]
            if scan_a_ids or scan_b_ids:
                chart_title = f"Progress Over Time \u2013 {simple_name}"
                img = _parts_progress_chart_img(progress_df, scan_a_ids, scan_b_ids, a_label, b_label, chart_title)
                progress_sections.append(
                    f"<div class='card progress-card'><h3>{_h(chart_title)}</h3>{img}</div>"
                )

        if a_dur is not None:
            total_a = (total_a or 0.0) + a_dur
        else:
            total_a = None
        if b_dur is not None:
            total_b = (total_b or 0.0) + b_dur
        else:
            total_b = None

    a_total_str = f"{total_a:.2f} h" if total_a is not None else ""
    b_total_str = f"{total_b:.2f} h" if total_b is not None else ""
    if total_a is not None and total_b is not None:
        diff_total = total_b - total_a
        pct_total = (diff_total / total_a * 100.0) if total_a else 0.0
        total_diff_str = f"{diff_total:+.2f} h ({pct_total:+.1f}%)"
    else:
        total_diff_str = ""

    table_rows.append(
        f"<tr class='total'>"
        f"<td><strong>Total</strong></td>"
        f"<td class='right'><strong>{_h(a_total_str)}</strong></td>"
        f"<td class='right'></td>"
        f"<td class='right'><strong>{_h(b_total_str)}</strong></td>"
        f"<td class='right'></td>"
        f"<td class='right'><strong>{_h(total_diff_str)}</strong></td>"
        f"</tr>"
    )

    unique_scan_names = list(dict.fromkeys(scan_names))
    subtitle = f"Scans compared: {', '.join(unique_scan_names)}" if unique_scan_names else f"Steps compared: {', '.join(str(s) for s in steps)}"
    if a_count > 1 or b_count > 1:
        subtitle += f"<br>Durations and progress charts are averages across {a_count} run{'s' if a_count != 1 else ''} for {a_label} and {b_count} run{'s' if b_count != 1 else ''} for {b_label}."

    style = """
        body { font-family: Arial, Helvetica, sans-serif; margin: 24px; background: #f8f9fa; color: #172033; }
        .page { max-width: 1100px; margin: 0 auto; }
        .hero { background: #13233f; color: white; padding: 24px 28px; border-radius: 8px; margin-bottom: 24px; }
        .card { background: white; border-radius: 8px; padding: 24px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin-bottom: 20px; }
        .progress-card { padding: 16px; }
        .progress-card img { max-width: 100%; height: auto; display: block; }
        h1 { margin: 0; font-size: 24px; }
        h2 { margin: 0 0 16px; font-size: 18px; color: #172033; }
        h3 { margin: 0 0 12px; font-size: 16px; color: #13233f; }
        table { border-collapse: collapse; width: 100%; font-size: 15px; }
        th, td { border: 1px solid #d0d7e2; padding: 10px 12px; text-align: left; }
        th { background: #eef1f7; font-weight: 600; }
        tr:nth-child(even) { background: #f9fafc; }
        tr.total { background: #e8f0fe; }
        .right { text-align: right; }
        .foot { margin-top: 16px; font-size: 12px; color: #6c757d; }
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_h(title)}</title>
  <style>{style}</style>
</head>
<body>
<div class="page">
  <section class="hero">
    <h1>{_h(title)}</h1>
    <p>{subtitle}</p>
  </section>
  <section class="card">
    <h2>Scan Duration Comparison</h2>
    <table>
      <thead>
        <tr>
          <th>Scan</th>
          <th class="right">{_h(a_label)}{a_count_str}</th>
          <th class="right">Status {_h(a_label)}</th>
          <th class="right">{_h(b_label)}{b_count_str}</th>
          <th class="right">Status {_h(b_label)}</th>
          <th class="right">Difference</th>
        </tr>
      </thead>
      <tbody>
        {''.join(table_rows)}
      </tbody>
    </table>
  </section>
  {''.join(progress_sections)}
  <p class="foot">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
</body>
</html>
"""


def _chart_html(title: str, labels: List[str], values_a: List[Any], values_b: List[Any],
                label_a: str, label_b: str, unit: str) -> str:
    vals = [v for v in values_a + values_b if v is not None and not (isinstance(v, float) and pd.isna(v))]
    max_val = max(vals) if vals else 1.0
    if max_val == 0:
        max_val = 1.0
    rows = []
    for i, label in enumerate(labels):
        va = values_a[i]
        vb = values_b[i]
        wa = (va / max_val * 100.0) if va is not None and not (isinstance(va, float) and pd.isna(va)) else 0.0
        wb = (vb / max_val * 100.0) if vb is not None and not (isinstance(vb, float) and pd.isna(vb)) else 0.0
        ta = _fmt_num(va, 2) if va is not None and not (isinstance(va, float) and pd.isna(va)) else "N/A"
        tb = _fmt_num(vb, 2) if vb is not None and not (isinstance(vb, float) and pd.isna(vb)) else "N/A"
        rows.append(
            f"<div class='bar-row'>"
            f"<div class='bar-label'>{_h(label)}</div>"
            f"<div class='bar-tracks'>"
            f"<div class='bar-a-fill' style='width:{wa:.1f}%'></div>"
            f"<div class='bar-b-fill' style='width:{wb:.1f}%'></div>"
            f"</div>"
            f"<div class='bar-values'>"
            f"<span class='a'>{_h(label_a)}: {ta} {unit}</span> | "
            f"<span class='b'>{_h(label_b)}: {tb} {unit}</span>"
            f"</div>"
            f"</div>"
        )
    return f"<div class='chart-card'><h3>{_h(title)}</h3>{''.join(rows)}</div>"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare workflow run steps and generate an HTML report."
    )
    parser.add_argument("--run-a", required=True, type=str,
                        help="First workflow run ID(s), e.g. 150 or 150,151,152 or 150-152")
    parser.add_argument("--run-b", required=True, type=str,
                        help="Second workflow run ID(s), e.g. 153 or 153,154,155 or 153-155")
    parser.add_argument("--steps", required=True, type=str, help="Steps to compare, e.g. 4-7 or 4,5,6,7")
    parser.add_argument("--out", type=str, default=None, help="Output HTML file path")
    parser.add_argument(
        "--mode", choices=["simple", "concise", "detailed"], default="simple",
        help="Report style: simple = only scan duration (default); concise = duration + throughput + progress charts; detailed = full data"
    )
    parser.add_argument("--label-a", type=str, default=None, help="Custom label for run-a (e.g. MongoDB)")
    parser.add_argument("--label-b", type=str, default=None, help="Custom label for run-b (e.g. Percona MongoDB)")
    args = parser.parse_args()
    run_a_ids = parse_run_ids(args.run_a)
    run_b_ids = parse_run_ids(args.run_b)
    steps = parse_steps(args.steps)
    if not run_a_ids:
        raise SystemExit("No run IDs provided for --run-a.")
    if not run_b_ids:
        raise SystemExit("No run IDs provided for --run-b.")
    if not steps:
        raise SystemExit("No steps provided.")
    if args.out:
        out = Path(args.out)
    else:
        a_part = "_".join(str(r) for r in run_a_ids)
        b_part = "_".join(str(r) for r in run_b_ids)
        out = Path(
            f"reports/workflow_compare_runs{a_part}_vs_runs{b_part}_steps_{'_'.join(str(s) for s in steps)}.html"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DB_CONNECTION_STRING)
    df = load_steps(engine, run_a_ids, run_b_ids, steps)
    scan_ids = [str(s) for s in df["scan_id"].dropna().unique()]
    progress_df = load_scan_progress(engine, scan_ids) if scan_ids else pd.DataFrame()
    if args.mode == "simple":
        html_report = build_simple_html_report(
            df, run_a_ids, run_b_ids, steps,
            label_a=args.label_a, label_b=args.label_b, progress_df=progress_df
        )
    elif args.mode == "concise":
        html_report = build_concise_html_report(
            df, run_a_ids, run_b_ids, steps,
            label_a=args.label_a, label_b=args.label_b, progress_df=progress_df
        )
    else:
        html_report = build_html_report(df, run_a_ids, run_b_ids, steps)
    out.write_text(html_report, encoding="utf-8")
    print(f"Wrote {args.mode} report: {out}")


if __name__ == "__main__":
    main()
