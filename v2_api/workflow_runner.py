"""
Workflow runner for DS + scan automation.

Reads `workflow_runs` + `workflow_steps` from the Streamlit UI and executes them with
**parallelism up to the workflow pool size** (active envs with "Use for workflow runs").

Per-step pipeline on its assigned environment (odd steps → env1, even → env2 by default):
  pre-action (optional) → create/update DS → test connection → create scan profile
  → start parent scan → reconcile until terminal → cooldown → next step.

With two environments, up to two steps can run at once when earlier steps have finished
or are waiting on capacity; `WORKFLOW_MAX_CONCURRENT_SCANS` caps concurrency (default = env count).

Secrets are not hardcoded: payloads live in `workflow_templates` with {{PLACEHOLDER}} variables.
"""

from __future__ import annotations

# Step is still using its environment (DS setup, profile, or scan) — keep env reserved / count toward concurrency.
WORKFLOW_ENV_OCCUPYING_STATUSES: tuple[str, ...] = (
    "running",
    "pre_action_done",
    "ds_created",
    "ds_test_ok",
    "scan_created",
    "scan_running",
    "scan_waiting",
)


def _workflow_env_occupying_sql_in() -> str:
    return ",".join(f"'{s}'" for s in WORKFLOW_ENV_OCCUPYING_STATUSES)


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
    if is_postgresql(engine):
        return f"COALESCE({column_sql}, TRUE) = TRUE"
    return f"COALESCE({column_sql}, 1) = 1"


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
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

def _sync_env_version_standalone_runner(env_id: int) -> None:
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

RUNNING_STATES = {"started", "awaiting", "inprogress", "in progress", "running", "queued", "pending"}
TERMINAL_STATES = {"failed", "completedwithfailures", "completed", "stopped", "aborted", "cancelled", "canceled", "stoprequested", "stop requested", "stopping"}

ENV_FILE_PATH = Path(__file__).parent / ".env"

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
    # MSSQL default 600s (10 min); others 180s.
    if k == 'MSSQL':
        v2 = _env_int('WORKFLOW_DS_TEST_TIMEOUT_SECONDS', 600)
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
    return str(base_url or "").strip().rstrip("/")


def build_api_url(base_url: str, api_path: str) -> str:
    base = normalize_base_url(base_url)
    p = str(api_path or "").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return f"{base}{p}"


def _request(method: str, url: str, token: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None, timeout_seconds: Optional[float] = None) -> requests.Response:
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_raw, timeout=(HTTP_TIMEOUT if timeout_seconds is None else float(timeout_seconds)))
    if resp.status_code in (401, 403) and token:
        resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_bearer, timeout=(HTTP_TIMEOUT if timeout_seconds is None else float(timeout_seconds)))
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
    resp = _request("GET", url, token, timeout_seconds=timeout_seconds)
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
                    updated_at TEXT DEFAULT (datetime('now'))
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
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
            return st
    except Exception:
        pass
    return None


def _is_workflow_scan_name(name: str, *, run_id: Optional[int] = None) -> bool:
    nm = str(name or '').strip()
    if not nm:
        return False
    if run_id is not None:
        if f'(wf {int(run_id)}-' in nm:
            return True
    return '(wf ' in nm


def env_has_running_scan(base_url: str, token: str, *, run_id: Optional[int] = None, exclude_wf_tag: str = "") -> bool:
    """
    Hard safety. By default, blocks if ANY scan is running on this environment.
    (This ensures clean benchmarks and prevents resource contention).

    Set WORKFLOW_BLOCK_ANY_RUNNING_SCAN=0 if you ONLY want to block on other workflow scans.
    """
    block_any = str(os.getenv('WORKFLOW_BLOCK_ANY_RUNNING_SCAN', '1') or '1').strip().lower() in {'1','true','yes','y','on'}
    try:
        states = [
            'Started','Awaiting','In Progress','InProgress','Running',
            'Stop Requested','StopRequested','Stopping','Queued','Pending'
        ]
        scans = fetch_parent_scans(base_url, token, states=states, limit=50, skip=0)
        print(f"[env_has_running_scan] base_url={base_url}, found {len(scans)} scans, block_any={block_any}, run_id={run_id}", flush=True)
    except Exception as ex:
        # Default fail-open so API errors (timeout, auth) don't block with "no env". Set WORKFLOW_FAIL_OPEN_ON_BUSY_CHECK=0 for strict fail-closed.
        fail_open = str(os.getenv('WORKFLOW_FAIL_OPEN_ON_BUSY_CHECK', '1') or '1').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        print(f"[env_has_running_scan] API check failed ({'treating as FREE' if fail_open else 'assuming BUSY'}): {ex}", flush=True)
        return not fail_open

    for sc in scans or []:
        if not isinstance(sc, dict):
            continue
        nm = _scan_name_from_parent_scan(sc)
        
        # If we have an exclusion tag (e.g. "(wf 9-4)"), and this running scan matches it,
        # then it is likely the current step itself, so we don't block.
        if exclude_wf_tag and str(exclude_wf_tag).lower().strip() in nm.lower():
            print(f"[env_has_running_scan] excluding scan '{nm}' (matches exclude_wf_tag)", flush=True)
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
        if _is_workflow_scan_name(nm, run_id=run_id):
            print(f"[env_has_running_scan] BLOCKING due to workflow scan '{nm}'", flush=True)
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
        with engine.begin() as conn:
            if is_busy:
                conn.execute(
                    text("UPDATE test_environments SET is_busy=1, busy_by_run_id=:rid, busy_since=:now WHERE id=:eid"),
                    {"rid": run_id, "now": now, "eid": int(env_id)}
                )
            else:
                conn.execute(
                    text("UPDATE test_environments SET is_busy=0, busy_by_run_id=NULL, busy_since=NULL WHERE id=:eid"),
                    {"eid": int(env_id)}
                )
    except Exception:
        pass


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
    except Exception:
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
    """
    if isinstance(action_payload, list):
        results = []
        for item in action_payload:
            results.append(run_action_template(base_url, token, item))
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
    
    resp = _request(method, url, token, params=(params if isinstance(params, dict) else None), json_body=body)
    
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
    except Exception:
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

def set_step_error(engine, step_id: int, message: str) -> None:
    with engine.connect() as conn:
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
                       COALESCE(include_in_workflow_runs, {bool_true}) AS include_in_workflow_runs
                FROM test_environments
                WHERE id = :id
                LIMIT 1
            """),
            {"id": int(env_id)},
        ).fetchone()
    if not row:
        return {}
    return {
        "id": row[0],
        "env_name": row[1],
        "base_url": row[2],
        "version": row[3],
        "auth_module_path": row[4],
        "auth_function_name": row[5],
        "classifiers_api_path": row[6],
        "is_active": row[7],
        "include_in_workflow_runs": bool(int(row[8])) if row[8] is not None else True,
    }


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
            sk_enc = _get("S3_secret_key_enc")
            sk = _get("S3_secret_key")
            if sk and sk_enc:
                ap["secret_key_enc"] = sk
                ap.pop("secret_key", None)
            elif sk_enc:
                ap["secret_key_enc"] = sk_enc
                ap.pop("secret_key", None)
            elif sk:
                # BigID UI uses secret_key_enc. If user only provides S3_secret_key,
                # place it into secret_key_enc (works for both encrypted and plain values
                # in many deployments; avoids silent ignore of secret_key).
                ap["secret_key_enc"] = sk
                ap.pop("secret_key", None)
            tid = _get("S3_tenantid")
            if tid and "tenantId" in ap:
                ap["tenantId"] = tid
            ds_conn["authenticationProperties"] = ap
    elif k == "smb":
        u = _get("SMB_username")
        d = _get("SMB_domain")
        pw_enc = _get("SMB_password_enc")
        pw = _get("SMB_password")
        ra = _get("SMB_resourceAddress")

        # Some exports store SMB creds at top-level, some under authenticationProperties.
        if u:
            ds_conn["username"] = u
        if d and ("domain" in ds_conn):
            ds_conn["domain"] = d
        if pw and pw_enc:
            ds_conn["password_enc"] = pw
            ds_conn.pop("password", None)
        elif pw_enc:
            ds_conn["password_enc"] = pw_enc
            ds_conn.pop("password", None)
        elif pw:
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
            if pw and pw_enc:
                ap["password_enc"] = pw
                ap.pop("password", None)
            elif pw_enc:
                ap["password_enc"] = pw_enc
                ap.pop("password", None)
            elif pw:
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

        allow = {
            "username","user","userid","user_id","account","accountname","domain",
            "password","password_enc","passwd","pwd",
            "secret","secret_enc","secret_key","secret_key_enc","clientsecret","client_secret",
            "accesskey","access_key","accesskeyid","access_key_id",
            "token","api_key","apikey","private_key","privatekey",
            "tenantid","tenant_id",
            # Non-secret but environment-specific connector endpoints/hosts (esp SMB/MSSQL/Postgres exports)
            "resourceaddress","host","hostname","server","serveraddress","address","endpoint","url",
        }

        def _walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for key in list(obj.keys()):
                    kl = str(key or "").strip().lower()
                    if kl in pref_map and (kl in allow):
                        obj[key] = pref_map[kl]
                    # common: template has password but env provides password_enc
                    if kl == "password" and ("password_enc" in pref_map) and ("password_enc" in allow):
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
                    if isinstance(is_success, bool):
                        if is_success:
                            return True, "success"
                        return False, _format_connection_test_detail(cst)
                    # if it's present but not bool, treat truthy strings
                    s = str(is_success or "").strip().lower()
                    if s in {"true", "1", "yes", "ok", "success"}:
                        return True, "success"
                    if s in {"false", "0", "no", "fail", "failed", "error"}:
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
            return resp.json() if resp.content else {}
        except Exception as ex:
            errors.append(f"POST {url} -> {ex}")
            continue
    raise RuntimeError(f"Failed to start scan profile via known endpoints: {' | '.join(errors[:6])}")


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
    resp = _request("POST", url, token, json_body=payload)
    return resp.json() if resp.content else {}


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


def _auto_resume_failed_run_if_stuck(engine, run_id: int) -> None:
    """
    If a run was marked failed while enabled steps are still non-terminal, flip back to running.
    Otherwise the runner never loads it (WHERE status='running') and parallelism / DS pipeline stall forever.
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
        with engine.connect() as conn:
            act = conn.execute(
                text(
                    f"SELECT 1 FROM workflow_steps WHERE run_id=:rid AND COALESCE(is_enabled,{bool_true})={bool_true} "
                    "AND LOWER(TRIM(COALESCE(status,''))) NOT IN ('completed','error','disabled') LIMIT 1"
                ),
                {"rid": int(run_id)},
            ).fetchone()
        if not act:
            return
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE workflow_runs SET status='running', finished_at=NULL, "
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
                            _sync_env_version_standalone_runner(int(eid))
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
        set_runner_heartbeat(engine, status="idle", message="no running workflows", started=False)
        return False

    # Failed runs with non-terminal enabled steps still need the runner (otherwise steps stay stuck forever).
    for rr0 in run_rows:
        try:
            _auto_resume_failed_run_if_stuck(engine, int(rr0[0]))
        except Exception:
            pass

    # Track global state across all running workflows
    inflight_envs: Set[int] = set()
    targeted_envs: Set[int] = set()
    targeted_steps: Set[int] = set()
    did_any_work = False

    # --- Pre-pass: Release environments where cooldown or run has ended ---
    try:
        with engine.connect() as conn:
            busy_envs = conn.execute(text("SELECT id, busy_by_run_id FROM test_environments WHERE is_busy=1")).fetchall()
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
            
            # If cooldown is active (in the future), keep it busy.
            is_in_cooldown = bool(r_next and isinstance(r_next, datetime) and r_next > now)
            
            if is_in_cooldown:
                targeted_envs.add(be_id)
                continue
            
            # Cooldown is over or not set. Release only if no in-flight DS/scan work on this env for this run.
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

            for rr in inflight_rows or []:
                step_id = int(rr[0])
                seq = int(rr[1])
                env_id = rr[2]
                sid = str(rr[3] or "").strip()
                st_status = str(rr[4] or "").strip().lower()
                scan_status = str(rr[5] or "").strip()
                step_updated_at = rr[6] if len(rr) > 6 else None
                
                if scan_status and _is_terminal_state(scan_status):
                    if _compact(scan_status) in {"completed", "completedwithfailures"}:
                        set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
                        completed_success_any = True
                        # Auto-save benchmark when scan completes/completes-with-failures through workflow
                        try:
                            _auto_save_benchmark_from_step(engine, step_id, sid, run_id, finished_at=_now_utc())
                        except Exception:
                            pass
                    else:
                        set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {scan_status}")
                        set_run_error(engine, run_id, f"Step {seq}: scan ended in state {scan_status}")
                        set_run_next_eligible(engine, run_id, _now_utc())
                    completed_any = True
                    did_any_work = True
                elif sid and not scan_status:
                    # Tracker likely hasn't synced yet.
                    # Verify via API directly before resetting to prevent duplicate scan loops.
                    api_status = None
                    try:
                        api_status = get_scan_status_via_api(base_url, token, sid)
                    except Exception:
                        # Safety: If API check fails, assume it's still running to avoid double-booking the env
                        api_status = "Running"

                    if api_status:
                        if _is_terminal_state(api_status):
                            if _compact(api_status) in {"completed", "completedwithfailures"}:
                                set_step_status(engine, step_id, "completed", finished_at=_now_utc())
                            else:
                                set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {api_status}")
                                set_run_error(engine, run_id, f"Step {seq}: scan ended in state {api_status}")
                            completed_any = True
                            did_any_work = True
                        else:
                            # Not terminal, just keep as scan_running
                            if st_status != "scan_running":
                                set_step_status(engine, step_id, "scan_running")
                            else:
                                touch_step_updated_at(engine, step_id)
                            if env_id is not None:
                                inflight_envs.add(int(env_id))
                        continue

                    # If API also says it's missing (or we can't tell), use grace period
                    if step_updated_at and isinstance(step_updated_at, datetime):
                        if (now - step_updated_at) < grace_delta:
                            if st_status != "scan_running":
                                set_step_status(engine, step_id, "scan_running")
                            else:
                                touch_step_updated_at(engine, step_id)
                            if env_id is not None:
                                inflight_envs.add(int(env_id))
                            continue

                    set_step_status(engine, step_id, "pending", scan_id=None, last_error="Scan aborted/deleted; reset for retry")
                    set_run_next_eligible(engine, run_id, _now_utc())
                    completed_any = True
                    did_any_work = True
                else:
                    if env_id is not None and sid and (scan_status or st_status == "scan_running"):
                        # Only consider an environment "inflight" if it's actively scanning.
                        # Include scan_running even when scan_status is empty (tracker not synced yet).
                        # scan_waiting means it's waiting for capacity, so it doesn't occupy a slot itself.
                        if st_status != "scan_waiting":
                            inflight_envs.add(int(env_id))
                    if sid and st_status != "scan_running":
                        set_step_status(engine, step_id, "scan_running")
                    elif sid and st_status == "scan_running":
                        # Tracker shows non-terminal scan; row already scan_running — still bump liveness.
                        touch_step_updated_at(engine, step_id)
            
            if completed_any and completed_success_any:
                try:
                    c_min = int(run_row[2]) if run_row[2] is not None else 15
                except Exception:
                    c_min = 15
                set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(c_min)))
        except Exception:
            pass

    # --- GLOBAL STALE STEP RECONCILIATION (after Pass 1 so active scans get updated_at refreshed first) ---
    try:
        try:
            stale_hours = int(os.getenv("WORKFLOW_STALE_SCAN_RUNNING_HOURS", "48") or "48")
        except Exception:
            stale_hours = 48
        stale_hours = max(2, min(stale_hours, 168))
        cutoff = now - timedelta(hours=float(stale_hours))
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
    except Exception as ex:
        logger.error("Stale scan_running cleanup error: %s", ex)

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
            # Skip this run if it's in cooldown, UNLESS it already has inflight work we might want to reconcile
            # (But reconciliation happened in Pass 1).
            continue

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
                           ds_id, scan_profile_id, scan_profile_name, build_version, build_prefix, scan_id, status
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
            num_envs = max(1, env_count)
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

                # ACTION STEPS: Must wait for ALL PREVIOUS STEPS to complete AND environment to be free
                # They execute on ONE environment at a time, respecting cooldown
                if is_action_step:
                    logger.info(f"_inner_pick: cand_id={cand_id} is ACTION step, seq={cand_seq}")
                    
                    # ACTION steps must wait for all previous steps to complete
                    if cand_seq > 1:
                        with engine.connect() as conn:
                            in_progress = conn.execute(
                                text(
                                    f"SELECT COUNT(1) FROM workflow_steps WHERE run_id=:rid AND seq < :seq AND LOWER(TRIM(COALESCE(status,''))) IN ({_workflow_env_occupying_sql_in()})"
                                ),
                                {"rid": run_id, "seq": cand_seq},
                            ).fetchone()
                        count_before = int(in_progress[0]) if in_progress else 0
                        logger.info(f"_inner_pick: ACTION step seq={cand_seq}, previous steps in progress={count_before}")
                        if count_before > 0:
                            logger.info(f"_inner_pick: ACTION step seq={cand_seq} waiting for {count_before} previous steps to complete")
                            continue  # Wait for all previous steps to finish
                    
                    target_env = int(c_env) if c_env is not None else None
                    
                    # If no environment assigned yet, find a free one
                    if target_env is None:
                        for eid in active_env_ids:
                            if int(eid) in targeted_envs:
                                continue  # This env is busy with another step
                            
                            # Check if this environment has any running scan
                            env_row_tmp = load_env(engine, eid)
                            if not env_row_tmp:
                                continue
                            tmp_burl = (env_row_tmp.get("base_url") or "").strip()
                            if not tmp_burl:
                                continue
                            
                            try:
                                tmp_token = Get_auth_token(tmp_burl, env_row_tmp)
                                if env_has_running_scan(tmp_burl, tmp_token, run_id=run_id):
                                    continue  # Scan running on this env, skip it
                            except Exception:
                                pass  # Fail-open: treat as free
                            
                            # This environment is free!
                            target_env = eid
                            break
                    else:
                        # Environment already assigned, check if it's free
                        if int(target_env) in targeted_envs:
                            continue  # Busy with another step
                        
                        env_row_tmp = load_env(engine, target_env)
                        if env_row_tmp:
                            tmp_burl = (env_row_tmp.get("base_url") or "").strip()
                            if tmp_burl:
                                try:
                                    tmp_token = Get_auth_token(tmp_burl, env_row_tmp)
                                    if env_has_running_scan(tmp_burl, tmp_token, run_id=run_id):
                                        continue  # Scan still running, wait
                                except Exception:
                                    pass
                    
                    if target_env is not None:
                        return (cand, target_env)
                    continue  # No free environment available yet

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

                # Search through ALL active environments (or just the assigned one). DB first (reserved by another run?), then API (scan running?).
                best_env = None
                env_candidates = [int(c_env)] if c_env is not None else active_env_ids
                logger.info(f"_inner_pick: cand_id={cand_id}, searching for env, env_candidates={env_candidates}")
                for eid in env_candidates:
                    logger.info(f"_inner_pick: cand_id={cand_id}, trying env {eid}")
                    if int(eid) not in active_env_ids:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} not in active_env_ids, skipping")
                        continue
                    if int(eid) in targeted_envs:
                        # Allow the step that already owns this env to continue its own pipeline.
                        if not (c_env is not None and int(c_env) == int(eid) and c_status in WORKFLOW_ENV_OCCUPYING_STATUSES):
                            logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} in targeted_envs, skipping")
                            continue
                    
                    # Check DB busy status
                    db_busy, db_busy_by = env_db_busy.get(eid, (False, None))
                    if db_busy and db_busy_by is not None and int(db_busy_by) != int(run_id):
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} busy in DB by run {db_busy_by}, skipping")
                        continue  # reserved by another run
                    
                    # DB says free — check API before creating DB/scan: if scan running on this env, try next env.
                    env_row_tmp = load_env(engine, eid)
                    if not env_row_tmp:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} not found in DB, skipping")
                        continue
                    tmp_burl = (env_row_tmp.get("base_url") or "").strip()
                    if not tmp_burl:
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} has no base_url, skipping")
                        continue
                    try:
                        tmp_token = Get_auth_token(tmp_burl, env_row_tmp)
                        has_scan = env_has_running_scan(tmp_burl, tmp_token, run_id=run_id)
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} API check result: has_scan={has_scan}")
                        if has_scan:
                            logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} has running scan (API check), skipping")
                            continue  # scan running on this env; try another
                    except Exception as e:
                        # fail-open: API error → treat as free so we don't block (env_has_running_scan also fail-opens)
                        logger.info(f"_inner_pick: cand_id={cand_id}, env {eid} API check failed ({e}), treating as free")
                        pass
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
        run_did_work = False
        logger.info(f"run_id={run_id}: slots_to_fill={slots_to_fill}, loop will run {slots_to_fill + 1} times")
        for iteration in range(slots_to_fill + 1):
            logger.info(f"run_id={run_id}: iteration {iteration + 1}/{slots_to_fill + 1}")
            pick = _inner_pick(allow_scan_running=False)
            if not pick:
                logger.info(f"run_id={run_id}: no pick with allow_scan_running=False, trying with True")
                pick = _inner_pick(allow_scan_running=True)
                if not pick:
                    logger.info(f"run_id={run_id}: no pick found, breaking loop")
                    # No free env this tick: mark step scan_waiting but do NOT block the run. Next tick will retry (no 22 min wait).
                    # IMPORTANT: Only mark steps that are NOT already in a terminal or active state
                    if candidates:
                        first_ready = candidates[0]
                        fr_id = int(first_ready[0])
                        fr_status = str(first_ready[19] or '').strip().lower()
                        # Do NOT overwrite completed/error/disabled steps, and do NOT overwrite steps that are already being processed
                        if fr_status not in {'completed','error','disabled','scan_running','running'} and fr_id not in targeted_steps:
                            set_step_status(engine, fr_id, 'scan_waiting', last_error='No free env this tick; will retry next tick.')
                            # Do NOT set next_eligible_at — run stays eligible so next runner tick retries immediately
                    break
            
            step, env_id = pick
            step_id = int(step[0])
            logger.info(f"run_id={run_id}: picked step_id={step_id}, env_id={env_id}")
            targeted_steps.add(step_id)
            if env_id is not None:
                targeted_envs.add(int(env_id))
                # Do NOT set environment_id here — only set when step actually starts in _process_step

            res = _process_step(engine, run_id, step, env_id, cooldown_m, now, logger, quiet)
            if res:
                did_any_work = True
                run_did_work = True
                if slots_to_fill > 0: slots_to_fill -= 1

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
        print(f"[AUTO-BENCHMARK] scanners={scanner_count} threads={thread_count} files={objects_scanned}", flush=True)

        # Compute all three throughput tiers
        tp_cluster_files = objects_scanned / scan_duration_h
        tp_scanner_files = (tp_cluster_files / scanner_count) if scanner_count > 0 else 0.0
        tp_thread_files  = (tp_cluster_files / thread_count)  if thread_count  > 0 else 0.0

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
                    
        fs_id = None
        if file_set_type:
            with engine.connect() as conn:
                r = conn.execute(text("SELECT id FROM file_set_types WHERE name = :n"), {"n": file_set_type}).fetchone()
                if r:
                    fs_id = r[0]
                else:
                    conn.execute(text("INSERT INTO file_set_types (name) VALUES (:n)"), {"n": file_set_type})
                    conn.commit()
                    fs_id = conn.execute(text("SELECT id FROM file_set_types WHERE name = :n"), {"n": file_set_type}).fetchone()[0]

        # Build INSERT dynamically based on normalized schema
        cols = [
            "build_id", "run_date", "scenario_type", "data_source_type_id", "file_set_type_id",
            "scan_start_time", "scan_end_time",
            "total_scan_hours",
            "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr",
        ]
        vals = [":b_id", "CURRENT_DATE", ":scenario", ":dst_id", ":fst_id",
                ":ss", ":se", ":tsh",
                ":tpcf", ":tpsf", ":tptf"]
        params: Dict[str, Any] = {
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
                    "WHERE table_name='benchmarks' AND column_name='scan_id' LIMIT 1"
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
                    "WHERE table_name='benchmarks' AND column_name='total_classification' LIMIT 1"
                )).fetchone())
            except Exception:
                has_cls = False
            if has_cls and total_classification is not None:
                cols.append("total_classification")
                vals.append(":tclass")
                params["tclass"] = total_classification

            conn.execute(
                text(f"INSERT INTO benchmarks ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
            conn.commit()

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
                    conn.execute(
                        text(
                            "UPDATE workflow_runs SET status='failed', finished_at=CURRENT_TIMESTAMP "
                            "WHERE id=:rid AND LOWER(TRIM(COALESCE(status,''))) IN ('running','failed')"
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
    env_row = load_env(engine, int(env_id)) if env_id is not None else {}
    if not env_row or not str(env_row.get("base_url") or "").strip():
        set_step_error(engine, step_id, f"Missing environment/base_url for env_id={env_id}")
        set_run_error(engine, run_id, f"Step {seq}: missing environment config")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        return True

    base_url = str(env_row.get("base_url") or "").strip()

    # Reserve env and set step's environment_id only when we actually start (so UI shows env only for running steps).
    _set_env_busy(engine, env_id, run_id, True)
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

    cur_status = str(step[20] or "").strip().lower() # index 20 is status
    
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
    }
    try:
        wf_tag = f"(wf {int(run_id)}-{int(seq)})"
    except Exception:
        wf_tag = "(wf )"

    # --- RECOVERY LOGIC (PRIORITY) ---
    # If this step is already in scan_running/scan_waiting, try to recover the scan_id immediately.
    # This prevents the "Busy Environment" check from stalling a step that has actually already started.
    if cur_status in {"scan_running", "scan_waiting"}:
        try:
            sid_db = str(step[19] or "").strip() # index 19 is scan_id
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
                    set_step_status(engine, step_id, 'scan_waiting', last_error=f'Env reserved by another workflow run (id={busy_by_rid})')
                    set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_reserved_by_{busy_by_rid}', started=False)
                    return True
        except Exception:
            pass

    # 2. Active scan check
    # Do not pipeline on a busy environment (prevents creating profiles when scan is actually still running).
    # SKIP this check for action steps - they don't run scans, just make API calls
    is_action_step = str(step_dict.get("ds_kind") or "").strip().lower() == "action"
    if not is_action_step:
        try:
            if env_has_running_scan(base_url, token, run_id=run_id, exclude_wf_tag=wf_tag) and cur_status not in {'scan_running'}:
                set_step_status(engine, step_id, 'scan_waiting', last_error='Env has a running scan; waiting for it to clear')
                set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_scan_wait', started=False)
                return True
        except Exception as ex:
            # Logic Hardening: If API fails, treat as BUSY/WAIT to avoid duplicates or overlap.
            set_step_status(engine, step_id, 'scan_waiting', last_error=f'Safety check failed (API error), waiting: {ex}')
            set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} api_fail_wait', started=False)
            return True

    # Reservation already handled at start of _process_step.
    logger.info("WORKFLOW_STEP_START run_id=%s step_seq=%s env_id=%s ds_name=%s", run_id, seq, env_id, step_dict.get("ds_name"))

    # Preserve stable phases so retries resume where they left off.
    stable_phases = {'scan_running','scan_created','ds_test_ok','ds_created','scan_waiting'}
    if cur_status in stable_phases:
        set_step_status(engine, step_id, cur_status)
    else:
        set_step_status(engine, step_id, 'running', started_at=now)
    set_runner_heartbeat(engine, status="working", message=f"run={run_id} step={seq} starting", started=False)
    # We are actively working; clear stale run-level failure messages so UI reflects current state.
    clear_run_error(engine, int(run_id))

    # Fast-path: if this step already has a scan_id and DB says it's terminal, mark completed.
    try:
        sid = str(step[19] or "").strip() # index 19 is scan_id
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
                    # DO NOT RELEASE HERE - Flowchart says: Wait Completion -> Cooldown -> env FREE
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
    
    # --- Action Step Type (CHECK FIRST - before DS name generation) ---
    # If ds_kind is 'action', we look for an action template matching the name/notes and execute it.
    # Action steps ALWAYS execute on ALL workflow environments, not just one.
    # Action steps should execute on ONE environment at a time, respecting cooldown and scan status
    # They should NOT run if ANY scan is running on that environment
    if str(step_dict.get("ds_kind") or "").strip().lower() == "action":
        try:
            # For action steps, we use the notes or ds_name as the template key
            action_key = str(step_dict.get("notes") or step_dict.get("ds_name") or "").strip()
            action_tpl = get_template(engine, action_key)
            if not action_tpl:
                # Try prefixing with action::
                action_tpl = get_template(engine, f"action::{action_key}")
            
            if not action_tpl:
                raise RuntimeError(f"Missing action template for key: {action_key}")
            
            # Get the ASSIGNED environment for this action step
            # Action steps are assigned to a specific environment by the workflow runner
            assigned_env_id = step_dict.get("environment_id")
            if not assigned_env_id:
                raise RuntimeError("Action step has no assigned environment")
            
            # Get environment details
            bool_true = _sql_bool_true(engine)
            bool_false = _sql_bool_false(engine)
            with engine.connect() as conn:
                env_row = conn.execute(text(f"""
                    SELECT id, env_name, base_url, auth_module_path, auth_function_name
                    FROM test_environments
                    WHERE id = :eid
                      AND COALESCE(is_active, {bool_false}) = {bool_true}
                      AND COALESCE(include_in_workflow_runs, {bool_true}) = {bool_true}
                """), {"eid": assigned_env_id}).fetchone()
            
            if not env_row:
                raise RuntimeError(f"Environment {assigned_env_id} not found or not active")
            
            env_id = int(env_row[0])
            env_name = str(env_row[1] or "")
            base_url = str(env_row[2] or "").strip()
            
            if not base_url:
                raise RuntimeError(f"Environment {env_name} has no base_url")
            
            # Get auth token for this environment
            token_item = Get_auth_token(base_url, {
                "auth_module_path": env_row[3],
                "auth_function_name": env_row[4]
            })
            if not token_item:
                raise RuntimeError(f"Failed to get auth token for {env_name}")
            
            # Execute action on this ONE environment
            action_raw = _render_template(action_tpl, {"BASE_URL": base_url, "TOKEN": token_item})
            action_payload = json.loads(action_raw)
            run_action_template(base_url, token_item, action_payload)
            
            logger.info(f"Action executed on env {env_id} ({env_name}): {action_key}")
            
            # Action completed successfully on this environment
            with engine.connect() as conn:
                conn.execute(text("""
                    UPDATE workflow_steps 
                    SET status = 'completed',
                        finished_at = CURRENT_TIMESTAMP,
                        last_error = NULL,
                        notes = CASE 
                            WHEN notes LIKE '%Executed on:%' THEN notes
                            ELSE COALESCE(notes, '') || ' [Executed on: ' || :env_name || ']'
                        END
                    WHERE id = :sid
                """), {"sid": step_id, "env_display": env_display})
                conn.commit()
            
            logger.info(f"Action step completed: {action_key} on {success_count}/{len(env_rows)} environments")
            
            # If this was an OLS enable action, mark the workflow as OLS-enabled
            if "OLS" in action_key.upper():
                try:
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE workflow_runs 
                            SET ols_enabled = TRUE 
                            WHERE id = :rid
                        """), {"rid": int(run_id)})
                except Exception:
                    pass
            
            _set_env_busy(engine, env_id, run_id, False) # RELEASE
            set_run_next_eligible(engine, run_id, _now_utc())
            return True
        except Exception as ex:
            set_step_error(engine, step_id, f"Action failed: {ex}")
            set_run_error(engine, run_id, f"Step {seq}: action failed")
            _set_env_busy(engine, env_id, run_id, False) # RELEASE
            set_run_next_eligible(engine, run_id, _now_utc())
            return True
    
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
        # Persist build + stable ds_name so UI + retries stay consistent.
        set_step_status(
            engine,
            step_id,
            "running",
            build_version=(build_version or None),
            build_prefix=(build_prefix or None),
            ds_name=(desired_ds_name or None),
        )
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

        # Idempotency: if DS already exists for this step (ds_id stored) and name matches,
        # avoid re-PUT to prevent accidental duplicate DS artifacts.
        existing_ds_id = str(step[14] or '').strip() # index 14 is ds_id
        existing_ds_name_db = str(step[4] or '').strip() # index 4 is ds_name
        skip_put = bool(existing_ds_id) and (str(cur_status or '').strip().lower() in {'ds_created','ds_test_ok','scan_created','scan_running','scan_waiting'}) and (existing_ds_name_db == desired_ds_name)
        if skip_put:
            try:
                fetched = fetch_ds_connection(base_url, token, desired_ds_name)
                if isinstance(fetched, dict) and fetched:
                    ds_payload = fetched
            except Exception:
                pass
            out = {}
            ds_id = existing_ds_id
        else:
            out = put_ds_connection(base_url, token, desired_ds_name, ds_payload)
        # PUT response often does not include DS internal id; fetch after PUT.
        ds_id = _extract_ds_id_from_payload(out) or _extract_id(out, ["_id", "id", "dsId", "ds_id"]) or _extract_id(out, ["ds_connection", "_id", "id"])
        try:
            fetched = fetch_ds_connection(base_url, token, desired_ds_name)
            if isinstance(fetched, dict) and fetched:
                ds_payload = fetched  # use canonical server payload for test
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
            # ds_payload was either {"ds_connection": {...}, ...} or connector payload.
            ds_conn_obj = ds_payload.get("ds_connection") if isinstance(ds_payload, dict) else None
            if not isinstance(ds_conn_obj, dict):
                ds_conn_obj = ds_payload if isinstance(ds_payload, dict) else {}

            # Ensure DS connection object has the runtime name.
            ds_conn_obj = dict(ds_conn_obj)
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
            for attempt in range(1, max_attempts + 1):
                broadcast = f"{desired_ds_name}-test-connection-complete-{uuid.uuid4()}"[:250]
                test_started = _now_utc()
                test_ds_connection_auto(
                    base_url,
                    token,
                    ds_name=desired_ds_name,
                    ds_id=ds_id_val,
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
        set_step_error(engine, step_id, f"DS connection test failed: {ex}")
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
                set_step_error(engine, step_id, f"Scan profile create failed: {ex}")
                set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
                set_run_next_eligible(engine, run_id, _now_utc())
                return True
        else:
            logger.error("WORKFLOW_STEP_SCAN_PROFILE_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
            set_step_error(engine, step_id, f"Scan profile create failed: {ex}")
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
                set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} already_done scan={prev_sid} terminal={prev_status}", started=False)
                set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
                return True

        # Hard safety: if any scan is already running in this env, do NOT start another.
        try:
            if env_has_running_scan(base_url, token, run_id=run_id):
                set_step_status(engine, step_id, 'scan_waiting', last_error='Env already has a running scan; waiting')
                set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} blocked_by_existing_running_scan', started=False)
                return True
        except Exception as ex:
            # Logic Hardening: Treat API check failure as BUSY
            set_step_status(engine, step_id, 'scan_waiting', last_error=f'Safety check failed before trigger (API error), waiting: {ex}')
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

        # Preferred (matches MegaScale): trigger by scan profile name (no scheduling).
        out3: Dict[str, Any] = {}
        if scan_profile_name:
            try:
                out3 = trigger_scan(base_url, token, scan_profile_name, scan_type="dsScan")
            except Exception as trig_ex:
                # Fallback for older deployments: try starting via scanProfileId endpoints.
                if not spid:
                    raise RuntimeError(f"Trigger by scan profile name failed and no scan_profile_id to fallback: {trig_ex}")
                out3 = start_scan_profile(base_url, token, spid)
        else:
            # No name stored; fallback to id-based start.
            out3 = start_scan_profile(base_url, token, spid)
        scan_id = _extract_id(out3, ["scanId", "scan_id", "parentScanId", "parent_scan_id"])
        if not scan_id:
            # Fallback: infer from running parent scans list
            wf_tag = f"(wf {int(run_id)}-{int(seq)})"
            scan_id = guess_latest_scan_id(base_url, token, name_contains=scan_profile_name, wf_tag=wf_tag) or guess_latest_scan_id(base_url, token, name_contains=desired_ds_name, wf_tag=wf_tag)
        if scan_id:
            logger.info("WORKFLOW_STEP_SCAN_STARTED run_id=%s step_seq=%s scan_id=%s", run_id, seq, scan_id)
            set_step_status(engine, step_id, "scan_running", scan_id=str(scan_id))
        else:
            logger.info("WORKFLOW_STEP_SCAN_STARTED run_id=%s step_seq=%s (no scan_id capture)", run_id, seq)
            # We don't always get scan_id from start response; keep running but runner will still wait if possible.
            set_step_status(engine, step_id, "scan_running")
    except Exception as ex:
        logger.error("WORKFLOW_STEP_SCAN_TRIGGER_FAILED run_id=%s step_seq=%s error=%s", run_id, seq, ex)
        set_step_error(engine, step_id, f"Scan start failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: scan start failed")
        _set_env_busy(engine, env_id, run_id, False) # RELEASE
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

    # Non-blocking: do NOT wait for completion here.
    # We'll reconcile terminal scans at the top of the loop using scan_parents.
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
