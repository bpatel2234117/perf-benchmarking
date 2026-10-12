"""
Background scan progress tracker (no browser required).

Runs forever (default every 10 minutes):
- pulls "running" parent scans from each active environment
- fetches scan insights for each running scan
- writes a snapshot row into MySQL table `scan_progress`
- captures scanner-group + services-status metrics at the **same** `captured_at_utc` as that snapshot
  (once per environment per slot when multiple scans run in parallel)

Usage:
  python3 tracker.py
  python3 tracker.py --interval-seconds 600
  python3 tracker.py --once
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import time
import zipfile
from pathlib import Path
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Module logger. Previously many exceptions were swallowed with a bare `pass`; they
# now log at DEBUG with a traceback so failures are diagnosable without changing
# control flow (set level via the LOG_LEVEL env / logging config).
logger = logging.getLogger("tracker")

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import requests
from sqlalchemy import create_engine, text
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from db_config_pg import DB_CONNECTION_STRING
except ImportError:
    from v2_api.db_config_pg import DB_CONNECTION_STRING
try:
    from pg_compat import (
        is_postgresql,
        sql_auto_increment,
        sql_datetime,
        sql_longtext,
        sql_on_update_current_timestamp,
        sql_upsert_heartbeat,
    )
except ImportError:
    from v2_api.pg_compat import (
        is_postgresql,
        sql_auto_increment,
        sql_datetime,
        sql_longtext,
        sql_on_update_current_timestamp,
        sql_upsert_heartbeat,
    )
try:
    from bigid_auth import Get_auth_token
except ImportError:
    from v2_api.bigid_auth import Get_auth_token  # type: ignore
try:
    from scanner_performance import (
        capture_scanner_performance_for_env,
        ensure_scanner_performance_tables,
        build_individual_insert,
    )
except ImportError:
    from v2_api.scanner_performance import (  # type: ignore
        capture_scanner_performance_for_env,
        ensure_scanner_performance_tables,
        build_individual_insert,
    )

try:
    from v2_api.add_benchmark_math import (
        infer_add_benchmark_defaults_from_scan_name,
        infer_scan_benchmark_shape,
    )
    from v2_api.add_benchmark_prefetch import load_file_set_type_row
except ImportError:
    from add_benchmark_math import (  # type: ignore
        infer_add_benchmark_defaults_from_scan_name,
        infer_scan_benchmark_shape,
    )
    from add_benchmark_prefetch import load_file_set_type_row  # type: ignore

# Live logs integration: push to incident_intelligence
try:
    from incident_intelligence_pusher import get_pusher, push_to_incident_intelligence
except ImportError:
    try:
        from ..incident_intelligence_pusher import get_pusher, push_to_incident_intelligence
    except ImportError:
        # Fallback if module not available
        get_pusher = None
        push_to_incident_intelligence = None


# .env is mounted at the app root (/app/.env per docker-compose), not under v2_api/.
ENV_FILE_PATH = Path(__file__).parent.parent / ".env"
if not ENV_FILE_PATH.exists():
    _alt_env = Path(__file__).parent / ".env"
    if _alt_env.exists():
        ENV_FILE_PATH = _alt_env

_DOTENV_MAP: Dict[str, str] = {}
_DOTENV_MTIME: Optional[float] = None


def _read_dotenv_map() -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        if not ENV_FILE_PATH.exists():
            return values
        text_value = ENV_FILE_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return values
    for line in text_value.splitlines():
        ss = line.strip()
        if not ss or ss.startswith("#") or "=" not in ss:
            continue
        k, v = ss.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def _maybe_reload_dotenv() -> None:
    global _DOTENV_MAP, _DOTENV_MTIME
    try:
        st = ENV_FILE_PATH.stat()
        mtime = float(st.st_mtime)
    except Exception:
        return
    if _DOTENV_MTIME is None or (mtime != _DOTENV_MTIME):
        try:
            _DOTENV_MAP = _read_dotenv_map()
            _DOTENV_MTIME = mtime
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)


def _getenv_str(name: str, default: str = "") -> str:
    _maybe_reload_dotenv()
    v = str(os.getenv(name, "") or "").strip()
    if v:
        return v
    v2 = str(_DOTENV_MAP.get(name, "") or "").strip()
    return v2 or str(default or "")


PARENT_SCANS_PATH = os.getenv("BIGID_PARENT_SCANS_API_PATH", "/api/v1/scans/parent-scans").strip() or "/api/v1/scans/parent-scans"
INSIGHTS_PATH = os.getenv("BIGID_PARENT_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/parent-scans").strip() or "/api/v1/scans/insights/parent-scans"
CHILD_INSIGHTS_PATH = os.getenv("BIGID_CHILD_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/child-scans").strip() or "/api/v1/scans/insights/child-scans"
ABOUT_PATH = os.getenv("BIGID_ABOUT_API_PATH", "/api/v1/about").strip() or "/api/v1/about"
SCAN_PARTS_PATH = os.getenv(
    "BIGID_SCAN_PARTS_API_PATH",
    "/api/v1/download-scan-results-files/file-download/scan_parts",
).strip() or "/api/v1/download-scan-results-files/file-download/scan_parts"
# This is the raw-fetch safety limit. The UI still displays only five rows, but
# local parent/state filtering must see the complete collection response first.
MAX_SCAN_PART_RECORDS = 20000
SCANS_COLLECTION_PATH = os.getenv(
    "BIGID_SCANS_COLLECTION_API_PATH",
    "/api/v1/download-scan-results-files/file-download/scans",
).strip() or "/api/v1/download-scan-results-files/file-download/scans"
SCAN_PARTS_STATE = os.getenv("BIGID_SCAN_PARTS_STATE", "New").strip() or "New"
FILTER_INITIATED_SOURCE = str(os.getenv("BIGID_PARENT_SCANS_FILTER_INITIATED_SOURCE", "0") or "0").strip() in {"1", "true", "yes", "y"}
TRACKER_NAME = os.getenv("SCAN_TRACKER_NAME", "default").strip() or "default"
DEFAULT_CLASSIFIERS_API_PATH = (
    "/api/v1/all-classifications?skip=0&limit=600&requireTotalCount=true&"
    "filter=%5B%7B%22field%22%3A%22enabled%22%2C%22value%22%3A%5Btrue%5D%2C%22operator%22%3A%22in%22%7D%2C"
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

# BigID purges parent-scan insights after a retention window; asking for anything older
# just 404s forever and spams the BigID API logs. Only backfill scans inside this window.
try:
    INSIGHTS_RETENTION_DAYS = max(1, int(str(os.getenv("BIGID_INSIGHTS_RETENTION_DAYS", "14") or "14").strip()))
except Exception:
    INSIGHTS_RETENTION_DAYS = 14

# tracker_kv key prefix marking a scan whose insights BigID has already purged (404).
INSIGHTS_UNAVAILABLE_KV_PREFIX = "insights_unavailable::"


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


def _load_stored_scan_status(engine, scan_id: str) -> str:
    """Read the authoritative persisted status without calling BigID."""
    sid = str(scan_id or "").strip()
    if not sid:
        return ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT scan_status FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
            if row and str(row[0] or "").strip():
                return str(row[0]).strip()
            row = conn.execute(
                text("SELECT scan_state FROM scan_progress WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
            return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


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
    # insight_state must be scanInsights.state only (not scanProfile.progress.state); see _scan_row_from_insights.
    return a if _state_rank(a) >= _state_rank(b) else b


def _now_utc_naive() -> datetime:
    # Naive UTC datetime suitable for MySQL DATETIME
    return datetime.utcnow()


def _push_scan_progress_to_incident_intelligence(
    scan_id: str,
    scan_name: str,
    environment_id: int,
    scan_state: str,
    captured_at_utc: datetime,
    completed_sub: int = 0,
    failed_sub: int = 0,
    total_sub: int = 0,
    objects_scanned: int = 0,
    objects_failed: int = 0,
    total_classifications: int = 0,
) -> None:
    """
    Non-blocking push of scan_progress snapshot to incident_intelligence API.
    This is fire-and-forget - errors are logged but not raised.
    """
    if not push_to_incident_intelligence or not get_pusher:
        return  # Module not available
    
    try:
        pusher = get_pusher()
        if not pusher.should_push():
            return
        
        # Build progress row dict to transform
        progress_row = {
            'scan_id': scan_id,
            'scan_name': scan_name,
            'environment_id': environment_id,
            'scan_state': scan_state,
            'captured_at_utc': captured_at_utc,
            'completed_sub_scans': completed_sub,
            'failed_sub_scans': failed_sub,
            'total_sub_scans': total_sub,
            'objects_scanned': objects_scanned,
            'objects_failed': objects_failed,
            'total_classifications': total_classifications,
        }
        
        log_event = pusher.transform_progress_to_log(progress_row)
        push_to_incident_intelligence([log_event])
    except Exception as e:
        # Non-blocking: log but don't raise
        logger.debug(f"[WARN] Failed to push scan_progress to incident_intelligence: {e}")


def _slot_minutes() -> int:
    """Slot size in minutes for progress snapshots and main loop alignment.

    Controlled by TRACKER_SLOT_MINUTES (default 20).
    """
    try:
        m = int(_getenv_str('TRACKER_SLOT_MINUTES', '20') or '20')
    except Exception:
        m = 20
    if m <= 0:
        m = 20
    if m > 60:
        m = 60
    return m


def _fast_slot_minutes() -> int:
    """Initial polling slot after a scan reaches the near-complete threshold."""
    try:
        m = int(_getenv_str("TRACKER_FAST_SLOT_MINUTES", "5") or "5")
    except Exception:
        m = 5
    return max(2, min(m, 60))


def fast_poll_minutes_for_remaining(
    remaining_objects: Any,
    objects_per_minute: Any,
    initial_minutes: int = 5,
) -> int:
    """Choose a fast-poll interval from the current estimated time remaining."""
    try:
        remaining = float(remaining_objects)
        speed = float(objects_per_minute)
        initial = max(2, min(int(initial_minutes), 5))
        if not math.isfinite(remaining) or not math.isfinite(speed) or remaining < 0 or speed <= 0:
            return initial
        remaining_minutes = remaining / speed
    except (TypeError, ValueError, ZeroDivisionError):
        return max(2, min(int(initial_minutes or 5), 5))

    if remaining_minutes > 5:
        return initial
    if remaining_minutes > 3:
        return min(initial, 3)
    return 2


def scan_progress_reaches_fast_poll(
    *,
    objects_scanned: Any,
    object_target: Any = None,
    completed_parts: Any = None,
    total_parts: Any = None,
    completed_scan_parts: Any = None,
    total_scan_parts: Any = None,
    threshold: float = 0.9,
) -> bool:
    """Return whether object progress has reached the fast-poll threshold.

    Fast polling is based only on the configured total object target. Part
    counters describe different scan phases and can reach 90% while object
    scanning is still well below 90% of the dataset.
    """
    try:
        limit = min(max(float(threshold), 0.0), 1.0)
        objects = float(objects_scanned or 0)
        target = float(object_target or 0)
        return target > 0 and objects / target >= limit
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _scan_scenario_object_target(engine: Any, scan_id: str, scan_name: str) -> Optional[int]:
    """Resolve the configured scenario object total used by ETA/fast polling."""
    try:
        defaults = infer_add_benchmark_defaults_from_scan_name(scan_name)
        ds_type = str(defaults.get("data_source_type") or "").strip()
        shape = infer_scan_benchmark_shape(scan_name, ds_type)
        file_set = str(shape.get("file_set_type") or "").strip()
        preset = load_file_set_type_row(engine, file_set)
        if preset and preset.get("total_files") is not None:
            value = int(preset["total_files"])
            if value > 0:
                return value

        # For custom/mixed scenarios the BigID insight total is the best
        # available target and is normally populated during enumeration.
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT objects_enumerated FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                {"sid": str(scan_id)},
            ).fetchone()
        value = int(row[0]) if row and row[0] is not None else 0
        return value if value > 0 else None
    except Exception:
        return None


def _progress_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def load_fast_poll_environment_intervals(engine: Any, threshold: float = 0.9) -> Dict[Any, int]:
    """Return the calculated fast-poll interval for each qualifying environment."""
    fast_intervals: Dict[Any, int] = {}
    try:
        with engine.connect() as conn:
            parents = conn.execute(
                text(
                    """
                    SELECT scan_id, environment_id, scan_name
                    FROM scan_parents
                    WHERE environment_id IS NOT NULL
                      AND scan_status IN ('Started','Awaiting','In Progress','InProgress','Running')
                    """
                )
            ).fetchall()
            for parent in parents or []:
                sid = str(parent[0] or "").strip()
                if not sid:
                    continue
                progress_row = conn.execute(
                    text(
                        """
                        SELECT objects_scanned, completed_parts, total_parts_scanned,
                               completed_scan_parts, total_scan_parts
                        FROM scan_progress
                        WHERE scan_id = :sid
                        ORDER BY captured_at_utc DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": sid},
                ).fetchone()
                if not progress_row:
                    continue
                if scan_progress_reaches_fast_poll(
                    objects_scanned=progress_row[0],
                    object_target=target,
                    completed_parts=progress_row[1],
                    total_parts=progress_row[2],
                    completed_scan_parts=progress_row[3],
                    total_scan_parts=progress_row[4],
                    threshold=threshold,
                ):
                    env_id = parent[1]
                    # The threshold is based on the latest completed/total snapshot only.
                    # Once reached, use the fixed five-minute fast-poll cadence; do not use
                    # the previous snapshot to change the interval to 2 or 3 minutes.
                    fast_intervals[env_id] = _fast_slot_minutes()
    except Exception:
        logger.debug("tracker: fast-poll state lookup failed", exc_info=True)
    return fast_intervals


def load_fast_poll_environment_ids(engine: Any, threshold: float = 0.9) -> set[Any]:
    """Return active environment IDs containing a scan at the fast-poll threshold."""
    return set(load_fast_poll_environment_intervals(engine, threshold=threshold))


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


def _te_is_active_sql(engine, alias: str = "te") -> str:
    """PostgreSQL uses BOOLEAN; `is_active = 1` matches zero rows and breaks workflow tracking."""
    from pg_compat import sql_truthy

    return sql_truthy(engine, f"{alias}.is_active")


def _ws_is_enabled_sql(engine) -> str:
    from pg_compat import sql_truthy

    return sql_truthy(engine, "ws.is_enabled")


def resolve_scan_status_value(scan: Dict[str, Any]) -> str:
    if not isinstance(scan, dict):
        return ""
    top = str(scan.get("state") or scan.get("status") or "").strip()
    if top:
        return top
    progress = scan.get("progress")
    if isinstance(progress, dict):
        return str(progress.get("state") or "").strip()
    return ""


def ensure_scan_tracking_tables(engine) -> None:
    dt = sql_datetime(engine)
    ai = sql_auto_increment(engine)
    lt = sql_longtext(engine)
    ou = sql_on_update_current_timestamp(engine)
    
    with engine.connect() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS tracker_heartbeat (
                name VARCHAR(64) PRIMARY KEY,
                last_started_at_utc {dt} NULL,
                last_finished_at_utc {dt} NULL,
                last_status VARCHAR(16) NULL,
                last_message VARCHAR(2000) NULL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS tracker_kv (
                k VARCHAR(200) PRIMARY KEY,
                v VARCHAR(2000) NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scan_parents (
                id {ai} PRIMARY KEY,
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou},
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scan_insights (
                id {ai} PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL UNIQUE,
                distinct_classification_attributes BIGINT NULL,
                classifier_attribute_names {lt} NULL,
                data_source_names {lt} NULL,
                objects_with_pi BIGINT NULL,
                objects_scanned BIGINT NULL,
                objects_enumerated BIGINT NULL,
                total_classification BIGINT NULL,
                new_attributes_count BIGINT NULL,
                removed_attributes_count BIGINT NULL,
                total_size_gb FLOAT NULL,
                insight_payload {lt} NULL,
                attributes_payload {lt} NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scan_part_records (
                id {ai} PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL,
                environment_id INT NULL,
                collection VARCHAR(64) NOT NULL DEFAULT 'scan_parts',
                subscan_id VARCHAR(128) NULL,
                state VARCHAR(64) NOT NULL,
                record_key VARCHAR(191) NOT NULL,
                record_payload {lt} NOT NULL,
                captured_at_utc {dt} NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (scan_id, record_key)
            )
        """))
        try:
            conn.execute(text("ALTER TABLE scan_part_records ADD COLUMN collection VARCHAR(64) NOT NULL DEFAULT 'scan_parts'"))
            conn.commit()
        except Exception:
            logger.debug("tracker: scan_part_records collection column already exists", exc_info=True)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_scan_part_records_scan_id ON scan_part_records(scan_id)",
            "CREATE INDEX IF NOT EXISTS idx_scan_part_records_subscan_id ON scan_part_records(subscan_id)",
            "CREATE INDEX IF NOT EXISTS idx_scan_part_records_collection ON scan_part_records(scan_id, collection)",
        ]:
            try:
                conn.execute(text(idx_sql))
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
        # Non-breaking schema upgrades (in case table existed with fewer columns)
        for stmt in [
            "ALTER TABLE scan_insights ADD COLUMN new_attributes_count BIGINT NULL",
            "ALTER TABLE scan_insights ADD COLUMN removed_attributes_count BIGINT NULL",
            "ALTER TABLE scan_insights ADD COLUMN total_size_gb FLOAT NULL",
            f"ALTER TABLE scan_insights ADD COLUMN insight_payload {lt} NULL",
            f"ALTER TABLE scan_insights ADD COLUMN attributes_payload {lt} NULL",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)

        # Tables used by Add New Benchmark "Findings Files" prefill
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scan_classifier_attributes (
                id {ai} PRIMARY KEY,
                name VARCHAR(500) NOT NULL UNIQUE
            )
        """))
        conn.execute(text(f"""
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
                logger.debug("tracker: suppressed exception", exc_info=True)
        conn.commit()

        # Auto scan benchmarks table (used by dashboard trend/compare pages).
        # Tracker does NOT create rows here (dashboard does today), but we may update
        # completed scans with computed throughputs (best-effort).
        try:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS auto_scan_benchmarks (
                    id {ai} PRIMARY KEY,
                    scan_id VARCHAR(128) NOT NULL UNIQUE,
                    environment_id INT NULL,
                    build_version VARCHAR(100) NULL,
                    run_date DATE NULL,
                    scenario_type VARCHAR(100) NULL,
                    scan_type VARCHAR(100) NULL,
                    data_source_type VARCHAR(100) NULL,
                    data_type VARCHAR(50) NULL,
                    file_set_type VARCHAR(150) NULL,
                    scan_start_time {dt} NULL,
                    scan_end_time {dt} NULL,
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
                    classifier_attribute_names {lt} NULL,
                    scan_name VARCHAR(1000) NULL,
                    throughput_cluster_gb_hr FLOAT NULL,
                    throughput_cluster_files_hr FLOAT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
                )
            """))
            conn.commit()
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)

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
                logger.debug("tracker: suppressed exception", exc_info=True)
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
                    logger.debug("tracker: suppressed exception", exc_info=True)

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
                text(sql_upsert_heartbeat(engine)),
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
        logger.debug("tracker: suppressed exception", exc_info=True)


def _kv_get(engine, key: str) -> str:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT v FROM tracker_kv WHERE k = :k LIMIT 1"), {"k": str(key)}).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _kv_set(engine, key: str, value: str, expire_seconds: int = 0) -> None:
    try:
        with engine.connect() as conn:
            if is_postgresql(engine):
                conn.execute(
                    text("""
                        INSERT INTO tracker_kv (k, v, updated_at)
                        VALUES (:k, :v, CURRENT_TIMESTAMP)
                        ON CONFLICT(k) DO UPDATE SET
                            v = EXCLUDED.v,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {"k": str(key), "v": str(value)},
                )
            else:
                # MySQL
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
        logger.debug("tracker: suppressed exception", exc_info=True)


def _kv_del(engine, key: str) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM tracker_kv WHERE k = :k"), {"k": str(key)})
            conn.commit()
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)


def ensure_scan_progress_table(engine) -> None:
    dt = sql_datetime(engine)
    ai = sql_auto_increment(engine)
    
    with engine.connect() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scan_progress (
                id {ai} PRIMARY KEY,
                scan_id VARCHAR(128) NOT NULL,
                scan_name VARCHAR(1000) NULL,
                environment_id INT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                captured_at_utc {dt} NULL,
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
                objects_with_pi BIGINT DEFAULT 0
            )
        """))
        # Create indexes separately for Postgres compatibility
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_scan_id ON scan_progress(scan_id)",
            "CREATE INDEX IF NOT EXISTS idx_timestamp ON scan_progress(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_captured_at_utc ON scan_progress(captured_at_utc)",
            "CREATE INDEX IF NOT EXISTS idx_state ON scan_progress(scan_state)",
        ]:
            try:
                conn.execute(text(idx_sql))
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
                
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scanner_group_performance (
                id {ai} PRIMARY KEY,
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
            )
        """))
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_env_time_group ON scanner_group_performance(environment_id, captured_at_utc)"))
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
            
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS scanner_individual_performance (
                id {ai} PRIMARY KEY,
                environment_id INT NOT NULL,
                captured_at_utc {dt} NOT NULL,
                scanner_id VARCHAR(255) NULL,
                name VARCHAR(255) NULL,
                status VARCHAR(64) NULL,
                group_name VARCHAR(255) NULL,
                running_threads INT DEFAULT 0,
                total_num_of_threads INT DEFAULT 0,
                system_load_average FLOAT DEFAULT 0,
                physical_memory_mb BIGINT DEFAULT 0,
                free_memory_mb BIGINT DEFAULT 0,
                scanner_threads INT DEFAULT 0,
                update_date_bigid {dt} NULL
            )
        """))
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_env_time_scanner ON scanner_individual_performance(environment_id, captured_at_utc, scanner_id)"))
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
        for stmt in (
            "ALTER TABLE scanner_individual_performance ADD COLUMN IF NOT EXISTS name VARCHAR(255) NULL",
            "ALTER TABLE scanner_individual_performance ADD COLUMN IF NOT EXISTS cpu_pct DOUBLE PRECISION NULL",
            "ALTER TABLE scanner_individual_performance ADD COLUMN IF NOT EXISTS available_processors INT NULL",
            # BigID service status per scanner — lets the UI show a failed pod as failed
            # instead of inferring health from a frozen thread count.
            "ALTER TABLE scanner_individual_performance ADD COLUMN IF NOT EXISTS status VARCHAR(64) NULL",
            "ALTER TABLE scanner_group_performance ADD COLUMN IF NOT EXISTS group_scanner_ids_json TEXT NULL",
        ):
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
        # schema upgrades for existing DBs
        # NOTE: MODIFY COLUMN removed — it rebuilds the full table and blocks all readers.
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
                logger.debug("tracker: suppressed exception", exc_info=True)

        # De-dupe existing rows (keep highest id per scan_id + captured_at slot)
        # and enforce uniqueness so we cannot insert duplicates again.
        if not is_postgresql(engine):
            # MySQL de-dupe
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
                logger.debug("tracker: suppressed exception", exc_info=True)
        else:
            # PostgreSQL de-dupe
            try:
                conn.execute(text("""
                    DELETE FROM scan_progress sp1
                    USING scan_progress sp2
                    WHERE sp1.scan_id = sp2.scan_id
                      AND sp1.captured_at_utc IS NOT NULL
                      AND sp2.captured_at_utc IS NOT NULL
                      AND sp1.captured_at_utc = sp2.captured_at_utc
                      AND sp1.id < sp2.id
                """))
                conn.commit()
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
        
        # Add unique constraint
        try:
            if is_postgresql(engine):
                conn.execute(text("ALTER TABLE scan_progress ADD CONSTRAINT uniq_scan_slot UNIQUE (scan_id, captured_at_utc)"))
            else:
                conn.execute(text("ALTER TABLE scan_progress ADD UNIQUE KEY uniq_scan_slot (scan_id, captured_at_utc)"))
            conn.commit()
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
        conn.commit()
    try:
        ensure_scanner_performance_tables(engine)
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)


def load_active_environments(engine) -> List[Dict[str, Any]]:
    # NB: do NOT use pd.read_sql(text(...), conn) here — pandas 2.x raises
    # "Query must be a string unless using sqlalchemy." for a TextClause + connection,
    # and a bare except used to swallow it → envs=0 every cycle (tracker blind to BigID
    # discovery, only picks scans up via the delayed DB safety net). See
    # tracker-envs-zero-root-cause memo. Use conn.execute + SQL-side is_active filter.
    active_sql = _te_is_active_sql(engine, "test_environments")
    cols = [
        "id", "env_name", "base_url", "version",
        "auth_module_path", "auth_function_name", "classifiers_api_path", "is_active",
    ]
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT {", ".join(cols)}
                FROM test_environments
                WHERE {active_sql}
            """)).mappings().all()
    except Exception:
        logger.exception("load_active_environments failed")
        return []
    return [{k: ("" if r.get(k) is None else r.get(k)) for k in cols} for r in rows]

def load_running_scan_env_rows(engine) -> List[Dict[str, Any]]:
    """
    DB-driven list of running scans we should definitely track.
    Returns rows with: scan_id, scan_name, environment_id, env_name, base_url
    """
    if not engine:
        return []
    active_sql = _te_is_active_sql(engine, "te")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    sp.scan_id,
                    sp.scan_name,
                    sp.environment_id,
                    te.env_name,
                    te.base_url
                FROM scan_parents sp
                JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.environment_id IS NOT NULL
                  AND {active_sql}
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


def load_workflow_running_scan_env_rows(engine) -> List[Dict[str, Any]]:
    """
    Safety net: scans started by workflow_runner may not be present in scan_parents yet.
    Track any scan_id referenced by workflow_steps with status='scan_running'.
    Returns rows with: scan_id, scan_name, environment_id, env_name, base_url
    """
    if not engine:
        return []
    active_sql = _te_is_active_sql(engine, "te")
    enabled_sql = _ws_is_enabled_sql(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    ws.scan_id,
                    COALESCE(ws.scan_profile_name, ws.ds_name, '') AS scan_name,
                    ws.environment_id,
                    te.env_name,
                    te.base_url
                FROM workflow_steps ws
                JOIN test_environments te ON te.id = ws.environment_id
                WHERE {active_sql}
                  AND {enabled_sql}
                  AND COALESCE(ws.status,'') = 'scan_running'
                  AND ws.scan_id IS NOT NULL AND TRIM(ws.scan_id) <> ''
                  -- Manually marked stopped (mark-stopped): the step can stay scan_running, but
                  -- the tracker must not keep polling insights for it.
                  AND NOT EXISTS (
                      SELECT 1 FROM scan_parents sp
                      WHERE sp.scan_id = ws.scan_id AND sp.manually_stopped_at IS NOT NULL
                  )
            """)).fetchall()
        out = []
        for r in rows:
            out.append({
                'scan_id': str(r[0] or '').strip(),
                'scan_name': str(r[1] or '').strip(),
                'environment_id': r[2],
                'env_name': str(r[3] or '').strip(),
                'base_url': str(r[4] or '').strip(),
                'scan_status': 'In Progress',
            })
        return [x for x in out if x.get('scan_id') and x.get('base_url')]
    except Exception:
        return []


def count_workflow_scan_running_steps(engine) -> int:
    """Enabled scan_running steps on active workflow runs (ground truth for queue audit)."""
    enabled_sql = _ws_is_enabled_sql(engine)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) FROM workflow_steps ws
                    INNER JOIN workflow_runs wr ON wr.id = ws.run_id
                    WHERE LOWER(TRIM(COALESCE(wr.status,''))) = 'running'
                      AND {enabled_sql}
                      AND LOWER(TRIM(COALESCE(ws.status,''))) = 'scan_running'
                      AND ws.scan_id IS NOT NULL AND TRIM(COALESCE(ws.scan_id,'')) <> ''
                    """
                )
            ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return -1


def audit_tracker_queues(engine) -> Dict[str, Any]:
    """
    Preflight / health: catch dead tracker queues before hours of silent failure.
    Reports how many envs the tracker will actually poll, plus the workflow scan queue state.
    """
    out: Dict[str, Any] = {
        "ok": True,
        "postgres": bool(is_postgresql(engine)),
        "is_active_sql": _te_is_active_sql(engine, "te"),
        "errors": [],
        "warnings": [],
    }

    if is_postgresql(engine):
        # The probe used to compare "legacy" vs "fixed" SQL, but both sides were built by the same
        # helper (so the comparison could never fire) and neither aliased the table, so every cycle
        # logged `missing FROM-clause entry for table "te"`. Just count what the tracker will
        # actually iterate: zero active envs is the real actionable signal.
        try:
            with engine.connect() as conn:
                n_active = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM test_environments te "
                        f"WHERE {_te_is_active_sql(engine, 'te')}"
                    )
                ).scalar()
            n_active_i = int(n_active or 0)
            out["active_envs"] = n_active_i
            if n_active_i == 0:
                out["warnings"].append(
                    "No active environments — the tracker has nothing to poll "
                    "(activate an environment on the Environments page)"
                )
        except Exception as ex:
            out["errors"].append(f"PostgreSQL is_active probe failed: {ex}")
            out["ok"] = False

    wf_steps = count_workflow_scan_running_steps(engine)
    out["workflow_scan_running_steps"] = wf_steps
    try:
        wf_queue = load_workflow_running_scan_env_rows(engine)
    except Exception as ex:
        wf_queue = []
        out["errors"].append(f"load_workflow_running_scan_env_rows failed: {ex}")
        out["ok"] = False
    out["workflow_queue_rows"] = len(wf_queue or [])

    if wf_steps > 0 and out["workflow_queue_rows"] == 0:
        out["errors"].append(
            f"TRACKER_QUEUE_DEAD: {wf_steps} workflow step(s) scan_running with scan_id but "
            "workflow queue returned 0 rows"
        )
        out["ok"] = False
    elif wf_steps > out["workflow_queue_rows"]:
        out["warnings"].append(
            f"workflow queue partial: {out['workflow_queue_rows']}/{wf_steps} scan_running steps enqueued "
            "(check environment join / is_active / include_in_workflow_runs)"
        )

    try:
        out["db_running_queue_rows"] = len(load_running_scan_env_rows(engine) or [])
    except Exception as ex:
        out["db_running_queue_rows"] = -1
        out["warnings"].append(f"load_running_scan_env_rows failed: {ex}")

    return out


def _emit_tracker_queue_audit(engine, metrics: Dict[str, Any]) -> None:
    try:
        audit = audit_tracker_queues(engine)
        metrics["tracker_audit_ok"] = bool(audit.get("ok", True))
        metrics["workflow_scan_running_steps"] = audit.get("workflow_scan_running_steps", 0)
        metrics["workflow_queue_rows"] = audit.get("workflow_queue_rows", 0)
        if not audit.get("ok"):
            for err in audit.get("errors") or []:
                print(f"[{_now_utc_naive().isoformat()}Z] TRACKER_AUDIT_FAIL: {err}", flush=True)
        for warn in audit.get("warnings") or []:
            print(f"[{_now_utc_naive().isoformat()}Z] TRACKER_AUDIT_WARN: {warn}", flush=True)
    except Exception as ex:
        print(f"[{_now_utc_naive().isoformat()}Z] TRACKER_AUDIT_ERROR: {ex}", flush=True)


def load_scans_missing_terminal_progress_row(engine, lookback_hours: int = 12, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Final-row safety net: scans that BigID reports as terminal (Completed/Failed/etc.) in
    scan_parents but for which scan_progress has NO terminal-state row yet.

    This happens because the main job list only tracks *running* scans. When a scan finishes,
    whoever marks it terminal first (workflow runner or a prior tracker cycle) removes it from
    every "running" source before the tracker writes the terminal scan_progress snapshot — so
    the closing 'Completed' row is missing. Re-processing these scans once backfills that row.
    """
    if not engine:
        return []
    terminal_csv = ",".join(f"'{s}'" for s in TERMINAL_STATES)
    try:
        cutoff = _now_utc_naive() - timedelta(hours=max(1, int(lookback_hours)))
    except Exception:
        cutoff = _now_utc_naive() - timedelta(hours=12)
    active_sql = _te_is_active_sql(engine, "te")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    sp.scan_id,
                    sp.scan_name,
                    sp.environment_id,
                    te.env_name,
                    te.base_url,
                    sp.scan_status
                FROM scan_parents sp
                JOIN test_environments te ON te.id = sp.environment_id
                WHERE sp.environment_id IS NOT NULL
                  AND {active_sql}
                  AND sp.scan_status IN ({terminal_csv})
                  AND COALESCE(sp.updated_at, sp.last_seen_at, sp.created_at) >= :cutoff
                  AND NOT EXISTS (
                        SELECT 1 FROM scan_progress pr
                        WHERE pr.scan_id = sp.scan_id
                          AND pr.scan_state IN ({terminal_csv})
                  )
                ORDER BY COALESCE(sp.updated_at, sp.last_seen_at, sp.created_at) DESC
                LIMIT :lim
            """), {"cutoff": cutoff, "lim": int(limit)}).fetchall()
        out = []
        for r in rows:
            out.append({
                "scan_id": str(r[0] or "").strip(),
                "scan_name": str(r[1] or "").strip(),
                "environment_id": r[2],
                "env_name": str(r[3] or "").strip(),
                "base_url": str(r[4] or "").strip(),
                "scan_status": str(r[5] or "").strip(),
            })
        return [x for x in out if x.get("scan_id") and x.get("base_url")]
    except Exception:
        return []


def _request_get(
    url: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout_seconds: int = 20,
) -> requests.Response:
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}

    def request_with_retry(headers: Dict[str, str]) -> requests.Response:
        for attempt in range(3):
            try:
                return requests.get(url, params=params or {}, headers=headers, timeout=int(timeout_seconds), verify=False)
            except Exception:
                if attempt == 2:
                    raise
                logger.debug("tracker: GET transport failed; retrying attempt %s", attempt + 2, exc_info=True)
                time.sleep(0.5)
        raise RuntimeError("unreachable")

    resp = request_with_retry(headers_raw)
    if resp.status_code in (401, 403) and token:
        resp = request_with_retry(headers_bearer)
    return resp


_SUBSCAN_ID_KEYS = {
    "subscan_id",
    "subscanid",
    "sub_scan_id",
}
_SUBSCAN_CONTAINER_KEYS = {
    "subscan",
    "subscans",
    "sub_scan",
    "sub_scans",
    "subscanlist",
    "subscan_list",
    "subscandetails",
    "subscan_details",
}


def _record_value(record: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return record.get(key)
    return None


def extract_subscan_ids(payload: Any) -> List[str]:
    """Find subscan IDs in the varying parent/insights response shapes."""
    found: set[str] = set()

    def add_subscan_id(value: Any) -> None:
        if isinstance(value, dict):
            value = _record_value(
                value,
                ("id", "_id", "subscan_id", "subscanId", "scan_id", "scanId"),
            )
        sid = str(value or "").strip()
        if sid:
            found.add(sid)

    def visit_subscan_container(value: Any) -> None:
        if isinstance(value, dict):
            add_subscan_id(value)
            for child in value.values():
                visit_subscan_container(child)
        elif isinstance(value, list):
            for child in value:
                visit_subscan_container(child)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).replace("-", "_").lower()
                if normalized_key in _SUBSCAN_ID_KEYS:
                    values = child if isinstance(child, list) else [child]
                    for item in values:
                        add_subscan_id(item)
                elif normalized_key in _SUBSCAN_CONTAINER_KEYS:
                    visit_subscan_container(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sorted(found)


def _extract_scan_part_items(payload: Any) -> List[Dict[str, Any]]:
    """Extract records from the common BigID wrapper shapes."""
    preferred = {
        "scanparts", "scan_parts", "parts", "items", "results", "files",
        "records", "data", "scanresultfiles", "scan_result_files",
    }

    def visit(value: Any, prefer_keys: bool = False) -> List[Dict[str, Any]]:
        if isinstance(value, list):
            records = [item for item in value if isinstance(item, dict)]
            if records:
                return records
            for item in value:
                result = visit(item)
                if result:
                    return result
            return []
        if not isinstance(value, dict):
            return []
        for key, child in value.items():
            if str(key).replace("-", "_").lower() in preferred:
                result = visit(child, prefer_keys=True)
                if result:
                    return result
        for child in value.values():
            result = visit(child)
            if result:
                return result
        return []

    return visit(payload)


def _response_json_payload(response: requests.Response) -> Any:
    """Decode BigID collection responses, which are JSON or ZIP-wrapped JSON exports."""
    try:
        return response.json()
    except Exception as json_error:
        content = getattr(response, "content", b"") or b""
        if not isinstance(content, (bytes, bytearray)) or not content.startswith(b"PK"):
            raise json_error
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = [
                    name for name in archive.namelist()
                    if not name.endswith("/") and name.lower().endswith((".json", ".jsonl"))
                ]
                if not members:
                    raise ValueError("BigID ZIP response contains no JSON member")
                for member in members:
                    raw = archive.read(member)
                    if member.lower().endswith(".jsonl"):
                        return [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
                    return json.loads(raw.decode("utf-8-sig"))
        except Exception:
            raise json_error


def filter_scan_part_records(
    records: List[Dict[str, Any]],
    scan_id: str = "",
    subscan_ids: Optional[List[str]] = None,
    state: Optional[str] = SCAN_PARTS_STATE,
) -> List[Dict[str, Any]]:
    """Keep records for the requested subscans, optionally restricted by state."""
    wanted_subscans = {str(item).strip() for item in (subscan_ids or []) if str(item).strip()}
    wanted_state = str(state).strip().casefold() if state is not None and str(state).strip() else None
    parent_scan_id = str(scan_id or "").strip()
    filtered: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_state = str(_record_value(record, ("state", "status")) or "").strip().casefold()
        if wanted_state is not None and record_state != wanted_state:
            continue
        record_subscan = str(_record_value(record, ("subscan_id", "subscanId", "sub_scan_id")) or "").strip()
        record_scan_candidate = str(_record_value(record, ("scan_id", "scanId")) or "").strip()
        record_parent = str(_record_value(record, ("parent_scan_id", "parentScanId")) or "").strip()
        if not record_subscan and record_scan_candidate in wanted_subscans:
            # BigID's scan_parts export sometimes stores the subscan relationship
            # in scan_id instead of emitting a separate subscan_id field.
            record_subscan = record_scan_candidate
        parent_matches = bool(parent_scan_id and record_parent == parent_scan_id)
        # A supplied subscan list is an allow-list. An empty list therefore keeps
        # nothing; this prevents accidentally storing records that cannot be
        # linked to one of the current scan's subscans.
        if subscan_ids is not None and record_subscan not in wanted_subscans:
            continue
        record_scan = str(_record_value(record, ("scan_id", "scanId")) or "").strip()
        if (
            parent_scan_id
            and not parent_matches
            and record_scan != parent_scan_id
            and record_scan not in wanted_subscans
            and record_subscan not in wanted_subscans
        ):
            continue
        filtered.append(record)
    return sorted(
        filtered,
        key=lambda row: (
            str(_record_value(row, ("subscan_id", "subscanId", "sub_scan_id")) or "").casefold(),
            str(_record_value(row, ("state", "status")) or "").casefold(),
            _scan_part_record_key(row),
        ),
    )


def _scan_parts_filter(
    subscan_ids: Optional[List[str]] = None,
    *,
    scan_id: str = "",
    state: Optional[str] = SCAN_PARTS_STATE,
) -> str:
    """Build a filter for diagnostics only; BigID scan_parts does not support it."""
    values = [str(item).strip() for item in (subscan_ids or []) if str(item).strip()]
    parent_scan_id = str(scan_id or "").strip()
    wanted_state = str(state or "").strip()
    items: List[Dict[str, Any]] = []
    # BigID's scan_parts response is scoped by parent_scan_id. Do not add the
    # subscan allow-list to this server-side filter: some versions omit
    # subscan_id even though parent_scan_id is present, and would return zero
    # rows for an otherwise valid parent/state query.
    if parent_scan_id:
        items.append({"field": "parent_scan_id", "value": [parent_scan_id], "operator": "in"})
    elif subscan_ids is not None:
        items.append({"field": "subscan_id", "value": values, "operator": "in"})
    if wanted_state:
        items.append({"field": "state", "value": [wanted_state], "operator": "in"})
    return json.dumps(items, separators=(",", ":"))


def fetch_scan_part_records(
    base_url: str,
    scan_id: str,
    token: str,
    subscan_ids: Optional[List[str]] = None,
    *,
    limit: int = 20000,
    timeout_seconds: int = 20,
    state: Optional[str] = SCAN_PARTS_STATE,
) -> List[Dict[str, Any]]:
    """Fetch raw scan-part files, then enforce parent/state filters locally."""
    url = build_api_url(base_url, SCAN_PARTS_PATH)
    max_records = min(MAX_SCAN_PART_RECORDS, max(1, int(limit)))
    page_size = min(500, max_records)
    sort = json.dumps(
        [
            {"field": "subscan_id", "order": "asc"},
            {"field": "state", "order": "asc"},
            {"field": "_id", "order": "asc"},
        ],
        separators=(",", ":"),
    )
    records: List[Dict[str, Any]] = []
    skip = 0
    seen_page_signatures: set[str] = set()
    while skip < max_records:
        params = {
            "skip": skip,
            "limit": page_size,
            "requireTotalCount": "true",
            "sort": sort,
        }
        resp = _request_get(url, token, params=params, timeout_seconds=int(timeout_seconds))
        if resp.status_code != 200:
            print(f"[SCAN_PARTS_API] status={resp.status_code} response={resp.text[:200]}", flush=True)
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Unauthorized scan parts API: {resp.status_code}")
        resp.raise_for_status()
        page = _extract_scan_part_items(_response_json_payload(resp))
        if not page:
            break
        # Some BigID versions cap the response below the requested page size;
        # use the returned page length for the next offset. Other versions
        # ignore pagination and return the same complete export each time.
        # Stop on a repeated page to avoid duplicate requests in that case.
        page_signature = hashlib.sha256(
            json.dumps(page, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        if page_signature in seen_page_signatures:
            break
        seen_page_signatures.add(page_signature)
        records.extend(page)
        # The download endpoint may ignore limit=500 and return the complete
        # ZIP export. Do not request that same full export again with new skip
        # values; the local filters can operate on this response directly.
        if len(page) > page_size:
            break
        skip += len(page)
    filtered = filter_scan_part_records(records, scan_id=scan_id, subscan_ids=subscan_ids, state=state)
    return filtered[:max_records]


def _scan_collection_filter(scan_id: str) -> str:
    return json.dumps(
        [{"field": "_id", "value": [str(scan_id).strip()], "operator": "in"}],
        separators=(",", ":"),
    )


def filter_scan_collection_records(records: List[Dict[str, Any]], scan_id: str) -> List[Dict[str, Any]]:
    """Keep only the exact current scan record from the full ``scans`` export."""
    wanted = str(scan_id or "").strip()
    if not wanted:
        return []
    filtered = []
    for record in records:
        if not isinstance(record, dict):
            continue
        # BigID returns parent and child rows together. The current scan is
        # identified only by its own `_id`; parent linkage must not pull child
        # or historical scan rows into the current-scan panel.
        record_id = str(record.get("_id") or "").strip()
        if record_id == wanted:
            filtered.append(record)
    return sorted(filtered, key=_scan_part_record_key)


def fetch_scan_collection_records(
    base_url: str,
    scan_id: str,
    token: str,
    *,
    limit: int = 20000,
    timeout_seconds: int = 20,
) -> List[Dict[str, Any]]:
    """Fetch the full `scans` collection in pages and keep only ``_id == scan_id``."""
    url = build_api_url(base_url, SCANS_COLLECTION_PATH)
    max_records = max(1, int(limit))
    page_size = min(500, max_records)
    sort = json.dumps([{"field": "_id", "order": "asc"}], separators=(",", ":"))
    records: List[Dict[str, Any]] = []
    for skip in range(0, max_records, page_size):
        params = {
            "skip": skip,
            "limit": page_size,
            "requireTotalCount": "true",
            "sort": sort,
        }
        resp = _request_get(url, token, params=params, timeout_seconds=int(timeout_seconds))
        if resp.status_code != 200:
            print(f"[SCANS_API] status={resp.status_code} response={resp.text[:200]}", flush=True)
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Unauthorized scans collection API: {resp.status_code}")
        resp.raise_for_status()
        page = _extract_scan_part_items(_response_json_payload(resp))
        records.extend(page)
        # The download endpoint may ignore limit=500 and return the complete
        # ZIP export. Do not request that same full export again with new skip
        # values; the local filters can operate on this response directly.
        if len(page) != page_size:
            break
    return filter_scan_collection_records(records, scan_id)


def _scan_part_record_key(record: Dict[str, Any]) -> str:
    value = _record_value(
        record,
        ("_id", "id", "file_id", "fileId", "path", "file_path", "filePath", "name", "file_name", "fileName"),
    )
    if value not in (None, ""):
        return str(value).strip()[:191]
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def upsert_scan_part_records(
    engine,
    scan_id: str,
    environment_id: Any,
    records: List[Dict[str, Any]],
    collection: str = "scan_parts",
) -> int:
    """Replace one scan's stored filtered records and return the current count."""
    sid = str(scan_id or "").strip()
    if not sid:
        return 0
    source_collection = str(collection or "scan_parts").strip()[:64] or "scan_parts"
    captured_at = _now_utc_naive()
    seen: set[str] = set()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM scan_part_records WHERE scan_id = :sid AND collection = :collection"),
            {"sid": sid, "collection": source_collection},
        )
        for record in records:
            if not isinstance(record, dict):
                continue
            key = f"{source_collection}:{_scan_part_record_key(record)}"[:191]
            if key in seen:
                continue
            seen.add(key)
            subscan_id = str(_record_value(record, ("subscan_id", "subscanId", "sub_scan_id")) or "").strip()
            if not subscan_id:
                # Some BigID scan_parts exports use scan_id for the child scan
                # relationship instead of emitting subscan_id.
                subscan_id = str(_record_value(record, ("scan_id", "scanId")) or "").strip()
            subscan_id = subscan_id or None
            state = str(_record_value(record, ("state", "status")) or SCAN_PARTS_STATE).strip()
            conn.execute(
                text("""
                    INSERT INTO scan_part_records
                        (scan_id, environment_id, collection, subscan_id, state, record_key, record_payload, captured_at_utc)
                    VALUES (:sid, :eid, :collection, :subsid, :state, :rkey, :payload, :captured)
                """),
                {
                    "sid": sid,
                    "eid": int(environment_id) if environment_id is not None else None,
                    "collection": source_collection,
                    "subsid": subscan_id,
                    "state": state[:64],
                    "rkey": key,
                    "payload": json.dumps(record, ensure_ascii=False, default=str),
                    "captured": captured_at,
                },
            )
    return len(seen)


def sync_scan_part_records_for_scan(
    engine,
    base_url: str,
    environment: Dict[str, Any],
    token: str,
    scan_id: str,
    *payloads: Any,
    scan_status: Optional[str] = None,
    allow_terminal: bool = False,
    collection: str = "all",
) -> int:
    """Fetch and persist one or both collection snapshots."""
    collection_scope = str(collection or "all").strip().lower()
    if collection_scope not in {"all", "scan_parts", "scans"}:
        raise ValueError("collection must be one of: all, scan_parts, scans")
    effective_status = str(scan_status or "").strip() or _load_stored_scan_status(engine, scan_id)
    if not allow_terminal and not _is_running_state(effective_status):
        # Automatic tracker passes must not fetch terminal/unknown scans.
        return 0
    # This also makes manual Sync now work on databases created before this feature.
    # The scan_parts response itself is authoritative for subscan_id; do not
    # derive the allow-list from an assumed insight payload shape.
    ensure_scan_tracking_tables(engine)
    total = 0
    if collection_scope in {"all", "scans"}:
        try:
            scan_records = fetch_scan_collection_records(base_url, scan_id, token)
            total += upsert_scan_part_records(engine, scan_id, environment.get("id"), scan_records, "scans")
        except Exception:
            logger.debug("tracker: scans collection fetch failed", exc_info=True)
    if collection_scope in {"all", "scan_parts"}:
        try:
            # Always take both snapshots. The New-filtered snapshot shows active
            # scan parts while the unfiltered snapshot is the comparison/debug
            # view that exposes any returned terminal states as well.
            new_part_records = fetch_scan_part_records(
                base_url,
                scan_id,
                token,
                state=SCAN_PARTS_STATE,
            )
            all_part_records = fetch_scan_part_records(
                base_url,
                scan_id,
                token,
                state=None,
            )
            merged_part_records: List[Dict[str, Any]] = []
            seen_part_keys: set[str] = set()
            for record in [*new_part_records, *all_part_records]:
                key = _scan_part_record_key(record)
                if key in seen_part_keys:
                    continue
                seen_part_keys.add(key)
                merged_part_records.append(record)
            total += upsert_scan_part_records(
                engine, scan_id, environment.get("id"), merged_part_records, "scan_parts"
            )
        except Exception:
            logger.debug("tracker: scan_parts collection fetch failed", exc_info=True)
    return total


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


def fetch_parent_scan_by_id(base_url: str, token: str, scan_id: str) -> Dict[str, Any]:
    """Single parent scan GET — used when insights API fails but we still need terminal status in DB."""
    from urllib.parse import quote

    sid = str(scan_id or "").strip()
    if not sid:
        return {}
    base_path = PARENT_SCANS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{quote(sid, safe='')}")
    resp = _request_get(url, token, params=None)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized parent scan API: {resp.status_code}")
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and data:
            return data
        return payload
    return {}


class InsightsNotFound(RuntimeError):
    """BigID has no insight document for this scan id in this environment (HTTP 404).

    Either the scan never ran in this env, or BigID purged its insights after the
    retention window. Retrying can never succeed, so callers should stop re-queueing
    the scan instead of 404-ing on every cycle.
    """

    def __init__(self, scan_id: str, base_url: str = ""):
        self.scan_id = str(scan_id or "")
        self.base_url = str(base_url or "")
        super().__init__(f"Scan insight not found: scan_id={self.scan_id} env={self.base_url}")


def fetch_scan_insights(base_url: str, scan_id: str, token: str) -> Dict[str, Any]:
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}")
    resp = _request_get(url, token, params=None)
    # Debug: log every insight call with scan_id + status so 404s can be correlated
    # against BigID's "Scan insight wasn't found" logs (grep INSIGHT_CALL in docker logs).
    print(
        f"[{_now_utc_naive().isoformat()}Z] INSIGHT_CALL src=tracker kind=insight scan_id={scan_id} "
        f"status={resp.status_code} env={normalize_base_url(base_url)} url={url}",
        flush=True,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized insights API: {resp.status_code}")
    if resp.status_code == 404:
        raise InsightsNotFound(scan_id, normalize_base_url(base_url))
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def fetch_child_scan_insights(base_url: str, subscan_id: str, token: str) -> Dict[str, Any]:
    """Fetch BigID's child-scan counters used by the Scan Insights UI."""
    from urllib.parse import quote

    child_id = str(subscan_id or "").strip()
    if not child_id:
        return {}
    path = f"{CHILD_INSIGHTS_PATH.rstrip('/')}/{quote(child_id, safe='')}"
    url = build_api_url(base_url, path)
    resp = _request_get(url, token, params={"shouldFetchAllScannerIds": "false"})
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Unauthorized child scan insights API: {resp.status_code}")
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _stored_subscan_ids(engine, scan_id: str) -> List[str]:
    """Return child scan IDs captured from BigID's scan_parts export."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT subscan_id FROM scan_part_records "
                    "WHERE scan_id = :sid AND subscan_id IS NOT NULL"
                ),
                {"sid": str(scan_id or "").strip()},
            ).fetchall()
    except Exception:
        logger.debug("tracker: unable to load stored subscan IDs", exc_info=True)
        return []
    return sorted({str(row[0]).strip() for row in rows if row and str(row[0]).strip()})


def fetch_child_scan_metrics(
    engine,
    base_url: str,
    scan_id: str,
    token: str,
) -> Dict[str, int]:
    """Aggregate child ``Scan`` counters across all subscans for a parent scan."""
    totals = {"total_scan_parts": 0, "completed_scan_parts": 0, "failed_scan_parts": 0}
    for subscan_id in _stored_subscan_ids(engine, scan_id):
        try:
            payload = fetch_child_scan_insights(base_url, subscan_id, token)
            info = ((payload.get("data") or {}).get("scanChildInfo") or {})
            scan = info.get("Scan") if isinstance(info, dict) else {}
            if not isinstance(scan, dict):
                continue
            totals["total_scan_parts"] += int(scan.get("totalParts") or 0)
            totals["completed_scan_parts"] += int(scan.get("partsCompleted") or 0)
            totals["failed_scan_parts"] += int(scan.get("partsFailed") or 0)
        except Exception:
            logger.debug("tracker: child scan insight fetch failed", exc_info=True)
    return totals


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

        about_block = data_block.get("about")
        if isinstance(about_block, dict):
            for k in ["releaseVersion", "coreVersion", "version", "build", "buildVersion", "release", "tag"]:
                v = about_block.get(k)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())

    for s in candidates:
        bv = _parse_build_version_from_text(s)
        if bv:
            return bv
    return ""


def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        if not s:
            return None
        ss = str(s).strip().replace('Z','')
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return datetime.strptime(ss, fmt)
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
        return None
    except Exception:
        return None


def sync_env_version_from_about(engine, env_row: Dict[str, Any], token: str, *, ttl_minutes: int = 60, force: bool = False) -> Tuple[bool, str]:
    """
    Best-effort: fetch /about and update test_environments.version when changed.
    Throttled by tracker_kv so we don't call /about too frequently.
    Returns (ok, message).
    """
    try:
        env_id = env_row.get('id')
        base_url = str(env_row.get('base_url') or '').strip().rstrip('/')
        key = str(env_id if env_id is not None else base_url)
        if not key:
            return False, 'no_env_key'
        ttl_m = int(ttl_minutes) if ttl_minutes is not None else 60
    except Exception:
        return False, 'bad_env'

    last = ''
    try:
        last = _kv_get(engine, f'env_about_last::{key}')
    except Exception:
        last = ''
    if not force:
        last_dt = _parse_iso_dt(last)
        if last_dt is not None:
            age_min = (datetime.utcnow() - last_dt).total_seconds() / 60.0
            if age_min >= 0 and age_min < float(ttl_m):
                return True, 'ttl_skip'

    # Record attempt time so failures don't hot-loop
    try:
        _kv_set(engine, f'env_about_last::{key}', datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'))
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)

    try:
        about = fetch_about(base_url, token)
        bv = _extract_build_version_from_about_payload(about)
    except Exception as ex:
        return False, f'about_failed: {ex}'

    bv = str(bv or '').strip()
    if not bv:
        return False, 'about_no_version'

    cur = _parse_build_version_from_text(env_row.get('version') or '')
    if not cur:
        cur = str(env_row.get('version') or '').strip()
    if cur == bv:
        return True, f'no_change:{bv}'

    # Update DB (and history if table exists)
    try:
        if env_id is not None:
            with engine.connect() as conn:
                conn.execute(text('UPDATE test_environments SET version=:v WHERE id=:id'), {'v': bv, 'id': int(env_id)})
                try:
                    conn.execute(
                        text('INSERT INTO test_environment_version_history (environment_id, version_value, change_note) VALUES (:eid, :v, :n)'),
                        {'eid': int(env_id), 'v': bv, 'n': 'tracker:/about'}
                    )
                except Exception:
                    logger.debug("tracker: suppressed exception", exc_info=True)
                conn.commit()
            env_row['version'] = bv
            return True, f'updated:{cur}->{bv}'
    except Exception as ex:
        return False, f'update_failed:{ex}'

    return True, f'no_env_id:{bv}'


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

    # Keep DB env version in sync with /about (handled in sync_env_version_from_about now)
    # Removing redundant update here
    
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
        try:
            sync_classifier_details_for_env_build(engine, env_row, token, str(build_version), force=False)
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
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
        logger.debug("tracker: suppressed exception", exc_info=True)
    try:
        # List/mappings were just refreshed — pull full per-classifier metadata so Deep-Dive/diffs stay aligned.
        sync_classifier_details_for_env_build(engine, env_row, token, str(build_version), force=True)
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)
    return True

def _extract_live_state(insight_payload: Dict[str, Any]) -> str:
    si = insight_payload.get("data", {}).get("scanInsights", {}) if isinstance(insight_payload, dict) else {}
    if not si and isinstance(insight_payload, dict):
        si = insight_payload
    # Lifecycle state: use scanInsights.state only. progress.state tracks sub-progress and can read
    # "Completed" before the parent scan is finished (do not use for DB/parent upserts).
    return str(si.get("state") or "").strip()


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
    for key in ["attributes", "items", "results", "dataSourcesInsights", "classifierAttributes", "classificationAttributes", "classificationsAttributes", "scanAttributes", "classification_insights", "attribute_counts", "findings"]:
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
    ai = sql_auto_increment(engine)
    lt = sql_longtext(engine)
    ou = sql_on_update_current_timestamp(engine)
    
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS builds (
                    id {ai} PRIMARY KEY,
                    version VARCHAR(50) UNIQUE
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS classifiers (
                    id {ai} PRIMARY KEY,
                    name VARCHAR(255) UNIQUE
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS build_templates (
                    id {ai} PRIMARY KEY,
                    build_id INT,
                    template_name VARCHAR(100),
                    classifier_set TEXT,
                    UNIQUE (build_id, template_name),
                    FOREIGN KEY (build_id) REFERENCES builds(id)
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS build_classifier_mappings (
                    id {ai} PRIMARY KEY,
                    build_id INT NOT NULL,
                    classifier_id INT NOT NULL,
                    environment_id INT NULL,
                    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou},
                    UNIQUE (build_id, classifier_id, environment_id),
                    FOREIGN KEY (build_id) REFERENCES builds(id),
                    FOREIGN KEY (classifier_id) REFERENCES classifiers(id)
                )
            """))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS classifier_details_snapshots (
                    id {ai} PRIMARY KEY,
                    environment_id INT NULL,
                    build_id INT NOT NULL,
                    bigid_classifier_id VARCHAR(64) NOT NULL,
                    classifier_name VARCHAR(500) NOT NULL,
                    snapshot_json {lt} NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (environment_id, build_id, bigid_classifier_id),
                    FOREIGN KEY (build_id) REFERENCES builds(id),
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                )
            """))
            conn.commit()
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)


def fetch_classifiers_list_items(base_url: str, token: str, api_path: str) -> List[Dict[str, Any]]:
    """
    Fetch classifiers list items (objects).
    """
    url = build_api_url(base_url, api_path)
    # Using the existing _request_get which might be defined in tracker.py (let's check)
    # Re-checking tracker.py imports and helpers...
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
    return items


def fetch_classifier_detail(base_url: str, classifier_id: str, token: str) -> Dict[str, Any]:
    """GET /api/v1/classifications/{id} -> detail data."""
    # BigID usually uses /api/v1/classifications/{id} for detail
    url = build_api_url(base_url, f"/api/v1/classifications/{classifier_id}")
    resp = _request_get(url, token, params=None)
    if resp.status_code != 200:
        # Try fallback /api/v1/all-classifications/{id}
        url = build_api_url(base_url, f"/api/v1/all-classifications/{classifier_id}")
        resp = _request_get(url, token, params=None)

    if resp.status_code != 200:
        return {}

    data = resp.json()
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data.get("data")
    return data if isinstance(data, dict) else {}


CLASSIFIER_DETAIL_KEYS = [
    "classification_name", "category", "classifierType", "dataType", "original_name",
    "detection_sensitivity", "enabled", "min_length", "max_length",
    "proximity_after", "proximity_before", "nst_proximity_after", "nst_proximity_before",
    "description",
]


def sync_classifier_details_for_env_build(
    engine, env_row: Dict[str, Any], token: str, build_version: str, *, force: bool = False
) -> bool:
    """
    Fetch and store full detail snapshots for all enabled classifiers.
    Throttled by CLASSIFIER_DETAILS_SYNC_TTL_HOURS (default 6h) unless force=True or no rows yet.
    """
    base_url = str(env_row.get("base_url") or "").strip()
    env_id = env_row.get("id")
    if not base_url or not env_id or not build_version:
        return False

    try:
        ttl_h = float(os.getenv("CLASSIFIER_DETAILS_SYNC_TTL_HOURS", "6") or "6")
    except Exception:
        ttl_h = 6.0
    ttl_sec = max(300, int(ttl_h * 3600))

    env_key = str(env_id)
    if not force:
        try:
            with engine.connect() as conn:
                bid_res = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": build_version}).fetchone()
                if bid_res:
                    bid = int(bid_res[0])
                    snap_count = conn.execute(
                        text("""
                            SELECT COUNT(*) FROM classifier_details_snapshots
                            WHERE build_id = :bid AND environment_id = :eid
                        """),
                        {"bid": bid, "eid": int(env_id)},
                    ).scalar()

                    if snap_count and int(snap_count or 0) > 0:
                        last_sync = _kv_get(engine, f"classifier_details_sync_last::{env_key}")
                        last_build = _kv_get(engine, f"classifier_details_sync_build::{env_key}")
                        if last_sync and last_build == build_version:
                            dt = _parse_iso_dt(last_sync)
                            if dt and (datetime.utcnow() - dt).total_seconds() < float(ttl_sec):
                                return True
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)

    api_path = str(env_row.get("classifiers_api_path") or "").strip() or "/api/v1/classifications"
    try:
        items = fetch_classifiers_list_items(base_url, token, api_path)
    except Exception:
        return False

    if not items:
        return False

    with engine.connect() as conn:
        if is_postgresql(engine):
            conn.execute(text("INSERT INTO builds (version) VALUES (:v) ON CONFLICT (version) DO NOTHING"), {"v": build_version})
        else:
            conn.execute(text("INSERT IGNORE INTO builds (version) VALUES (:v)"), {"v": build_version})
        conn.commit()
        bid_row = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": build_version}).fetchone()
        if not bid_row:
            return False
        build_id = int(bid_row[0])

    print(f"[{datetime.utcnow().isoformat()}Z] env={env_row.get('env_name')} Syncing {len(items)} classifier details for build {build_version}...")

    import json
    for it in items:
        cid = it.get("_id") or it.get("id")
        name = it.get("name") or it.get("classificationName") or it.get("displayName")
        if not cid or not name:
            continue

        try:
            detail = fetch_classifier_detail(base_url, str(cid), token)
            if not detail:
                # Use current list item as detail if GET /{id} fails
                detail = it

            snapshot = {k: detail.get(k) for k in CLASSIFIER_DETAIL_KEYS if k in detail}
            snapshot_json = json.dumps(snapshot, default=str)

            with engine.connect() as conn:
                if is_postgresql(engine):
                    conn.execute(text("""
                        INSERT INTO classifier_details_snapshots (environment_id, build_id, bigid_classifier_id, classifier_name, snapshot_json)
                        VALUES (:eid, :bid, :cid, :cname, :json)
                        ON CONFLICT (environment_id, build_id, bigid_classifier_id) DO UPDATE SET
                            classifier_name = EXCLUDED.classifier_name,
                            snapshot_json = EXCLUDED.snapshot_json,
                            created_at = CURRENT_TIMESTAMP
                    """), {
                        "eid": int(env_id),
                        "bid": build_id,
                        "cid": str(cid)[:64],
                        "cname": str(name)[:500],
                        "json": snapshot_json
                    })
                else:
                    # MySQL
                    conn.execute(text("""
                        INSERT INTO classifier_details_snapshots (environment_id, build_id, bigid_classifier_id, classifier_name, snapshot_json)
                        VALUES (:eid, :bid, :cid, :cname, :json)
                        ON DUPLICATE KEY UPDATE
                            classifier_name = VALUES(classifier_name),
                            snapshot_json = VALUES(snapshot_json),
                            created_at = CURRENT_TIMESTAMP
                    """), {
                        "eid": int(env_id),
                        "bid": build_id,
                        "cid": str(cid)[:64],
                        "cname": str(name)[:500],
                        "json": snapshot_json
                    })
                conn.commit()
        except Exception as ex:
            print(f"  [!] Failed detail for {name}: {ex}")

    try:
        _kv_set(engine, f"classifier_details_sync_last::{env_key}", datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'))
        _kv_set(engine, f"classifier_details_sync_build::{env_key}", str(build_version))
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)

    return True


def fetch_enabled_classifiers(base_url: str, token: str, api_path: str) -> List[str]:
    """
    Fetch enabled classifiers list (used as template classifier set approximation).
    """
    items = fetch_classifiers_list_items(base_url, token, api_path)
    names: List[str] = []
    for it in items:
        # Only count genuinely-enabled classifiers. BigID returns enabled=null for
        # classifiers that are not actually on; counting them inflates the total.
        if "enabled" in it and not it.get("enabled"):
            continue
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
        if is_postgresql(engine):
            conn.execute(text("INSERT INTO builds (version) VALUES (:v) ON CONFLICT (version) DO NOTHING"), {"v": build_version})
        else:
            conn.execute(text("INSERT IGNORE INTO builds (version) VALUES (:v)"), {"v": build_version})
        conn.commit()
        b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()
        if not b_row:
            return
        build_id = int(b_row[0])

        # Save template row (comma-separated names)
        classifier_set_text = ", ".join([str(x).strip() for x in classifier_names if str(x).strip()])
        if is_postgresql(engine):
            conn.execute(
                text("""
                    INSERT INTO build_templates (build_id, template_name, classifier_set)
                    VALUES (:b, :t, :s)
                    ON CONFLICT (build_id, template_name) DO UPDATE SET
                        classifier_set = EXCLUDED.classifier_set
                """),
                {"b": build_id, "t": template_name[:100], "s": classifier_set_text},
            )
        else:
            # MySQL
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
            if is_postgresql(engine):
                conn.execute(text("INSERT INTO classifiers (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"), {"n": n})
            else:
                conn.execute(text("INSERT IGNORE INTO classifiers (name) VALUES (:n)"), {"n": n})
            c_row = conn.execute(text("SELECT id FROM classifiers WHERE name = :n LIMIT 1"), {"n": n}).fetchone()
            if not c_row:
                continue
            cid = int(c_row[0])
            if is_postgresql(engine):
                conn.execute(
                    text("""
                        INSERT INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                        VALUES (:b, :c, :e)
                        ON CONFLICT (build_id, classifier_id, environment_id) DO NOTHING
                    """),
                    {"b": build_id, "c": cid, "e": int(environment_id) if environment_id is not None else None},
                )
            else:
                conn.execute(
                    text("""
                        INSERT IGNORE INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                        VALUES (:b, :c, :e)
                    """),
                    {"b": build_id, "c": cid, "e": int(environment_id) if environment_id is not None else None},
                )
        conn.commit()

def fetch_scan_classifier_attributes(
    base_url: str,
    scan_id: str,
    token: str,
    *,
    limit: int = 200,
    max_pages: int = 10,
    request_timeout_seconds: int = 20,
    max_seconds_total: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch per-scan classifier attributes delta list.
    Endpoint: {INSIGHTS_PATH}/{scan_id}/attributes?type=classifier
    """
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{scan_id}/attributes")
    all_items: List[Dict[str, Any]] = []
    skip = 0
    total_count: Optional[int] = None
    started = time.time()
    for _ in range(int(max_pages) if int(max_pages) > 0 else 0):
        try:
            if max_seconds_total is not None and (time.time() - started) > float(max_seconds_total):
                break
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
        params = {
            "skip": int(skip),
            "limit": int(limit),
            "requireTotalCount": "true",
            "sort": json.dumps([{"field": "new", "order": "desc"}], separators=(",", ":")),
            "type": "classifier",
        }
        resp = _request_get(url, token, params=params, timeout_seconds=int(request_timeout_seconds))
        print(
            f"[{_now_utc_naive().isoformat()}Z] INSIGHT_CALL src=tracker kind=attributes scan_id={scan_id} "
            f"status={resp.status_code} skip={skip} env={normalize_base_url(base_url)} url={url}",
            flush=True,
        )
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

    # Hardening: Only accept completed_at if the scan is in a terminal state.
    # Some BigID versions return a non-null endTime/completedAt for in-progress scans.
    if status_val and not _is_terminal_state(status_val):
        completed_at = None
    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    with engine.connect() as conn:
        if is_postgresql(engine):
            conn.execute(
                text("""
                    INSERT INTO scan_parents (
                        scan_id, environment_id, scan_name, scan_type, started_at, completed_at, template_name, scan_status, is_active, last_seen_at
                    ) VALUES (:sid, :eid, :sn, :st, :sa, :ca, :tn, :ss, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT (scan_id) DO UPDATE SET
                        -- Never downgrade a known environment to NULL: callers that don't know the
                        -- env (bulk syncs) would otherwise wipe attribution, and every env-scoped
                        -- queue (insights backfill, running/terminal safety nets) skips NULL-env rows.
                        environment_id = COALESCE(EXCLUDED.environment_id, scan_parents.environment_id),
                        scan_name = EXCLUDED.scan_name,
                        scan_type = EXCLUDED.scan_type,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        template_name = EXCLUDED.template_name,
                        -- A manual "Mark stopped" sticks until BigID reports a terminal state.
                        scan_status = CASE
                            WHEN scan_parents.manually_stopped_at IS NOT NULL AND :keep_stopped
                            THEN scan_parents.scan_status ELSE EXCLUDED.scan_status END,
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
                    "keep_stopped": not _is_terminal_state(status_val),
                },
            )
        else:
            # MySQL
            conn.execute(
                text("""
                    INSERT INTO scan_parents (
                        scan_id, environment_id, scan_name, scan_type, started_at, completed_at, template_name, scan_status, is_active
                    ) VALUES (:sid, :eid, :sn, :st, :sa, :ca, :tn, :ss, TRUE)
                    ON DUPLICATE KEY UPDATE
                        environment_id = COALESCE(VALUES(environment_id), environment_id),
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

    if status_val and _is_terminal_state(status_val):
        try:
            from v2_api.workflow_scan_reconcile import complete_workflow_steps_for_scan  # noqa: PLC0415

            complete_workflow_steps_for_scan(engine, scan_id, status_val)
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)


def _parent_completed_at_dt(engine, scan_id: str) -> Optional[datetime]:
    """
    Terminal timestamp for a scan, from the parent-scans value stored in scan_parents.

    The insights API cannot supply this: BigID returns the placeholder string
    "Scan is still in progress..." for completedAt even hours after a scan is terminal,
    and stopped scans often carry no completedAt at all.
    """
    sid = str(scan_id or "").strip()
    if not engine or not sid:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT completed_at FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
        if row and row[0]:
            return _parse_iso_dt(str(row[0]))
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)
    return None


def ensure_terminal_progress_row(
    engine,
    env_id: Any,
    scan_id: str,
    scan_name: str,
    state: str,
    completed_at_raw: Any = None,
) -> Optional[datetime]:
    """
    Close out a scan in scan_progress the moment we learn it is no longer in progress.

    Used when the insights API can't give us a snapshot (404 / timeout / purged) but
    parent-scans reports a terminal state: without this, scan_progress keeps the scan's
    last 'In Progress'/'Started' row forever and every consumer thinks it never finished.

    The row's counters are carried forward from the scan's most recent snapshot — writing
    zeros here would put a fake cliff at the end of every progress chart. Returns the
    captured_at_utc written, or None if nothing was written (no state, or a terminal row
    already exists).
    """
    sid = str(scan_id or "").strip()
    st = str(state or "").strip()
    if not engine or not sid or not st or not _is_terminal_state(st):
        return None

    # Terminal timestamp, best source first: the caller's completedAt, then the parent-scans
    # value. Both are missing for stopped scans, so the row is placed below from the scan's own
    # snapshot history instead.
    captured_at = _parse_iso_dt(str(completed_at_raw or "")) if completed_at_raw else None
    if captured_at is None:
        captured_at = _parent_completed_at_dt(engine, sid)

    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    terminal_csv = ",".join(f"'{s}'" for s in TERMINAL_STATES)
    try:
        with engine.connect() as conn:
            already = conn.execute(
                text(f"SELECT 1 FROM scan_progress WHERE scan_id = :sid AND scan_state IN ({terminal_csv}) LIMIT 1"),
                {"sid": sid},
            ).fetchone()
            if already:
                return None

            last = conn.execute(
                text("""
                    SELECT completed_sub_scans, failed_sub_scans, total_sub_scans,
                           completed_parts, failed_parts, total_parts_scanned,
                           total_classifications, newly_added_classifications, false_removed_classifications,
                           objects_scanned, objects_failed, objects_with_pi,
                           environment_id, captured_at_utc
                    FROM scan_progress
                    WHERE scan_id = :sid
                    ORDER BY captured_at_utc DESC
                    LIMIT 1
                """),
                {"sid": sid},
            ).fetchone()
            vals = [int(v or 0) for v in (last[:12] if last else [0] * 12)]
            if env_id_int is None and last is not None and last[12] is not None:
                env_id_int = int(last[12])

            if captured_at is None:
                # No completedAt from BigID at all (typical for stopped scans). Place the row by
                # how long ago we last saw the scan alive: a snapshot from minutes ago means it
                # just finished (use now), while a scan that went quiet slots ago finished back
                # then — stamping it "now" would inflate its duration by the whole silent gap.
                now_utc = _now_utc_naive()
                slot_m = _slot_minutes()
                last_seen = last[13] if (last is not None and isinstance(last[13], datetime)) else None
                if last_seen is not None and (now_utc - last_seen) > timedelta(minutes=slot_m * 3):
                    captured_at = last_seen + timedelta(seconds=1)
                else:
                    captured_at = now_utc.replace(minute=(now_utc.minute // slot_m) * slot_m, second=0, microsecond=0)
            captured_at = captured_at.replace(microsecond=0)

            # Never collide with a slot snapshot sitting on the same timestamp.
            if captured_at.second == 0 and captured_at.minute % _slot_minutes() == 0:
                if conn.execute(
                    text("SELECT 1 FROM scan_progress WHERE scan_id = :sid AND captured_at_utc = :cau LIMIT 1"),
                    {"sid": sid, "cau": captured_at},
                ).fetchone():
                    captured_at = captured_at + timedelta(seconds=1)

            params = {
                "cau": captured_at,
                "sid": sid,
                "eid": env_id_int,
                "sst": st,
                "cs": vals[0], "fs": vals[1], "tsc": vals[2],
                "cp": vals[3], "fp": vals[4], "tp": vals[5],
                "tc": vals[6], "nc": vals[7], "fr": vals[8],
                "os": vals[9], "of": vals[10], "owp": vals[11],
            }
            if is_postgresql(engine):
                conn.execute(
                    text("""
                        INSERT INTO scan_progress (
                            captured_at_utc, "timestamp",
                            scan_id, environment_id, scan_state,
                            completed_sub_scans, failed_sub_scans, total_sub_scans,
                            completed_parts, failed_parts, total_parts_scanned,
                            total_classifications, newly_added_classifications, false_removed_classifications,
                            objects_scanned, objects_failed, objects_with_pi
                        ) VALUES (
                            :cau, :cau,
                            :sid, :eid, :sst,
                            :cs, :fs, :tsc,
                            :cp, :fp, :tp,
                            :tc, :nc, :fr,
                            :os, :of, :owp
                        )
                        ON CONFLICT (scan_id, captured_at_utc) DO UPDATE SET
                            scan_state = EXCLUDED.scan_state,
                            environment_id = COALESCE(EXCLUDED.environment_id, scan_progress.environment_id)
                    """),
                    params,
                )
            else:
                params["sname"] = str(scan_name or "")
                conn.execute(
                    text("""
                        INSERT INTO scan_progress (
                            captured_at_utc, `timestamp`,
                            scan_id, scan_name, environment_id, scan_state,
                            completed_sub_scans, failed_sub_scans, total_sub_scans,
                            completed_parts, failed_parts, total_parts_scanned,
                            total_classifications, newly_added_classifications, false_removed_classifications,
                            objects_scanned, objects_failed, objects_with_pi
                        ) VALUES (
                            :cau, :cau,
                            :sid, :sname, :eid, :sst,
                            :cs, :fs, :tsc,
                            :cp, :fp, :tp,
                            :tc, :nc, :fr,
                            :os, :of, :owp
                        )
                        ON DUPLICATE KEY UPDATE
                            scan_state = VALUES(scan_state),
                            environment_id = COALESCE(VALUES(environment_id), environment_id)
                    """),
                    params,
                )
            conn.commit()
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)
        return None

    print(
        f"[{_now_utc_naive().isoformat()}Z] terminal_progress_row scan_id={sid} state={st} "
        f"captured_at_utc={captured_at.isoformat()} (insights unavailable)",
        flush=True,
    )
    try:
        _push_scan_progress_to_incident_intelligence(
            scan_id=sid,
            scan_name=str(scan_name or ""),
            environment_id=env_id_int or 0,
            scan_state=st,
            captured_at_utc=captured_at,
        )
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)
    return captured_at


def upsert_scan_progress_record(
    engine,
    env_id: Any,
    scan_id: str,
    scan_name: str,
    insight_payload: Dict[str, Any],
    forced_state: Optional[str] = None,
    child_metrics: Optional[Dict[str, Any]] = None,
    slot_minutes: Optional[int] = None,
) -> Optional[datetime]:
    """
    Writes slot-aligned scan_progress row. Returns captured_at_utc used for the main upsert
    so scanner performance can use the same timestamp as the insights snapshot.
    """
    if not scan_id:
        return None

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
    scan_state = str(forced_state or si.get("state") or "").strip() or None

    completed_sub = _to_int(subscans.get("completedSubScans", 0))
    failed_sub = _to_int(subscans.get("failedSubScans", 0))
    total_sub = _to_int(subscans.get("totalSubScans", 0))

    completed_parts = _to_int(subscans.get("completedParts") or progress.get("complete") or 0)
    failed_parts = _to_int(subscans.get("failedParts") or progress.get("failed") or 0)
    total_parts = _to_int(subscans.get("totalPartsScanned") or progress.get("total") or 0)
    child_metrics = child_metrics if isinstance(child_metrics, dict) else {}
    completed_scan_parts = _to_int(child_metrics.get("completed_scan_parts"))
    failed_scan_parts = _to_int(child_metrics.get("failed_scan_parts"))
    total_scan_parts = _to_int(child_metrics.get("total_scan_parts"))

    total_class = _to_int(classifications.get("total", 0))
    new_class = _to_int(classifications.get("newlyAdded", 0))
    false_removed = _to_int(classifications.get("falseRemoved", 0))
    objects_scanned = _to_int(data_sources.get("objectsScanned") or si.get("objectsScanned") or 0)
    objects_failed = _to_int(data_sources.get("objectsFailed") or 0)
    objects_with_pi = _to_int(data_sources.get("objectsWithPi") or 0)

    # Align to the cadence of the cycle that is writing this row. Fast cycles
    # are environment-scoped, so they must not collapse back onto the normal
    # environment-wide slot and overwrite a 20-minute snapshot.
    now_utc = _now_utc_naive()
    slot_m = max(1, int(slot_minutes or _slot_minutes()))
    captured_at_utc = now_utc.replace(minute=(now_utc.minute // slot_m) * slot_m, second=0, microsecond=0)

    try:
        env_id_int = int(env_id) if env_id is not None and str(env_id).strip() != "" else None
    except Exception:
        env_id_int = None

    with engine.connect() as conn:
        # start-marker row (scan start time): write the real BigID startedAt as the first DB row for this scan
        try:
            sp0 = si.get('scanProfile') if isinstance(si.get('scanProfile'), dict) else {}
            started_raw = sp0.get('startedAt') or sp0.get('startTime') or sp0.get('createdAt')
            started_dt = _parse_iso_dt(str(started_raw or '')) if started_raw else None
            if started_dt is not None:
                row = conn.execute(text('SELECT 1 FROM scan_progress WHERE scan_id=:sid LIMIT 1'), {'sid': str(scan_id)}).fetchone()
                if not row:
                    cau = started_dt.replace(microsecond=0)
                    slot_m = max(1, int(slot_minutes or _slot_minutes()))
                    # Avoid colliding with slot-aligned snapshots (seconds=0 at slot boundaries)
                    if (cau.second == 0) and (cau.minute % slot_m == 0):
                        cau = cau + timedelta(seconds=1)
                    ts0 = started_dt.replace(microsecond=0)
                    if is_postgresql(engine):
                        conn.execute(text("""
                            INSERT INTO scan_progress (
                                captured_at_utc, "timestamp",
                                scan_id, environment_id,
                                scan_state,
                                completed_sub_scans, failed_sub_scans, total_sub_scans,
                                completed_parts, failed_parts, total_parts_scanned,
                                completed_scan_parts, failed_scan_parts, total_scan_parts,
                                total_classifications, newly_added_classifications, false_removed_classifications,
                                objects_scanned, objects_failed, objects_with_pi
                            ) VALUES (
                                :cau, :ts,
                                :sid, :eid,
                                :sst,
                                0,0,0,
                                0,0,0,
                                0,0,0,
                                0,0,0,
                                0,0,0
                            )
                            ON CONFLICT (scan_id, captured_at_utc) DO NOTHING
                        """), {
                            'cau': cau,
                            'ts': ts0,
                            'sid': str(scan_id),
                            'eid': env_id_int,
                            'sst': 'Started',
                        })
                    else:
                        conn.execute(text("""
                            INSERT IGNORE INTO scan_progress (
                                captured_at_utc, `timestamp`,
                                scan_id, scan_name, environment_id,
                                scan_state,
                                completed_sub_scans, failed_sub_scans, total_sub_scans,
                                completed_parts, failed_parts, total_parts_scanned,
                                total_classifications, newly_added_classifications, false_removed_classifications,
                                objects_scanned, objects_failed, objects_with_pi
                            ) VALUES (
                                :cau, :ts,
                                :sid, :sname, :eid,
                                :sst,
                                0,0,0,
                                0,0,0,
                                0,0,0,
                                0,0,0
                            )
                        """), {
                            'cau': cau,
                            'ts': ts0,
                            'sid': str(scan_id),
                            'sname': str(scan_name or ''),
                            'eid': env_id_int,
                            'sst': 'Started',
                        })
                        
                        # Push scan started event to incident_intelligence (non-blocking)
                        _push_scan_progress_to_incident_intelligence(
                            scan_id=str(scan_id),
                            scan_name=str(scan_name or ''),
                            environment_id=env_id_int or 0,
                            scan_state='Started',
                            captured_at_utc=cau if isinstance(cau, datetime) else datetime.utcnow(),
                        )
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)

        # Skip the slot row for terminal scans ONLY when we can place a final row at the real
        # completion time — otherwise the terminal state would never reach scan_progress at all.
        # BigID hands back the placeholder "Scan is still in progress..." for insights completedAt,
        # and stopped scans often have no completedAt anywhere, so both are unparseable: in that
        # case keep the slot row (at detection time) and let it carry the terminal state.
        _skip_slot_row = False
        if scan_state and _is_terminal_state(scan_state):
            sp0_check = si.get('scanProfile') if isinstance(si.get('scanProfile'), dict) else {}
            _completed_raw_check = (sp0_check.get('completedAt') or sp0_check.get('endTime')
                                    or sp0_check.get('endedAt') or si.get('completedAt'))
            _terminal_dt = _parse_iso_dt(str(_completed_raw_check or '')) if _completed_raw_check else None
            if _terminal_dt is None:
                # parent-scans is authoritative for terminal timing when insights isn't usable.
                _terminal_dt = _parent_completed_at_dt(engine, scan_id)
            _skip_slot_row = _terminal_dt is not None

        if not _skip_slot_row:
            conn.execute(
            text(f"""
                INSERT INTO scan_progress (
                    captured_at_utc,
                    scan_id, {"scan_name, " if not is_postgresql(engine) else ""}environment_id,
                    scan_state,
                    completed_sub_scans, failed_sub_scans, total_sub_scans,
                    completed_parts, failed_parts, total_parts_scanned,
                    completed_scan_parts, failed_scan_parts, total_scan_parts,
                    total_classifications, newly_added_classifications, false_removed_classifications,
                    objects_scanned, objects_failed, objects_with_pi
                ) VALUES (
                    :cau,
                    :sid, {":sname, " if not is_postgresql(engine) else ""}:eid,
                    :sst,
                    :cs, :fs, :tsc,
                    :cp, :fp, :tp,
                    :csp, :fsp, :tsp,
                    :tc, :nc, :fr,
                    :os, :of, :owp
                )
                {"ON CONFLICT (scan_id, captured_at_utc) DO UPDATE SET" if is_postgresql(engine) else "ON DUPLICATE KEY UPDATE"}
                    {"" if is_postgresql(engine) else "scan_name = VALUES(scan_name),"}
                    environment_id = {"EXCLUDED.environment_id" if is_postgresql(engine) else "VALUES(environment_id)"},
                    scan_state = {"EXCLUDED.scan_state" if is_postgresql(engine) else "VALUES(scan_state)"},
                    completed_sub_scans = {"EXCLUDED.completed_sub_scans" if is_postgresql(engine) else "VALUES(completed_sub_scans)"},
                    failed_sub_scans = {"EXCLUDED.failed_sub_scans" if is_postgresql(engine) else "VALUES(failed_sub_scans)"},
                    total_sub_scans = {"EXCLUDED.total_sub_scans" if is_postgresql(engine) else "VALUES(total_sub_scans)"},
                    completed_parts = {"EXCLUDED.completed_parts" if is_postgresql(engine) else "VALUES(completed_parts)"},
                    failed_parts = {"EXCLUDED.failed_parts" if is_postgresql(engine) else "VALUES(failed_parts)"},
                    total_parts_scanned = {"EXCLUDED.total_parts_scanned" if is_postgresql(engine) else "VALUES(total_parts_scanned)"},
                    completed_scan_parts = {"EXCLUDED.completed_scan_parts" if is_postgresql(engine) else "VALUES(completed_scan_parts)"},
                    failed_scan_parts = {"EXCLUDED.failed_scan_parts" if is_postgresql(engine) else "VALUES(failed_scan_parts)"},
                    total_scan_parts = {"EXCLUDED.total_scan_parts" if is_postgresql(engine) else "VALUES(total_scan_parts)"},
                    total_classifications = {"EXCLUDED.total_classifications" if is_postgresql(engine) else "VALUES(total_classifications)"},
                    newly_added_classifications = {"EXCLUDED.newly_added_classifications" if is_postgresql(engine) else "VALUES(newly_added_classifications)"},
                    false_removed_classifications = {"EXCLUDED.false_removed_classifications" if is_postgresql(engine) else "VALUES(false_removed_classifications)"},
                    objects_scanned = {"EXCLUDED.objects_scanned" if is_postgresql(engine) else "VALUES(objects_scanned)"},
                    objects_failed = {"EXCLUDED.objects_failed" if is_postgresql(engine) else "VALUES(objects_failed)"},
                    objects_with_pi = {"EXCLUDED.objects_with_pi" if is_postgresql(engine) else "VALUES(objects_with_pi)"},
                    {"timestamp" if is_postgresql(engine) else "`timestamp`"} = CURRENT_TIMESTAMP
            """),
            {
                "cau": captured_at_utc,
                "sid": scan_id,
                **({} if is_postgresql(engine) else {"sname": str(scan_name or "")[:1000] or None}),
                "eid": env_id_int,
                "sst": scan_state,
                "cs": completed_sub,
                "fs": failed_sub,
                "tsc": total_sub,
                "cp": completed_parts,
                "fp": failed_parts,
                "tp": total_parts,
                "csp": completed_scan_parts,
                "fsp": failed_scan_parts,
                "tsp": total_scan_parts,
                "tc": total_class,
                "nc": new_class,
                "fr": false_removed,
                "os": objects_scanned,
                "of": objects_failed,
                "owp": objects_with_pi,
            },
        )
        # end if not _skip_slot_row
        
        # Push scan progress update to incident_intelligence (non-blocking)
        _push_scan_progress_to_incident_intelligence(
            scan_id=str(scan_id),
            scan_name=str(scan_name or ""),
            environment_id=env_id_int or 0,
            scan_state=str(scan_state or ""),
            captured_at_utc=captured_at_utc if isinstance(captured_at_utc, datetime) else datetime.utcnow(),
            completed_sub=completed_sub,
            failed_sub=failed_sub,
            total_sub=total_sub,
            objects_scanned=objects_scanned,
            objects_failed=objects_failed,
            total_classifications=total_class,
        )

        # FINAL/TERMINAL row: If scan is terminal, insert a row with the ACTUAL completedAt as captured_at_utc.
        # _terminal_dt was resolved above (insights completedAt, else parent-scans completed_at); when it is
        # None the slot row above already carried the terminal state, so there is nothing left to write.
        if scan_state and _is_terminal_state(scan_state):
            completed_dt = _terminal_dt
            cau_end: Optional[datetime] = None
            try:
                if completed_dt:
                    cau_end = completed_dt.replace(microsecond=0)
                    # Offset by 1s if it lands exactly on a slot to avoid collision with PULSE snapshots
                    if (cau_end.second == 0) and (cau_end.minute % slot_m == 0):
                        cau_end = cau_end + timedelta(seconds=1)
                    
                    if is_postgresql(engine):
                        conn.execute(text("""
                            INSERT INTO scan_progress (
                                captured_at_utc, "timestamp",
                                scan_id, environment_id,
                                scan_state,
                                completed_sub_scans, failed_sub_scans, total_sub_scans,
                                completed_parts, failed_parts, total_parts_scanned,
                                total_classifications, newly_added_classifications, false_removed_classifications,
                                objects_scanned, objects_failed, objects_with_pi
                            ) VALUES (
                                :cau, :ts,
                                :sid, :eid,
                                :sst,
                                :cs, :fs, :tsc,
                                :cp, :fp, :tp,
                                :tc, :nc, :fr,
                                :os, :of, :owp
                            )
                            ON CONFLICT (scan_id, captured_at_utc) DO NOTHING
                        """), {
                            'cau': cau_end,
                            'ts': cau_end,
                            'sid': str(scan_id),
                            'eid': env_id_int,
                            'sst': scan_state,
                            'cs': completed_sub, 'fs': failed_sub, 'tsc': total_sub,
                            'cp': completed_parts, 'fp': failed_parts, 'tp': total_parts,
                            'tc': total_class, 'nc': new_class, 'fr': false_removed,
                            'os': objects_scanned, 'of': objects_failed, 'owp': objects_with_pi
                        })
                    else:
                        conn.execute(text("""
                            INSERT IGNORE INTO scan_progress (
                                captured_at_utc, `timestamp`,
                                scan_id, scan_name, environment_id,
                                scan_state,
                                completed_sub_scans, failed_sub_scans, total_sub_scans,
                                completed_parts, failed_parts, total_parts_scanned,
                                total_classifications, newly_added_classifications, false_removed_classifications,
                                objects_scanned, objects_failed, objects_with_pi
                            ) VALUES (
                                :cau, :ts,
                                :sid, :sname, :eid,
                                :sst,
                                :cs, :fs, :tsc,
                                :cp, :fp, :tp,
                                :tc, :nc, :fr,
                                :os, :of, :owp
                            )
                        """), {
                            'cau': cau_end,
                            'ts': cau_end,
                            'sid': str(scan_id),
                            'sname': str(scan_name or ''),
                            'eid': env_id_int,
                            'sst': scan_state,
                            'cs': completed_sub, 'fs': failed_sub, 'tsc': total_sub,
                            'cp': completed_parts, 'fp': failed_parts, 'tp': total_parts,
                            'tc': total_class, 'nc': new_class, 'fr': false_removed,
                            'os': objects_scanned, 'of': objects_failed, 'owp': objects_with_pi
                        })
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
            
            # Push terminal scan state to incident_intelligence (non-blocking)
            if completed_dt:
                _push_scan_progress_to_incident_intelligence(
                    scan_id=str(scan_id),
                    scan_name=str(scan_name or ""),
                    environment_id=env_id_int or 0,
                    scan_state=str(scan_state or ""),
                    captured_at_utc=cau_end if isinstance(cau_end, datetime) else datetime.utcnow(),
                    completed_sub=completed_sub,
                    failed_sub=failed_sub,
                    total_sub=total_sub,
                    objects_scanned=objects_scanned,
                    objects_failed=objects_failed,
                    total_classifications=total_class,
                )
        conn.commit()
    return captured_at_utc


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
        if is_postgresql(engine):
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
                        removed_attributes_count,
                        updated_at
                    ) VALUES (
                        :sid, :dca, :can, :dsn, :owp, :osc, :oen, :tcl, :nac, :rac, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (scan_id) DO UPDATE SET
                        distinct_classification_attributes = EXCLUDED.distinct_classification_attributes,
                        classifier_attribute_names = EXCLUDED.classifier_attribute_names,
                        data_source_names = EXCLUDED.data_source_names,
                        objects_with_pi = EXCLUDED.objects_with_pi,
                        objects_scanned = EXCLUDED.objects_scanned,
                        objects_enumerated = EXCLUDED.objects_enumerated,
                        total_classification = EXCLUDED.total_classification,
                        new_attributes_count = COALESCE(EXCLUDED.new_attributes_count, scan_insights.new_attributes_count),
                        removed_attributes_count = COALESCE(EXCLUDED.removed_attributes_count, scan_insights.removed_attributes_count),
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
        else:
            # MySQL
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

def _strip_mb_to_int(v: Any) -> int:
    try:
        if not v: return 0
        s = str(v).upper().replace("MB", "").replace(" ", "").strip()
        return int(float(s))
    except Exception:
        return 0

def _scanner_sample_minutes() -> int:
    """Sample interval while scans run (default 2 min → full coverage on 8h+ scans)."""
    try:
        m = int(_getenv_str("TRACKER_SCANNER_SAMPLE_MINUTES", "2") or "2")
    except Exception:
        m = 2
    return max(1, min(int(m), 15))


def load_envs_with_active_scans(engine) -> List[Dict[str, Any]]:
    """
    Every environment that currently has a non-terminal scan (scan_parents and/or workflow).
    Used so scanner metrics keep sampling for the full scan duration even after BigID drops
    the scan from the parent-scans "running" API list.
    """
    if not engine:
        return []
    active_sql = _te_is_active_sql(engine, "te")
    terminal_csv = ",".join(f"'{s}'" for s in TERMINAL_STATES)
    try:
        from v2_api.workflow_runner import _workflow_env_occupying_sql_in
        occupying = _workflow_env_occupying_sql_in()
    except Exception:
        occupying = "'running', 'scan_running', 'scan_created', 'ds_created', 'ds_creation', 'test_conn', 'test_conn_creating'"
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT te.id, te.env_name, te.base_url,
                           te.auth_module_path, te.auth_function_name
                    FROM test_environments te
                    WHERE {active_sql}
                      AND (
                        EXISTS (
                            SELECT 1 FROM scan_parents sp
                            WHERE sp.environment_id = te.id
                              AND COALESCE(sp.is_active, TRUE) = TRUE
                              AND TRIM(COALESCE(sp.scan_status,'')) <> ''
                              AND TRIM(COALESCE(sp.scan_status,'')) NOT IN ({terminal_csv})
                        )
                        OR EXISTS (
                            SELECT 1 FROM workflow_steps ws
                            INNER JOIN workflow_runs wr ON wr.id = ws.run_id
                            WHERE ws.environment_id = te.id
                              AND LOWER(TRIM(COALESCE(wr.status,''))) = 'running'
                              AND LOWER(TRIM(COALESCE(ws.status,''))) IN ({occupying})
                              AND ws.scan_id IS NOT NULL
                              AND TRIM(COALESCE(ws.scan_id,'')) <> ''
                        )
                      )
                    ORDER BY te.id
                    """
                )
            ).fetchall()
        out = []
        for r in rows or []:
            out.append(
                {
                    "id": r[0],
                    "env_name": str(r[1] or ""),
                    "base_url": str(r[2] or ""),
                    "auth_module_path": r[3],
                    "auth_function_name": r[4],
                }
            )
        return [x for x in out if x.get("id") is not None and str(x.get("base_url") or "").strip()]
    except Exception:
        return []


def _service_status_minutes() -> int:
    """Service-status sample interval (default 5 min), independent of the scanner sample interval."""
    try:
        m = int(_getenv_str("TRACKER_SERVICE_STATUS_MINUTES", "5") or "5")
    except Exception:
        m = 5
    return max(1, min(int(m), 60))


def _capture_service_status_throttled(
    engine,
    env_row: Dict[str, Any],
    token: str,
    *,
    force: bool = False,
    captured_at: Optional[datetime] = None,
) -> None:
    """One sample per service every TRACKER_SERVICE_STATUS_MINUTES (default 5), for Scan Progress history."""
    env_id = env_row.get("id")
    base_url = str(env_row.get("base_url") or "").strip()
    if env_id is None or not base_url or not token:
        return
    now_s = captured_at if isinstance(captured_at, datetime) else _now_utc_naive()
    svc_m = _service_status_minutes()
    bucket = (now_s.hour * 60 + now_s.minute) // svc_m
    s_key = f"service_status_cap::{env_id}::{now_s.strftime('%Y%m%d')}_{bucket}"
    if not force and _kv_get(engine, s_key):
        return
    try:
        from scanner_performance import fetch_services_status_result, record_service_capture, upsert_service_status_samples
    except ImportError:
        from v2_api.scanner_performance import fetch_services_status_result, record_service_capture, upsert_service_status_samples  # type: ignore
    aligned_s = now_s.replace(second=0, microsecond=0) - timedelta(minutes=now_s.minute % svc_m)
    try:
        res = fetch_services_status_result(base_url, token)
    except Exception as svc_ex:
        res = {"outcome": "error", "items": [], "error": str(svc_ex)[:500]}
    # Every attempt is logged, including empty and failed ones, so a gap in the history
    # is visible as NO DATA instead of looking like "no pods".
    record_service_capture(engine, int(env_id), aligned_s, res["outcome"], len(res["items"]), res["error"])
    if res["outcome"] == "ok":
        try:
            upsert_service_status_samples(engine, int(env_id), res["items"], aligned_s)
        except Exception as svc_ex:
            print(
                f"[{_now_utc_naive().isoformat()}Z] env={env_row.get('env_name', env_id)} service_status_write_failed: {svc_ex}",
                flush=True,
            )
    else:
        print(
            f"[{_now_utc_naive().isoformat()}Z] env={env_row.get('env_name', env_id)} service_status_{res['outcome']}: {res['error']}",
            flush=True,
        )
    if not force:
        _kv_set(engine, s_key, res["outcome"], expire_seconds=svc_m * 60 + 30)


SERVICE_HISTORY_RETENTION_DAYS = 7


def purge_service_status_history(engine) -> None:
    """Delete service samples and capture-log rows older than the retention window. Runs at most hourly."""
    if _kv_get(engine, "service_history_purge_last"):
        return
    _kv_set(engine, "service_history_purge_last", "1", expire_seconds=3600)
    cutoff = _now_utc_naive() - timedelta(days=SERVICE_HISTORY_RETENTION_DAYS)
    for table in ("service_status_samples", "service_capture_log"):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DELETE FROM {table} WHERE captured_at_utc < :cutoff"), {"cutoff": cutoff})
        except Exception:
            # Table not created yet (no capture has run) — nothing to purge.
            logger.debug("tracker: service history purge skipped for %s", table, exc_info=True)


_service_sampler_token_cache: Dict[str, Tuple[str, float]] = {}
SERVICE_SAMPLER_TOKEN_TTL_SECONDS = 300


def _service_sampler_token(env_row: Dict[str, Any]) -> str:
    """
    Auth token for the service sampler. main() keeps its own token_cache in a closure, so this has a
    module-level one. Refreshed every few minutes: a token cached for the whole process life went
    stale and services-status returned HTTP 401 from then on.
    """
    base_url = str(env_row.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return ""
    cached = _service_sampler_token_cache.get(base_url)
    now_ts = time.time()
    if cached and now_ts - cached[1] < SERVICE_SAMPLER_TOKEN_TTL_SECONDS:
        return cached[0]
    tok = Get_auth_token(base_url, env_row)
    _service_sampler_token_cache[base_url] = (tok, now_ts)
    return tok


def sample_service_status_all_envs(engine) -> None:
    """
    Service samples run on their own 5-minute cadence, from the tracker's 2-minute wake loop.
    Only environments with a non-terminal scan are sampled; idle environments are not polled.
    """
    purge_service_status_history(engine)
    # Liveness marker for the service sampler, separate from the cycle heartbeat: shows whether
    # this loop runs between 20-minute cycles.
    _kv_set(engine, "service_sampler_last_run", _now_utc_naive().isoformat(), expire_seconds=86400)
    for env_row in load_envs_with_active_scans(engine) or []:
        try:
            _capture_service_status_throttled(engine, env_row, _service_sampler_token(env_row))
        except Exception as ex:
            # Logged at WARNING-level print, not debug: a silent failure here left 10:15+ with no samples.
            print(
                f"[{_now_utc_naive().isoformat()}Z] env={env_row.get('env_name', env_row.get('id'))} service_status_sample_failed: {ex}",
                flush=True,
            )


def sample_scanner_perf_all_envs(engine) -> None:
    """
    Scanner group + pod samples on TRACKER_SCANNER_SAMPLE_MINUTES, from the same 2-minute wake loop as
    the service sampler. Without this, scanner samples only land on the 20-minute cycle.
    Only environments with a non-terminal scan are sampled.
    """
    for env_row in load_envs_with_active_scans(engine) or []:
        try:
            _capture_scanner_perf_throttled(engine, env_row, _service_sampler_token(env_row), {})
        except Exception as ex:
            print(
                f"[{_now_utc_naive().isoformat()}Z] env={env_row.get('env_name', env_row.get('id'))} scanner_perf_sample_failed: {ex}",
                flush=True,
            )


def _capture_scanner_perf_throttled(
    engine,
    env_row: Dict[str, Any],
    token: str,
    metrics: Dict[str, Any],
    *,
    force: bool = False,
    captured_at: Optional[datetime] = None,
) -> None:
    """
    Periodic scanner-group + per-pod capture for one environment.
    Must run for DB/workflow-tracked scans too — not only when BigID parent-scans API lists "running".
    """
    _capture_service_status_throttled(engine, env_row, token, force=force, captured_at=captured_at)
    env_id = env_row.get("id")
    base_url = str(env_row.get("base_url") or "").strip()
    if env_id is None or not base_url or not token:
        return
    now_p = captured_at if isinstance(captured_at, datetime) else _now_utc_naive()
    slot_m = _scanner_sample_minutes()
    p_bucket = now_p.minute // slot_m
    p_key = f"scanner_perf_cap::{env_id}::{now_p.strftime('%Y%m%d%H')}_{p_bucket}"
    if not force and _kv_get(engine, p_key):
        return
    try:
        p_groups = get_scanner_group_performance(base_url, token)
        p_inds = get_individual_scanner_performance(base_url, token, group_metrics=p_groups)
        if not p_inds and p_groups:
            try:
                from scanner_performance import individuals_from_scanner_groups as _ifg
            except ImportError:
                from v2_api.scanner_performance import individuals_from_scanner_groups as _ifg  # type: ignore
            p_inds = _ifg(p_groups)
        if p_groups or p_inds:
            aligned_p = now_p.replace(minute=(now_p.minute // slot_m) * slot_m, second=0, microsecond=0)
            upsert_scanner_performance(engine, int(env_id), p_groups, p_inds, captured_at=aligned_p)
            if not force:
                _kv_set(engine, p_key, "done", expire_seconds=int(slot_m) * 60 + 30)
            metrics["scanner_capture_ok"] = int(metrics.get("scanner_capture_ok", 0) or 0) + 1
            metrics["scanner_pods_written"] = int(metrics.get("scanner_pods_written", 0) or 0) + len(p_inds or [])
    except Exception as perf_ex:
        metrics["scanner_capture_fail"] = int(metrics.get("scanner_capture_fail", 0) or 0) + 1
        print(
            f"[{_now_utc_naive().isoformat()}Z] env={env_row.get('env_name', env_id)} scanner_perf_failed: {perf_ex}",
            flush=True,
        )


def get_scanner_group_performance(base_url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch scanner group metrics from /api/v1/scanner-groups"""
    url = f"{base_url.rstrip('/')}/api/v1/scanner-groups"
    try:
        resp = requests.get(url, headers={"Authorization": token}, timeout=20, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            # Expecting data: { "status": "success", "data": [ { "scannerGroups": [...] } ] }
            groups_outer = data.get("data", [])
            if groups_outer and isinstance(groups_outer, list):
                return groups_outer[0].get("scannerGroups", [])
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)
    return []

def get_individual_scanner_performance(
    base_url: str, token: str, group_metrics: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Delegate to scanner_performance (handles wrapped JSON + scanner-groups fallback)."""
    try:
        from scanner_performance import get_individual_scanner_performance as _get_inds
    except ImportError:
        from v2_api.scanner_performance import get_individual_scanner_performance as _get_inds  # type: ignore
    return _get_inds(base_url, token, group_metrics=group_metrics)

def _parse_memory_mb(v) -> int:
    """Parse memory value that may come as int, float, or string like '14336MB'."""
    if v is None:
        return 0
    s = str(v).strip().upper().replace('MB', '').replace('GB', '').replace('KB', '').strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0

def upsert_scanner_performance(engine, env_id: int, group_data: List[Dict[str, Any]], individual_data: List[Dict[str, Any]], captured_at=None) -> None:
    now_utc = captured_at if captured_at else _now_utc_naive()
    # If no captured_at was passed, we might still want to align to 20-min marks for the periodic loop.
    # However, I'll move the alignment logic TO THE CALLER (the main loop) so manual calls aren't forced into buckets.
    captured_at_utc = now_utc.replace(second=0, microsecond=0)
    
    with engine.connect() as conn:
        # Save group stats
        for g in group_data:
            update_date = _parse_iso_dt(str(g.get("updateDate") or ""))
            if is_postgresql(engine):
                conn.execute(text("""
                    INSERT INTO scanner_group_performance (
                        environment_id, captured_at_utc, group_name,
                        running_scanners, running_threads, max_threads, scanner_threads,
                        physical_memory_mb, free_memory_mb, avg_scan_speed, update_date_bigid
                    ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ud)
                    ON CONFLICT (environment_id, captured_at_utc, group_name) DO UPDATE SET
                        running_scanners = EXCLUDED.running_scanners,
                        running_threads = EXCLUDED.running_threads,
                        max_threads = EXCLUDED.max_threads,
                        scanner_threads = EXCLUDED.scanner_threads,
                        physical_memory_mb = EXCLUDED.physical_memory_mb,
                        free_memory_mb = EXCLUDED.free_memory_mb,
                        avg_scan_speed = EXCLUDED.avg_scan_speed,
                        update_date_bigid = EXCLUDED.update_date_bigid
                """), {
                    "eid": env_id, "cau": captured_at_utc, "gn": str(g.get("name") or ""),
                    "rs": int(g.get("runningScanners") or 0), "rt": int(g.get("runningThreads") or 0),
                    "mt": int(g.get("maxThreads") or 0), "st": int(g.get("scannerThreads") or 0),
                    "pm": int(g.get("physicalMemory") or 0), "fm": int(g.get("freeMemory") or 0),
                    "as": float(g.get("avgScanSpeed") or 0), "ud": update_date
                })
            else:
                conn.execute(text("""
                    INSERT IGNORE INTO scanner_group_performance (
                        environment_id, captured_at_utc, group_name,
                        running_scanners, running_threads, max_threads, scanner_threads,
                        physical_memory_mb, free_memory_mb, avg_scan_speed, update_date_bigid
                    ) VALUES (:eid, :cau, :gn, :rs, :rt, :mt, :st, :pm, :fm, :as, :ud)
                """), {
                    "eid": env_id, "cau": captured_at_utc, "gn": str(g.get("name") or ""),
                    "rs": int(g.get("runningScanners") or 0), "rt": int(g.get("runningThreads") or 0),
                    "mt": int(g.get("maxThreads") or 0), "st": int(g.get("scannerThreads") or 0),
                    "pm": int(g.get("physicalMemory") or 0), "fm": int(g.get("freeMemory") or 0),
                    "as": float(g.get("avgScanSpeed") or 0), "ud": update_date
                })
            
        if not individual_data and group_data:
            try:
                from scanner_performance import individuals_from_scanner_groups as _ifg
            except ImportError:
                from v2_api.scanner_performance import individuals_from_scanner_groups as _ifg  # type: ignore
            individual_data = _ifg(group_data)

        # Save individual stats
        for s in individual_data:
            update_date = _parse_iso_dt(str(s.get("updateDate") or ""))
            sid = str(s.get("id") or "")
            s_name = str(s.get("name") or "")
            # If multiple scanners have the same name, suffix with ID to separate lines in charts
            display_name = f"{s_name} [{sid[:4]}]" if sid else s_name
            # CPU% = systemLoadAverage / availableProcessors * 100
            try:
                _load = float(s.get("systemLoadAverage") or 0)
                _cores = int(str(s.get("availableProcessors") or 1).strip() or 1)
                _cpu_pct = round(_load / max(1, _cores) * 100, 1) if _load else None
            except Exception:
                _cpu_pct = None
            # Build INSERT against the columns that actually exist in this deployment's
            # scanner_individual_performance table (schema varies: scanner_name/max_threads/
            # scan_speed vs name/total_num_of_threads). A hardcoded column list silently
            # breaks pod capture and can roll back the whole cycle (group rows included).
            ind_sql, ind_params = build_individual_insert(
                engine,
                {
                    "environment_id": env_id,
                    "captured_at_utc": captured_at_utc,
                    "scanner_id": sid,
                    "name": display_name,
                    "status": "Running",
                    "group_name": str(s.get("groupName") or s.get("group") or ""),
                    "running_threads": int(s.get("runningThreads") or 0),
                    "max_threads": int(s.get("totalNumOfThreads") or s.get("maxThreads") or 0),
                    "system_load_average": float(s.get("systemLoadAverage") or 0),
                    "available_processors": int(str(s.get("availableProcessors") or 1).strip() or 1),
                    "physical_memory_mb": _parse_memory_mb(s.get("physicalMemory")),
                    "free_memory_mb": _parse_memory_mb(s.get("freeMemory")),
                    "scanner_threads": int(s.get("scannerThreads") or 0),
                    "scan_speed": float(s.get("avgScanSpeed") or s.get("scanSpeed") or 0),
                    "cpu_pct": _cpu_pct,
                    "update_date_bigid": update_date,
                },
            )
            if ind_sql is not None:
                conn.execute(ind_sql, ind_params)
        conn.commit()
    return  # scanner performance write complete; remainder is dead code from a different context


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
        raw_id = str(item.get("_id") or "").strip()
        if raw_id.startswith("classifier."):
            raw_id = raw_id[len("classifier."):]
        attr_name = str(item.get("name") or item.get("attributeName") or item.get("classifierAttribute") or raw_id or "").strip()
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
            if is_postgresql(engine):
                conn.execute(text("INSERT INTO scan_classifier_attributes (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"), {"n": attr_name})
            else:
                conn.execute(text("INSERT IGNORE INTO scan_classifier_attributes (name) VALUES (:n)"), {"n": attr_name})
            row = conn.execute(text("SELECT id FROM scan_classifier_attributes WHERE name = :n LIMIT 1"), {"n": attr_name}).fetchone()
            if not row:
                continue
            if is_postgresql(engine):
                conn.execute(text("""
                    INSERT INTO scan_insight_attribute_delta_map
                    (scan_id, attribute_id, is_new, is_removed, finding_count, attribute_value, new_count_value, removed_count_value)
                    VALUES (:sid, :aid, :is_new, :is_removed, :fc, :av, :ncv, :rcv)
                    ON CONFLICT (scan_id, attribute_id) DO UPDATE SET
                        is_new = EXCLUDED.is_new,
                        is_removed = EXCLUDED.is_removed,
                        finding_count = EXCLUDED.finding_count,
                        attribute_value = EXCLUDED.attribute_value,
                        new_count_value = EXCLUDED.new_count_value,
                        removed_count_value = EXCLUDED.removed_count_value
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
            else:
                # MySQL
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
        # Store attributes_payload as {"attributes": [...]} so the classifier hits table stays fresh.
        attrs_payload_json = json.dumps({"attributes": attribute_items}, ensure_ascii=False) if attribute_items else None
        if is_postgresql(engine):
            conn.execute(
                text("""
                    INSERT INTO scan_insights
                        (scan_id, distinct_classification_attributes, classifier_attribute_names, new_attributes_count, removed_attributes_count, attributes_payload, updated_at)
                    VALUES
                        (:sid, :dca, :can, :nac, :rac, :ap, CURRENT_TIMESTAMP)
                    ON CONFLICT (scan_id) DO UPDATE SET
                        distinct_classification_attributes = COALESCE(EXCLUDED.distinct_classification_attributes, scan_insights.distinct_classification_attributes),
                        classifier_attribute_names = COALESCE(EXCLUDED.classifier_attribute_names, scan_insights.classifier_attribute_names),
                        new_attributes_count = COALESCE(EXCLUDED.new_attributes_count, scan_insights.new_attributes_count),
                        removed_attributes_count = COALESCE(EXCLUDED.removed_attributes_count, scan_insights.removed_attributes_count),
                        attributes_payload = COALESCE(EXCLUDED.attributes_payload, scan_insights.attributes_payload),
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "sid": str(scan_id),
                    "dca": int(distinct_attrs) if distinct_attrs else None,
                    "can": classifier_attribute_names_text or None,
                    "nac": int(new_attributes_count) if new_attributes_count is not None else None,
                    "rac": int(removed_attributes_count) if removed_attributes_count is not None else None,
                    "ap": attrs_payload_json,
                },
            )
        else:
            # MySQL
            conn.execute(
                text("""
                    INSERT INTO scan_insights
                        (scan_id, distinct_classification_attributes, classifier_attribute_names, new_attributes_count, removed_attributes_count, attributes_payload)
                    VALUES
                        (:sid, :dca, :can, :nac, :rac, :ap)
                    ON DUPLICATE KEY UPDATE
                        distinct_classification_attributes = COALESCE(VALUES(distinct_classification_attributes), distinct_classification_attributes),
                        classifier_attribute_names = COALESCE(VALUES(classifier_attribute_names), classifier_attribute_names),
                        new_attributes_count = COALESCE(VALUES(new_attributes_count), new_attributes_count),
                        removed_attributes_count = COALESCE(VALUES(removed_attributes_count), removed_attributes_count),
                        attributes_payload = COALESCE(VALUES(attributes_payload), attributes_payload),
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "sid": str(scan_id),
                    "dca": int(distinct_attrs) if distinct_attrs else None,
                    "can": classifier_attribute_names_text or None,
                    "nac": int(new_attributes_count) if new_attributes_count is not None else None,
                    "rac": int(removed_attributes_count) if removed_attributes_count is not None else None,
                    "ap": attrs_payload_json,
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


def track_once(
    engine,
    environment_ids: Optional[set[Any]] = None,
    slot_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    One tracker cycle.

    Key design goals:
    - Always prefer tracking *running* scans quickly (so we don't miss 10-min slots).
    - Avoid slow attribute-delta fetches unless needed.
    - Provide actionable counters for heartbeat diagnostics.
    """
    cycle_slot_minutes = int(slot_minutes or (_fast_slot_minutes() if environment_ids else _slot_minutes()))
    envs = load_active_environments(engine)
    if environment_ids is not None:
        allowed = {str(value) for value in environment_ids}
        envs = [env for env in envs if str(env.get("id")) in allowed]
    ensure_classifier_mapping_tables(engine)

    # Silently mark orphaned scans (running status but workflow not active) as stopped.
    # Handles workflow restarts and completion races across parallel workflows.
    try:
        from v2_api.scan_bigid_sync import mark_orphaned_workflow_scans_as_stopped
        orphaned_count = mark_orphaned_workflow_scans_as_stopped(engine)
        if orphaned_count > 0:
            logger.debug(f"tracker: marked {orphaned_count} orphaned workflow scans as stopped")
    except Exception:
        logger.debug("tracker: orphaned scan check failed", exc_info=True)

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
        "backfill_unavailable": 0,
        "backfill_skipped_no_env": 0,
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
        "insights_backfill_unavailable": 0,
        "global_classifier_sync_ok": 0,
        "global_classifier_sync_fail": 0,
        "global_builds_seen": [],
        "env_version_sync_ok": 0,
        "env_version_sync_fail": 0,
        "env_version_updated": 0,
        "scan_parts_calls": 0,
        "scan_parts_records": 0,
        "scan_parts_fail": 0,
    }

    # Attribute-delta fetch can be heavy. Keep it enabled for running scans,
    # but throttle it so it never blocks progress snapshots.
    try:
        attrs_running_ttl_min = int(_getenv_str('TRACKER_ATTRS_RUNNING_TTL_MINUTES', '20') or '20')
    except Exception:
        attrs_running_ttl_min = 20
    try:
        attrs_max_running_per_cycle = int(_getenv_str('TRACKER_ATTRS_MAX_RUNNING_PER_CYCLE', '1') or '1')
    except Exception:
        attrs_max_running_per_cycle = 1
    try:
        attrs_timeout_running_s = int(_getenv_str('TRACKER_ATTRS_TIMEOUT_SECONDS_RUNNING', '8') or '8')
    except Exception:
        attrs_timeout_running_s = 8
    try:
        attrs_timeout_terminal_s = int(_getenv_str('TRACKER_ATTRS_TIMEOUT_SECONDS_TERMINAL', '20') or '20')
    except Exception:
        attrs_timeout_terminal_s = 20
    try:
        attrs_max_pages_running = int(_getenv_str('TRACKER_ATTRS_MAX_PAGES_RUNNING', '2') or '2')
    except Exception:
        attrs_max_pages_running = 2
    try:
        attrs_max_pages_terminal = int(_getenv_str('TRACKER_ATTRS_MAX_PAGES_TERMINAL', '10') or '10')
    except Exception:
        attrs_max_pages_terminal = 10
    try:
        attrs_max_seconds_running = int(_getenv_str('TRACKER_ATTRS_MAX_SECONDS_RUNNING', '12') or '12')
    except Exception:
        attrs_max_seconds_running = 12
    try:
        attrs_max_seconds_terminal = int(_getenv_str('TRACKER_ATTRS_MAX_SECONDS_TERMINAL', '60') or '60')
    except Exception:
        attrs_max_seconds_terminal = 60

    attrs_running_sem = threading.Semaphore(max(0, int(attrs_max_running_per_cycle)))

    def _should_fetch_attrs_for_running(scan_id: str) -> bool:
        if attrs_running_ttl_min <= 0:
            return True
        key = f'attrs_running_last::{str(scan_id)}'
        last = _parse_iso_dt(_kv_get(engine, key))
        if not last:
            return True
        try:
            age_min = (datetime.utcnow() - last).total_seconds() / 60.0
            return bool(age_min >= float(attrs_running_ttl_min))
        except Exception:
            return True


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
        state = str(si.get("state") or "").strip()
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

    def _load_backfill_benchmark_scan_pairs(limit: int = 6) -> List[Dict[str, Any]]:
        """
        Backfill missing scan_insights for recent benchmark scans.
        Uses auto_scan_benchmarks as the seed list.

        Three hard rules, learned from 404-spamming BigID with February scans:

        1. The scan's environment must resolve to a *currently active* environment
           (from auto_scan_benchmarks or scan_parents). There is no "try any env"
           fallback — asking env A for a scan that ran on env B is a guaranteed 404.
        2. The scan must be inside BigID's insights retention window
           (INSIGHTS_RETENTION_DAYS); older insights are purged and can never be fetched.
        3. Scans already marked insights-unavailable (a prior 404) are skipped forever,
           so a handful of dead rows can't permanently occupy the small backfill budget.

        Historical one-offs outside these rules are left to the manual "Sync now" path.
        """
        active_sql = _te_is_active_sql(engine, "te")
        try:
            cutoff = _now_utc_naive() - timedelta(days=INSIGHTS_RETENTION_DAYS)
        except Exception:
            cutoff = _now_utc_naive() - timedelta(days=14)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(f"""
                        SELECT ab.scan_id, te.id, te.base_url
                        FROM auto_scan_benchmarks ab
                        LEFT JOIN scan_parents sp ON sp.scan_id = ab.scan_id
                        JOIN test_environments te
                            ON te.id = COALESCE(ab.environment_id, sp.environment_id)
                        LEFT JOIN scan_insights si ON si.scan_id = ab.scan_id
                        WHERE
                            ab.scan_id IS NOT NULL
                            AND ab.scan_id <> ''
                            AND {active_sql}
                            AND COALESCE(te.base_url, '') <> ''
                            AND COALESCE(ab.scan_end_time, ab.scan_start_time, ab.updated_at) >= :cutoff
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
                    {"cutoff": cutoff, "lim": int(limit) * 4},
                ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows or []:
                sid = str(r[0] or "").strip()
                base_url = str(r[2] or "").strip()
                if not sid or not base_url:
                    continue
                # Rule 3: never re-ask for insights BigID has already purged.
                if _kv_get(engine, f"{INSIGHTS_UNAVAILABLE_KV_PREFIX}{sid}"):
                    continue
                try:
                    eid = int(r[1]) if r[1] is not None else None
                except Exception:
                    eid = None
                out.append({"scan_id": sid, "environment_id": eid, "base_url": base_url})
                if len(out) >= int(limit):
                    break
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
                logger.debug("tracker: suppressed exception", exc_info=True)
        metrics["index_pages"] += 1
        metrics["index_scans"] += int(wrote)
        # advance cursor
        _kv_set(engine, skip_key, str(skip + int(page_limit)))
        return wrote

    def _load_scan_parents_needing_insights(limit: int = 50) -> List[Dict[str, Any]]:
        """
        DB-driven work queue: any scan in scan_parents that is missing scan_insights
        or missing key classification fields needed by the dashboard.

        NOTE: We deliberately do NOT re-queue terminal scans to chase a real
        completedAt/runningTime from the insights API. Verified against BigID 25.6:
        the parent-scan insights endpoint keeps returning the placeholder
        "Scan is still in progress..." for completedAt/runningTime even hours after a
        scan is terminal — so it can never supply terminal timings. Authoritative scan
        timing comes from scan_parents.started_at/completed_at (parent-scans API).

        Scope: only backfill scans that belong to a workflow run currently in
        'running' state. BigID purges insights after a retention window, so old scans
        outside any active workflow 404 ("Scan insight wasn't found") on every cycle and
        spam BigID logs. Scans of a running workflow are recent enough that BigID still
        has their insights; anything else is skipped here (use manual "Sync now" for
        historical one-offs).
        """
        active_sql = _te_is_active_sql(engine, "te")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(f"""
                        SELECT
                            sp.scan_id,
                            sp.environment_id,
                            sp.scan_name,
                            te.base_url
                        FROM scan_parents sp
                        JOIN test_environments te ON te.id = sp.environment_id
                        LEFT JOIN scan_insights si ON si.scan_id = sp.scan_id
                        WHERE {active_sql}
                          AND sp.environment_id IS NOT NULL
                          AND EXISTS (
                            SELECT 1
                            FROM workflow_steps ws2
                            INNER JOIN workflow_runs wr2 ON wr2.id = ws2.run_id
                            WHERE ws2.scan_id = sp.scan_id
                              AND LOWER(TRIM(COALESCE(wr2.status, ''))) = 'running'
                          )
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
                sid = str(r[0] or "").strip()
                # Already 404'd once — BigID purged it, so stop asking.
                if sid and _kv_get(engine, f"{INSIGHTS_UNAVAILABLE_KV_PREFIX}{sid}"):
                    continue
                out.append({
                    "scan_id": sid,
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

        # Keep environment version updated from /about (throttled).
        try:
            ttl = int(os.getenv('TRACKER_ABOUT_TTL_MINUTES', '60') or '60')
        except Exception:
            ttl = 60
        try:
            okv, msgv = sync_env_version_from_about(engine, env, token, ttl_minutes=ttl, force=False)
            if okv:
                metrics['env_version_sync_ok'] += 1
                if str(msgv).startswith('updated:'):
                    metrics['env_version_updated'] += 1
            else:
                metrics['env_version_sync_fail'] += 1
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)

        # Global classifier list + metadata snapshots (throttled inside helpers; runs even with no active scans).
        try:
            if sync_global_classifiers_for_env_build(engine, env, token):
                metrics["global_classifier_sync_ok"] += 1
            else:
                metrics["global_classifier_sync_fail"] += 1
        except Exception:
            metrics["global_classifier_sync_fail"] += 1

        # Service status is recorded every cycle (throttled to TRACKER_SERVICE_STATUS_MINUTES),
        # not only while a scan runs, so the history has no gaps between scans.
        _capture_service_status_throttled(engine, env, token)

        try:
            scans = _fetch_running_parent_scans(base_url, token)
            if scans:
                try:
                    _capture_scanner_perf_throttled(engine, env, token, metrics)
                except Exception:
                    logger.debug("tracker: suppressed exception", exc_info=True)
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
            # The DB status is the source of truth. A scan already terminal there (including a manual
            # Stop) is not written back to running and not polled, even if BigID still lists it as running.
            if _is_terminal_state(_load_stored_scan_status(engine, scan_id)):
                continue
            try:
                upsert_scan_parent_record(engine, env_id, scan, status)
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
            if not _is_running_state(status):
                continue
            jobs.append((base_url, env, scan_id, scan_name, status))

    # DB-driven safety net: keep tracking scans that DB believes are still running
    db_running = load_running_scan_env_rows(engine)
    wf_running = load_workflow_running_scan_env_rows(engine)
    if environment_ids is not None:
        allowed = {str(value) for value in environment_ids}
        db_running = [row for row in db_running if str(row.get("environment_id")) in allowed]
        wf_running = [row for row in wf_running if str(row.get("environment_id")) in allowed]
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

    metrics["wf_queue_rows"] = len(wf_running or [])
    _emit_tracker_queue_audit(engine, metrics)

    # Scanner samples for every env with an active scan (full scan duration, not only BigID "running" list).
    envs_to_sample: Dict[int, Dict[str, Any]] = {}
    for env_active in load_envs_with_active_scans(engine) or []:
        try:
            eid_a = int(env_active.get("id"))
            envs_to_sample[eid_a] = env_by_id.get(eid_a) or env_active
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
    for job in jobs:
        _burl, env_row_job, _sid, _sname, _pst = job
        eid_job = env_row_job.get("id") if isinstance(env_row_job, dict) else None
        if eid_job is not None:
            envs_to_sample[int(eid_job)] = env_row_job
    metrics["scanner_envs_sampled"] = len(envs_to_sample)
    for _eid, env_row_sample in envs_to_sample.items():
        try:
            tok_sample = get_token_for_env(env_row_sample)
            _capture_scanner_perf_throttled(engine, env_row_sample, tok_sample, metrics)
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)

    # Workflow-driven safety net: track scans referenced by workflow_steps scan_running
    for r in (wf_running or []):
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
                str(r.get("scan_status") or "In Progress"),
            )
        )
        seen_scan_ids.add(sid)

    # Final-row safety net: scans that finished but never got a terminal scan_progress row.
    # Re-process them once so the closing 'Completed' (or other terminal) row is written.
    try:
        missing_terminal = load_scans_missing_terminal_progress_row(engine)
    except Exception:
        missing_terminal = []
    if environment_ids is not None:
        allowed = {str(value) for value in environment_ids}
        missing_terminal = [row for row in missing_terminal if str(row.get("environment_id")) in allowed]
    metrics["missing_terminal_seen"] = len(missing_terminal)
    for r in (missing_terminal or []):
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
        insight: Optional[Dict[str, Any]] = None
        attempted = 0
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
                insight = None

        if insight is None:
            # Finished scans drop off the "running" parent-scans list; insights may 404/timeout.
            # Still update scan_parents from GET /parent-scans/{id} so workflow reconciliation can proceed.
            try:
                parent = fetch_parent_scan_by_id(base_url, token, scan_id)
                if isinstance(parent, dict) and parent:
                    pst = resolve_scan_status_value(parent)
                    upsert_scan_parent_record(engine, env_row.get("id"), parent, pst)
                    if _is_terminal_state(pst):
                        # The scan is no longer in progress, so close it out in scan_progress too —
                        # insights can't tell us, and otherwise its last row stays 'In Progress'.
                        ensure_terminal_progress_row(
                            engine,
                            env_row.get("id"),
                            scan_id,
                            scan_name,
                            pst,
                            completed_at_raw=parent.get("completedAt") or parent.get("endTime") or parent.get("endedAt"),
                        )
                        try:
                            metrics["scan_parts_records"] += sync_scan_part_records_for_scan(
                                engine, base_url, env_row, token, scan_id, parent, scan_status=pst
                            )
                            metrics["scan_parts_calls"] += 1
                        except Exception:
                            metrics["scan_parts_fail"] += 1
                            logger.debug("tracker: scan parts sync failed", exc_info=True)
                        return True, "parent_terminal", attempted or 1
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
            return False, "insights", attempted or 2

        try:
            # Use insights payload to keep scan_parents status accurate (running -> terminal, etc.)
            scan_row, insight_state, insight_name = _scan_row_from_insights(scan_id, insight)
            best_state = _choose_best_state(parent_state, insight_state)
            scan_row["state"] = best_state
            try:
                upsert_scan_parent_record(engine, env_row.get("id"), scan_row, best_state)
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)

            # Skip first snapshot if scan started less than 30 seconds ago — avoid zero-progress noise.
            _scan_started_at = _parse_iso_dt(str(scan_row.get("startedAt") or ""))
            _too_new = (
                _scan_started_at is not None
                and (_now_utc_naive() - _scan_started_at).total_seconds() < 30
            )

            # Capture subscan IDs before writing progress so the child-scan API can supply
            # BigID's distinct "Scanned Parts" counters.
            try:
                metrics["scan_parts_records"] += sync_scan_part_records_for_scan(
                    engine, base_url, env_row, token, scan_id, insight, scan_status=best_state
                )
                metrics["scan_parts_calls"] += 1
            except Exception:
                metrics["scan_parts_fail"] += 1
                logger.debug("tracker: scan parts sync failed", exc_info=True)
            child_metrics = fetch_child_scan_metrics(engine, base_url, scan_id, token)

            # Snapshot row (slot-aligned) + normalized insights summary
            cau = None
            if not _too_new:
                cau = upsert_scan_progress_record(
                    engine,
                    env_row.get("id"),
                    scan_id,
                    scan_name or insight_name,
                    insight,
                    forced_state=best_state,
                    child_metrics=child_metrics,
                    slot_minutes=cycle_slot_minutes,
                )
            upsert_scan_insights_record(engine, scan_id, insight)

            # Scanner metrics for the whole time the scan is running (groups + pods every ~TRACKER_SCANNER_SAMPLE_MINUTES).
            try:
                cap_at = cau.replace(microsecond=0) if cau is not None else _now_utc_naive().replace(second=0, microsecond=0)
                if _is_terminal_state(best_state):
                    _capture_scanner_perf_throttled(
                        engine, env_row, token, metrics, force=True, captured_at=cap_at
                    )
                else:
                    _capture_scanner_perf_throttled(
                        engine, env_row, token, metrics, captured_at=cap_at
                    )
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)

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
                            
                            # NEW: Also sync full details for the classifier_details_snapshots table
                            try:
                                sync_classifier_details_for_env_build(engine, env_row, token, str(bver), force=True)
                            except Exception:
                                logger.debug("tracker: suppressed exception", exc_info=True)
                                
                            metrics["global_classifier_sync_ok"] += 1
                            try:
                                metrics["global_builds_seen"].append(str(bver))
                            except Exception:
                                logger.debug("tracker: suppressed exception", exc_info=True)
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
                                logger.debug("tracker: suppressed exception", exc_info=True)
                            # Remember last synced build for this env (best-effort).
                            try:
                                env_key = str(eid if eid is not None else base_url.rstrip("/"))
                                _kv_set(engine, f"global_cls_last_build::{env_key}", str(bver))
                            except Exception:
                                logger.debug("tracker: suppressed exception", exc_info=True)
                        else:
                            metrics["global_classifier_sync_fail"] += 1
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)

            # Attribute delta fetch can be slow; only do it when useful.
            try:
                needs_terminal_attrs = bool(_needs_attribute_deltas(scan_id) or _needs_attribute_delta_counts(scan_id))
                if _is_terminal_state(best_state):
                    if needs_terminal_attrs:
                        attrs = fetch_scan_classifier_attributes(
                            base_url,
                            scan_id,
                            get_token_for_env(env_row),
                            max_pages=int(attrs_max_pages_terminal),
                            request_timeout_seconds=int(attrs_timeout_terminal_s),
                            max_seconds_total=int(attrs_max_seconds_terminal),
                        )
                        upsert_scan_attribute_deltas(engine, scan_id, attrs)
                else:
                    acquired = False
                    try:
                        acquired = bool(attrs_running_sem.acquire(blocking=False))
                        if acquired and _should_fetch_attrs_for_running(scan_id):
                            _kv_set(engine, f"attrs_running_last::{str(scan_id)}", _now_utc_naive().strftime('%Y-%m-%dT%H:%M:%S'))
                            attrs = fetch_scan_classifier_attributes(
                                base_url,
                                scan_id,
                                get_token_for_env(env_row),
                                max_pages=int(attrs_max_pages_running),
                                request_timeout_seconds=int(attrs_timeout_running_s),
                                max_seconds_total=int(attrs_max_seconds_running),
                            )
                            upsert_scan_attribute_deltas(engine, scan_id, attrs)
                    finally:
                        try:
                            if acquired:
                                attrs_running_sem.release()
                        except Exception:
                            logger.debug("tracker: suppressed exception", exc_info=True)
            except Exception:
                return True, "ok_attrs_failed", attempted

            # When scan is terminal, compute and persist throughput for existing auto row.
            try:
                if _is_terminal_state(best_state):
                    update_auto_scan_benchmarks_throughput(
                        engine,
                        scan_id,
                        started_at=scan_row.get("startedAt"),
                        completed_at=scan_row.get("completedAt"),
                    )
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)

            return True, "ok", attempted
        except Exception as ex:
            # Log the actual error for debugging
            import traceback
            print(f"[{_now_utc_naive().isoformat()}Z] WRITE ERROR for scan {scan_id}: {ex}", flush=True)
            print(f"[{_now_utc_naive().isoformat()}Z] Traceback: {traceback.format_exc()}", flush=True)
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
                    logger.debug("tracker: suppressed exception", exc_info=True)
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
    for row in (backfill_pairs or []):
        sid = str(row.get("scan_id") or "").strip()
        eid = row.get("environment_id")
        # Only ever ask the scan's own active environment. No "any env" fallback:
        # querying the wrong env 404s forever and spams BigID's API logs.
        env_row = env_by_id.get(eid) if eid is not None else None
        if not env_row:
            env_row = env_by_base.get(str(row.get("base_url") or "").strip().rstrip("/"))
        base_url = str((env_row or {}).get("base_url") or "").strip()
        if not sid or not base_url:
            metrics["backfill_skipped_no_env"] += 1
            continue
        try:
            tok = get_token_for_env(env_row)
            insight = fetch_scan_insights(base_url, sid, tok)
            upsert_scan_insights_record(engine, sid, insight)
            try:
                sync_scan_part_records_for_scan(
                    engine, base_url, env_row, tok, sid, insight,
                    scan_status=_extract_live_state(insight),
                )
            except Exception:
                logger.debug("tracker: scan parts backfill failed", exc_info=True)
            # Fetch attribute deltas when needed for counts / names (may be slow).
            if _needs_attribute_deltas(sid) or _needs_attribute_delta_counts(sid):
                attrs = fetch_scan_classifier_attributes(base_url, sid, tok)
                upsert_scan_attribute_deltas(engine, sid, attrs)
            metrics["backfill_ok"] += 1
        except InsightsNotFound:
            # Purged by BigID (or never ran here) — retiring the row keeps the small
            # backfill budget available for scans that can actually be filled.
            _kv_set(engine, f"{INSIGHTS_UNAVAILABLE_KV_PREFIX}{sid}", _now_utc_naive().isoformat())
            metrics["backfill_unavailable"] += 1
            print(
                f"[{_now_utc_naive().isoformat()}Z] backfill: insights unavailable (404), "
                f"will not retry scan_id={sid} env={normalize_base_url(base_url)}",
                flush=True,
            )
        except Exception:
            metrics["backfill_fail"] += 1

    # Index all scans (all states) incrementally, then backfill missing insights.
    # This is the main mechanism to eventually populate scan_insights for every scan.
    try:
        for env in envs:
            try:
                _index_some_parent_scans_all_states(env, page_limit=500)
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
    except Exception:
        logger.debug("tracker: suppressed exception", exc_info=True)

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
            try:
                sync_scan_part_records_for_scan(
                    engine, base_url, env_row, tok, scan_id, insight,
                    scan_status=_extract_live_state(insight),
                )
            except Exception:
                logger.debug("tracker: scan parts manual sync failed", exc_info=True)
            # Pull attributes only when needed for classifier counts / delta counts.
            if _needs_attribute_deltas(scan_id) or _needs_attribute_delta_counts(scan_id):
                attrs = fetch_scan_classifier_attributes(base_url, scan_id, tok)
                upsert_scan_attribute_deltas(engine, scan_id, attrs)
            return True, "ok"
        except InsightsNotFound:
            # Retire the scan: a 404 here can never turn into a 200 later.
            _kv_set(engine, f"{INSIGHTS_UNAVAILABLE_KV_PREFIX}{scan_id}", _now_utc_naive().isoformat())
            print(
                f"[{_now_utc_naive().isoformat()}Z] insights_backfill: insights unavailable (404), "
                f"will not retry scan_id={scan_id} env={normalize_base_url(base_url)}",
                flush=True,
            )
            return False, "unavailable"
        except Exception:
            return False, "fail"

    if backlog:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(backfill_one, r) for r in backlog]
            for f in as_completed(futs):
                try:
                    ok, reason = f.result()
                except Exception:
                    ok, reason = False, "fail"
                if ok:
                    metrics["insights_backfill_ok"] += 1
                elif reason == "unavailable":
                    metrics["insights_backfill_unavailable"] += 1
                else:
                    metrics["insights_backfill_fail"] += 1

    return metrics


def _known_environment_id_for_scan(engine, scan_id: str) -> Optional[int]:
    """The environment a scan is already known to belong to (scan_parents, else workflow_steps)."""
    sid = str(scan_id or "").strip()
    if not engine or not sid:
        return None
    for sql in (
        "SELECT environment_id FROM scan_parents WHERE scan_id = :sid AND environment_id IS NOT NULL LIMIT 1",
        "SELECT environment_id FROM workflow_steps WHERE scan_id = :sid AND environment_id IS NOT NULL LIMIT 1",
    ):
        try:
            with engine.connect() as conn:
                row = conn.execute(text(sql), {"sid": sid}).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            logger.debug("tracker: suppressed exception", exc_info=True)
    return None


def run_for_scan_id(engine, scan_id: str, verbose: bool = True) -> bool:
    """
    Force-update a specific scan_id. Writes scan_progress + scan_insights + attribute delta map.

    When the scan's environment is already known (scan_parents / workflow_steps) and that env is
    active, only that env is queried — probing every active env would 404 in all the others and
    spam BigID's API logs. Only an unknown env falls back to trying each active env in turn.
    """
    scan_id = str(scan_id or "").strip()
    if not scan_id:
        return False
    envs = load_active_environments(engine)
    known_eid = _known_environment_id_for_scan(engine, scan_id)
    if known_eid is not None:
        scoped = [e for e in envs if e.get("id") is not None and int(e.get("id")) == int(known_eid)]
        if scoped:
            envs = scoped
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
            try:
                sync_scan_part_records_for_scan(
                    engine, base_url, env, token, scan_id, insight,
                    scan_status=state, allow_terminal=True,
                )
            except Exception:
                logger.debug("tracker: scan parts manual sync failed", exc_info=True)
            child_metrics = fetch_child_scan_metrics(engine, base_url, scan_id, token)
            cau = upsert_scan_progress_record(
                engine, env_id, scan_id, "", insight, child_metrics=child_metrics
            )
            upsert_scan_insights_record(engine, scan_id, insight)
            # Insights are reachable after all — un-retire the scan for auto-backfill.
            _kv_del(engine, f"{INSIGHTS_UNAVAILABLE_KV_PREFIX}{scan_id}")
            if cau is not None:
                try:
                    capture_scanner_performance_for_env(
                        engine, env, captured_at=cau.replace(microsecond=0)
                    )
                except Exception:
                    logger.debug("tracker: suppressed exception", exc_info=True)
            try:
                attrs = fetch_scan_classifier_attributes(base_url, scan_id, token)
                upsert_scan_attribute_deltas(engine, scan_id, attrs)
            except Exception:
                logger.debug("tracker: suppressed exception", exc_info=True)
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


def quick_completion_check(engine) -> int:
    """
    Lightweight completion fast-poll. Runs between full tracker cycles (every 2 min).
    Checks if any DB-running scans have gone terminal by comparing against BigID's
    current running list. Returns number of scans detected as newly terminal.
    Full data capture (scanner perf, attributes, throughput) is handled by the caller
    triggering track_once() when this returns > 0.
    """
    completed_count = 0
    try:
        db_running = load_running_scan_env_rows(engine)
        wf_running = load_workflow_running_scan_env_rows(engine)
        all_running: Dict[str, Dict[str, Any]] = {}
        for r in list(db_running) + list(wf_running):
            sid = str(r.get('scan_id') or '').strip()
            if sid and sid not in all_running:
                all_running[sid] = r
    except Exception:
        return 0

    if not all_running:
        return 0

    try:
        envs = load_active_environments(engine)
        env_by_id: Dict[Any, Dict[str, Any]] = {e.get('id'): e for e in envs if isinstance(e, dict)}
    except Exception:
        return 0

    token_cache: Dict[str, str] = {}

    def _tok(env_row: Dict[str, Any]) -> str:
        base = str(env_row.get('base_url') or '').strip().rstrip('/')
        if base in token_cache:
            return token_cache[base]
        tok = Get_auth_token(base, env_row)
        token_cache[base] = tok
        return tok

    # Group by env
    by_env: Dict[Any, List[Dict[str, Any]]] = {}
    for sid, r in all_running.items():
        by_env.setdefault(r.get('environment_id'), []).append(r)

    for env_id, scans in by_env.items():
        env_row = env_by_id.get(env_id)
        if not env_row:
            continue
        base_url = str(env_row.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            continue
        try:
            tok = _tok(env_row)
        except Exception:
            continue
        try:
            running_now = fetch_parent_scans(base_url, tok, states=RUNNING_STATES, limit=500)
            running_ids_api = {str(s.get('_id') or s.get('id') or '') for s in running_now if isinstance(s, dict)}
        except Exception:
            continue

        for r in scans:
            sid = str(r.get('scan_id') or '').strip()
            if not sid or sid in running_ids_api:
                continue  # still running

            # No longer in running list — confirm terminal via the parent-scans API
            # (authoritative for terminal state + completedAt). Do NOT use the insights
            # API here: it lags and can keep returning "Started" / "Scan is still in
            # progress..." long after completion, which would suppress detection. See
            # bigid-insights-no-terminal-timings memo.
            try:
                parent = fetch_parent_scan_by_id(base_url, tok, sid)
                raw_state = resolve_scan_status_value(parent)
                if not raw_state or not _is_terminal_state(raw_state):
                    continue  # not terminal yet (or API lag); recheck next poll
                completed_count += 1
                print(
                    f"[{_now_utc_naive().isoformat()}Z] quick_check: scan {sid} "
                    f"terminal={raw_state} env={env_row.get('env_name', env_id)}",
                    flush=True,
                )
            except Exception as ex:
                print(f"[{_now_utc_naive().isoformat()}Z] quick_check: scan {sid} error={ex}", flush=True)

    return completed_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-seconds", type=int, default=1200, help="Polling interval in seconds (default: 1200)")
    ap.add_argument(
        "--align-10min",
        action="store_true",
        default=True,
        help="Align runs to UTC slot boundaries (TRACKER_SLOT_MINUTES, default 20)",
    )
    ap.add_argument(
        "--no-align-10min",
        action="store_false",
        dest="align_10min",
        help="Disable alignment to slot boundaries",
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
    ap.add_argument(
        "--validate-queues",
        action="store_true",
        help="Run tracker queue audit (PostgreSQL is_active + workflow scan_running) and exit",
    )
    args = ap.parse_args()

    engine = create_engine(
        DB_CONNECTION_STRING,
        pool_pre_ping=True,
        pool_recycle=3600,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10}
    )
    ensure_scan_tracking_tables(engine)
    ensure_scan_progress_table(engine)

    if bool(args.validate_queues):
        import json as _json

        audit = audit_tracker_queues(engine)
        print(_json.dumps(audit, indent=2), flush=True)
        return 0 if audit.get("ok") else 2

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

    def seconds_until_next_slot_boundary(slot_minutes: int) -> int:
        now = _now_utc_naive()
        base = now.replace(second=0, microsecond=0)
        next_min = ((base.minute // slot_minutes) + 1) * slot_minutes
        if next_min >= 60:
            target = base.replace(minute=0) + timedelta(hours=1)
        else:
            target = base.replace(minute=next_min)
        delta = (target - now).total_seconds()
        return max(1, int(delta))

    last_slot_key = None
    slot_minutes = _slot_minutes()
    fast_slot_minutes = _fast_slot_minutes()
    fast_env_ids: set[Any] = set()
    fast_poll_intervals: Dict[Any, int] = {}
    while True:
        cycle_environment_ids: Optional[set[Any]] = None
        if args.align_10min and last_slot_key is not None:
            now = _now_utc_naive()
            # Match the due minute rather than a two-second window. A cycle can wake a few
            # seconds after the boundary and must still record that slot once.
            normal_due = now.minute % slot_minutes == 0
            due_fast_env_ids = {
                env_id
                for env_id, interval in fast_poll_intervals.items()
                if now.minute % interval == 0
            }
            fast_due = bool(due_fast_env_ids)
            if normal_due:
                slot_key = f"normal:{now.strftime('%Y-%m-%dT%H:%M')}"
            elif fast_due:
                cycle_environment_ids = due_fast_env_ids
                slot_key = f"fast:{now.strftime('%Y-%m-%dT%H:%M')}"
            else:
                waits = [seconds_until_next_slot_boundary(slot_minutes)]
                if fast_env_ids:
                    waits.append(seconds_until_next_slot_boundary(fast_slot_minutes))
                time.sleep(max(1, min(waits)))
                continue
            if slot_key == last_slot_key:
                time.sleep(1)
                continue
            last_slot_key = slot_key
        elif not args.align_10min and last_slot_key is not None:
            # In unaligned mode, only the fast-polling environments need the
            # shorter interval; the normal cycle remains configurable.
            cycle_environment_ids = set(fast_env_ids) if fast_env_ids else None

        started = _now_utc_naive()
        _hb_update(engine, status="running", message="cycle_start", started=started)
        try:
            cycle_slot_minutes = (
                min(fast_poll_intervals.get(env_id, fast_slot_minutes) for env_id in cycle_environment_ids)
                if cycle_environment_ids
                else slot_minutes
            )
            m = track_once(
                engine,
                environment_ids=cycle_environment_ids,
                slot_minutes=cycle_slot_minutes,
            ) or {}
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
                f"insights_backfill_unavailable={m.get('insights_backfill_unavailable', 0)} "
                f"backfill_seen={m.get('backfill_seen', 0)} "
                f"backfill_ok={m.get('backfill_ok', 0)} "
                f"backfill_fail={m.get('backfill_fail', 0)} "
                f"backfill_unavailable={m.get('backfill_unavailable', 0)} "
                f"backfill_skipped_no_env={m.get('backfill_skipped_no_env', 0)} "
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

        if last_slot_key is None:
            last_slot_key = f"initial:{started.strftime('%Y-%m-%dT%H:%M:%S')}"

        # Refresh the per-environment fast-poll set after each cycle. A normal
        # cycle discovers new near-complete scans; fast cycles drop environments
        # as soon as their scans become terminal.
        # Re-read the latest two snapshots after every cycle so remaining
        # objects and current speed can change the next interval immediately.
        fast_poll_intervals = load_fast_poll_environment_intervals(engine)
        fast_env_ids = set(fast_poll_intervals)
        if fast_poll_intervals:
            fast_slot_minutes = min(fast_poll_intervals.values())

        # Fast-poll between full cycles: check every 2 minutes for scan completion,
        # but wake at the next normal or fast tracker boundary as appropriate.
        quick_check_interval = 120  # seconds
        if args.align_10min:
            while True:
                waits = [seconds_until_next_slot_boundary(slot_minutes)]
                if fast_env_ids:
                    waits.append(seconds_until_next_slot_boundary(fast_slot_minutes))
                boundary_wait = max(1, min(waits))
                if boundary_wait <= quick_check_interval:
                    time.sleep(boundary_wait)
                    break
                time.sleep(quick_check_interval)
                try:
                    sample_service_status_all_envs(engine)
                except Exception as sex:
                    print(f"[{_now_utc_naive().isoformat()}Z] service_status_sampler error: {sex}", flush=True)
                try:
                    sample_scanner_perf_all_envs(engine)
                except Exception as pex:
                    print(f"[{_now_utc_naive().isoformat()}Z] scanner_perf_sampler error: {pex}", flush=True)
                try:
                    n = quick_completion_check(engine)
                    if n > 0:
                        print(
                            f"[{_now_utc_naive().isoformat()}Z] quick_check: {n} scan(s) terminal — triggering full cycle",
                            flush=True,
                        )
                        _hb_update(engine, status="running", message="full_cycle_after_completion", started=_now_utc_naive())
                        m2 = track_once(engine, slot_minutes=slot_minutes) or {}
                        fast_poll_intervals = load_fast_poll_environment_intervals(engine)
                        fast_env_ids = set(fast_poll_intervals)
                        if fast_poll_intervals:
                            fast_slot_minutes = min(fast_poll_intervals.values())
                        _hb_update(engine, status="ok", message=f"full_cycle_done snapshots={m2.get('snapshots_ok',0)}", finished=_now_utc_naive())
                        print(
                            f"[{_now_utc_naive().isoformat()}Z] full_cycle_after_completion done "
                            f"snapshots={m2.get('snapshots_ok',0)} envs={m2.get('envs',0)}",
                            flush=True,
                        )
                except Exception as qex:
                    print(f"[{_now_utc_naive().isoformat()}Z] quick_check error: {qex}", flush=True)
        else:
            sleep_remaining = max(
                5,
                int(fast_slot_minutes * 60 if fast_env_ids else args.interval_seconds),
            )
            while sleep_remaining > quick_check_interval:
                time.sleep(quick_check_interval)
                sleep_remaining -= quick_check_interval
                try:
                    sample_service_status_all_envs(engine)
                except Exception as sex:
                    print(f"[{_now_utc_naive().isoformat()}Z] service_status_sampler error: {sex}", flush=True)
                try:
                    sample_scanner_perf_all_envs(engine)
                except Exception as pex:
                    print(f"[{_now_utc_naive().isoformat()}Z] scanner_perf_sampler error: {pex}", flush=True)
                try:
                    n = quick_completion_check(engine)
                    if n > 0:
                        print(
                            f"[{_now_utc_naive().isoformat()}Z] quick_check: {n} scan(s) terminal — triggering full cycle",
                            flush=True,
                        )
                        _hb_update(engine, status="running", message="full_cycle_after_completion", started=_now_utc_naive())
                        m2 = track_once(engine, slot_minutes=slot_minutes) or {}
                        fast_poll_intervals = load_fast_poll_environment_intervals(engine)
                        fast_env_ids = set(fast_poll_intervals)
                        if fast_poll_intervals:
                            fast_slot_minutes = min(fast_poll_intervals.values())
                        _hb_update(engine, status="ok", message=f"full_cycle_done snapshots={m2.get('snapshots_ok',0)}", finished=_now_utc_naive())
                except Exception as qex:
                    print(f"[{_now_utc_naive().isoformat()}Z] quick_check error: {qex}", flush=True)

            if sleep_remaining > 0:
                time.sleep(sleep_remaining)


if __name__ == "__main__":
    raise SystemExit(main())

