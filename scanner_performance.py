"""
BigID scanner group + per-scanner services-status capture.

Policy: tracker calls capture only while at least one parent scan is running on the env.
Scan Progress charts read samples within that scan's time window only (not outside scan period).

Used by:
- Scan Progress (scanner performance metrics)
- tracker (20-minute buckets during active scans)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy import text

from bigid_auth import Get_auth_token

try:
    from pg_compat import is_postgresql
except ImportError:
    from v2_api.pg_compat import is_postgresql  # type: ignore

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
    # Dialect-aware DDL. On Postgres, MySQL-isms (INT AUTO_INCREMENT, DATETIME, DOUBLE,
    # inline INDEX ...) all raise, and because this whole block is wrapped in a silent
    # try/except the tables would never get created — which 500s the Scanner
    # Infrastructure page ("Failed to load scanner metrics") on relation-not-found.
    pg = is_postgresql(engine)
    pk = "SERIAL PRIMARY KEY" if pg else "INT AUTO_INCREMENT PRIMARY KEY"
    dt = "TIMESTAMP" if pg else "DATETIME"
    dbl = "DOUBLE PRECISION" if pg else "DOUBLE"
    try:
        with engine.connect() as conn:
            conn.execute(
                text(f"""
                CREATE TABLE IF NOT EXISTS scanner_group_performance (
                    id {pk},
                    environment_id INT NOT NULL,
                    captured_at_utc {dt} NOT NULL,
                    group_name VARCHAR(255) NULL,
                    running_scanners INT DEFAULT 0,
                    running_threads INT DEFAULT 0,
                    max_threads INT DEFAULT 0,
                    scanner_threads INT DEFAULT 0,
                    physical_memory_mb BIGINT DEFAULT 0,
                    free_memory_mb BIGINT DEFAULT 0,
                    avg_scan_speed FLOAT DEFAULT 0,
                    group_scanner_ids_json TEXT NULL,
                    update_date_bigid {dt} NULL
                    {'' if pg else ', INDEX idx_env_time_group (environment_id, captured_at_utc)'}
                )
            """)
            )
            conn.execute(
                text(f"""
                CREATE TABLE IF NOT EXISTS scanner_individual_performance (
                    id {pk},
                    environment_id INT NOT NULL,
                    captured_at_utc {dt} NOT NULL,
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
                    cpu_pct {dbl} NULL,
                    update_date_bigid {dt} NULL
                    {'' if pg else ', INDEX idx_env_time_scanner (environment_id, captured_at_utc, scanner_id)'}
                )
            """)
            )
            # Postgres: inline INDEX isn't supported, so create indexes separately.
            if pg:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_env_time_group "
                    "ON scanner_group_performance (environment_id, captured_at_utc)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_env_time_scanner "
                    "ON scanner_individual_performance (environment_id, captured_at_utc, scanner_id)"
                ))
            # Existing DBs: add columns that predate the current schema. Postgres supports
            # ADD COLUMN IF NOT EXISTS; MySQL does not, so those raise and are ignored.
            col_if = "IF NOT EXISTS " if pg else ""
            for alter_stmt in (
                f"ALTER TABLE scanner_individual_performance ADD COLUMN {col_if}cpu_pct {dbl} NULL",
                f"ALTER TABLE scanner_group_performance ADD COLUMN {col_if}group_scanner_ids_json TEXT NULL",
                f"ALTER TABLE scanner_individual_performance ADD COLUMN {col_if}available_processors INT NULL",
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


_INDIVIDUAL_COLUMNS_CACHE: Dict[int, set] = {}


def _individual_table_columns(engine) -> set:
    """Actual columns of scanner_individual_performance (cached per engine).

    Deployments differ: some tables use scanner_name/max_threads/scan_speed,
    others use name/total_num_of_threads. We introspect so the INSERT only ever
    targets columns that exist (a hardcoded list silently breaks pod capture).
    """
    key = id(engine)
    cached = _INDIVIDUAL_COLUMNS_CACHE.get(key)
    if cached is not None:
        return cached
    cols: set = set()
    try:
        with engine.connect() as conn:
            if is_postgresql(engine):
                rows = conn.execute(
                    text(
                        "SELECT lower(column_name) FROM information_schema.columns "
                        "WHERE lower(table_name) = 'scanner_individual_performance'"
                    )
                ).fetchall()
            else:
                rows = conn.execute(text("SHOW COLUMNS FROM scanner_individual_performance")).fetchall()
        cols = {str(r[0]).lower() for r in rows}
    except Exception:
        cols = set()
    _INDIVIDUAL_COLUMNS_CACHE[key] = cols
    return cols


def build_individual_insert(engine, values: Dict[str, Any]):
    """Build an INSERT for scanner_individual_performance using only existing columns.

    `values` is a normalized dict keyed by logical field names. Returns (sql, params)
    or (None, None) when the target table cannot be resolved.
    """
    existing = _individual_table_columns(engine)
    if not existing:
        return None, None

    # logical field -> (candidate column names in priority order)
    field_to_columns = {
        "environment_id": ("environment_id",),
        "captured_at_utc": ("captured_at_utc",),
        "scanner_id": ("scanner_id",),
        "name": ("scanner_name", "name"),
        "status": ("status",),
        "group_name": ("group_name",),
        "running_threads": ("running_threads",),
        "max_threads": ("max_threads", "total_num_of_threads"),
        "system_load_average": ("system_load_average",),
        "available_processors": ("available_processors",),
        "physical_memory_mb": ("physical_memory_mb",),
        "free_memory_mb": ("free_memory_mb",),
        "scanner_threads": ("scanner_threads",),
        "scan_speed": ("scan_speed",),
        "cpu_pct": ("cpu_pct",),
        "update_date_bigid": ("update_date_bigid",),
    }

    columns: List[str] = []
    placeholders: List[str] = []
    params: Dict[str, Any] = {}
    for field, candidates in field_to_columns.items():
        if field not in values:
            continue
        col = next((c for c in candidates if c in existing), None)
        if not col:
            continue
        columns.append(col)
        placeholders.append(f":{field}")
        params[field] = values[field]

    if not columns:
        return None, None

    ignore = "" if is_postgresql(engine) else "IGNORE "
    sql = (
        f"INSERT {ignore}INTO scanner_individual_performance ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(placeholders)
        + ")"
    )
    return text(sql), params


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


def _normalize_services_status_list(data: Any) -> List[Dict[str, Any]]:
    """BigID may return a bare list or {data|services|items: [...]}."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "services", "items", "results", "payload"):
            chunk = data.get(key)
            if isinstance(chunk, list):
                return [x for x in chunk if isinstance(x, dict)]
            if isinstance(chunk, dict):
                for nested in ("services", "items", "data"):
                    inner = chunk.get(nested)
                    if isinstance(inner, list):
                        return [x for x in inner if isinstance(x, dict)]
    return []


def _is_scanner_service_item(item: Dict[str, Any]) -> bool:
    kind = str(item.get("type") or item.get("serviceType") or item.get("role") or "").strip().lower()
    if kind == "scanner":
        return True
    name = str(item.get("name") or item.get("serviceName") or "").strip().lower()
    return name.startswith("scanner") or "scanner-" in name


def individuals_from_scanner_groups(group_metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """When services-status is empty, use scanner-groups scannersIDs so pod charts can render."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for g in group_metrics or []:
        gn = str(g.get("name") or "")
        n_scanners = max(1, int(g.get("runningScanners") or 0))
        rt = int(g.get("runningThreads") or 0)
        # Distribute threads evenly, giving the floor-division remainder to the first
        # `rem` scanners so the per-scanner threads sum back to the group's runningThreads
        # (plain `rt // n_scanners` silently dropped the remainder).
        base_rt = rt // n_scanners if rt else 0
        rem_rt = rt % n_scanners if rt else 0
        emitted_in_group = 0
        ids = g.get("scannersIDs") or g.get("scannersIds") or []
        if not isinstance(ids, list):
            ids = []
        for raw_id in ids:
            sid = str(raw_id or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            per_rt = base_rt + (1 if emitted_in_group < rem_rt else 0)
            emitted_in_group += 1
            out.append(
                {
                    "id": sid,
                    "name": sid[:12],
                    "status": "Running",
                    "group": gn,
                    "runningThreads": per_rt,
                    "totalNumOfThreads": int(g.get("maxThreads") or 0),
                    "systemLoadAverage": 0,
                    "availableProcessors": 1,
                    "cpu_pct": None,
                    "physicalMemory": g.get("physicalMemory"),
                    "freeMemory": g.get("freeMemory"),
                    "scannerThreads": int(g.get("scannerThreads") or 0),
                    "updateDate": g.get("updateDate"),
                }
            )
    return out


def get_individual_scanner_performance(
    base_url: str, token: str, group_metrics: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    if not base_url or not token:
        return []
    url = _api_url(base_url, SERVICES_STATUS_API_PATH)
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code != 200:
            return individuals_from_scanner_groups(group_metrics or [])
        items = _normalize_services_status_list(resp.json())
        scanners: List[Dict[str, Any]] = []
        for item in items:
            if not _is_scanner_service_item(item):
                continue
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
        if scanners:
            return scanners
        return individuals_from_scanner_groups(group_metrics or [])
    except Exception:
        return individuals_from_scanner_groups(group_metrics or [])


def upsert_scanner_performance(
    db_engine,
    env_id: Any,
    group_metrics: List[Dict[str, Any]],
    individual_metrics: List[Dict[str, Any]],
    captured_at: Optional[datetime] = None,
) -> None:
    if not db_engine or env_id is None:
        return
    if not individual_metrics and group_metrics:
        individual_metrics = individuals_from_scanner_groups(group_metrics)
    now = captured_at if captured_at else datetime.utcnow()
    # Truncate to the minute — deduplicates multiple calls within the same minute
    now = now.replace(second=0, microsecond=0)
    pg = is_postgresql(db_engine)
    if pg:
        group_sql = """
            INSERT INTO scanner_group_performance (
                environment_id, captured_at_utc, group_name,
                running_scanners, running_threads, max_threads, scanner_threads,
                physical_memory_mb, free_memory_mb, avg_scan_speed, group_scanner_ids_json, update_date_bigid
            ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ids, :ud)
        """
    else:
        group_sql = """
            INSERT IGNORE INTO scanner_group_performance (
                environment_id, captured_at_utc, group_name,
                running_scanners, running_threads, max_threads, scanner_threads,
                physical_memory_mb, free_memory_mb, avg_scan_speed, group_scanner_ids_json, update_date_bigid
            ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ids, :ud)
        """
    try:
        with db_engine.connect() as conn:
            for g in group_metrics:
                update_date = _parse_iso_dt(str(g.get("updateDate") or ""))
                _ids_json = None
                try:
                    _raw_ids = g.get("scannersIDs") or g.get("scannersIds") or []
                    if isinstance(_raw_ids, list) and _raw_ids:
                        _ids_json = json.dumps([str(x) for x in _raw_ids])
                except Exception:
                    _ids_json = None
                conn.execute(
                    text(group_sql),
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
                ind_sql, ind_params = build_individual_insert(
                    db_engine,
                    {
                        "environment_id": env_id,
                        "captured_at_utc": now,
                        "scanner_id": sid,
                        "name": display_name,
                        "status": str(i.get("status") or "Running"),
                        "group_name": str(i.get("group") or i.get("groupName") or ""),
                        "running_threads": int(i.get("runningThreads") or 0),
                        "max_threads": int(i.get("totalNumOfThreads") or 0),
                        "system_load_average": float(i.get("systemLoadAverage") or 0),
                        "available_processors": i.get("availableProcessors"),
                        "physical_memory_mb": _strip_mb_scanner(i.get("physicalMemory")),
                        "free_memory_mb": _strip_mb_scanner(i.get("freeMemory")),
                        "scanner_threads": int(i.get("scannerThreads") or 0),
                        "scan_speed": 0,
                        "cpu_pct": i.get("cpu_pct"),
                        "update_date_bigid": update_date,
                    },
                )
                if ind_sql is not None:
                    conn.execute(ind_sql, ind_params)
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
        i = get_individual_scanner_performance(str(base_url or ""), token, group_metrics=g)
        if g or i:
            upsert_scanner_performance(db_engine, eid, g, i, captured_at=captured_at)
            return {"ok": True, "groups": len(g), "individuals": len(i)}
    except Exception:
        pass
    return {"ok": False}


# ---------------------------------------------------------------------------
# Service status history (every BigID service, not only scanners)
# One row per service per capture slot. Read back as "last N statuses" per service.
# ---------------------------------------------------------------------------


def ensure_service_status_table(engine) -> None:
    if not engine:
        return
    pg = is_postgresql(engine)
    pk = "SERIAL PRIMARY KEY" if pg else "INT AUTO_INCREMENT PRIMARY KEY"
    dt = "TIMESTAMP" if pg else "DATETIME"
    dbl = "DOUBLE PRECISION" if pg else "DOUBLE"
    try:
        with engine.connect() as conn:
            conn.execute(
                text(f"""
                CREATE TABLE IF NOT EXISTS service_status_samples (
                    id {pk},
                    environment_id INT NOT NULL,
                    captured_at_utc {dt} NOT NULL,
                    service_key VARCHAR(255) NOT NULL,
                    name VARCHAR(255) NULL,
                    service_type VARCHAR(64) NULL,
                    group_name VARCHAR(255) NULL,
                    status VARCHAR(64) NULL,
                    message VARCHAR(512) NULL,
                    running_threads INT DEFAULT 0,
                    total_threads INT DEFAULT 0,
                    cpu_pct {dbl} NULL,
                    update_date_bigid {dt} NULL,
                    created_at_bigid {dt} NULL
                )
            """)
            )
            # Tables created before createDate was stored need the column added in place.
            if pg:
                conn.execute(text(
                    "ALTER TABLE service_status_samples ADD COLUMN IF NOT EXISTS created_at_bigid TIMESTAMP NULL"
                ))
            else:
                try:
                    conn.execute(text("ALTER TABLE service_status_samples ADD COLUMN created_at_bigid DATETIME NULL"))
                except Exception:
                    pass  # column already exists
            if pg:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_svc_env_time "
                    "ON service_status_samples (environment_id, captured_at_utc)"
                ))
            else:
                try:
                    conn.execute(text(
                        "CREATE INDEX idx_svc_env_time ON service_status_samples (environment_id, captured_at_utc)"
                    ))
                except Exception:
                    pass  # index already exists
            conn.commit()
    except Exception:
        pass


def _service_items_from_payload(data: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in _normalize_services_status_list(data):
        sid = str(it.get("id") or it.get("name") or "").strip()
        if not sid:
            continue
        out.append(
            {
                "service_key": sid,
                "name": str(it.get("name") or it.get("serviceName") or sid)[:255],
                "service_type": str(it.get("type") or it.get("serviceType") or it.get("role") or "")[:64],
                "group_name": str(it.get("groupName") or it.get("group") or "")[:255],
                "status": str(it.get("status") or "")[:64],
                "message": str(it.get("message") or "")[:512],
                "running_threads": int(it.get("runningThreads") or 0),
                "total_threads": int(it.get("totalNumOfThreads") or 0),
                "cpu_pct": _resolve_cpu_pct_for_scanner_item(it),
                "update_date_bigid": _parse_iso_dt(str(it.get("updateDate") or "")),
                "created_at_bigid": _parse_iso_dt(str(it.get("createDate") or "")),
            }
        )
    return out


def fetch_services_status_result(base_url: str, token: str) -> Dict[str, Any]:
    """
    One capture attempt. outcome is 'ok' (items found), 'empty' (BigID answered with no
    services) or 'error' (no token, HTTP error, timeout, bad payload). Never raises.
    """
    out: Dict[str, Any] = {"outcome": "error", "items": [], "error": ""}
    if not base_url or not token:
        out["error"] = "missing base_url or auth token"
        return out
    url = _api_url(base_url, SERVICES_STATUS_API_PATH)
    try:
        resp = requests.get(url, headers={"authorization": token}, verify=False, timeout=15)
        if resp.status_code != 200:
            out["error"] = f"services-status HTTP {resp.status_code}"
            return out
        items = _service_items_from_payload(resp.json())
    except Exception as e:
        out["error"] = str(e)[:500]
        return out
    if not items:
        out["outcome"] = "empty"
        out["error"] = "BigID returned no services"
        return out
    out["outcome"] = "ok"
    out["items"] = items
    return out


def fetch_services_status_items(base_url: str, token: str) -> List[Dict[str, Any]]:
    """Items only, for callers that don't need the outcome."""
    return fetch_services_status_result(base_url, token)["items"]


def ensure_service_capture_log(engine) -> None:
    """One row per capture slot per environment, whatever the outcome. Gaps become visible."""
    if not engine:
        return
    pg = is_postgresql(engine)
    pk = "SERIAL PRIMARY KEY" if pg else "INT AUTO_INCREMENT PRIMARY KEY"
    dt = "TIMESTAMP" if pg else "DATETIME"
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS service_capture_log (
                    id {pk},
                    environment_id INT NOT NULL,
                    captured_at_utc {dt} NOT NULL,
                    outcome VARCHAR(16) NOT NULL,
                    item_count INT DEFAULT 0,
                    error VARCHAR(512) NULL
                )
            """))
            if pg:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_svc_cap_env_time "
                    "ON service_capture_log (environment_id, captured_at_utc)"
                ))
            conn.commit()
    except Exception:
        pass


def record_service_capture(engine, env_id: Any, captured_at: datetime, outcome: str, item_count: int, error: str) -> None:
    if not engine or env_id is None:
        return
    ensure_service_capture_log(engine)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO service_capture_log (environment_id, captured_at_utc, outcome, item_count, error)
                    VALUES (:eid, :cau, :oc, :ic, :er)
                """),
                {
                    "eid": env_id,
                    "cau": captured_at.replace(second=0, microsecond=0),
                    "oc": outcome,
                    "ic": int(item_count or 0),
                    "er": (error or "")[:512] or None,
                },
            )
            conn.commit()
    except Exception:
        pass


def upsert_service_status_samples(db_engine, env_id: Any, items: List[Dict[str, Any]], captured_at: datetime) -> None:
    if not db_engine or env_id is None or not items:
        return
    ensure_service_status_table(db_engine)
    cau = captured_at.replace(second=0, microsecond=0)
    sql = text("""
        INSERT INTO service_status_samples (
            environment_id, captured_at_utc, service_key, name, service_type, group_name,
            status, message, running_threads, total_threads, cpu_pct, update_date_bigid, created_at_bigid
        ) VALUES (:eid, :cau, :sk, :nm, :st, :gn, :ss, :msg, :rt, :tt, :cpu, :ud, :cb)
    """)
    try:
        with db_engine.connect() as conn:
            for it in items:
                conn.execute(sql, {
                    "eid": env_id, "cau": cau, "sk": it["service_key"], "nm": it["name"],
                    "st": it["service_type"], "gn": it["group_name"], "ss": it["status"],
                    "msg": it["message"], "rt": it["running_threads"], "tt": it["total_threads"],
                    "cpu": it["cpu_pct"], "ud": it["update_date_bigid"], "cb": it["created_at_bigid"],
                })
            conn.commit()
    except Exception:
        pass
