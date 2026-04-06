#!/usr/bin/env python3
"""
Generate a Markdown report: best benchmark row per scenario for two builds (e.g. 250.1 vs 250.9),
then a "best of both" column. Aligns with dashboard Small/Large Files Trend Analysis pick logic.

Usage (repo root):
  python scripts/report_250_build_best.py --builds 250.1 250.9 --file-set "Small Files" --out report_250_small_files.md

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


def pick_best_between(a: Optional[pd.Series], b: Optional[pd.Series]) -> Tuple[Optional[pd.Series], str]:
    """Choose row with higher cluster files/hr; tie-break higher gb/hr; then lower hours."""
    if a is None and b is None:
        return None, ""
    if a is None:
        return b, "250.9 only"
    if b is None:
        return a, "250.1 only"

    def score(s: pd.Series) -> Tuple[float, float, float]:
        tf = pd.to_numeric(s.get("throughput_cluster_files_hr"), errors="coerce")
        tg = pd.to_numeric(s.get("throughput_cluster_gb_hr"), errors="coerce")
        th = pd.to_numeric(s.get("total_scan_hours"), errors="coerce")
        return (
            float(tf) if tf == tf else float("-inf"),
            float(tg) if tg == tg else float("-inf"),
            -float(th) if th == th else 0.0,
        )

    sa, sb = score(a), score(b)
    if sa > sb:
        return a, "250.1"
    if sb > sa:
        return b, "250.9"
    return a, "tie"


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
    builds: List[str],
    file_set_keyword: str,
    dashboard_url: str,
) -> str:
    lines: List[str] = []
    b1, b2 = builds[0], builds[1]
    lines.append(f"# Build {b1} vs {b2} — best runs report ({file_set_keyword})")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append(
        f"This report mirrors the dashboard **Trend Analysis** pick logic: for each scenario, "
        f"the fastest run by `total_scan_hours` wins; if hours are missing, highest `throughput_cluster_gb_hr` wins."
    )
    lines.append("")
    lines.append(f"- **Interactive charts:** [Small/Large Files Trend Analysis]({dashboard_url})")
    lines.append(
        "- **Overall performance:** compare cluster **GB/hr** and **files/hr** between builds; "
        "higher is better for throughput, lower **scan hours** is better for duration."
    )
    lines.append("")

    sub = df[
        df["Build_Version"].astype(str).isin(builds)
        & df["file_set_type"].astype(str).str.contains(file_set_keyword, case=False, na=False)
    ].copy()
    if sub.empty:
        lines.append("_No benchmark rows for these builds and file-set filter._")
        return "\n".join(lines)

    # Best per build per scenario
    best_by_build: Dict[str, pd.DataFrame] = {}
    for bv in builds:
        part = sub[sub["Build_Version"].astype(str) == str(bv)].copy()
        if part.empty:
            best_by_build[bv] = pd.DataFrame()
            continue
        rows = []
        for key, grp in part.groupby(
            ["Data_Source_Type", "Scan_Type", "is_acl", "is_feature_flags", "file_set_type"],
            dropna=False,
        ):
            rows.append(select_best_run(grp))
        best_by_build[bv] = pd.DataFrame(rows) if rows else pd.DataFrame()

    lines.append("## Summary — build-level (best run per scenario)")
    lines.append("")
    lines.append(
        "| Data source | Scan type | ACL | FF | File set | "
        f"{b1} files/hr | {b1} GB/hr | {b1} hrs | "
        f"{b2} files/hr | {b2} GB/hr | {b2} hrs | "
        "**Best of two** | **Winner** |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    # Union scenarios
    keys = set()
    for bv in builds:
        dfb = best_by_build.get(bv)
        if dfb is None or dfb.empty:
            continue
        for _, r in dfb.iterrows():
            keys.add(scenario_key_row(r))

    for key in sorted(keys, key=lambda x: (x[0], x[1], x[4], x[2], x[3])):
        ds, st, acl, ff, fst = key
        r1 = None
        r2 = None
        for _, r in best_by_build.get(b1, pd.DataFrame()).iterrows():
            if scenario_key_row(r) == key:
                r1 = r
                break
        for _, r in best_by_build.get(b2, pd.DataFrame()).iterrows():
            if scenario_key_row(r) == key:
                r2 = r
                break
        winner_row, winner_label = pick_best_between(r1, r2)
        w_files = fmt_num(winner_row.get("throughput_cluster_files_hr")) if winner_row is not None else ""
        lines.append(
            f"| {ds} | {st} | {acl} | {ff} | {fst} | "
            f"{fmt_num(r1.get('throughput_cluster_files_hr') if r1 is not None else None)} | "
            f"{fmt_num(r1.get('throughput_cluster_gb_hr') if r1 is not None else None)} | "
            f"{fmt_num(r1.get('total_scan_hours') if r1 is not None else None)} | "
            f"{fmt_num(r2.get('throughput_cluster_files_hr') if r2 is not None else None)} | "
            f"{fmt_num(r2.get('throughput_cluster_gb_hr') if r2 is not None else None)} | "
            f"{fmt_num(r2.get('total_scan_hours') if r2 is not None else None)} | "
            f"**{w_files}** | {winner_label} |"
        )

    lines.append("")
    lines.append("## Trend narrative (auto)")
    lines.append("")

    # Aggregate mean throughput per build for trend line
    for bv in builds:
        dfb = best_by_build.get(bv)
        if dfb is None or dfb.empty:
            lines.append(f"- **{bv}:** no rows after best-run selection.")
            continue
        m_gb = pd.to_numeric(dfb["throughput_cluster_gb_hr"], errors="coerce").mean()
        m_f = pd.to_numeric(dfb["throughput_cluster_files_hr"], errors="coerce").mean()
        m_h = pd.to_numeric(dfb["total_scan_hours"], errors="coerce").mean()
        lines.append(
            f"- **{bv}** (mean of best-per-scenario): **{m_f:,.0f}** files/hr, **{m_gb:,.2f}** GB/hr, "
            f"**{m_h:,.2f}** h mean duration."
        )

    if not best_by_build.get(b1, pd.DataFrame()).empty and not best_by_build.get(b2, pd.DataFrame()).empty:
        k1 = set(best_by_build[b1].apply(scenario_key_row, axis=1))
        k2 = set(best_by_build[b2].apply(scenario_key_row, axis=1))
        common = k1 & k2
        if common:
            deltas_f = []
            deltas_g = []
            for key in common:
                r1 = next(r for _, r in best_by_build[b1].iterrows() if scenario_key_row(r) == key)
                r2 = next(r for _, r in best_by_build[b2].iterrows() if scenario_key_row(r) == key)
                f1 = pd.to_numeric(r1.get("throughput_cluster_files_hr"), errors="coerce")
                f2 = pd.to_numeric(r2.get("throughput_cluster_files_hr"), errors="coerce")
                g1 = pd.to_numeric(r1.get("throughput_cluster_gb_hr"), errors="coerce")
                g2 = pd.to_numeric(r2.get("throughput_cluster_gb_hr"), errors="coerce")
                if f1 == f1 and f2 == f2 and f1 and f2:
                    deltas_f.append((f2 - f1) / f1 * 100.0)
                if g1 == g1 and g2 == g2 and g1 and g2:
                    deltas_g.append((g2 - g1) / g1 * 100.0)
            if deltas_f:
                lines.append(
                    f"- **Files/hr (paired scenarios, {len(deltas_f)} pairs):** median Δ **{pd.Series(deltas_f).median():+.1f}%** "
                    f"({b2} vs {b1})."
                )
            if deltas_g:
                lines.append(
                    f"- **GB/hr (paired scenarios):** median Δ **{pd.Series(deltas_g).median():+.1f}%** "
                    f"({b2} vs {b1})."
                )

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Rows come from `benchmarks` joined to `builds` / `configurations`, same as the dashboard table.")
    lines.append("- If a build has no row for a scenario, cells show blank.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="250.x best-run Markdown report (two builds)")
    ap.add_argument("--builds", nargs=2, default=["250.1", "250.9"], metavar=("B1", "B2"))
    ap.add_argument("--file-set", default="Small Files", help='Substring match on file_set_type, e.g. "Small Files" or "Large Files"')
    ap.add_argument(
        "--trend-url",
        default="",
        help="Override dashboard trend URL (default: Small Files Analysis on 10.2.20.89)",
    )
    ap.add_argument("--out", default="", help="Write Markdown to this path")
    ap.add_argument("--include-inactive-benchmarks", action="store_true")
    args = ap.parse_args()

    fs = str(args.file_set).strip()
    url = args.trend_url.strip()
    if not url:
        url = TREND_PAGE_LARGE if "large" in fs.lower() else TREND_PAGE_SMALL

    engine = create_engine(DB_CONNECTION_STRING)
    try:
        df = load_benchmarks_like_dashboard(engine, include_inactive=bool(args.include_inactive_benchmarks))
    except OperationalError as exc:
        print(f"DB connection failed: {exc}", file=sys.stderr)
        return 1

    md = build_report(df, [str(x).strip() for x in args.builds], fs, url)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
