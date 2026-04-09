#!/usr/bin/env python3
"""
Generate a Markdown report for the **250.x line**: take the **best** benchmark row per scenario
across multiple candidate builds (e.g. 250.1 and 250.9 are two runs — keep whichever wins per scenario).

Optionally compare that merged result to a **baseline** build (default **249.14**) the same way
the dashboard **Trend Analysis** charts compare builds over time.

Usage (repo root):
  python scripts/report_250_build_best.py \\
    --candidates 250.1 250.9 \\
    --baseline 249.14 \\
    --file-set "Small Files" \\
    --out report_250_small_files_best.md

Uses the same `benchmarks` + `builds` + `configurations` + `scan_insights` shape as dashboard.load_data().
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from db_config import DB_CONNECTION_STRING  # noqa: E402

TREND_PAGE_SMALL = "http://10.2.20.89:8501/?page=Small+Files+Trend+Analysis"
TREND_PAGE_LARGE = "http://10.2.20.89:8501/?page=Large+Files+Trend+Analysis"


def _db_has_column(engine, table: str, col: str) -> bool:
    try:
        with engine.connect() as conn:
            r = conn.execute(text(f"SHOW COLUMNS FROM {table} LIKE :c"), {"c": col}).fetchone()
            return r is not None
    except Exception:
        return False


def load_benchmarks_like_dashboard(engine, *, include_inactive: bool = False) -> pd.DataFrame:
    """Mirror dashboard.load_data() SELECT (without Streamlit)."""
    where_clause = "" if include_inactive else "WHERE COALESCE(b.is_active, 1) = 1"
    has_total_classification = _db_has_column(engine, "benchmarks", "total_classification")
    has_removed_attributes = _db_has_column(engine, "benchmarks", "removed_attributes")

    total_class_expr = (
        "CASE WHEN si.total_classification IS NOT NULL THEN si.total_classification ELSE b.total_classification END as Total_Classification"
        if has_total_classification
        else "si.total_classification as Total_Classification"
    )
    removed_attr_expr = (
        "CASE WHEN si.removed_attributes_count IS NOT NULL THEN si.removed_attributes_count ELSE b.removed_attributes END as Removed_Attributes"
        if has_removed_attributes
        else "si.removed_attributes_count as Removed_Attributes"
    )
    new_attr_expr = (
        "CASE WHEN si.new_attributes_count IS NOT NULL THEN si.new_attributes_count ELSE b.total_catalog_items END as New_Attributes"
    )

    query = f"""
        SELECT
            b.id,
            bu.version as Build_Version,
            COALESCE(NULLIF(b.classifier_list, ''), c.name) as Classifier_Name,
            conf.id as config_id,
            conf.scanner_count, conf.thread_count, conf.is_acl, conf.is_ocr, conf.is_feature_flags, conf.feature_flags, conf.comment,
            b.run_date as Date,
            b.scenario_type as Scenario_Type,
            b.scan_type as Scan_Type,
            b.data_source_type as Data_Source_Type,
            b.data_type as Data_Type,
            b.file_set_type as file_set_type,
            b.total_files as Total_Files,
            b.total_folders as Total_Folders,
            b.total_size_gb as Total_Size_GB,
            {new_attr_expr},
            {total_class_expr},
            {removed_attr_expr},
            b.throughput_cluster_gb_hr,
            b.throughput_scanner_gb_hr,
            b.throughput_thread_gb_hr,
            b.throughput_cluster_files_hr,
            b.throughput_scanner_files_hr,
            b.throughput_thread_files_hr,
            b.scan_start_time,
            b.scan_end_time,
            b.total_scan_hours,
            b.avg_file_size,
            b.is_active,
            b.scan_id,
            COALESCE(sp.scan_status, 'Completed') as Scan_Status,
            COALESCE(si.objects_scanned, 0) as objects_scanned
        FROM benchmarks b
        JOIN builds bu ON b.build_id = bu.id
        JOIN configurations conf ON b.config_id = conf.id
        LEFT JOIN classifiers c ON b.classifier_id = c.id
        LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
        LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
        {where_clause}
    """
    df = pd.read_sql(text(query), engine)
    for _c in ["New_Attributes", "Total_Classification", "Removed_Attributes"]:
        if _c in df.columns:
            try:
                df[_c] = pd.to_numeric(df[_c], errors="coerce").astype("Int64")
            except Exception:
                pass
    if not df.empty and "scan_start_time" in df.columns and "scan_end_time" in df.columns:
        start = pd.to_datetime(df["scan_start_time"], errors="coerce")
        end = pd.to_datetime(df["scan_end_time"], errors="coerce")
        calc_hours = (end - start).dt.total_seconds() / 3600
        df["total_scan_hours"] = calc_hours.combine_first(df.get("total_scan_hours"))
    return df


def select_best_run(group: pd.DataFrame) -> pd.Series:
    """Same priority as dashboard trend: min total_scan_hours, else max throughput_cluster_gb_hr."""
    if group is None or group.empty:
        raise ValueError("select_best_run: empty group")
    if "total_scan_hours" in group.columns and group["total_scan_hours"].notna().any():
        valid_time = group[group["total_scan_hours"].notna()]
        if not valid_time.empty:
            return valid_time.loc[valid_time["total_scan_hours"].idxmin()]
    if "throughput_cluster_gb_hr" in group.columns and group["throughput_cluster_gb_hr"].notna().any():
        valid_tp = group[group["throughput_cluster_gb_hr"].notna()]
        if not valid_tp.empty:
            return valid_tp.loc[valid_tp["throughput_cluster_gb_hr"].idxmax()]
    return group.iloc[0]


def scenario_key_row(r: pd.Series) -> Tuple:
    return (
        str(r.get("Data_Source_Type") or "").strip(),
        str(r.get("Scan_Type") or "").strip(),
        int(r.get("is_acl") or 0),
        int(r.get("is_feature_flags") or 0),
        str(r.get("file_set_type") or "").strip(),
    )


def best_per_scenario_for_builds(
    df: pd.DataFrame, build_versions: List[str], file_set_keyword: str
) -> pd.DataFrame:
    """One best row per scenario per build version, then returned as separate frames."""
    sub = df[
        df["Build_Version"].astype(str).isin([str(x).strip() for x in build_versions])
        & df["file_set_type"].astype(str).str.contains(file_set_keyword, case=False, na=False)
    ].copy()
    out_rows = []
    for bv in build_versions:
        part = sub[sub["Build_Version"].astype(str) == str(bv).strip()].copy()
        if part.empty:
            continue
        for _, grp in part.groupby(
            ["Data_Source_Type", "Scan_Type", "is_acl", "is_feature_flags", "file_set_type"],
            dropna=False,
        ):
            out_rows.append(select_best_run(grp))
    return pd.DataFrame(out_rows) if out_rows else pd.DataFrame()


def score_row(s: pd.Series) -> Tuple[float, float, float]:
    """Higher is better for first two; lower hours is better (negated)."""
    tf = pd.to_numeric(s.get("throughput_cluster_files_hr"), errors="coerce")
    tg = pd.to_numeric(s.get("throughput_cluster_gb_hr"), errors="coerce")
    th = pd.to_numeric(s.get("total_scan_hours"), errors="coerce")
    return (
        float(tf) if tf == tf else float("-inf"),
        float(tg) if tg == tg else float("-inf"),
        -float(th) if th == th else 0.0,
    )


def merge_best_across_candidates(best_by_build: pd.DataFrame) -> pd.DataFrame:
    """
    For each scenario, keep the single best row across all builds present in best_by_build.
    Adds column `_picked_build` = Build_Version that won.
    """
    if best_by_build.empty:
        return pd.DataFrame()
    rows = []
    for key, grp in best_by_build.groupby(
        ["Data_Source_Type", "Scan_Type", "is_acl", "is_feature_flags", "file_set_type"],
        dropna=False,
    ):
        if grp.empty:
            continue
        best = grp.iloc[0]
        best_score = score_row(best)
        for _, r in grp.iloc[1:].iterrows():
            sc = score_row(r)
            if sc > best_score:
                best, best_score = r, sc
        br = best.copy()
        br["_picked_build"] = str(br.get("Build_Version") or "").strip()
        rows.append(br)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def pct_delta(new_v: float, old_v: float) -> Optional[float]:
    if old_v == old_v and new_v == new_v and old_v not in (0, None) and abs(float(old_v)) > 1e-9:
        return (float(new_v) - float(old_v)) / float(old_v) * 100.0
    return None


def fmt_num(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    try:
        x = float(v)
        if abs(x) >= 1000:
            return f"{x:,.0f}"
        return f"{x:.4g}"
    except Exception:
        return str(v)


def build_report(
    df: pd.DataFrame,
    candidate_builds: List[str],
    baseline_build: str,
    file_set_keyword: str,
    dashboard_url: str,
    report_title: str,
) -> str:
    lines: List[str] = []
    cand_label = " | ".join(str(x).strip() for x in candidate_builds)
    lines.append(f"# {report_title}")
    lines.append("")
    lines.append("## What this report is")
    lines.append("")
    lines.append(
        f"- **Candidate builds ({cand_label}):** two (or more) runs on the **250.x** line. "
        "For each scenario we first pick the **best single run per build** (same rule as the dashboard "
        "**Trend Analysis** chart: shortest `total_scan_hours`, else highest `throughput_cluster_gb_hr`). "
        f"Then we take the **best of those** so you get **one row per scenario** representing the strongest 250.x result."
    )
    lines.append(
        f"- **Baseline ({baseline_build}):** same scenario selection — compared on the same axes as the trend charts "
        f"([open Trend Analysis]({dashboard_url}))."
    )
    lines.append("")
    lines.append("This is **not** “250.1 vs 250.9” as the headline story — it is **best-of-250.x** vs **{0}**.".format(baseline_build))
    lines.append("")

    per_cand = best_per_scenario_for_builds(df, candidate_builds, file_set_keyword)
    merged = merge_best_across_candidates(per_cand)
    baseline_df = best_per_scenario_for_builds(df, [baseline_build], file_set_keyword)

    if merged.empty and baseline_df.empty:
        lines.append("_No benchmark rows for the requested builds and file-set filter._")
        return "\n".join(lines)

    lines.append("## Best of 250.x (merged candidates) vs baseline")
    lines.append("")
    lines.append(
        "| Data source | Scan type | ACL | FF | File set | Best files/hr | Best GB/hr | Hours | From build | "
        f"{baseline_build} files/hr | {baseline_build} GB/hr | hrs | "
        "Δ files/hr vs baseline | Δ GB/hr vs baseline | scan_id (best) |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    keys_m = set(merged.apply(scenario_key_row, axis=1)) if not merged.empty else set()
    keys_b = set(baseline_df.apply(scenario_key_row, axis=1)) if not baseline_df.empty else set()
    all_keys = sorted(keys_m | keys_b, key=lambda x: (x[0], x[1], x[4], x[2], x[3]))

    for key in all_keys:
        ds, st, acl, ff, fst = key
        rm = None
        if not merged.empty:
            for _, r in merged.iterrows():
                if scenario_key_row(r) == key:
                    rm = r
                    break
        rb = None
        if not baseline_df.empty:
            for _, r in baseline_df.iterrows():
                if scenario_key_row(r) == key:
                    rb = r
                    break
        f_m = pd.to_numeric(rm.get("throughput_cluster_files_hr"), errors="coerce") if rm is not None else float("nan")
        g_m = pd.to_numeric(rm.get("throughput_cluster_gb_hr"), errors="coerce") if rm is not None else float("nan")
        f_b = pd.to_numeric(rb.get("throughput_cluster_files_hr"), errors="coerce") if rb is not None else float("nan")
        g_b = pd.to_numeric(rb.get("throughput_cluster_gb_hr"), errors="coerce") if rb is not None else float("nan")
        d_f = pct_delta(f_m, f_b) if rm is not None and rb is not None else None
        d_g = pct_delta(g_m, g_b) if rm is not None and rb is not None else None
        d_f_s = f"{d_f:+.1f}%" if d_f is not None and d_f == d_f else "—"
        d_g_s = f"{d_g:+.1f}%" if d_g is not None and d_g == d_g else "—"
        sid = str(rm.get("scan_id") or "").strip() if rm is not None else ""
        lines.append(
            f"| {ds} | {st} | {acl} | {ff} | {fst} | "
            f"{fmt_num(rm.get('throughput_cluster_files_hr') if rm is not None else None)} | "
            f"{fmt_num(rm.get('throughput_cluster_gb_hr') if rm is not None else None)} | "
            f"{fmt_num(rm.get('total_scan_hours') if rm is not None else None)} | "
            f"**{str(rm.get('_picked_build') or '') if rm is not None else ''}** | "
            f"{fmt_num(rb.get('throughput_cluster_files_hr') if rb is not None else None)} | "
            f"{fmt_num(rb.get('throughput_cluster_gb_hr') if rb is not None else None)} | "
            f"{fmt_num(rb.get('total_scan_hours') if rb is not None else None)} | "
            f"{d_f_s} | {d_g_s} | `{sid}` |"
        )

    lines.append("")
    lines.append("## Alignment with Trend Analysis charts")
    lines.append("")
    lines.append(
        f"On **[Trend Analysis]({dashboard_url})**, each point per build is the same “best run” rule within the filtered file set. "
        f"Use the chart to see **{baseline_build} → {cand_label}** as a time/build series; this table states the **numeric best-of-250.x** "
        f"and how it moves versus **{baseline_build}** on the same scenarios."
    )
    lines.append("")

    # Optional: paired median delta best-of-250 vs baseline
    paired_f: List[float] = []
    paired_g: List[float] = []
    for key in keys_m & keys_b:
        rm = next((r for _, r in merged.iterrows() if scenario_key_row(r) == key), None)
        rb = next((r for _, r in baseline_df.iterrows() if scenario_key_row(r) == key), None)
        if rm is None or rb is None:
            continue
        f_m = pd.to_numeric(rm.get("throughput_cluster_files_hr"), errors="coerce")
        f_b = pd.to_numeric(rb.get("throughput_cluster_files_hr"), errors="coerce")
        g_m = pd.to_numeric(rm.get("throughput_cluster_gb_hr"), errors="coerce")
        g_b = pd.to_numeric(rb.get("throughput_cluster_gb_hr"), errors="coerce")
        df_ = pct_delta(f_m, f_b)
        dg_ = pct_delta(g_m, g_b)
        if df_ is not None and df_ == df_:
            paired_f.append(df_)
        if dg_ is not None and dg_ == dg_:
            paired_g.append(dg_)

    if paired_f or paired_g:
        lines.append("### Paired scenarios (best-of-250.x vs baseline)")
        lines.append("")
        if paired_f:
            lines.append(
                f"- **Files/hr:** median Δ vs {baseline_build}: **{pd.Series(paired_f).median():+.1f}%** "
                f"({len(paired_f)} scenarios)."
            )
        if paired_g:
            lines.append(
                f"- **GB/hr:** median Δ vs {baseline_build}: **{pd.Series(paired_g).median():+.1f}%** "
                f"({len(paired_g)} scenarios)."
            )
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("- Data source: `benchmarks` + `builds` + `configurations` + overlays from `scan_insights` (same as dashboard).")
    lines.append("- Empty baseline or empty merged cells mean that scenario had no row for that build filter.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Best-of-250.x (merged candidates) Markdown report vs baseline build"
    )
    ap.add_argument(
        "--candidates",
        nargs="+",
        default=["250.1", "250.9"],
        help="Build versions to merge (best per scenario across these), e.g. 250.1 250.9",
    )
    ap.add_argument(
        "--baseline",
        default="249.14",
        help="Reference build to compare against (e.g. 249.14)",
    )
    ap.add_argument("--file-set", default="Small Files", help='Substring match on file_set_type')
    ap.add_argument("--title", default="", help="Report H1 title (default: auto)")
    ap.add_argument(
        "--trend-url",
        default="",
        help="Override dashboard trend URL",
    )
    ap.add_argument("--out", default="", help="Write Markdown to this path")
    ap.add_argument("--include-inactive-benchmarks", action="store_true")
    args = ap.parse_args()

    fs = str(args.file_set).strip()
    url = args.trend_url.strip()
    if not url:
        url = TREND_PAGE_LARGE if "large" in fs.lower() else TREND_PAGE_SMALL

    candidates = [str(x).strip() for x in args.candidates]
    baseline = str(args.baseline).strip()

    title = args.title.strip()
    if not title:
        title = f"250.x best-of ({' + '.join(candidates)}) vs {baseline} — {fs}"

    engine = create_engine(DB_CONNECTION_STRING)
    try:
        df = load_benchmarks_like_dashboard(engine, include_inactive=bool(args.include_inactive_benchmarks))
    except OperationalError as exc:
        print(f"DB connection failed: {exc}", file=sys.stderr)
        return 1

    md = build_report(df, candidates, baseline, fs, url, title)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
