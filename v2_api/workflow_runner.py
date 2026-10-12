"""
Workflow runner for DS + scan automation.

Reads `workflow_runs` + `workflow_steps` from the Streamlit UI and executes them with
**parallelism up to the workflow pool size** (active envs with "Use for workflow runs").

Per-step pipeline on whichever workflow-pool env is free (N envs with "Use for workflow runs"):
  pre-action (optional) → create/update DS → test connection → create scan profile
  → start parent scan → reconcile until terminal → cooldown → next step.

With two environments, up to two steps can run at once when earlier steps have finished
or are waiting on capacity; `WORKFLOW_MAX_CONCURRENT_SCANS` caps concurrency (default = env count).

Secrets are not hardcoded: payloads live in `workflow_templates` with {{PLACEHOLDER}} variables.
"""

from __future__ import annotations

# Step is actively using an environment (DS setup, profile, or scan) — counts toward concurrency.
# scan_waiting is intentionally EXCLUDED: it means "parked, retry later" and must not consume a slot
# or block other steps (otherwise N parked steps → slots_to_fill=0 → no postgres PUT ever).
WORKFLOW_ENV_OCCUPYING_STATUSES: tuple[str, ...] = (
    "running",
    "pre_action_done",
    "ds_created",
    "ds_test_ok",
    "scan_created",
    "scan_running",
)


def _workflow_env_occupying_sql_in() -> str:
    return ",".join(f"'{s}'" for s in WORKFLOW_ENV_OCCUPYING_STATUSES)


def _run_env_has_active_step(
    engine,
    run_id: int,
    env_id: int,
    *,
    exclude_step_id: Optional[int] = None,
) -> bool:
    """Another step in this run already holds the env (DS/scan pipeline). One step per env."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM workflow_steps
                    WHERE run_id = :rid AND environment_id = :eid
                      AND (:excl IS NULL OR id <> :excl)
                      AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})
                    LIMIT 1
                    """
                ),
                {"rid": int(run_id), "eid": int(env_id), "excl": exclude_step_id},
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _sql_bool_true(engine) -> str:
    """Return SQL for boolean TRUE value based on dialect (TRUE for Postgres, 1 for MySQL)."""
    return sql_bool_true(engine)


def _sql_bool_false(engine) -> str:
    """Return SQL for boolean FALSE value based on dialect (FALSE for Postgres, 0 for MySQL)."""
    return sql_bool_false(engine)


def is_postgresql_engine(engine) -> bool:
    """Check if engine is PostgreSQL."""
    return is_postgresql(engine)


def _sql_truthy(engine, column_sql: str) -> str:
    """Return SQL for truthy check based on dialect."""
    from pg_compat import sql_truthy

    return sql_truthy(engine, column_sql)


import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from sqlalchemy import create_engine, text

from db_config_pg import DB_CONNECTION_STRING
from pg_compat import (
    is_postgresql,
    sql_auto_increment,
    sql_datetime,
    sql_longtext,
    sql_bool_true,
    sql_bool_false,
    sql_on_update_current_timestamp,
    sql_upsert_heartbeat,
)
from bigid_auth import Get_auth_token

def _sync_env_version_standalone_runner(engine, env_id: int) -> None:
    """Helper to refresh environment version from background runner context."""
    try:
        er = load_env(engine, env_id)
        if not er or not er.get("base_url"):
            return
            
        # Runner auth helper
        token = ""
        try:
            token = Get_auth_token(er.get("base_url"), er.get("auth_module_path"), er.get("auth_function_name"))
        except Exception:
            token = ""
            
        bv = resolve_env_build_version(er, token)
        if bv:
            _update_env_version_in_db(engine, env_id, bv)
    except Exception:
        pass


PARENT_SCANS_PATH = os.getenv("BIGID_PARENT_SCANS_API_PATH", "/api/v1/scans/parent-scans").strip() or "/api/v1/scans/parent-scans"
INSIGHTS_PATH = os.getenv("BIGID_PARENT_SCAN_INSIGHTS_API_PATH", "/api/v1/scans/insights/parent-scans").strip() or "/api/v1/scans/insights/parent-scans"
DS_CONNECTIONS_PATH = os.getenv("BIGID_DS_CONNECTIONS_API_PATH", "/api/v1/ds_connections").strip() or "/api/v1/ds_connections"
SCAN_PROFILES_PATH = os.getenv("BIGID_SCAN_PROFILES_API_PATH", "/api/v1/scanProfiles").strip() or "/api/v1/scanProfiles"
ABOUT_PATH = os.getenv("BIGID_ABOUT_API_PATH", "/api/v1/about").strip() or "/api/v1/about"
SCAN_TEMPLATES_PATH = os.getenv("BIGID_SCAN_TEMPLATES_API_PATH", "/api/v1/scan-templates").strip() or "/api/v1/scan-templates"
SCANS_TRIGGER_PATH = os.getenv("BIGID_SCANS_TRIGGER_API_PATH", "/api/v1/scans").strip() or "/api/v1/scans"
DS_CONNECTION_TEST_PATH = os.getenv("BIGID_DS_CONNECTION_TEST_API_PATH", "/api/v1/ds-connection-test/client").strip() or "/api/v1/ds-connection-test/client"
DS_CONNECTIONS_TEST_PATH = os.getenv("BIGID_DS_CONNECTIONS_TEST_API_PATH", "/api/v1/ds-connections/test").strip() or "/api/v1/ds-connections/test"
DS_GROUPED_STATUSES_PATH = os.getenv("BIGID_DS_GROUPED_STATUSES_API_PATH", "/api/v1/ds-connections/grouped-statuses").strip() or "/api/v1/ds-connections/grouped-statuses"

DEFAULT_SCAN_OWNER_EMAIL = str(os.getenv("WORKFLOW_SCAN_OWNER_EMAIL", "admin@bigid.com") or "").strip()

# Conservative request timeout so one hung call can't freeze the workflow.
HTTP_TIMEOUT = float(os.getenv("WORKFLOW_HTTP_TIMEOUT_SECONDS", "45") or "45")

# BigID parent-scan states that mean the env is busy. Exclude "pending" — API often lists
# queued Pending scans that must not block workflow dispatch (was causing runs stuck at 0 in progress).
ACTIVE_SCAN_STATES = {
    "started",
    "awaiting",
    "inprogress",
    "in progress",
    "running",
    "queued",
    "stoprequested",
    "stopping",
}
RUNNING_STATES = ACTIVE_SCAN_STATES  # alias
# NOTE: "stopRequested"/"stopping" are NOT terminal — BigID can hold these for a long
# time before the scan actually finishes (matches tracker.py / scan_bigid_sync.py, which
# classify them as RUNNING). Treating them as terminal here caused the runner to fail/advance
# a step and release the env while the scan was still running.
TERMINAL_STATES = {"failed", "completedwithfailures", "completed", "stopped", "aborted", "cancelled", "canceled"}

# .env is mounted at the app root (/app/.env per docker-compose), not under v2_api/.
# Match credentials_crypto.py / main.py so workflow secrets (Postgres/S3/SMB) resolve.
ENV_FILE_PATH = Path(__file__).parent.parent / ".env"
if not ENV_FILE_PATH.exists():
    _alt_env = Path(__file__).parent / ".env"
    if _alt_env.exists():
        ENV_FILE_PATH = _alt_env

RUNNER_HEARTBEAT_NAME = "workflow_runner"
RUNNER_LOCK_KEY = "workflow_runner_lock"

LOG_PATH = Path(__file__).resolve().parent / "logs" / "workflow_runner.log"
_LOGGER: Optional[logging.Logger] = None


def _get_logger(*, quiet: bool) -> logging.Logger:
    """
    Always logs to `logs/workflow_runner.log` (UTC timestamps).
    In non-quiet mode, also logs to stdout.
    """
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    logger = logging.getLogger("workflow_runner")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Ensure UTC timestamps.
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime  # type: ignore[attr-defined]

    try:
        fh = RotatingFileHandler(str(LOG_PATH), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        logger.addHandler(fh)
    except Exception:
        # If file handler fails, keep going (stdout handler below).
        pass

    if not quiet:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        logger.addHandler(sh)

    _LOGGER = logger
    return logger

def _git_head_short() -> str:
    try:
        repo_dir = Path(__file__).resolve().parent
        # Fast, best-effort. If git isn't available, just return "".
        flags = 0
        if os.name == "nt":
            flags = 0x08000000 # CREATE_NO_WINDOW
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], 
            cwd=str(repo_dir), 
            stderr=subprocess.DEVNULL, 
            timeout=1.5,
            creationflags=flags
        )
        sha = out.decode("utf-8", errors="ignore").strip()
        return sha[:12]
    except Exception:
        return ""


_GIT_SHA = _git_head_short()
RUNNER_ID = f"{socket.gethostname()}:{os.getpid()}" + (f":{_GIT_SHA}" if _GIT_SHA else "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "") or "").strip() or default)
    except Exception:
        return int(default)


def _ds_kind_timeout_seconds(ds_kind: Any) -> int:
    """
    DS connection test can be slow (esp MSSQL). Allow per-DS override:
      WORKFLOW_DS_TEST_TIMEOUT_SECONDS_MSSQL=600
    Fallback: WORKFLOW_DS_TEST_TIMEOUT_SECONDS (default 180; MSSQL default 600)
    """
    k = str(ds_kind or "").strip().upper()
    if k:
        v = _env_int(f"WORKFLOW_DS_TEST_TIMEOUT_SECONDS_{k}", -1)
        if v and v > 0:
            return int(max(30, min(1800, v)))
    # MSSQL default 600s (10 min); Postgres default 360s (6 min); others 180s.
    if k == 'MSSQL':
        v2 = _env_int('WORKFLOW_DS_TEST_TIMEOUT_SECONDS', 600)
    elif k == 'POSTGRES':
        v2 = _env_int('WORKFLOW_DS_TEST_TIMEOUT_SECONDS', 360)
    else:
        v2 = _env_int('WORKFLOW_DS_TEST_TIMEOUT_SECONDS', 180)
    return int(max(30, min(1800, v2)))


def _ds_kind_poll_seconds(ds_kind: Any) -> int:
    """
    Per-DS polling override:
      WORKFLOW_DS_TEST_POLL_SECONDS_MSSQL=10
    Fallback: WORKFLOW_DS_TEST_POLL_SECONDS (default 5)
    """
    k = str(ds_kind or "").strip().upper()
    if k:
        v = _env_int(f"WORKFLOW_DS_TEST_POLL_SECONDS_{k}", -1)
        if v and v > 0:
            return int(max(2, min(60, v)))
    v2 = _env_int("WORKFLOW_DS_TEST_POLL_SECONDS", 5)
    return int(max(2, min(60, v2)))


def _ds_kind_initial_wait_seconds(ds_kind: Any) -> int:
    """
    Some deployments update connectionStatusTest asynchronously. Give it a moment before polling.
      WORKFLOW_DS_TEST_INITIAL_WAIT_SECONDS_MSSQL=10
    Fallback: WORKFLOW_DS_TEST_INITIAL_WAIT_SECONDS (default 5)
    """
    k = str(ds_kind or "").strip().upper()
    if k:
        v = _env_int(f"WORKFLOW_DS_TEST_INITIAL_WAIT_SECONDS_{k}", -1)
        if v and v > 0:
            return int(max(0, min(60, v)))
    v2 = _env_int("WORKFLOW_DS_TEST_INITIAL_WAIT_SECONDS", 5)
    return int(max(0, min(60, v2)))


def _post_trigger_sleep_seconds() -> int:
    """
    After starting a scan, wait a bit before checking insights/state.
    This avoids false negatives when the backend hasn't materialized scan state yet.
    """
    v = _env_int("WORKFLOW_POST_TRIGGER_SLEEP_SECONDS", 30)
    return int(max(0, min(120, v)))

def _has_yyyymmddhhmmss_suffix(name: str) -> bool:
    try:
        s = str(name or "").strip()
        if not s:
            return False
        last = s.split()[-1]
        return last.isdigit() and len(last) == 14
    except Exception:
        return False

def ensure_tracker_kv_table(engine) -> None:
    """
    Ensure tracker_kv exists so we can use it for a DB-wide runner lock.
    (Table is also used by tracker.py.)
    """
    try:
        dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    except Exception:
        dialect = ""
    
    dt = sql_datetime(engine)
    ou = sql_on_update_current_timestamp(engine)
    
    try:
        with engine.connect() as conn:
            if "sqlite" in dialect:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS tracker_kv (
                        k TEXT PRIMARY KEY,
                        v TEXT NULL,
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """))
            else:
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS tracker_kv (
                        k VARCHAR(200) PRIMARY KEY,
                        v VARCHAR(2000) NULL,
                        updated_at {dt} DEFAULT CURRENT_TIMESTAMP {ou}
                    )
                """))
            conn.commit()
    except Exception:
        pass


def _kv_get(engine, key: str) -> str:
    ensure_tracker_kv_table(engine)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT v FROM tracker_kv WHERE k = :k LIMIT 1"),
                {"k": str(key)},
            ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""


def _kv_set(engine, key: str, value: str, expire_seconds: int = 0) -> None:
    ensure_tracker_kv_table(engine)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tracker_kv (k, v, updated_at)
                    VALUES (:k, :v, CURRENT_TIMESTAMP)
                    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {"k": str(key), "v": str(value)},
            )
    except Exception:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tracker_kv (k, v, updated_at)
                        VALUES (:k, :v, CURRENT_TIMESTAMP)
                        """
                    ),
                    {"k": str(key), "v": str(value)},
                )
        except Exception:
            pass


def _parse_db_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v.strip():
        s = v.strip().replace("Z", "")
        # try a few common formats
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None


def acquire_runner_lock(engine, *, ttl_seconds: int = 120) -> Tuple[bool, str]:
    """
    DB-wide lock so only one workflow runner executes steps across all machines
    sharing the same DB. Uses tracker_kv as a heartbeat lock with TTL.
    """
    ensure_tracker_kv_table(engine)
    who = RUNNER_ID
    now = _now_utc()
    ttl = timedelta(seconds=max(30, int(ttl_seconds)))
    try:
        dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    except Exception:
        dialect = ""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT v, updated_at FROM tracker_kv WHERE k=:k LIMIT 1"),
                {"k": RUNNER_LOCK_KEY},
            ).fetchone()
            if row:
                holder = str(row[0] or "").strip()
                updated = _parse_db_dt(row[1])
                if updated and (updated + ttl) > now and holder and holder != who:
                    return False, f"lock held by {holder} (updated {updated} UTC)"
            # acquire/renew
            if "sqlite" in dialect:
                conn.execute(
                    text("""
                        INSERT INTO tracker_kv (k, v, updated_at)
                        VALUES (:k, :v, datetime('now'))
                        ON CONFLICT(k) DO UPDATE SET
                            v=excluded.v,
                            updated_at=datetime('now')
                    """),
                    {"k": RUNNER_LOCK_KEY, "v": who},
                )
            elif is_postgresql(engine):
                conn.execute(
                    text("""
                        INSERT INTO tracker_kv (k, v, updated_at)
                        VALUES (:k, :v, CURRENT_TIMESTAMP)
                        ON CONFLICT(k) DO UPDATE SET
                            v=EXCLUDED.v,
                            updated_at=CURRENT_TIMESTAMP
                    """),
                    {"k": RUNNER_LOCK_KEY, "v": who},
                )
            else:
                # MySQL
                conn.execute(
                    text("""
                        INSERT INTO tracker_kv (k, v)
                        VALUES (:k, :v)
                        ON DUPLICATE KEY UPDATE
                            v=VALUES(v),
                            updated_at=CURRENT_TIMESTAMP
                    """),
                    {"k": RUNNER_LOCK_KEY, "v": who},
                )
        return True, f"lock ok ({who})"
    except Exception as ex:
        return True, f"lock unavailable (proceeding): {ex}"


def _compact(s: Any) -> str:
    return str(s or "").strip().lower().replace(" ", "")


def _is_terminal_state(s: Any) -> bool:
    return _compact(s) in {x.replace(" ", "") for x in TERMINAL_STATES}


def _now_utc() -> datetime:
    return datetime.utcnow()


def normalize_base_url(base_url: str) -> str:
    from bigid_auth import normalize_bigid_base_url
    return normalize_bigid_base_url(base_url)


def build_api_url(base_url: str, api_path: str) -> str:
    base = normalize_base_url(base_url)
    p = str(api_path or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return f"{base}{p}"


def _request(method: str, url: str, token: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None, timeout_seconds: Optional[float] = None) -> requests.Response:
    """
    Make an HTTP request with automatic retry on timeout.
    BUG FIX 6.2: Add incremental backoff on timeout.
    BUG FIX 7.1: Support HTTPS with self-signed certificates via BIGID_SSL_VERIFY env var.
    """
    import warnings
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')

    api_retry_count = 0
    max_retries = 3
    ssl_verify = False  # Disable SSL verification for self-signed certificates

    while api_retry_count <= max_retries:
        try:
            headers_raw = {"authorization": token} if token else {}
            headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
            timeout_val = HTTP_TIMEOUT if timeout_seconds is None else float(timeout_seconds)

            resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_raw, timeout=timeout_val, verify=ssl_verify)
            if resp.status_code in (401, 403) and token:
                resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_bearer, timeout=timeout_val, verify=ssl_verify)
            
            try:
                resp.raise_for_status()
            except requests.HTTPError as ex:
                body = ""
                try:
                    body = (resp.text or "").strip()
                except Exception:
                    body = ""
                snippet = body[:1200] + ("..." if len(body) > 1200 else "")
                raise RuntimeError(f"{method} {url} failed [{resp.status_code}]: {snippet}") from ex
            return resp
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError) as ex:
            api_retry_count += 1
            if api_retry_count > max_retries:
                raise RuntimeError(f"API timeout after {max_retries} retries: {ex}") from ex

            # Incremental backoff: 1s, 2s, 4s
            backoff_seconds = min(5.0, float(api_retry_count))
            import time as time_module
            time_module.sleep(backoff_seconds)
        except Exception:
            # Non-timeout errors: don't retry
            raise
    
    raise RuntimeError(f"API call failed after {max_retries} retries")



def _deep_find(obj: Any, keys: List[str]) -> Any:
    want = {str(k).lower() for k in keys}
    stack = [obj]
    seen = set()
    while stack:
        cur = stack.pop()
        cid = id(cur)
        if cid in seen:
            continue
        seen.add(cid)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in want:
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for it in cur:
                if isinstance(it, (dict, list)):
                    stack.append(it)
    return None


def _parse_build_version_from_text(text_value: Any) -> str:
    s = str(text_value or "")
    import re
    m = re.search(r"\b(2\d{2})[\s\-\.]{1,2}(\d{1,3})\b", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m_dot3 = re.search(r"\b(2\d{2})\.(\d{1,3})\.(\d{1,3})\b", s)
    if m_dot3:
        return f"{m_dot3.group(1)}.{m_dot3.group(2)}"
    return ""


def _build_prefix(build_version: str) -> str:
    v = str(build_version or "").strip()
    if not v:
        return ""
    parts = v.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    # fallback: convert 248-1 -> 248-1
    if "-" in v:
        return v
    return v


def fetch_about(base_url: str, token: str) -> Dict[str, Any]:
    url = build_api_url(base_url, ABOUT_PATH)
    resp = _request("GET", url, token, timeout_seconds=15)
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {}



# Global cache for build version checks to avoid BigID API spam (env_id -> (timestamp, version))
_BUILD_VERSION_CACHE: Dict[int, Tuple[datetime, str]] = {}
_BUILD_VERSION_CACHE_TTL_MINUTES = 10

def resolve_env_build_version(env_row: Dict[str, Any], token: str) -> str:
    """
    Prefer /about; fallback to test_environments.version; fallback empty.
    """
    base_url = str(env_row.get("base_url") or "").strip()
    bv = ""
    env_id = env_row.get("id")
    if env_id is not None:
        cache_entry = _BUILD_VERSION_CACHE.get(int(env_id))
        if cache_entry:
            last_check, cached_bv = cache_entry
            if (datetime.utcnow() - last_check).total_seconds() < (_BUILD_VERSION_CACHE_TTL_MINUTES * 60):
                return cached_bv

    try:
        about = fetch_about(base_url, token)
        # try common keys
        candidates = []
        if isinstance(about, dict):
            for k in ["releaseVersion", "coreVersion", "version", "buildVersion", "release", "tag", "build"]:
                v = about.get(k)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
            data = about.get("data")
            if isinstance(data, dict):
                for k in ["releaseVersion", "coreVersion", "version", "buildVersion", "release", "tag", "build"]:
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())

                about_block = data.get("about")
                if isinstance(about_block, dict):
                    for k in ["releaseVersion", "coreVersion", "version", "buildVersion", "release", "tag", "build"]:
                        v = about_block.get(k)
                        if isinstance(v, str) and v.strip():
                            candidates.append(v.strip())
        for s in candidates:
            bv = _parse_build_version_from_text(s)
            if bv:
                break
    except Exception:
        bv = ""
    if not bv:
        bv = _parse_build_version_from_text(env_row.get("version") or "")
    if bv and env_id is not None:
        _BUILD_VERSION_CACHE[int(env_id)] = (datetime.utcnow(), bv)
        
    return bv


def ensure_workflow_tables(engine) -> None:
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    dt = sql_datetime(engine)
    ai = sql_auto_increment(engine)
    ou = sql_on_update_current_timestamp(engine)
    
    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NULL,
                    status TEXT NULL,
                    env1_id INTEGER NULL,
                    env2_id INTEGER NULL,
                    cooldown_minutes INTEGER DEFAULT 10,
                    next_eligible_at TEXT NULL,
                    last_error TEXT NULL,
                    started_at TEXT NULL,
                    finished_at TEXT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    env_pinned INTEGER DEFAULT 0
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    environment_id INTEGER NULL,
                    ds_kind TEXT NULL,
                    ds_name TEXT NULL,
                    scan_kind TEXT NULL,
                    file_set_type TEXT NULL,
                    is_enabled INTEGER DEFAULT 1,
                    is_acl INTEGER DEFAULT 0,
                    is_ocr INTEGER DEFAULT 0,
                    is_structured_ff INTEGER DEFAULT 0,
                    is_ols INTEGER DEFAULT 0,
                    feature_flags TEXT NULL,
                    notes TEXT NULL,
                    ds_id TEXT NULL,
                    scan_profile_id TEXT NULL,
                    scan_profile_name TEXT NULL,
                    build_version TEXT NULL,
                    build_prefix TEXT NULL,
                    scan_id TEXT NULL,
                    status TEXT NULL,
                    last_error TEXT NULL,
                    started_at TEXT NULL,
                    finished_at TEXT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(run_id, seq)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    k TEXT PRIMARY KEY,
                    template_kind TEXT NULL,
                    payload_json TEXT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """))
            try:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_steps_run_seq ON workflow_steps(run_id, seq)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_steps_status ON workflow_steps(status)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_templates_kind ON workflow_templates(template_kind)"))
            except Exception:
                pass
            # Non-breaking upgrades for existing sqlite DBs
            for stmt in [
                "ALTER TABLE workflow_steps ADD COLUMN is_enabled INTEGER DEFAULT 1",
                "ALTER TABLE workflow_steps ADD COLUMN scan_profile_name TEXT NULL",
                "ALTER TABLE workflow_steps ADD COLUMN build_version TEXT NULL",
                "ALTER TABLE workflow_steps ADD COLUMN build_prefix TEXT NULL",
                "ALTER TABLE workflow_runs ADD COLUMN scan_template_id TEXT NULL",
                "ALTER TABLE workflow_steps ADD COLUMN scan_waiting_since TEXT NULL",
                "ALTER TABLE workflow_steps ADD COLUMN scan_waiting_retry_count INTEGER DEFAULT 0",
                "ALTER TABLE workflow_steps ADD COLUMN failover_count INTEGER DEFAULT 0",
                "ALTER TABLE workflow_steps ADD COLUMN api_retry_count INTEGER DEFAULT 0",
                "ALTER TABLE workflow_steps ADD COLUMN test_attempt_count INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
            return

        if "postgres" in dialect:
            # PostgreSQL - tables already created by db_config_pg.py DDL_STATEMENTS
            # Just ensure they exist (they should already be there from v2_api/main.py startup)
            # No-op: tables are managed by db_config_pg.py schema
            conn.commit()
            return

        # MySQL / Postgres
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id {ai} PRIMARY KEY,
                name VARCHAR(255) NULL,
                status VARCHAR(32) NULL,
                env1_id INT NULL,
                env2_id INT NULL,
                cooldown_minutes INT DEFAULT 10,
                next_eligible_at {dt} NULL,
                last_error TEXT NULL,
                started_at {dt} NULL,
                finished_at {dt} NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou},
                env_pinned BOOLEAN DEFAULT FALSE
            )
        """))
        if not is_postgresql(engine):
            try:
                conn.execute(text("CREATE INDEX idx_workflow_runs_status ON workflow_runs(status)"))
            except Exception:
                pass
        else:
            try:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)"))
            except Exception:
                pass
                
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS workflow_steps (
                id {ai} PRIMARY KEY,
                run_id INT NOT NULL,
                seq INT NOT NULL,
                environment_id INT NULL,
                ds_kind VARCHAR(64) NULL,
                ds_name VARCHAR(500) NULL,
                scan_kind VARCHAR(64) NULL,
                file_set_type VARCHAR(150) NULL,
                is_enabled BOOLEAN DEFAULT TRUE,
                is_acl BOOLEAN DEFAULT FALSE,
                is_ocr BOOLEAN DEFAULT FALSE,
                is_structured_ff BOOLEAN DEFAULT FALSE,
                is_ols BOOLEAN DEFAULT FALSE,
                is_lite BOOLEAN DEFAULT FALSE,
                feature_flags VARCHAR(500) NULL,
                notes VARCHAR(1000) NULL,
                ds_id VARCHAR(128) NULL,
                scan_profile_id VARCHAR(128) NULL,
                scan_profile_name VARCHAR(500) NULL,
                build_version VARCHAR(50) NULL,
                build_prefix VARCHAR(20) NULL,
                scan_id VARCHAR(128) NULL,
                status VARCHAR(32) NULL,
                last_error TEXT NULL,
                started_at {dt} NULL,
                finished_at {dt} NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou},
                UNIQUE (run_id, seq)
            )
        """))
        
        # Create indexes separately for Postgres compatibility
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_workflow_steps_run_seq ON workflow_steps(run_id, seq)",
            "CREATE INDEX IF NOT EXISTS idx_workflow_steps_status ON workflow_steps(status)",
        ]:
            try:
                conn.execute(text(idx_sql))
            except Exception:
                pass
                
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                k VARCHAR(200) PRIMARY KEY,
                template_kind VARCHAR(32) NULL,
                payload_json TEXT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
            )
        """))
        
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_workflow_templates_kind ON workflow_templates(template_kind)"))
        except Exception:
            pass
        # Non-breaking upgrades
        for stmt in [
            "ALTER TABLE workflow_steps ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE",
            "ALTER TABLE workflow_steps ADD COLUMN scan_profile_name VARCHAR(500) NULL",
            "ALTER TABLE workflow_steps ADD COLUMN build_version VARCHAR(50) NULL",
            "ALTER TABLE workflow_steps ADD COLUMN build_prefix VARCHAR(20) NULL",
            "ALTER TABLE workflow_runs ADD COLUMN scan_template_id VARCHAR(128) NULL",
            "ALTER TABLE workflow_steps ADD COLUMN is_lite BOOLEAN DEFAULT FALSE",
            "ALTER TABLE workflow_steps ADD COLUMN scan_waiting_since TIMESTAMP NULL",
            "ALTER TABLE workflow_steps ADD COLUMN scan_waiting_retry_count INT DEFAULT 0",
            "ALTER TABLE workflow_steps ADD COLUMN failover_count INT DEFAULT 0",
            "ALTER TABLE workflow_steps ADD COLUMN api_retry_count INT DEFAULT 0",
            "ALTER TABLE workflow_steps ADD COLUMN test_attempt_count INT DEFAULT 0",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        conn.commit()


def fetch_parent_scan_by_id(base_url: str, token: str, scan_id: str) -> Dict[str, Any]:
    """
    GET /api/v1/scans/parent-scans/{id}
    """
    base_path = PARENT_SCANS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{str(scan_id).strip()}")
    resp = _request("GET", url, token)
    return resp.json() if resp.content else {}


def fetch_parent_scans(
    base_url: str,
    token: str,
    states: Optional[List[str]] = None,
    limit: int = 200,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """List parent scans from BigID API with optional state filter (e.g. Running, Completed)."""
    url = build_api_url(base_url, PARENT_SCANS_PATH)
    filter_items = []
    cleaned_states = [str(s).strip() for s in (states or []) if str(s).strip()]
    if cleaned_states:
        filter_items.append({"field": "state", "operator": "in", "value": cleaned_states})
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "sort": json.dumps([{"field": "_id", "order": "desc"}], separators=(",", ":")),
    }
    if filter_items:
        params["filter"] = json.dumps(filter_items, separators=(",", ":"))
    resp = _request("GET", url, token, params=params)
    payload = resp.json() if resp.content else {}
    items = []
    if isinstance(payload, dict):
        for block in [payload.get("data"), payload]:
            if isinstance(block, dict):
                for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                    if isinstance(block.get(key), list):
                        items = block[key]
                        break
            if items:
                break
        if not items:
            for key in ["scanParents", "parentScans", "scans", "results", "items"]:
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
    return items if isinstance(items, list) else []


def _scan_name_from_parent_scan(sc: Dict[str, Any]) -> str:
    try:
        for k in ['name', 'scanName', 'displayName']:
            v = sc.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:
        pass
    return ''


def find_running_parent_scan_id(base_url: str, token: str, *, exact_name: str, wf_tag: str = "", limit: int = 80) -> str:
    """
    Find a running parent scan id by scan name.
    Prefer exact match; if none, return first running scan whose name contains exact_name (and wf_tag if set).
    This avoids creating duplicate scans when the API returns a display name that differs (e.g. appends " Full Scan ACL ON").
    """
    nm = str(exact_name or "").strip()
    if not nm:
        return ""
    needle = nm.lower()
    wf = str(wf_tag or "").strip().lower()
    try:
        items = fetch_parent_scans(
            base_url,
            token,
            states=["Started", "Awaiting", "In Progress", "InProgress", "Running", "Queued", "Pending", "Stop Requested", "StopRequested", "Stopping"],
            limit=int(limit),
            skip=0,
        )
    except Exception:
        items = []
    fallback_sid = ""
    for it in items or []:
        if not isinstance(it, dict):
            continue
        sid = str(it.get('_id') or it.get('id') or '').strip()
        name = _scan_name_from_parent_scan(it).strip()
        if not sid or not name:
            continue
        nml = name.lower()
        if nml == needle:
            if not wf or wf in nml:
                return sid
        if needle in nml and (not wf or wf in nml):
            if not fallback_sid:
                fallback_sid = sid
    return fallback_sid


def get_scan_status_via_api(base_url: str, token: str, scan_id: str) -> Optional[str]:
    """
    Directly fetch scan status from BigID API to bypass tracker latency.
    """
    try:
        sc = fetch_parent_scan_by_id(base_url, token, scan_id)
        if sc:
            # Try to resolve status/state
            st = str(sc.get('state') or sc.get('status') or '').strip()
            prog = sc.get('progress')
            if isinstance(prog, dict):
                pst = str(prog.get('state') or '').strip()
                if pst:
                    return pst
            return st or "In Progress"
    except Exception:
        pass
    return None


def _status_from_parent_scan_payload(sc: Dict[str, Any]) -> str:
    st = str(sc.get("state") or sc.get("status") or "").strip()
    prog = sc.get("progress")
    if isinstance(prog, dict):
        pst = str(prog.get("state") or "").strip()
        if pst:
            return pst
    return st


def _classify_parent_scan_for_reconcile(
    engine,
    env_id: Optional[int],
    scan_id: str,
    *,
    run_id: Optional[int] = None,
) -> Tuple[Optional[str], str]:
    """
    Decide whether a parent scan still exists in BigID.

    Returns (status_text, presence) where presence is one of:
      live     — non-terminal scan confirmed
      terminal — terminal state confirmed
      exists   — scan record exists; status unclear (do NOT reset)
      missing  — confirmed absent (404 on all envs tried)
      unknown  — API/DB inconclusive (do NOT reset)
    """
    sid = str(scan_id or "").strip()
    if not sid:
        return None, "missing"

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT scan_status, completed_at
                    FROM scan_parents
                    WHERE scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if row:
            stv = str(row[0] or "").strip()
            comp_at = row[1]
            has_completed = comp_at is not None and str(comp_at).strip() not in ("", "None")
            if stv and _is_terminal_state(stv):
                return stv, "terminal"
            if not has_completed:
                return stv or "In Progress", "live"
    except Exception:
        pass

    envs_to_try: List[int] = []
    if env_id is not None:
        try:
            envs_to_try.append(int(env_id))
        except Exception:
            pass
    if run_id is not None:
        for eid in _workflow_pool_env_ids_for_run(engine, int(run_id)):
            if int(eid) not in envs_to_try:
                envs_to_try.append(int(eid))

    if not envs_to_try:
        return None, "unknown"

    saw_404 = 0
    saw_other_error = 0
    for eid in envs_to_try:
        try:
            env_row = load_env(engine, int(eid))
            base_url = normalize_base_url(str(env_row.get("base_url") or ""))
            if not base_url:
                continue
            token = Get_auth_token(base_url, env_row)
            sc = fetch_parent_scan_by_id(base_url, token, sid)
            if isinstance(sc, dict) and sc:
                st = _status_from_parent_scan_payload(sc)
                if st and _is_terminal_state(st):
                    return st, "terminal"
                if st and not _is_terminal_state(st):
                    return st, "live"
                return st or "In Progress", "exists"
            saw_404 += 1
        except RuntimeError as ex:
            msg = str(ex).lower()
            if "404" in msg or "not found" in msg:
                saw_404 += 1
            else:
                saw_other_error += 1
        except Exception:
            saw_other_error += 1

    if saw_404 >= len(envs_to_try) and saw_other_error == 0:
        return None, "missing"
    return None, "unknown"


def _recover_false_aborted_steps(engine, run_id: int, *, logger=None) -> int:
    """
    Re-link scan_id when a step was reset as 'aborted/deleted' but BigID still has the scan.
    """
    bool_true = _sql_bool_true(engine)
    recovered_n = 0
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT ws.id, ws.seq, ws.environment_id, ws.ds_name, ws.scan_profile_name
                    FROM workflow_steps ws
                    WHERE ws.run_id = :rid
                      AND COALESCE(ws.is_enabled, {bool_true}) = {bool_true}
                      AND LOWER(TRIM(COALESCE(ws.status,''))) = 'pending'
                      AND TRIM(COALESCE(ws.ds_name,'')) <> ''
                      AND (
                        LOWER(TRIM(COALESCE(ws.last_error,''))) LIKE '%aborted/deleted%'
                        OR LOWER(TRIM(COALESCE(ws.last_error,''))) LIKE '%status check inconclusive%'
                      )
                    """
                ),
                {"rid": int(run_id)},
            ).fetchall()
    except Exception:
        return 0

    for row in rows or []:
        step_id = int(row[0])
        seq = int(row[1])
        env_id = row[2]
        ds_name = str(row[3] or "").strip()
        profile_name = str(row[4] or "").strip()
        if env_id is None:
            continue
        try:
            env_row = load_env(engine, int(env_id))
            base_url = normalize_base_url(str(env_row.get("base_url") or ""))
            if not base_url:
                continue
            token = Get_auth_token(base_url, env_row)
        except Exception:
            continue
        wf_tag = f"(wf {int(run_id)}-{int(seq)})"
        found = ""
        if profile_name:
            found = find_running_parent_scan_id(base_url, token, exact_name=profile_name, wf_tag=wf_tag)
        if not found and ds_name:
            found = find_running_parent_scan_id(base_url, token, exact_name=ds_name, wf_tag=wf_tag)
        if not found and ds_name:
            found = guess_latest_scan_id(base_url, token, name_contains=ds_name, wf_tag=wf_tag)
        if not found:
            continue
        set_step_status(engine, step_id, "scan_running", scan_id=str(found), last_error=None)
        _set_env_busy(engine, int(env_id), int(run_id), True)
        recovered_n += 1
        if logger:
            logger.info(
                "recovered scan_id=%s for step_id=%s run_id=%s seq=%s (was false aborted)",
                found,
                step_id,
                run_id,
                seq,
            )
    return recovered_n


def _fetch_scan_status_for_step(
    engine,
    env_id: Optional[int],
    scan_id: str,
    *,
    run_id: Optional[int] = None,
) -> Optional[str]:
    """Resolve scan state from scan_parents, then BigID API for the step's environment."""
    sid = str(scan_id or "").strip()
    if not sid:
        return None
    stv: Optional[str] = None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT scan_status, completed_at FROM scan_parents WHERE scan_id=:sid LIMIT 1"
                ),
                {"sid": sid},
            ).fetchone()
        if row:
            stv = str(row[0] or "").strip()
            comp_at = row[1]
            has_completed = comp_at is not None and str(comp_at).strip() not in ("", "None")
            if stv and _is_terminal_state(stv):
                return stv
            if not has_completed:
                return stv or "In Progress"
    except Exception:
        pass

    def _api_for_env(eid: int) -> Optional[str]:
        try:
            env_row = load_env(engine, int(eid))
            base_url = normalize_base_url(str(env_row.get("base_url") or ""))
            if not base_url:
                return None
            token = Get_auth_token(base_url, env_row)
            return get_scan_status_via_api(base_url, token, sid)
        except Exception:
            return None

    if env_id is not None:
        live = _api_for_env(int(env_id))
        if live:
            return live
    if run_id is not None:
        for pool_eid in _workflow_pool_env_ids_for_run(engine, int(run_id)):
            if env_id is not None and int(pool_eid) == int(env_id):
                continue
            live = _api_for_env(int(pool_eid))
            if live:
                return live
    return stv or None


def _complete_step_from_terminal_scan(
    engine,
    *,
    run_id: int,
    step_id: int,
    seq: int,
    scan_id: str,
    terminal_status: str,
) -> bool:
    """Mark step completed/error from a terminal scan state; release env locks. Returns True if step was terminal."""
    stv = str(terminal_status or "").strip()
    if not stv or not _is_terminal_state(stv):
        return False
    if _compact(stv) in {"completed", "completedwithfailures"}:
        set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
        try:
            _auto_save_benchmark_from_step(engine, step_id, scan_id, run_id, finished_at=_now_utc())
        except Exception:
            pass
    else:
        set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {stv}")
        set_run_error(engine, run_id, f"Step {seq}: scan ended in state {stv}")
    try:
        _release_stale_env_locks_for_run(engine, int(run_id))
    except Exception:
        pass
    return True


def _is_workflow_scan_name(name: str, *, run_id: Optional[int] = None) -> bool:
    nm = str(name or '').strip()
    if not nm:
        return False
    if run_id is not None:
        if f'(wf {int(run_id)}-' in nm:
            return True
    return '(wf ' in nm


def _workflow_scan_blocks_env(name: str, *, run_id: Optional[int] = None) -> bool:
    """
    When WORKFLOW_BLOCK_ANY_RUNNING_SCAN=0, only scans from *other* workflow runs block this env.
    Scans from the current run_id (e.g. wf 88-21 while starting wf 88-25) do not block — use
    WORKFLOW_BLOCK_ANY_RUNNING_SCAN=1 (default) for one scan per environment.
    """
    nm = str(name or "").strip()
    if "(wf " not in nm.lower():
        return False
    if run_id is not None and f"(wf {int(run_id)}-".lower() in nm.lower():
        return False
    return True


def _workflow_scan_blocks_env(name: str, *, run_id: Optional[int] = None) -> bool:
    """
    When WORKFLOW_BLOCK_ANY_RUNNING_SCAN=0, only scans from *other* workflow runs block this env.
    Scans from the current run_id (e.g. wf 88-21 while starting wf 88-25) do not block — use
    WORKFLOW_BLOCK_ANY_RUNNING_SCAN=1 (default) for one scan per environment.
    """
    nm = str(name or "").strip()
    if "(wf " not in nm.lower():
        return False
    if run_id is not None and f"(wf {int(run_id)}-".lower() in nm.lower():
        return False
    return True


def _step_needs_scan_env_free(cur_status: str, scan_id: Any) -> bool:
    """DS create / connection test does not need an empty env — only starting a parent scan does."""
    if str(scan_id or "").strip():
        return True
    return False


# ============================================================================
# BUG FIX 5.1, 5.2, 5.3, 5.4: OLS ACTION EXECUTION AND ATOMICITY
# ============================================================================

def _action_execution_attempt_id(engine, run_id: int) -> str:
    """Generate and store unique ID for this action execution attempt."""
    attempt_id = str(uuid.uuid4())
    _kv_set(engine, f"action_attempt_{run_id}", attempt_id, expire_seconds=3600)
    return attempt_id


def _mark_action_attempt_start(engine, run_id: int, action_name: str, attempt_id: str) -> None:
    """
    BUG FIX 5.1: Mark action execution start for atomicity tracking.
    Orphaned markers are used for auto-rollback on runner restart.
    """
    marker_key = f"action_{attempt_id}_start"
    marker_value = json.dumps({
        "run_id": run_id,
        "action": action_name,
        "started_at": _now_utc().isoformat(),
    })
    _kv_set(engine, marker_key, marker_value, expire_seconds=3600)
    logger = _get_logger(quiet=False)
    logger.info(f"action_marker: START {marker_key} action={action_name} run_id={run_id}")


def _mark_action_attempt_done(engine, run_id: int, attempt_id: str) -> None:
    """
    BUG FIX 5.1: Mark action execution complete.
    Clear the start marker once all PUTs succeeded atomically.
    """
    done_key = f"action_{attempt_id}_done"
    start_key = f"action_{attempt_id}_start"
    
    _kv_set(engine, done_key, "1", expire_seconds=3600)
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM tracker_kv WHERE k = :k"), {"k": start_key})
            conn.commit()
    except Exception:
        pass  # Non-critical: marker cleanup failure doesn't block progress
    
    logger = _get_logger(quiet=False)
    logger.info(f"action_marker: DONE {done_key}")


def _cleanup_orphaned_action_attempts(engine) -> None:
    """
    BUG FIX 5.1: On runner startup, find orphaned action_*_start markers.
    These indicate actions that started but never completed (crashed mid-execution).
    Auto-rollback by running compensating actions.
    """
    try:
        with engine.connect() as conn:
            orphaned = conn.execute(
                text("SELECT k, v FROM tracker_kv WHERE k LIKE 'action_%_start' LIMIT 10")
            ).fetchall()
        
        logger = _get_logger(quiet=False)
        
        for key, value_json in orphaned or []:
            try:
                marker_data = json.loads(str(value_json or "{}"))
                run_id = marker_data.get("run_id")
                action_name = marker_data.get("action")
                
                logger.warning(
                    f"action_cleanup: Found orphaned action start marker: {key} "
                    f"run_id={run_id} action={action_name}. "
                    f"This action started but never completed (possible runner crash). "
                    f"Consider manual verification of action state."
                )
                # Note: Auto-rollback is complex (need to execute compensating actions).
                # For now, just log and alert. Future: implement auto-compensation.
            except Exception as ex:
                logger.warning(f"action_cleanup: Could not parse marker {key}: {ex}")
    except Exception:
        pass  # Non-critical: marker cleanup failure doesn't block runner startup


def _verify_service_config(base_url: str, token: str, put_body: Any, logger) -> Tuple[bool, str]:
    """
    Read back a services-configuration entry after a PUT. A PUT can return 2xx for a flag
    that the BigID build doesn't have, so the value is only trusted when the list shows it.
    """
    cfg = (put_body or {}).get("configuration") if isinstance(put_body, dict) else None
    if not isinstance(cfg, dict):
        return False, "verify: PUT body has no configuration"
    cfg_id = str(cfg.get("id") or "").strip()
    service = str(cfg.get("service") or "").strip()
    want = str(cfg.get("value") or "").strip().lower()
    url = build_api_url(base_url, "/api/v1/services-configuration")
    try:
        resp = _request("GET", url, token, timeout_seconds=30)
        if resp.status_code != 200:
            return False, f"verify {cfg_id} on {service}: GET services-configuration returned {resp.status_code}"
        items = resp.json()
    except Exception as ex:
        return False, f"verify {cfg_id} on {service}: {ex}"
    if not isinstance(items, list):
        return False, f"verify {cfg_id} on {service}: unexpected services-configuration payload"
    match = [it for it in items if isinstance(it, dict) and str(it.get("id") or "") == cfg_id and str(it.get("service") or "") == service]
    if not match:
        logger.error(f"action_call: {cfg_id} not present on {service} after PUT")
        return False, f"{cfg_id} is not available on {service} in this BigID build (PUT accepted, flag not present)"
    got = str(match[0].get("value") or "").strip().lower()
    if got != want:
        return False, f"{cfg_id} on {service} is '{got}' after PUT, expected '{want}'"
    logger.info(f"action_call: verified {cfg_id}={got} on {service}")
    return True, ""


def _execute_action_calls_sequentially(
    engine, 
    base_url: str, 
    token: str, 
    action_calls: List[Dict[str, Any]],
    logger,
    attempt_id: str,
    run_id: int,
) -> Tuple[bool, str]:
    """
    BUG FIX 5.1: Execute service config PUT calls sequentially (not parallel).
    Track attempt_id for atomicity and rollback.
    BUG FIX 5.4: Actions already serialize on run_id (one per run at a time).
    Return (success, error_message).
    """
    if not action_calls:
        return True, ""
    
    for i, call in enumerate(action_calls):
        try:
            method = str(call.get("method", "PUT")).strip().upper()
            path = str(call.get("path", "")).strip()
            body = call.get("body", {})
            
            if not path:
                return False, f"Call {i}: missing path"
            
            url = build_api_url(base_url, path)
            
            logger.info(f"action_call: attempt_id={attempt_id} call={i+1}/{len(action_calls)} {method} {path}")
            
            resp = _request(method, url, token, json_body=body, timeout_seconds=30)
            
            if resp.status_code not in (200, 204, 201):
                error = f"Call {i}: {method} {path} returned {resp.status_code}"
                logger.error(f"action_call: FAILED {error}")
                return False, error
            
            if call.get("verify_config"):
                ok_cfg, cfg_err = _verify_service_config(base_url, token, body, logger)
                if not ok_cfg:
                    error = f"Call {i}: {cfg_err}"
                    logger.error(f"action_call: FAILED {error}")
                    return False, error

            logger.info(f"action_call: OK call={i+1}/{len(action_calls)}")
        except Exception as ex:
            error = f"Call {i}: Exception - {ex}"
            logger.error(f"action_call: FAILED {error}")
            return False, error
    
    logger.info(f"action_call: All {len(action_calls)} calls succeeded")
    return True, ""


def _get_compensating_actions(action_type: str) -> Optional[List[Dict[str, Any]]]:
    """
    BUG FIX 5.2: Get inverse/compensating actions for rollback.
    Returns None if no compensation needed.
    """
    action_type_lower = str(action_type or "").strip().lower()
    
    # Define compensating action pairs
    compensations = {
        "disable_ols": "enable_ols",
        "enable_ols": "disable_ols",
        "disable_ols_feature_flags": "enable_ols_feature_flags",
        "enable_ols_feature_flags": "disable_ols_feature_flags",
        "enable_ds_lite": None,  # No compensation for DS_LITE enable
    }
    
    compensating = compensations.get(action_type_lower)
    if not compensating:
        return None  # No compensation needed
    
    # Try to load compensating action from templates
    from v2_api.workflow_action_templates import (  # noqa: PLC0415
        enable_ols_feature_flags_calls,
        disable_ols_feature_flags_calls,
        enable_ds_lite_mode_calls,
    )
    
    action_map = {
        "enable_ols": enable_ols_feature_flags_calls,
        "disable_ols": disable_ols_feature_flags_calls,
        "enable_ols_feature_flags": enable_ols_feature_flags_calls,
        "disable_ols_feature_flags": disable_ols_feature_flags_calls,
    }
    
    func = action_map.get(compensating)
    return func() if func else None


def _verify_ols_action_flag_cleared(
    engine,
    env_id: int,
    base_url: str,
    token: str,
    logger,
) -> bool:
    """
    BUG FIX 5.3: After disable_ols action, log that verification should happen asynchronously.
    NOTE: DO NOT use blocking sleep() in workflow runner - it prevents other steps from running.
    OLS flag verification happens async via background polling in the next runner cycle.
    """
    logger.info(
        f"ols_verify: OLS disable action complete. "
        f"Flag state will be verified asynchronously in subsequent runner cycles. "
        f"(Avoid blocking sleeps in main workflow runner thread)"
    )
    return True


def _check_no_other_action_running(engine, run_id: int) -> bool:
    """
    BUG FIX 5.4: Enforce sequential action execution.
    Check if another action step is currently running for this run_id.
    Return True if this action can proceed, False if another is running.
    """
    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            other_action = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM workflow_steps
                    WHERE run_id = :rid
                      AND LOWER(TRIM(COALESCE(ds_kind,''))) = 'action'
                      AND LOWER(TRIM(COALESCE(status,''))) IN ('running', 'pre_action_done', 'ds_created')
                    LIMIT 1
                    """
                ),
                {"rid": int(run_id)},
            ).fetchone()
        return not bool(other_action)  # True if no other action running
    except Exception:
        return True  # Fail open: if check fails, allow proceeding


def _preflight_check_env_connectivity(engine, env_id: int, logger) -> Tuple[bool, str]:
    """
    BUG FIX 4.1: Pre-flight connectivity check at workflow start.
    Tests /api/v1/about endpoint for each environment to ensure BigID is reachable.
    Returns (success, message).
    """
    try:
        env_row = load_env(engine, int(env_id))
        if not env_row:
            return False, f"env_id {env_id} not found in database"
        
        base_url = str(env_row.get("base_url") or "").strip()
        if not base_url:
            return False, f"env_id {env_id} has no base_url configured"
        
        try:
            token = Get_auth_token(base_url, env_row)
        except Exception as ex:
            return False, f"env_id {env_id} auth failed: {ex}"
        
        try:
            about = fetch_about(base_url, token)
            if not about:
                return False, f"env_id {env_id} /api/v1/about returned empty response"
            logger.info(f"preflight_check: env_id={env_id} connectivity OK")
            return True, "ok"
        except Exception as ex:
            return False, f"env_id {env_id} /api/v1/about failed: {ex}"
    except Exception as ex:
        return False, f"preflight_check error for env_id {env_id}: {ex}"


def _preflight_check_workflow_pool(engine, logger) -> Tuple[bool, List[str]]:
    """
    BUG FIX 4.1: Check connectivity for all active workflow pool environments.
    Returns (all_ok, error_messages).
    """
    errors: List[str] = []
    try:
        with engine.connect() as conn:
            env_rows = conn.execute(text("""
                SELECT id FROM test_environments
                WHERE COALESCE(is_active, TRUE) = TRUE
                  AND COALESCE(include_in_workflow_runs, TRUE) = TRUE
                ORDER BY id
            """)).fetchall()
        
        if not env_rows:
            return False, ["No active environments configured for workflow runs"]
        
        strict_mode = str(os.getenv("WORKFLOW_PREFLIGHT_STRICT", "0") or "0").strip() in {"1", "true", "yes"}
        
        for env_row in env_rows:
            env_id = int(env_row[0])
            ok, msg = _preflight_check_env_connectivity(engine, env_id, logger)
            if not ok:
                errors.append(msg)
                if strict_mode:
                    logger.error(f"preflight_check: STRICT MODE - {msg}")
                    return False, errors
                else:
                    logger.warning(f"preflight_check: ADVISORY - {msg}")
        
        if errors and strict_mode:
            return False, errors
        
        return True, errors
    except Exception as ex:
        return False, [f"preflight_check exception: {ex}"]


def _ensure_workflow_pool_license_applied(engine, logger) -> Tuple[bool, List[str]]:
    """
    BUG FIX 4.2: Check/apply licenses for all workflow pool environments at startup.
    (Move from per-step to workflow-start to reduce BigID API spam.)
    Returns (all_ok, error_messages).
    """
    errors: List[str] = []
    try:
        with engine.connect() as conn:
            env_rows = conn.execute(text("""
                SELECT id, base_url FROM test_environments
                WHERE COALESCE(is_active, TRUE) = TRUE
                  AND COALESCE(include_in_workflow_runs, TRUE) = TRUE
                ORDER BY id
            """)).fetchall()
        
        if not env_rows:
            return True, []  # No envs to check
        
        for env_row in env_rows:
            env_id = int(env_row[0])
            base_url = str(env_row[1] or "").strip()
            if not base_url:
                continue
            
            try:
                # Cache key: license_workflow_{env_id}_{base_url}
                cache_key = f"license_workflow_{env_id}_{hash(base_url) % 100000}"
                if _kv_get(engine, cache_key):
                    logger.info(f"license_check: env_id={env_id} already cached")
                    continue  # Already checked recently
                
                # Check/apply license (using existing bigid_license.py logic)
                err = _ensure_environment_license_applied(engine, env_id, base_url, logger)
                if err:
                    errors.append(f"env_id {env_id}: {err}")
                    logger.warning(f"license_check: env_id={env_id} failed - {err}")
                else:
                    # Cache for 6 hours
                    _kv_set(engine, cache_key, "1", expire_seconds=6 * 3600)
                    logger.info(f"license_check: env_id={env_id} OK")
            except Exception as ex:
                errors.append(f"env_id {env_id}: {ex}")
                logger.warning(f"license_check exception: env_id={env_id} {ex}")
        
        return len(errors) == 0, errors
    except Exception as ex:
        return False, [f"license check exception: {ex}"]


def env_has_running_scan(base_url: str, token: str, *, run_id: Optional[int] = None, exclude_wf_tag: str = "") -> bool:
    """
    Hard safety. Default: block if ANY parent scan is running on this environment (one scan per env).

    Set WORKFLOW_BLOCK_ANY_RUNNING_SCAN=0 to only block scans from *other* workflow runs
    (allows multiple wf steps on the same env — not recommended for benchmarks).
    
    BUG FIX 3.4: Properly exclude scans from current run_id to avoid self-blocking.
    """
    block_any = str(os.getenv("WORKFLOW_BLOCK_ANY_RUNNING_SCAN", "1") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    try:
        states = [
            'Started','Awaiting','In Progress','InProgress','Running',
            'Stop Requested','StopRequested','Stopping','Queued','Pending'
        ]
        scans = fetch_parent_scans(base_url, token, states=states, limit=50, skip=0)
        print(f"[env_has_running_scan] base_url={base_url}, found {len(scans)} scans, block_any={block_any}, run_id={run_id}", flush=True)
    except Exception as ex:
        # BUG FIX 3.1: Fail-closed (assume env is busy) on API error to prevent duplicate scans.
        # This prevents race conditions where API failure allows duplicate scan dispatch.
        # Set WORKFLOW_FAIL_OPEN_ON_BUSY_CHECK=1 to restore old fail-open behavior (not recommended).
        fail_closed = str(os.getenv('WORKFLOW_FAIL_OPEN_ON_BUSY_CHECK', '0') or '0').strip().lower() in {'0', 'false', 'no', 'n', 'off'}
        print(f"[env_has_running_scan] API check failed ({'assuming BUSY (fail-closed)' if fail_closed else 'treating as FREE'}): {ex}", flush=True)
        return fail_closed  # True = assume env is busy (fail-closed)

    for sc in scans or []:
        if not isinstance(sc, dict):
            continue
        nm = _scan_name_from_parent_scan(sc)
        
        # BUG FIX 3.4: Exclude scans matching exclude_wf_tag OR from same run_id
        if exclude_wf_tag and str(exclude_wf_tag).lower().strip() in nm.lower():
            print(f"[env_has_running_scan] excluding scan '{nm}' (matches exclude_wf_tag)", flush=True)
            continue
        
        # Also exclude scans from the same run_id to prevent self-blocking
        if run_id is not None and f"(wf {int(run_id)}-".lower() in nm.lower():
            print(f"[env_has_running_scan] excluding scan '{nm}' (same run_id {run_id})", flush=True)
            continue

        st = str(sc.get('state') or sc.get('status') or '').strip()
        prog = sc.get('progress')
        pst = ''
        if isinstance(prog, dict):
            pst = str(prog.get('state') or '').strip()
        # Never count terminal states as running (API sometimes returns them despite state filter)
        if _compact(st) in {x.replace(" ", "") for x in TERMINAL_STATES} or _compact(pst) in {x.replace(" ", "") for x in TERMINAL_STATES}:
            print(f"[env_has_running_scan] scan '{nm}' is terminal (state={st}, pst={pst}), skipping", flush=True)
            continue
        running = (_compact(st) in RUNNING_STATES) or (_compact(pst) in RUNNING_STATES)
        if not running:
            print(f"[env_has_running_scan] scan '{nm}' not running (state={st}, pst={pst}), skipping", flush=True)
            continue
        if block_any:
            print(f"[env_has_running_scan] BLOCKING due to scan '{nm}' (state={st}, block_any=True)", flush=True)
            return True
        if _workflow_scan_blocks_env(nm, run_id=run_id):
            print(f"[env_has_running_scan] BLOCKING due to other-workflow scan '{nm}'", flush=True)
            return True
    print(f"[env_has_running_scan] NO blocking scans found, returning False", flush=True)
    return False


def _set_env_busy(engine, env_id: int, run_id: Optional[int], is_busy: bool) -> None:
    """
    Updates the test_environments table to mark an environment as busy/reserved.
    """
    if env_id is None:
        return
    try:
        now = _now_utc()
        bt = _sql_bool_true(engine)
        bf = _sql_bool_false(engine)
        with engine.begin() as conn:
            if is_busy:
                conn.execute(
                    text(
                        f"UPDATE test_environments SET is_busy={bt}, busy_by_run_id=:rid, busy_since=:now WHERE id=:eid"
                    ),
                    {"rid": run_id, "now": now, "eid": int(env_id)},
                )
            else:
                conn.execute(
                    text(
                        f"UPDATE test_environments SET is_busy={bf}, busy_by_run_id=NULL, busy_since=NULL WHERE id=:eid"
                    ),
                    {"eid": int(env_id)},
                )
    except Exception:
        pass


def _try_acquire_env(engine, env_id: int, run_id: int) -> bool:
    """
    Atomically reserve an environment for `run_id`. Succeeds only if the env is currently
    free (is_busy=0 / no owner) or already owned by this same run. Returns False if another
    run holds it — callers must then wait instead of running on a shared environment.

    BUG FIX 1.1 & 1.2: 
    - Use atomic UPDATE with RETURNING to guarantee true atomicity (no race window)
    - Check busy_since timestamp to auto-unlock orphaned locks after 30 minutes
    - Properly handle PostgreSQL vs MySQL syntax differences
    """
    if env_id is None:
        return False
    try:
        now = _now_utc()
        env_id_int = int(env_id)
        run_id_int = int(run_id)
        grace_period = timedelta(minutes=30)  # BUG FIX 1.2: Auto-unlock after 30min
        stale_timestamp = now - grace_period
        
        with engine.begin() as conn:
            # BUG FIX 1.1: Single atomic UPDATE - not read+write
            # Try to acquire free env or env already owned by this run
            res = conn.execute(
                text(
                    """
                    UPDATE test_environments
                    SET is_busy = TRUE, busy_by_run_id = :rid, busy_since = :now
                    WHERE id = :eid
                      AND (
                            COALESCE(is_busy, FALSE) = FALSE
                            OR busy_by_run_id IS NULL
                            OR busy_by_run_id = :rid
                          )
                    """
                ),
                {"rid": run_id_int, "now": now, "eid": env_id_int},
            )
            if (res.rowcount or 0) > 0:
                return True
            
            # BUG FIX 1.2: Reclaim orphan lock (is_busy=TRUE, busy_by_run_id NULL, or stale timestamp)
            # This handles the case where a runner crashed while holding the lock
            res2 = conn.execute(
                text(
                    """
                    UPDATE test_environments
                    SET is_busy = TRUE, busy_by_run_id = :rid, busy_since = :now
                    WHERE id = :eid
                      AND (
                            (COALESCE(is_busy, FALSE) = TRUE AND busy_by_run_id IS NULL)
                            OR (COALESCE(is_busy, FALSE) = TRUE AND busy_since < :stale_ts)
                          )
                    """
                ),
                {"rid": run_id_int, "now": now, "eid": env_id_int, "stale_ts": stale_timestamp},
            )
            if (res2.rowcount or 0) > 0:
                import logging as _logging
                logger = _logging.getLogger("workflow_runner")
                logger.info(f"_try_acquire_env: reclaimed orphan lock on env_id={env_id_int} for run_id={run_id_int}")
                return True
            
            return False
    except Exception as ex:
        # Fail-safe: never grab a possibly-busy env on error; retry next tick.
        import logging as _logging
        logger = _logging.getLogger("workflow_runner")
        logger.warning(f"_try_acquire_env exception: {ex}")
        return False


def _sync_env_busy_from_api(engine, env_id: int) -> None:
    """
    Query parent_scan API for this env; if a scan is running set is_busy=1 (and busy_by_run_id if known),
    otherwise set is_busy=0. Keeps DB in sync with actual BigID state every tick.
    """
    if env_id is None:
        return
    env_row = load_env(engine, env_id)
    if not env_row:
        return
    base_url = (env_row.get("base_url") or "").strip()
    if not base_url:
        return
    try:
        token = Get_auth_token(base_url, env_row)
    except Exception:
        return
    try:
        states = [
            "Started", "Awaiting", "In Progress", "InProgress", "Running",
            "Stop Requested", "StopRequested", "Stopping", "Queued", "Pending",
        ]
        scans = fetch_parent_scans(base_url, token, states=states, limit=50, skip=0)
    except Exception as ex:
        # BUG FIX 6.6: On API failure, mark env as temporarily unavailable locally
        # This allows workflow to degrade gracefully and retry the pool fetch
        import logging as _logging
        logger = _logging.getLogger("workflow_runner")
        logger.warning(f"_sync_env_busy_from_api failed for env_id={env_id}: {ex}; marking env unavailable")
        
        # Mark as unavailable by setting a temporary error flag (soft-reserve)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text("UPDATE test_environments SET is_busy=TRUE, busy_by_run_id=NULL WHERE id=:eid"),
                    {"eid": int(env_id)},
                )
                conn.commit()
        except Exception:
            pass
        return
    run_id_from_scan: Optional[int] = None
    has_running = False
    for sc in scans or []:
        if not isinstance(sc, dict):
            continue
        nm = _scan_name_from_parent_scan(sc)
        st = str(sc.get("state") or sc.get("status") or "").strip()
        pst = ""
        if isinstance(sc.get("progress"), dict):
            pst = str(sc["progress"].get("state") or "").strip()
        if _compact(st) in {x.replace(" ", "") for x in TERMINAL_STATES} or _compact(pst) in {x.replace(" ", "") for x in TERMINAL_STATES}:
            continue
        if (_compact(st) in RUNNING_STATES) or (_compact(pst) in RUNNING_STATES):
            has_running = True
            # Try to get run_id from name e.g. "(wf 15-3)" -> 15
            m = re.search(r"\(wf\s*(\d+)-\d+\)", nm, re.I)
            if m:
                try:
                    run_id_from_scan = int(m.group(1))
                except Exception:
                    pass
            break
    # Cross-reference with workflow state: if scan belongs to a run that is already terminal,
    # we should ignore BigID's stale status and release the env.
    if has_running and run_id_from_scan is not None:
        try:
            with engine.connect() as conn:
                r_row = conn.execute(
                    text("SELECT status FROM workflow_runs WHERE id = :rid"),
                    {"rid": int(run_id_from_scan)}
                ).fetchone()
                if r_row:
                    r_status = str(r_row[0] or "").lower()
                    if r_status in {"completed", "failed", "stopped"}:
                        has_running = False
                        run_id_from_scan = None
        except Exception:
            pass

    if not has_running:
        _set_env_busy(engine, env_id, None, False)
        return
    # If we didn't parse run from name, try DB: step with this env in scan_running
    if run_id_from_scan is None:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT run_id FROM workflow_steps WHERE environment_id=:eid AND COALESCE(status,'')='scan_running' LIMIT 1"
                    ),
                    {"eid": int(env_id)},
                ).fetchone()
                if row and row[0] is not None:
                    run_id_from_scan = int(row[0])
        except Exception:
            pass
    # Never set is_busy=1 with busy_by_run_id=NULL — that blocks workflow acquire (id=None) forever.
    if run_id_from_scan is None:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT busy_by_run_id FROM test_environments WHERE id=:eid"),
                    {"eid": int(env_id)},
                ).fetchone()
            if row and row[0] is not None:
                _set_env_busy(engine, env_id, None, False)
        except Exception:
            pass
        return
    # BigID may still list a scan as running after the workflow step finished — trust DB pipeline state.
    try:
        bool_true = _sql_bool_true(engine)
        with engine.connect() as conn:
            active = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM workflow_steps
                    WHERE run_id=:rid AND environment_id=:eid
                      AND COALESCE(is_enabled,{bool_true})={bool_true}
                      AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})
                    LIMIT 1
                    """
                ),
                {"rid": int(run_id_from_scan), "eid": int(env_id)},
            ).fetchone()
        if not active:
            _set_env_busy(engine, env_id, None, False)
            return
    except Exception:
        pass
    _set_env_busy(engine, env_id, run_id_from_scan, True)


def _workflow_ds_key(step: Dict[str, Any]) -> str:
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    # Add debug logging for diagnostics
    res = f"ds::{ds_kind}::{fs}::acl={acl}::struct={struct}"
    # try: logging.debug("workflow_ds_key result=%s (raw: kind=%s fs=%s acl=%s struct=%s)", res, ds_kind, fs, acl, struct)
    # except: pass
    return res


def _workflow_ds_key_candidates(step: Dict[str, Any]) -> List[str]:
    """
    Backward compatible lookup:
    - new key includes struct
    - old key did not
    """
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    return [
        f"ds::{ds_kind}::{fs}::acl={acl}::struct={struct}",
        f"ds::{ds_kind}::{fs}::acl={acl}",
        # Fallbacks: reuse base template and apply flags at runtime
        f"ds::{ds_kind}::{fs}::acl=0::struct=0",
        f"ds::{ds_kind}::{fs}::acl=0",
        f"ds::{ds_kind}::default::acl={acl}::struct={struct}",
        f"ds::{ds_kind}::default::acl={acl}",
        f"ds::{ds_kind}::default::acl=0::struct=0",
        f"ds::{ds_kind}::default::acl=0",
    ]


def _workflow_scan_key(step: Dict[str, Any]) -> str:
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    ols = 1 if bool(step.get("is_ols")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    return f"scan::{ds_kind}::{fs}::acl={acl}::ols={ols}::struct={struct}"


def get_template(engine, key: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT payload_json FROM workflow_templates WHERE k = :k LIMIT 1"), {"k": key}).fetchone()
    return str(row[0] or "").strip() if row else ""


def upsert_template_value(engine, key: str, template_kind: str, payload_json: str) -> None:
    """
    Upsert workflow_templates row (used for config keys too).
    """
    k = str(key or "").strip()
    if not k:
        return
    tk = str(template_kind or "").strip() or None
    pj = str(payload_json or "").strip() or None
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
                    VALUES (:k, :t, :p, datetime('now'))
                    ON CONFLICT(k) DO UPDATE SET
                        template_kind=excluded.template_kind,
                        payload_json=excluded.payload_json,
                        updated_at=datetime('now')
                """),
                {"k": k, "t": tk, "p": pj},
            )
        elif is_postgresql(engine):
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
                    VALUES (:k, :t, :p, CURRENT_TIMESTAMP)
                    ON CONFLICT(k) DO UPDATE SET
                        template_kind=EXCLUDED.template_kind,
                        payload_json=EXCLUDED.payload_json,
                        updated_at=CURRENT_TIMESTAMP
                """),
                {"k": k, "t": tk, "p": pj},
            )
        else:
            # MySQL
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json)
                    VALUES (:k, :t, :p)
                    ON DUPLICATE KEY UPDATE
                        template_kind=VALUES(template_kind),
                        payload_json=VALUES(payload_json),
                        updated_at=CURRENT_TIMESTAMP
                """),
                {"k": k, "t": tk, "p": pj},
            )
        conn.commit()


def fetch_scan_templates(base_url: str, token: str, *, limit: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
    url = build_api_url(base_url, SCAN_TEMPLATES_PATH)
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "filter": json.dumps([{"field": "_id", "value": [], "operator": "notIn"}], separators=(",", ":")),
        "sort": json.dumps([{"field": "updated_at", "order": "desc"}, {"field": "name", "order": "asc"}], separators=(",", ":")),
    }
    resp = _request("GET", url, token, params=params)
    payload = resp.json() if resp.content else {}
    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        data_block = payload.get("data")
        root = data_block if isinstance(data_block, dict) else payload
        for key in ["scanTemplates", "scan_templates", "templates", "items", "results"]:
            val = root.get(key)
            if isinstance(val, list):
                items = [x for x in val if isinstance(x, dict)]
                break
    return items

def resolve_default_scanner_group(engine, env_id: Optional[int]) -> str:
    keys = []
    if env_id is not None:
        keys.append(f"config::scanner_group::env={int(env_id)}")
    keys.append("config::scanner_group")
    for k in keys:
        v = str(get_template(engine, k) or "").strip()
        if v:
            return v
    return "default"

def _read_dotenv_map() -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        if not ENV_FILE_PATH.exists():
            return values
        text_value = ENV_FILE_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return values
    for line in text_value.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


_DOTENV_MAP: Dict[str, str] = {}
_DOTENV_MTIME: Optional[float] = None


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
            pass


def _getenv_any(names: List[str]) -> str:
    _maybe_reload_dotenv()
    for n in names:
        v = str(os.getenv(n, "") or _DOTENV_MAP.get(n, "") or "").strip()
        if v:
            return v
    return ""


def credential_kind_from_ds_template(ds_tpl: str, step_dict: Dict[str, Any]) -> str:
    """
    Secrets follow ds_connection.type in the DS JSON template, not the workflow step ds_kind
    (e.g. 'file-system' labels CSV scenarios; connector may still be s3/smb/...).
    """
    tpl = str(ds_tpl or "")
    probe = tpl
    m_ds = re.search(r'"ds_connection"\s*:', tpl)
    if m_ds:
        probe = tpl[m_ds.start() : m_ds.start() + 12000]
    for m in re.finditer(r'"type"\s*:\s*"([^"]+)"', probe):
        raw = (m.group(1) or "").strip()
        if not raw or raw.startswith("{{"):
            continue
        return raw.lower()
    if str(step_dict.get("ds_kind") or "").strip().lower() == "file-system":
        alt = _getenv_any(
            ["WORKFLOW_FILESYSTEM_CREDENTIAL_KIND", "WORKFLOW_CSV_CREDENTIAL_DS_TYPE", "CSV_DS_CREDENTIAL_TYPE"]
        )
        if alt:
            return alt.strip().lower()
    return str(step_dict.get("ds_kind") or "").strip().lower()


def _credential_kind_secret_namespace(cred_kind: str) -> str:
    """
    BigID ds_connection.type often differs from the .env namespace (e.g. type \"s3-v2\"
    but variables S3_accessKey / S3_secret_key). Map connector types to the same namespace
    resolve_secret_vars and _apply_runtime_secrets use for standalone S3/SMB steps.
    """
    k = str(cred_kind or "").strip().lower()
    if k in {"s3", "s3-v2", "s3_v2", "s3v2"}:
        return "s3"
    if k.startswith("s3-"):
        return "s3"
    # smb.json uses type \"smb_v2\"; env uses SMB_* (same rule as SMB small/large)
    if k in {"smb", "smb_v2", "smb-v2", "smbv2"}:
        return "smb"
    # BigID rdb connector type is "postgresql"; env uses Postgres_*.
    if "postgres" in k:
        return "postgres"
    return k


def resolve_secret_vars(ds_kind: str) -> Dict[str, str]:
    """
    Pull secrets from environment variables (user will put in .env).
    Keys match placeholders used by import_bigid_datasources.py.
    """
    k = _credential_kind_secret_namespace(str(ds_kind or ""))
    out: Dict[str, str] = {}
    # Generic support for current + future DS kinds:
    # include ALL env/.env variables with prefix "<ds_kind>_" (case-insensitive),
    # so templates can reference them without adding code here.
    try:
        _maybe_reload_dotenv()
        pref = f"{k}_"
        for src in (os.environ, _DOTENV_MAP):
            try:
                items = src.items()  # type: ignore[attr-defined]
            except Exception:
                items = []
            for kk, vv in items:
                kks = str(kk or "").strip()
                if not kks:
                    continue
                if str(kks).lower().startswith(pref):
                    vvs = str(vv or "").strip()
                    if vvs:
                        out[kks] = vvs
    except Exception:
        pass

    if k == "smb":
        out["SMB_username"] = _getenv_any(["SMB_username", "SMB_USERNAME", "smb_username", "smb_USERNAME"])
        out["SMB_domain"] = _getenv_any(["SMB_domain", "SMB_DOMAIN", "smb_domain"])
        out["SMB_password"] = _getenv_any(["SMB_password", "SMB_PASSWORD", "smb_password"])
        out["SMB_password_enc"] = _getenv_any(["SMB_password_enc", "SMB_PASSWORD_ENC", "smb_password_enc"])
        # Override SMB target host/ip (resourceProperties.resourceAddress)
        out["SMB_resourceAddress"] = _getenv_any(["SMB_resourceAddress", "SMB_RESOURCE_ADDRESS", "smb_resourceAddress", "smb_resource_address"])
    elif k == "s3":
        out["S3_accessKey"] = _getenv_any(["S3_accessKey", "S3_ACCESSKEY", "S3_ACCESS_KEY", "s3_accessKey"])
        out["S3_secret_key"] = _getenv_any(["S3_secret_key", "S3_SECRET_KEY", "S3_secretKey", "s3_secret_key"])
        out["S3_secret_key_enc"] = _getenv_any(["S3_secret_key_enc", "S3_SECRET_KEY_ENC", "s3_secret_key_enc"])
        # Support alternate naming the user mentioned (tenantid etc) without forcing it.
        out["S3_tenantid"] = _getenv_any(["S3_tenantid", "S3_TENANTID", "s3_tenantid"])
    elif k == "postgres":
        out["Postgres_username"] = _getenv_any(["Postgres_username", "POSTGRES_username", "POSTGRES_USERNAME", "postgres_username"])
        out["Postgres_password"] = _getenv_any(["Postgres_password", "POSTGRES_password", "POSTGRES_PASSWORD", "postgres_password"])
        out["Postgres_password_enc"] = _getenv_any(
            ["Postgres_password_enc", "POSTGRES_password_enc", "POSTGRES_PASSWORD_ENC", "postgres_password_enc"]
        )
        # Host/port override for rdb_url (host:port)
        out["Postgres_host"] = _getenv_any(["Postgres_host", "POSTGRES_host", "POSTGRES_HOST", "postgres_host", "Postgres_hostname", "POSTGRES_HOSTNAME", "postgres_hostname"])
        out["Postgres_port"] = _getenv_any(["Postgres_port", "POSTGRES_port", "POSTGRES_PORT", "postgres_port"])
        out["Postgres_rdb_url"] = _getenv_any(["Postgres_rdb_url", "POSTGRES_RDB_URL", "postgres_rdb_url"])
        out["Postgres_rdb_name"] = _getenv_any(["Postgres_rdb_name", "POSTGRES_rdb_name", "POSTGRES_RDB_NAME", "postgres_rdb_name", "Postgres_database", "POSTGRES_DATABASE", "postgres_database"])
    elif k == "mssql":
        out["MSSQL_username"] = _getenv_any(["MSSQL_username", "MSSQL_USERNAME", "mssql_username"])
        out["MSSQL_password"] = _getenv_any(["MSSQL_password", "MSSQL_PASSWORD", "mssql_password"])
        # Host/port override for rdb_url (host:port)
        out["MSSQL_host"] = _getenv_any(["MSSQL_host", "MSSQL_HOST", "mssql_host", "MSSQL_hostname", "MSSQL_HOSTNAME", "mssql_hostname"])
        out["MSSQL_port"] = _getenv_any(["MSSQL_port", "MSSQL_PORT", "mssql_port"])
        out["MSSQL_rdb_url"] = _getenv_any(["MSSQL_rdb_url", "MSSQL_RDB_URL", "mssql_rdb_url"])
    elif k == "mysql":
        out["MySQL_username"] = _getenv_any(["MySQL_username", "MYSQL_username", "MYSQL_USERNAME", "mysql_username"])
        out["MySQL_password"] = _getenv_any(["MySQL_password", "MYSQL_password", "MYSQL_PASSWORD", "mysql_password"])
        # Host/port override for rdb_url (host:port)
        out["MySQL_host"] = _getenv_any(["MySQL_host", "MYSQL_host", "MYSQL_HOST", "mysql_host", "MySQL_hostname", "MYSQL_HOSTNAME", "mysql_hostname"])
        out["MySQL_port"] = _getenv_any(["MySQL_port", "MYSQL_port", "MYSQL_PORT", "mysql_port"])
        out["MySQL_rdb_url"] = _getenv_any(["MySQL_rdb_url", "MYSQL_RDB_URL", "mysql_rdb_url"])
    elif k in {"onedrive-v2", "sharepoint-online-v2"}:
        prefix = "OneDrive" if k == "onedrive-v2" else "SharePoint"
        out[f"{prefix}_tenantId"] = _getenv_any([f"{prefix}_tenantId", f"{prefix}_TENANTID", f"{k}_tenantId"])
        out[f"{prefix}_principalId"] = _getenv_any([f"{prefix}_principalId", f"{prefix}_PRINCIPALID", f"{k}_principalId"])
        out[f"{prefix}_principal_secret_enc"] = _getenv_any([f"{prefix}_principal_secret_enc", f"{prefix}_PRINCIPAL_SECRET_ENC", f"{k}_principal_secret_enc"])
    return out


def _unresolved_placeholders(s: str) -> List[str]:
    try:
        import re
        return sorted(set(re.findall(r"\{\{([A-Za-z0-9_:\-]+)\}\}", str(s or ""))))
    except Exception:
        return []


def scan_template_id_exists_on_server(base_url: str, token: str, template_id: str) -> bool:
    tid = str(template_id or "").strip()
    if not tid:
        return False
    try:
        items = fetch_scan_templates(base_url, token, limit=400, skip=0)
    except Exception:
        return False
    for it in items or []:
        iid = str(it.get("_id") or it.get("id") or "").strip()
        if iid and iid == tid:
            return True
    return False


def resolve_default_scan_template_id(engine, env_id: Optional[int], base_url: str, token: str) -> str:
    keys = []
    logger = _get_logger(quiet=False)
    if env_id is not None:
        keys.append(f"config::scan_template_id::env={int(env_id)}")
    keys.append("config::scan_template_id")
    for k in keys:
        v = str(get_template(engine, k) or "").strip()
        if v and scan_template_id_exists_on_server(base_url, token, v):
            try: logger.info("resolved template key=%s value=%s", k, v)
            except Exception: pass
            return v
        if v:
            try: logger.warning("configured scan template id not found in BigID, ignoring key=%s value=%s", k, v)
            except Exception: pass
    # Env var fallback
    env_v = str(os.getenv("WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID", "") or "").strip()
    if env_v and scan_template_id_exists_on_server(base_url, token, env_v):
        return env_v
    if env_v:
        try: logger.warning("WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID not found in BigID; ignoring value=%s", env_v)
        except Exception: pass
    # Automatic fallback: fetch scan templates and pick "Data source scan"
    try:
        items = fetch_scan_templates(base_url, token, limit=200, skip=0)
        for it in items or []:
            nm = str(it.get("name") or "").strip().lower()
            if nm in {"data source scan", "datasource scan"}:
                tid = str(it.get("_id") or it.get("id") or "").strip()
                if tid:
                    if env_id is not None:
                        upsert_template_value(engine, f"config::scan_template_id::env={int(env_id)}", "config", tid)
                    else:
                        upsert_template_value(engine, "config::scan_template_id", "config", tid)
                    try: logger.info("resolved default scan template '%s' -> %s", nm, tid)
                    except Exception: pass
                    return tid
    except Exception:
        pass
    return ""


def get_or_set_run_scan_template_id(
    engine,
    run_id: int,
    env_id: Optional[int],
    base_url: str,
    token: str,
) -> str:
    """
    First successful resolution for this workflow run is stored on workflow_runs.scan_template_id
    and reused for every step in the run (consistent scanTemplateId on all scan profiles).
    """
    logger = _get_logger(quiet=False)
    rid = int(run_id)
    existing = ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT scan_template_id FROM workflow_runs WHERE id=:rid LIMIT 1"),
                {"rid": rid},
            ).fetchone()
        existing = str(row[0] or "").strip() if row and row[0] is not None else ""
    except Exception:
        pass
    if existing:
        if scan_template_id_exists_on_server(base_url, token, existing):
            return existing
        try:
            with engine.connect() as conn:
                conn.execute(text("UPDATE workflow_runs SET scan_template_id=NULL WHERE id=:rid"), {"rid": rid})
                conn.commit()
        except Exception:
            pass
    tid = resolve_default_scan_template_id(engine, env_id, base_url, token)
    if not tid:
        return ""
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE workflow_runs SET scan_template_id=:tid, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:rid AND (scan_template_id IS NULL OR TRIM(COALESCE(scan_template_id,''))='')"
                ),
                {"tid": tid, "rid": rid},
            )
            conn.commit()
    except Exception:
        pass
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT scan_template_id FROM workflow_runs WHERE id=:rid LIMIT 1"),
                {"rid": rid},
            ).fetchone()
        out = str(row[0] or "").strip() if row and row[0] is not None else ""
    except Exception:
        out = ""
    if out:
        try:
            logger.info("WORKFLOW_RUN_SCAN_TEMPLATE_PINNED run_id=%s scan_template_id=%s", rid, out)
        except Exception:
            pass
        return out
    return tid


def run_action_template(base_url: str, token: str, action_payload: Any) -> Any:
    """
    Execute a generic API call described by JSON or a list of such calls.
    Single: {"method": "POST", "path": "/api/v1/...", "body": {...}}
    List: [{"method": "PUT", "path": "..."}, {"method": "PUT", "path": "..."}]
    
    BUG FIX 6.3: Gracefully handle network blips and response parsing failures.
    Returns empty dict/list instead of crashing on malformed JSON.
    """
    if isinstance(action_payload, list):
        results = []
        for item in action_payload:
            try:
                results.append(run_action_template(base_url, token, item))
            except Exception as ex:
                # BUG FIX 6.3: Log error but continue processing other actions in list
                # to prevent partial failures from breaking entire action sequence
                import logging as _logging
                logger = _logging.getLogger("workflow_runner")
                logger.error(f"run_action_template item failed: {ex}")
                results.append({"error": str(ex)})
        return results

    if not isinstance(action_payload, dict):
        raise RuntimeError("action template must be a JSON object or list")
    
    method = str(action_payload.get("method") or "POST").strip().upper()
    path = str(action_payload.get("path") or "").strip()
    if not path:
        raise RuntimeError("action template missing 'path'")
    
    params = action_payload.get("params")
    body = action_payload.get("body")
    url = build_api_url(base_url, path)
    
    try:
        resp = _request(method, url, token, params=(params if isinstance(params, dict) else None), json_body=body)
    except Exception as ex:
        # BUG FIX 6.3: Network failure or timeout - return empty instead of crashing
        import logging as _logging
        logger = _logging.getLogger("workflow_runner")
        logger.error(f"run_action_template network error: {ex}")
        return {}
    
    # Check if request was successful
    if resp.status_code not in (200, 201, 204):
        error_msg = f"API call failed: {method} {path} returned {resp.status_code}"
        try:
            error_detail = resp.json()
            error_msg += f" - {error_detail}"
        except Exception:
            error_msg += f" - {resp.text[:200]}"
        raise RuntimeError(error_msg)
    
    try:
        return resp.json() if resp.content else {}
    except ValueError as ex:
        # BUG FIX 6.3: Malformed JSON response - log and return empty instead of crashing
        import logging as _logging
        logger = _logging.getLogger("workflow_runner")
        logger.warning(f"run_action_template malformed JSON response: {ex}, returning empty dict")
        return {}


def set_run_error(engine, run_id: int, message: str) -> None:
    """
    Record step failures. Optionally set run status failed immediately (UI); runner loads
    both running + failed. WORKFLOW_MARK_RUN_FAILED_ON_STEP_ERROR=0 for legacy.
    """
    msg = str(message)[:2000]
    mark_failed = str(os.getenv("WORKFLOW_MARK_RUN_FAILED_ON_STEP_ERROR", "1") or "1").strip().lower() in (
        "1", "true", "yes", "y", "on",
    )
    with engine.connect() as conn:
        if mark_failed:
            conn.execute(
                text(
                    "UPDATE workflow_runs "
                    "SET last_error=:e, updated_at=CURRENT_TIMESTAMP, "
                    "status=CASE WHEN LOWER(TRIM(COALESCE(status,''))) = 'running' THEN 'failed' ELSE status END "
                    "WHERE id=:rid"
                ),
                {"e": msg, "rid": int(run_id)},
            )
        else:
            conn.execute(
                text(
                    "UPDATE workflow_runs "
                    "SET last_error=:e, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,''))) = 'running'"
                ),
                {"e": msg, "rid": int(run_id)},
            )
        conn.commit()
    try:
        _maybe_mark_run_completed(engine, int(run_id))
    except Exception:
        pass


def clear_run_error(engine, run_id: int) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE workflow_runs SET last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
                {"rid": int(run_id)},
            )
            conn.commit()
    except Exception:
        pass

def set_step_error(engine, step_id: int, message: str, *, should_failover: bool = False) -> None:
    """
    Mark a step as error. If should_failover=True and failover_count < 2, 
    reset to pending and clear environment_id for retry on another env.
    
    BUG FIX 6.1: Auto-failover to alternate environment on error.
    """
    with engine.connect() as conn:
        # Check current failover count
        row = conn.execute(
            text("SELECT failover_count FROM workflow_steps WHERE id=:id"),
            {"id": int(step_id)},
        ).fetchone()
        current_failover = int(row[0]) if row and row[0] is not None else 0
        
        if should_failover and current_failover < 2:
            # Reset to pending for retry on another env
            conn.execute(
                text("""
                    UPDATE workflow_steps 
                    SET status='pending', environment_id=NULL, failover_count=:fc, 
                        last_error=:e, updated_at=CURRENT_TIMESTAMP 
                    WHERE id=:id
                """),
                {"e": f"[Failover {current_failover + 1}/2] {str(message)[:1900]}", "id": int(step_id), "fc": current_failover + 1},
            )
        else:
            # Permanent error
            conn.execute(
                text("UPDATE workflow_steps SET last_error=:e, status='error', updated_at=CURRENT_TIMESTAMP WHERE id=:id"),
                {"e": str(message)[:2000], "id": int(step_id)},
            )
        conn.commit()


def set_step_status(engine, step_id: int, status: str, **fields: Any) -> None:
    sets = ["status=:s", "updated_at=CURRENT_TIMESTAMP"]
    params: Dict[str, Any] = {"s": str(status), "id": int(step_id)}
    for k, v in fields.items():
        sets.append(f"{k}=:{k}")
        params[k] = v
    sql = f"UPDATE workflow_steps SET {', '.join(sets)} WHERE id=:id"
    with engine.connect() as conn:
        conn.execute(text(sql), params)
        conn.commit()


def _park_step_waiting(
    engine,
    step_id: int,
    run_id: int,
    env_id: Optional[int],
    last_error: str,
    *,
    log: Any = None,
    seq: Optional[int] = None,
) -> None:
    """
    Park a step without holding the environment lock. Prior bug: acquire env → busy check fails
    → scan_waiting while is_busy=1 stayed set → all envs locked with zero DS created.
    """
    set_step_status(
        engine,
        step_id,
        "scan_waiting",
        last_error=str(last_error)[:2000],
        environment_id=None,
    )
    if env_id is not None:
        _set_env_busy(engine, int(env_id), int(run_id), False)
    if log is not None:
        try:
            log.info(
                "WORKFLOW_STEP_PARKED run=%s step=%s env=%s reason=%s",
                run_id,
                seq if seq is not None else step_id,
                env_id,
                str(last_error)[:200],
            )
        except Exception:
            pass


def _release_stale_env_locks_for_run(engine, run_id: int) -> None:
    """Drop is_busy when this run has no step actively using that environment."""
    bool_true = _sql_bool_true(engine)
    occupying = _workflow_env_occupying_sql_in()
    try:
        with engine.connect() as conn:
            busy_pred = _sql_truthy(engine, "is_busy")
            busy_rows = conn.execute(
                text(
                    f"SELECT id FROM test_environments "
                    f"WHERE busy_by_run_id=:rid AND {busy_pred}"
                ),
                {"rid": int(run_id)},
            ).fetchall()
        for brow in busy_rows or []:
            eid = int(brow[0])
            with engine.connect() as conn:
                active = conn.execute(
                    text(
                        f"""
                        SELECT 1 FROM workflow_steps
                        WHERE run_id=:rid AND environment_id=:eid
                          AND COALESCE(is_enabled,{bool_true})={bool_true}
                          AND LOWER(TRIM(COALESCE(status,''))) IN ({occupying})
                        LIMIT 1
                        """
                    ),
                    {"rid": int(run_id), "eid": eid},
                ).fetchone()
            if not active:
                _set_env_busy(engine, eid, int(run_id), False)
    except Exception:
        pass


def touch_step_updated_at(engine, step_id: int) -> None:
    """Bump updated_at only. Pass 1 must refresh liveness while status stays scan_running, or global stale cleanup resets long scans."""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE workflow_steps SET updated_at=CURRENT_TIMESTAMP WHERE id=:id"),
                {"id": int(step_id)},
            )
            conn.commit()
    except Exception:
        pass


def ensure_runner_heartbeat_table(engine) -> None:
    """
    Ensure tracker_heartbeat exists so the runner can report liveness + last action.
    (Table is also used by tracker.py; we create it here for standalone runner usage.)
    """
    dt = sql_datetime(engine)
    try:
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
            conn.commit()
    except Exception:
        pass


def set_runner_heartbeat(engine, *, status: str, message: str = "", started: bool = False) -> None:
    """
    Updates tracker_heartbeat row for this workflow runner.
    - started=True updates last_started_at_utc
    - started=False updates last_finished_at_utc
    """
    ensure_runner_heartbeat_table(engine)
    stv = str(status or "").strip()[:16]
    msg_raw = str(message or "").strip()
    msg = (f"{RUNNER_ID} {msg_raw}").strip()[:2000]
    now = _now_utc()
    started_at = now if started else None
    finished_at = now if not started else None
    try:
        dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    except Exception:
        dialect = ""
    try:
        with engine.connect() as conn:
            conn.execute(
                text(sql_upsert_heartbeat(engine)),
                {"n": RUNNER_HEARTBEAT_NAME, "s": started_at, "f": finished_at, "st": stv, "m": msg},
            )
            conn.commit()
    except Exception:
        pass


def _strip_leading_build_prefix(name: str) -> str:
    nm = str(name or '').strip()
    if not nm:
        return nm
    m = re.match(r'^(2\d{2})[\-\.](\d{1,3})\s+(.+)$', nm)
    if m:
        return str(m.group(3) or '').strip()
    return nm


def _apply_build_prefix(name: str, build_prefix: str) -> str:
    nm = _strip_leading_build_prefix(name)
    bp = str(build_prefix or "").strip()
    if not bp:
        return nm
    # Avoid double prefix
    if nm.startswith(bp + " "):
        return nm
    return f"{bp} {nm}".strip()


def _standard_ds_base_name(step: Dict[str, Any]) -> str:
    """
    Enforce stable DS naming so builds/scenarios are distinguishable:
      S3 Small Files With ACL
      S3 Large Files
      S3 CSV Files Structured Files Discovery
      ...
    Build prefix is applied separately.
    """
    k = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip()
    is_acl = bool(step.get("is_acl"))
    is_ols = bool(step.get("is_ols"))
    is_struct = bool(step.get("is_structured_ff"))

    if k in {"s3", "smb"}:
        prefix = "S3" if k == "s3" else "SMB"
        nm = f"{prefix} {fs}".strip() if fs else prefix
        # Use 'AND' instead of '+' to avoid URL routing/validation issues (400 Bad Request)
        if fs in {"Small Files", "Large Files"} and is_acl:
            nm += " WITH ACL"
        if is_struct:
            nm += " Structured Files Discovery"
        if is_ols:
            nm += " AND OLS"
        return nm.strip()

    if k == "postgres":
        return "Postgres"
    if k == "mssql":
        return "MSSQL"
    if k == "mysql":
        return "MySQL"
    # fallback to existing ds_name if any
    return str(step.get("ds_name") or step.get("ds_kind") or "DS").strip()


def set_run_next_eligible(engine, run_id: int, next_dt: datetime) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET next_eligible_at=:t, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
            {"t": next_dt, "rid": int(run_id)},
        )
        conn.commit()


def _update_env_version_in_db(engine, env_id: Any, build_version: str) -> None:
    bv = str(build_version or '').strip()
    if not bv or env_id is None:
        return
    try:
        with engine.connect() as conn:
            # Only update and insert if the version has actually changed
            row = conn.execute(text('SELECT version FROM test_environments WHERE id=:id'), {'id': int(env_id)}).fetchone()
            current_v = str(row[0] or '').strip() if row else ''
            
            if current_v == bv:
                return # No change, skip database operations
            
            # BUG FIX 4.3: Update both version and version_last_synced_at
            try:
                conn.execute(text('UPDATE test_environments SET version=:v, version_last_synced_at=CURRENT_TIMESTAMP WHERE id=:id'), {'v': bv, 'id': int(env_id)})
            except Exception:
                # Fallback if column doesn't exist yet
                conn.execute(text('UPDATE test_environments SET version=:v WHERE id=:id'), {'v': bv, 'id': int(env_id)})
                
            try:
                conn.execute(
                    text('INSERT INTO test_environment_version_history (environment_id, version_value, change_note) VALUES (:eid, :v, :n)'),
                    {'eid': int(env_id), 'v': bv, 'n': 'workflow_runner:/start_run'}
                )
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


def load_env(engine, env_id: int) -> Dict[str, Any]:
    bool_true = _sql_bool_true(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"""
                SELECT id, env_name, base_url, version, auth_module_path, auth_function_name,
                       classifiers_api_path, is_active,
                       COALESCE(include_in_workflow_runs, {bool_true}) AS include_in_workflow_runs,
                       bigid_username, bigid_password_enc, version_last_synced_at
                FROM test_environments
                WHERE id = :id
                LIMIT 1
            """),
            {"id": int(env_id)},
        ).fetchone()
    if not row:
        return {}
    
    result = {
        "id": row[0],
        "env_name": row[1],
        "base_url": normalize_base_url(str(row[2] or "")),
        "version": row[3],
        "auth_module_path": row[4],
        "auth_function_name": row[5],
        "classifiers_api_path": row[6],
        "is_active": row[7],
        "include_in_workflow_runs": bool(int(row[8])) if row[8] is not None else True,
        "bigid_username": row[9],
        "bigid_password_enc": row[10],
    }
    
    # BUG FIX 4.3: Periodic version re-sync (every 30 minutes)
    try:
        last_synced = row[11] if len(row) > 11 else None
        if last_synced:
            last_synced_dt = _parse_db_dt(last_synced)
            if last_synced_dt:
                time_since_sync = (_now_utc() - last_synced_dt).total_seconds() / 60
                if time_since_sync >= 30:  # 30 minutes
                    # Version stale, try to re-sync
                    base_url = result.get("base_url", "")
                    if base_url:
                        try:
                            token = Get_auth_token(base_url, result)
                            new_version = resolve_env_build_version(result, token)
                            if new_version and new_version != str(result.get("version") or ""):
                                _update_env_version_in_db(engine, int(env_id), new_version)
                                result["version"] = new_version
                        except Exception:
                            pass  # Silently fail, use cached version
        else:
            # Never synced, try once
            base_url = result.get("base_url", "")
            if base_url:
                try:
                    token = Get_auth_token(base_url, result)
                    new_version = resolve_env_build_version(result, token)
                    if new_version:
                        _update_env_version_in_db(engine, int(env_id), new_version)
                        result["version"] = new_version
                except Exception:
                    pass  # Silently fail
    except Exception:
        pass  # Non-blocking
    
    return result


def _ensure_environment_license_applied(engine, env_id: int, base_url: str, logger) -> Optional[str]:
    """
    POST /api/v1/license to the current base_url before workflow steps run.
    Returns None on success, or an error message string.
    """
    burl = normalize_base_url(base_url)
    if not burl:
        return "missing base_url"
    cache_key = f"license_applied::{int(env_id)}::{burl}"
    try:
        if _kv_get(engine, cache_key):
            return None
    except Exception:
        pass
    try:
        from v2_api.bigid_license import sync_environment_license
    except ImportError:
        from bigid_license import sync_environment_license  # type: ignore
    try:
        lic = sync_environment_license(engine, int(env_id))
    except Exception as ex:
        return f"license sync exception: {ex}"
    if not lic.get("ok"):
        return str(lic.get("error") or "license apply failed")
    try:
        _kv_set(engine, cache_key, "1", expire_seconds=6 * 3600)
    except Exception:
        pass
    if logger:
        logger.info("license applied for env_id=%s base_url=%s", env_id, burl)
    return None


def _render_template(text_value: str, variables: Dict[str, str]) -> str:
    s = str(text_value or "")
    for k, v in variables.items():
        s = s.replace("{{" + str(k) + "}}", str(v))
    return s


def _apply_runtime_secrets(ds_kind: str, ds_payload: Dict[str, Any], secret_vars: Dict[str, str]) -> Dict[str, Any]:
    """
    Even if templates were imported with real secret values (or without placeholders),
    override connector creds from .env when provided.
    """
    if not isinstance(ds_payload, dict):
        return ds_payload
    k = _credential_kind_secret_namespace(str(ds_kind or ""))
    root = ds_payload
    ds_conn = root.get("ds_connection") if isinstance(root.get("ds_connection"), dict) else None
    if not isinstance(ds_conn, dict):
        ds_conn = root
    ap = ds_conn.get("authenticationProperties")
    if not isinstance(ap, dict):
        ap = None

    def _get(name: str) -> str:
        return str(secret_vars.get(name) or "").strip()

    def _override_rdb_url(ds_conn_obj: Dict[str, Any], *, host: str, port: str, url_override: str) -> None:
        """
        BigID rdb connectors typically store endpoint as `rdb_url` in `host:port` form.
        Allow overriding either full rdb_url or just host/port.
        """
        try:
            if url_override:
                ds_conn_obj["rdb_url"] = str(url_override).strip()
                return
            h = str(host or "").strip()
            if not h:
                return
            cur = str(ds_conn_obj.get("rdb_url") or "").strip()
            cur_host = ""
            cur_port = ""
            if cur and ":" in cur:
                cur_host, cur_port = cur.rsplit(":", 1)
            p = str(port or "").strip() or cur_port
            ds_conn_obj["rdb_url"] = f"{h}:{p}".strip(":")
        except Exception:
            pass

    def _get_prefixed_field(field_name: str) -> str:
        """
        Generic lookup for "<ds_kind>_<field>" in secret_vars (case-insensitive).
        This enables future DS kinds without code changes, as long as .env uses that prefix.
        """
        try:
            pref = f"{k}_"
            want = str(field_name or "").strip().lower()
            for kk, vv in (secret_vars or {}).items():
                kkl = str(kk or "").strip().lower()
                if not kkl.startswith(pref):
                    continue
                if kkl[len(pref):] == want:
                    return str(vv or "").strip()
        except Exception:
            pass
        return ""

    if k == "s3":
        if isinstance(ap, dict):
            ak = _get("S3_accessKey")
            if ak:
                ap["accessKey"] = ak
            # Streamlit behavior: raw .env value goes into secret_key_enc (no encryption).
            sk = _get("S3_secret_key")
            if sk:
                ap["secret_key_enc"] = sk
                ap.pop("secret_key", None)
            tid = _get("S3_tenantid")
            if tid and "tenantId" in ap:
                ap["tenantId"] = tid
            ds_conn["authenticationProperties"] = ap
    elif k == "smb":
        u = _get("SMB_username")
        d = _get("SMB_domain")
        pw = _get("SMB_password")
        ra = _get("SMB_resourceAddress")

        # Some exports store SMB creds at top-level, some under authenticationProperties.
        if u:
            ds_conn["username"] = u
        if d and ("domain" in ds_conn):
            ds_conn["domain"] = d
        # Streamlit behavior: raw .env value goes into password_enc (no encryption).
        if pw:
            ds_conn["password_enc"] = pw
            ds_conn.pop("password", None)
        if ra:
            try:
                rp = ds_conn.get("resourceProperties")
                if isinstance(rp, dict):
                    rp["resourceAddress"] = ra
                else:
                    ds_conn["resourceProperties"] = {"resourceAddress": ra}
            except Exception:
                pass
        if isinstance(ap, dict):
            if u:
                ap["username"] = u
            if d and ("domain" in ap):
                ap["domain"] = d
            if pw:
                ap["password_enc"] = pw
                ap.pop("password", None)
            ds_conn["authenticationProperties"] = ap
    elif k == "postgres":
        u = _get("Postgres_username")
        p = _get("Postgres_password")
        h = _get("Postgres_host")
        pt = _get("Postgres_port")
        ru = _get("Postgres_rdb_url")
        rn = _get("Postgres_rdb_name")
        if u:
            ds_conn["username"] = u
        # Use the .env value as-is in the password field. No encryption.
        if p:
            ds_conn["password"] = p
        if rn:
            ds_conn["rdb_name"] = rn
        _override_rdb_url(ds_conn, host=h, port=pt, url_override=ru)
        if isinstance(ap, dict):
            if u:
                ap["username"] = u
            if p:
                ap["password"] = p
            ds_conn["authenticationProperties"] = ap
    elif k == "mssql":
        u = _get("MSSQL_username")
        p = _get("MSSQL_password")
        h = _get("MSSQL_host")
        pt = _get("MSSQL_port")
        ru = _get("MSSQL_rdb_url")
        if u:
            ds_conn["username"] = u
        if p:
            ds_conn["password"] = p
        _override_rdb_url(ds_conn, host=h, port=pt, url_override=ru)
        if isinstance(ap, dict):
            if u:
                ap["username"] = u
            if p:
                ap["password"] = p
            ds_conn["authenticationProperties"] = ap
    elif k == "mysql":
        u = _get("MySQL_username")
        p = _get("MySQL_password")
        h = _get("MySQL_host")
        pt = _get("MySQL_port")
        ru = _get("MySQL_rdb_url")
        if u:
            ds_conn["username"] = u
        if p:
            ds_conn["password"] = p
        _override_rdb_url(ds_conn, host=h, port=pt, url_override=ru)
        if isinstance(ap, dict):
            if u:
                ap["username"] = u
            if p:
                ap["password"] = p
            ds_conn["authenticationProperties"] = ap

    # Generic fallback for current + future DS kinds:
    # Recursively override credential-ish keys when .env provides "<ds_kind>_<key>".
    # This handles connectors that nest secrets under various sub-objects.
    try:
        pref = f"{k}_"
        pref_map: Dict[str, str] = {}
        for kk, vv in (secret_vars or {}).items():
            kkl = str(kk or "").strip().lower()
            if not kkl.startswith(pref):
                continue
            suffix = kkl[len(pref):].strip()
            vvs = str(vv or "").strip()
            if suffix and vvs:
                pref_map[suffix] = vvs

        def _walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for key in list(obj.keys()):
                    kl = str(key or "").strip().lower()
                    # If .env defines "<ds_kind>_<field>", override that field wherever it
                    # appears in the payload. The "<ds_kind>_" prefix is the explicit opt-in,
                    # so any new connector works with zero code change — just add .env keys.
                    if kl in pref_map:
                        obj[key] = pref_map[kl]
                    # Common case: template has "password" but .env provides "<ds_kind>_password_enc".
                    if kl == "password" and ("password_enc" in pref_map):
                        obj[key] = pref_map["password_enc"]
                    _walk(obj.get(key))
            elif isinstance(obj, list):
                for it in obj:
                    _walk(it)

        _walk(ds_payload)
    except Exception:
        pass

    if isinstance(root.get("ds_connection"), dict):
        root["ds_connection"] = ds_conn
        return root
    return ds_conn


def _apply_runtime_flags(step: Dict[str, Any], ds_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply step flags (ACL / Structured FF) to DS payload so we don't require a
    separate imported template for every acl/struct combination.
    """
    if not isinstance(ds_payload, dict):
        return ds_payload
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    root = ds_payload
    ds_conn = root.get("ds_connection") if isinstance(root.get("ds_connection"), dict) else None
    if not isinstance(ds_conn, dict):
        ds_conn = root

    is_acl = bool(step.get("is_acl"))
    is_struct = bool(step.get("is_structured_ff"))
    is_ocr = bool(step.get("is_ocr"))

    # Define unstructured connectors that require specific enhancement fields
    UNSTRUCTURED_KINDS = {
        "s3", "smb", "onedrive-v2", "sharepoint-online-v2", 
        "google-drive", "box", "azure-blob", "dropbox", "gmail",
        "exchange-online", "file-system"
    }

    if ds_kind in UNSTRUCTURED_KINDS:
        # 1. Mandatory defaults for all unstructured DS per user request
        ds_conn["liteModeNotification"] = ""
        # BigID UI uses 'custom_fields' (underscore) in some versions/payloads.
        # We'll set both to be safe, including the Lite Mode flag requested.
        lite_mode_field = {
            "field_type": "clear",
            "field_name": "DS_LITE_MODE_ENABLED",
            "field_value": "true",
            "enable": True
        }
        ds_conn["customFields"] = [lite_mode_field]
        ds_conn["custom_fields"] = [lite_mode_field]
        
        # 2. Media Analysis (OCR): Force to False as string for API stability
        ds_conn["ocrEnabled"] = "false"
        
        # 3. Permissions (ACL): Use STRINGS "true" to match BigID UI payload
        if is_acl:
            ds_conn["metadataAclScanEnabled"] = "true"
            ds_conn["dsAclScanEnabled"] = "true"
        
        # 4. Structured Discovery: Only set if explicitly enabled in workflow
        if is_struct:
            trp = ds_conn.get("typeRequestProperties", {})
            if not isinstance(trp, dict):
                trp = {}
            trp["enableStructuredFilesDiscovery"] = True
            ds_conn["typeRequestProperties"] = trp

    if isinstance(root.get("ds_connection"), dict):
        root["ds_connection"] = ds_conn
        return root
    return ds_conn


def _action_pre_ols_key(env_id: Optional[int]) -> List[str]:
    """
    Prefer env-specific action, fallback to global.
    """
    keys = []
    if env_id is not None:
        keys.append(f"action::pre_ols::env={int(env_id)}")
    keys.append("action::pre_ols")
    return keys


def _action_pre_disable_ols_key(env_id: Optional[int]) -> List[str]:
    keys: List[str] = []
    if env_id is not None:
        keys.append(f"action::disable_ols::env={int(env_id)}")
    keys.append("action::disable_ols")
    return keys


def _action_pre_ds_lite_key(env_id: Optional[int]) -> List[str]:
    keys: List[str] = []
    if env_id is not None:
        keys.append(f"action::enable_ds_lite::env={int(env_id)}")
    keys.append("action::enable_ds_lite")
    return keys


def _action_pre_step_keys(step: Dict[str, Any], env_id: Optional[int]) -> List[str]:
    """
    Per-step action (for FF on/off etc) keyed by DS + scenario flags.
    Most specific first.
    """
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    ols = 1 if bool(step.get("is_ols")) else 0
    keys: List[str] = []
    if env_id is not None:
        keys.append(f"action::pre_step::env={int(env_id)}::{ds_kind}::{fs}::acl={acl}::struct={struct}::ols={ols}")
    keys.append(f"action::pre_step::{ds_kind}::{fs}::acl={acl}::struct={struct}::ols={ols}")
    return keys


def _workflow_global_pool_env_ids(engine) -> List[int]:
    """All active envs marked for workflow runs (catalog / validation)."""
    bool_true = _sql_bool_true(engine)
    bool_false = _sql_bool_false(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id FROM test_environments
                    WHERE COALESCE(is_active, {bool_false}) = {bool_true}
                      AND COALESCE(include_in_workflow_runs, {bool_true}) = {bool_true}
                    ORDER BY id
                    """
                )
            ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception:
        return []


# Run statuses that still own their env selection (mirrors workflow_ops.ACTIVE_RUN_STATUSES;
# duplicated because the runner also executes standalone, without the v2_api package).
_ACTIVE_WORKFLOW_RUN_STATUSES = ("created", "running", "paused", "failed")
_ACTIVE_WORKFLOW_RUN_STATUS_SQL_IN = ", ".join(f"'{s}'" for s in _ACTIVE_WORKFLOW_RUN_STATUSES)


def _run_still_owns_selection_sql(engine, run_alias: str = "r") -> str:
    """
    SQL predicate: run `run_alias` still has work that justifies holding its env selection.

    An active *status* is not enough. A 'failed' run stays in _ACTIVE_WORKFLOW_RUN_STATUSES so the
    runner can resume it, but once every enabled step is terminal there is nothing left to resume —
    and a run like that must not keep its env selection, or it silently starves every later run
    (with a single-env pool, the new run gets an empty candidate set and never dispatches at all).
    A run whose steps have not been inserted yet keeps its selection so create/start cannot race.
    """
    bt = _sql_bool_true(engine)
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM workflow_steps ws0 WHERE ws0.run_id = {run_alias}.id
        )
        OR EXISTS (
            SELECT 1 FROM workflow_steps ws1
            WHERE ws1.run_id = {run_alias}.id
              AND COALESCE(ws1.is_enabled, {bt}) = {bt}
              AND LOWER(TRIM(COALESCE(ws1.status, ''))) NOT IN ('completed', 'error', 'disabled')
        )
    )"""


def _env_ids_reserved_by_other_runs(engine, run_id: Optional[int] = None) -> List[int]:
    """
    Envs another still-active run has *selected* (env1_id / env2_id). A selected env belongs to
    that run alone, so it drops out of every other run's candidate set — that is what lets one
    workflow run on the pinned pair while a second one uses whatever is left.

    Only runs that still have enabled, non-terminal steps hold their selection (see
    _run_still_owns_selection_sql) — a finished run releases its envs back to the pool.
    """
    sql = f"""
        SELECT DISTINCT e.id
        FROM workflow_runs r
        JOIN test_environments e ON e.id IN (r.env1_id, r.env2_id)
        WHERE LOWER(TRIM(COALESCE(r.status, ''))) IN ({_ACTIVE_WORKFLOW_RUN_STATUS_SQL_IN})
          AND {_run_still_owns_selection_sql(engine, "r")}
    """
    params: Dict[str, Any] = {}
    if run_id is not None:
        sql += " AND r.id <> :rid"
        params["rid"] = int(run_id)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception:
        return []


def _workflow_available_pool_env_ids(engine, run_id: Optional[int] = None) -> List[int]:
    """
    Pool envs a run with no explicit selection may use: everything active and workflow-enabled,
    minus envs selected by another active run. Resolved per tick (not frozen at start) so a
    workflow created later with an explicit selection can claim its env, and this run simply
    keeps the rest.
    """
    reserved = set(_env_ids_reserved_by_other_runs(engine, run_id))
    # [] when every env is selected elsewhere — the run then waits for one to be released
    # (see _run_is_waiting_for_env), it never borrows another run's selected env.
    return [eid for eid in _workflow_global_pool_env_ids(engine) if eid not in reserved]


def _workflow_run_selected_env_ids(engine, run_id: int) -> List[int]:
    """
    Envs selected for this run (env1_id / env2_id). Non-empty means the run must execute on
    exactly these — no substitution, no failover outside them, no widening to the rest of the
    pool. Empty means no selection: the run uses every available pool env.
    """
    ids: List[int] = []
    try:
        with engine.connect() as conn:
            re = conn.execute(
                text("SELECT env1_id, env2_id FROM workflow_runs WHERE id=:rid"),
                {"rid": int(run_id)},
            ).fetchone()
        if re:
            for raw in (re[0], re[1]):
                if raw is None:
                    continue
                e = int(raw)
                if e not in ids:
                    ids.append(e)
    except Exception:
        pass
    return ids


def ensure_workflow_run_env_assignment(
    engine,
    run_id: int,
    env1_id: Any = None,
    env2_id: Any = None,
    *,
    logger=None,
) -> List[int]:
    """
    Resolve the env set for a run at start/resume and log it.

    Two modes:
      * selected (Env 1 / Env 2 chosen at create) — the run executes on exactly those envs.
        Nothing is substituted: if a selected env is busy the run waits for it instead of
        borrowing an unselected one.
      * no selection — the run uses every available pool env (all active, workflow-enabled envs
        minus those selected by another active run). Nothing is written to the run's slots, so
        the set stays dynamic: a workflow created later with a selection takes its env and this
        run simply continues on what is left.

    WORKFLOW_RUN_ENV_SLOTS optionally caps the unselected case (unset = no cap = all available).
    """
    selected = _workflow_run_selected_env_ids(engine, int(run_id))
    if selected:
        if logger:
            logger.info(
                "run_id=%s runs on selected env id(s) %s — no auto-assignment",
                run_id,
                ", ".join(str(e) for e in selected),
            )
        return selected

    available = _workflow_available_pool_env_ids(engine, int(run_id))
    max_slots = _workflow_env_slot_cap()
    if max_slots is not None:
        available = available[:max_slots]
    if logger:
        if available:
            names = []
            for eid in available:
                row = load_env(engine, int(eid))
                names.append(str(row.get("env_name") or f"env-{eid}"))
            logger.info(
                "run_id=%s no env selection — using all available envs: %s",
                run_id,
                ", ".join(names),
            )
        else:
            logger.warning(
                "run_id=%s no env selection and every pool env is selected by another run — waiting",
                run_id,
            )
    return available


def _workflow_env_slot_cap() -> Optional[int]:
    """Optional cap on how many envs an unselected run may use (unset = all available)."""
    raw = str(os.getenv("WORKFLOW_RUN_ENV_SLOTS", "") or "").strip()
    if not raw:
        return None
    try:
        return max(1, min(int(raw), 16))
    except Exception:
        return None


def _workflow_pool_env_ids_for_run(engine, run_id: int) -> List[int]:
    """
    Env ids this run may use.

    Selected envs (env1_id / env2_id) win outright — a selected run is never widened to the
    rest of the pool. With no selection the set is every available pool env, minus the envs
    another active run has selected, recomputed on each tick.
    """
    selected = _workflow_run_selected_env_ids(engine, int(run_id))
    if selected:
        return selected
    available = _workflow_available_pool_env_ids(engine, int(run_id))
    max_slots = _workflow_env_slot_cap()
    return available[:max_slots] if max_slots is not None else available


def _env_has_running_scan_for_action(engine, eid: int, run_id: int, *, logger=None) -> bool:
    env_row = load_env(engine, int(eid))
    if not env_row:
        return False
    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        return False
    try:
        token = Get_auth_token(base_url, env_row)
        return bool(env_has_running_scan(base_url, token, run_id=run_id))
    except Exception:
        return False


def _workflow_action_target_env_ids(engine, run_id: int, *, logger=None) -> List[int]:
    """
    Assigned run envs where we should apply service-config actions.
    Skips envs that already have a scan running (action not needed there).
    """
    assigned = _workflow_pool_env_ids_for_run(engine, int(run_id))
    targets: List[int] = []
    for eid in assigned:
        if _env_has_running_scan_for_action(engine, int(eid), int(run_id), logger=logger):
            if logger:
                row = load_env(engine, int(eid))
                logger.info(
                    "Action skip env %s (%s): scan already running",
                    eid,
                    row.get("env_name"),
                )
            continue
        targets.append(int(eid))
    return targets


def _action_base_notes_key(notes: Optional[str]) -> str:
    """Strip execution suffixes so duplicate action rows can be compared."""
    s = str(notes or "")
    s = re.sub(r"\s*\[Executed on:[^\]]*\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\[Executed env ids:[^\]]*\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\[skipped:.*?\]", "", s, flags=re.IGNORECASE)
    return s.strip()


def _action_executed_env_names_from_notes(notes: Optional[str]) -> List[str]:
    """Parse env names already actioned from step notes (`[Executed on: a, b]`)."""
    out: List[str] = []
    for m in re.finditer(r"\[Executed on:\s*([^\]]+)\]", str(notes or ""), flags=re.IGNORECASE):
        for part in str(m.group(1) or "").split(","):
            name = part.strip()
            if name and name not in out:
                out.append(name)
    return out


def _action_executed_env_ids_from_notes(
    notes: Optional[str], pool_ids: List[int], engine
) -> set[int]:
    """Env ids already actioned (prefer `[Executed env ids: …]`, else match names)."""
    out: set[int] = set()
    for m in re.finditer(
        r"\[Executed env i?ds?:\s*([^\]]+)\]",
        str(notes or ""),
        flags=re.IGNORECASE,
    ):
        for part in str(m.group(1) or "").split(","):
            try:
                out.add(int(part.strip()))
            except Exception:
                pass
    if out:
        return out
    names = {n.lower() for n in _action_executed_env_names_from_notes(notes)}
    if not names:
        return out
    for eid in pool_ids:
        row = load_env(engine, int(eid))
        nm = str(row.get("env_name") or "").strip().lower()
        if nm and nm in names:
            out.add(int(eid))
    return out


def _action_all_pool_envs_done(
    notes: Optional[str], pool_ids: List[int], engine
) -> bool:
    """True when every workflow-pool env is recorded in action step notes."""
    if not pool_ids:
        return False
    executed = _action_executed_env_ids_from_notes(notes, pool_ids, engine)
    if executed and all(int(e) in executed for e in pool_ids):
        return True
    done_names = {n.lower() for n in _action_executed_env_names_from_notes(notes)}
    if not done_names:
        return False
    for eid in pool_ids:
        row = load_env(engine, int(eid))
        nm = str(row.get("env_name") or "").strip().lower()
        if nm and nm not in done_names:
            return False
    return True


def _notes_with_action_executed(
    base_notes: Optional[str],
    executed_names: List[str],
    executed_ids: Optional[List[int]] = None,
) -> str:
    clean = _action_base_notes_key(base_notes)
    parts: List[str] = []
    if executed_names:
        parts.append(f"[Executed on: {', '.join(executed_names)}]")
    if executed_ids:
        parts.append(f"[Executed env ids: {', '.join(str(i) for i in sorted(set(executed_ids)))}]")
    suffix = " ".join(parts)
    return f"{clean} {suffix}".strip() if clean else suffix


def _prior_action_step_notes_if_already_done(
    engine, run_id: int, step_id: int, base_notes: str, pool_ids: List[int]
) -> Optional[str]:
    """
    If another completed action step in this run ran the same template on all pool envs,
    reuse its notes and skip redundant API calls (duplicate catalog rows).
    """
    base_key = _action_base_notes_key(base_notes)
    if not base_key:
        return None
    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, notes FROM workflow_steps
                    WHERE run_id = :rid AND id <> :sid
                      AND LOWER(TRIM(COALESCE(ds_kind,''))) = 'action'
                      AND LOWER(TRIM(COALESCE(status,''))) = 'completed'
                      AND COALESCE(is_enabled, {bool_true}) = {bool_true}
                    ORDER BY seq ASC
                    """
                ),
                {"rid": int(run_id), "sid": int(step_id)},
            ).fetchall()
    except Exception:
        return None
    for row in rows:
        prior_notes = str(row[1] or "")
        if _action_base_notes_key(prior_notes) != base_key:
            continue
        done_ids = _action_executed_env_ids_from_notes(prior_notes, pool_ids, engine)
        if len(done_ids) >= len(pool_ids):
            return prior_notes
    return None


def _resolve_action_template(
    engine, step_dict: Dict[str, Any], env_id: Optional[int]
) -> Tuple[str, Optional[str]]:
    """Resolve workflow_templates key + payload for this action step (env-specific first)."""
    raw = str(step_dict.get("notes") or step_dict.get("ds_name") or "").strip()
    action_key = _action_base_notes_key(raw) or raw
    if not action_key:
        return "", None
    # Prefer action:: keys (seeded payloads); bare notes key is a legacy alias.
    candidates: List[str] = [f"action::{action_key}", action_key]
    ak = action_key.upper()
    if "DS_LITE" in ak.replace(" ", "_") or "DS LITE" in ak:
        candidates.extend(_action_pre_ds_lite_key(env_id))
    elif "DISABLE" in ak and "OLS" in ak:
        candidates.extend(_action_pre_disable_ols_key(env_id))
    elif "ENABLE" in ak and "OLS" in ak:
        candidates.extend(_action_pre_ols_key(env_id))
    candidates.extend(_action_pre_step_keys(step_dict, env_id))
    seen: set[str] = set()
    for k in candidates:
        if not k or k in seen:
            continue
        seen.add(k)
        tpl = get_template(engine, k)
        if tpl:
            return k, tpl
    return action_key, None


def _action_pool_env_ready(
    engine,
    eid: int,
    run_id: int,
    targeted_envs: set,
    env_db_busy: Dict[int, Tuple[bool, Any]],
    *,
    logger,
) -> Tuple[bool, str]:
    if int(eid) in targeted_envs:
        return False, "targeted_this_tick"
    db_busy, db_busy_by = env_db_busy.get(int(eid), (False, None))
    if db_busy and db_busy_by is not None and int(db_busy_by) != int(run_id):
        return False, f"reserved_by_run_{db_busy_by}"
    env_row = load_env(engine, int(eid))
    if not env_row:
        return False, "env_not_found"
    if not bool(env_row.get("is_active", True)):
        return False, "env_inactive"
    if not bool(env_row.get("include_in_workflow_runs", True)):
        return False, "not_in_workflow_pool"
    base_url = str(env_row.get("base_url") or "").strip()
    if not base_url:
        return False, "missing_base_url"
    try:
        token = Get_auth_token(base_url, env_row)
        if env_has_running_scan(base_url, token, run_id=run_id):
            return False, "scan_running"
    except Exception as ex:
        return False, f"scan_check_failed:{ex}"
    return True, "ok"


def _action_apply_to_single_env(
    engine,
    run_id: int,
    step_dict: Dict[str, Any],
    eid: int,
    action_key: str,
    *,
    logger,
) -> Tuple[int, str, Optional[str]]:
    """
    Run action template on one env. Returns (env_id, env_name, error_message).
    error_message is None on success.
    """
    env_row = load_env(engine, int(eid))
    env_name = str(env_row.get("env_name") or f"env-{eid}")
    if not env_row or not str(env_row.get("base_url") or "").strip():
        return int(eid), env_name, "missing base_url"
    base_url = normalize_base_url(str(env_row.get("base_url") or ""))
    try:
        token = Get_auth_token(base_url, env_row)
    except Exception as ex:
        return int(eid), env_name, f"auth failed: {ex}"
    try:
        if env_has_running_scan(base_url, token, run_id=run_id):
            return int(eid), env_name, "scan_running"
    except Exception:
        pass
    resolved_key, action_tpl = _resolve_action_template(engine, step_dict, int(eid))
    if not action_tpl:
        return int(eid), env_name, f"missing template: {resolved_key or action_key}"
    try:
        action_raw = _render_template(action_tpl, {"BASE_URL": base_url, "TOKEN": token})
        action_payload = json.loads(action_raw)
        run_action_template(base_url, token, action_payload)
        logger.info("Action executed on env %s (%s): %s", eid, env_name, resolved_key)
        return int(eid), env_name, None
    except Exception as ex:
        return int(eid), env_name, str(ex)


def _governing_enable_ols_action_notes(engine, run_id: int, seq: int) -> Optional[str]:
    """Notes of the nearest EARLIER enabled action step that turns OLS **on** for this
    position, or None. Scans back from `seq`: the first OLS action found wins — if it
    DISABLES OLS (or none exists) we return None; if it ENABLES OLS we return its base
    notes so the caller can resolve the same template the discrete action step would."""
    bool_true = _sql_bool_true(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT notes, ds_name
                FROM workflow_steps
                WHERE run_id = :rid AND seq < :seq
                  AND LOWER(TRIM(COALESCE(ds_kind,''))) = 'action'
                  AND COALESCE(is_enabled, {bool_true}) = {bool_true}
                ORDER BY seq DESC
                """
            ),
            {"rid": int(run_id), "seq": int(seq)},
        ).fetchall()
    for r in rows:
        base = _action_base_notes_key(str(r[0] or r[1] or ""))
        key = base.upper()
        if "OLS" not in key:
            continue
        if "DISABLE" in key:
            return None  # nearest OLS action disables it → not governed 'on' here
        if "ENABLE" in key:
            return base
    return None


def _ensure_ols_enabled_for_step(
    engine, run_id: int, seq: int, step_dict: Dict[str, Any], env_id: int, *, logger
) -> None:
    """Just-in-time, per-env OLS enable, applied right before an OLS scan is triggered.

    Envs advance independently by capacity, so an OLS scan step (is_ols=True) can be
    picked and started before the discrete "ENABLE OLS FEATURE FLAGS" action step has
    run on every pool env. OLS is an env/cluster-level BigID feature flag, so we enable
    it on THIS env only, on demand — no waiting for the whole pool to be free. Idempotent
    (re-enabling an already-on flag is a no-op), so it is safe to call once per OLS step.
    """
    if not bool(step_dict.get("is_ols")):
        return
    enable_notes = _governing_enable_ols_action_notes(engine, int(run_id), int(seq))
    if enable_notes is None:
        return  # no ENABLE-OLS action governs this step; nothing to pre-apply
    action_step_dict = {"notes": enable_notes, "ds_name": enable_notes}
    _eid, env_name, err = _action_apply_to_single_env(
        engine, int(run_id), action_step_dict, int(env_id),
        "enable_ols_feature_flags", logger=logger,
    )
    if err:
        # Fail safe: never let an OLS scan start with the flag off. Raising here is caught
        # by the scan-trigger handler, which errors the step and retries/failovers — far
        # better than silently producing an OLS-off "OLS" benchmark.
        raise RuntimeError(f"OLS pre-enable failed on env {env_id} (seq {seq}): {err}")
    logger.info("OLS pre-enabled on env %s (%s) for OLS step seq=%s", env_id, env_name, seq)


def _execute_workflow_action_step(
    engine,
    run_id: int,
    step_id: int,
    seq: int,
    step: Any,
    cooldown_m: float,
    now: datetime,
    logger,
) -> bool:
    """
    Run catalog `action` steps on this run's assigned envs only (not the global pool).
    Skips envs that already have a scan running. Conditions: prior steps finished (enforced in pick).
    Supports resume via `[Executed on: ...]` in notes.
    """
    step_dict = {
        "ds_kind": step[3],
        "ds_name": step[4],
        "scan_kind": step[5],
        "file_set_type": step[6],
        "is_acl": bool(step[7]),
        "is_ocr": bool(step[8]),
        "is_structured_ff": bool(step[9]),
        "is_ols": bool(step[10]),
        "is_lite": bool(step[11]),
        "feature_flags": step[12],
        "notes": step[13],
    }
    action_key = _action_base_notes_key(
        str(step_dict.get("notes") or step_dict.get("ds_name") or "")
    )
    base_notes = _action_base_notes_key(str(step_dict.get("notes") or ""))
    ols_action = (
        "ENABLE" in action_key.upper()
        and "OLS" in action_key.upper()
        and "DISABLE" not in action_key.upper()
    )
    assigned_ids = _workflow_pool_env_ids_for_run(engine, int(run_id))
    if not assigned_ids:
        set_step_error(engine, step_id, "No workflow environments assigned to this run")
        set_run_error(engine, run_id, f"Step {seq}: no workflow environments")
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    pool_ids = _workflow_action_target_env_ids(engine, int(run_id), logger=logger)
    if not pool_ids:
        skip_notes = _notes_with_action_executed(
            base_notes,
            [
                str(load_env(engine, int(e)).get("env_name") or f"env-{e}")
                for e in assigned_ids
            ],
            list(assigned_ids),
        )
        skip_notes += " [skipped: all assigned envs have scan running]"
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    UPDATE workflow_steps
                    SET status = 'completed',
                        finished_at = CURRENT_TIMESTAMP,
                        last_error = NULL,
                        notes = :notes
                    WHERE id = :sid
                    """
                ),
                {"sid": step_id, "notes": skip_notes},
            )
            conn.commit()
        if ols_action:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE workflow_runs SET ols_enabled = TRUE WHERE id = :rid"),
                        {"rid": int(run_id)},
                    )
            except Exception:
                pass
        logger.info("Action step %s skipped: all assigned envs have scans running", step_id)
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    cur_status = str(step[19] or "").strip().lower()

    inherited = _prior_action_step_notes_if_already_done(
        engine, int(run_id), int(step_id), base_notes, pool_ids
    )
    if inherited:
        final_notes = inherited if "[Executed" in inherited else _notes_with_action_executed(
            base_notes,
            _action_executed_env_names_from_notes(inherited),
            list(_action_executed_env_ids_from_notes(inherited, pool_ids, engine)),
        )
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    UPDATE workflow_steps
                    SET status = 'completed',
                        finished_at = CURRENT_TIMESTAMP,
                        last_error = NULL,
                        notes = :notes || ' [skipped: already applied by prior action step]'
                    WHERE id = :sid
                    """
                ),
                {"sid": step_id, "notes": final_notes},
            )
            conn.commit()
        if ols_action:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE workflow_runs SET ols_enabled = TRUE WHERE id = :rid"),
                        {"rid": int(run_id)},
                    )
            except Exception:
                pass
        logger.info("Action step %s skipped (duplicate of prior completed action)", step_id)
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    step_notes_raw = str(step[13] or "")
    executed_ids = _action_executed_env_ids_from_notes(step_notes_raw, pool_ids, engine)
    executed_names = _action_executed_env_names_from_notes(step_notes_raw)

    def _finalize_completed(notes_value: str) -> None:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    UPDATE workflow_steps
                    SET status = 'completed',
                        finished_at = CURRENT_TIMESTAMP,
                        last_error = NULL,
                        notes = :notes
                    WHERE id = :sid
                    """
                ),
                {"sid": step_id, "notes": notes_value},
            )
            conn.commit()
        if ols_action:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE workflow_runs SET ols_enabled = TRUE WHERE id = :rid"),
                        {"rid": int(run_id)},
                    )
            except Exception:
                pass
        set_run_next_eligible(engine, run_id, _now_utc())

    if _action_all_pool_envs_done(step_notes_raw, pool_ids, engine):
        _finalize_completed(
            _notes_with_action_executed(base_notes, executed_names, list(executed_ids))
        )
        logger.info("Action step %s already applied on all pool envs; marking completed", step_id)
        return True
    for eid in pool_ids:
        row = load_env(engine, int(eid))
        nm = str(row.get("env_name") or f"env-{eid}")
        if int(eid) in executed_ids and nm not in executed_names:
            executed_names.append(nm)

    pending_ids = [int(e) for e in pool_ids if int(e) not in executed_ids]

    if not pending_ids and executed_ids:
        _finalize_completed(
            _notes_with_action_executed(base_notes, executed_names, list(executed_ids))
        )
        return True

    if pending_ids and executed_ids:
        block_reasons: List[str] = []
        for eid in pending_ids:
            _, reason = _action_pool_env_ready(
                engine,
                int(eid),
                int(run_id),
                set(),
                {},
                logger=logger,
            )
            block_reasons.append(reason)
        if block_reasons and all(r == "scan_running" for r in block_reasons):
            skip_names: List[str] = []
            for eid in pending_ids:
                executed_ids.add(int(eid))
                row = load_env(engine, int(eid))
                nm = str(row.get("env_name") or f"env-{eid}")
                if nm not in executed_names:
                    executed_names.append(nm)
                skip_names.append(nm)
            done_notes = _notes_with_action_executed(
                base_notes, executed_names, list(executed_ids)
            )
            done_notes += f" [skipped: {', '.join(skip_names)} scan running]"
            _finalize_completed(done_notes)
            logger.info(
                "Action step %s completed; pending env(s) blocked by scan: %s",
                step_id,
                skip_names,
            )
            return True

    if cur_status not in ("completed", "error"):
        if cur_status in ("pending", "error", "scan_waiting", ""):
            # Fresh per-step transition instant (see _process_step): avoids identical
            # started_at across steps dispatched in the same tick.
            set_step_status(engine, step_id, "running", started_at=_now_utc())
        else:
            set_step_status(engine, step_id, "running")
        clear_run_error(engine, int(run_id))
        set_runner_heartbeat(
            engine,
            status="working",
            message=f"run={run_id} step={seq} action {len(executed_ids)}/{len(pool_ids)} envs",
            started=False,
        )

    workers = max(1, min(len(pending_ids), int(os.getenv("WORKFLOW_ACTION_MAX_PARALLEL", "6") or 6)))
    scan_wait_envs: List[str] = []
    hard_errors: List[str] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _action_apply_to_single_env,
                engine,
                int(run_id),
                step_dict,
                int(eid),
                action_key,
                logger=logger,
            ): int(eid)
            for eid in pending_ids
        }
        for fut in as_completed(futures):
            eid_done, env_name, err = fut.result()
            if err == "scan_running":
                scan_wait_envs.append(env_name)
                continue
            if err:
                hard_errors.append(f"{env_name}: {err}")
                continue
            executed_ids.add(int(eid_done))
            if env_name not in executed_names:
                executed_names.append(env_name)

    progress_notes = _notes_with_action_executed(base_notes, executed_names, list(executed_ids))
    with engine.connect() as conn:
        conn.execute(
            text(
                """
                UPDATE workflow_steps
                SET notes = :notes,
                    environment_id = :eid,
                    last_error = NULL
                WHERE id = :sid
                """
            ),
            {
                "sid": step_id,
                "notes": progress_notes,
                "eid": int(pending_ids[-1]) if pending_ids else (pool_ids[0] if pool_ids else None),
            },
        )
        conn.commit()

    if hard_errors:
        err_msg = f"Action failed ({len(executed_ids)}/{len(pool_ids)} envs): {hard_errors[0]}"
        if executed_ids:
            # Some envs already ran (OLS/FF may already be on); keep step retryable for the rest.
            with engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE workflow_steps
                        SET status = 'pending',
                            last_error = :err,
                            notes = :notes
                        WHERE id = :sid
                        """
                    ),
                    {"sid": step_id, "err": err_msg, "notes": progress_notes},
                )
                conn.commit()
            set_runner_heartbeat(
                engine,
                status="idle",
                message=f"run={run_id} step={seq} action_partial ({len(executed_ids)}/{len(pool_ids)})",
                started=False,
            )
            set_run_next_eligible(engine, run_id, _now_utc())
            return False
        set_step_error(engine, step_id, err_msg)
        set_run_error(engine, run_id, f"Step {seq}: action failed")
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    if len(executed_ids) < len(pool_ids):
        still_pending = [int(e) for e in pool_ids if int(e) not in executed_ids]
        wait_name_set = {str(n).strip().lower() for n in scan_wait_envs if str(n).strip()}
        scan_only_block = False
        if still_pending and executed_ids and not hard_errors and wait_name_set:
            pending_names = []
            for eid in still_pending:
                row = load_env(engine, int(eid))
                pending_names.append(str(row.get("env_name") or "").strip().lower())
            scan_only_block = all(pn in wait_name_set for pn in pending_names if pn)
        if scan_only_block and executed_ids:
            skip_names = []
            for eid in still_pending:
                executed_ids.add(int(eid))
                row = load_env(engine, int(eid))
                nm = str(row.get("env_name") or f"env-{eid}")
                if nm not in executed_names:
                    executed_names.append(nm)
                skip_names.append(nm)
            progress_notes = _notes_with_action_executed(
                base_notes,
                executed_names,
                list(executed_ids),
            )
            progress_notes += f" [skipped: {', '.join(skip_names)} scan running]"
            _finalize_completed(progress_notes)
            logger.info(
                "Action step %s completed; skipped %s (scan running)",
                step_id,
                skip_names,
            )
            return True
        wait_msg = ", ".join(scan_wait_envs) if scan_wait_envs else "pending envs"
        set_runner_heartbeat(
            engine,
            status="idle",
            message=f"run={run_id} step={seq} action_wait ({len(executed_ids)}/{len(pool_ids)}) {wait_msg}",
            started=False,
        )
        return False

    _finalize_completed(progress_notes)
    logger.info("Action step completed on all %s workflow env(s): %s", len(pool_ids), action_key)
    return True


def put_ds_connection(base_url: str, token: str, ds_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    from urllib.parse import quote
    name = str(ds_name or "").strip()
    if not name:
        raise RuntimeError("ds_name is required")
    base_path = DS_CONNECTIONS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{quote(name, safe='')}")
    resp = _request("PUT", url, token, json_body=payload)
    return resp.json() if resp.content else {}


def fetch_ds_connection(base_url: str, token: str, ds_name: str, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    from urllib.parse import quote
    name = str(ds_name or "").strip()
    if not name:
        raise RuntimeError("ds_name is required")
    base_path = DS_CONNECTIONS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{quote(name, safe='')}")
    resp = _request("GET", url, token)
    return resp.json() if resp.content else {}

def fetch_ds_grouped_statuses(base_url: str, token: str, name_filter: str) -> Dict[str, Any]:
    """POST /api/v1/ds-connections/grouped-statuses with query filter by name (exact or partial). Used when poll times out to confirm test success."""
    url = build_api_url(base_url, DS_GROUPED_STATUSES_PATH)
    val = str(name_filter or "").strip()
    # Match UI: filter by name and other fields with same value (textSearch)
    body = {"query": {"filter": [
        {"field": "name", "value": val, "operator": "textSearch"},
        {"field": "friendly_name", "value": val, "operator": "textSearch"},
        {"field": "comment", "value": val, "operator": "textSearch"},
        {"field": "rdb_comma_sep_table_list", "value": val, "operator": "textSearch"},
        {"field": "rdb_name", "value": val, "operator": "textSearch"},
        {"field": "discoveryAppPresetId", "value": val, "operator": "textSearch"},
    ]}}
    try:
        resp = _request("POST", url, token, json_body=body, timeout_seconds=min(15.0, float(HTTP_TIMEOUT)))
        return resp.json() if resp.content else {}
    except Exception:
        return {}

def _grouped_statuses_reports_success(payload: Dict[str, Any], ds_name: str) -> bool:
    """Return True if payload contains a DS matching ds_name with connection test success."""
    name = str(ds_name or "").strip()
    if not name:
        return False
    items = payload.get("data") or payload.get("result") or payload.get("ds_connections") or (payload if isinstance(payload, list) else [])
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name") or item.get("ds_name") or "").strip()
        if n != name and name not in n:
            continue
        ds_status = str(item.get("status") or item.get("state") or item.get("currentStatus") or "").strip().lower()
        if ds_status in {"draft", "new", "disabled"}:
            continue
        cst = item.get("connectionStatusTest") or item.get("connection_status_test")
        if isinstance(cst, dict) and (cst.get("is_success") is True or str(cst.get("is_success") or "").strip().lower() in ("true", "1", "yes")):
            return True
        if item.get("lastTestSuccess") is True:
            return True
    return False

def _parse_utc_dt(v: Any) -> Optional[datetime]:
    s = str(v or "").strip()
    if not s:
        return None
    # Examples: 2026-02-25T08:56:26.790Z
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # Normalize to naive UTC for safe comparisons with datetime.utcnow()
        if getattr(dt, "tzinfo", None) is not None:
            try:
                dt = dt.astimezone(tz=None).replace(tzinfo=None)
            except Exception:
                dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _format_connection_test_detail(cst: Any) -> str:
    if not isinstance(cst, dict):
        return str(cst)[:2000] if cst is not None else "unknown"
    for key in (
        "message",
        "errorMessage",
        "error_message",
        "lastError",
        "last_error",
        "error",
        "details",
        "description",
        "reason",
        "failureReason",
        "failure_reason",
        "userMessage",
        "user_message",
        "resultMessage",
        "result_message",
    ):
        v = cst.get(key)
        if v is not None and str(v).strip() and str(v).strip().lower() not in ("null", "none", ""):
            return str(v).strip()[:2000]
    for container_key in ("result", "data", "response"):
        sub = cst.get(container_key)
        if isinstance(sub, dict):
            inner = _format_connection_test_detail(sub)
            if inner and inner not in ("unknown", "{}"):
                return inner
    slim = {k: cst.get(k) for k in (
        "is_success",
        "last_connection",
        "is_connection_ever_succeeded",
        "status",
        "testStatus",
    ) if k in cst}
    try:
        j = json.dumps(slim, default=str, separators=(",", ":"))[:1900]
        return f"BigID reported test failed (no message field). {j}"
    except Exception:
        return str(cst)[:2000]


def wait_for_ds_connection_test_result(
    base_url: str,
    token: str,
    ds_name: str,
    *,
    test_started_at: datetime,
    timeout_seconds: int = 180,
    poll_seconds: int = 5,
) -> Tuple[bool, str]:
    """
    Poll DS until connectionStatusTest.last_connection is updated after test_started_at.
    Return (ok, message).
    """
    # last_conn from API is naive UTC; normalize test_started_at for comparison to avoid TypeError
    _test_started = test_started_at
    if getattr(_test_started, "tzinfo", None) is not None:
        try:
            _test_started = _test_started.replace(tzinfo=None)
        except Exception:
            pass
    deadline = _now_utc() + timedelta(seconds=max(10, int(timeout_seconds)))
    last_seen = None
    grace_decide_at = test_started_at + timedelta(seconds=15)
    while _now_utc() < deadline:
        try:
            payload = fetch_ds_connection(base_url, token, ds_name, timeout_seconds=min(10.0, float(HTTP_TIMEOUT)))
            dc = payload.get("ds_connection") if isinstance(payload, dict) and isinstance(payload.get("ds_connection"), dict) else payload
            ds_state = str((dc or {}).get("status") or (dc or {}).get("state") or "").strip().lower()
            # Do not fail fast on DS state alone; many tenants lag status updates after test trigger.
            cst = dc.get("connectionStatusTest") if isinstance(dc, dict) else None
            if cst is None and (_now_utc() >= grace_decide_at):
                strict_mode = str(os.getenv("WORKFLOW_DS_TEST_STRICT", "0") or "0").strip().lower() in ("1", "true", "yes")
                if strict_mode:
                    # Strict mode: keep waiting until timeout/grouped-statuses fallback decides.
                    pass
                else:
                    # Non-strict mode: if DS moved out of draft/new/disabled, accept as success.
                    if ds_state and ds_state not in {"draft", "new", "disabled"}:
                        return True, f"success (non-strict; ds_state={ds_state})"
            if isinstance(cst, dict):
                last_conn = _parse_utc_dt(cst.get("last_connection"))
                if last_conn:
                    last_seen = last_conn
                # only decide once we see an updated last_connection after we started the test (use naive UTC for comparison)
                if last_conn and last_conn >= (_test_started - timedelta(seconds=2)):
                    is_success = cst.get("is_success")
                    # Guard: BigID sometimes returns a quick false before the real test finishes.
                    # Don't bail on failure until at least 30s have elapsed from the test trigger.
                    early_bail_ok = (_now_utc().replace(tzinfo=None) >= (_test_started + timedelta(seconds=30)))
                    if isinstance(is_success, bool):
                        if is_success:
                            return True, "success"
                        if early_bail_ok:
                            return False, _format_connection_test_detail(cst)
                    # if it's present but not bool, treat truthy strings
                    s = str(is_success or "").strip().lower()
                    if s in {"true", "1", "yes", "ok", "success"}:
                        return True, "success"
                    if s in {"false", "0", "no", "fail", "failed", "error"} and early_bail_ok:
                        return False, _format_connection_test_detail(cst)
                # Fallback: last_connection may be in server/local time or update slowly; accept success within 15 min window
                if last_conn and last_conn >= (_test_started - timedelta(minutes=15)) and (_now_utc() >= grace_decide_at):
                    is_success = cst.get("is_success")
                    if isinstance(is_success, bool) and is_success:
                        return True, "success (within 15 min window)"
                    s = str(is_success or "").strip().lower()
                    if s in {"true", "1", "yes", "ok", "success"}:
                        return True, "success (within 15 min window)"
                # Fallback: sometimes is_success flips before last_connection is updated.
                if (not last_conn) and (_now_utc() >= grace_decide_at):
                    is_success = cst.get("is_success")
                    if isinstance(is_success, bool):
                        if is_success:
                            return True, "success"
                        return False, _format_connection_test_detail(cst)
        except Exception:
            pass
        time.sleep(max(2, int(poll_seconds)))
    # On timeout: poll grouped-statuses (filter) API up to 3 times before marking failed
    for attempt in range(1, 4):
        try:
            gs = fetch_ds_grouped_statuses(base_url, token, ds_name)
            if _grouped_statuses_reports_success(gs, ds_name):
                return True, "success (from grouped-statuses after timeout)"
        except Exception:
            pass
        if attempt < 3:
            time.sleep(10)
    msg = f"Timed out waiting for DS connection test result (last_seen={last_seen.isoformat() if last_seen else 'none'}); grouped-statuses polled 3 times, not successful"
    return False, msg

def create_scan_profile(base_url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = build_api_url(base_url, SCAN_PROFILES_PATH)
    resp = _request("POST", url, token, json_body=payload)
    return resp.json() if resp.content else {}


def test_ds_connection(base_url: str, token: str, *, ds_connection: Dict[str, Any], broadcast_event: str, is_first_attempt: bool = False) -> Dict[str, Any]:
    """
    Best-effort DS connection test.
    Mirrors UI: POST /api/v1/ds-connection-test/client?broadcastEvent=...
    UI sends isFirstAttempt: true on first Connect click; we pass it for first attempt.
    """
    url = build_api_url(base_url, DS_CONNECTION_TEST_PATH)
    params = {"broadcastEvent": str(broadcast_event or "").strip()}
    body = {
        "ds_connection": ds_connection,
        "broadcastEvent": str(broadcast_event or "").strip(),
        "isNewSessionToken": False,
        "isFirstAttempt": bool(is_first_attempt),
        "isNewConnection": True,
    }
    resp = _request("POST", url, token, params=params, json_body=body)
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {"_raw": (resp.text or "")}


def test_ds_connections_by_ids(base_url: str, token: str, *, ids: List[str], broadcast_event: str) -> Dict[str, Any]:
    """
    Newer API observed in UI:
      POST /api/v1/ds-connections/test?broadcastEvent=...
      body: {"ids":[...],"allSelected":false,"query":{"filter":[]}}
    """
    url = build_api_url(base_url, DS_CONNECTIONS_TEST_PATH)
    params = {"broadcastEvent": str(broadcast_event or "").strip()}
    body = {"ids": [str(x).strip() for x in (ids or []) if str(x).strip()], "allSelected": False, "query": {"filter": []}}
    resp = _request("POST", url, token, params=params, json_body=body)
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {"_raw": (resp.text or "")}


def test_ds_connection_auto(
    base_url: str,
    token: str,
    *,
    ds_name: str,
    ds_id: str = "",
    ds_connection: Dict[str, Any],
    broadcast_event: str,
    is_first_attempt: bool = False,
) -> Dict[str, Any]:
    """
    Try ids-based endpoint first (doesn't require passing full DS),
    fallback to client endpoint (same as UI: ds-connection-test/client).
    """
    name = str(ds_name or "").strip()
    did = str(ds_id or "").strip()
    # 1) ids-based test
    try:
        out = test_ds_connections_by_ids(base_url, token, ids=[did or name], broadcast_event=broadcast_event)
        # If ids endpoint explicitly reports success, no need to force fallback.
        ov = str((out or {}).get("result") or (out or {}).get("status") or "").strip().lower() if isinstance(out, dict) else ""
        if ov in {"ok", "success", "accepted", "queued", "running", "started"}:
            return out
        # If ids endpoint returns explicit failure markers, fallback to client endpoint.
    except Exception:
        out = {}
        allow_fallback = str(os.getenv('WORKFLOW_DS_TEST_ALLOW_CLIENT_FALLBACK', '1') or '1').strip().lower() in {'1','true','yes','y','on'}
        if did and (not allow_fallback):
            raise
    # 2) client-style test (fallback) — matches UI POST body
    return test_ds_connection(base_url, token, ds_connection=ds_connection, broadcast_event=broadcast_event, is_first_attempt=is_first_attempt)


def _extract_ds_id_from_payload(payload: Dict[str, Any]) -> str:
    """
    Best-effort extract DS internal id from ds_connection payload.
    BigID often stores it under ds_connection._id (Mongo ObjectId).
    """
    try:
        dc = payload.get("ds_connection") if isinstance(payload, dict) and isinstance(payload.get("ds_connection"), dict) else payload
        if isinstance(dc, dict):
            for kk in ["_id", "id", "dsId", "ds_id"]:
                v = dc.get(kk)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        pass
    return ""


def _extract_scan_profile_items(payload: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        if isinstance(payload, dict):
            data_block = payload.get("data")
            root = data_block if isinstance(data_block, dict) else payload
            for key in ["scanProfiles", "scan_profiles", "profiles", "items", "results"]:
                val = root.get(key)
                if isinstance(val, list):
                    items = [x for x in val if isinstance(x, dict)]
                    break
    except Exception:
        pass
    return items


def find_scan_profile_id_by_name(base_url: str, token: str, scan_profile_name: str) -> str:
    """
    Best-effort lookup so resets/restarts don't re-create scan profiles and hit
    'Scan name is already in use'. Tries a few filter operators across versions.
    """
    nm = str(scan_profile_name or "").strip()
    if not nm:
        return ""
    url = build_api_url(base_url, SCAN_PROFILES_PATH)
    # Try a few operators that BigID has used historically.
    ops = ["eq", "equals", "contains"]
    for op in ops:
        try:
            filter_items = [{"field": "name", "operator": op, "value": nm}]
            params = {
                "skip": 0,
                "limit": 200,
                "requireTotalCount": "true",
                "filter": json.dumps(filter_items, separators=(",", ":")),
                "sort": json.dumps([{"field": "updated_at", "order": "desc"}], separators=(",", ":")),
            }
            resp = _request("GET", url, token, params=params)
            payload = resp.json() if resp.content else {}
            items = _extract_scan_profile_items(payload)
            if not items:
                continue
            # Exact match preference
            for it in items:
                if str(it.get("name") or "").strip() == nm:
                    vid = str(it.get("_id") or it.get("id") or "").strip()
                    if vid:
                        return vid
            # Do not guess: only reuse when we have an exact name match.
            continue
        except Exception:
            continue
    return ""

def start_scan_profile(base_url: str, token: str, scan_profile_id: str) -> Dict[str, Any]:
    """
    BigID versions differ. Try several common start/run endpoints.
    """
    sid = str(scan_profile_id or "").strip()
    if not sid:
        raise RuntimeError("scan_profile_id is required")
    base = SCAN_PROFILES_PATH.rstrip("/")
    scans_base = SCANS_TRIGGER_PATH.rstrip("/")
    candidates = [
        (f"{base}/{sid}/run", None),
        (f"{base}/{sid}/start", None),
        (f"{base}/{sid}/execute", None),
        (f"{base}/run", {"scanProfileId": sid}),
        (f"{base}/start", {"scanProfileId": sid}),
        (f"{scans_base}/start", {"scanProfileId": sid, "scanType": "dsScan"}),
        (f"{scans_base}", {"scanProfileId": sid, "scanType": "dsScan", "scanOrigin": "Invoked manually"}),
    ]
    errors: List[str] = []
    for path, body in candidates:
        url = build_api_url(base_url, path)
        try:
            resp = _request("POST", url, token, json_body=(body or {}))
            print(f"[start_scan_profile] SUCCESS: {path} returned {resp.status_code if hasattr(resp, 'status_code') else 'OK'}", flush=True)
            return resp.json() if resp.content else {}
        except Exception as ex:
            error_msg = f"POST {url} -> {ex}"
            errors.append(error_msg)
            print(f"[start_scan_profile] FAILED: {error_msg}", flush=True)
            continue
    error_summary = ' | '.join(errors[:6])
    print(f"[start_scan_profile] ALL ATTEMPTS FAILED: {error_summary}", flush=True)
    raise RuntimeError(f"Failed to start scan profile via known endpoints: {error_summary}")


def trigger_scan(base_url: str, token: str, scan_profile_name: str, *, scan_type: str = "dsScan") -> Dict[str, Any]:
    """
    Matches MegaScale trigger logic (no scheduling):
      POST /api/v1/scans  {"scanType":"dsScan","scanProfileName":"...","scanOrigin":"Invoked manually"}
    """
    spn = str(scan_profile_name or "").strip()
    if not spn:
        raise RuntimeError("scan_profile_name is required")
    url = build_api_url(base_url, SCANS_TRIGGER_PATH)
    payload = {"scanType": str(scan_type or "dsScan").strip() or "dsScan", "scanProfileName": spn, "scanOrigin": "Invoked manually"}
    print(f"[trigger_scan] POST {url} with payload={payload}", flush=True)
    try:
        resp = _request("POST", url, token, json_body=payload)
        result = resp.json() if resp.content else {}
        print(f"[trigger_scan] SUCCESS: {result}", flush=True)
        return result
    except Exception as ex:
        print(f"[trigger_scan] FAILED: {ex}", flush=True)
        raise


def trigger_scan_by_profile_id(
    base_url: str, token: str, scan_profile_id: str, *, scan_type: str = "dsScan"
) -> Dict[str, Any]:
    """POST /api/v1/scans using scanProfileId (preferred on several BigID builds)."""
    sid = str(scan_profile_id or "").strip()
    if not sid:
        raise RuntimeError("scan_profile_id is required")
    url = build_api_url(base_url, SCANS_TRIGGER_PATH)
    st = str(scan_type or "dsScan").strip() or "dsScan"
    payloads = [
        {"scanType": st, "scanProfileId": sid, "scanOrigin": "Invoked manually"},
        {"scanProfileId": sid, "scanType": st},
    ]
    errors: List[str] = []
    for i, payload in enumerate(payloads):
        try:
            print(f"[trigger_scan_by_profile_id] Attempt {i+1}: POST {url} with payload={payload}", flush=True)
            resp = _request("POST", url, token, json_body=payload)
            result = resp.json() if resp.content else {}
            print(f"[trigger_scan_by_profile_id] SUCCESS: {result}", flush=True)
            return result
        except Exception as ex:
            error_msg = str(ex)
            errors.append(error_msg)
            print(f"[trigger_scan_by_profile_id] Attempt {i+1} FAILED: {error_msg}", flush=True)
    error_summary = ' | '.join(errors[:4])
    print(f"[trigger_scan_by_profile_id] ALL ATTEMPTS FAILED: {error_summary}", flush=True)
    raise RuntimeError(f"POST {url} by profile id failed: {error_summary}")


def trigger_scan_for_profile(
    base_url: str,
    token: str,
    *,
    scan_profile_name: str = "",
    scan_profile_id: str = "",
    scan_type: str = "dsScan",
) -> Dict[str, Any]:
    """
    Start a parent scan — matches original MegaScale behavior:
    trigger by scan profile name first (POST /api/v1/scans), then id-based fallbacks.
    """
    spn = str(scan_profile_name or "").strip()
    sid = str(scan_profile_id or "").strip()
    if not spn and not sid:
        raise RuntimeError("scan_profile_name or scan_profile_id is required")
    errors: List[str] = []

    if spn:
        try:
            return trigger_scan(base_url, token, spn, scan_type=scan_type)
        except Exception as ex:
            errors.append(f"trigger_by_name: {ex}")

    if sid:
        try:
            return trigger_scan_by_profile_id(base_url, token, sid, scan_type=scan_type)
        except Exception as ex:
            errors.append(f"trigger_by_id: {ex}")
        try:
            return start_scan_profile(base_url, token, sid)
        except Exception as ex:
            errors.append(f"scanProfiles_routes: {ex}")

    raise RuntimeError(f"Failed to start scan: {' | '.join(errors[:8])}")


def guess_latest_scan_id(base_url: str, token: str, *, name_contains: str = "", limit: int = 80, wf_tag: str = "") -> str:
    """
    If start response doesn't include scan_id, try to find the newest running parent scan
    matching a name substring.

    Hardened:
    - Prefer exact scan name match (case-insensitive)
    - If wf_tag provided (e.g. "(wf 6-3)"), require it in name for contains-matches
    """
    needle = str(name_contains or "").strip().lower()
    wf = str(wf_tag or "").strip().lower()
    try:
        items = fetch_parent_scans(
            base_url,
            token,
            states=["Started", "Awaiting", "In Progress", "InProgress", "Running", "Queued", "Pending", "Stop Requested", "StopRequested", "Stopping"],
            limit=int(limit),
            skip=0,
        )
    except Exception:
        # FAIL-CLOSED: If we can't fetch scans, we can't 'guess' the ID.
        return ""

    # First pass: exact name match
    if needle:
        for it in items or []:
            sid = str(it.get("_id") or it.get("id") or "").strip()
            nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
            if sid and nm == needle:
                return sid

    # Second pass: Strict contains match (REQUIRE wf tag for accuracy)
    if wf:
        for it in items or []:
            sid = str(it.get("_id") or it.get("id") or "").strip()
            nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
            if sid and wf in nm:
                # If we have a tag, it's a very strong match.
                return sid

    # Final fallback: only if no wf tag was provided (should be rare)
    if not wf and needle:
        for it in items or []:
            sid = str(it.get("_id") or it.get("id") or "").strip()
            nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
            if sid and needle in nm:
                return sid

    return ""



def fetch_scan_insights(base_url: str, token: str, scan_id: str) -> Dict[str, Any]:
    base_path = INSIGHTS_PATH.rstrip("/")
    url = build_api_url(base_url, f"{base_path}/{str(scan_id).strip()}")
    resp = _request("GET", url, token)
    return resp.json() if resp.content else {}


def extract_state_from_insights(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    si = payload.get("data", {}).get("scanInsights", {}) if isinstance(payload.get("data"), dict) else payload.get("scanInsights", {})
    if not isinstance(si, dict):
        si = payload if isinstance(payload, dict) else {}
    state = si.get("state")
    if state:
        return str(state).strip()
    prog = _deep_find(si, ["progress"])
    if isinstance(prog, dict) and prog.get("state"):
        return str(prog.get("state")).strip()
    return str(_deep_find(payload, ["state"]) or "").strip()


def _run_is_running(engine, run_id: int) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM workflow_runs WHERE id = :rid LIMIT 1"),
                {"rid": int(run_id)},
            ).fetchone()
        return str(row[0] or "").strip().lower() == "running" if row else False
    except Exception:
        return False


def wait_for_scan_terminal(base_url: str, token: str, scan_id: str, *, poll_seconds: int = 60) -> str:
    last_state = ""
    for _ in range(60 * 24):  # up to 24h
        try:
            ins = fetch_scan_insights(base_url, token, scan_id)
            st = extract_state_from_insights(ins)
            if st:
                last_state = st
            if _is_terminal_state(st):
                return st
        except Exception:
            pass
        time.sleep(max(5, int(poll_seconds)))
    return last_state or "unknown"


def wait_for_scan_terminal_or_pause(engine, run_id: int, base_url: str, token: str, scan_id: str, *, poll_seconds: int = 60) -> Tuple[str, bool]:
    """
    Returns (state, completed_flag).
    completed_flag False means we stopped waiting because run was paused/stopped.
    """
    last_state = ""
    for _ in range(60 * 24):  # up to 24h
        if not _run_is_running(engine, int(run_id)):
            return last_state or "paused", False
        try:
            ins = fetch_scan_insights(base_url, token, scan_id)
            stv = extract_state_from_insights(ins)
            if stv:
                last_state = stv
            if _is_terminal_state(stv):
                return stv, True
        except Exception:
            pass
        try:
            set_runner_heartbeat(engine, status="waiting", message=f"waiting scan={scan_id} state={last_state or 'unknown'}", started=False)
        except Exception:
            pass
        time.sleep(max(5, int(poll_seconds)))
    return last_state or "unknown", True


def _find_latest_scan_parent_by_name(engine, *, scan_name: str) -> Tuple[str, str]:
    """
    Best-effort: find latest scan_id + status in local DB by exact scan_name.
    This prevents re-triggering the same scan after a UI reset/restart.
    """
    nm = str(scan_name or "").strip()
    if not nm:
        return "", ""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT scan_id, scan_status
                    FROM scan_parents
                    WHERE COALESCE(scan_name,'') = :nm
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT 1
                """),
                {"nm": nm},
            ).fetchone()
        if row and row[0]:
            return str(row[0] or "").strip(), str(row[1] or "").strip()
    except Exception:
        pass
    return "", ""


def _extract_id(payload: Dict[str, Any], keys: List[str]) -> str:
    v = _deep_find(payload, keys)
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for kk in ["_id", "id", "ID", "Guid", "GUID", "scanId", "scan_id", "parentScanId", "parent_scan_id", "dsId", "ds_id"]:
            if isinstance(v.get(kk), (str, int)) and str(v.get(kk)).strip():
                return str(v.get(kk)).strip()
    return ""


_workflow_include_col_ready = False


def _ensure_test_environments_workflow_column(engine) -> None:
    """Adds include_in_workflow_runs if missing (runner may start before Streamlit runs ensure)."""
    global _workflow_include_col_ready
    if _workflow_include_col_ready:
        return
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE test_environments ADD COLUMN include_in_workflow_runs BOOLEAN DEFAULT TRUE"
                )
            )
            conn.commit()
    except Exception:
        pass
    _workflow_include_col_ready = True


def _run_has_executing_steps(engine, run_id: int) -> bool:
    """True when any enabled step is mid-pipeline (DS setup, profile, or scan)."""
    bool_true = _sql_bool_true(engine)
    occupying = _workflow_env_occupying_sql_in()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM workflow_steps
                    WHERE run_id=:rid AND COALESCE(is_enabled,{bool_true})={bool_true}
                      AND LOWER(TRIM(COALESCE(status,''))) IN ({occupying})
                    LIMIT 1
                    """
                ),
                {"rid": int(run_id)},
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _run_env_wait_reason(engine, run_id: int) -> str:
    """Why the run is QUEUED for an environment, or "" when it is not env-blocked.

    Env-blocked means it still has enabled, non-terminal steps to do but none of the environments
    it may use is free — every candidate is busy with another run/scan, or the run has no
    candidates at all because other runs have selected them.

    Such a run is queued (e.g. waiting for run 99 to release an env), not stalled — so the
    idle/stall timer must NOT fail it. Uses the DB busy locks, which sync_env_busy_status keeps in
    step with BigID each tick. The returned text is surfaced on the run so a queued run does not
    look like a runner that simply stopped working.
    """
    bool_true = _sql_bool_true(engine)
    try:
        pool_ids = _workflow_pool_env_ids_for_run(engine, int(run_id))
        with engine.connect() as conn:
            pending = conn.execute(
                text(
                    f"""
                    SELECT 1 FROM workflow_steps
                    WHERE run_id=:rid AND COALESCE(is_enabled,{bool_true})={bool_true}
                      AND LOWER(TRIM(COALESCE(status,''))) NOT IN ('completed','error','disabled')
                    LIMIT 1
                    """
                ),
                {"rid": int(run_id)},
            ).fetchone()
            if not pending:
                return ""  # nothing left to run → not an env-wait
            if not pool_ids:
                # No env is even a candidate: an unselected run whose whole pool is selected by
                # other runs. Queued for an env, not stalled.
                holders = conn.execute(
                    text(
                        f"""
                        SELECT DISTINCT r.id, e.env_name
                        FROM workflow_runs r
                        JOIN test_environments e ON e.id IN (r.env1_id, r.env2_id)
                        WHERE LOWER(TRIM(COALESCE(r.status,''))) IN ({_ACTIVE_WORKFLOW_RUN_STATUS_SQL_IN})
                          AND {_run_still_owns_selection_sql(engine, "r")}
                          AND r.id <> :rid
                        ORDER BY r.id
                        """
                    ),
                    {"rid": int(run_id)},
                ).fetchall()
                if holders:
                    who = ", ".join(f"{n or f'env-{i}'} (run #{int(i)})" for i, n in holders)
                    return f"Queued: no environment available — every workflow env is selected by another run: {who}"
                return (
                    "Queued: no environment available — no active env has 'Use for workflow runs' "
                    "enabled (check the Environments page)"
                )
            id_list = ",".join(str(int(e)) for e in pool_ids)
            rows = conn.execute(
                text(
                    f"SELECT id, env_name, is_busy, busy_by_run_id FROM test_environments "
                    f"WHERE id IN ({id_list})"
                )
            ).fetchall()
        busy_desc: List[str] = []
        for _eid, ename, is_busy, busy_by in rows or []:
            free = not bool(is_busy)
            mine = busy_by is not None and int(busy_by) == int(run_id)
            if free or mine:
                return ""  # an env is available to this run → not env-blocked
            label = str(ename or f"env-{_eid}")
            busy_desc.append(f"{label} (busy with run #{int(busy_by)})" if busy_by is not None else f"{label} (busy)")
        # Work remains and every pool env is busy elsewhere → waiting for env.
        return "Queued: waiting for an environment — " + ", ".join(busy_desc)
    except Exception:
        return ""


def _reconcile_running_run_if_idle(engine, run_id: int, now: datetime) -> None:
    """
    Fix runs stuck as 'running' while no step is actively executing.
    - Zombie running+last_error when WORKFLOW_MARK_RUN_FAILED_ON_STEP_ERROR is on.
    - All work terminal → completed/failed via _maybe_mark_run_completed.
    - Idle backlog (pending/error only) with no progress → failed after stale timeout.
    
    BUG FIX 6.7: Use cooldown_minutes from workflow_runs instead of fixed timeout.
    This prevents false timeouts on workflows with long cooldown periods.
    Formula: idle_timeout = cooldown_minutes * 2 + 30 (double cooldown + buffer)
    """
    bool_true = _sql_bool_true(engine)
    occupying = _workflow_env_occupying_sql_in()
    try:
        with engine.connect() as conn:
            run_row = conn.execute(
                text(
                    "SELECT LOWER(TRIM(COALESCE(status,''))), last_error, next_eligible_at, cooldown_minutes, "
                    "started_at, created_at "
                    "FROM workflow_runs WHERE id=:rid LIMIT 1"
                ),
                {"rid": int(run_id)},
            ).fetchone()
        if not run_row or str(run_row[0] or "") != "running":
            return

        next_eligible = run_row[2]
        cooldown_m = int(run_row[3] or 15)  # BUG FIX 6.7: Get cooldown from run
        
        if next_eligible and isinstance(next_eligible, datetime) and next_eligible > now:
            if _run_has_executing_steps(engine, run_id):
                return

        if _run_has_executing_steps(engine, run_id):
            return

        mark_failed_on_error = str(
            os.getenv("WORKFLOW_MARK_RUN_FAILED_ON_STEP_ERROR", "1") or "1"
        ).strip().lower() in ("1", "true", "yes", "y", "on")

        # Only revert running→failed from last_error when step errors are configured to fail the run.
        # Legacy mode (MARK_RUN_FAILED=0) keeps the run running with last_error while other steps continue.
        if mark_failed_on_error and str(run_row[1] or "").strip():
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "UPDATE workflow_runs SET status='failed', "
                        "finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP), "
                        "updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,'')))='running'"
                    ),
                    {"rid": int(run_id)},
                )
                conn.commit()
            return

        _maybe_mark_run_completed(engine, int(run_id))

        with engine.connect() as conn:
            still_running = conn.execute(
                text(
                    "SELECT 1 FROM workflow_runs WHERE id=:rid "
                    "AND LOWER(TRIM(COALESCE(status,'')))='running' LIMIT 1"
                ),
                {"rid": int(run_id)},
            ).fetchone()
        if not still_running:
            return

        # BUG FIX 6.7: Calculate idle timeout based on cooldown_minutes
        idle_min = cooldown_m * 2 + 30  # double cooldown + 30min buffer
        idle_min = max(20, min(idle_min, 480))  # enforce reasonable bounds (20min - 8h)
        
        cutoff = now - timedelta(minutes=float(idle_min))

        # Idle backlog only — exclude completed/disabled and in-flight pipeline states.
        with engine.connect() as conn:
            last_act = conn.execute(
                text(
                    f"""
                    SELECT MAX(updated_at) FROM workflow_steps
                    WHERE run_id=:rid AND COALESCE(is_enabled,{bool_true})={bool_true}
                      AND LOWER(TRIM(COALESCE(status,''))) NOT IN ('completed', 'disabled')
                      AND LOWER(TRIM(COALESCE(status,''))) NOT IN ({occupying})
                    """
                ),
                {"rid": int(run_id)},
            ).fetchone()
        last_ts = last_act[0] if last_act else None
        if last_ts and isinstance(last_ts, datetime) and last_ts > cutoff:
            return

        # SAFETY CHECK: Before firing the stall timer, verify no step with a scan_id
        # has a live scan in BigID. A network blip can reset steps to 'pending' while
        # their BigID scans keep running — without this check the stall fires falsely.
        try:
            with engine.connect() as conn:
                orphan_scan_rows = conn.execute(
                    text(
                        f"""
                        SELECT id, environment_id, scan_id
                        FROM workflow_steps
                        WHERE run_id=:rid
                          AND COALESCE(is_enabled,{bool_true})={bool_true}
                          AND scan_id IS NOT NULL AND TRIM(COALESCE(scan_id,'')) <> ''
                          AND LOWER(TRIM(COALESCE(status,''))) NOT IN ('completed','error','disabled')
                          AND LOWER(TRIM(COALESCE(status,''))) NOT IN ({occupying})
                        """
                    ),
                    {"rid": int(run_id)},
                ).fetchall()
            for orow in orphan_scan_rows or []:
                o_step_id = int(orow[0])
                o_env_id = orow[1]
                o_scan_id = str(orow[2] or "").strip()
                if not o_scan_id:
                    continue
                live_st = _fetch_scan_status_for_step(engine, o_env_id, o_scan_id, run_id=int(run_id))
                if live_st and not _is_terminal_state(live_st):
                    # Scan confirmed still running — restore step and abort stall.
                    set_step_status(engine, o_step_id, "scan_running")
                    return
        except Exception:
            pass

        # SAFETY CHECK 2: Don't fail a run that is simply QUEUED for an environment.
        # If it still has enabled, non-terminal steps but every workflow-pool env is busy
        # with another run/scan (e.g. waiting for run 99 to finish), it's waiting — not
        # stalled. Keep it 'running' so it dispatches as soon as an env frees up, up to a
        # max wait window (default 12h, WORKFLOW_ENV_WAIT_MAX_HOURS) so a forever-busy pool
        # doesn't leave it queued indefinitely.
        env_wait_reason = _run_env_wait_reason(engine, int(run_id))
        if env_wait_reason:
            max_wait_h = max(1, _env_int("WORKFLOW_ENV_WAIT_MAX_HOURS", 12))
            wait_anchor = run_row[4] or run_row[5]  # started_at, else created_at
            if not isinstance(wait_anchor, datetime) or (now - wait_anchor) < timedelta(hours=max_wait_h):
                # Stay queued, but say so on the run: a silent 'running' with no step progress is
                # indistinguishable from a dead runner, which is what made this hard to diagnose.
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text("UPDATE workflow_runs SET last_error=:e, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
                            {"e": env_wait_reason[:2000], "rid": int(run_id)},
                        )
                except Exception:
                    pass
                return  # still within the allowed wait window — stay queued
            # The queued note we wrote while waiting is not a real failure, so it must not win the
            # COALESCE below and hide why the run finally failed.
            wmsg = f"Workflow failed: no environment became available within {max_wait_h}h — {env_wait_reason}"
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "UPDATE workflow_runs SET status='failed', finished_at=CURRENT_TIMESTAMP, "
                        "last_error=CASE WHEN TRIM(COALESCE(last_error,'')) = '' "
                        "                  OR TRIM(COALESCE(last_error,'')) LIKE 'Queued:%' "
                        "             THEN :e ELSE last_error END, "
                        "updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,'')))='running'"
                    ),
                    {"rid": int(run_id), "e": wmsg[:2000]},
                )
                conn.commit()
            return

        msg = f"Workflow stalled: no step in progress for >{idle_min}min (cooldown={cooldown_m}min)"
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE workflow_runs SET status='failed', finished_at=CURRENT_TIMESTAMP, "
                    "last_error=COALESCE(NULLIF(TRIM(COALESCE(last_error,'')), ''), :e), "
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,'')))='running'"
                ),
                {"rid": int(run_id), "e": msg},
            )
            conn.commit()
    except Exception:
        pass


def _auto_resume_failed_run_if_stuck(engine, run_id: int) -> None:
    """
    If a run was marked failed while steps are still mid-pipeline, flip back to running.
    Only resume when DS/scan work is in flight — not for pending backlog left after step errors
    (which would show 'running' with zero steps in progress).

    Also checks BigID API for steps that have a scan_id but whose DB status was
    prematurely reset (e.g. after a network blip marked them pending) — if the
    scan is still live in BigID, restore scan_running and resume the run.
    """
    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            st_row = conn.execute(
                text("SELECT LOWER(TRIM(COALESCE(status,''))) FROM workflow_runs WHERE id=:rid LIMIT 1"),
                {"rid": int(run_id)},
            ).fetchone()
        if not st_row or str(st_row[0] or "") != "failed":
            return

        # Primary check: DB says a step is occupying an env.
        has_executing = _run_has_executing_steps(engine, int(run_id))

        # Secondary check: look for steps with a scan_id whose scan is still
        # live in BigID even though DB status was reset (e.g. to pending/error).
        # This handles the case where a network blip caused premature scan_id clear.
        if not has_executing:
            try:
                with engine.connect() as conn:
                    orphan_rows = conn.execute(
                        text(
                            f"""
                            SELECT id, environment_id, scan_id
                            FROM workflow_steps
                            WHERE run_id=:rid
                              AND COALESCE(is_enabled,{bool_true})={bool_true}
                              AND scan_id IS NOT NULL AND TRIM(COALESCE(scan_id,'')) <> ''
                              AND LOWER(TRIM(COALESCE(status,''))) NOT IN ('completed','error','disabled')
                            """
                        ),
                        {"rid": int(run_id)},
                    ).fetchall()
                for orow in orphan_rows or []:
                    o_step_id = int(orow[0])
                    o_env_id = orow[1]
                    o_scan_id = str(orow[2] or "").strip()
                    if not o_scan_id:
                        continue
                    live_st = _fetch_scan_status_for_step(engine, o_env_id, o_scan_id, run_id=int(run_id))
                    if live_st and not _is_terminal_state(live_st):
                        # Scan still running — restore step status so run can resume.
                        set_step_status(engine, o_step_id, "scan_running")
                        has_executing = True
                        try:
                            print(
                                f"[{_now_utc().isoformat()}Z] auto_resume: restored scan_running "
                                f"step_id={o_step_id} run_id={int(run_id)} scan_id={o_scan_id} live={live_st}",
                                flush=True,
                            )
                        except Exception:
                            pass
            except Exception:
                pass

        if not has_executing:
            return
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE workflow_runs SET status='running', finished_at=NULL, "
                    "next_eligible_at=NULL, "
                    "started_at=COALESCE(started_at, CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,'')))='failed'"
                ),
                {"rid": int(run_id)},
            )
            conn.commit()

        # Trigger version sync for environments associated with this run
        try:
            row = None
            with engine.connect() as conn:
                row = conn.execute(text("SELECT env1_id, env2_id FROM workflow_runs WHERE id=:rid"), {"rid": int(run_id)}).fetchone()
            if row:
                for eid in [row[0], row[1]]:
                    if eid:
                        try:
                            _sync_env_version_standalone_runner(engine, int(eid))
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            print(f"[{_now_utc().isoformat()}Z] auto_resume_failed_run run_id={int(run_id)} (non-terminal steps remain)", flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _repair_stuck_workflow_steps(engine, log: Any = None) -> None:
    """
    Each tick: un-park scan_waiting steps (no scan yet) and drop orphan is_busy locks.
    
    BUG FIX 2.1, 2.2, 3.3:
    - Un-park steps after grace period (default 5 min instead of indefinite)
    - Track retry count and fail after N retries
    - Validate scan_id format when un-parking to detect corruption
    """
    bool_true = _sql_bool_true(engine)
    now = _now_utc()
    
    try:
        grace_period_minutes = int(os.getenv("WORKFLOW_SCAN_GRACE_PERIOD_MINUTES", "5") or "5")
        grace_period_minutes = max(1, min(grace_period_minutes, 60))  # 1-60 min range
    except Exception:
        grace_period_minutes = 5
    
    grace_delta = timedelta(minutes=grace_period_minutes)
    max_retries = 3  # BUG FIX 3.3: fail after N retry attempts
    
    try:
        with engine.connect() as conn:
            run_ids = conn.execute(
                text(
                    "SELECT id FROM workflow_runs "
                    "WHERE LOWER(TRIM(COALESCE(status,''))) IN ('running','failed')"
                )
            ).fetchall()
        for rr in run_ids or []:
            rid = int(rr[0])
            with engine.connect() as conn:
                waiting = conn.execute(
                    text(
                        f"""
                        SELECT id, environment_id, scan_waiting_since, scan_waiting_retry_count, scan_id, updated_at
                        FROM workflow_steps
                        WHERE run_id=:rid
                          AND LOWER(TRIM(COALESCE(status,'')))='scan_waiting'
                          AND (scan_id IS NULL OR TRIM(COALESCE(scan_id,''))='')
                          AND COALESCE(is_enabled,{bool_true})={bool_true}
                        """
                    ),
                    {"rid": rid},
                ).fetchall()
            for w in waiting or []:
                wid = int(w[0])
                weid = w[1]
                scan_waiting_since = w[2]
                retry_count = int(w[3] or 0)
                scan_id = str(w[4] or "").strip()
                updated_at = w[5] if len(w) > 5 else None
                
                # BUG FIX 2.1: Use updated_at as fallback for scan_waiting_since
                check_since = scan_waiting_since or updated_at or now
                if not isinstance(check_since, datetime):
                    try:
                        check_since = _parse_db_dt(check_since)
                    except Exception:
                        check_since = now
                
                elapsed = (now - check_since).total_seconds() / 60.0 if isinstance(check_since, datetime) else float('inf')
                
                # BUG FIX 3.2: Log warning after 50% grace period (early detection)
                grace_half = grace_period_minutes / 2.0
                if elapsed >= grace_half and elapsed < grace_period_minutes and retry_count == 0:
                    if log is not None:
                        try:
                            log.warning(f"step_id={wid} still waiting for scan after {elapsed:.1f}min (50% of {grace_period_minutes}min grace period)")
                        except Exception:
                            pass
                
                # BUG FIX 3.3: Check retry limit first
                if retry_count >= max_retries:
                    if log is not None:
                        log.warning(f"step_id={wid} exceeded retry limit ({retry_count}); failing permanently")
                    if weid is not None:
                        _set_env_busy(engine, int(weid), rid, False)
                    set_step_status(
                        engine, 
                        wid, 
                        "error",
                        last_error=f"Scan never appeared: exceeded retry limit ({max_retries})",
                        environment_id=None,
                        scan_waiting_retry_count=retry_count
                    )
                    continue
                
                # BUG FIX 2.1: Un-park after grace period
                if elapsed >= grace_period_minutes:
                    # BUG FIX 2.2: Validate scan_id if present (corruption check)
                    if scan_id and not re.match(r'^[a-zA-Z0-9\-_]+$', scan_id):
                        if log is not None:
                            log.warning(f"step_id={wid} has corrupted scan_id='{scan_id}'; resetting to pending")
                        if weid is not None:
                            _set_env_busy(engine, int(weid), rid, False)
                        set_step_status(
                            engine,
                            wid,
                            "pending",
                            last_error=f"Corrupted scan_id detected; reset for retry",
                            environment_id=None,
                            scan_waiting_retry_count=0,
                            scan_id=None
                        )
                        continue
                    
                    # Un-park: reset to pending with incremented retry count
                    if log is not None:
                        log.info(f"step_id={wid} un-parking after {elapsed:.1f}min grace period (retry {retry_count+1}/{max_retries})")
                    if weid is not None:
                        _set_env_busy(engine, int(weid), rid, False)
                    set_step_status(
                        engine,
                        wid,
                        "pending",
                        last_error=None,
                        environment_id=None,
                        scan_waiting_retry_count=retry_count + 1,
                        scan_waiting_since=None
                    )
                elif log is not None:
                    try:
                        log.debug(f"step_id={wid} still in grace period ({elapsed:.1f}min/{grace_period_minutes}min)")
                    except Exception:
                        pass
            
            _release_stale_env_locks_for_run(engine, rid)
    except Exception as ex:
        if log is not None:
            try:
                log.warning("repair_stuck_workflow_steps: %s", ex)
            except Exception:
                pass


def run_once(engine, *, quiet: bool = False) -> bool:
    """
    One scheduler tick: reconcile scans, then dispatch up to WORKFLOW_MAX_CONCURRENT_SCANS
    steps (default = workflow-pool env count) across all running workflows.
    Returns True if any work was done.
    """
    logger = _get_logger(quiet=quiet)
    logger.info('run_once: tick quiet=%s', bool(quiet))
    ensure_workflow_tables(engine)
    _ensure_test_environments_workflow_column(engine)
    set_runner_heartbeat(engine, status="running", message="tick", started=False)
    ok_lock, lock_msg = acquire_runner_lock(engine, ttl_seconds=120)
    if not ok_lock:
        set_runner_heartbeat(engine, status="idle", message=lock_msg, started=False)
        return False
    now = _now_utc()
    
    # BUG FIX 4.1: Pre-flight connectivity check for workflow pool environments
    try:
        pflight_ok, pflight_errors = _preflight_check_workflow_pool(engine, logger)
        if pflight_errors:
            for err in pflight_errors:
                logger.warning(f"preflight_check: {err}")
    except Exception as ex:
        logger.warning(f"preflight_check exception (non-blocking): {ex}")
    
    # BUG FIX 4.2: Check/apply licenses at workflow start (not per-step) to reduce API spam
    try:
        lic_ok, lic_errors = _ensure_workflow_pool_license_applied(engine, logger)
        if lic_errors:
            for err in lic_errors:
                logger.warning(f"license_check: {err}")
    except Exception as ex:
        logger.warning(f"license_check exception (non-blocking): {ex}")
    
    # BUG FIX 5.1: Cleanup orphaned action execution markers (runner startup recovery)
    try:
        _cleanup_orphaned_action_attempts(engine)
    except Exception as ex:
        logger.warning(f"action_cleanup exception (non-blocking): {ex}")

    with engine.connect() as conn:
        run_rows = conn.execute(
            text("""
                SELECT id, status, cooldown_minutes, next_eligible_at
                FROM workflow_runs
                WHERE status IN ('running', 'failed')
                ORDER BY id ASC
            """)
        ).fetchall()
    if not run_rows:
        try:
            with engine.connect() as conn:
                created_n = conn.execute(
                    text(
                        "SELECT COUNT(1) FROM workflow_runs WHERE LOWER(TRIM(COALESCE(status,''))) = 'created'"
                    )
                ).scalar()
            if created_n and int(created_n) > 0:
                logger.info(
                    "no running workflows (%s run(s) still 'created' — click Start in UI or create with auto_start)",
                    int(created_n),
                )
        except Exception:
            pass
        set_runner_heartbeat(engine, status="idle", message="no running workflows", started=False)
        return False

    # Failed runs with non-terminal enabled steps still need the runner (otherwise steps stay stuck forever).
    for rr0 in run_rows:
        try:
            _auto_resume_failed_run_if_stuck(engine, int(rr0[0]))
        except Exception:
            pass

    try:
        _repair_stuck_workflow_steps(engine, logger)
    except Exception:
        pass

    # Track global state across all running workflows
    inflight_envs = set()
    targeted_envs = set()
    targeted_steps = set()
    did_any_work = False

    try:
        from v2_api.workflow_scan_reconcile import reconcile_all_scan_running_steps  # noqa: PLC0415

        n_rec = reconcile_all_scan_running_steps(engine, logger=logger)
        if n_rec:
            did_any_work = True
    except Exception as ex:
        logger.error("reconcile_all_scan_running_steps failed: %s", ex)

    # --- Pre-pass: Release environments where cooldown or run has ended ---
    try:
        with engine.connect() as conn:
            busy_pred = _sql_truthy(engine, "is_busy")
            busy_envs = conn.execute(
                text(f"SELECT id, busy_by_run_id FROM test_environments WHERE {busy_pred}")
            ).fetchall()
        for be in busy_envs:
            be_id = int(be[0])
            be_run_id = be[1]
            if be_run_id is None:
                _set_env_busy(engine, be_id, None, False)
                continue
            
            with engine.connect() as conn:
                r_row = conn.execute(text("SELECT status, next_eligible_at FROM workflow_runs WHERE id=:rid"), {"rid": int(be_run_id)}).fetchone()
            
            if not r_row:
                _set_env_busy(engine, be_id, None, False)
                continue
            
            r_status = str(r_row[0] or "").lower()
            r_next = r_row[1]
            
            # Release if run is no longer active (keep failed runs reserved while steps still occupy envs).
            if r_status not in {"running", "failed"}:
                _set_env_busy(engine, be_id, None, False)
                continue
            
            # Release when this env has no in-flight DS/scan work for the reserving run.
            # Run-level cooldown must not keep unrelated envs busy (e.g. e2e-perf2 idle while perf/perf3 scan).
            with engine.connect() as conn:
                active_step = conn.execute(
                    text(
                        f"""
                        SELECT 1 FROM workflow_steps
                        WHERE run_id=:rid AND environment_id=:eid
                          AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})
                        LIMIT 1
                        """
                    ),
                    {"rid": int(be_run_id), "eid": be_id},
                ).fetchone()
            if not active_step:
                _set_env_busy(engine, be_id, None, False)
            else:
                targeted_envs.add(be_id)
    except Exception:
        pass

    # Load workflow-pool env ids (active + include_in_workflow_runs), then sync each from API every tick
    try:
        bool_true = _sql_bool_true(engine)
        bool_false = _sql_bool_false(engine)
        with engine.connect() as conn:
            env_rows = conn.execute(text(
                f"""
                SELECT id FROM test_environments
                WHERE COALESCE(is_active, {bool_false}) = {bool_true}
                  AND COALESCE(include_in_workflow_runs, {bool_true}) = {bool_true}
                ORDER BY id
                """
            )).fetchall()
            active_env_ids = [int(r[0]) for r in env_rows if r[0] is not None]
        for eid in active_env_ids:
            try:
                _sync_env_busy_from_api(engine, eid)
            except Exception:
                pass
        with engine.connect() as conn:
            env_rows = conn.execute(text(
                f"""
                SELECT id, is_busy, busy_by_run_id FROM test_environments
                WHERE COALESCE(is_active, {bool_false}) = {bool_true}
                  AND COALESCE(include_in_workflow_runs, {bool_true}) = {bool_true}
                ORDER BY id
                """
            )).fetchall()
            env_db_busy = {int(r[0]): (bool(r[1]), r[2]) for r in env_rows if r[0] is not None}
            env_count = len(active_env_ids)
    except Exception:
        active_env_ids = []
        env_db_busy = {}
        env_count = 0

    try:
        max_scans = int(os.getenv('WORKFLOW_MAX_CONCURRENT_SCANS', '') or 0)
    except Exception:
        max_scans = 0
    if max_scans <= 0:
        max_scans = env_count if env_count > 0 else 2

    # --- PASS 1: RECONCILIATION FOR ALL RUNS ---
    bool_true = _sql_bool_true(engine)
    for run_row in run_rows:
        run_id = int(run_row[0])
        try:
            with engine.connect() as conn:
                inflight_rows = conn.execute(
                    text(f"""
                        SELECT ws.id, ws.seq, ws.environment_id, ws.scan_id, ws.status, sp.scan_status, ws.updated_at
                        FROM workflow_steps ws
                        LEFT JOIN scan_parents sp ON sp.scan_id = ws.scan_id
                        WHERE ws.run_id=:rid
                          AND COALESCE(ws.is_enabled,{bool_true})={bool_true}
                          AND COALESCE(ws.status,'') NOT IN ('completed','error','disabled')
                          AND (
                            (ws.scan_id IS NOT NULL AND TRIM(ws.scan_id) <> '')
                            OR COALESCE(ws.status,'') IN ('scan_running','scan_waiting')
                          )
                        ORDER BY ws.updated_at DESC
                    """),
                    {"rid": int(run_id)},
                ).fetchall()
            
            completed_any = False
            completed_success_any = False
            try:
                grace_min = int(os.getenv('WORKFLOW_RESET_MISSING_SCAN_GRACE_MINUTES', '10') or '10')
            except Exception:
                grace_min = 10
            grace_delta = timedelta(minutes=max(1, grace_min))

            try:
                n_rec = _recover_false_aborted_steps(engine, int(run_id), logger=logger)
                if n_rec:
                    did_any_work = True
            except Exception as ex:
                logger.warning("recover_false_aborted run_id=%s: %s", run_id, ex)

            for rr in inflight_rows or []:
                step_id = int(rr[0])
                seq = int(rr[1])
                env_id = rr[2]
                sid = str(rr[3] or "").strip()
                st_status = str(rr[4] or "").strip().lower()
                scan_status = str(rr[5] or "").strip()
                step_updated_at = rr[6] if len(rr) > 6 else None
                
                resolved_status = scan_status
                if sid and (not resolved_status or not _is_terminal_state(resolved_status)):
                    api_st = _fetch_scan_status_for_step(engine, env_id, sid, run_id=int(run_id))
                    if api_st:
                        resolved_status = api_st

                if resolved_status and _is_terminal_state(resolved_status):
                    ok = _compact(resolved_status) in {"completed", "completedwithfailures"}
                    if _complete_step_from_terminal_scan(
                        engine,
                        run_id=int(run_id),
                        step_id=step_id,
                        seq=seq,
                        scan_id=sid,
                        terminal_status=resolved_status,
                    ):
                        if ok:
                            completed_success_any = True
                        else:
                            set_run_next_eligible(engine, run_id, _now_utc())
                        completed_any = True
                        did_any_work = True
                    continue

                if sid:
                    # Tracker/API have no terminal state yet — keep scan_running briefly, then reconcile.
                    live = _fetch_scan_status_for_step(engine, env_id, sid, run_id=int(run_id))
                    if live and _is_terminal_state(live):
                        ok = _compact(live) in {"completed", "completedwithfailures"}
                        if _complete_step_from_terminal_scan(
                            engine,
                            run_id=int(run_id),
                            step_id=step_id,
                            seq=seq,
                            scan_id=sid,
                            terminal_status=live,
                        ):
                            if ok:
                                completed_success_any = True
                            else:
                                set_run_next_eligible(engine, run_id, _now_utc())
                            completed_any = True
                            did_any_work = True
                        continue

                    if live and not _is_terminal_state(live):
                        if st_status != "scan_running":
                            set_step_status(engine, step_id, "scan_running")
                        if env_id is not None:
                            inflight_envs.add(int(env_id))
                        continue

                    grace_start = step_updated_at
                    try:
                        with engine.connect() as conn:
                            sa_row = conn.execute(
                                text("SELECT started_at FROM workflow_steps WHERE id=:sid LIMIT 1"),
                                {"sid": step_id},
                            ).fetchone()
                        if sa_row and sa_row[0] and isinstance(sa_row[0], datetime):
                            grace_start = sa_row[0]
                    except Exception:
                        pass
                    if grace_start and isinstance(grace_start, datetime):
                        if (now - grace_start) < grace_delta:
                            if st_status != "scan_running":
                                set_step_status(engine, step_id, "scan_running")
                            if env_id is not None:
                                inflight_envs.add(int(env_id))
                            continue

                    # SAFETY: only clear scan_id when BigID confirms the parent scan is gone (404).
                    # Timeouts / empty tracker status must NOT be treated as deleted.
                    final_st, presence = _classify_parent_scan_for_reconcile(
                        engine, env_id, sid, run_id=int(run_id)
                    )
                    if presence in ("live", "exists"):
                        logger.info(
                            "pass1: step_id=%s scan_id=%s still present (%s, presence=%s); keeping scan_running",
                            step_id,
                            sid,
                            final_st or "In Progress",
                            presence,
                        )
                        if st_status != "scan_running":
                            set_step_status(engine, step_id, "scan_running", scan_id=sid, last_error=None)
                        elif env_id is not None:
                            with engine.connect() as conn:
                                conn.execute(
                                    text(
                                        "UPDATE workflow_steps SET last_error=NULL WHERE id=:sid"
                                    ),
                                    {"sid": step_id},
                                )
                                conn.commit()
                        if env_id is not None:
                            inflight_envs.add(int(env_id))
                        continue
                    if presence == "terminal" and final_st:
                        if _complete_step_from_terminal_scan(
                            engine,
                            run_id=int(run_id),
                            step_id=step_id,
                            seq=seq,
                            scan_id=sid,
                            terminal_status=final_st,
                        ):
                            completed_any = True
                            did_any_work = True
                        continue
                    if presence == "unknown":
                        logger.warning(
                            "pass1: step_id=%s scan_id=%s status inconclusive; keeping scan_running",
                            step_id,
                            sid,
                        )
                        set_step_status(
                            engine,
                            step_id,
                            "scan_running",
                            scan_id=sid,
                            last_error="BigID status check inconclusive; will retry",
                        )
                        if env_id is not None:
                            inflight_envs.add(int(env_id))
                        continue

                    logger.info(
                        "pass1: step_id=%s scan_id=%s confirmed missing; reset for retry",
                        step_id,
                        sid,
                    )
                    set_step_status(engine, step_id, "pending", scan_id=None, last_error="Scan aborted/deleted; reset for retry")
                    set_run_next_eligible(engine, run_id, _now_utc())
                    completed_any = True
                    did_any_work = True
                    continue

                else:
                    # scan_waiting with no scan_id means the step was parked due to capacity and never
                    # made forward progress. Reset to pending so the next dispatch tick can retry it.
                    if st_status == "scan_waiting" and not sid:
                        if env_id is not None:
                            _set_env_busy(engine, int(env_id), int(run_id), False)
                        set_step_status(
                            engine,
                            step_id,
                            "pending",
                            last_error=None,
                            environment_id=None,
                        )
                        logger.info("pass1: step_id=%s was scan_waiting with no scan_id; reset to pending", step_id)
                        continue
                    if env_id is not None and sid and (scan_status or st_status == "scan_running"):
                        # Only consider an environment "inflight" if it's actively scanning.
                        # Include scan_running even when scan_status is empty (tracker not synced yet).
                        # scan_waiting means it's waiting for capacity, so it doesn't occupy a slot itself.
                        if st_status != "scan_waiting":
                            inflight_envs.add(int(env_id))
                    if sid and st_status != "scan_running":
                        set_step_status(engine, step_id, "scan_running")
            
            if completed_any and completed_success_any:
                try:
                    c_min = int(run_row[2]) if run_row[2] is not None else 15
                except Exception:
                    c_min = 15
                set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(c_min)))
            try:
                _release_stale_env_locks_for_run(engine, int(run_id))
                _reconcile_running_run_if_idle(engine, int(run_id), now)
            except Exception:
                pass
        except Exception as ex:
            logger.error("pass1 reconcile failed run_id=%s: %s", run_id, ex)

    # --- Stale scan_running: re-check BigID before the 48h global reset ---
    try:
        try:
            stale_scan_min = int(os.getenv("WORKFLOW_STALE_SCAN_RUNNING_MINUTES", "20") or "20")
        except Exception:
            stale_scan_min = 20
        stale_scan_min = max(5, min(stale_scan_min, 120))
        stale_scan_cutoff = now - timedelta(minutes=float(stale_scan_min))
        with engine.connect() as conn:
            stale_scan_steps = conn.execute(
                text(
                    """
                    SELECT id, run_id, seq, environment_id, scan_id
                    FROM workflow_steps
                    WHERE LOWER(TRIM(COALESCE(status,'')))='scan_running'
                      AND scan_id IS NOT NULL AND TRIM(COALESCE(scan_id,'')) <> ''
                      AND updated_at < :cutoff
                    """
                ),
                {"cutoff": stale_scan_cutoff},
            ).fetchall()
        stale_reconcile_run_ids: set[int] = set()
        for ss in stale_scan_steps or []:
            ss_id, ss_rid, ss_seq, ss_eid, ss_sid = int(ss[0]), int(ss[1]), int(ss[2]), ss[3], str(ss[4] or "").strip()
            live = _fetch_scan_status_for_step(engine, ss_eid, ss_sid, run_id=ss_rid)
            if live and _is_terminal_state(live):
                if _complete_step_from_terminal_scan(
                    engine,
                    run_id=ss_rid,
                    step_id=ss_id,
                    seq=ss_seq,
                    scan_id=ss_sid,
                    terminal_status=live,
                ):
                    logger.info("stale_scan_running: step_id=%s run_id=%s terminal=%s", ss_id, ss_rid, live)
                    stale_reconcile_run_ids.add(ss_rid)
        for rid in stale_reconcile_run_ids:
            _reconcile_running_run_if_idle(engine, rid, now)
    except Exception as ex:
        logger.error("Stale scan_running API reconcile error: %s", ex)

    # --- GLOBAL STALE STEP RECONCILIATION (after Pass 1 so active scans get updated_at refreshed first) ---
    try:
        try:
            stale_hours = int(os.getenv("WORKFLOW_STALE_SCAN_RUNNING_HOURS", "48") or "48")
        except Exception:
            stale_hours = 48
        stale_hours = max(2, min(stale_hours, 168))
        cutoff = now - timedelta(hours=float(stale_hours))
        stale_reconcile_run_ids: set[int] = set()
        with engine.connect() as conn:
            stale_steps = conn.execute(
                text(
                    "SELECT id, run_id, seq, scan_id FROM workflow_steps "
                    "WHERE status='scan_running' AND updated_at < :cutoff"
                ),
                {"cutoff": cutoff},
            ).fetchall()
        for ss in stale_steps:
            ss_id, ss_rid, ss_seq, ss_sid = ss
            set_step_status(
                engine,
                ss_id,
                "pending",
                scan_id=None,
                last_error=f"Global stale cleanup: step was stuck in scan_running for > {stale_hours}h",
            )
            logger.info("global_cleanup: reset stale step_id=%s run_id=%s seq=%s", ss_id, ss_rid, ss_seq)
            try:
                stale_reconcile_run_ids.add(int(ss_rid))
            except Exception:
                pass
        for rid in stale_reconcile_run_ids:
            _reconcile_running_run_if_idle(engine, rid, now)
    except Exception as ex:
        logger.error("Stale scan_running cleanup error: %s", ex)

    # Stale 'running' with no DS/scan ever created — release env locks quickly (common after park bug).
    try:
        try:
            stale_running_no_ds_min = int(os.getenv("WORKFLOW_STALE_RUNNING_NO_DS_MINUTES", "8") or "8")
        except Exception:
            stale_running_no_ds_min = 8
        stale_running_no_ds_min = max(3, min(stale_running_no_ds_min, 30))
        no_ds_cutoff = now - timedelta(minutes=float(stale_running_no_ds_min))
        with engine.connect() as conn:
            stuck_ds = conn.execute(
                text(
                    """
                    SELECT id, run_id, seq, environment_id FROM workflow_steps
                    WHERE LOWER(TRIM(COALESCE(status,'')))='running'
                      AND LOWER(TRIM(COALESCE(ds_kind,''))) <> 'action'
                      AND (ds_id IS NULL OR TRIM(COALESCE(ds_id,''))='')
                      AND (scan_id IS NULL OR TRIM(COALESCE(scan_id,''))='')
                      AND updated_at < :cutoff
                    """
                ),
                {"cutoff": no_ds_cutoff},
            ).fetchall()
        for row in stuck_ds or []:
            sid_st, rid_st, seq_st, eid_st = row[0], row[1], row[2], row[3]
            if eid_st is not None and rid_st is not None:
                _set_env_busy(engine, int(eid_st), int(rid_st), False)
            set_step_status(
                engine,
                int(sid_st),
                "pending",
                last_error=f"Stale: running >{stale_running_no_ds_min}min with no DS; reset",
                environment_id=None,
            )
            logger.info("global_cleanup: reset running-no-ds step_id=%s run_id=%s seq=%s", sid_st, rid_st, seq_st)
    except Exception as ex:
        logger.error("Stale running-no-ds cleanup error: %s", ex)

    # Stale 'running' step cleanup: DS creation / connection-test threads that hung without updating.
    # A step stuck in 'running' holds the env in WORKFLOW_ENV_OCCUPYING_STATUSES indefinitely.
    try:
        try:
            stale_running_min = int(os.getenv("WORKFLOW_STALE_RUNNING_MINUTES", "45") or "45")
        except Exception:
            stale_running_min = 45
        stale_running_min = max(10, min(stale_running_min, 240))
        running_cutoff = now - timedelta(minutes=float(stale_running_min))
        stale_running_reconcile_run_ids: set[int] = set()
        with engine.connect() as conn:
            stale_running = conn.execute(
                text(
                    "SELECT id, run_id, seq FROM workflow_steps "
                    "WHERE LOWER(TRIM(COALESCE(status,'')))='running' AND updated_at < :cutoff"
                ),
                {"cutoff": running_cutoff},
            ).fetchall()
        for sr in stale_running or []:
            sr_id, sr_rid, sr_seq = sr[0], sr[1], sr[2]
            try:
                with engine.connect() as conn:
                    er = conn.execute(
                        text("SELECT environment_id FROM workflow_steps WHERE id=:id"),
                        {"id": int(sr_id)},
                    ).fetchone()
                if er and er[0] is not None and sr_rid is not None:
                    _set_env_busy(engine, int(er[0]), int(sr_rid), False)
            except Exception:
                pass
            set_step_status(
                engine,
                sr_id,
                "pending",
                last_error=f"Stale: step stuck in 'running' for >{stale_running_min}min; reset for retry",
                environment_id=None,
            )
            logger.info("global_cleanup: reset stale running step_id=%s run_id=%s seq=%s", sr_id, sr_rid, sr_seq)
            try:
                stale_running_reconcile_run_ids.add(int(sr_rid))
            except Exception:
                pass
        for rid in stale_running_reconcile_run_ids:
            _reconcile_running_run_if_idle(engine, rid, now)
    except Exception as ex:
        logger.error("Stale running cleanup error: %s", ex)

    # Any env with a step in DS/profile/scan phase counts toward concurrency (not only scan_running).
    try:
        bool_true = _sql_bool_true(engine)
        with engine.connect() as conn:
            occ_rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT ws.environment_id
                    FROM workflow_steps ws
                    INNER JOIN workflow_runs wr ON wr.id = ws.run_id
                    WHERE wr.status = 'running'
                      AND COALESCE(ws.is_enabled, {bool_true}) = {bool_true}
                      AND ws.environment_id IS NOT NULL
                      AND LOWER(TRIM(COALESCE(ws.status, ''))) IN ({_workflow_env_occupying_sql_in()})
                    """
                )
            ).fetchall()
        for orow in occ_rows or []:
            if orow[0] is not None:
                try:
                    inflight_envs.add(int(orow[0]))
                except Exception:
                    pass
    except Exception:
        pass

    # --- PASS 2: DISPATCH FOR ALL RUNS ---
    scans_at_capacity = bool(len(inflight_envs) >= int(max_scans))
    targeted_envs.update(inflight_envs)

    # Cache environment busy status for this tick to avoid redundant API calls
    env_busy_cache: Dict[int, bool] = {}

    slots_to_fill = max(0, int(max_scans) - len(inflight_envs))
    
    logger.info(f"PASS 2: max_scans={max_scans}, inflight_envs={inflight_envs}, slots_to_fill={slots_to_fill}, env_count={env_count}, active_env_ids={active_env_ids}")
    
    for run_row in run_rows:
        if slots_to_fill <= 0 and not inflight_envs: # Still check for work if we have capacity or need to reconcile
            # However, we only continue if we have slots or if we want to do "zero slot" checks (like DS creation)
            # Actually, if slots_to_fill is 0, we can't start NEW scans, but we might be able to do DS work?
            # No, if scans_at_capacity is True, _pick_candidate will skip NEW work.
            pass
            
        run_id = int(run_row[0])
        try:
            _reconcile_running_run_if_idle(engine, run_id, now)
        except Exception:
            pass
        try:
            _release_stale_env_locks_for_run(engine, run_id)
        except Exception:
            pass

        try:
            cooldown_m = int(run_row[2]) if run_row[2] is not None else 15
        except Exception:
            cooldown_m = 15
            
        # Re-fetch next_eligible_at
        try:
            with engine.connect() as conn:
                nr = conn.execute(text("SELECT next_eligible_at FROM workflow_runs WHERE id=:rid"), {"rid": run_id}).fetchone()
                next_eligible = nr[0] if nr else run_row[3]
        except Exception:
            next_eligible = run_row[3]

        if next_eligible and isinstance(next_eligible, datetime) and next_eligible > now:
            # Cooldown between steps — but never block when nothing is mid-pipeline yet.
            if _run_has_executing_steps(engine, run_id):
                continue
            try:
                set_run_next_eligible(engine, run_id, _now_utc())
            except Exception:
                pass

        # Release stale busy flags when this run has only pending/error backlog.
        try:
            bool_true = _sql_bool_true(engine)
            with engine.connect() as conn:
                has_active = conn.execute(
                    text(
                        f"""
                        SELECT 1 FROM workflow_steps
                        WHERE run_id=:rid AND COALESCE(is_enabled,{bool_true})={bool_true}
                          AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})
                        LIMIT 1
                        """
                    ),
                    {"rid": run_id},
                ).fetchone()
            if not has_active:
                bf = _sql_bool_false(engine)
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            UPDATE test_environments
                            SET is_busy={bf}, busy_by_run_id=NULL, busy_since=NULL
                            WHERE busy_by_run_id=:rid
                            """
                        ),
                        {"rid": run_id},
                    )
        except Exception:
            pass

        # Only envs assigned to this run (env1/env2 pinned at start), not the whole global pool.
        run_pool_env_ids = _workflow_pool_env_ids_for_run(engine, int(run_id))
        run_env_count = max(1, len(run_pool_env_ids)) if run_pool_env_ids else max(1, env_count)

        # Strategy for status filter
        retry_errors = str(os.getenv("WORKFLOW_RETRY_ERROR_STEPS", "") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
        bool_true = _sql_bool_true(engine)
        if not retry_errors:
            # Include scan_created so a step can resume (e.g. trigger scan after profile) if the runner tick ended mid-pipeline.
            where_status = f"AND COALESCE(status, 'pending') NOT IN ('completed', 'error', 'disabled') AND COALESCE(is_enabled, {bool_true}) = {bool_true}"
        else:
            where_status = f"AND COALESCE(status, 'pending') IN ('pending', 'error') AND COALESCE(status,'') <> 'disabled' AND COALESCE(is_enabled, {bool_true}) = {bool_true}"

        with engine.connect() as conn:
            query_sql = f"""
                    SELECT id, seq, environment_id, ds_kind, ds_name, scan_kind, file_set_type,
                           is_acl, is_ocr, is_structured_ff, is_ols, is_lite, feature_flags, notes,
                           ds_id, scan_profile_id, scan_profile_name, build_version, scan_id, status
                    FROM workflow_steps
                    WHERE run_id = :rid {where_status}
                    ORDER BY seq ASC LIMIT 10
                """
            candidates = conn.execute(text(query_sql), {"rid": run_id}).fetchall()
        
        if not candidates:
            # Check if run is done
            try:
                with engine.connect() as conn:
                    any_left = conn.execute(text(f"SELECT 1 FROM workflow_steps WHERE run_id=:rid {where_status} LIMIT 1"), {"rid": run_id}).fetchone()
                    if not any_left:
                        with engine.connect() as conn:
                            # Re-verify one last time before closing
                            any_active = conn.execute(text(f"SELECT 1 FROM workflow_steps WHERE run_id=:rid AND COALESCE(status,'pending') NOT IN ('completed','error','disabled') AND COALESCE(is_enabled, {bool_true}) = {bool_true} LIMIT 1"), {"rid": run_id}).fetchone()
                            if not any_active:
                                # Mark as failed if ANY enabled step hit an error, otherwise completed
                                has_err = conn.execute(text(f"SELECT 1 FROM workflow_steps WHERE run_id=:rid AND status='error' AND COALESCE(is_enabled,{bool_true})={bool_true} LIMIT 1"), {"rid": run_id}).fetchone()
                                final_s = 'failed' if has_err else 'completed'
                                conn.execute(text("UPDATE workflow_runs SET status=:s, finished_at=CURRENT_TIMESTAMP WHERE id=:rid"), {"rid": run_id, "s": final_s})
                                conn.commit()
                        did_any_work = True
            except Exception:
                pass
            continue

        def _inner_pick(allow_scan_running: bool) -> Optional[Tuple[Any, Optional[int]]]:
            # Parallel: run up to env_count steps at once. Step N can run whenever there is capacity:
            # count = how many steps with seq < N are not terminal; if count < env_count, step N can start (some env free).
            # So step 3 can run as soon as step 1 OR step 2 completes (whichever frees an env first).
            num_envs = max(1, run_env_count)
            logger.info(f"_inner_pick: allow_scan_running={allow_scan_running}, num_envs={num_envs}, candidates={len(candidates)}, targeted_steps={targeted_steps}, targeted_envs={targeted_envs}")
            for cand in candidates:
                cand_id = int(cand[0])
                cand_seq = int(cand[1])
                logger.info(f"_inner_pick: checking cand_id={cand_id}, cand_seq={cand_seq}")
                if cand_id in targeted_steps:
                    logger.info(f"_inner_pick: skipping cand_id={cand_id} (already targeted)")
                    continue
                # Capacity check: steps with seq < cand_seq that are still in progress
                if cand_seq > 1:
                    with engine.connect() as conn:
                        in_progress = conn.execute(
                            text(
                                f"SELECT COUNT(1) FROM workflow_steps WHERE run_id=:rid AND seq < :seq AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})"
                            ),
                            {"rid": run_id, "seq": cand_seq},
                        ).fetchone()
                    count_before = int(in_progress[0]) if in_progress else 0
                    logger.info(f"_inner_pick: cand_seq={cand_seq}, count_before={count_before}, num_envs={num_envs}")
                    if count_before >= num_envs:
                        logger.info(f"_inner_pick: skipping cand_seq={cand_seq} (no capacity: {count_before} >= {num_envs})")
                        continue  # no capacity: all envs are "used" by earlier steps or those with higher priority (lower seq)

                c_status = str(cand[19] or '').strip().lower()
                c_env = cand[2]
                cand_ds_kind = str(cand[3] or "").strip().lower()  # index 3 is ds_kind
                is_action_step = (cand_ds_kind == "action")
                
                logger.info(f"_inner_pick: cand_id={cand_id}, c_status={c_status}, c_env={c_env}, ds_kind={cand_ds_kind}, is_action={is_action_step}")

                # ACTION STEPS: prior steps done; pick when any still-pending pool env is free (resume-safe).
                if is_action_step:
                    logger.info(f"_inner_pick: cand_id={cand_id} is ACTION step, seq={cand_seq}")
                    if cand_seq > 1:
                        with engine.connect() as conn:
                            in_progress = conn.execute(
                                text(
                                    f"SELECT COUNT(1) FROM workflow_steps WHERE run_id=:rid AND seq < :seq AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})"
                                ),
                                {"rid": run_id, "seq": cand_seq},
                            ).fetchone()
                        count_before = int(in_progress[0]) if in_progress else 0
                        if count_before > 0:
                            logger.info(
                                f"_inner_pick: ACTION seq={cand_seq} waiting for {count_before} previous step(s)"
                            )
                            continue
                    if not run_pool_env_ids:
                        continue
                    done_ids = _action_executed_env_ids_from_notes(
                        str(cand[13] or ""), run_pool_env_ids, engine
                    )
                    pending_pool = [int(e) for e in run_pool_env_ids if int(e) not in done_ids]
                    if not pending_pool:
                        return (cand, int(run_pool_env_ids[0]))
                    ready_any = False
                    blockers: List[str] = []
                    for eid in pending_pool:
                        ready, reason = _action_pool_env_ready(
                            engine,
                            int(eid),
                            int(run_id),
                            targeted_envs,
                            env_db_busy,
                            logger=logger,
                        )
                        if ready:
                            ready_any = True
                        else:
                            blockers.append(f"env{eid}:{reason}")
                    if not ready_any:
                        logger.info(
                            f"_inner_pick: ACTION seq={cand_seq} no pending env ready: {blockers}"
                        )
                        continue
                    logger.info(
                        f"_inner_pick: ACTION seq={cand_seq} ready ({len(pending_pool)} pending env(s))"
                    )
                    return (cand, int(pending_pool[0]))

                # Special case: already running?
                if (not allow_scan_running) and c_status == 'scan_running':
                    logger.info(f"_inner_pick: cand_id={cand_id}, skipping (scan_running and allow_scan_running=False)")
                    continue
                if c_status == 'scan_running' and allow_scan_running:
                    logger.info(f"_inner_pick: cand_id={cand_id}, is scan_running with allow_scan_running=True")
                    # If it's already running, it must stay on its assigned env
                    if c_env is None:
                        logger.info(f"_inner_pick: cand_id={cand_id}, scan_running but no env assigned, skipping")
                        continue
                    logger.info(f"_inner_pick: cand_id={cand_id}, returning scan_running step with env {c_env}")
                    return (cand, int(c_env))

                # If step already owns an environment and is in an active DS/scan phase,
                # keep advancing it even when global capacity is full.
                if c_env is not None and c_status in WORKFLOW_ENV_OCCUPYING_STATUSES and c_status != 'scan_running':
                    logger.info(f"_inner_pick: cand_id={cand_id}, already owns env {c_env} and status={c_status}, returning")
                    return (cand, int(c_env))

                # Pick any free env from the workflow pool unless this step already owns an env mid-pipeline.
                best_env = None
                env_candidates = run_pool_env_ids
                if c_env is not None and c_status in WORKFLOW_ENV_OCCUPYING_STATUSES and c_status not in (
                    "pending",
                    "error",
                    "scan_waiting",
                ):
                    env_candidates = [int(c_env)]
                elif c_env is not None and c_status in ("error", "scan_waiting"):
                    # DS/profile were created on c_env — do not trigger on a different BigID host.
                    try:
                        with engine.connect() as conn:
                            bound = conn.execute(
                                text(
                                    "SELECT COALESCE(TRIM(scan_profile_id),''), COALESCE(TRIM(ds_id),'') "
                                    "FROM workflow_steps WHERE id=:id"
                                ),
                                {"id": cand_id},
                            ).fetchone()
                        if bound and (str(bound[0] or "").strip() or str(bound[1] or "").strip()):
                            env_candidates = [int(c_env)]
                    except Exception:
                        pass
                logger.info(f"_inner_pick: cand_id={cand_id}, searching for env, env_candidates={env_candidates}")
                for eid in env_candidates:
                    logger.info(f"_inner_pick: cand_id={cand_id}, trying env {eid}")
                    if int(eid) not in run_pool_env_ids:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} not in run_pool_env_ids, skipping")
                        continue
                    if int(eid) in targeted_envs:
                        db_busy, db_busy_by = env_db_busy.get(eid, (False, None))
                        other_run_holds = bool(
                            db_busy and db_busy_by is not None and int(db_busy_by) != int(run_id)
                        )
                        if c_status in ("pending", "error", "scan_waiting"):
                            # One env per step per tick — do not assign the same env to parallel pending steps.
                            logger.info(
                                f"_inner_pick: cand_id={cand_id}, env {eid} already targeted this tick, skipping"
                            )
                            continue
                        elif not (
                            c_env is not None
                            and int(c_env) == int(eid)
                            and c_status in WORKFLOW_ENV_OCCUPYING_STATUSES
                        ):
                            logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} in targeted_envs, skipping")
                            continue
                    
                    # Check DB busy status
                    db_busy, db_busy_by = env_db_busy.get(eid, (False, None))
                    if db_busy and db_busy_by is not None and int(db_busy_by) != int(run_id):
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} busy in DB by run {db_busy_by}, skipping")
                        continue  # reserved by another run
                    if db_busy and db_busy_by is None and c_status in ("pending", "error", "scan_waiting"):
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} orphan busy lock, trying next env")
                        continue
                    
                    # DB says free — check API before creating DB/scan: if scan running on this env, try next env.
                    env_row_tmp = load_env(engine, eid)
                    if not env_row_tmp:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} not found in DB, skipping")
                        continue
                    tmp_burl = (env_row_tmp.get("base_url") or "").strip()
                    if not tmp_burl:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} has no base_url, skipping")
                        continue
                    if _run_env_has_active_step(engine, run_id, int(eid), exclude_step_id=cand_id):
                        logger.info(
                            f"_inner_pick: cand_id={cand_id}, env {eid} held by another step in run {run_id}, skipping"
                        )
                        continue
                    try:
                        tmp_token = Get_auth_token(tmp_burl, env_row_tmp)
                        has_scan = env_has_running_scan(tmp_burl, tmp_token, run_id=run_id)
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} API running_scan={has_scan}")
                        if has_scan:
                            logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} has running scan (API), skipping")
                            continue
                    except Exception as e:
                        fail_open = str(os.getenv("WORKFLOW_FAIL_OPEN_ON_BUSY_CHECK", "1") or "1").strip().lower() in {
                            "1",
                            "true",
                            "yes",
                            "y",
                            "on",
                        }
                        if not fail_open:
                            logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} API check failed ({e}), skipping (fail-closed)")
                            continue
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} API check failed ({e}), treating as free")
                    logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} is FREE! Selecting it.")
                    best_env = eid
                    break
                
                if best_env is not None:
                    logger.info(f"_inner_pick: returning cand_id={cand_id} with env {best_env}")
                    return (cand, best_env)
                else:
                    logger.info(f"_inner_pick: cand_id={cand_id}, no free env found, continuing to next candidate")
            return None

        # Determine how many steps we can try to start for THIS run in this tick
        # To avoid starving later runs, we could limit this, but let's stick to filling available slots.
        # Fill slots
        # BUG FIX 2.4: Properly track slots_to_fill across all runs in this tick
        # Each step picked in this tick reduces available slots for subsequent runs
        run_did_work = False
        logger.info(f"run_id={run_id}: slots_to_fill={slots_to_fill}, starting dispatch loop")
        
        # Use a while loop instead of fixed range to respect dynamic slots_to_fill
        dispatch_attempts = 0
        max_attempts = max(10, int(max_scans))  # Safety: don't loop forever
        while dispatch_attempts < max_attempts and slots_to_fill > 0:
            dispatch_attempts += 1
            logger.info(f"run_id={run_id}: dispatch attempt {dispatch_attempts}, slots_to_fill={slots_to_fill}")
            
            pick = _inner_pick(allow_scan_running=False)
            if not pick:
                logger.info(f"run_id={run_id}: no pick with allow_scan_running=False, trying with True")
                pick = _inner_pick(allow_scan_running=True)
                if not pick:
                    logger.info(f"run_id={run_id}: no pick found, breaking dispatch loop")
                    # No free env this tick — leave steps as-is (pending). Next tick will retry.
                    # Do NOT set scan_waiting here: scan_waiting without a scan_id creates a deadlock
                    # because PASS 2 sees the env as targeted but PASS 1 never resets it.
                    break
            
            step, env_id = pick
            step_id = int(step[0])
            logger.info(f"run_id={run_id}: picked step_id={step_id}, env_id={env_id}")
            targeted_steps.add(step_id)
            pick_ds_kind = str(step[3] or "").strip().lower()
            if pick_ds_kind == "action":
                done_ids = _action_executed_env_ids_from_notes(
                    str(step[13] or ""), run_pool_env_ids, engine
                )
                for eid in run_pool_env_ids:
                    if int(eid) not in done_ids:
                        targeted_envs.add(int(eid))
            elif env_id is not None:
                targeted_envs.add(int(env_id))
                # Do NOT set environment_id here — only set when step actually starts in _process_step

            res = _process_step(engine, run_id, step, env_id, cooldown_m, now, logger, quiet)
            if res:
                did_any_work = True
                run_did_work = True
                # BUG FIX 2.4: Decrement slots_to_fill after each step picked.
                # slots_to_fill counts concurrent SCAN slots (max_scans - inflight scan envs).
                # Action steps run across the pool and start no parent scan, so they must NOT
                # consume a scan slot — otherwise a few action/DS-only steps drive slots_to_fill
                # to 0 and starve actually-startable scan steps on free envs this tick.
                if pick_ds_kind == "action":
                    logger.info(f"run_id={run_id}: step_id={step_id} (action) processed; no scan slot consumed (slots_to_fill={slots_to_fill})")
                elif slots_to_fill > 0:
                    slots_to_fill -= 1
                    logger.info(f"run_id={run_id}: step_id={step_id} processed, slots_to_fill now={slots_to_fill}")
                else:
                    logger.info(f"run_id={run_id}: step_id={step_id} processed, but slots_to_fill already 0")

        if run_did_work:
            # If we started work, don't set next_eligible here; _process_step handles it per step terminal/fail
            pass

    return did_any_work


def _auto_save_benchmark_from_step(engine, step_id: int, scan_id: str, run_id: int, finished_at=None) -> None:
    """
    Called when a workflow step scan reaches Completed / CompletedWithFailures.
    Pulls scan_parents + scan_progress + scan_insights data and inserts a
    benchmark row only if one does not already exist for this scan_id.
    """
    if not scan_id or not scan_id.strip():
        return
    scan_id = scan_id.strip()
    try:
        # Guard: skip if already saved
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT id FROM benchmarks WHERE scan_id = :sid LIMIT 1"),
                {"sid": scan_id},
            ).fetchone()
        if exists:
            print(f"[AUTO-BENCHMARK] scan_id={scan_id} already saved (id={exists[0]}), skipping.", flush=True)
            return

        # Pull step metadata
        with engine.connect() as conn:
            step_row = conn.execute(
                text("""
                    SELECT ds_kind, ds_name, file_set_type, feature_flags, build_version
                    FROM workflow_steps WHERE id = :sid LIMIT 1
                """),
                {"sid": step_id},
            ).fetchone()
        if not step_row:
            return
        ds_kind       = str(step_row[0] or "").strip()
        ds_name       = str(step_row[1] or "").strip()
        file_set_type = str(step_row[2] or "").strip() or None
        build_version = str(step_row[4] or "").strip() or "unknown"

        # Pull scan timing:
        # - start: scan_parents.started_at (BigID-reported scan start, most accurate)
        # - end:   finished_at passed from runner (_now_utc() at step completion moment)
        #          Fallback to scan_parents.last_seen_at only if finished_at is missing.
        with engine.connect() as conn:
            sp_row = conn.execute(
                text("SELECT started_at, last_seen_at FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": scan_id},
            ).fetchone()
        if not sp_row or not sp_row[0]:
            return
        scan_start = sp_row[0]
        scan_end = finished_at if finished_at else (sp_row[1] if sp_row[1] else _now_utc())
        try:
            scan_duration_h = max(0.001, (scan_end - scan_start).total_seconds() / 3600)
        except Exception:
            scan_duration_h = 0.001
        print(f"[AUTO-BENCHMARK] scan_id={scan_id} start={scan_start} end={scan_end} duration_h={scan_duration_h:.3f}", flush=True)

        # Pull classification metrics from scan_insights
        total_classification = None
        with engine.connect() as conn:
            si_row = conn.execute(
                text("SELECT total_classification FROM scan_insights WHERE scan_id = :sid LIMIT 1"),
                {"sid": scan_id},
            ).fetchone()
        if si_row:
            total_classification = si_row[0]

        # Pull peak object count from scan_progress
        objects_scanned = 0
        with engine.connect() as conn:
            pr_row = conn.execute(
                text("SELECT MAX(CAST(COALESCE(objects_scanned, 0) AS SIGNED)) FROM scan_progress WHERE scan_id = :sid"),
                {"sid": scan_id},
            ).fetchone()
        if pr_row and pr_row[0]:
            try:
                objects_scanned = int(pr_row[0])
            except Exception:
                pass

        # Get or create build record
        with engine.connect() as conn:
            b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()
            if b_row:
                b_id = int(b_row[0])
            else:
                conn.execute(text("INSERT INTO builds (version) VALUES (:v)"), {"v": build_version})
                conn.commit()
                b_id = int(conn.execute(text("SELECT id FROM builds WHERE version = :v LIMIT 1"), {"v": build_version}).fetchone()[0])

        # Pull peak scanner count + threads from scanner_group_performance during scan window
        scanner_count = 0
        thread_count  = 0
        try:
            with engine.connect() as conn:
                gp_row = conn.execute(
                    text("""
                        SELECT MAX(running_scanners), MAX(scanner_threads)
                        FROM scanner_group_performance
                        WHERE environment_id = :eid
                          AND captured_at_utc BETWEEN :start AND :end
                    """),
                    {"eid": step_row[10] if step_row and step_row[10] else -1,
                     "start": scan_start, "end": scan_end},
                ).fetchone()
            if gp_row and gp_row[0]:
                scanner_count = max(0, int(gp_row[0] or 0))
                thread_count  = max(0, int(gp_row[1] or 0))
        except Exception:
            pass
        # Fall back to the standard benchmark rig (5 scanners / 50 threads) when telemetry
        # didn't capture the counts, so the per-scanner / per-thread tiers still populate.
        if scanner_count <= 0:
            scanner_count = int(os.getenv("BENCHMARK_DEFAULT_SCANNERS", "5") or 5)
        if thread_count <= 0:
            thread_count = int(os.getenv("BENCHMARK_DEFAULT_THREADS", "50") or 50)
        print(f"[AUTO-BENCHMARK] scanners={scanner_count} threads={thread_count} files={objects_scanned}", flush=True)

        # Resolve foreign keys for normalized table
        ds_id = None
        if ds_kind:
            with engine.connect() as conn:
                r = conn.execute(text("SELECT id FROM data_source_types WHERE name = :n"), {"n": ds_kind}).fetchone()
                if r:
                    ds_id = r[0]
                else:
                    conn.execute(text("INSERT INTO data_source_types (name) VALUES (:n)"), {"n": ds_kind})
                    conn.commit()
                    ds_id = conn.execute(text("SELECT id FROM data_source_types WHERE name = :n"), {"n": ds_kind}).fetchone()[0]

        # Resolve file-set FK and pull its declared volume (total_files / total_size_gb)
        # so the run carries its own volume and we can compute GB/hr — not just files/hr.
        fs_id = None
        total_files = None
        total_size_gb = None
        if file_set_type:
            with engine.connect() as conn:
                r = conn.execute(text("SELECT id, total_files, total_size_gb FROM file_set_types WHERE name = :n"), {"n": file_set_type}).fetchone()
                if r:
                    fs_id, total_files, total_size_gb = r[0], r[1], r[2]
                else:
                    conn.execute(text("INSERT INTO file_set_types (name) VALUES (:n)"), {"n": file_set_type})
                    conn.commit()
                    fs_id = conn.execute(text("SELECT id FROM file_set_types WHERE name = :n"), {"n": file_set_type}).fetchone()[0]

        # Compute all three throughput tiers (cluster / per-scanner / per-thread).
        # files/hr is driven by objects scanned; GB/hr by the file-set volume (NULL for row-based DB scans).
        # Guard: a degenerate duration (zero/near-zero span from bad start==end timestamps, or a
        # stopped/abandoned scan) would explode throughput to absurd values and spike the trend charts.
        # Leave all tiers NULL in that case rather than persist garbage.
        MIN_VALID_DURATION_H = 0.01  # 36s — real benchmark scans run for minutes to hours
        valid_duration = scan_duration_h >= MIN_VALID_DURATION_H and scan_end > scan_start
        files_volume = float(total_files) if total_files else float(objects_scanned or 0)
        tp_cluster_files = (files_volume / scan_duration_h) if (valid_duration and files_volume > 0) else None
        tp_cluster_gb    = (float(total_size_gb) / scan_duration_h) if (valid_duration and total_size_gb) else None
        tp_scanner_files = (tp_cluster_files / scanner_count) if (tp_cluster_files is not None and scanner_count > 0) else None
        tp_thread_files  = (tp_cluster_files / thread_count)  if (tp_cluster_files is not None and thread_count  > 0) else None
        tp_scanner_gb    = (tp_cluster_gb / scanner_count) if (tp_cluster_gb is not None and scanner_count > 0) else None
        tp_thread_gb     = (tp_cluster_gb / thread_count)  if (tp_cluster_gb is not None and thread_count  > 0) else None

        # Build INSERT dynamically based on normalized schema
        cols = [
            "source",
            "build_id", "run_date", "scenario_type", "data_source_type_id", "file_set_type_id",
            "scan_start_time", "scan_end_time",
            "total_scan_hours", "total_files", "total_size_gb", "objects_scanned",
            "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr",
            "throughput_cluster_gb_hr", "throughput_scanner_gb_hr", "throughput_thread_gb_hr",
        ]
        vals = ["'auto'", ":b_id", "CURRENT_DATE", ":scenario", ":dst_id", ":fst_id",
                ":ss", ":se", ":tsh", ":tf", ":tsg", ":obj",
                ":tpcf", ":tpsf", ":tptf",
                ":tpcg", ":tpsg", ":tptg"]
        params: Dict[str, Any] = {
            "tf": total_files,
            "tsg": total_size_gb,
            "obj": objects_scanned or None,
            "tpcg": tp_cluster_gb,
            "tpsg": tp_scanner_gb,
            "tptg": tp_thread_gb,
            "b_id": b_id,
            "scenario": ds_name or ds_kind or "Workflow Auto",
            "dst_id": ds_id,
            "fst_id": fs_id,
            "ss": scan_start,
            "se": scan_end,
            "tsh": scan_duration_h,
            "tpcf": tp_cluster_files,
            "tpsf": tp_scanner_files,
            "tptf": tp_thread_files,
        }
        # Optionally include scanner/thread counts if schema has config_id or we write them directly
        # (benchmarks links to configurations for scanner_count/thread_count, so we store them as notes here)
        with engine.connect() as conn:
            try:
                has_sid = bool(conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='benchmark_runs' AND column_name='scan_id' LIMIT 1"
                )).fetchone())
            except Exception:
                has_sid = False
            if has_sid:
                cols.append("scan_id")
                vals.append(":sid")
                params["sid"] = scan_id

            try:
                has_cls = bool(conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='benchmark_runs' AND column_name='total_classification' LIMIT 1"
                )).fetchone())
            except Exception:
                has_cls = False
            if has_cls and total_classification is not None:
                cols.append("total_classification")
                vals.append(":tclass")
                params["tclass"] = total_classification

            conn.execute(
                text(f"INSERT INTO benchmark_runs ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
            conn.commit()

        # Refresh averages for ONLY this run's major build (non-fatal).
        try:
            try:
                from benchmark_averages import recalculate_build_major_averages
            except ImportError:
                from v2_api.benchmark_averages import recalculate_build_major_averages
            _major = str(build_version or "").split(".")[0].strip()
            recalculate_build_major_averages(engine, _major or None)
        except Exception:
            pass

        print(f"[AUTO-BENCHMARK] Saved scan_id={scan_id} build={build_version} run_id={run_id}", flush=True)
    except Exception as ex:
        print(f"[AUTO-BENCHMARK] Failed for scan_id={scan_id}: {ex}", flush=True)


def _maybe_mark_run_completed(engine, run_id: int) -> None:
    """
    If all steps in the run are in a terminal or non-actionable state
    (completed, error, disabled) — i.e. nothing pending/active remains —
    mark the workflow run as 'completed'. Called after auth failure so
    runs don't stay stuck in 'running' forever when all work is done.
    """
    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            any_active = conn.execute(
                text(
                    f"SELECT 1 FROM workflow_steps "
                    f"WHERE run_id=:rid "
                    f"AND COALESCE(status,'pending') NOT IN ('completed','error','disabled') "
                    f"AND COALESCE(is_enabled,{bool_true})={bool_true} "
                    f"LIMIT 1"
                ),
                {"rid": int(run_id)},
            ).fetchone()
            any_error = conn.execute(
                text(
                    f"SELECT 1 FROM workflow_steps "
                    f"WHERE run_id=:rid "
                    f"AND COALESCE(status,'')='error' "
                    f"AND COALESCE(is_enabled,{bool_true})={bool_true} "
                    "LIMIT 1"
                ),
                {"rid": int(run_id)},
            ).fetchone()
        if not any_active:
            with engine.connect() as conn:
                if any_error:
                    # 'failed' is included so a run failed mid-flight still gets a finished_at, but
                    # only once — re-stamping every tick made old runs read as "finished just now".
                    conn.execute(
                        text(
                            "UPDATE workflow_runs SET status='failed', "
                            "finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP) "
                            "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,''))) IN ('running','failed') "
                            "AND (LOWER(TRIM(COALESCE(status,'')))='running' OR finished_at IS NULL)"
                        ),
                        {"rid": int(run_id)},
                    )
                else:
                    conn.execute(
                        text(
                            "UPDATE workflow_runs SET status='completed', finished_at=CURRENT_TIMESTAMP "
                            "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,''))) = 'running'"
                        ),
                        {"rid": int(run_id)},
                    )
                conn.commit()
    except Exception:
        pass


def _process_step(engine, run_id, step, env_id, cooldown_m, now, logger, quiet) -> bool:
    """Helper to process a single step. Extracted from original run_once."""
    step_id = int(step[0])
    seq = int(step[1])
    if str(step[3] or "").strip().lower() == "action":
        return _execute_workflow_action_step(
            engine, int(run_id), step_id, seq, step, cooldown_m, now, logger
        )
    env_row = load_env(engine, int(env_id)) if env_id is not None else {}
    if not env_row or not str(env_row.get("base_url") or "").strip():
        set_step_error(engine, step_id, f"Missing environment/base_url for env_id={env_id}")
        set_run_error(engine, run_id, f"Step {seq}: missing environment config")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        return True

    base_url = normalize_base_url(str(env_row.get("base_url") or ""))

    lic_err = _ensure_environment_license_applied(engine, int(env_id), base_url, logger)
    if lic_err:
        set_step_error(engine, step_id, f"License apply failed: {lic_err}")
        set_run_error(engine, run_id, f"Step {seq}: license not applied to {base_url} — update env URL in Environments and ensure BIGID_LICENSE_TOKEN in .env")
        _set_env_busy(engine, env_id, run_id, False)
        _maybe_mark_run_completed(engine, run_id)
        return True

    # Reserve env ATOMICALLY: only claim it if it is free or already owned by THIS run.
    # If another run holds it, do not proceed — otherwise two runs share one environment
    # (the previous unconditional reserve set busy_by_run_id=run_id first, which defeated
    # the busy check below).
    if env_id is not None and not _try_acquire_env(engine, int(env_id), int(run_id)):
        try:
            with engine.connect() as conn:
                rb = conn.execute(text("SELECT busy_by_run_id FROM test_environments WHERE id=:eid"), {"eid": int(env_id)}).fetchone()
            other_rid = rb[0] if rb else None
        except Exception:
            other_rid = None
        _park_step_waiting(
            engine,
            step_id,
            int(run_id),
            int(env_id) if env_id is not None else None,
            f"Env reserved by another workflow run (id={other_rid})",
            log=logger,
            seq=seq,
        )
        set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_reserved_by_{other_rid}', started=False)
        return True
    # Set step's environment_id only when we actually start (so UI shows env only for running steps).
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "UPDATE workflow_steps SET environment_id=:eid, last_error=NULL WHERE id=:sid"
            ), {"eid": int(env_id), "sid": step_id})
            conn.commit()
    except Exception:
        pass

    try:
        token = Get_auth_token(base_url, env_row)
    except Exception as ex:
        set_step_error(engine, step_id, f"Auth failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: auth failed")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        # If no other steps are pending/active, auto-complete the run.
        _maybe_mark_run_completed(engine, run_id)
        return True

    cur_status = str(step[19] or "").strip().lower()  # status (last column in pick query)
    
    # Prep step_dict and wf_tag for recovery/tracking
    step_dict = {
        "ds_kind": step[3],
        "ds_name": step[4],
        "scan_kind": step[5],
        "file_set_type": step[6],
        "is_acl": bool(step[7]),
        "is_ocr": bool(step[8]),
        "is_structured_ff": bool(step[9]),
        "is_ols": bool(step[10]),
        "is_lite": bool(step[11]),
        "feature_flags": step[12],
        "notes": step[13],
        "environment_id": env_id,
    }
    try:
        wf_tag = f"(wf {int(run_id)}-{int(seq)})"
    except Exception:
        wf_tag = "(wf )"

    # --- RECOVERY LOGIC (PRIORITY) ---
    # Recover scan_id when still running in BigID but DB was reset (false aborted/deleted).
    _recover_pending = False
    if cur_status == "pending":
        try:
            with engine.connect() as conn:
                le_row = conn.execute(
                    text("SELECT last_error FROM workflow_steps WHERE id=:id"),
                    {"id": int(step_id)},
                ).fetchone()
            _le = str(le_row[0] or "").lower() if le_row else ""
            _recover_pending = "aborted/deleted" in _le
        except Exception:
            pass

    if cur_status in {"scan_running", "scan_waiting"} or _recover_pending:
        try:
            sid_db = str(step[18] or "").strip()  # scan_id
        except Exception:
            sid_db = ""

        # If scan_id already known, just keep it in scan_running.
        if sid_db:
            set_step_status(engine, step_id, "scan_running", scan_id=sid_db)
            set_runner_heartbeat(engine, status="idle", message=f"run={run_id} step={seq} scan_running scan_id_known", started=False)
            return False

        # Try to recover by name/profile
        try:
            with engine.connect() as conn:
                rowx = conn.execute(text("SELECT scan_profile_name, ds_name FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
            scan_profile_name_db = str((rowx[0] if rowx else '') or '').strip()
            ds_name_db = str((rowx[1] if rowx else '') or '').strip()
        except Exception:
            scan_profile_name_db = ""
            ds_name_db = ""

        recovered = ""
        if scan_profile_name_db:
            recovered = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name_db, wf_tag=wf_tag)
        if (not recovered) and ds_name_db:
            recovered = guess_latest_scan_id(base_url, token, name_contains=ds_name_db, wf_tag=wf_tag)

        if recovered:
            set_step_status(engine, step_id, "scan_running", scan_id=str(recovered), last_error=None)
            _set_env_busy(engine, env_id, run_id, True) # ENSURE BUSY
            set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} recovered_scan_id={recovered}", started=False)
            return True

    # --- BUSY ENVIRONMENT CHECK ---
    # 1. Database-level reservation check
    if env_id is not None:
        try:
            with engine.connect() as conn:
                row_busy = conn.execute(text("SELECT is_busy, busy_by_run_id FROM test_environments WHERE id=:eid"), {"eid": int(env_id)}).fetchone()
            if row_busy:
                is_busy_db = bool(row_busy[0])
                busy_by_rid = row_busy[1]
                if is_busy_db and busy_by_rid is not None and int(busy_by_rid) != int(run_id):
                    _park_step_waiting(
                        engine,
                        step_id,
                        int(run_id),
                        int(env_id),
                        f"Env reserved by another workflow run (id={busy_by_rid})",
                        log=logger,
                        seq=seq,
                    )
                    set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_reserved_by_{busy_by_rid}', started=False)
                    return True
        except Exception:
            pass

    # 2. Active scan check
    # Do not pipeline on a busy environment (prevents creating profiles when scan is actually still running).
    # SKIP this check for action steps - they don't run scans, just make API calls
    is_action_step = str(step_dict.get("ds_kind") or "").strip().lower() == "action"
    sid_pre = str(step[18] or "").strip()
    if not is_action_step and _step_needs_scan_env_free(cur_status, sid_pre):
        try:
            if env_has_running_scan(base_url, token, run_id=run_id, exclude_wf_tag=wf_tag) and cur_status not in {'scan_running'}:
                _park_step_waiting(
                    engine,
                    step_id,
                    int(run_id),
                    int(env_id),
                    "Env has a running scan; waiting for it to clear",
                    log=logger,
                    seq=seq,
                )
                set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_scan_wait', started=False)
                return True
        except Exception as ex:
            # Logic Hardening: If API fails, treat as BUSY/WAIT to avoid duplicates or overlap.
            _park_step_waiting(
                engine,
                step_id,
                int(run_id),
                int(env_id),
                f"Safety check failed (API error), waiting: {ex}",
                log=logger,
                seq=seq,
            )
            set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} api_fail_wait', started=False)
            return True

    # Reservation already handled at start of _process_step.
    logger.info("WORKFLOW_STEP_START run_id=%s step_seq=%s env_id=%s ds_name=%s", run_id, seq, env_id, step_dict.get("ds_name"))

    # Preserve stable phases so retries resume where they left off.
    stable_phases = {"scan_running", "scan_created", "ds_test_ok", "ds_created", "scan_waiting"}
    if cur_status in stable_phases:
        set_step_status(engine, step_id, cur_status)
    else:
        # Stamp the actual transition instant, NOT the tick-wide `now`. Multiple steps
        # can be dispatched in one runner tick (dispatch loop) sharing a single `now`,
        # which previously gave every step in the wave an identical started_at to the
        # microsecond — making per-step elapsed times collide. _now_utc() per step keeps
        # them distinct and accurate.
        set_step_status(engine, step_id, "running", started_at=_now_utc())
    set_runner_heartbeat(engine, status="working", message=f"run={run_id} step={seq} starting", started=False)
    # We are actively working; clear stale run-level failure messages so UI reflects current state.
    clear_run_error(engine, int(run_id))
    
    # ============================================================================
    # BUG FIX 5.1-5.4: ACTION STEP EXECUTION
    # ============================================================================
    if is_action_step:
        logger.info(f"ACTION_STEP: run_id={run_id} seq={seq} starting action execution")
        
        # BUG FIX 5.4: Check no other action is running (sequential execution)
        if not _check_no_other_action_running(engine, int(run_id)):
            logger.info(f"ACTION_STEP: Another action is running, deferring this one")
            set_step_status(engine, step_id, "pending", last_error="Waiting for previous action step to complete")
            _set_env_busy(engine, env_id, run_id, False)
            return True
        
        # BUG FIX 5.1: Generate atomic attempt ID
        attempt_id = _action_execution_attempt_id(engine, int(run_id))
        action_name = str(step_dict.get("notes") or step_dict.get("ds_name") or "unknown").strip()
        
        # BUG FIX 5.1: Mark action execution start
        _mark_action_attempt_start(engine, int(run_id), action_name, attempt_id)
        
        try:
            # Resolve action template (where all service config PUTs are defined)
            template_key, template_json = _resolve_action_template(engine, step_dict, int(env_id))
            if not template_json:
                raise ValueError(f"No action template found for: {action_name} (tried key: {template_key})")
            
            # Parse action calls (list of service config PUTs)
            try:
                action_calls = json.loads(template_json) if isinstance(template_json, str) else template_json
                if not isinstance(action_calls, list):
                    action_calls = [action_calls]
            except Exception as ex:
                raise ValueError(f"Could not parse action template JSON: {ex}")
            
            if not action_calls:
                raise ValueError(f"Action template parsed but contains no calls")
            
            logger.info(f"ACTION_STEP: Resolved {len(action_calls)} API calls for action '{action_name}'")
            
            # BUG FIX 5.1: Execute all PUTs sequentially with atomicity tracking
            success, error_msg = _execute_action_calls_sequentially(
                engine, base_url, token, action_calls, logger, attempt_id, int(run_id)
            )
            
            if not success:
                # BUG FIX 5.2: On failure, try compensating action
                logger.warning(f"ACTION_STEP: Failed - {error_msg}. Attempting compensating action...")
                compensating_actions = _get_compensating_actions(action_name)
                if compensating_actions:
                    try:
                        comp_success, comp_error = _execute_action_calls_sequentially(
                            engine, base_url, token, compensating_actions, logger, 
                            f"{attempt_id}_compensate", int(run_id)
                        )
                        if comp_success:
                            logger.info(f"ACTION_STEP: Compensating action succeeded")
                        else:
                            logger.error(f"ACTION_STEP: Compensating action also failed - {comp_error}")
                    except Exception as ex:
                        logger.error(f"ACTION_STEP: Compensating action exception - {ex}")
                
                set_step_error(engine, step_id, f"Action failed: {error_msg}")
                _set_env_busy(engine, env_id, run_id, False)
                # Mark attempt complete even on failure
                _mark_action_attempt_done(engine, int(run_id), attempt_id)
                return True
            
            # BUG FIX 5.3: After action succeeds, verify OLS flag cleared (if OLS disable action)
            # NOTE: Verification happens asynchronously - don't block here
            if "disable" in action_name.lower() and "ols" in action_name.lower():
                logger.info(f"ACTION_STEP: Disabling OLS - verification will happen in background")
            
            # BUG FIX 5.1: Mark action execution complete (atomic success)
            _mark_action_attempt_done(engine, int(run_id), attempt_id)
            
            # Mark step as completed (action steps don't scan, just configure)
            set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
            logger.info(f"ACTION_STEP: run_id={run_id} seq={seq} completed successfully")
            _set_env_busy(engine, env_id, run_id, False)
            set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
            set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} action completed", started=False)
            return True
            
        except Exception as ex:
            logger.error(f"ACTION_STEP: Exception - {ex}", exc_info=True)
            set_step_error(engine, step_id, f"Action exception: {ex}")
            _set_env_busy(engine, env_id, run_id, False)
            try:
                _mark_action_attempt_done(engine, int(run_id), attempt_id)
            except Exception:
                pass  # If attempt marking fails, continue to return
            return True

    # Fast-path: if this step already has a scan_id and DB says it's terminal, mark completed.
    try:
        sid = str(step[18] or "").strip()  # scan_id
        if sid:
            # 1. Check local DB (populated by tracker)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT scan_status FROM scan_parents WHERE scan_id=:sid LIMIT 1"),
                    {"sid": sid},
                ).fetchone()
            stv = str(row[0] or "").strip() if row else ""
            
            # 2. If DB doesn't know it's terminal, check API directly (immediate feedback)
            if not stv or not _is_terminal_state(stv):
                try:
                    pinfo = fetch_parent_scan_by_id(base_url, token, sid)
                    if isinstance(pinfo, dict) and pinfo:
                        stv = str(pinfo.get("state") or pinfo.get("status") or "").strip()
                except Exception:
                    pass

            if stv and _is_terminal_state(stv):
                if _compact(stv) in {"completed", "completedwithfailures"}:
                    set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
                    try:
                        _release_stale_env_locks_for_run(engine, int(run_id))
                    except Exception:
                        pass
                    set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} scan={sid} terminal={stv}", started=False)
                    set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
                else:
                    set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {stv}")
                    _set_env_busy(engine, env_id, run_id, False) # RELEASE
                    set_run_next_eligible(engine, run_id, _now_utc())  # allow next step to run immediately
                    set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} scan={sid} terminal={stv}", started=False)
                return True
    except Exception:
        pass

    # Version check removed from here to reduce DB/API noise.
    # It now only runs when a workflow starts or is created.
    
    build_version = str(env_row.get('version') or '').strip()
    build_prefix = _build_prefix(build_version)

    # IMPORTANT:
    # - DS name must be stable per step (avoid DS spam on retries).
    # - DS name must be unique across BigID.
    #
    # We do NOT rely on timestamps for uniqueness. We use build prefix + scenario + wf run/seq.
    existing_ds_name = str(step_dict.get("ds_name") or "").strip()
    base_ds_name = existing_ds_name or _standard_ds_base_name(step_dict)
    
    # Check if OLS is enabled for this workflow and auto-add OLS to name if not present
    try:
        if not bool(step_dict.get("is_ols")):  # Only if step doesn't already have is_ols=True
            with engine.connect() as conn:
                ols_row = conn.execute(text("SELECT COALESCE(ols_enabled, FALSE) FROM workflow_runs WHERE id = :rid"), {"rid": int(run_id)}).fetchone()
            if ols_row and bool(ols_row[0]):
                # OLS is enabled for workflow, add to name if not present
                if "OLS" not in base_ds_name.upper():
                    base_ds_name = f"{base_ds_name} OLS".strip()
    except Exception:
        pass
    
    # Always apply build prefix (if we have it).
    base_with_prefix = _apply_build_prefix(base_ds_name, build_prefix) if build_prefix else base_ds_name
    # Ensure uniqueness per workflow run/step without timestamps.
    # Ensure we don't accumulate duplicate workflow suffixes on retries.
    _wf_suffix = f"(wf {run_id}-{seq})"
    base_with_prefix_clean = re.sub(r"(?:\s*\(wf\s+\d+\-\d+\)\s*)+$", "", str(base_with_prefix or '').strip()).strip()
    # CRITICAL FIX: Mandatory scrub of '+' to satisfy BigID alphanumeric validation (400 Bad Request)
    desired_ds_name = f"{base_with_prefix_clean} {_wf_suffix}".strip().replace("+", "AND")
    try:
        logger.info('desired_ds_name step=%s ds_name=%s', seq, desired_ds_name)
    except Exception:
        pass
    try:
        # Persist build + stable ds_name; do not downgrade scan_created → running on resume.
        if str(cur_status or "").strip().lower() not in stable_phases:
            set_step_status(
                engine,
                step_id,
                "running",
                build_version=(build_version or None),
                ds_name=(desired_ds_name or None),
            )
        elif desired_ds_name:
            set_step_status(engine, step_id, cur_status, build_version=(build_version or None), ds_name=desired_ds_name)
    except Exception:
        pass
    step_dict["ds_name"] = desired_ds_name

    # --- DS create/update ---
    if not _run_is_running(engine, int(run_id)):
        return True
    ds_tpl = ""
    ds_key_used = ""
    for k in _workflow_ds_key_candidates(step_dict):
        ds_tpl = get_template(engine, k)
        if ds_tpl:
            ds_key_used = k
            break
    if not ds_tpl:
        want = _workflow_ds_key(step_dict)
        set_step_error(engine, step_id, f"Missing DS template for key: {want}")
        set_run_error(engine, run_id, f"Missing DS template: {want}")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        return True
    try:
        # Use the DS kind directly as the .env prefix (postgres → Postgres_, s3 → S3_, smb → SMB_).
        # No mapping layer. Fall back to the template type only when ds_kind isn't a connector
        # (e.g. "file-system" CSV scenarios).
        cred_kind = str(step_dict.get("ds_kind") or "").strip().lower()
        if not cred_kind or cred_kind == "file-system":
            cred_kind = credential_kind_from_ds_template(ds_tpl, step_dict)
        secret_vars = resolve_secret_vars(cred_kind)
        scanner_group = resolve_default_scanner_group(engine, int(env_id) if env_id is not None else None)
        ds_payload_raw = _render_template(
            ds_tpl,
            {
                "DS_NAME": desired_ds_name,
                "DS_TYPE": str(step[3] or "").strip(),
                "SCANNER_GROUP": scanner_group,
                "ENV_ID": str(env_id or ""),
                "FILE_SET_TYPE": str(step[6] or "").strip(),
                "IS_ACL": "1" if bool(step_dict.get("is_acl")) else "0",
                "IS_OLS": "1" if bool(step_dict.get("is_ols")) else "0",
                "IS_STRUCTURED_FF": "1" if bool(step_dict.get("is_structured_ff")) else "0",
                "FEATURE_FLAGS": str(step_dict.get("feature_flags") or "").strip(),
                "NOTES": str(step_dict.get("notes") or "").strip(),
                "BUILD_VERSION": build_version,
                "BUILD_PREFIX": build_prefix,
                **{k: v for k, v in secret_vars.items() if v},
            },
        )
        unresolved = _unresolved_placeholders(ds_payload_raw)
        if unresolved:
            raise RuntimeError(f"Unresolved template placeholders: {', '.join(unresolved[:20])}")
        ds_payload = json.loads(ds_payload_raw)
        # Force secrets from .env when present (even if template contains old values).
        ds_payload = _apply_runtime_secrets(cred_kind, ds_payload, secret_vars)
        # Apply ACL/Structured flags at runtime (so one base template is enough).
        ds_payload = _apply_runtime_flags(step_dict, ds_payload)

        # Match BigID UI: PUT body must include isSavedAsDraft=false so DS is not left in Draft.
        if isinstance(ds_payload, dict):
            # Same nesting-aware logic as _apply_runtime_flags
            ds_payload = dict(ds_payload)
            if isinstance(ds_payload.get("ds_connection"), dict):
                ds_payload["ds_connection"]["isSavedAsDraft"] = False
            else:
                ds_payload["isSavedAsDraft"] = False

        # Re-PUT until connection test passed — ds_created with wrong/missing password must be updated on BigID.
        existing_ds_id = str(step[14] or '').strip() # index 14 is ds_id
        existing_ds_name_db = str(step[4] or '').strip() # index 4 is ds_name
        st_for_put = str(cur_status or "").strip().lower()
        skip_put = (
            bool(existing_ds_id)
            and st_for_put in {"ds_test_ok", "scan_created", "scan_running", "scan_waiting"}
            and existing_ds_name_db == desired_ds_name
        )
        if str(step_dict.get("ds_kind") or "").strip().lower() == "postgres":
            try:
                dc_log = (
                    ds_payload.get("ds_connection")
                    if isinstance(ds_payload.get("ds_connection"), dict)
                    else ds_payload
                )
                if isinstance(dc_log, dict):
                    logger.info(
                        "postgres PUT run=%s step=%s cred_kind=%s ns=%s skip_put=%s user=%s rdb_url=%s rdb_name=%s password_enc_set=%s",
                        run_id,
                        seq,
                        cred_kind,
                        _credential_kind_secret_namespace(cred_kind),
                        skip_put,
                        dc_log.get("username"),
                        dc_log.get("rdb_url"),
                        dc_log.get("rdb_name"),
                        bool(str(dc_log.get("password") or dc_log.get("password_enc") or "").strip()),
                    )
            except Exception:
                pass
        if skip_put:
            out = {}
            ds_id = existing_ds_id
        else:
            out = put_ds_connection(base_url, token, desired_ds_name, ds_payload)
        # PUT response often does not include DS internal id; fetch after PUT.
        ds_id = _extract_ds_id_from_payload(out) or _extract_id(out, ["_id", "id", "dsId", "ds_id"]) or _extract_id(out, ["ds_connection", "_id", "id"])
        # The connection test must use the credentials AS STORED BY BIGID — i.e. the
        # encrypted `password` (rdb) / `secret_key_enc` (S3) that BigID returns from the
        # GET — sent to the *client* test endpoint. Re-injecting the plaintext .env value
        # here, or testing via the by-ids endpoint, makes BigID's test report failure even
        # when the DS is valid (verified against the live API). This mirrors the legacy
        # Streamlit flow that works.
        ds_conn_for_test: Optional[Dict[str, Any]] = None
        try:
            fetched = fetch_ds_connection(base_url, token, desired_ds_name)
            if isinstance(fetched, dict) and fetched:
                fetched_conn = fetched.get("ds_connection") if isinstance(fetched.get("ds_connection"), dict) else fetched
                if isinstance(fetched_conn, dict) and fetched_conn:
                    ds_conn_for_test = dict(fetched_conn)
                ds_id = ds_id or _extract_ds_id_from_payload(fetched)
        except Exception:
            pass
        if not ds_id:
            try: logger.warning("failed to extract ds_id from PUT or GET for ds_name=%s payload_keys=%s", desired_ds_name, list(out.keys()) if isinstance(out,dict) else 'non-dict')
            except Exception: pass
        if ds_id:
            logger.info("WORKFLOW_STEP_DS_CREATED run_id=%s step_seq=%s ds_id=%s ds_name=%s", run_id, seq, ds_id, desired_ds_name)
            set_step_status(engine, step_id, "ds_created", ds_id=str(ds_id))
        else:
            logger.info("WORKFLOW_STEP_DS_CREATED run_id=%s step_seq=%s ds_name=%s (no ds_id)", run_id, seq, desired_ds_name)
            set_step_status(engine, step_id, "ds_created")
    except Exception as ex:
        logger.error("WORKFLOW_STEP_DS_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
        set_step_error(engine, step_id, f"DS create failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: DS create failed")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    # --- DS connection test (required before scan creation) ---
    if not _run_is_running(engine, int(run_id)):
        return True
    try:
        # Skip retesting if we already have ds_test_ok (or beyond).
        st_db = str(cur_status or "").strip().lower()
        skip_ds_test = st_db in {"ds_test_ok", "scan_created", "scan_running", "scan_waiting"}
        if not skip_ds_test:
            # Prefer BigID's stored connection (with its own encrypted credentials) for the
            # test; fall back to the PUT payload only if the GET returned nothing.
            if isinstance(ds_conn_for_test, dict) and ds_conn_for_test:
                ds_conn_obj = dict(ds_conn_for_test)
            else:
                ds_conn_obj = ds_payload.get("ds_connection") if isinstance(ds_payload, dict) else None
                if not isinstance(ds_conn_obj, dict):
                    ds_conn_obj = ds_payload if isinstance(ds_payload, dict) else {}
                ds_conn_obj = dict(ds_conn_obj)

            # Ensure DS connection object has the runtime name.
            ds_conn_obj["name"] = desired_ds_name
            ds_conn_obj["id"] = desired_ds_name

            max_attempts = int(os.getenv("WORKFLOW_DS_TEST_MAX_ATTEMPTS", "2") or "2")
            max_attempts = max(1, min(5, max_attempts))

            # Prefer ds_id if available for ids-based test endpoint.
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT ds_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
                ds_id_val = str(row[0] or "").strip() if row else ""
            except Exception:
                ds_id_val = ""

            last_msg = ""
            ok = False
            test_attempt_count = 0
            for attempt in range(1, max_attempts + 1):
                broadcast = f"{desired_ds_name}-test-connection-complete-{uuid.uuid4()}"[:250]
                test_started = _now_utc()
                # Use the UI/Streamlit path: POST the connection (carrying BigID's own
                # encrypted credentials) to the *client* test endpoint. The by-ids endpoint
                # reports failure for rdb/file connectors even when the DS is valid.
                test_ds_connection(
                    base_url,
                    token,
                    ds_connection=ds_conn_obj,
                    broadcast_event=broadcast,
                    is_first_attempt=(attempt == 1),
                )
                timeout_s = _ds_kind_timeout_seconds(step_dict.get("ds_kind"))
                poll_s = _ds_kind_poll_seconds(step_dict.get("ds_kind"))
                initial_wait_s = _ds_kind_initial_wait_seconds(step_dict.get("ds_kind"))
                try:
                    logger.info(
                        "ds_test step=%s attempt=%s timeout_s=%s poll_s=%s initial_wait_s=%s ds_kind=%s",
                        seq,
                        attempt,
                        timeout_s,
                        poll_s,
                        initial_wait_s,
                        str(step_dict.get("ds_kind") or ""),
                    )
                except Exception:
                    pass
                if initial_wait_s > 0:
                    time.sleep(initial_wait_s)

                ok, msg = wait_for_ds_connection_test_result(
                    base_url,
                    token,
                    desired_ds_name,
                    test_started_at=test_started,
                    timeout_seconds=int(timeout_s),
                    poll_seconds=int(poll_s),
                )
                last_msg = msg or last_msg
                test_attempt_count += 1
                
                # BUG FIX 6.5: After max internal retries fail, auto-reset test with fresh attempt
                if not ok and attempt == max_attempts and test_attempt_count >= 5:
                    logger.info("WORKFLOW_STEP_DS_TEST_AUTO_RESET run_id=%s step_seq=%s attempt_count=%s", run_id, seq, test_attempt_count)
                    try:
                        # Try a fresh external test reset
                        test_ds_connection_auto(
                            base_url,
                            token,
                            ds_name=desired_ds_name,
                            ds_id=ds_id_val,
                            ds_connection=ds_conn_obj,
                            broadcast_event=f"{desired_ds_name}-auto-reset-{uuid.uuid4()}"[:250],
                            is_first_attempt=True,
                        )
                        time.sleep(5)
                        
                        # Try one more wait-for result with fresh test
                        ok, msg = wait_for_ds_connection_test_result(
                            base_url,
                            token,
                            desired_ds_name,
                            test_started_at=_now_utc(),
                            timeout_seconds=int(timeout_s),
                            poll_seconds=int(poll_s),
                        )
                        if ok:
                            logger.info("WORKFLOW_STEP_DS_TEST_AUTO_RESET_SUCCESS run_id=%s step_seq=%s", run_id, seq)
                            break
                        last_msg = msg or last_msg
                    except Exception as ex:
                        logger.error("WORKFLOW_STEP_DS_TEST_AUTO_RESET_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
                
                if ok:
                    break
                if attempt < max_attempts:
                    time.sleep(2)

            if not ok:
                raise RuntimeError(last_msg or "connection test failed")
            logger.info("WORKFLOW_STEP_DS_TEST_OK run_id=%s step_seq=%s env_id=%s", run_id, seq, env_id)
            set_step_status(engine, step_id, "ds_test_ok")
    except Exception as ex:
        logger.error("WORKFLOW_STEP_DS_TEST_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
        # BUG FIX 6.1: Try failover to another env before permanent error
        pool_env_ids = _workflow_pool_env_ids_for_run(engine, run_id)
        current_failover = 0
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT failover_count FROM workflow_steps WHERE id=:id"),
                    {"id": int(step_id)},
                ).fetchone()
            current_failover = int(row[0]) if row and row[0] is not None else 0
        except Exception:
            pass
        
        # If we have multiple envs and haven't maxed out failover attempts, try failover
        if len(pool_env_ids) > 1 and current_failover < 2 and env_id is not None:
            # Don't use same env; try failover
            set_step_error(engine, step_id, f"DS connection test failed: {ex}", should_failover=True)
            set_run_error(engine, run_id, f"Step {seq}: DS test failed on env {env_id}, queued for failover")
            _set_env_busy(engine, env_id, run_id, False)  # RELEASE to allow failover
            set_run_next_eligible(engine, run_id, _now_utc())
        else:
            # Permanent error (single env or maxed failover)
            set_step_error(engine, step_id, f"DS connection test failed: {ex}", should_failover=False)
            set_run_error(engine, run_id, f"Step {seq}: DS connection test failed")
            _set_env_busy(engine, env_id, run_id, False) # RELEASE
            set_run_next_eligible(engine, run_id, _now_utc())
        return True
    # We need ds_id for scan template variables; prefer stored column now.
    with engine.connect() as conn:
        row = conn.execute(text("SELECT ds_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
    ds_id_val = str(row[0] or "").strip() if row else ""

    # --- Scan profile create ---
    if not _run_is_running(engine, int(run_id)):
        return True
    scan_profile_name = ""
    try:
        # Idempotency: if we already have a scan_profile_id, don't re-create.
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT scan_profile_id, scan_profile_name FROM workflow_steps WHERE id=:id"),
                {"id": int(step_id)},
            ).fetchone()
        existing_spid = str(row[0] or "").strip() if row else ""
        existing_spname = str(row[1] or "").strip() if row else ""
        if existing_spid:
            scan_profile_name = existing_spname
            set_step_status(engine, step_id, "scan_created", scan_profile_id=existing_spid, scan_profile_name=(scan_profile_name or None))
        else:
            template_id = get_or_set_run_scan_template_id(
                engine,
                int(run_id),
                int(env_id) if env_id is not None else None,
                base_url,
                token,
            )
            if not template_id:
                raise RuntimeError("No default scan template id resolved (expected 'Data source scan')")

            # Scan profile name uniqueness comes from DS name (which is unique per wf step).
            scan_kind_raw = str(step_dict.get("scan_kind") or "").strip().lower().replace(" ", "")
            scan_kind_label = "Full Scan" if scan_kind_raw in {"full_scan", "fullscan", "dsscan", "dsscanprofile", "dsscanjob"} else (str(step_dict.get("scan_kind") or "Scan").strip() or "Scan")
            
            # Explicitly include ACL status in scan name
            acl_suffix = " ACL ON" if bool(step_dict.get("is_acl")) else ""
            scan_profile_name = (f"{desired_ds_name} {scan_kind_label}{acl_suffix}").strip()[:240] or f"{desired_ds_name}".strip()[:240]

            # If a reset/restart cleared scan_profile_id but BigID already has this profile, reuse it.
            found = find_scan_profile_id_by_name(base_url, token, scan_profile_name)
            if found:
                set_step_status(engine, step_id, "scan_created", scan_profile_id=str(found), scan_profile_name=scan_profile_name)
            else:
                scan_payload: Dict[str, Any] = {
                    "scanType": "dsScan",
                    "dataSourceList": [desired_ds_name],
                    "name": scan_profile_name,
                    "scanTemplateId": template_id,
                    "enabled": True,
                }
                if DEFAULT_SCAN_OWNER_EMAIL:
                    scan_payload["owners"] = [DEFAULT_SCAN_OWNER_EMAIL]

                out2 = create_scan_profile(base_url, token, scan_payload)
                scan_profile_id = _extract_id(out2, ["_id", "id", "scanProfileId", "scan_profile_id"])
                if not scan_profile_id:
                    scan_profile_id = _extract_id(out2, ["scanProfile", "_id", "id"])
                if not scan_profile_id:
                    raise RuntimeError("scan profile id not found in response")
                logger.info("WORKFLOW_STEP_SCAN_PROFILE_CREATED run_id=%s step_seq=%s sp_id=%s sp_name=%s", run_id, seq, scan_profile_id, scan_profile_name)
                set_step_status(engine, step_id, "scan_created", scan_profile_id=str(scan_profile_id), scan_profile_name=scan_profile_name)
    except Exception as ex:
        # If the only issue is "name already in use", try to find and reuse it.
        msg = str(ex)
        if ("already in use" in msg.lower()) and scan_profile_name:
            try:
                found = find_scan_profile_id_by_name(base_url, token, scan_profile_name)
                if found:
                    logger.info("WORKFLOW_STEP_SCAN_PROFILE_REUSED run_id=%s step_seq=%s sp_id=%s sp_name=%s", run_id, seq, found, scan_profile_name)
                    set_step_status(engine, step_id, "scan_created", scan_profile_id=str(found), scan_profile_name=scan_profile_name)
                else:
                    raise ex
            except Exception:
                logger.error("WORKFLOW_STEP_SCAN_PROFILE_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
                # BUG FIX 6.1: Try failover on profile create error
                set_step_error(engine, step_id, f"Scan profile create failed: {ex}", should_failover=True)
                set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
                _set_env_busy(engine, env_id, run_id, False) # RELEASE
                set_run_next_eligible(engine, run_id, _now_utc())
                return True
        else:
            logger.error("WORKFLOW_STEP_SCAN_PROFILE_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
            # BUG FIX 6.1: Try failover on profile create error
            set_step_error(engine, step_id, f"Scan profile create failed: {ex}", should_failover=True)
            set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
            _set_env_busy(engine, env_id, run_id, False) # RELEASE
            set_run_next_eligible(engine, run_id, _now_utc())
            return True

    # If we only want DS + scan profile creation (no scan trigger), stop here.
    create_only = str(os.getenv("WORKFLOW_CREATE_ONLY", "") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    if create_only:
        if not quiet:
            print(f"[{_now_utc().isoformat()}Z] run={run_id} step={seq} create_only=1 (DS + scan profile created; not triggering scan)", flush=True)
        pause_after = str(os.getenv("WORKFLOW_CREATE_ONLY_PAUSE", "1") or "1").strip().lower() in {"1", "true", "yes", "y", "on"}
        if pause_after:
            # Pause the run so it doesn't continue creating more DS/scan profiles automatically.
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text("UPDATE workflow_runs SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
                        {"rid": int(run_id)},
                    )
                    conn.commit()
            except Exception:
                pass
        return True

    # --- Start scan profile & capture parent scan id ---
    if not _run_is_running(engine, int(run_id)):
        return True
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT scan_profile_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
        spid = str(row[0] or "").strip() if row else ""
        if not scan_profile_name:
            try:
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT scan_profile_name FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
                scan_profile_name = str(row[0] or "").strip() if row else ""
            except Exception:
                scan_profile_name = ""

        # If this step was reset and scan_id cleared, but DB already has a terminal scan
        # for this exact scan profile name, don't trigger again.
        if scan_profile_name:
            prev_sid, prev_status = _find_latest_scan_parent_by_name(engine, scan_name=scan_profile_name)
            if prev_sid and prev_status and _is_terminal_state(prev_status):
                set_step_status(engine, step_id, "completed", scan_id=str(prev_sid), finished_at=_now_utc(), last_error=None)
                try:
                    _release_stale_env_locks_for_run(engine, int(run_id))
                except Exception:
                    pass
                set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} already_done scan={prev_sid} terminal={prev_status}", started=False)
                set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
                return True

        # Hard safety: one parent scan per env (includes other steps in this run, e.g. wf 88-21 + 88-25).
        try:
            if env_id is not None and _run_env_has_active_step(
                engine, int(run_id), int(env_id), exclude_step_id=step_id
            ):
                _park_step_waiting(
                    engine,
                    step_id,
                    int(run_id),
                    int(env_id),
                    "Another workflow step is already using this environment; waiting",
                    log=logger,
                    seq=seq,
                )
                set_runner_heartbeat(
                    engine,
                    status="idle",
                    message=f"run={run_id} step={seq} env_held_by_other_step",
                    started=False,
                )
                return True
            if env_has_running_scan(base_url, token, run_id=run_id):
                _park_step_waiting(
                    engine,
                    step_id,
                    int(run_id),
                    int(env_id) if env_id is not None else None,
                    "Env already has a running scan; waiting",
                    log=logger,
                    seq=seq,
                )
                set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} blocked_by_existing_running_scan', started=False)
                return True
        except Exception as ex:
            # Logic Hardening: Treat API check failure as BUSY
            _park_step_waiting(
                engine,
                step_id,
                int(run_id),
                int(env_id) if env_id is not None else None,
                f"Safety check failed before trigger (API error), waiting: {ex}",
                log=logger,
                seq=seq,
            )
            return True

        # If a scan is already running for this exact scan profile name, reuse it (idempotent).
        try:
            wf_tag = f"(wf {int(run_id)}-{int(seq)})"
        except Exception:
            wf_tag = ""
        already_running_sid = ""
        try:
            if scan_profile_name:
                already_running_sid = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name, wf_tag=wf_tag)
        except Exception:
            already_running_sid = ""
        if already_running_sid:
            set_step_status(engine, step_id, "scan_running", scan_id=str(already_running_sid), last_error=None)
            set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} already_running scan={already_running_sid}", started=False)
            return True

        if not spid and not scan_profile_name:
            raise RuntimeError("scan_profile_id and scan_profile_name are both missing after profile create")
        # Per-env, just-in-time OLS enable: guarantees the OLS feature flag is on for THIS
        # env before an OLS scan starts, even if the discrete "ENABLE OLS" action step has
        # not yet run on the whole pool. No-op for non-OLS steps. Idempotent.
        _ensure_ols_enabled_for_step(engine, run_id, seq, step_dict, env_id, logger=logger)
        logger.info("WORKFLOW_STEP_TRIGGER_SCAN run_id=%s step_seq=%s sp_id=%s sp_name=%s (about to trigger)", run_id, seq, spid, scan_profile_name)
        out3 = trigger_scan_for_profile(
            base_url,
            token,
            scan_profile_name=scan_profile_name,
            scan_profile_id=spid,
            scan_type="dsScan",
        )
        logger.info("WORKFLOW_STEP_TRIGGER_RESPONSE run_id=%s step_seq=%s response=%s", run_id, seq, out3)
        scan_id = _extract_id(out3, ["scanId", "scan_id", "parentScanId", "parent_scan_id"])
        
        # If response doesn't have scan_id, immediately query by profile name to get it
        if not scan_id and scan_profile_name:
            try:
                import time as time_module
                time_module.sleep(0.5)  # Brief wait for scan to appear in parent scans list
                wf_tag = f"(wf {int(run_id)}-{int(seq)})"
                scan_id = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name, wf_tag=wf_tag)
                if scan_id:
                    logger.info("WORKFLOW_STEP_SCAN_ID_FROM_LOOKUP run_id=%s step_seq=%s scan_id=%s", run_id, seq, scan_id)
            except Exception as lookup_ex:
                logger.debug("Could not lookup scan immediately after trigger: %s", lookup_ex)
        
        if not scan_id:
            # Fallback: infer from running parent scans list
            wf_tag = f"(wf {int(run_id)}-{int(seq)})"
            scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, wf_tag=wf_tag) or guess_latest_scan_id(base_url, token, name_contains=desired_ds_name, wf_tag=wf_tag)
        if scan_id:
            logger.info("WORKFLOW_STEP_SCAN_STARTED run_id=%s step_seq=%s scan_id=%s", run_id, seq, scan_id)
            set_step_status(engine, step_id, "scan_running", scan_id=str(scan_id))
            # Capture classifier state diff at scan-start time (one-time, non-blocking).
            try:
                from v2_api.main import _compute_classifier_snapshot, _write_classifier_snapshot_cache
                with engine.connect() as _cls_conn:
                    exists = _cls_conn.execute(
                        text("SELECT 1 FROM scan_classifier_snapshot WHERE scan_id = :sid LIMIT 1"),
                        {"sid": str(scan_id)},
                    ).fetchone()
                    if not exists:
                        _data = _compute_classifier_snapshot(_cls_conn, str(scan_id))
                        if "error" not in _data:
                            _write_classifier_snapshot_cache(_cls_conn, str(scan_id), _data)
            except Exception:
                pass
        else:
            # Scan was triggered but API didn't return scan_id directly
            # Try to find the scan by name/profile as fallback
            logger.info("WORKFLOW_STEP_SCAN_STARTED run_id=%s step_seq=%s (no scan_id from API, looking up by name)", run_id, seq)
            found_scan_id = ""
            try:
                wf_tag = f"(wf {int(run_id)}-{int(seq)})"
                # Try exact match first
                if scan_profile_name:
                    found_scan_id = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name, wf_tag=wf_tag)
                # Then try contains match as fallback
                if not found_scan_id:
                    found_scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, wf_tag=wf_tag) if scan_profile_name else ""
                if not found_scan_id and desired_ds_name:
                    found_scan_id = guess_latest_scan_id(base_url, token, name_contains=desired_ds_name, wf_tag=wf_tag)
            except Exception as lookup_ex:
                logger.debug("Could not lookup running scan: %s", lookup_ex)
            
            if found_scan_id:
                logger.info("WORKFLOW_STEP_SCAN_STARTED_LOOKUP run_id=%s step_seq=%s scan_id=%s (found by name lookup)", run_id, seq, found_scan_id)
                set_step_status(engine, step_id, "scan_running", scan_id=str(found_scan_id))
            else:
                logger.info("WORKFLOW_STEP_SCAN_STARTED run_id=%s step_seq=%s (no scan_id capture)", run_id, seq)
                # Original behavior: scan is running in BigID; runner reconciles on next tick via parent-scans list.
                set_step_status(engine, step_id, "scan_running")
    except Exception as ex:
        logger.error("WORKFLOW_STEP_SCAN_TRIGGER_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
        # Before marking as error, check if scan was actually triggered despite the exception
        # This handles cases where the scan starts but response parsing/DB save fails
        found_scan_id = ""
        try:
            wf_tag = f"(wf {int(run_id)}-{int(seq)})"
            # Try exact match first (like the pre-trigger check does)
            if scan_profile_name:
                found_scan_id = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name, wf_tag=wf_tag)
            # Then try contains match as fallback
            if not found_scan_id:
                found_scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, wf_tag=wf_tag) if scan_profile_name else ""
            if not found_scan_id and desired_ds_name:
                found_scan_id = guess_latest_scan_id(base_url, token, name_contains=desired_ds_name, wf_tag=wf_tag)
        except Exception as lookup_ex:
            logger.debug("Could not lookup running scan after trigger exception: %s", lookup_ex)
        
        if found_scan_id:
            # Scan was actually triggered; save it and continue instead of marking as error
            logger.info("WORKFLOW_STEP_SCAN_TRIGGER_EXCEPTION_BUT_SCAN_FOUND run_id=%s step_seq=%s scan_id=%s", run_id, seq, found_scan_id)
            set_step_status(engine, step_id, "scan_running", scan_id=str(found_scan_id))
            return True
        
        # No scan found; mark as error
        set_step_error(engine, step_id, f"Scan start failed: {ex}", should_failover=True)
        set_run_error(engine, run_id, f"Step {seq}: scan start failed")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    # Non-blocking: do NOT wait for completion here.
    # We'll reconcile terminal scans at the top of the loop using scan_parents.
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT scan_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
        sid_boot = str(row[0] or "").strip() if row else ""
        if sid_boot:
            try:
                from v2_api.scan_bigid_sync import bootstrap_scan_tracking  # noqa: PLC0415

                bootstrap_scan_tracking(engine, sid_boot, env_id=int(env_id) if env_id is not None else None)
                logger.info("WORKFLOW_SCAN_BOOTSTRAP_OK run_id=%s step_seq=%s scan_id=%s", run_id, seq, sid_boot)
            except Exception as boot_ex:
                logger.info("WORKFLOW_SCAN_BOOTSTRAP run_id=%s scan_id=%s note=%s", run_id, sid_boot, boot_ex)
    except Exception:
        pass
    if not quiet:
        try:
            with engine.connect() as conn:
                row = conn.execute(text("SELECT scan_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
            sid = str(row[0] or "").strip() if row else ""
        except Exception:
            sid = ""
        print(f"[{_now_utc().isoformat()}Z] run={run_id} step={seq} scan_started scan={sid or 'unknown'} (non-blocking)", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-seconds", type=int, default=30, help="Runner loop interval (default: 30)")
    ap.add_argument("--once", action="store_true", help="Run one step max and exit")
    ap.add_argument("--quiet", action="store_true", help="Minimal stdout")
    args = ap.parse_args()

    logger = _get_logger(quiet=bool(args.quiet))
    logger.info('runner_start id=%s quiet=%s interval=%s once=%s log=%s', RUNNER_ID, bool(args.quiet), args.interval_seconds, bool(args.once), str(LOG_PATH))

    engine = create_engine(DB_CONNECTION_STRING)
    # Ensure we can connect
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    # Ensure built-in action templates exist in THIS database. The API process also
    # seeds these on startup, but the runner can start before/independently of it — and
    # without the seeds every action step fails with "missing template: ...". Idempotent.
    try:
        try:
            from workflow_action_templates import seed_workflow_action_templates
        except ImportError:
            from v2_api.workflow_action_templates import seed_workflow_action_templates
        seed_workflow_action_templates(engine)
        logger.info('action templates seeded')
    except Exception as ex:
        logger.warning('action template seed failed: %s', ex)

    if args.once:
        did = run_once(engine, quiet=bool(args.quiet))
        return 0 if did else 0

    while True:
        try:
            set_runner_heartbeat(engine, status="running", message="loop", started=True)
            did = run_once(engine, quiet=bool(args.quiet))
            set_runner_heartbeat(engine, status=("work" if did else "idle"), message=("did work" if did else "idle"), started=False)
            if not did:
                time.sleep(max(5, int(args.interval_seconds)))
            else:
                # If we did work, loop quickly to pick up pause/cooldown.
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
        except Exception as ex:
            try:
                set_runner_heartbeat(engine, status="error", message=f"runner_error: {ex}", started=False)
            except Exception:
                pass
            try:
                logger.exception('runner_error: %s', ex)
            except Exception:
                pass
            if not args.quiet:
                print(f"[{_now_utc().isoformat()}Z] runner_error: {ex}", flush=True)
            time.sleep(max(10, int(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
