#!/usr/bin/env python3
"""
Build a Markdown comparison report for all scans tied to a workflow run.

Usage (repo root):
  python scripts/workflow_run_scan_report.py --run-id 24
  python scripts/workflow_run_scan_report.py --run-id 24 --out workflow_24_report.md

Uses: workflow_steps, workflow_runs, scan_parents, auto_scan_benchmarks, scan_insights,
      aggregated scan_progress per scan_id.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from db_config import DB_CONNECTION_STRING  # noqa: E402


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return pd.to_datetime(s, utc=True, errors="coerce").to_pydatetime()
    except Exception:
        return None


def _hours_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if not a or not b:
        return None
    try:
        return max(0.0, (b - a).total_seconds() / 3600.0)
    except Exception:
        return None


def _norm_fs(s: str) -> str:
    u = str(s or "").strip().upper()
    if "LARGE" in u:
        return "large"
    if "SMALL" in u:
        return "small"
    if "MIXED" in u:
        return "mixed"
    return str(s or "").strip().lower() or "unknown"


def _norm_ds(s: str) -> str:
    x = str(s or "").strip().lower()
    if x in {"share", "smb", "cifs", "windows_file_share"}:
        return "smb_share"
    return x or "unknown"


def _boolish(v: Any) -> bool:
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def load_run_report(engine, run_id: int) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    with engine.connect() as conn:
        r0 = conn.execute(
            text(
                "SELECT id, name, status, started_at, finished_at, cooldown_minutes, "
                "env1_id, env2_id, last_error FROM workflow_runs WHERE id = :rid LIMIT 1"
            ),
            {"rid": int(run_id)},
        ).fetchone()
        if r0:
            meta = {
                "id": r0[0],
                "name": r0[1],
                "status": r0[2],
                "started_at": r0[3],
                "finished_at": r0[4],
                "cooldown_minutes": r0[5],
                "env1_id": r0[6],
                "env2_id": r0[7],
                "last_error": r0[8],
            }

        rows = conn.execute(
            text(
                """
                SELECT
                    ws.seq,
                    ws.environment_id,
                    te.env_name,
                    ws.ds_kind,
                    ws.ds_name,
                    ws.scan_kind,
                    ws.file_set_type,
                    ws.is_acl,
                    ws.is_ols,
                    ws.is_structured_ff,
                    ws.notes,
                    ws.build_version,
                    ws.scan_id,
                    ws.status AS step_status,
                    ws.started_at AS step_started_at,
                    ws.finished_at AS step_finished_at,
                    sp.scan_status,
                    sp.started_at AS parent_started_at,
                    sp.completed_at AS parent_completed_at,
                    sp.scan_name AS parent_scan_name,
                    ab.total_scan_hours,
                    ab.objects_scanned AS ab_objects,
                    ab.objects_with_pi AS ab_pii,
                    ab.objects_enumerated AS ab_enum,
                    ab.total_classification AS ab_tcl,
                    ab.new_attributes AS ab_new_attr,
                    ab.removed_attributes AS ab_rem_attr,
                    ab.total_size_gb AS ab_size_gb,
                    ab.throughput_cluster_files_hr AS ab_tp_files,
                    ab.throughput_cluster_gb_hr AS ab_tp_gb,
                    si.objects_scanned AS si_objects,
                    si.objects_with_pi AS si_pii,
                    si.total_classification AS si_tcl,
                    pg.objects_scanned_max,
                    pg.objects_with_pi_max,
                    pg.tcl_max,
                    pg.snap_min,
                    pg.snap_max,
                    pg.latest_state
                FROM workflow_steps ws
                LEFT JOIN test_environments te ON te.id = ws.environment_id
                LEFT JOIN scan_parents sp ON sp.scan_id = ws.scan_id
                LEFT JOIN auto_scan_benchmarks ab ON ab.scan_id = ws.scan_id
                LEFT JOIN scan_insights si ON si.scan_id = ws.scan_id
                LEFT JOIN (
                    SELECT
                        scan_id,
                        MAX(COALESCE(objects_scanned, 0)) AS objects_scanned_max,
                        MAX(COALESCE(objects_with_pi, 0)) AS objects_with_pi_max,
                        MAX(COALESCE(total_classifications, 0)) AS tcl_max,
                        MIN(COALESCE(captured_at_utc, timestamp)) AS snap_min,
                        MAX(COALESCE(captured_at_utc, timestamp)) AS snap_max,
                        SUBSTRING(MAX(CONCAT(LPAD(id, 20, '0'), '|', COALESCE(scan_state, ''))), 22) AS latest_state
                    FROM scan_progress
                    GROUP BY scan_id
                ) pg ON pg.scan_id = ws.scan_id
                WHERE ws.run_id = :rid
                ORDER BY ws.seq ASC
                """
            ),
            {"rid": int(run_id)},
        ).fetchall()

    cols = [
        "seq",
        "environment_id",
        "env_name",
        "ds_kind",
        "ds_name",
        "scan_kind",
        "file_set_type",
        "is_acl",
        "is_ols",
        "is_structured_ff",
        "notes",
        "build_version",
        "scan_id",
        "step_status",
        "step_started_at",
        "step_finished_at",
        "scan_status",
        "parent_started_at",
        "parent_completed_at",
        "parent_scan_name",
        "total_scan_hours",
        "ab_objects",
        "ab_pii",
        "ab_enum",
        "ab_tcl",
        "ab_new_attr",
        "ab_rem_attr",
        "ab_size_gb",
        "ab_tp_files",
        "ab_tp_gb",
        "si_objects",
        "si_pii",
        "si_tcl",
        "objects_scanned_max",
        "objects_with_pi_max",
        "tcl_max",
        "snap_min",
        "snap_max",
        "latest_state",
    ]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    return df, meta


def enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["ds_norm"] = out["ds_kind"].map(_norm_ds)
    out["file_set_bucket"] = out["file_set_type"].map(_norm_fs)
    out["acl_flag"] = out["is_acl"].map(_boolish)

    def _objects_row(r):
        for c in ("ab_objects", "si_objects", "objects_scanned_max"):
            v = r.get(c)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                try:
                    iv = int(v)
                    if iv > 0:
                        return iv
                except Exception:
                    pass
        return None

    def _pii_row(r):
        for c in ("ab_pii", "si_pii", "objects_with_pi_max"):
            v = r.get(c)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                try:
                    return int(v)
                except Exception:
                    pass
        return None

    out["objects_eff"] = out.apply(_objects_row, axis=1)
    out["pii_eff"] = out.apply(_pii_row, axis=1)
    out["pii_rate"] = out.apply(
        lambda r: (float(r["pii_eff"]) / float(r["objects_eff"])) if r["objects_eff"] and r["pii_eff"] is not None else None,
        axis=1,
    )

    tp_files = []
    tp_calc = []
    wall_h = []
    for _, r in out.iterrows():
        tpf = r.get("ab_tp_files")
        if tpf is not None and not (isinstance(tpf, float) and pd.isna(tpf)) and float(tpf) > 0:
            tp_files.append(float(tpf))
        else:
            tp_files.append(None)
        obs = r.get("objects_eff")
        tsh = r.get("total_scan_hours")
        calc = None
        try:
            if obs and tsh and float(tsh) > 0:
                calc = float(obs) / float(tsh)
        except Exception:
            pass
        tp_calc.append(calc)
        ps = _parse_dt(r.get("parent_started_at"))
        pe = _parse_dt(r.get("parent_completed_at"))
        ss = _parse_dt(r.get("step_started_at"))
        sf = _parse_dt(r.get("step_finished_at"))
        wall_h.append(_hours_between(ss, sf) or _hours_between(ps, pe))
    out["wall_hours"] = wall_h
    tp_merged: List[Optional[float]] = []
    for i, (_, row) in enumerate(out.iterrows()):
        tpf = tp_files[i]
        calc = tp_calc[i]
        wh = wall_h[i]
        obs = row.get("objects_eff")
        if tpf is not None:
            tp_merged.append(tpf)
        elif calc is not None:
            tp_merged.append(calc)
        else:
            try:
                if obs and wh and float(wh) > 0:
                    tp_merged.append(float(obs) / float(wh))
                else:
                    tp_merged.append(None)
            except Exception:
                tp_merged.append(None)
    out["throughput_files_hr"] = tp_merged

    out["scenario_label"] = out.apply(
        lambda r: f"{_norm_ds(r['ds_kind'])} | {str(r.get('file_set_type') or '').strip() or '?'} | "
        f"ACL={_boolish(r.get('is_acl'))}",
        axis=1,
    )
    return out


def md_table(df: pd.DataFrame, columns: List[str]) -> str:
    if df.empty:
        return "_No rows._\n"
    use = df[[c for c in columns if c in df.columns]].copy()
    hdr = "| " + " | ".join(str(c) for c in use.columns) + " |"
    sep = "| " + " | ".join("---" for _ in use.columns) + " |"
    lines = [hdr, sep]
    for _, row in use.iterrows():
        cells = []
        for c in use.columns:
            v = row[c]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                cells.append("")
            elif isinstance(v, float):
                cells.append(f"{v:.4g}" if abs(v) < 1e6 else f"{v:,.0f}")
            else:
                cells.append(str(v).replace("|", "\\|")[:200])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def compare_pairs(df: pd.DataFrame, a_mask: pd.Series, b_mask: pd.Series, title: str) -> str:
    a = df.loc[a_mask].sort_values("seq", ascending=True)
    b = df.loc[b_mask].sort_values("seq", ascending=True)
    lines = [f"### {title}", ""]
    if a.empty and b.empty:
        lines.append("_No matching steps._\n")
        return "\n".join(lines)
    if a.empty or b.empty:
        lines.append(f"_One side missing (A rows={len(a)}, B rows={len(b)})._")
        lines.append("")
    for side, sub in (("A", a), ("B", b)):
        if sub.empty:
            continue
        r = sub.iloc[0]
        lines.append(f"**{side}** — step {int(r['seq'])} `{r.get('ds_kind')}` / `{r.get('file_set_type')}` ACL={_boolish(r.get('is_acl'))}")
        lines.append(
            f"- scan_id: `{r.get('scan_id')}` | status: {r.get('step_status') or r.get('scan_status')} | build: {r.get('build_version')}"
        )
        pr = r.get("pii_rate")
        pr_str = (
            f"{float(pr) * 100:.2f}%"
            if pr is not None and not (isinstance(pr, float) and pd.isna(pr))
            else "-"
        )
        lines.append(
            f"- objects: {r.get('objects_eff')} | PII objects: {r.get('pii_eff')} | PII rate: {pr_str}"
        )
        lines.append(
            f"- wall hours (step/parent): {r.get('wall_hours')} | ab total_scan_hours: {r.get('total_scan_hours')}"
        )
        lines.append(
            f"- throughput files/hr: {r.get('throughput_files_hr')} | total_class (max): {r.get('tcl_max') or r.get('ab_tcl')}"
        )
        lines.append("")
    if not a.empty and not b.empty:
        ra, rb = a.iloc[0], b.iloc[0]
        oa, ob = ra.get("objects_eff"), rb.get("objects_eff")
        ta, tb = ra.get("throughput_files_hr"), rb.get("throughput_files_hr")
        lines.append("**Delta (A vs B)**")
        if oa and ob:
            lines.append(f"- objects ratio B/A: {float(ob)/float(oa):.3f}")
        if ta and tb and float(ta) > 0:
            lines.append(f"- throughput ratio B/A: {float(tb)/float(ta):.3f}")
        lines.append("")
    return "\n".join(lines)


def build_markdown(run_id: int, df: pd.DataFrame, meta: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Workflow run {run_id} — scan comparison")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    if meta:
        lines.append(f"- **Name:** {meta.get('name')}")
        lines.append(f"- **Status:** {meta.get('status')}")
        lines.append(f"- **Started / finished:** {meta.get('started_at')} → {meta.get('finished_at')}")
        lines.append(f"- **Envs:** env1_id={meta.get('env1_id')} env2_id={meta.get('env2_id')}")
        if meta.get("last_error"):
            lines.append(f"- **Last error:** {str(meta.get('last_error'))[:300]}")
    else:
        lines.append("_Run not found._")
    lines.append("")

    if df.empty:
        lines.append("## Steps")
        lines.append("_No workflow_steps rows for this run._")
        return "\n".join(lines)

    lines.append("## Per-step summary")
    lines.append("")
    show_cols = [
        "seq",
        "env_name",
        "ds_kind",
        "file_set_type",
        "is_acl",
        "notes",
        "scan_id",
        "step_status",
        "build_version",
        "objects_eff",
        "pii_eff",
        "pii_rate",
        "wall_hours",
        "total_scan_hours",
        "throughput_files_hr",
        "ab_tp_gb",
        "ab_size_gb",
        "tcl_max",
        "latest_state",
    ]
    lines.append(md_table(df, show_cols))

    lines.append("## Paired comparisons (same run)")
    lines.append("")
    lines.append(
        "_Matchers use normalized DS kind (`s3`, `smb_share`, …) and file-set bucket (`small` / `large` / …). "
        "Adjust filters in the script if your `ds_kind` values differ (e.g. `share`)._"
    )
    lines.append("")

    # S3 small vs SMB small (non-ACL)
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "S3 Small Files (no ACL) vs SMB Small Files (no ACL)",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "large") & (~df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "large") & (~df["acl_flag"]),
            "S3 Large Files vs SMB Large Files (no ACL)",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (df["acl_flag"]),
            "S3 Small (ACL ON) vs SMB Small (ACL ON)",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "large") & (df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "large") & (df["acl_flag"]),
            "S3 Large (ACL ON) vs SMB Large (ACL ON)",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "S3 Small + ACL vs SMB Small without ACL",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (df["acl_flag"]),
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "S3 Small with ACL vs S3 Small without ACL",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "SMB Small with ACL vs SMB Small without ACL",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "large") & (~df["acl_flag"]),
            (df["ds_norm"] == "s3") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "S3 Large vs S3 Small (no ACL)",
        )
    )
    lines.append(
        compare_pairs(
            df,
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "large") & (~df["acl_flag"]),
            (df["ds_norm"] == "smb_share") & (df["file_set_bucket"] == "small") & (~df["acl_flag"]),
            "SMB Large vs SMB Small (no ACL)",
        )
    )

    lines.append("## Observations (auto)")
    lines.append("")
    done = df[df["step_status"].astype(str).str.lower().eq("completed")]
    if not done.empty:
        tp_col = done["throughput_files_hr"]
        if tp_col.notna().any():
            best_tp = tp_col.max()
            worst_tp = tp_col.min()
            lines.append(
                f"- **Completed steps:** {len(done)} / {len(df)}. "
                f"Highest throughput (files/hr, merged): **{best_tp:.4g}**; lowest: **{worst_tp:.4g}**."
            )
            idx = tp_col.idxmax()
            hi = done.loc[idx]
            lines.append(
                f"- Fastest step by throughput: seq **{int(hi['seq'])}** `{hi.get('ds_kind')}` / `{hi.get('file_set_type')}`."
            )
        else:
            lines.append(f"- **Completed steps:** {len(done)} / {len(df)} (throughput not available for completed rows).")
    pii = df[df["pii_rate"].notna()]
    if not pii.empty and pii["pii_rate"].notna().any():
        mx = pii.loc[pii["pii_rate"].idxmax()]
        lines.append(
            f"- Highest PII rate: step **{int(mx['seq'])}** ({float(mx['pii_rate']) * 100:.2f}% of objects) "
            f"`{mx.get('ds_kind')}` / `{mx.get('file_set_type')}`."
        )
    lines.append("- **Data quality:** metrics come from `auto_scan_benchmarks` first, then `scan_insights`, then max from `scan_progress`.")
    lines.append("- **Timing:** `wall_hours` uses workflow step timestamps, else parent scan started/completed.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Workflow run scan comparison report")
    ap.add_argument("--run-id", type=int, default=24, help="workflow_runs.id")
    ap.add_argument("--out", type=str, default="", help="write Markdown to this path")
    args = ap.parse_args()

    engine = create_engine(DB_CONNECTION_STRING)
    try:
        df, meta = load_run_report(engine, args.run_id)
    except OperationalError as exc:
        print(
            "Could not connect to MySQL (check VPN / db_config.py). "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1
    df = enrich_df(df)
    md = build_markdown(args.run_id, df, meta)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
