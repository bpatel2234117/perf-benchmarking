"""
Background scan progress tracker (no browser required).

Runs forever (default every 10 minutes):
- pulls "running" parent scans from each active environment
- fetches scan insights for each running scan
- writes a snapshot row into MySQL table `scan_progress`

Usage:
  python3 tracker.py
  python3 tracker.py --interval-seconds 600
  python3 tracker.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from sqlalchemy import create_engine, text
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_config import DB_CONNECTION_STRING
from bigid_auth import Get_auth_token


PARENT_SCANS_PATH = os.getenv("BIGID_PARENT_SCANS_API_PATH", "/api/v1/scans/parent-scans").strip() or "/api/v1/scans/parent-scans"
INSIGHTS_PATH = os.getenv("BIGID_PARENT_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/parent-scans").strip() or "/api/v1/scans/insights/parent-scans"
ABOUT_PATH = os.getenv("BIGID_ABOUT_API_PATH", "/api/v1/about").strip() or "/api/v1/about"
FILTER_INITIATED_SOURCE = str(os.getenv("BIGID_PARENT_SCANS_FILTER_INITIATED_SOURCE", "0") or "0").strip() in {"1", "true", "yes", "y"}
TRACKER_NAME = os.getenv("SCAN_TRACKER_NAME", "default").strip() or "default"
DEFAULT_CLASSIFIERS_API_PATH = (
    "/api/v1/all-classifications?skip=0&limit=600&requireTotalCount=true&"
    "filter=%5B%7B%22field%22%3A%22enabled%22%2C%22value%22%3A%5Btrue%2Cnull%5D%2C%22operator%22%3A%22in%22%7D%2C"
    "%7B%22field%22%3A%22type%22%2C%22value%22%3A%5B%22data%22%2C%22columnName%22%5D%2C%22operator%22%3A%22in%22%7D%5D"
)
CLASSIFIERS_API_PATH = os.getenv("BIGID_CLASSIFIERS_API_PATH", DEFAULT_CLASSIFIERS_API_PATH).strip() or DEFAULT_CLASSIFIERS_API_PATH


# Include non-terminal transitional states so we keep tracking them.
# BigID can show "Stop Requested" for a long time before becoming terminal.
RUNNING_STATES = [
    "Started",
    "Awaiting",
    "In Progress",
    "InProgress",
    "Running",
    "Stop Requested",
    "StopRequested",
    "Stopping",
]
TERMINAL_STATES = ["Failed", "CompletedWithFailures", "Completed", "Stopped", "Aborted", "Cancelled", "Canceled"]


def _compact_state(s: Any) -> str:
    return str(s or "").strip().lower().replace(" ", "")


def _normalize_scan_type_value(raw_type: Any, template_name: Any = None) -> str:
    """
    BigID returns scan "type" inconsistently across endpoints:
    - insights.scanProfile.type: e.g. "dsScan"
    - parent-scans.scan.type: e.g. "full_scan"
    Both can represent a user-visible "Full Scan". Normalize to reduce confusion.
    """
    t = str(raw_type or "").strip()
    ct = _compact_state(t)
    tmpl = str(template_name or "").strip().lower()

    if ct in {"dsscan"}:
        return "full_scan"
    if ct in {"full_scan", "fullscan"}:
        return "full_scan"
    if "data source scan" in tmpl and ct in {"", "dsscan"}:
        return "full_scan"
    return t


def _is_terminal_state(s: Any) -> bool:
    return _compact_state(s) in {_compact_state(x) for x in TERMINAL_STATES}


def _is_running_state(s: Any) -> bool:
    return _compact_state(s) in {_compact_state(x) for x in RUNNING_STATES}


def _state_rank(s: Any) -> int:
    """
    Prefer more-advanced states to avoid regressions like:
    parent-scans says "In Progress" but insights says "Started".
    """
    cs = _compact_state(s)
    if not cs:
        return 0
    if _is_terminal_state(cs):
        return 100
    if _is_running_state(cs):
        # Within running, "inprogress" should win over "started".
        order = {
            _compact_state("In Progress"): 40,
            _compact_state("InProgress"): 40,
            _compact_state("Stop Requested"): 35,
            _compact_state("StopRequested"): 35,
            _compact_state("Stopping"): 34,
            _compact_state("Running"): 30,
            _compact_state("Awaiting"): 20,
            _compact_state("Started"): 10,
        }
        return 50 + int(order.get(cs, 0))
    return 10


def _choose_best_state(parent_state: Any, insight_state: Any) -> str:
    a = str(parent_state or "").strip()
    b = str(insight_state or "").strip()
    if not a:
        return b
    if not b:
        return a
    return a if _state_rank(a) >= _state_rank(b) else b


def _now_utc_naive() -> datetime:
    # Naive UTC datetime suitable for MySQL DATETIME
    return datetime.utcnow()


def normalize_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def build_api_url(base_url: str, api_path: str) -> str:
    base = normalize_base_url(base_url)
    p = str(api_path or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return f"{base}{p}"


def resolve_scan_status_value(scan: Dict[str, Any]) -> str:
    if not isinstance(scan, dict):
        return ""
    progress = scan.get("progress")
    if isinstance(progress, dict):
        state = str(progress.get("state") or "").strip()
        if state:
            return state
    return str(scan.get("state") or scan.get("status") or "").strip()


def ensure_scan_tracking_tables(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tracker_heartbeat (
                name VARCHAR(64) PRIMARY KEY,
                last_started_at_utc DATETIME NULL,
                last_finished_at_utc DATETIME NULL,
                last_status VARCHAR(16) NULL,
                last_message VARCHAR(2000) NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tracker_kv (
                k VARCHAR(200) PRIMARY KEY,
                v VARCHAR(2000) NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_parents (
                id INT AUTO_INCREMENT PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL UNIQUE,
                environment_id INT NULL,
                scan_name VARCHAR(1000) NULL,
                scan_type VARCHAR(120) NULL,
                started_at VARCHAR(80) NULL,
                completed_at VARCHAR(80) NULL,
                template_name VARCHAR(255) NULL,
                scan_status VARCHAR(120) NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_insights (
                id INT AUTO_INCREMENT PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL UNIQUE,
                distinct_classification_attributes BIGINT NULL,
                classifier_attribute_names LONGTEXT NULL,
                data_source_names LONGTEXT NULL,
                objects_with_pi BIGINT NULL,
                objects_scanned BIGINT NULL,
                objects_enumerated BIGINT NULL,
                total_classification BIGINT NULL,
                new_attributes_count BIGINT NULL,
                removed_attributes_count BIGINT NULL,
                total_size_gb FLOAT NULL,
                insight_payload LONGTEXT NULL,
                attributes_payload LONGTEXT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """))
        # Non-breaking schema upgrades (in case table existed with fewer columns)
        for stmt in [
            "ALTER TABLE scan_insights ADD COLUMN new_attributes_count BIGINT NULL",
            "ALTER TABLE scan_insights ADD COLUMN removed_attributes_count BIGINT NULL",
            "ALTER TABLE scan_insights ADD COLUMN total_size_gb FLOAT NULL",
            "ALTER TABLE scan_insights ADD COLUMN insight_payload LONGTEXT NULL",
            "ALTER TABLE scan_insights ADD COLUMN attributes_payload LONGTEXT NULL",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass

        # Tables used by Add New Benchmark "Findings Files" prefill
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_classifier_attributes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(500) NOT NULL UNIQUE
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_insight_attribute_delta_map (
                scan_id VARCHAR(128) NOT NULL,
                attribute_id INT NOT NULL,
                is_new BOOLEAN DEFAULT FALSE,
                is_removed BOOLEAN DEFAULT FALSE,
                finding_count BIGINT NULL,
                attribute_value VARCHAR(500) NULL,
                new_count_value BIGINT NULL,
                removed_count_value BIGINT NULL,
                PRIMARY KEY (scan_id, attribute_id),
                FOREIGN KEY (attribute_id) REFERENCES scan_classifier_attributes(id)
            )
        """))
        for stmt in [
            "ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN finding_count BIGINT NULL",
            "ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN attribute_value VARCHAR(500) NULL",
            "ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN new_count_value BIGINT NULL",
            "ALTER TABLE scan_insight_attribute_delta_map ADD COLUMN removed_count_value BIGINT NULL",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        conn.commit()

        # Auto scan benchmarks table (used by dashboard trend/compare pages).
        # Tracker does NOT create rows here (dashboard does today), but we may update
        # completed scans with computed throughputs (best-effort).
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS auto_scan_benchmarks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL UNIQUE,
                    environment_id INT NULL,
                    build_version VARCHAR(100) NULL,
                    run_date DATE NULL,
                    scenario_type VARCHAR(100) NULL,
                    scan_type VARCHAR(100) NULL,
                    data_source_type VARCHAR(100) NULL,
                    data_type VARCHAR(50) NULL,
                    file_set_type VARCHAR(150) NULL,
                    scan_start_time DATETIME NULL,
                    scan_end_time DATETIME NULL,
                    total_scan_hours FLOAT NULL,
                    total_size_gb FLOAT NULL,
                    total_files BIGINT NULL,
                    total_folders BIGINT NULL,
                    objects_with_pi BIGINT NULL,
                    objects_scanned BIGINT NULL,
                    objects_enumerated BIGINT NULL,
                    total_classification BIGINT NULL,
                    new_attributes BIGINT NULL,
                    removed_attributes BIGINT NULL,
                    classifier_attribute_names LONGTEXT NULL,
                    scan_name VARCHAR(1000) NULL,
                    throughput_cluster_gb_hr FLOAT NULL,
                    throughput_cluster_files_hr FLOAT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """))
            conn.commit()
        except Exception:
            pass

        # Non-breaking schema upgrades: add throughput columns if missing.
        for stmt in [
            "ALTER TABLE auto_scan_benchmarks ADD COLUMN throughput_cluster_gb_hr FLOAT NULL",
            "ALTER TABLE auto_scan_benchmarks ADD COLUMN throughput_cluster_files_hr FLOAT NULL",
            "ALTER TABLE auto_scan_benchmarks ADD COLUMN total_size_gb FLOAT NULL",
            "ALTER TABLE auto_scan_benchmarks ADD COLUMN total_files BIGINT NULL",
            "ALTER TABLE auto_scan_benchmarks ADD COLUMN total_folders BIGINT NULL",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        conn.commit()


def _parse_bigid_dt(value: Any) -> Optional[datetime]:
    """
    Parse BigID timestamps into naive UTC datetimes (MySQL DATETIME friendly).
    Accepts ISO strings with optional 'Z' suffix.
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        # Normalize 'Z' into offset.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # Convert aware -> naive UTC
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def update_auto_scan_benchmarks_throughput(engine, scan_id: str, started_at: Any = None, completed_at: Any = None) -> bool:
    """
    Best-effort: when a scan is terminal, compute throughput and update existing
    auto_scan_benchmarks row for this scan_id.
    Does NOT insert new rows.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        return False
    try:
        start_hint = _parse_bigid_dt(started_at)
        end_hint = _parse_bigid_dt(completed_at)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT
                        scan_start_time,
                        scan_end_time,
                        total_scan_hours,
                        total_size_gb,
                        total_files,
                        throughput_cluster_gb_hr,
                        throughput_cluster_files_hr
                    FROM auto_scan_benchmarks
                    WHERE scan_id = :sid
                    LIMIT 1
                """),
                {"sid": sid},
            ).fetchone()
            if not row:
                return False

            scan_start_time = row[0]
            scan_end_time = row[1]
            total_scan_hours = row[2]
            total_size_gb = row[3]
            total_files = row[4]

            # Fill missing timestamps from hints.
            if scan_start_time is None and start_hint is not None:
                scan_start_time = start_hint
            if scan_end_time is None and end_hint is not None:
                scan_end_time = end_hint

            hours = None
            try:
                hours = float(total_scan_hours) if total_scan_hours is not None else None
            except Exception:
                hours = None
            if hours is None or hours <= 0 or hours < 0.01:
                try:
                    if scan_start_time is not None and scan_end_time is not None and scan_end_time > scan_start_time:
                        hours = (scan_end_time - scan_start_time).total_seconds() / 3600.0
                except Exception:
                    pass

            t_gb = None
            t_files = None
            try:
                if hours and hours > 0:
                    if total_size_gb is not None:
                        t_gb = float(total_size_gb) / float(hours)
                    if total_files is not None:
                        t_files = float(total_files) / float(hours)
            except Exception:
                t_gb = None
                t_files = None

            conn.execute(
                text("""
                    UPDATE auto_scan_benchmarks
                    SET
                        scan_start_time = COALESCE(scan_start_time, :sst),
                        scan_end_time = COALESCE(scan_end_time, :set),
                        total_scan_hours = COALESCE(:hrs, total_scan_hours),
                        throughput_cluster_gb_hr = COALESCE(:tgb, throughput_cluster_gb_hr),
                        throughput_cluster_files_hr = COALESCE(:tfs, throughput_cluster_files_hr),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE scan_id = :sid
                """),
                {"sid": sid, "sst": scan_start_time, "set": scan_end_time, "hrs": hours, "tgb": t_gb, "tfs": t_files},
            )
            conn.commit()
        return True
    except Exception:
        return False


def _hb_update(engine, status: str, message: str = "", started: Optional[datetime] = None, finished: Optional[datetime] = None) -> None:
    """
    Best-effort heartbeat update. Never raises.
    """
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO tracker_heartbeat (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
                    VALUES (:n, :s, :f, :st, :m)
                    ON DUPLICATE KEY UPDATE
                        last_started_at_utc = COALESCE(VALUES(last_started_at_utc), last_started_at_utc),
                        last_finished_at_utc = COALESCE(VALUES(last_finished_at_utc), last_finished_at_utc),
                        last_status = VALUES(last_status),
                        last_message = VALUES(last_message)
                """),
                {
                    "n": TRACKER_NAME[:64],
                    "s": started,
                    "f": finished,
                    "st": str(status or "")[:16] or None,
                    "m": str(message or "")[:2000] or None,
                },
            )
            conn.commit()
    except Exception:
        pass


def _kv_get(engine, key: str) -> str:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT v FROM tracker_kv WHERE k = :k LIMIT 1"), {"k": str(key)}).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _kv_set(engine, key: str, value: str) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO tracker_kv (k, v)
                    VALUES (:k, :v)
                    ON DUPLICATE KEY UPDATE
                        v = VALUES(v),
                        updated_at = CURRENT_TIMESTAMP
                """),
                {"k": str(key), "v": str(value)},
            )
            conn.commit()
    except Exception:
        pass


def ensure_scan_progress_table(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_progress (
                id INT AUTO_INCREMENT PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL,
                scan_name VARCHAR(1000) NULL,
                environment_id INT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                captured_at_utc DATETIME NULL,
                scan_state VARCHAR(64) NULL,
                completed_sub_scans BIGINT DEFAULT 0,
                failed_sub_scans BIGINT DEFAULT 0,
                total_sub_scans BIGINT DEFAULT 0,
                completed_parts BIGINT DEFAULT 0,
                failed_parts BIGINT DEFAULT 0,
                total_parts_scanned BIGINT DEFAULT 0,
                total_classifications BIGINT DEFAULT 0,
                newly_added_classifications BIGINT DEFAULT 0,
                false_removed_classifications BIGINT DEFAULT 0,
                objects_scanned BIGINT DEFAULT 0,
                objects_failed BIGINT DEFAULT 0,
                objects_with_pi BIGINT DEFAULT 0,
                INDEX idx_scan_id (scan_id),
                INDEX idx_timestamp (timestamp),
                INDEX idx_captured_at_utc (captured_at_utc),
                INDEX idx_state (scan_state)
            )
        """))
        # schema upgrades for existing DBs
        for stmt in [
            "ALTER TABLE scan_progress ADD COLUMN captured_at_utc DATETIME NULL",
            "ALTER TABLE scan_progress ADD COLUMN scan_state VARCHAR(64) NULL",
            "ALTER TABLE scan_progress ADD COLUMN false_removed_classifications BIGINT DEFAULT 0",
            "ALTER TABLE scan_progress ADD COLUMN objects_failed BIGINT DEFAULT 0",
            "ALTER TABLE scan_progress ADD COLUMN objects_with_pi BIGINT DEFAULT 0",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass

        # De-dupe existing rows (keep highest id per scan_id + captured_at slot)
        # and enforce uniqueness so we cannot insert duplicates again.
        try:
            conn.execute(text("""
                DELETE sp1 FROM scan_progress sp1
                JOIN scan_progress sp2
                  ON sp1.scan_id = sp2.scan_id
                 AND sp1.captured_at_utc IS NOT NULL
                 AND sp2.captured_at_utc IS NOT NULL
                 AND sp1.captured_at_utc = sp2.captured_at_utc
                 AND sp1.id < sp2.id
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE scan_progress ADD UNIQUE KEY uniq_scan_slot (scan_id, captured_at_utc)"))
            conn.commit()
        except Exception:
            pass
        conn.commit()


def load_active_environments(engine) -> List[Dict[str, Any]]:
    try:
        with engine.connect() as conn:
            df = pd.read_sql(
                text("""
                    SELECT id, env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path, is_active
                    FROM test_environments
                """),
                conn,
            )
    except Exception:
        return []
    if df.empty:
        return []
    if "is_active" in df.columns:
        df = df[df["is_active"].apply(lambda x: bool(x))]
    return df.fillna("").to_dict(orient="records")

def load_running_scan_env_rows(engine) -> List[Dict[str, Any]]:
    """
    DB-driven list of running scans we should definitely track.
    Returns rows with: scan_id, scan_name, environment_id, env_name, base_url
    """
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    sp.scan_id,
                    sp.scan_name,
                    sp.environment_id,
                    te.env_name,
                    te.base_url
                FROM scan_parents sp
                JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.environment_id IS NOT NULL
                  AND te.is_active = 1
                  AND sp.scan_status IN ('Started','Awaiting','In Progress','InProgress','Running')
            """)).fetchall()
        out = []
        for r in rows:
            out.append({
                "scan_id": str(r[0] or "").strip(),
                "scan_name": str(r[1] or "").strip(),
                "environment_id": r[2],
                "env_name": str(r[3] or "").strip(),
                "base_url": str(r[4] or "").strip(),
            })
        return [x for x in out if x.get("scan_id") and x.get("base_url")]
    except Exception:
        return []


def _request_get(url: str, token: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, params=params or {}, headers=headers_raw, timeout=20)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, params=params or {}, headers=headers_bearer, timeout=20)
    return resp


def fetch_parent_scans(
    base_url: str,
    token: str,
    states: Optional[List[str]] = None,
    limit: int = 200,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    url = build_api_url(base_url, PARENT_SCANS_PATH)
    filter_items = []
    # Some environments don't use initiatedSource="scans" and filtering can hide scans.
    # Keep it off by default; enable via BIGID_PARENT_SCANS_FILTER_INITIATED_SOURCE=1 if needed.
    if FILTER_INITIATED_SOURCE:
        filter_items.append({"field": "initiatedSource", "value": ["scans", "system", "scheduler", None], "operator": "in"})
    cleaned_states = [str(s).strip() for s in (states or []) if str(s).strip()]
    if cleaned_states:
        filter_items.insert(0, {"field": "state", "operator": "in", "value": cleaned_states})
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "filter": json.dumps(filter_items, separators=(",", ":")),
        "sort": json.dumps([{"field": "_id", "order": "desc"}], separators=(",", ":")),
    }

    resp = _request_get(url, token, params=params)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized parent scans API: {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()

    # Extract list from common keys
    items = []
    if isinstance(payload, dict):
        data_block = payload.get("data")
        if isinstance(data_block, dict):
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(data_block.get(key), list):
                    items = data_block.get(key)
                    break
        if not items:
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(payload.get(key), list):
                    items = payload.get(key)
                    break

    return items if isinstance(items, list) else []


def fetch_scan_insights(base_url: str, scan_id: str, token: str) -> Dict[str, Any]:
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}")
    resp = _request_get(url, token, params=None)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized insights API: {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def fetch_about(base_url: str, token: str) -> Dict[str, Any]:
    url = build_api_url(base_url, ABOUT_PATH)
    resp = _request_get(url, token, params=None)
    # /about can be unauth in some envs, but if it is protected respect auth failures.
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized about API: {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _extract_build_version_from_about_payload(payload: Dict[str, Any]) -> str:
    """
    Tries to extract a numeric build like 247.7 from /api/v1/about response.
    Supports strings like 'release-247.7' or '247-7'.
    """
    if not isinstance(payload, dict):
        return ""
    candidates: List[str] = []
    for k in ["releaseVersion", "coreVersion", "version", "build", "buildVersion", "release", "tag"]:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    data_block = payload.get("data")
    if isinstance(data_block, dict):
        for k in ["releaseVersion", "coreVersion", "version", "build", "buildVersion", "release", "tag"]:
            v = data_block.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())

    for s in candidates:
        bv = _parse_build_version_from_text(s)
        if bv:
            return bv
    return ""


def _count_build_classifier_mappings(engine, build_version: str, environment_id: Optional[int]) -> int:
    build_version = str(build_version or "").strip()
    if not build_version:
        return 0
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()
            if not row:
                return 0
            build_id = int(row[0])
            cnt_row = conn.execute(
                text("""
                    SELECT COUNT(DISTINCT classifier_id)
                    FROM build_classifier_mappings
                    WHERE build_id = :b AND (environment_id <=> :e)
                """),
                {"b": build_id, "e": int(environment_id) if environment_id is not None else None},
            ).fetchone()
        return int(cnt_row[0] or 0) if cnt_row else 0
    except Exception:
        return 0


def _max_build_classifier_mapping_sync_time(engine, build_version: str, environment_id: Optional[int]) -> Optional[datetime]:
    build_version = str(build_version or "").strip()
    if not build_version:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()
            if not row:
                return None
            build_id = int(row[0])
            t_row = conn.execute(
                text("""
                    SELECT MAX(last_synced_at)
                    FROM build_classifier_mappings
                    WHERE build_id = :b AND (environment_id <=> :e)
                """),
                {"b": build_id, "e": int(environment_id) if environment_id is not None else None},
            ).fetchone()
        if not t_row:
            return None
        return t_row[0]
    except Exception:
        return None


def sync_global_classifiers_for_env_build(engine, env_row: Dict[str, Any], token: str) -> bool:
    """
    Fetch global enabled classifiers list once per build+environment and store it in build_classifier_mappings.
    """
    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        return False
    env_id = env_row.get("id")
    try:
        about = fetch_about(base_url, token)
        build_version = _extract_build_version_from_about_payload(about)
    except Exception:
        build_version = ""
    if not build_version:
        # fallback to env_row version string if it contains build number
        build_version = _parse_build_version_from_text(env_row.get("version") or "")
    if not build_version:
        return False

    # If version changed since last successful sync, force sync now.
    try:
        env_key = str(env_id if env_id is not None else base_url.rstrip("/"))
        last_build = str(_kv_get(engine, f"global_cls_last_build::{env_key}") or "").strip()
        force_on_version_change = bool(last_build and last_build != str(build_version))
    except Exception:
        force_on_version_change = False

    # Keep DB env version in sync with /about.
    try:
        if env_id is not None:
            with engine.connect() as conn:
                conn.execute(
                    text("UPDATE test_environments SET version = :v WHERE id = :id"),
                    {"v": str(build_version), "id": int(env_id)},
                )
                conn.commit()
    except Exception:
        pass

    ensure_classifier_mapping_tables(engine)

    # If already stored recently, do nothing. Otherwise refresh (mappings can drift).
    existing = _count_build_classifier_mappings(engine, build_version, int(env_id) if env_id is not None else None)
    last_synced_at = _max_build_classifier_mapping_sync_time(engine, build_version, int(env_id) if env_id is not None else None)
    try:
        ttl_hours = float(os.getenv("GLOBAL_CLASSIFIER_SYNC_TTL_HOURS", "12") or "12")
    except Exception:
        ttl_hours = 12.0
    stale = True
    try:
        if last_synced_at is not None:
            age_h = (datetime.utcnow() - last_synced_at).total_seconds() / 3600.0
            stale = bool(age_h > ttl_hours)
    except Exception:
        stale = True
    # Also refresh if count seems suspiciously low (should be ~70+ usually).
    try:
        min_expected = int(os.getenv("GLOBAL_CLASSIFIER_SYNC_MIN_COUNT", "40") or "40")
    except Exception:
        min_expected = 40
    if (not force_on_version_change) and existing and existing > 0 and not stale and existing >= min_expected:
        return True

    api_path = str(env_row.get("classifiers_api_path") or "").strip() or CLASSIFIERS_API_PATH
    try:
        names = fetch_enabled_classifiers(base_url, token, api_path)
    except Exception as ex:
        # Make failures visible in tracker logs.
        env_name = str(env_row.get("env_name") or env_id or base_url).strip()
        print(f"[{_now_utc_naive().isoformat()}Z] env={env_name} global_classifiers_fetch_failed: {ex}", flush=True)
        return False
    if not names:
        env_name = str(env_row.get("env_name") or env_id or base_url).strip()
        print(f"[{_now_utc_naive().isoformat()}Z] env={env_name} global_classifiers_empty build={build_version}", flush=True)
        return False
    upsert_build_classifier_set(
        engine,
        build_version=str(build_version),
        template_name="Global Enabled Classifiers",
        classifier_names=names,
        environment_id=int(env_id) if env_id is not None else None,
    )
    try:
        env_key2 = str(env_id if env_id is not None else base_url.rstrip("/"))
        _kv_set(engine, f"global_cls_last_build::{env_key2}", str(build_version))
    except Exception:
        pass
    return True

def _extract_live_state(insight_payload: Dict[str, Any]) -> str:
    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
    if not si and isinstance(insight_payload, dict):
        si = insight_payload
    prog = si.get("scanProfile", {}).get("progress", {}) if isinstance(si.get("scanProfile"), dict) else {}
    return str(si.get("state") or prog.get("state") or "").strip()


def _extract_scan_attributes_payload(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    Returns (items, total_count) for scan attributes response.
    """
    items: List[Dict[str, Any]] = []
    total_count: Optional[int] = None
    if not isinstance(payload, dict):
        return items, total_count
    data_block = payload.get("data")
    root = data_block if isinstance(data_block, dict) else payload
    for key in ["attributes", "items", "results", "classifierAttributes", "classificationAttributes", "scanAttributes"]:
        val = root.get(key)
        if isinstance(val, list):
            items = [x for x in val if isinstance(x, dict)]
            break
    tc = root.get("totalCount")
    if tc is not None:
        try:
            total_count = int(tc)
        except Exception:
            total_count = None
    return items, total_count


def _parse_build_version_from_text(text_value: Any) -> str:
    s = str(text_value or "")
    m = re.search(r"\b(2\d{2})[\s\-\.]{1,2}(\d{1,3})\b", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m_dot3 = re.search(r"\b(2\d{2})\.(\d{1,3})\.(\d{1,3})\b", s)
    if m_dot3:
        return f"{m_dot3.group(1)}.{m_dot3.group(2)}"
    return ""


def ensure_classifier_mapping_tables(engine) -> None:
    """
    Tables used to store per-build classifier sets (template classifier count).
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS builds (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    version VARCHAR(50) UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS classifiers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) UNIQUE
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS build_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    build_id INT,
                    template_name VARCHAR(100),
                    classifier_set TEXT,
                    UNIQUE KEY uniq_build_template (build_id, template_name),
                    FOREIGN KEY (build_id) REFERENCES builds(id)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS build_classifier_mappings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    build_id INT NOT NULL,
                    classifier_id INT NOT NULL,
                    environment_id INT NULL,
                    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_build_classifier_env (build_id, classifier_id, environment_id),
                    FOREIGN KEY (build_id) REFERENCES builds(id),
                    FOREIGN KEY (classifier_id) REFERENCES classifiers(id)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE build_classifier_mappings ADD UNIQUE KEY uniq_build_classifier_env (build_id, classifier_id, environment_id)"))
                conn.commit()
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


def fetch_enabled_classifiers(base_url: str, token: str, api_path: str) -> List[str]:
    """
    Fetch enabled classifiers list (used as template classifier set approximation).
    """
    url = build_api_url(base_url, api_path)
    resp = _request_get(url, token, params=None)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized classifiers API: {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()

    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        data_block = payload.get("data")
        root = data_block if isinstance(data_block, dict) else payload
        for key in ["classifications", "allClassifications", "results", "items", "rows"]:
            if isinstance(root.get(key), list):
                items = [x for x in root.get(key) if isinstance(x, dict)]
                break

    names: List[str] = []
    for it in items:
        n = str(it.get("name") or it.get("classificationName") or it.get("displayName") or "").strip()
        if n:
            names.append(n)
    # de-dupe while preserving order
    out: List[str] = []
    seen: set[str] = set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def upsert_build_classifier_set(engine, build_version: str, template_name: str, classifier_names: List[str], environment_id: Optional[int]) -> None:
    build_version = str(build_version or "").strip()
    template_name = str(template_name or "").strip() or "Data Source Scan"
    if not build_version or not classifier_names:
        return
    ensure_classifier_mapping_tables(engine)

    with engine.connect() as conn:
        conn.execute(text("INSERT IGNORE INTO builds (version) VALUES (:v)"), {"v": build_version})
        b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()
        if not b_row:
            conn.commit()
            return
        build_id = int(b_row[0])

        # Save template row (comma-separated names)
        classifier_set_text = ", ".join([str(x).strip() for x in classifier_names if str(x).strip()])
        conn.execute(
            text("""
                INSERT INTO build_templates (build_id, template_name, classifier_set)
                VALUES (:b, :t, :s)
                ON DUPLICATE KEY UPDATE
                    classifier_set = VALUES(classifier_set)
            """),
            {"b": build_id, "t": template_name[:100], "s": classifier_set_text},
        )

        # Upsert classifier names + mappings
        for name in classifier_names:
            n = str(name or "").strip()
            if not n:
                continue
            conn.execute(text("INSERT IGNORE INTO classifiers (name) VALUES (:n)"), {"n": n})
            c_row = conn.execute(text("SELECT id FROM classifiers WHERE name = :n LIMIT 1"), {"n": n}).fetchone()
            if not c_row:
                continue
            cid = int(c_row[0])
            conn.execute(
                text("""
                    INSERT IGNORE INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                    VALUES (:b, :c, :e)
                """),
                {"b": build_id, "c": cid, "e": int(environment_id) if environment_id is not None else None},
            )
        conn.commit()

def fetch_scan_classifier_attributes(base_url: str, scan_id: str, token: str, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Fetch per-scan classifier attributes delta list.
    Endpoint: {INSIGHTS_PATH}/{scan_id}/attributes?type=classifier
    """
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}/attributes")
    all_items: List[Dict[str, Any]] = []
    skip = 0
    total_count: Optional[int] = None
    for _ in range(10):
        params = {
            "skip": int(skip),
            "limit": int(limit),
            "requireTotalCount": "true",
            "sort": json.dumps([{"field": "new", "order": "desc"}], separators=(",", ":")),
            "type": "classifier",
        }
        resp = _request_get(url, token, params=params)
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Unauthorized scan attributes API: {resp.status_code}")
        resp.raise_for_status()
        payload = resp.json()
        items, page_total = _extract_scan_attributes_payload(payload if isinstance(payload, dict) else {})
        if total_count is None and page_total is not None:
            total_count = int(page_total)
        if items:
            all_items.extend(items)
        if not items:
            break
        skip += int(limit)
        if total_count is not None and skip >= total_count:
            break
    return all_items


def upsert_scan_parent_record(engine, env_id: Any, scan: Dict[str, Any], status: str) -> None:
    scan_id = str(scan.get("_id") or scan.get("id") or "").strip()
    if not scan_id:
        return
    name = str(scan.get("name") or scan.get("scanName") or scan.get("displayName") or "").strip() or None
    template_name = str(scan.get("scanTemplateName") or "").strip() or None
    scan_type = _normalize_scan_type_value(scan.get("type") or "", template_name) or None
    started_at = str(scan.get("startedAt") or scan.get("startTime") or scan.get("createdAt") or "").strip() or None
    completed_at = str(scan.get("completedAt") or scan.get("endTime") or scan.get("endedAt") or "").strip() or None
    status_val = str(status or "").strip() or None
    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_parents (
                    scan_id, environment_id, scan_name, scan_type, started_at, completed_at, template_name, scan_status, is_active
                ) VALUES (:sid, :eid, :sn, :st, :sa, :ca, :tn, :ss, TRUE)
                ON DUPLICATE KEY UPDATE
                    environment_id = VALUES(environment_id),
                    scan_name = VALUES(scan_name),
                    scan_type = VALUES(scan_type),
                    started_at = VALUES(started_at),
                    completed_at = VALUES(completed_at),
                    template_name = VALUES(template_name),
                    scan_status = VALUES(scan_status),
                    last_seen_at = CURRENT_TIMESTAMP
            """),
            {
                "sid": scan_id,
                "eid": env_id_int,
                "sn": name,
                "st": scan_type,
                "sa": started_at,
                "ca": completed_at,
                "tn": template_name,
                "ss": status_val,
            },
        )
        conn.commit()


def upsert_scan_progress_record(
    engine,
    env_id: Any,
    scan_id: str,
    scan_name: str,
    insight_payload: Dict[str, Any],
    forced_state: Optional[str] = None,
) -> None:
    if not scan_id:
        return

    # Unwrap BigID response shape: {data:{scanInsights:{...}}}
    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
    if not si and isinstance(insight_payload, dict):
        si = insight_payload

    def _to_int(v: Any, default: int = 0) -> int:
        try:
            if v is None or v == "":
                return int(default)
            return int(v)
        except Exception:
            return int(default)

    subscans = si.get("subScans") if isinstance(si.get("subScans"), dict) else {}
    data_sources = si.get("dataSources") if isinstance(si.get("dataSources"), dict) else {}
    classifications = data_sources.get("classifications") if isinstance(data_sources.get("classifications"), dict) else {}
    progress = si.get("scanProfile", {}).get("progress", {}) if isinstance(si.get("scanProfile"), dict) else {}
    scan_state = str(forced_state or si.get("state") or progress.get("state") or "").strip() or None

    completed_sub = _to_int(subscans.get("completedSubScans", 0))
    failed_sub = _to_int(subscans.get("failedSubScans", 0))
    total_sub = _to_int(subscans.get("totalSubScans", 0))

    completed_parts = _to_int(subscans.get("completedParts") or progress.get("complete") or 0)
    failed_parts = _to_int(subscans.get("failedParts") or progress.get("failed") or 0)
    total_parts = _to_int(subscans.get("totalPartsScanned") or progress.get("total") or 0)

    total_class = _to_int(classifications.get("total", 0))
    new_class = _to_int(classifications.get("newlyAdded", 0))
    false_removed = _to_int(classifications.get("falseRemoved", 0))
    objects_scanned = _to_int(data_sources.get("objectsScanned") or si.get("objectsScanned") or 0)
    objects_failed = _to_int(data_sources.get("objectsFailed") or 0)
    objects_with_pi = _to_int(data_sources.get("objectsWithPi") or 0)

    # Align to 10-minute marks in UTC for clean chart axis (11:10, 11:20, ...)
    now_utc = _now_utc_naive()
    captured_at_utc = now_utc.replace(minute=(now_utc.minute // 10) * 10, second=0, microsecond=0)

    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_progress (
                    captured_at_utc,
                    scan_id, scan_name, environment_id,
                    scan_state,
                    completed_sub_scans, failed_sub_scans, total_sub_scans,
                    completed_parts, failed_parts, total_parts_scanned,
                    total_classifications, newly_added_classifications, false_removed_classifications,
                    objects_scanned, objects_failed, objects_with_pi
                ) VALUES (
                    :cau,
                    :sid, :sname, :eid,
                    :sst,
                    :cs, :fs, :tsc,
                    :cp, :fp, :tp,
                    :tc, :nc, :fr,
                    :os, :of, :owp
                )
                ON DUPLICATE KEY UPDATE
                    scan_name = VALUES(scan_name),
                    environment_id = VALUES(environment_id),
                    scan_state = VALUES(scan_state),
                    completed_sub_scans = VALUES(completed_sub_scans),
                    failed_sub_scans = VALUES(failed_sub_scans),
                    total_sub_scans = VALUES(total_sub_scans),
                    completed_parts = VALUES(completed_parts),
                    failed_parts = VALUES(failed_parts),
                    total_parts_scanned = VALUES(total_parts_scanned),
                    total_classifications = VALUES(total_classifications),
                    newly_added_classifications = VALUES(newly_added_classifications),
                    false_removed_classifications = VALUES(false_removed_classifications),
                    objects_scanned = VALUES(objects_scanned),
                    objects_failed = VALUES(objects_failed),
                    objects_with_pi = VALUES(objects_with_pi)
            """),
            {
                "cau": captured_at_utc,
                "sid": scan_id,
                "sname": str(scan_name or "")[:1000] or None,
                "eid": env_id_int,
                "sst": scan_state,
                "cs": completed_sub,
                "fs": failed_sub,
                "tsc": total_sub,
                "cp": completed_parts,
                "fp": failed_parts,
                "tp": total_parts,
                "tc": total_class,
                "nc": new_class,
                "fr": false_removed,
                "os": objects_scanned,
                "of": objects_failed,
                "owp": objects_with_pi,
            },
        )
        conn.commit()


def upsert_scan_insights_record(engine, scan_id: str, insight_payload: Dict[str, Any]) -> None:
    if not scan_id:
        return

    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
    if not si and isinstance(insight_payload, dict):
        si = insight_payload

    data_sources = si.get("dataSources") if isinstance(si.get("dataSources"), dict) else {}
    classifications = data_sources.get("classifications") if isinstance(data_sources.get("classifications"), dict) else {}

    def _to_int(v: Any) -> Optional[int]:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except Exception:
            return None

    ds_names = data_sources.get("dataSourceNames")
    if isinstance(ds_names, list):
        data_source_names_text = ", ".join(sorted({str(x).strip() for x in ds_names if str(x).strip()}))
    else:
        data_source_names_text = ""

    attr_names = []
    for k in ["classifierAttributeNames", "csAttributeNames"]:
        v = data_sources.get(k)
        if isinstance(v, list):
            attr_names.extend([str(x).strip() for x in v if str(x).strip()])
    uniq_attrs = sorted(set(attr_names))
    classifier_attribute_names_text = ", ".join(uniq_attrs)
    distinct_attrs = len(uniq_attrs) if uniq_attrs else None

    objects_with_pi = _to_int(data_sources.get("objectsWithPi"))
    objects_scanned = _to_int(data_sources.get("objectsScanned"))
    objects_enumerated = _to_int(data_sources.get("objectsEnumerated"))
    total_classification = _to_int(classifications.get("total"))

    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_insights (
                    scan_id,
                    distinct_classification_attributes,
                    classifier_attribute_names,
                    data_source_names,
                    objects_with_pi,
                    objects_scanned,
                    objects_enumerated,
                    total_classification,
                    new_attributes_count,
                    removed_attributes_count
                ) VALUES (
                    :sid, :dca, :can, :dsn, :owp, :osc, :oen, :tcl, :nac, :rac
                )
                ON DUPLICATE KEY UPDATE
                    distinct_classification_attributes = VALUES(distinct_classification_attributes),
                    classifier_attribute_names = VALUES(classifier_attribute_names),
                    data_source_names = VALUES(data_source_names),
                    objects_with_pi = VALUES(objects_with_pi),
                    objects_scanned = VALUES(objects_scanned),
                    objects_enumerated = VALUES(objects_enumerated),
                    total_classification = VALUES(total_classification),
                    new_attributes_count = COALESCE(VALUES(new_attributes_count), new_attributes_count),
                    removed_attributes_count = COALESCE(VALUES(removed_attributes_count), removed_attributes_count),
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "sid": scan_id,
                "dca": distinct_attrs,
                "can": classifier_attribute_names_text or None,
                "dsn": data_source_names_text or None,
                "owp": objects_with_pi,
                "osc": objects_scanned,
                "oen": objects_enumerated,
                "tcl": total_classification,
                "nac": None,
                "rac": None,
            },
        )
        conn.commit()


def upsert_scan_attribute_deltas(engine, scan_id: str, attribute_items: List[Dict[str, Any]]) -> None:
    """
    Upsert scan attribute delta details into:
    - scan_classifier_attributes
    - scan_insight_attribute_delta_map
    """
    if not scan_id:
        return

    def try_int(v: Any) -> Optional[int]:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except Exception:
            return None

    attr_map: Dict[str, Dict[str, Any]] = {}
    for item in (attribute_items or []):
        if not isinstance(item, dict):
            continue
        attr_name = str(item.get("name") or item.get("attributeName") or item.get("classifierAttribute") or "").strip()
        if not attr_name:
            continue

        current = attr_map.get(attr_name, {
            "is_new": False,
            "is_removed": False,
            "finding_count": None,
            "attribute_value": "",
            "new_count_value": 0,
            "removed_count_value": 0,
        })

        raw_new = item.get("new")
        raw_removed = item.get("removed")
        is_new = bool(raw_new is True)
        is_removed = bool(raw_removed is True)
        new_cnt_val = try_int(raw_new)
        removed_cnt_val = try_int(raw_removed)
        if new_cnt_val is not None and new_cnt_val > 0:
            is_new = True
        if removed_cnt_val is not None and removed_cnt_val > 0:
            is_removed = True

        finding_count = (
            try_int(item.get("findingCount"))
            or try_int(item.get("findings"))
            or try_int(item.get("count"))
            or try_int(item.get("total"))
            or try_int(item.get("totalCount"))
            or try_int(item.get("objectsWithPi"))
            or try_int(item.get("objectsCount"))
        )
        attribute_value = str(
            item.get("value")
            or item.get("attributeValue")
            or item.get("deltaValue")
            or item.get("changeValue")
            or item.get("displayValue")
            or ""
        ).strip()

        current["is_new"] = bool(current.get("is_new") or is_new)
        current["is_removed"] = bool(current.get("is_removed") or is_removed)

        if new_cnt_val is not None:
            current["new_count_value"] = max(int(current.get("new_count_value") or 0), int(new_cnt_val))
        elif is_new and int(current.get("new_count_value") or 0) == 0:
            current["new_count_value"] = 1

        if removed_cnt_val is not None:
            current["removed_count_value"] = max(int(current.get("removed_count_value") or 0), int(removed_cnt_val))
        elif is_removed and int(current.get("removed_count_value") or 0) == 0:
            current["removed_count_value"] = 1

        if finding_count is not None:
            prev_fc = current.get("finding_count")
            current["finding_count"] = finding_count if prev_fc is None else max(int(prev_fc), int(finding_count))

        if attribute_value:
            current["attribute_value"] = attribute_value

        if not attribute_value:
            if int(current.get("new_count_value") or 0) > 0 and int(current.get("removed_count_value") or 0) > 0:
                current["attribute_value"] = "New, Removed"
            elif int(current.get("new_count_value") or 0) > 0:
                current["attribute_value"] = "New"
            elif int(current.get("removed_count_value") or 0) > 0:
                current["attribute_value"] = "Removed"

        if current.get("finding_count") is None:
            current["finding_count"] = int(current.get("new_count_value") or 0) + int(current.get("removed_count_value") or 0)

        attr_map[attr_name] = current

    if not attr_map:
        return

    # Summary metrics we want available for trend analysis / scan status.
    # IMPORTANT: "New/Removed" here refers to the *counts* shown in BigID's attributes table,
    # not the number of attributes.
    distinct_attrs = len(attr_map)
    classifier_attribute_names_text = ", ".join(sorted(attr_map.keys()))
    new_attributes_count = sum(int(v.get("new_count_value") or 0) for v in attr_map.values())
    removed_attributes_count = sum(int(v.get("removed_count_value") or 0) for v in attr_map.values())

    with engine.connect() as conn:
        conn.execute(text("DELETE FROM scan_insight_attribute_delta_map WHERE scan_id = :sid"), {"sid": str(scan_id)})
        for attr_name, flags in attr_map.items():
            conn.execute(text("INSERT IGNORE INTO scan_classifier_attributes (name) VALUES (:n)"), {"n": attr_name})
            row = conn.execute(text("SELECT id FROM scan_classifier_attributes WHERE name = :n LIMIT 1"), {"n": attr_name}).fetchone()
            if not row:
                continue
            conn.execute(text("""
                INSERT INTO scan_insight_attribute_delta_map
                (scan_id, attribute_id, is_new, is_removed, finding_count, attribute_value, new_count_value, removed_count_value)
                VALUES (:sid, :aid, :is_new, :is_removed, :fc, :av, :ncv, :rcv)
                ON DUPLICATE KEY UPDATE
                    is_new = VALUES(is_new),
                    is_removed = VALUES(is_removed),
                    finding_count = VALUES(finding_count),
                    attribute_value = VALUES(attribute_value),
                    new_count_value = VALUES(new_count_value),
                    removed_count_value = VALUES(removed_count_value)
            """), {
                "sid": str(scan_id),
                "aid": int(row[0]),
                "is_new": bool(flags.get("is_new")),
                "is_removed": bool(flags.get("is_removed")),
                "fc": flags.get("finding_count"),
                "av": str(flags.get("attribute_value") or "") or None,
                "ncv": int(flags.get("new_count_value") or 0),
                "rcv": int(flags.get("removed_count_value") or 0),
            })

        # Also update scan_insights so the dashboard can plot build-by-build trends.
        # We intentionally only set these when we have attribute delta data available.
        conn.execute(
            text("""
                INSERT INTO scan_insights
                    (scan_id, distinct_classification_attributes, classifier_attribute_names, new_attributes_count, removed_attributes_count)
                VALUES
                    (:sid, :dca, :can, :nac, :rac)
                ON DUPLICATE KEY UPDATE
                    distinct_classification_attributes = COALESCE(VALUES(distinct_classification_attributes), distinct_classification_attributes),
                    classifier_attribute_names = COALESCE(VALUES(classifier_attribute_names), classifier_attribute_names),
                    new_attributes_count = COALESCE(VALUES(new_attributes_count), new_attributes_count),
                    removed_attributes_count = COALESCE(VALUES(removed_attributes_count), removed_attributes_count),
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "sid": str(scan_id),
                "dca": int(distinct_attrs) if distinct_attrs else None,
                "can": classifier_attribute_names_text or None,
                "nac": int(new_attributes_count) if new_attributes_count is not None else None,
                "rac": int(removed_attributes_count) if removed_attributes_count is not None else None,
            },
        )
        conn.commit()


def _last_progress_state(engine, scan_id: str) -> str:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT scan_state
                    FROM scan_progress
                    WHERE scan_id = :sid
                    ORDER BY COALESCE(captured_at_utc, timestamp) DESC, id DESC
                    LIMIT 1
                """),
                {"sid": scan_id},
            ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def track_once(engine) -> Dict[str, Any]:
    """
    One tracker cycle.

    Key design goals:
    - Always prefer tracking *running* scans quickly (so we don't miss 10-min slots).
    - Avoid slow attribute-delta fetches unless needed.
    - Provide actionable counters for heartbeat diagnostics.
    """
    envs = load_active_environments(engine)
    ensure_classifier_mapping_tables(engine)

    # Cache tokens per base_url for this cycle
    token_cache: Dict[str, str] = {}

    # Metrics
    metrics: Dict[str, Any] = {
        "envs": len(envs),
        "env_failures": 0,
        "running_scans_seen": 0,
        "db_running_seen": 0,
        "backfill_seen": 0,
        "backfill_ok": 0,
        "backfill_fail": 0,
        "attempted_insights": 0,
        "snapshots_ok": 0,
        "fail_auth": 0,
        "fail_parent_scans": 0,
        "fail_insights": 0,
        "fail_write": 0,
        "fail_attrs": 0,
        "index_pages": 0,
        "index_scans": 0,
        "insights_backfill_seen": 0,
        "insights_backfill_ok": 0,
        "insights_backfill_fail": 0,
        "global_classifier_sync_ok": 0,
        "global_classifier_sync_fail": 0,
        "global_builds_seen": [],
    }

    def get_token_for_env(env_row: Dict[str, Any]) -> str:
        base_url = str(env_row.get("base_url") or "").strip()
        if not base_url:
            return ""
        key = base_url.rstrip("/")
        if key in token_cache:
            return token_cache[key]
        tok = Get_auth_token(base_url, env_row)
        token_cache[key] = tok
        return tok

    def _scan_row_from_insights(scan_id: str, insight_payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
        si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
        if not si and isinstance(insight_payload, dict):
            si = insight_payload
        sp = si.get("scanProfile") if isinstance(si.get("scanProfile"), dict) else {}
        prog = sp.get("progress") if isinstance(sp.get("progress"), dict) else {}
        state = str(si.get("state") or prog.get("state") or "").strip()
        name = str(sp.get("name") or "").strip()
        tmpl = str(sp.get("scanTemplateName") or "").strip()
        scan_row = {
            "_id": str(scan_id),
            "name": name,
            "type": _normalize_scan_type_value(sp.get("type") or "", tmpl),
            "startedAt": sp.get("startedAt") or sp.get("updatedAt"),
            "completedAt": sp.get("completedAt"),
            "scanTemplateName": tmpl,
            "state": state,
        }
        return scan_row, state, name

    def _needs_attribute_deltas(scan_id: str) -> bool:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT classifier_attribute_names FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                    {"sid": str(scan_id)},
                ).fetchone()
            v = str(row[0] or "").strip() if row else ""
            return not bool(v)
        except Exception:
            return True

    def _needs_attribute_delta_counts(scan_id: str) -> bool:
        """
        We want these fields for trend analysis:
        - distinct_classification_attributes (# classifiers)
        - new_attributes_count
        - removed_attributes_count
        """
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT
                            distinct_classification_attributes,
                            new_attributes_count,
                            removed_attributes_count
                        FROM scan_insights
                        WHERE scan_id = :sid
                        LIMIT 1
                    """),
                    {"sid": str(scan_id)},
                ).fetchone()
            if not row:
                return True
            return any(v is None for v in row)
        except Exception:
            return True

    def _load_backfill_benchmark_scan_pairs(limit: int = 6) -> List[Tuple[str, Optional[int]]]:
        """
        Backfill missing scan_insights for historical benchmark scans.
        Uses auto_scan_benchmarks as the seed list.
        """
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT ab.scan_id, ab.environment_id
                        FROM auto_scan_benchmarks ab
                        LEFT JOIN scan_insights si ON si.scan_id = ab.scan_id
                        WHERE
                            ab.scan_id IS NOT NULL
                            AND ab.scan_id <> ''
                            AND (
                                si.scan_id IS NULL
                                OR si.total_classification IS NULL
                                OR si.distinct_classification_attributes IS NULL
                                OR si.new_attributes_count IS NULL
                                OR si.removed_attributes_count IS NULL
                            )
                        ORDER BY ab.updated_at DESC, ab.id DESC
                        LIMIT :lim
                    """),
                    {"lim": int(limit)},
                ).fetchall()
            out: List[Tuple[str, Optional[int]]] = []
            for r in rows or []:
                sid = str(r[0] or "").strip()
                if not sid:
                    continue
                try:
                    eid = int(r[1]) if r[1] is not None else None
                except Exception:
                    eid = None
                out.append((sid, eid))
            return out
        except Exception:
            return []

    def _fetch_running_parent_scans(base_url: str, token: str) -> List[Dict[str, Any]]:
        all_items: List[Dict[str, Any]] = []
        skip = 0
        limit = 500
        for _ in range(6):  # cap pages to keep cycle bounded
            page = fetch_parent_scans(base_url, token, states=RUNNING_STATES, limit=limit, skip=skip)
            if not page:
                break
            all_items.extend(page)
            if len(page) < limit:
                break
            skip += limit
        return all_items

    def _index_some_parent_scans_all_states(env_row: Dict[str, Any], page_limit: int = 500) -> int:
        """
        Incrementally index *all* parent scans into scan_parents (not just running),
        using a stored skip cursor per environment.
        """
        base_url = str(env_row.get("base_url") or "").strip()
        if not base_url:
            return 0
        env_id = env_row.get("id")
        env_key = str(env_id or base_url.rstrip("/"))
        skip_key = f"parent_index_skip::{env_key}"
        raw_skip = _kv_get(engine, skip_key)
        try:
            skip = int(raw_skip) if str(raw_skip).strip() else 0
        except Exception:
            skip = 0
        token = get_token_for_env(env_row)
        page = fetch_parent_scans(base_url, token, states=None, limit=int(page_limit), skip=int(skip))
        if not page:
            # wrap around to keep eventually consistent if API order changes
            _kv_set(engine, skip_key, "0")
            return 0
        wrote = 0
        for scan in page:
            if not isinstance(scan, dict):
                continue
            status = resolve_scan_status_value(scan)
            scan_id = str(scan.get("_id") or scan.get("id") or "").strip()
            if not scan_id:
                continue
            try:
                upsert_scan_parent_record(engine, env_id, scan, status)
                wrote += 1
            except Exception:
                pass
        metrics["index_pages"] += 1
        metrics["index_scans"] += int(wrote)
        # advance cursor
        _kv_set(engine, skip_key, str(skip + int(page_limit)))
        return wrote

    def _load_scan_parents_needing_insights(limit: int = 50) -> List[Dict[str, Any]]:
        """
        DB-driven work queue: any scan in scan_parents that is missing scan_insights
        or missing key classification fields needed by the dashboard.
        """
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT
                            sp.scan_id,
                            sp.environment_id,
                            sp.scan_name,
                            te.base_url
                        FROM scan_parents sp
                        JOIN test_environments te ON te.id = sp.environment_id
                        LEFT JOIN scan_insights si ON si.scan_id = sp.scan_id
                        WHERE te.is_active = 1
                          AND sp.environment_id IS NOT NULL
                          AND (
                            si.scan_id IS NULL
                            OR si.total_classification IS NULL
                            OR si.distinct_classification_attributes IS NULL
                            OR si.new_attributes_count IS NULL
                            OR si.removed_attributes_count IS NULL
                          )
                        ORDER BY sp.updated_at DESC
                        LIMIT :lim
                    """),
                    {"lim": int(limit)},
                ).fetchall()
            out = []
            for r in rows or []:
                out.append({
                    "scan_id": str(r[0] or "").strip(),
                    "environment_id": r[1],
                    "scan_name": str(r[2] or "").strip(),
                    "base_url": str(r[3] or "").strip(),
                })
            return [x for x in out if x.get("scan_id") and x.get("base_url")]
        except Exception:
            return []

    # Build active env maps (used by DB-running safety net)
    env_by_id: Dict[Any, Dict[str, Any]] = {e.get("id"): e for e in envs if isinstance(e, dict)}
    env_by_base: Dict[str, Dict[str, Any]] = {
        str(e.get("base_url") or "").strip().rstrip("/"): e
        for e in envs
        if isinstance(e, dict) and str(e.get("base_url") or "").strip()
    }

    # Discover running scans via API (fast path)
    jobs: List[Tuple[str, Dict[str, Any], str, str]] = []
    seen_scan_ids: set[str] = set()

    for env in envs:
        base_url = str(env.get("base_url") or "").strip()
        if not base_url:
            continue
        env_id = env.get("id")
        env_name = str(env.get("env_name") or "").strip() or str(env_id or "")

        try:
            token = get_token_for_env(env)
        except Exception as ex:
            metrics["env_failures"] += 1
            metrics["fail_auth"] += 1
            print(f"[{_now_utc_naive().isoformat()}Z] env={env_name} auth_failed: {ex}", flush=True)
            continue

        # NOTE:
        # We do NOT call /about every cycle (wasteful). Version + classifier sync is
        # done when we see running scans (build inferred from scan name/insights).

        try:
            scans = _fetch_running_parent_scans(base_url, token)
        except Exception as ex:
            metrics["env_failures"] += 1
            metrics["fail_parent_scans"] += 1
            print(f"[{_now_utc_naive().isoformat()}Z] env={env_name} parent_scans_failed: {ex}", flush=True)
            continue

        metrics["running_scans_seen"] += len(scans)

        for scan in scans:
            if not isinstance(scan, dict):
                continue
            status = resolve_scan_status_value(scan)
            scan_id = str(scan.get("_id") or scan.get("id") or "").strip()
            scan_name = str(scan.get("name") or scan.get("scanName") or scan.get("displayName") or "").strip()
            if not scan_id:
                continue
            seen_scan_ids.add(scan_id)
            try:
                upsert_scan_parent_record(engine, env_id, scan, status)
            except Exception:
                pass
            if not _is_running_state(status):
                continue
            jobs.append((base_url, env, scan_id, scan_name, status))

    # DB-driven safety net: keep tracking scans that DB believes are still running
    db_running = load_running_scan_env_rows(engine)
    metrics["db_running_seen"] = len(db_running)
    for r in db_running:
        sid = str(r.get("scan_id") or "").strip()
        if not sid or sid in seen_scan_ids:
            continue
        base = str(r.get("base_url") or "").strip().rstrip("/")
        env_row = env_by_id.get(r.get("environment_id")) or env_by_base.get(base) or {
            "id": r.get("environment_id"),
            "env_name": r.get("env_name"),
            "base_url": r.get("base_url"),
        }
        jobs.append(
            (
                str(env_row.get("base_url") or "").strip(),
                env_row,
                sid,
                str(r.get("scan_name") or ""),
                str(r.get("scan_status") or ""),
            )
        )
        seen_scan_ids.add(sid)

    def process_scan(job: Tuple[str, Dict[str, Any], str, str, str]) -> Tuple[bool, str, int]:
        base_url, env_row, scan_id, scan_name, parent_state = job
        if not base_url or not scan_id:
            return False, "bad_job", 0
        try:
            token = get_token_for_env(env_row)
        except Exception:
            return False, "auth", 0

        # Insights fetch with one retry (fresh token)
        try:
            insight = fetch_scan_insights(base_url, scan_id, token)
            attempted = 1
        except Exception:
            try:
                token_cache.pop(base_url.rstrip("/"), None)
                token2 = get_token_for_env(env_row)
                insight = fetch_scan_insights(base_url, scan_id, token2)
                attempted = 2
            except Exception:
                return False, "insights", 2

        try:
            # Use insights payload to keep scan_parents status accurate (running -> terminal, etc.)
            scan_row, insight_state, insight_name = _scan_row_from_insights(scan_id, insight)
            best_state = _choose_best_state(parent_state, insight_state)
            scan_row["state"] = best_state
            try:
                upsert_scan_parent_record(engine, env_row.get("id"), scan_row, best_state)
            except Exception:
                pass

            # Snapshot row (slot-aligned) + normalized insights summary
            upsert_scan_progress_record(
                engine,
                env_row.get("id"),
                scan_id,
                scan_name or insight_name,
                insight,
                forced_state=best_state,
            )
            upsert_scan_insights_record(engine, scan_id, insight)

            # Template classifier set sync (what user means by "template has ~72 classifiers").
            # Only when we are already processing a scan (no extra background /about polling).
            # If this build+env has no mappings (or stale / too small), fetch enabled classifiers.
            try:
                scan_profile = (insight.get("data", {}).get("scanInsights", {}).get("scanProfile", {}) if isinstance(insight, dict) else {}) or {}
                bver = _parse_build_version_from_text(scan_profile.get("name") or scan_name or insight_name)
                tmpl = str(scan_profile.get("scanTemplateName") or scan_profile.get("scanTemplateId") or "Data Source Scan").strip()
                eid = env_row.get("id")
                if bver:
                    try:
                        min_expected = int(os.getenv("GLOBAL_CLASSIFIER_SYNC_MIN_COUNT", "40") or "40")
                    except Exception:
                        min_expected = 40
                    try:
                        ttl_hours = float(os.getenv("GLOBAL_CLASSIFIER_SYNC_TTL_HOURS", "12") or "12")
                    except Exception:
                        ttl_hours = 12.0

                    existing = _count_build_classifier_mappings(engine, bver, int(eid) if eid is not None else None)
                    last_synced_at = _max_build_classifier_mapping_sync_time(engine, bver, int(eid) if eid is not None else None)
                    stale = True
                    try:
                        if last_synced_at is not None:
                            age_h = (datetime.utcnow() - last_synced_at).total_seconds() / 3600.0
                            stale = bool(age_h > ttl_hours)
                    except Exception:
                        stale = True

                    needs = (existing <= 0) or (existing < min_expected) or stale
                    if needs:
                        api_path = str(env_row.get("classifiers_api_path") or "").strip() or CLASSIFIERS_API_PATH
                        names = fetch_enabled_classifiers(base_url, token, api_path)
                        if names:
                            upsert_build_classifier_set(engine, bver, str(tmpl or "Data Source Scan"), names, int(eid) if eid is not None else None)
                            metrics["global_classifier_sync_ok"] += 1
                            try:
                                metrics["global_builds_seen"].append(str(bver))
                            except Exception:
                                pass
                            # Keep env version aligned to inferred build (best-effort).
                            try:
                                if eid is not None:
                                    with engine.connect() as conn:
                                        conn.execute(
                                            text("UPDATE test_environments SET version = :v WHERE id = :id"),
                                            {"v": str(bver), "id": int(eid)},
                                        )
                                        conn.commit()
                            except Exception:
                                pass
                            # Remember last synced build for this env (best-effort).
                            try:
                                env_key = str(eid if eid is not None else base_url.rstrip("/"))
                                _kv_set(engine, f"global_cls_last_build::{env_key}", str(bver))
                            except Exception:
                                pass
                        else:
                            metrics["global_classifier_sync_fail"] += 1
            except Exception:
                pass

            # Attribute delta fetch can be slow; only do it when useful.
            try:
                if _is_terminal_state(state) or _needs_attribute_deltas(scan_id) or _needs_attribute_delta_counts(scan_id):
                    attrs = fetch_scan_classifier_attributes(base_url, scan_id, get_token_for_env(env_row))
                    upsert_scan_attribute_deltas(engine, scan_id, attrs)
            except Exception:
                return True, "ok_attrs_failed", attempted

            # When scan is terminal, compute and persist throughput for existing auto row.
            try:
                if _is_terminal_state(state):
                    update_auto_scan_benchmarks_throughput(
                        engine,
                        scan_id,
                        started_at=scan_row.get("startedAt"),
                        completed_at=scan_row.get("completedAt"),
                    )
            except Exception:
                pass

            return True, "ok", attempted
        except Exception:
            return False, "write", attempted

    # Process jobs concurrently to keep cycle under 10 minutes
    if jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(process_scan, j) for j in jobs]
            for f in as_completed(futs):
                try:
                    ok, reason, attempted = f.result()
                except Exception:
                    ok, reason, attempted = False, "unknown", 0
                try:
                    metrics["attempted_insights"] += int(attempted or 0)
                except Exception:
                    pass
                if ok:
                    metrics["snapshots_ok"] += 1
                    if reason == "ok_attrs_failed":
                        metrics["fail_attrs"] += 1
                else:
                    if reason == "auth":
                        metrics["fail_auth"] += 1
                    elif reason == "insights":
                        metrics["fail_insights"] += 1
                    elif reason == "write":
                        metrics["fail_write"] += 1

    # Backfill classification metrics for recent benchmark scans (bounded work).
    # This is needed so Trend Analysis can plot classification metrics build-by-build
    # even when those scans are not currently running.
    try:
        backfill_pairs = _load_backfill_benchmark_scan_pairs(limit=6)
    except Exception:
        backfill_pairs = []

    metrics["backfill_seen"] = len(backfill_pairs)
    for sid, eid in (backfill_pairs or []):
        env_row = env_by_id.get(eid) if eid is not None else None
        if not env_row:
            # fallback: try any active env (best-effort)
            env_row = envs[0] if envs else None
        base_url = str((env_row or {}).get("base_url") or "").strip()
        if not base_url:
            metrics["backfill_fail"] += 1
            continue
        try:
            tok = get_token_for_env(env_row)
            insight = fetch_scan_insights(base_url, sid, tok)
            upsert_scan_insights_record(engine, sid, insight)
            # Fetch attribute deltas when needed for counts / names (may be slow).
            if _needs_attribute_deltas(sid) or _needs_attribute_delta_counts(sid):
                attrs = fetch_scan_classifier_attributes(base_url, sid, tok)
                upsert_scan_attribute_deltas(engine, sid, attrs)
            metrics["backfill_ok"] += 1
        except Exception:
            metrics["backfill_fail"] += 1

    # Index all scans (all states) incrementally, then backfill missing insights.
    # This is the main mechanism to eventually populate scan_insights for every scan.
    try:
        for env in envs:
            try:
                _index_some_parent_scans_all_states(env, page_limit=500)
            except Exception:
                pass
    except Exception:
        pass

    try:
        backlog = _load_scan_parents_needing_insights(limit=50)
    except Exception:
        backlog = []

    metrics["insights_backfill_seen"] = len(backlog)

    def backfill_one(row: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            base_url = str(row.get("base_url") or "").strip()
            env_id = row.get("environment_id")
            scan_id = str(row.get("scan_id") or "").strip()
            if not base_url or not scan_id:
                return False, "bad_row"
            env_row = env_by_id.get(env_id) or env_by_base.get(base_url.rstrip("/")) or {"id": env_id, "base_url": base_url}
            tok = get_token_for_env(env_row)
            insight = fetch_scan_insights(base_url, scan_id, tok)
            upsert_scan_insights_record(engine, scan_id, insight)
            # Pull attributes only when needed for classifier counts / delta counts.
            if _needs_attribute_deltas(scan_id) or _needs_attribute_delta_counts(scan_id):
                attrs = fetch_scan_classifier_attributes(base_url, scan_id, tok)
                upsert_scan_attribute_deltas(engine, scan_id, attrs)
            return True, "ok"
        except Exception:
            return False, "fail"

    if backlog:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(backfill_one, r) for r in backlog]
            for f in as_completed(futs):
                try:
                    ok, _ = f.result()
                except Exception:
                    ok = False
                if ok:
                    metrics["insights_backfill_ok"] += 1
                else:
                    metrics["insights_backfill_fail"] += 1

    return metrics


def run_for_scan_id(engine, scan_id: str, verbose: bool = True) -> bool:
    """
    Force-update a specific scan_id by trying all active envs until the insights API succeeds.
    Writes scan_progress + scan_insights + attribute delta map when possible.
    """
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return False
    envs = load_active_environments(engine)
    for env in envs:
        base_url = str(env.get("base_url") or "").strip()
        env_id = env.get("id")
        env_name = str(env.get("env_name") or "").strip() or str(env_id or "")
        if not base_url:
            continue
        try:
            token = Get_auth_token(base_url, env)
        except Exception as ex:
            if verbose:
                print(f"[{_now_utc_naive().isoformat()}Z] scan={scan_id} env={env_name} auth_failed: {ex}", flush=True)
            continue
        try:
            insight = fetch_scan_insights(base_url, scan_id, token)
        except Exception as ex:
            # most common: 404 (scan not in this env) or 401/403
            if verbose:
                print(f"[{_now_utc_naive().isoformat()}Z] scan={scan_id} env={env_name} insights_failed: {ex}", flush=True)
            continue
        try:
            state = _extract_live_state(insight)
            upsert_scan_progress_record(engine, env_id, scan_id, "", insight)
            upsert_scan_insights_record(engine, scan_id, insight)
            try:
                attrs = fetch_scan_classifier_attributes(base_url, scan_id, token)
                upsert_scan_attribute_deltas(engine, scan_id, attrs)
            except Exception:
                pass
            if verbose:
                print(f"[{_now_utc_naive().isoformat()}Z] scan={scan_id} env={env_name} updated state={state or 'unknown'}", flush=True)
            return True
        except Exception as ex:
            if verbose:
                print(f"[{_now_utc_naive().isoformat()}Z] scan={scan_id} env={env_name} write_failed: {ex}", flush=True)
            continue
    if verbose:
        print(f"[{_now_utc_naive().isoformat()}Z] scan={scan_id} not_found_in_any_active_env", flush=True)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-seconds", type=int, default=600, help="Polling interval in seconds (default: 600)")
    ap.add_argument(
        "--align-10min",
        action="store_true",
        default=True,
        help="Align runs to UTC 10-minute boundaries (default: on)",
    )
    ap.add_argument(
        "--no-align-10min",
        action="store_false",
        dest="align_10min",
        help="Disable alignment to 10-minute boundaries",
    )
    ap.add_argument("--once", action="store_true", help="Run one cycle then exit")
    ap.add_argument("--scan-id", type=str, default="", help="Force-update a specific scan id (try all envs)")
    ap.add_argument(
        "--backfill-all-insights",
        action="store_true",
        help="Backfill scan_insights for all scans (uses pagination; may take a long time)",
    )
    ap.add_argument(
        "--backfill-batch",
        type=int,
        default=200,
        help="Batch size for --backfill-all-insights (default: 200)",
    )
    ap.add_argument("--quiet", action="store_true", help="Less logging")
    args = ap.parse_args()

    engine = create_engine(DB_CONNECTION_STRING)
    ensure_scan_tracking_tables(engine)
    ensure_scan_progress_table(engine)

    if str(args.scan_id or "").strip():
        _hb_update(engine, status="running", message=f"manual_scan_id={str(args.scan_id).strip()}", started=_now_utc_naive())
        ok = run_for_scan_id(engine, str(args.scan_id).strip(), verbose=(not args.quiet))
        _hb_update(engine, status=("ok" if ok else "error"), message=f"manual_scan_id_done ok={ok}", finished=_now_utc_naive())
        return 0 if ok else 2

    if bool(args.backfill_all_insights):
        started = _now_utc_naive()
        _hb_update(engine, status="running", message="backfill_all_insights_start", started=started)
        total_ok = 0
        total_fail = 0
        # Run repeated cycles until backlog is empty (or an error occurs).
        try:
            while True:
                m = track_once(engine) or {}
                total_ok += int(m.get("insights_backfill_ok", 0) or 0)
                total_fail += int(m.get("insights_backfill_fail", 0) or 0)
                # Stop when there is no more backlog in scan_parents.
                if int(m.get("insights_backfill_seen", 0) or 0) == 0:
                    break
                # Safety: don't spin too tight.
                time.sleep(1)
        except Exception as ex:
            finished = _now_utc_naive()
            _hb_update(engine, status="error", message=f"backfill_all_insights_failed {ex}", finished=finished)
            return 2
        finished = _now_utc_naive()
        _hb_update(
            engine,
            status="ok",
            message=f"backfill_all_insights_done ok={total_ok} fail={total_fail}",
            finished=finished,
        )
        if not args.quiet:
            print(
                f"[{_now_utc_naive().isoformat()}Z] backfill_all_insights_done ok={total_ok} fail={total_fail}",
                flush=True,
            )
        return 0

    def seconds_until_next_10min_boundary() -> int:
        now = _now_utc_naive()
        base = now.replace(second=0, microsecond=0)
        next_min = ((base.minute // 10) + 1) * 10
        if next_min >= 60:
            target = base.replace(minute=0) + timedelta(hours=1)
        else:
            target = base.replace(minute=next_min)
        delta = (target - now).total_seconds()
        return max(1, int(delta))

    last_slot_key = None
    while True:
        if args.align_10min:
            now = _now_utc_naive()
            slot = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
            slot_key = slot.strftime("%Y-%m-%dT%H:%M")
            if slot_key == last_slot_key:
                time.sleep(seconds_until_next_10min_boundary())
                continue
            last_slot_key = slot_key
        started = _now_utc_naive()
        _hb_update(engine, status="running", message="cycle_start", started=started)
        try:
            m = track_once(engine) or {}
            status = "ok"
            try:
                gb = [str(x).strip() for x in (m.get("global_builds_seen") or []) if str(x).strip()]
                gb = sorted(set(gb))[:5]
                gb_str = ",".join(gb)
            except Exception:
                gb_str = ""
            msg = (
                "cycle_done "
                f"envs={m.get('envs', 0)} "
                f"env_failures={m.get('env_failures', 0)} "
                f"global_classifier_sync_ok={m.get('global_classifier_sync_ok', 0)} "
                f"global_classifier_sync_fail={m.get('global_classifier_sync_fail', 0)} "
                f"global_builds_seen={gb_str} "
                f"running_scans_seen={m.get('running_scans_seen', 0)} "
                f"db_running_seen={m.get('db_running_seen', 0)} "
                f"index_pages={m.get('index_pages', 0)} "
                f"index_scans={m.get('index_scans', 0)} "
                f"insights_backfill_seen={m.get('insights_backfill_seen', 0)} "
                f"insights_backfill_ok={m.get('insights_backfill_ok', 0)} "
                f"insights_backfill_fail={m.get('insights_backfill_fail', 0)} "
                f"attempted_insights={m.get('attempted_insights', 0)} "
                f"snapshots_ok={m.get('snapshots_ok', 0)} "
                f"fail_auth={m.get('fail_auth', 0)} "
                f"fail_parent_scans={m.get('fail_parent_scans', 0)} "
                f"fail_insights={m.get('fail_insights', 0)} "
                f"fail_write={m.get('fail_write', 0)} "
                f"fail_attrs={m.get('fail_attrs', 0)}"
            )
        except Exception as ex:
            status = "error"
            m = {}
            msg = f"cycle_failed {ex}"
        finished = _now_utc_naive()
        _hb_update(engine, status=status, message=msg, finished=finished)
        elapsed = (finished - started).total_seconds()
        if not args.quiet:
            print(
                f"[{_now_utc_naive().isoformat()}Z] {msg} elapsed_s={int(elapsed)}",
                flush=True,
            )
        if args.once:
            return 0
        if args.align_10min:
            time.sleep(seconds_until_next_10min_boundary())
        else:
            time.sleep(max(5, int(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())

