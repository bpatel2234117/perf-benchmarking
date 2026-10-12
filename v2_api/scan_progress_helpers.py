"""
Scan Progress detail helpers for v2_api (no Streamlit / pandas).
Used by FastAPI routes to mirror dashboard Scan Progress data paths.

Scanner metrics and log errors shown here are bounded to each scan's period
(started_at → completed_at, or start → last progress snapshot while running).
Background capture (tracker, spb_logs) runs only while a parent scan is running.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
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


def _is_current_scan_collection_record(payload: Any, scan_id: str) -> bool:
    """Return whether a ``scans`` record is the requested scan itself.

    The BigID ``scans`` collection also returns child scans with only a
    ``parent_scan_id`` pointing at the requested scan. Those children are not
    current-scan records for the detail panel and must not be shown there.
    """
    wanted = str(scan_id or "").strip()
    if not wanted or not isinstance(payload, dict):
        return False
    return str(payload.get("_id") or "").strip() == wanted


def _cnt_attr(v: Any) -> int:
    if v is True:
        return 1
    if v in (False, None, ""):
        return 0
    try:
        return int(v)
    except Exception:
        return 0


def _load_previous_build_classifier_hits(engine: Engine, scan_id: str) -> Dict[str, Dict[str, int]]:
    """
    Load classifier hits from the previous build scan for comparison.
    Returns a dict mapping classifier name to {'new': count, 'removed': count}.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        return {}
    
    try:
        # Get current scan's build version and workflow context
        with engine.connect() as conn:
            cur_row = conn.execute(
                text("""
                    SELECT build_version, ds_kind, file_set_type, is_acl, is_ols, is_structured_ff, environment_id
                    FROM workflow_steps
                    WHERE scan_id = :sid
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                """),
                {"sid": sid}
            ).fetchone()
        
        if not cur_row:
            return {}
        
        cur_build = str(cur_row[0] or "").strip()
        ds_kind = str(cur_row[1] or "").strip()
        file_set_type = cur_row[2]
        is_acl = int(cur_row[3] or 0)
        is_ols = int(cur_row[4] or 0)
        is_structured_ff = int(cur_row[5] or 0)
        env_id = cur_row[6]
        
        if not cur_build or not ds_kind:
            return {}
        
        # Find previous build scan with same parameters
        with engine.connect() as conn:
            prev_row = conn.execute(
                text("""
                    SELECT ws.scan_id
                    FROM workflow_steps ws
                    WHERE ws.scan_id IS NOT NULL AND TRIM(ws.scan_id) <> ''
                      AND ws.scan_id <> :cur_sid
                      AND ws.ds_kind = :ds_kind
                      AND ((:fst IS NULL AND ws.file_set_type IS NULL) OR ws.file_set_type = :fst)
                      AND COALESCE(ws.is_acl, 0) = :acl
                      AND COALESCE(ws.is_ols, 0) = :ols
                      AND COALESCE(ws.is_structured_ff, 0) = :sff
                      AND ws.build_version < :cur_build
                    ORDER BY (ws.environment_id = :env_id) DESC, ws.updated_at DESC, ws.id DESC
                    LIMIT 1
                """),
                {
                    "cur_sid": sid,
                    "ds_kind": ds_kind,
                    "fst": file_set_type,
                    "acl": is_acl,
                    "ols": is_ols,
                    "sff": is_structured_ff,
                    "cur_build": cur_build,
                    "env_id": env_id if env_id else -1
                }
            ).fetchone()
        
        if not prev_row:
            return {}
        
        prev_scan_id = str(prev_row[0]).strip()
        
        # Load classifier hits from previous scan
        with engine.connect() as conn:
            prev_si_row = conn.execute(
                text("""
                    SELECT attributes_payload, insight_payload
                    FROM scan_insights
                    WHERE scan_id = :sid
                    LIMIT 1
                """),
                {"sid": prev_scan_id}
            ).fetchone()
        
        if not prev_si_row:
            return {}
        
        ap_obj = _loads_json(prev_si_row[0])
        items = []
        if isinstance(ap_obj, dict):
            items, _ = extract_scan_attributes_payload(ap_obj)
        
        if not items:
            ip_obj = _loads_json(prev_si_row[1])
            if isinstance(ip_obj, dict):
                items, _ = extract_scan_attributes_payload(ip_obj)
        
        # Build result dict
        result = {}
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
            
            result[label] = {
                "new": _cnt_attr(item.get("new")),
                "removed": _cnt_attr(item.get("removed"))
            }
        
        return result
    except Exception:
        return {}


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

    ap_obj = _loads_json(attributes_payload_raw)
    if isinstance(ap_obj, dict):
        items, total_hint = extract_scan_attributes_payload(ap_obj)
        if items:
            source = "attributes API snapshot"

    ip_obj = _loads_json(insight_payload_raw)
    if not items and isinstance(ip_obj, dict):
        items, insight_hint = extract_scan_attributes_payload(ip_obj)
        if items:
            source = "parent-scan insight payload"
        if total_hint is None:
            total_hint = insight_hint

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
) -> Tuple[List[Dict[str, Any]], str, Optional[int], List[str], Optional[str], Optional[str]]:
    """Classifier rows from scan_insights + fallback name list from classifier_attribute_names + build versions."""
    sid = str(scan_id or "").strip()
    if not sid:
        return [], "scan_id required", None, [], None, None

    fallback_names: List[str] = []
    cur_build_version: Optional[str] = None
    prev_build_version: Optional[str] = None
    
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
            return ([], "No scan_insights row yet — use Sync now (manual) or wait for tracker.", None, [], None, None)

        raw_names = row[2]
        if raw_names and str(raw_names).strip():
            fallback_names = [x.strip() for x in str(raw_names).split(",") if x.strip()]

        rows, note, hint = parse_classifier_hits_from_payloads(row[0], row[1])
        
        # Get build versions (always, not just when prev_hits exist)
        try:
            with engine.connect() as conn:
                cur_row = conn.execute(
                    text("""
                        SELECT build_version, ds_kind, file_set_type, is_acl, is_ols, is_structured_ff, environment_id
                        FROM workflow_steps
                        WHERE scan_id = :sid
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 1
                    """),
                    {"sid": sid}
                ).fetchone()
                if cur_row:
                    cur_build_version = str(cur_row[0] or "").strip()
                    ds_kind = str(cur_row[1] or "").strip()
                    file_set_type = cur_row[2]
                    is_acl = int(cur_row[3] or 0)
                    is_ols = int(cur_row[4] or 0)
                    is_structured_ff = int(cur_row[5] or 0)
                    env_id = cur_row[6]
                    
                    prev_row = conn.execute(
                        text("""
                            SELECT build_version
                            FROM workflow_steps
                            WHERE scan_id IS NOT NULL AND TRIM(scan_id) <> ''
                              AND scan_id <> :cur_sid
                              AND ds_kind = :ds_kind
                              AND ((:fst IS NULL AND file_set_type IS NULL) OR file_set_type = :fst)
                              AND COALESCE(is_acl, 0) = :acl
                              AND COALESCE(is_ols, 0) = :ols
                              AND COALESCE(is_structured_ff, 0) = :sff
                              AND build_version < :cur_build
                            ORDER BY (environment_id = :env_id) DESC, updated_at DESC, id DESC
                            LIMIT 1
                        """),
                        {
                            "cur_sid": sid,
                            "ds_kind": ds_kind,
                            "fst": file_set_type,
                            "acl": is_acl,
                            "ols": is_ols,
                            "sff": is_structured_ff,
                            "cur_build": cur_build_version,
                            "env_id": env_id if env_id else -1
                        }
                    ).fetchone()
                    if prev_row:
                        prev_build_version = str(prev_row[0] or "").strip()
        except Exception:
            pass
        
        # Add previous build classifier hits for comparison
        try:
            prev_hits = _load_previous_build_classifier_hits(engine, sid)
            if prev_hits:
                
                # Add previous build columns to current rows
                for row_dict in rows:
                    cls_name = row_dict.get("classifier", "")
                    if cls_name in prev_hits:
                        row_dict["prev_new"] = prev_hits[cls_name].get("new", 0)
                        row_dict["prev_removed"] = prev_hits[cls_name].get("removed", 0)
                    else:
                        row_dict["prev_new"] = 0
                        row_dict["prev_removed"] = 0
                
                # Add classifiers from previous build that are not in current build
                current_classifiers = {r.get("classifier", "") for r in rows}
                prev_only_classifiers = set(prev_hits.keys()) - current_classifiers
                for cls_name in sorted(prev_only_classifiers):
                    rows.append({
                        "classifier": cls_name,
                        "new_findings": 0,
                        "removed": 0,
                        "prev_new": prev_hits[cls_name].get("new", 0),
                        "prev_removed": prev_hits[cls_name].get("removed", 0),
                    })
                
                # Re-sort by new_findings descending
                rows.sort(key=lambda r: (-int(r.get("new_findings") or 0), str(r.get("classifier") or "")))
        except Exception:
            # If previous build lookup fails, just add zero columns
            for row_dict in rows:
                row_dict["prev_new"] = 0
                row_dict["prev_removed"] = 0
        
        return rows, note, hint, fallback_names, cur_build_version, prev_build_version
    except SQLAlchemyError as ex:
        return [], f"scan_insights unavailable: {ex}", None, [], None, None


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


def load_scan_part_records(
    engine: Engine,
    scan_id: str,
    limit: int = 500,
    collection: str = "scan_parts",
) -> Dict[str, Any]:
    """Load collection records, returning the newest scan-part rows for the UI."""
    sid = str(scan_id or "").strip()
    if not sid or not pg_table_exists(engine, "scan_part_records"):
        return {"count": 0, "new_filter_count": 0, "items": [], "new_items": []}
    try:
        requested_limit = max(1, min(int(limit), 2000))
    except Exception:
        requested_limit = 500
    source_collection = str(collection or "scan_parts").strip()[:64] or "scan_parts"
    display_limit = min(requested_limit, 15 if source_collection == "scans" else 5)
    # The scans collection contains parent + child rows. Fetch enough rows to
    # remove child rows before calculating the displayed count; scan_parts keeps
    # its smaller bounded query because it already has an exact DB-side scope.
    fetch_limit = 2000 if source_collection == "scans" else min(max(display_limit, 25), 2000)
    try:
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM scan_part_records WHERE scan_id = :sid AND collection = :collection"),
                    {"sid": sid, "collection": source_collection},
                ).scalar()
                or 0
            )
            new_filter_count = 0
            if source_collection == "scan_parts":
                new_filter_count = int(
                    conn.execute(
                        text("""
                            SELECT COUNT(*)
                            FROM scan_part_records
                            WHERE scan_id = :sid
                              AND collection = :collection
                              AND LOWER(state) = 'new'
                        """),
                        {"sid": sid, "collection": source_collection},
                    ).scalar()
                    or 0
                )
            rows = conn.execute(
                text("""
                    SELECT id, subscan_id, state, record_key, record_payload, captured_at_utc
                    FROM scan_part_records
                    WHERE scan_id = :sid
                      AND collection = :collection
                    ORDER BY captured_at_utc DESC, id DESC
                    LIMIT :lim
                """),
                {"sid": sid, "collection": source_collection, "lim": fetch_limit},
            ).fetchall()
            new_rows = []
            if source_collection == "scan_parts" and new_filter_count:
                # The unfiltered window can be dominated by terminal records.
                # Query New independently so the first UI block is truly the
                # latest five New records, not merely New rows among the latest
                # 25 records overall.
                new_rows = conn.execute(
                    text("""
                        SELECT id, subscan_id, state, record_key, record_payload, captured_at_utc
                        FROM scan_part_records
                        WHERE scan_id = :sid
                          AND collection = :collection
                          AND LOWER(state) = 'new'
                        ORDER BY captured_at_utc DESC, id DESC
                        LIMIT :lim
                    """),
                    {"sid": sid, "collection": source_collection, "lim": fetch_limit},
                ).fetchall()
        items: List[Dict[str, Any]] = []
        def row_to_item(row: Any) -> Dict[str, Any]:
            payload = _loads_json(row[4])
            return {
                "id": int(row[0]) if row[0] is not None else None,
                "subscan_id": str(row[1]) if row[1] is not None else None,
                "state": str(row[2] or ""),
                "record_key": str(row[3] or ""),
                "captured_at_utc": str(row[5]) if row[5] is not None else None,
                "payload": payload if payload is not None else {},
            }

        for row in rows:
            items.append(row_to_item(row))
        filtered_new_items = [row_to_item(row) for row in new_rows]
        if source_collection == "scans":
            items = [
                item for item in items
                if _is_current_scan_collection_record(item["payload"], sid)
            ]
            # The DB owner column is shared by parent and child rows. Recount
            # after filtering so the UI count reflects only the current scan.
            total = len(items)
        def latest_key(item: Dict[str, Any]) -> Tuple[str, str, int]:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            payload_time = str(
                payload.get("updated_at")
                or payload.get("updatedAt")
                or payload.get("created_at")
                or payload.get("createdAt")
                or ""
            )
            return (payload_time, str(item.get("captured_at_utc") or ""), int(item.get("id") or 0))

        items.sort(key=latest_key, reverse=True)
        new_items = filtered_new_items or [
            item for item in items
            if str(item.get("state") or "").strip().casefold() == "new"
        ]
        new_items.sort(key=latest_key, reverse=True)
        return {
            "count": total,
            "new_filter_count": new_filter_count,
            "items": items[:display_limit],
            "new_items": new_items[:display_limit],
        }
    except SQLAlchemyError:
        return {"count": 0, "new_filter_count": 0, "items": [], "new_items": []}


def pg_column_exists(engine: Engine, table: str, column: str) -> bool:
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND lower(table_name) = lower(:t)
                          AND lower(column_name) = lower(:c)
                    )
                    """
                ),
                {"t": str(table or "").strip(), "c": str(column or "").strip()},
            ).scalar()
        return bool(r)
    except Exception:
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


_TERMINAL_SCAN_STATUSES = frozenset(
    {
        "completed",
        "completedwithfailures",
        "failed",
        "stopped",
        "stop",
        "aborted",
        "cancelled",
        "canceled",
        "paused",
        "pause",
    }
)


def _parse_scan_period_dt(value: Any) -> Optional[datetime]:
    """Parse scan_parents timestamps to naive UTC for period filtering."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if getattr(dt, "tzinfo", None) is not None:
            return dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    s = str(value).strip()
    if not s:
        return None
    try:
        iso = s
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if getattr(dt, "tzinfo", None) is not None:
            return dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    s = s.replace("T", " ").rstrip("Z").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def resolve_scan_period_window(engine: Engine, scan_id: str) -> Dict[str, Any]:
    """
    Scan period for scanner/log charts: bounded by scan start and end times
    (scan_parents.started_at → completed_at, or now while running).
    The URL scan_id only selects which scan's period to show — filters are time-based.
    """
    sid = str(scan_id or "").strip()
    out: Dict[str, Any] = {
        "scan_id": sid,
        "period_start": None,
        "period_end": None,
        "period_start_raw": None,
        "period_end_raw": None,
        "is_running": False,
    }
    if not sid:
        return out
    try:
        with engine.connect() as conn:
            parent = conn.execute(
                text(
                    """
                    SELECT started_at, completed_at, scan_status
                    FROM scan_parents
                    WHERE scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if parent:
            raw_start, raw_end, raw_status = parent[0], parent[1], parent[2]
            out["period_start_raw"] = str(raw_start) if raw_start is not None else None
            out["period_end_raw"] = str(raw_end) if raw_end is not None else None
            start_dt = _parse_scan_period_dt(raw_start)
            status = str(raw_status or "").strip().replace(" ", "").lower()
            terminal = status in _TERMINAL_SCAN_STATUSES
            out["is_running"] = bool(status) and not terminal
            if terminal and raw_end:
                end_dt = _parse_scan_period_dt(raw_end)
                if end_dt is not None:
                    end_dt = end_dt + timedelta(minutes=1)
            elif out["is_running"]:
                end_dt = datetime.utcnow()
            else:
                end_dt = datetime.utcnow()
            if start_dt is None:
                tmin, tmax = scan_progress_time_window(engine, sid)
                start_dt = _parse_scan_period_dt(tmin)
                if end_dt is None or (not terminal and tmax):
                    end_dt = _parse_scan_period_dt(tmax) or end_dt
            out["period_start"] = start_dt
            out["period_end"] = end_dt
            return out
    except SQLAlchemyError:
        pass
    tmin, tmax = scan_progress_time_window(engine, sid)
    out["period_start"] = _parse_scan_period_dt(tmin)
    out["period_end"] = _parse_scan_period_dt(tmax) or datetime.utcnow()
    out["period_start_raw"] = str(tmin) if tmin is not None else None
    out["period_end_raw"] = str(tmax) if tmax is not None else None
    return out


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


def try_live_scanner_capture_for_scan(engine: Engine, scan_id: str) -> bool:
    """One-shot BigID capture when DB has no per-pod rows (e.g. tracker queue was broken during scan)."""
    sid = str(scan_id or "").strip()
    env_id = resolve_env_id_for_scan(engine, sid)
    if env_id is None:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, env_name, base_url, auth_module_path, auth_function_name
                    FROM test_environments WHERE id = :eid LIMIT 1
                    """
                ),
                {"eid": int(env_id)},
            ).fetchone()
        if not row:
            return False
        env_row = {
            "id": row[0],
            "env_name": row[1],
            "base_url": row[2],
            "auth_module_path": row[3],
            "auth_function_name": row[4],
        }
    except Exception:
        return False
    try:
        from scanner_performance import capture_scanner_performance_for_env
    except ImportError:
        try:
            from v2_api.scanner_performance import capture_scanner_performance_for_env  # type: ignore
        except ImportError:
            return False
    try:
        out = capture_scanner_performance_for_env(engine, env_row, captured_at=datetime.utcnow())
        return bool(out and out.get("ok"))
    except Exception:
        return False


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

    period = resolve_scan_period_window(engine, sid)
    tmin, tmax = scan_progress_time_window(engine, sid)
    start_b = period.get("period_start")
    end_b = period.get("period_end")
    # Original chart window: scan_progress snapshots with padding (works before scan_parents times exist).
    if tmin is not None and tmax is not None:
        try:
            start_b = _parse_scan_period_dt(tmin)
            end_b = _parse_scan_period_dt(tmax)
            if start_b is not None:
                start_b = start_b - timedelta(minutes=30)
            if end_b is not None:
                end_b = end_b + timedelta(minutes=60)
        except Exception:
            pass
    if start_b is None or end_b is None:
        out["message"] = "No scan_progress snapshots — cannot bound scanner time window."
        out["environment_id"] = env_id
        return out

    out["environment_id"] = env_id
    out["period_start"] = str(period.get("period_start_raw") or start_b)
    out["period_end"] = str(period.get("period_end_raw") or end_b)
    out["period_is_running"] = bool(period.get("is_running"))
    out["window_start"] = str(start_b)
    out["window_end"] = str(end_b)
    out["message"] = ""

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

            name_col = (
                "scanner_name"
                if pg_column_exists(engine, "scanner_individual_performance", "scanner_name")
                else "name"
            )
            threads_col = (
                "max_threads"
                if pg_column_exists(engine, "scanner_individual_performance", "max_threads")
                else "total_num_of_threads"
            )
            cpu_expr = (
                "cpu_pct"
                if pg_column_exists(engine, "scanner_individual_performance", "cpu_pct")
                else "NULL::double precision AS cpu_pct"
            )
            status_expr = (
                "COALESCE(status,'')"
                if pg_column_exists(engine, "scanner_individual_performance", "status")
                else "''"
            )
            irows = conn.execute(
                text(
                    f"""
                    SELECT captured_at_utc, COALESCE(scanner_id,''), COALESCE({name_col},''),
                           COALESCE(group_name,''), COALESCE(running_threads,0), COALESCE({threads_col},0),
                           COALESCE(physical_memory_mb,0), COALESCE(free_memory_mb,0),
                           {cpu_expr}, {status_expr}
                    FROM scanner_individual_performance
                    WHERE environment_id = :eid
                      AND captured_at_utc BETWEEN :start AND :end
                    ORDER BY captured_at_utc ASC
                    """
                ),
                {"eid": env_id, "start": start_b, "end": end_b},
            ).fetchall()

            if not irows and env_id is not None:
                try:
                    if try_live_scanner_capture_for_scan(engine, sid):
                        irows = conn.execute(
                            text(
                                f"""
                                SELECT captured_at_utc, COALESCE(scanner_id,''), COALESCE({name_col},''),
                                       COALESCE(group_name,''), COALESCE(running_threads,0), COALESCE({threads_col},0),
                                       COALESCE(physical_memory_mb,0), COALESCE(free_memory_mb,0),
                                       {cpu_expr}, {status_expr}
                                FROM scanner_individual_performance
                                WHERE environment_id = :eid
                                  AND captured_at_utc BETWEEN :start AND :end
                                ORDER BY captured_at_utc ASC
                                """
                            ),
                            {"eid": env_id, "start": start_b, "end": end_b},
                        ).fetchall()
                except Exception:
                    pass

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
            phy = float(r[6] or 0)
            free = float(r[7] or 0)
            used = max(0.0, phy - free)
            mem_pct = (used / phy * 100.0) if phy > 0 else 0.0
            cpu_pct = float(r[8]) if len(r) > 8 and r[8] is not None else None
            raw_status = str(r[9] or "").strip() if len(r) > 9 else ""
            individuals.append(
                {
                    "captured_at_utc": str(r[0]) if r[0] is not None else "",
                    "scanner_id": str(r[1] or ""),
                    "name": str(r[2] or ""),
                    "status": raw_status or "Running",
                    "group_name": str(r[3] or ""),
                    "running_threads": int(r[4] or 0),
                    "total_num_of_threads": int(r[5] or 0),
                    "physical_memory_mb": phy,
                    "free_memory_mb": free,
                    "used_memory_mb": used,
                    "memory_usage_pct": round(mem_pct, 1),
                    "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
                    "scan_speed": 0,
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
            out["message"] = (
                "No data yet for this environment — the tracker samples scanner groups and pods "
                "every few minutes while a scan is running (TRACKER_SCANNER_SAMPLE_MINUTES, default 2)."
            )
        return out
    except SQLAlchemyError as ex:
        out["message"] = str(ex)
        return out


def _is_datadog_related(*parts: Any) -> bool:
    """User-facing log panel: skip Datadog agent/API noise."""
    blob = " ".join(str(p or "") for p in parts).lower()
    if not blob.strip():
        return False
    needles = (
        "datadog",
        "dd-trace",
        "ddtrace",
        "datadoghq",
        "dd_agent",
        "dd-agent",
    )
    return any(n in blob for n in needles)


def load_scan_log_correlation(engine: Engine, scan_id: str) -> Dict[str, Any]:
    sid = str(scan_id or "").strip()
    out: Dict[str, Any] = {
        "scan_id": sid,
        "report": None,
        "timestamp": None,
        "is_final": None,
        "period_start": None,
        "period_end": None,
    }
    if not sid:
        return out
    period = resolve_scan_period_window(engine, sid)
    out["period_start"] = str(period.get("period_start_raw") or period.get("period_start"))
    out["period_end"] = str(period.get("period_end_raw") or period.get("period_end"))
    if not pg_table_exists(engine, "scan_log_correlations"):
        return out
    start_b = period.get("period_start")
    end_b = period.get("period_end")
    try:
        with engine.connect() as conn:
            if start_b is not None and end_b is not None:
                row = conn.execute(
                    text(
                        """
                        SELECT correlation_report, "timestamp", is_final
                        FROM scan_log_correlations
                        WHERE scan_id = :sid
                          AND "timestamp" >= :started
                          AND "timestamp" <= :ended
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": sid, "started": start_b, "ended": end_b},
                ).fetchone()
            else:
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
            report_txt = str(row[0] or "")
            if _is_datadog_related(report_txt):
                out["report"] = None
                out["datadog_filtered"] = True
            else:
                out["report"] = report_txt
            out["timestamp"] = str(row[1]) if row[1] is not None else None
            out["is_final"] = bool(row[2]) if row[2] is not None else None
    except SQLAlchemyError:
        pass
    return out


# Real-error signals. A captured block is shown only if at least one of its lines
# carries a genuine severity tag or a strong error keyword. Pure INFO/DEBUG/TRACE
# heartbeat blocks (e.g. "Initializing RDBConnectorObjectReader", health checks)
# are noise and must not appear in the errors panel.
_REAL_ERROR_LEVEL_RE = re.compile(r"\[(?:ERROR|FATAL|SEVERE|WARN|WARNING)\]", re.I)
_REAL_ERROR_KEYWORD_RE = re.compile(
    r"(?i)(?:"
    r"\bexception\b|\bstack ?trace\b|\btraceback\b|\bfatal\b|\bsevere\b|"
    r"\bfail(?:ed|ure)?\b|\brefused\b|\btimeout\b|\btimed ?out\b|\bunreachable\b|"
    r"\bbroken ?pipe\b|\bconnection.?(?:reset|closed|lost|drop)\b|"
    r"\bout.?of.?memory\b|\boom\b|\bnull ?pointer\b|\bnullpointer\b|"
    r"\bunauthorized\b|\bforbidden\b|\baccess.?denied\b|\bdenied\b|"
    r"\b(?:http|status)\s+[45]\d{2}\b|\b(?:400|401|403|404|408|409|429|500|502|503|504)\b|"
    r"\bcaused by\b|\bpanic\b|\bcrash(?:ed)?\b"
    r")"
)


def _context_has_real_error(ctx: str) -> bool:
    """True only if the captured block contains a genuine error signal (severity tag
    or strong keyword) — strips URLs first so path segments don't false-match."""
    if not ctx:
        return False
    if _REAL_ERROR_LEVEL_RE.search(ctx):
        return True
    scrubbed = re.sub(r"https?://[^\s]+", " ", ctx, flags=re.I)
    return bool(_REAL_ERROR_KEYWORD_RE.search(scrubbed))


def load_scan_log_errors_for_scan(engine: Engine, scan_id: str, limit: int = 40) -> Dict[str, Any]:
    """Log errors for Scan Progress — original env + scan_parents time window (with workflow env fallback)."""
    sid = str(scan_id or "").strip()
    period = resolve_scan_period_window(engine, sid) if sid else {}
    empty: Dict[str, Any] = {
        "scan_id": sid,
        "period_start": str(period.get("period_start_raw") or period.get("period_start") or ""),
        "period_end": str(period.get("period_end_raw") or period.get("period_end") or ""),
        "period_is_running": bool(period.get("is_running")),
        "items": [],
    }
    if not sid:
        return empty
    if not pg_table_exists(engine, "scan_log_errors"):
        return empty
    lim = max(1, min(int(limit), 200))
    try:
        with engine.connect() as conn:
            meta = conn.execute(
                text(
                    """
                    SELECT sp.environment_id, sp.started_at, sp.completed_at
                    FROM scan_parents sp
                    WHERE sp.scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
            env_id = meta[0] if meta and meta[0] is not None else None
            started_at = meta[1] if meta else None
            completed_at = meta[2] if meta else None
            if env_id is None:
                wr = conn.execute(
                    text(
                        """
                        SELECT environment_id
                        FROM workflow_steps
                        WHERE scan_id = :sid AND environment_id IS NOT NULL
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": sid},
                ).fetchone()
                env_id = wr[0] if wr and wr[0] is not None else None

            # scan_parents started_at/completed_at are stored as ISO strings, while
            # scan_log_errors.log_timestamp is VARCHAR stored as "%Y-%m-%d %H:%M:%S"
            # (see ai_log_fetcher). Parse the bounds and re-format to the SAME text shape so
            # the comparison is a correct lexical text-vs-text compare — and so we never do
            # `str + timedelta` (which raised TypeError on the VARCHAR value).
            _LOG_TS_FMT = "%Y-%m-%d %H:%M:%S"
            started_dt = _parse_scan_period_dt(started_at)
            params: Dict[str, Any] = {"sid": sid, "lim": lim}
            if env_id is not None and started_dt is not None:
                params.update({"eid": env_id, "started": started_dt.strftime(_LOG_TS_FMT)})
                completed_dt = _parse_scan_period_dt(completed_at)
                if completed_dt is not None:
                    params["completed_cap"] = (completed_dt + timedelta(minutes=5)).strftime(_LOG_TS_FMT)
                else:
                    params["completed_cap"] = datetime.utcnow().strftime(_LOG_TS_FMT)
                where_sql = "environment_id = :eid AND log_timestamp >= :started AND log_timestamp <= :completed_cap"
            else:
                return empty

            rows = conn.execute(
                text(
                    f"""
                    SELECT log_timestamp, log_source, scan_id, error_context, ai_analysis, is_resolved,
                           COALESCE(log_category, 'unknown') AS log_category
                    FROM scan_log_errors
                    WHERE {where_sql}
                    ORDER BY log_timestamp DESC, id DESC
                    LIMIT :lim
                    """
                ),
                params,
            ).fetchall()
        items = []
        for r in rows:
            items.append(
                {
                    "log_timestamp": r[0],
                    "log_source": r[1],
                    "scan_id": r[2],
                    "error_context": (str(r[3])[:8000] if r[3] else ""),
                    "ai_analysis": (str(r[4])[:4000] if r[4] else ""),
                    "is_resolved": bool(r[5]) if r[5] is not None else False,
                    "log_category": r[6] if r[6] is not None else "unknown",
                }
            )
        empty["items"] = items
        if started_at is not None:
            empty["window_start"] = str(started_at)
        if completed_at is not None:
            empty["window_end"] = str(completed_at)
        return empty
    except SQLAlchemyError:
        return empty
