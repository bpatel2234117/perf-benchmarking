"""
DB-backed prefill for Add Benchmark parity with dashboard.page_add_benchmark.

Loads file_set_types presets and findings rows (same queries / fallbacks as Streamlit).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from v2_api.add_benchmark_math import infer_add_benchmark_defaults_from_scan_name, infer_scan_benchmark_shape


def _try_data_source_names_from_map(engine: Engine, scan_id: str) -> Optional[str]:
    """Same idea as dashboard.load_scan_insight_map ds_by_scan backfill (optional tables)."""
    sid = str(scan_id or "").strip()
    if not sid:
        return None
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.name
                    FROM scan_insight_data_source_map m
                    JOIN scan_data_sources d ON d.id = m.data_source_id
                    WHERE m.scan_id = :sid
                    ORDER BY d.name
                    """
                ),
                {"sid": sid},
            ).fetchall()
            names = [str(r[0]).strip() for r in (rows or []) if r and r[0]]
            if names:
                return ", ".join(sorted(set(names)))
    except SQLAlchemyError:
        return None
    return None


def _latest_scan_progress_snapshot(engine: Engine, scan_id: str) -> Optional[Dict[str, Any]]:
    """Latest tracker snapshot — often populated before scan_insights totals (dashboard parity)."""
    sid = str(scan_id or "").strip()
    if not sid:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT objects_scanned, total_classifications, total_parts_scanned,
                           newly_added_classifications
                    FROM scan_progress
                    WHERE scan_id = :sid
                    ORDER BY COALESCE(captured_at_utc, "timestamp") DESC NULLS LAST, id DESC
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
            if not row:
                return None
            return {
                "objects_scanned": row[0],
                "total_classifications": row[1],
                "total_parts_scanned": row[2],
                "newly_added_classifications": row[3],
            }
    except SQLAlchemyError:
        return None
    return None


def load_file_set_type_row(engine: Engine, file_set_name: str) -> Optional[Dict[str, Any]]:
    """Lookup preset totals from normalized file_set_types (dashboard.get_file_set_types details)."""
    n = str(file_set_name or "").strip()
    if not n:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT name, data_type, total_files, total_folders, total_size_gb
                    FROM file_set_types
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name))
                    LIMIT 1
                    """
                ),
                {"name": n},
            ).fetchone()
            if row:
                return {
                    "name": row[0],
                    "data_type": row[1],
                    "total_files": row[2],
                    "total_folders": row[3],
                    "total_size_gb": float(row[4]) if row[4] is not None else None,
                }
    except SQLAlchemyError:
        return None
    return None


def load_findings_prefill_rows(engine: Engine, scan_id: str) -> List[Dict[str, Any]]:
    """
    Mirrors dashboard.get_scan_attribute_findings_prefill (without BigID sync side-effects).

    Uses scan_insight_attribute_delta_map when present; falls back to scan_progress aggregates.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        return []

    rows = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT a.name, m.is_new, m.is_removed, m.finding_count, m.attribute_value,
                           m.new_count_value, m.removed_count_value
                    FROM scan_insight_attribute_delta_map m
                    JOIN scan_classifier_attributes a ON a.id = m.attribute_id
                    WHERE m.scan_id = :sid
                    ORDER BY a.name
                    """
                ),
                {"sid": sid},
            ).fetchall()
    except SQLAlchemyError:
        rows = []

    prefill_rows: List[Dict[str, Any]] = []
    for row in rows:
        attr_name, is_new, is_removed, finding_count, stored_value, new_count_value, removed_count_value = row
        label = ""
        if bool(is_new) and bool(is_removed):
            label = "New, Removed"
        elif bool(is_new):
            label = "New"
        elif bool(is_removed):
            label = "Removed"
        if str(stored_value or "").strip():
            label = str(stored_value or "").strip()
        try:
            new_cnt = int(new_count_value) if new_count_value is not None else 0
        except Exception:
            new_cnt = 0
        try:
            rem_cnt = int(removed_count_value) if removed_count_value is not None else 0
        except Exception:
            rem_cnt = 0
        if new_cnt > 0:
            prefill_rows.append(
                {
                    "Classifier Attribute": str(attr_name or "").strip(),
                    "Attribute Value": "New",
                    "Findings": new_cnt,
                }
            )
        if rem_cnt > 0:
            prefill_rows.append(
                {
                    "Classifier Attribute": str(attr_name or "").strip(),
                    "Attribute Value": "Removed",
                    "Findings": rem_cnt,
                }
            )
        if new_cnt == 0 and rem_cnt == 0:
            try:
                findings_count_val = int(finding_count) if finding_count is not None else 0
            except Exception:
                findings_count_val = 0
            prefill_rows.append(
                {
                    "Classifier Attribute": str(attr_name or "").strip(),
                    "Attribute Value": label,
                    "Findings": findings_count_val,
                }
            )

    if not prefill_rows and sid:
        try:
            with engine.connect() as conn:
                last_progress = conn.execute(
                    text(
                        """
                        SELECT total_classifications, newly_added_classifications
                        FROM scan_progress
                        WHERE scan_id = :sid
                        ORDER BY COALESCE(captured_at_utc, "timestamp") DESC NULLS LAST, id DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": sid},
                ).fetchone()
                if last_progress and last_progress[0] and int(last_progress[0]) > 0:
                    prefill_rows.append(
                        {
                            "Classifier Attribute": "General Classifications",
                            "Attribute Value": "Aggregate",
                            "Findings": int(last_progress[0]),
                        }
                    )
        except SQLAlchemyError:
            pass

    seen_names = set()
    final_rows: List[Dict[str, Any]] = []
    for r in prefill_rows:
        key = (r["Classifier Attribute"], r["Attribute Value"])
        if key not in seen_names:
            final_rows.append(r)
            seen_names.add(key)

    return final_rows


def build_form_sections_meta(
    engine: Engine,
    scan_id: str,
    parent: Dict[str, Any],
    insights: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Structured sections mirroring Streamlit Add New Benchmark layout:
    run metrics labels, file set preset row, insight captions, findings table rows.
    """
    scan_name = str(parent.get("scan_name") or "")
    prefill = infer_add_benchmark_defaults_from_scan_name(scan_name)
    ds_hint = str(prefill.get("data_source_type") or "").strip()
    ins_in = insights or {}
    if not ds_hint:
        raw = str(ins_in.get("data_source_names") or "").strip()
        if raw:
            ds_hint = raw.split(",")[0].strip()
    if not ds_hint:
        mapped = _try_data_source_names_from_map(engine, scan_id)
        if mapped:
            ds_hint = mapped.split(",")[0].strip()

    shape = infer_scan_benchmark_shape(scan_name, ds_hint)

    fs_label = str(shape.get("file_set_type") or "").strip()
    preset_row = load_file_set_type_row(engine, fs_label)
    if not preset_row and ds_hint and prefill.get("file_set_type"):
        alt = f"{ds_hint} {prefill['file_set_type']}".strip()
        preset_row = load_file_set_type_row(engine, alt)

    pf_tf = preset_row.get("total_files") if preset_row else None
    pf_tfo = preset_row.get("total_folders") if preset_row else None
    pf_tgb = preset_row.get("total_size_gb") if preset_row else None

    total_files: Optional[int] = int(pf_tf) if pf_tf is not None else None
    total_folders: Optional[int] = int(pf_tfo) if pf_tfo is not None else None
    total_gb: Optional[float] = float(pf_tgb) if pf_tgb is not None else None

    insight_captions: List[str] = []
    ins = insights or {}
    files_from_insights = False

    try:
        osc = ins.get("objects_scanned")
        if osc is not None and str(osc).strip() != "":
            total_files = int(osc)
            files_from_insights = True
            insight_captions.append(f"Prefilled from scan_insights: Total Files = {total_files:,}")
        oen = ins.get("objects_enumerated")
        if oen is not None and str(oen).strip() != "":
            total_folders = int(oen)
        tgb = ins.get("total_size_gb")
        if tgb is not None and str(tgb).strip() != "":
            total_gb = float(tgb)
            insight_captions.append(f"Prefilled from scan_insights: Total Size (GB) = {total_gb:.4f}")
    except Exception:
        pass

    # Latest tracker snapshot — fills gaps when file_set_types has no preset ("Mixed Files") and scan_insights is sparse.
    snap = _latest_scan_progress_snapshot(engine, scan_id)
    if not files_from_insights and snap and snap.get("objects_scanned") is not None and str(snap.get("objects_scanned")).strip() != "":
        try:
            total_files = int(snap["objects_scanned"])
            insight_captions.append(f"Prefilled from scan_progress: objects_scanned = {total_files:,}")
        except Exception:
            pass
    if total_folders is None and snap and snap.get("total_parts_scanned") is not None:
        try:
            parts = int(snap["total_parts_scanned"])
            if parts >= 0:
                total_folders = parts
                insight_captions.append(f"Prefilled from scan_progress: total_parts_scanned = {total_folders:,} (proxy)")
        except Exception:
            pass

    display_scenario = str(prefill.get("scenario_type") or "").strip() or shape.get("scenario_type") or ""

    run_metrics = {
        "scenario_type": display_scenario,
        "scan_type": prefill.get("scan_type"),
        "data_source_type": ds_hint or prefill.get("data_source_type"),
        "data_type": shape.get("data_type"),
        "file_set_type": fs_label,
        "is_acl": bool(prefill.get("is_acl")),
        "is_ff": bool(prefill.get("is_ff")),
        "build_version": prefill.get("build_version"),
    }

    file_set_details_row = {
        "file_set_type": fs_label,
        "total_files": total_files,
        "total_folders": total_folders,
        # None => unknown (show —); 0.0 => explicit zero from DB overlay.
        "total_size_gb": total_gb,
        "preset_table_snapshot": preset_row,
        "insight_captions": insight_captions,
    }

    findings_prefill = load_findings_prefill_rows(engine, scan_id)

    eff_tf = int(total_files) if total_files is not None else 0
    eff_tfo = int(total_folders) if total_folders is not None else 0
    eff_tgb = float(total_gb) if total_gb is not None else 0.0

    return {
        "run_metrics": run_metrics,
        "file_set_details": file_set_details_row,
        "findings_prefill": findings_prefill,
        "effective_volumes": {
            "total_files": eff_tf,
            "total_folders": eff_tfo,
            "total_gb": eff_tgb,
        },
    }
