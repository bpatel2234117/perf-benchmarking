"""
BigID scanner group + per-scanner services-status capture into MySQL.

Used by:
- dashboard (Scan Progress → Scanner Performance Metrics, manual Sync)
- tracker (one snapshot per environment per cycle while running scans are tracked)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy import text

from bigid_auth import Get_auth_token

SCANNER_GROUPS_API_PATH = (
    os.getenv("BIGID_SCANNER_GROUPS_API_PATH", "/api/v1/scanner-groups").strip() or "/api/v1/scanner-groups"
)
SERVICES_STATUS_API_PATH = (
    os.getenv("BIGID_SERVICES_STATUS_API_PATH", "/api/v1/health-checks/services-status").strip()
    or "/api/v1/health-checks/services-status"
)
# If set, read CPU % only from this JSON key on each scanner object (e.g. when BigID uses a custom name).
SCANNER_CPU_JSON_KEY = os.getenv("BIGID_SCANNER_CPU_JSON_KEY", "").strip()
# When no native cpu* field exists, derive the same value BigID Scanner UI shows:
#   cpu_display ≈ float(systemLoadAverage) * 100 / float(availableProcessors)
# Set to 0/false to store NULL cpu_pct unless the API sends an explicit field.
SCANNER_CPU_DERIVE_FROM_LOAD = os.getenv("BIGID_SCANNER_CPU_DERIVE_FROM_LOAD", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _api_url(base_url: str, path: str) -> str:
    p = str(path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    # HAR/curl sometimes has /api/v1//scanner-groups — collapse duplicate slashes in path only
    while "//" in p:
        p = p.replace("//", "/")
    return f"{str(base_url or '').strip().rstrip('/')}{p}"


def ensure_scanner_performance_tables(engine) -> None:
    if not engine:
        return
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS scanner_group_performance (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    captured_at_utc DATETIME NOT NULL,
                    group_name VARCHAR(255) NULL,
                    running_scanners INT DEFAULT 0,
                    running_threads INT DEFAULT 0,
                    max_threads INT DEFAULT 0,
                    scanner_threads INT DEFAULT 0,
                    physical_memory_mb BIGINT DEFAULT 0,
                    free_memory_mb BIGINT DEFAULT 0,
                    avg_scan_speed FLOAT DEFAULT 0,
                    group_scanner_ids_json TEXT NULL,
                    update_date_bigid DATETIME NULL,
                    INDEX idx_env_time_group (environment_id, captured_at_utc)
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS scanner_individual_performance (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    captured_at_utc DATETIME NOT NULL,
                    scanner_id VARCHAR(255) NULL,
                    name VARCHAR(255) NULL,
                    status VARCHAR(64) NULL,
                    group_name VARCHAR(255) NULL,
                    running_threads INT DEFAULT 0,
                    total_num_of_threads INT DEFAULT 0,
                    system_load_average FLOAT DEFAULT 0,
                    available_processors INT NULL,
                    physical_memory_mb BIGINT DEFAULT 0,
                    free_memory_mb BIGINT DEFAULT 0,
                    scanner_threads INT DEFAULT 0,
                    cpu_pct DOUBLE NULL,
                    update_date_bigid DATETIME NULL,
                    INDEX idx_env_time_scanner (environment_id, captured_at_utc, scanner_id)
                )
            """)
            )
            # Existing DBs: add CPU % column (BigID UI "CPU (%)" can exceed 100% on multi-core)
            for alter_stmt in (
                "ALTER TABLE scanner_individual_performance ADD COLUMN cpu_pct DOUBLE NULL",
                "ALTER TABLE scanner_group_performance ADD COLUMN group_scanner_ids_json TEXT NULL",
                "ALTER TABLE scanner_individual_performance ADD COLUMN available_processors INT NULL",
            ):
                try:
                    conn.execute(text(alter_stmt))
                    conn.commit()
                except Exception:
                    pass
            conn.commit()
    except Exception:
        pass


def _coerce_cpu_pct_value(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v):  # NaN
            return None
        return float(v)
    s = str(v).strip().replace("%", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _extract_cpu_pct_from_service_item(item: Dict[str, Any]) -> Optional[float]:
    """Best-effort: BigID services-status field names vary by release."""
    if not isinstance(item, dict):
        return None
    if SCANNER_CPU_JSON_KEY and SCANNER_CPU_JSON_KEY in item:
        v = _coerce_cpu_pct_value(item.get(SCANNER_CPU_JSON_KEY))
        if v is not None:
            return v
    keys_priority = (
        "cpuPercentage",
        "cpuPercent",
        "cpuUsage",
        "cpuUsagePercentage",
        "averageCpuUsage",
        "avgCpuUsage",
        "processorsAverageCpuUsage",
        "processorsCpuUsage",
        "systemCpuUsage",
        "systemCpu",
        "totalCpuUsage",
        "hostCpuUsage",
        "cpu",
    )
    for k in keys_priority:
        if k in item:
            out = _coerce_cpu_pct_value(item.get(k))
            if out is not None:
                return out
    for nest in ("metrics", "resourceUsage", "resources", "hostMetrics", "systemMetrics"):
        sub = item.get(nest)
        if isinstance(sub, dict):
            for k in keys_priority:
                if k in sub:
                    out = _coerce_cpu_pct_value(sub.get(k))
                    if out is not None:
                        return out
    for k, v in item.items():
        lk = str(k).lower()
        if lk in {"cpu", "cpupercent", "cpuusage", "cpupercentage"}:
            out = _coerce_cpu_pct_value(v)
            if out is not None:
                return out
        if lk.endswith("cpuusage") or lk.endswith("cpupercentage"):
            out = _coerce_cpu_pct_value(v)
            if out is not None:
                return out
    return None


def _derive_cpu_pct_from_load_and_processors(item: Dict[str, Any]) -> Optional[float]:
    """
    Match BigID Scanner Management "CPU (%)" when services-status only exposes
    systemLoadAverage + availableProcessors (e.g. release-249.x).

    This is (load / logical CPUs) * 100, which can exceed 100% when load average
    is high — same behavior as the product UI.
    """
    if not isinstance(item, dict):
        return None
    try:
        la = _coerce_cpu_pct_value(item.get("systemLoadAverage"))
        ap = _coerce_cpu_pct_value(item.get("availableProcessors"))
        if la is None or ap is None:
            return None
        if ap <= 0:
            ap = 1.0
        return la * 100.0 / ap
    except Exception:
        return None


def _resolve_cpu_pct_for_scanner_item(item: Dict[str, Any]) -> Optional[float]:
    raw = _extract_cpu_pct_from_service_item(item)
    if raw is not None:
        return raw
    if SCANNER_CPU_DERIVE_FROM_LOAD:
        return _derive_cpu_pct_from_load_and_processors(item)
    return None


def _parse_iso_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        clean = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(clean).replace(tzinfo=None)
    except Exception:
        return None


def _positive_int_from_api(val: Any) -> Optional[int]:
    """Parse availableProcessors-style fields; None if missing or invalid."""
    if val is None or val is False:
        return None
    try:
        x = float(str(val).strip().replace(",", ""))
        if x <= 0 or x != x:  # NaN
            return None
        return int(x)
    except Exception:
        return None


def _strip_mb_scanner(val: Any) -> int:
    if val is None:
        return 0
    try:
        if not val:
            return 0
        s = str(val).upper().replace("MB", "").replace(" ", "").strip()
        return int(float(s))
    except Exception:
        return 0


def get_scanner_group_performance(base_url: str, token: str) -> List[Dict[str, Any]]:
    """
    GET /api/v1/scanner-groups — canonical group metrics.

    - runningScanners + scannersIDs: same 5 pods BigID counts as "running" for the group.
    - maxThreads: large pool cap (e.g. 3235); scannerThreads is often the smaller UI cap (e.g. 50).
    - avgScanSpeed: in recent releases usually bytes/s (24006 B/s ≈ 23.4 KiB/s in the product UI).
    """
    if not base_url or not token:
        return []
    url = _api_url(base_url, SCANNER_GROUPS_API_PATH)
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        groups: List[Dict[str, Any]] = []
        for item in data.get("data", []):
            for sg in item.get("scannerGroups", []):
                ids = sg.get("scannersIDs") or sg.get("scannersIds") or []
                if not isinstance(ids, list):
                    ids = []
                groups.append(
                    {
                        "name": sg.get("name"),
                        "runningScanners": sg.get("runningScanners", 0),
                        "runningThreads": sg.get("runningThreads", 0),
                        "maxThreads": sg.get("maxThreads", 0),
                        "scannerThreads": sg.get("scannerThreads", 0),
                        "avgScanSpeed": sg.get("avgScanSpeed", 0),
                        "physicalMemory": sg.get("physicalMemory"),
                        "freeMemory": sg.get("freeMemory"),
                        "updateDate": sg.get("updateDate"),
                        "scannersIDs": ids,
                    }
                )
        return groups
    except Exception:
        return []


def get_individual_scanner_performance(base_url: str, token: str) -> List[Dict[str, Any]]:
    if not base_url or not token:
        return []
    url = _api_url(base_url, SERVICES_STATUS_API_PATH)
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        scanners: List[Dict[str, Any]] = []
        for item in data:
            if item.get("type") == "scanner":
                scanners.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "group": item.get("groupName") or item.get("group"),
                        "runningThreads": item.get("runningThreads", 0),
                        "totalNumOfThreads": item.get("totalNumOfThreads", 0),
                        "systemLoadAverage": item.get("systemLoadAverage", 0),
                        "availableProcessors": _positive_int_from_api(item.get("availableProcessors")),
                        "cpu_pct": _resolve_cpu_pct_for_scanner_item(item),
                        "physicalMemory": item.get("physicalMemory"),
                        "freeMemory": item.get("freeMemory"),
                        "scannerThreads": item.get("scannerThreads", 0),
                        "updateDate": item.get("updateDate"),
                    }
                )
        return scanners
    except Exception:
        return []


def upsert_scanner_performance(
    db_engine,
    env_id: Any,
    group_metrics: List[Dict[str, Any]],
    individual_metrics: List[Dict[str, Any]],
    captured_at: Optional[datetime] = None,
) -> None:
    if not db_engine or env_id is None:
        return
    now = captured_at if captured_at else datetime.utcnow()
    now = now.replace(microsecond=0)
    try:
        with db_engine.connect() as conn:
            for g in group_metrics:
                update_date = _parse_iso_dt(str(g.get("updateDate") or ""))
                _ids_json = None
                try:
                    _raw_ids = g.get("scannersIDs") or []
                    if isinstance(_raw_ids, list) and _raw_ids:
                        _ids_json = json.dumps([str(x) for x in _raw_ids])
                except Exception:
                    _ids_json = None
                conn.execute(
                    text("""
                    INSERT IGNORE INTO scanner_group_performance (
                        environment_id, captured_at_utc, group_name,
                        running_scanners, running_threads, max_threads, scanner_threads,
                        physical_memory_mb, free_memory_mb, avg_scan_speed, group_scanner_ids_json, update_date_bigid
                    ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ids, :ud)
                """),
                    {
                        "eid": env_id,
                        "cau": now,
                        "gn": str(g.get("name") or ""),
                        "rs": int(g.get("runningScanners") or 0),
                        "rt": int(g.get("runningThreads") or 0),
                        "mt": int(g.get("maxThreads") or 0),
                        "st": int(g.get("scannerThreads") or 0),
                        "pm": _strip_mb_scanner(g.get("physicalMemory")),
                        "fm": _strip_mb_scanner(g.get("freeMemory")),
                        "as": float(g.get("avgScanSpeed") or 0),
                        "ids": _ids_json,
                        "ud": update_date,
                    },
                )
            for i in individual_metrics:
                update_date = _parse_iso_dt(str(i.get("updateDate") or ""))
                sid = str(i.get("id") or "")
                s_name = str(i.get("name") or "")
                display_name = f"{s_name} [{sid[:4]}]" if sid else s_name
                conn.execute(
                    text("""
                    INSERT IGNORE INTO scanner_individual_performance (
                        environment_id, captured_at_utc, scanner_id, name, status, group_name,
                        running_threads, total_num_of_threads, system_load_average, available_processors,
                        physical_memory_mb, free_memory_mb, scanner_threads, cpu_pct, update_date_bigid
                    ) VALUES (
                        :eid, :cau, :sid, :name, :status, :gn,
                        :rt, :tt, :la, :ap, :pm, :fm, :st, :cpu, :ud
                    )
                """),
                    {
                        "eid": env_id,
                        "cau": now,
                        "sid": sid,
                        "name": display_name,
                        "status": str(i.get("status") or ""),
                        "gn": str(i.get("group") or ""),
                        "rt": int(i.get("runningThreads") or 0),
                        "tt": int(i.get("totalNumOfThreads") or 0),
                        "la": float(i.get("systemLoadAverage") or 0),
                        "ap": i.get("availableProcessors"),
                        "pm": _strip_mb_scanner(i.get("physicalMemory")),
                        "fm": _strip_mb_scanner(i.get("freeMemory")),
                        "st": int(i.get("scannerThreads") or 0),
                        "cpu": i.get("cpu_pct"),
                        "ud": update_date,
                    },
                )
            conn.commit()
    except Exception:
        pass


def capture_scanner_performance_for_env(
    db_engine,
    env_row: Dict[str, Any],
    captured_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    if not env_row or not db_engine:
        return {"ok": False}
    eid = env_row.get("id")
    base_url = env_row.get("base_url")
    token = None
    try:
        token = Get_auth_token(env_row)
    except Exception:
        pass
    if not token:
        return {"ok": False}
    try:
        g = get_scanner_group_performance(str(base_url or ""), token)
        i = get_individual_scanner_performance(str(base_url or ""), token)
        if g or i:
            upsert_scanner_performance(db_engine, eid, g, i, captured_at=captured_at)
            return {"ok": True, "groups": len(g), "individuals": len(i)}
    except Exception:
        pass
    return {"ok": False}
