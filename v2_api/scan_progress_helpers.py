"""
Scan Progress detail helpers for v2_api (no Streamlit / pandas).
Used by FastAPI routes to mirror dashboard Scan Progress data paths.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from bigid_auth import Get_auth_token

from v2_api.scan_bigid_sync import (
    extract_scan_attributes_payload,
    fetch_parent_scan_attributes_http,
    load_environment_for_scan_id,
)


def _loads_json(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, dict):
        return x
    try:
        return json.loads(str(x))
    except Exception:
        return None


def _cnt_attr(v: Any) -> int:
    if v is True:
        return 1
    if v in (False, None, ""):
        return 0
    try:
        return int(v)
    except Exception:
        return 0


def classifier_rows_from_items(items: List[Any]) -> List[Dict[str, Any]]:
    rows_out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = (
            item.get("_id")
            or item.get("id")
            or item.get("name")
            or item.get("attributeName")
            or item.get("classifierAttribute")
        )
        if raw_id is None:
            continue
        label = str(raw_id).replace("classifier.", "").strip()
        if not label:
            continue
        rows_out.append(
            {
                "classifier": label,
                "new_findings": _cnt_attr(item.get("new")),
                "removed": _cnt_attr(item.get("removed")),
            }
        )
    rows_out.sort(key=lambda r: (-int(r.get("new_findings") or 0), str(r.get("classifier") or "")))
    return rows_out


def parse_classifier_hits_from_payloads(
    attributes_payload_raw: Any,
    insight_payload_raw: Any,
) -> Tuple[List[Dict[str, Any]], str, Optional[int]]:
    """
    Returns (rows, note, total_count_hint).
    """
    items: List[Any] = []
    total_hint: Optional[int] = None
    source = ""

    ap_obj = _loads_json(attributes_payload_raw)
    if isinstance(ap_obj, dict):
        items, total_hint = extract_scan_attributes_payload(ap_obj)
        if items:
            source = "attributes API snapshot"

    if not items:
        ip_obj = _loads_json(insight_payload_raw)
        if isinstance(ip_obj, dict):
            items, total_hint = extract_scan_attributes_payload(ip_obj)
            if items:
                source = "parent-scan insight payload"
        if not items and isinstance(ip_obj, dict):
            data = ip_obj.get("data")
            if isinstance(data, dict):
                si = data.get("scanInsights")
                if isinstance(si, dict):
                    items, total_hint = extract_scan_attributes_payload(si)
                    if items:
                        source = "insight.scanInsights"

    if not items:
        return (
            [],
            "attributes_payload is empty for this scan. Run Sync now (manual) to fetch attributes from BigID.",
            total_hint,
        )

    rows = classifier_rows_from_items(items if isinstance(items, list) else [])
    if not rows:
        return ([], "Attributes payload exists but no classifier rows could be parsed.", total_hint)

    note = f"Source: {source} · {len(rows)} row(s) in DB"
    if total_hint is not None and int(total_hint) > len(rows):
        note += (
            f" (API totalCount {int(total_hint)} — increase attributes fetch limit in sync for full table)."
        )
    return rows, note, total_hint


def load_classifier_hits_from_db(
    engine: Engine, scan_id: str
) -> Tuple[List[Dict[str, Any]], str, Optional[int], List[str]]:
    """Classifier rows from scan_insights + fallback name list from classifier_attribute_names."""
    sid = str(scan_id or "").strip()
    if not sid:
        return [], "scan_id required", None, []

    fallback_names: List[str] = []
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT attributes_payload, insight_payload, classifier_attribute_names
                    FROM scan_insights
                    WHERE scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if not row:
            return ([], "No scan_insights row yet — use Sync now (manual) or wait for tracker.", None, [])

        raw_names = row[2]
        if raw_names and str(raw_names).strip():
            fallback_names = [x.strip() for x in str(raw_names).split(",") if x.strip()]

        rows, note, hint = parse_classifier_hits_from_payloads(row[0], row[1])
        return rows, note, hint, fallback_names
    except SQLAlchemyError as ex:
        return [], f"scan_insights unavailable: {ex}", None, []


def fetch_live_classifier_hits_from_bigid(
    engine: Engine, scan_id: str
) -> Tuple[List[Dict[str, Any]], str, Optional[int]]:
    """One-shot attributes API (does not persist). Same idea as dashboard live fallback."""
    sid = str(scan_id or "").strip()
    if not sid:
        return [], "scan_id required", None

    env_row = load_environment_for_scan_id(engine, sid)
    if not env_row:
        return [], "No environment linked to this scan (scan_parents).", None

    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        return [], "test_environments.base_url is empty.", None

    try:
        token = Get_auth_token(base_url, env_row)
        payload = fetch_parent_scan_attributes_http(base_url, sid, token, skip=0, limit=500)
        items, total_hint = extract_scan_attributes_payload(payload if isinstance(payload, dict) else {})
        rows = classifier_rows_from_items(items if isinstance(items, list) else [])
        note = f"Live BigID attributes API · {len(rows)} row(s)"
        return rows, note, total_hint
    except Exception as ex:
        return [], f"Live fetch failed: {ex}", None


def pg_table_exists(engine: Engine, table: str) -> bool:
    t = str(table or "").strip().lower()
    if not t:
        return False
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = current_schema() AND lower(table_name) = lower(:t)
                    )
                    """
                ),
                {"t": t},
            ).scalar()
        return bool(r)
    except SQLAlchemyError:
        return False


def resolve_env_id_for_scan(engine: Engine, scan_id: str) -> Optional[int]:
    sid = str(scan_id or "").strip()
    if not sid:
        return None
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
            if r and r[0] is not None:
                return int(r[0])
            wr = conn.execute(
                text(
                    "SELECT environment_id FROM workflow_steps WHERE scan_id = :sid ORDER BY id DESC LIMIT 1"
                ),
                {"sid": sid},
            ).fetchone()
            if wr and wr[0] is not None:
                return int(wr[0])
    except SQLAlchemyError:
        pass
    return None


def scan_progress_time_window(engine: Engine, scan_id: str) -> Tuple[Optional[Any], Optional[Any]]:
    """Min/max snapshot times from scan_progress for scanner chart window."""
    sid = str(scan_id or "").strip()
    if not sid:
        return None, None
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    """
                    SELECT MIN(COALESCE(captured_at_utc, "timestamp")),
                           MAX(COALESCE(captured_at_utc, "timestamp"))
                    FROM scan_progress
                    WHERE scan_id = :sid
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if not r:
            return None, None
        return r[0], r[1]
    except SQLAlchemyError:
        return None, None


def load_scanner_performance_series(engine: Engine, scan_id: str) -> Dict[str, Any]:
    """
    Returns JSON-serializable scanner samples for React charts.
    If scanner_* tables are missing (common on Postgres-only installs), available=false.
    """
    sid = str(scan_id or "").strip()
    out: Dict[str, Any] = {
        "available": False,
        "scan_id": sid,
        "environment_id": None,
        "window_start": None,
        "window_end": None,
        "groups": [],
        "individuals": [],
        "message": "",
    }

    if not pg_table_exists(engine, "scanner_group_performance") or not pg_table_exists(
        engine, "scanner_individual_performance"
    ):
        out["message"] = "Scanner performance tables are not present in this database (tracker writes these on MySQL)."
        return out

    env_id = resolve_env_id_for_scan(engine, sid)
    if env_id is None:
        out["message"] = "Could not resolve environment_id for this scan."
        return out

    tmin, tmax = scan_progress_time_window(engine, sid)
    if tmin is None or tmax is None:
        out["message"] = "No scan_progress snapshots — cannot bound scanner time window."
        out["environment_id"] = env_id
        return out

    try:
        start_b = tmin
        end_b = tmax
        # Mirror dashboard padding
        if hasattr(start_b, "__sub__"):
            start_b = start_b - timedelta(minutes=30)
        if hasattr(end_b, "__add__"):
            end_b = end_b + timedelta(minutes=60)
    except Exception:
        start_b, end_b = tmin, tmax

    out["environment_id"] = env_id
    out["window_start"] = str(start_b) if start_b is not None else None
    out["window_end"] = str(end_b) if end_b is not None else None

    try:
        with engine.connect() as conn:
            gsql = text(
                """
                SELECT captured_at_utc, COALESCE(group_name,''), COALESCE(running_scanners,0),
                       COALESCE(running_threads,0), COALESCE(max_threads,0), COALESCE(scanner_threads,0),
                       COALESCE(avg_scan_speed,0), COALESCE(physical_memory_mb,0), COALESCE(free_memory_mb,0)
                FROM scanner_group_performance
                WHERE environment_id = :eid
                  AND captured_at_utc BETWEEN :start AND :end
                ORDER BY captured_at_utc ASC
                """
            )
            grows = conn.execute(gsql, {"eid": env_id, "start": start_b, "end": end_b}).fetchall()

            # Try wider column set for individuals; fall back if PG schema differs.
            def _ind_rows():
                sql_wide = text(
                    """
                    SELECT captured_at_utc, COALESCE(scanner_id,''), COALESCE(name,''), COALESCE(status,''),
                           COALESCE(group_name,''), COALESCE(running_threads,0), COALESCE(total_num_of_threads,0),
                           COALESCE(system_load_average,0), COALESCE(physical_memory_mb,0), COALESCE(free_memory_mb,0),
                           cpu_pct, available_processors
                    FROM scanner_individual_performance
                    WHERE environment_id = :eid
                      AND captured_at_utc BETWEEN :start AND :end
                    ORDER BY captured_at_utc ASC
                    """
                )
                try:
                    return conn.execute(sql_wide, {"eid": env_id, "start": start_b, "end": end_b}).fetchall()
                except ProgrammingError:
                    sql_narrow = text(
                        """
                        SELECT captured_at_utc, COALESCE(scanner_id,''), COALESCE(name,''), COALESCE(status,''),
                               COALESCE(group_name,''), COALESCE(running_threads,0), COALESCE(total_num_of_threads,0),
                               COALESCE(system_load_average,0), COALESCE(physical_memory_mb,0), COALESCE(free_memory_mb,0)
                        FROM scanner_individual_performance
                        WHERE environment_id = :eid
                          AND captured_at_utc BETWEEN :start AND :end
                        ORDER BY captured_at_utc ASC
                        """
                    )
                    return conn.execute(sql_narrow, {"eid": env_id, "start": start_b, "end": end_b}).fetchall()

            irows = _ind_rows()

        groups: List[Dict[str, Any]] = []
        for r in grows:
            phy = float(r[7] or 0)
            free = float(r[8] or 0)
            used = max(0.0, phy - free)
            spd = float(r[6] or 0)
            groups.append(
                {
                    "captured_at_utc": str(r[0]) if r[0] is not None else "",
                    "group_name": str(r[1] or ""),
                    "running_scanners": int(r[2] or 0),
                    "running_threads": int(r[3] or 0),
                    "max_threads": int(r[4] or 0),
                    "scanner_threads": int(r[5] or 0),
                    "avg_scan_speed": spd,
                    "avg_scan_speed_kib_s": spd / 1024.0 if spd else 0.0,
                    "physical_memory_mb": phy,
                    "free_memory_mb": free,
                    "used_memory_mb": used,
                }
            )

        individuals: List[Dict[str, Any]] = []
        for r in irows:
            sla = float(r[7] or 0)
            phy = float(r[8] or 0)
            free = float(r[9] or 0)
            used = max(0.0, phy - free)
            mem_pct = (used / phy * 100.0) if phy > 0 else 0.0
            cpu_f: Optional[float] = None
            ap_f: Optional[float] = None
            if len(r) >= 12:
                try:
                    cpu_f = float(r[10]) if r[10] is not None else None
                except Exception:
                    cpu_f = None
                try:
                    ap_f = float(r[11]) if r[11] is not None else None
                except Exception:
                    ap_f = None
            if cpu_f is None and ap_f and ap_f > 0:
                cpu_f = sla * 100.0 / ap_f
            individuals.append(
                {
                    "captured_at_utc": str(r[0]) if r[0] is not None else "",
                    "scanner_id": str(r[1] or ""),
                    "name": str(r[2] or ""),
                    "status": str(r[3] or ""),
                    "group_name": str(r[4] or ""),
                    "running_threads": int(r[5] or 0),
                    "total_num_of_threads": int(r[6] or 0),
                    "system_load_average": sla,
                    "physical_memory_mb": phy,
                    "free_memory_mb": free,
                    "used_memory_mb": used,
                    "memory_usage_pct": mem_pct,
                    "cpu_pct": cpu_f,
                }
            )

        out["groups"] = groups
        out["individuals"] = individuals
        out["available"] = True
        out["summary"] = {
            "group_samples": len(groups),
            "pod_samples": len(individuals),
        }
        if not groups and not individuals:
            out["message"] = "No scanner_group_performance / scanner_individual_performance rows in this window."
        return out
    except SQLAlchemyError as ex:
        out["message"] = str(ex)
        return out


def load_scan_log_correlation(engine: Engine, scan_id: str) -> Dict[str, Any]:
    sid = str(scan_id or "").strip()
    out: Dict[str, Any] = {"scan_id": sid, "report": None, "timestamp": None, "is_final": None}
    if not sid:
        return out
    if not pg_table_exists(engine, "scan_log_correlations"):
        return out
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT correlation_report, "timestamp", is_final
                    FROM scan_log_correlations
                    WHERE scan_id = :sid
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if row:
            out["report"] = row[0]
            out["timestamp"] = str(row[1]) if row[1] is not None else None
            out["is_final"] = bool(row[2]) if row[2] is not None else None
    except SQLAlchemyError:
        pass
    return out


def load_scan_log_errors_for_scan(engine: Engine, scan_id: str, limit: int = 40) -> List[Dict[str, Any]]:
    sid = str(scan_id or "").strip()
    if not sid:
        return []
    if not pg_table_exists(engine, "scan_log_errors"):
        return []
    lim = max(1, min(int(limit), 200))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT log_timestamp, log_source, scan_id, error_context, ai_analysis, is_resolved
                    FROM scan_log_errors
                    WHERE scan_id = :sid
                    ORDER BY id DESC
                    LIMIT :lim
                    """
                ),
                {"sid": sid, "lim": lim},
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "log_timestamp": r[0],
                    "log_source": r[1],
                    "scan_id": r[2],
                    "error_context": (str(r[3])[:8000] if r[3] else ""),
                    "ai_analysis": (str(r[4])[:4000] if r[4] else ""),
                    "is_resolved": bool(r[5]) if r[5] is not None else False,
                }
            )
        return out
    except SQLAlchemyError:
        return []
