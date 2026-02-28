"""
Workflow runner for DS + scan automation.

Reads `workflow_runs` + `workflow_steps` produced by the Streamlit UI
and executes them sequentially:
  - create/update DS connection
  - create scan profile
  - start scan (best-effort across BigID versions)
  - wait until scan reaches a terminal state
  - enforce cooldown between steps (default 15 minutes)

Secrets are not hardcoded: DS and scan payloads are stored in DB table
`workflow_templates` as JSON strings (with {{PLACEHOLDER}} variables).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import create_engine, text

from db_config import DB_CONNECTION_STRING
from bigid_auth import Get_auth_token


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

RUNNING_STATES = {"started", "awaiting", "inprogress", "in progress", "running", "queued", "pending", "stoprequested", "stop requested", "stopping"}
TERMINAL_STATES = {"failed", "completedwithfailures", "completed", "stopped", "aborted", "cancelled", "canceled"}

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
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_dir), stderr=subprocess.DEVNULL, timeout=1.5)
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
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS tracker_kv (
                        k VARCHAR(200) PRIMARY KEY,
                        v VARCHAR(2000) NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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
            else:
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


def resolve_env_build_version(env_row: Dict[str, Any], token: str) -> str:
    """
    Prefer /about; fallback to test_environments.version; fallback empty.
    """
    base_url = str(env_row.get("base_url") or "").strip()
    bv = ""
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
    return bv


def ensure_workflow_tables(engine) -> None:
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
    with engine.connect() as conn:
        if "sqlite" in dialect:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NULL,
                    status TEXT NULL,
                    env1_id INTEGER NULL,
                    env2_id INTEGER NULL,
                    cooldown_minutes INTEGER DEFAULT 15,
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
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
            return

        # MySQL / others
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NULL,
                status VARCHAR(32) NULL,
                env1_id INT NULL,
                env2_id INT NULL,
                cooldown_minutes INT DEFAULT 15,
                next_eligible_at DATETIME NULL,
                last_error TEXT NULL,
                started_at DATETIME NULL,
                finished_at DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_workflow_runs_status (status)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_steps (
                id INT AUTO_INCREMENT PRIMARY KEY,
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
                started_at DATETIME NULL,
                finished_at DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_workflow_step (run_id, seq),
                INDEX idx_workflow_steps_run_seq (run_id, seq),
                INDEX idx_workflow_steps_status (status)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                k VARCHAR(200) PRIMARY KEY,
                template_kind VARCHAR(32) NULL,
                payload_json LONGTEXT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_workflow_templates_kind (template_kind)
            )
        """))
        # Non-breaking upgrades
        for stmt in [
            "ALTER TABLE workflow_steps ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE",
            "ALTER TABLE workflow_steps ADD COLUMN scan_profile_name VARCHAR(500) NULL",
            "ALTER TABLE workflow_steps ADD COLUMN build_version VARCHAR(50) NULL",
            "ALTER TABLE workflow_steps ADD COLUMN build_prefix VARCHAR(20) NULL",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        conn.commit()


def fetch_parent_scans(base_url: str, token: str, states: Optional[List[str]] = None, *, limit: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
    """
    Best-effort parent scans listing using BigID filter query params.
    """
    url = build_api_url(base_url, PARENT_SCANS_PATH)
    cleaned_states = [str(x).strip() for x in (states or []) if str(x).strip()]
    filter_items: List[Dict[str, Any]] = []
    if cleaned_states:
        filter_items.append({"field": "state", "operator": "in", "value": cleaned_states})
    params = {
        "skip": int(skip),
        "limit": int(limit),
        "requireTotalCount": "true",
        "filter": json.dumps(filter_items, separators=(",", ":")),
        "sort": json.dumps([{"field": "_id", "order": "desc"}], separators=(",", ":")),
    }
    resp = _request("GET", url, token, params=params)
    payload = resp.json() if resp.content else {}
    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        data_block = payload.get("data")
        root = data_block if isinstance(data_block, dict) else payload
        for key in ["scanParents", "parentScans", "scans", "results", "items"]:
            val = root.get(key)
            if isinstance(val, list):
                items = [x for x in val if isinstance(x, dict)]
                break
    return items


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
    Find a running parent scan id by exact scan name.
    If wf_tag is provided (e.g. "(wf 6-3)"), require it in the name too.
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
    for it in items or []:
        if not isinstance(it, dict):
            continue
        sid = str(it.get('_id') or it.get('id') or '').strip()
        name = _scan_name_from_parent_scan(it).strip()
        if not sid or not name:
            continue
        nml = name.lower()
        if nml != needle:
            continue
        if wf and wf not in nml:
            continue
        return sid
    return ""


def _is_workflow_scan_name(name: str, *, run_id: Optional[int] = None) -> bool:
    nm = str(name or '').strip()
    if not nm:
        return False
    if run_id is not None:
        if f'(wf {int(run_id)}-' in nm:
            return True
    return '(wf ' in nm


def env_has_running_scan(base_url: str, token: str, *, run_id: Optional[int] = None) -> bool:
    """
    Hard safety. By default, only blocks if a *workflow-created* scan is running
    (identified by name containing '(wf ... )').

    Set WORKFLOW_BLOCK_ANY_RUNNING_SCAN=1 to block on any running scan.
    """
    block_any = str(os.getenv('WORKFLOW_BLOCK_ANY_RUNNING_SCAN', '0') or '0').strip().lower() in {'1','true','yes','y','on'}
    try:
        states = [
            'Started','Awaiting','In Progress','InProgress','Running',
            'Stop Requested','StopRequested','Stopping','Queued','Pending'
        ]
        scans = fetch_parent_scans(base_url, token, states=states, limit=50, skip=0)
    except Exception:
        return False

    for sc in scans or []:
        if not isinstance(sc, dict):
            continue
        st = str(sc.get('state') or sc.get('status') or '').strip()
        prog = sc.get('progress')
        pst = ''
        if isinstance(prog, dict):
            pst = str(prog.get('state') or '').strip()
        running = (_compact(st) in RUNNING_STATES) or (_compact(pst) in RUNNING_STATES)
        if not running:
            continue
        if block_any:
            return True
        nm = _scan_name_from_parent_scan(sc)
        if _is_workflow_scan_name(nm, run_id=run_id):
            return True
    return False


def _workflow_ds_key(step: Dict[str, Any]) -> str:
    ds_kind = str(step.get("ds_kind") or "").strip().lower()
    fs = str(step.get("file_set_type") or "").strip() or "default"
    acl = 1 if bool(step.get("is_acl")) else 0
    struct = 1 if bool(step.get("is_structured_ff")) else 0
    return f"ds::{ds_kind}::{fs}::acl={acl}::struct={struct}"


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
        else:
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


def resolve_secret_vars(ds_kind: str) -> Dict[str, str]:
    """
    Pull secrets from environment variables (user will put in .env).
    Keys match placeholders used by import_bigid_datasources.py.
    """
    k = str(ds_kind or "").strip().lower()
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
    return out


def _unresolved_placeholders(s: str) -> List[str]:
    try:
        import re
        return sorted(set(re.findall(r"\{\{([A-Za-z0-9_:\-]+)\}\}", str(s or ""))))
    except Exception:
        return []

def resolve_default_scan_template_id(engine, env_id: Optional[int], base_url: str, token: str) -> str:
    keys = []
    if env_id is not None:
        keys.append(f"config::scan_template_id::env={int(env_id)}")
    keys.append("config::scan_template_id")
    for k in keys:
        v = str(get_template(engine, k) or "").strip()
        if v:
            return v
    # Env var fallback
    env_v = str(os.getenv("WORKFLOW_DEFAULT_SCAN_TEMPLATE_ID", "") or "").strip()
    if env_v:
        return env_v
    # Automatic fallback: fetch scan templates and pick "Data source scan"
    try:
        items = fetch_scan_templates(base_url, token, limit=200, skip=0)
        for it in items or []:
            nm = str(it.get("name") or "").strip().lower()
            if nm == "data source scan":
                tid = str(it.get("_id") or it.get("id") or "").strip()
                if tid:
                    if env_id is not None:
                        upsert_template_value(engine, f"config::scan_template_id::env={int(env_id)}", "config", tid)
                    else:
                        upsert_template_value(engine, "config::scan_template_id", "config", tid)
                    return tid
    except Exception:
        pass
    return ""


def run_action_template(base_url: str, token: str, action_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a generic API call described by JSON:
      {
        "method": "POST",
        "path": "/api/v1/...",
        "params": {...},        # optional
        "body": {...}           # optional
      }
    """
    if not isinstance(action_payload, dict):
        raise RuntimeError("action template must be a JSON object")
    method = str(action_payload.get("method") or "POST").strip().upper()
    path = str(action_payload.get("path") or "").strip()
    if not path:
        raise RuntimeError("action template missing 'path'")
    params = action_payload.get("params")
    body = action_payload.get("body")
    url = build_api_url(base_url, path)
    resp = _request(method, url, token, params=(params if isinstance(params, dict) else None), json_body=body)
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {}


def set_run_error(engine, run_id: int, message: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET last_error=:e, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
            {"e": str(message)[:2000], "rid": int(run_id)},
        )
        conn.commit()


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


def ensure_runner_heartbeat_table(engine) -> None:
    """
    Ensure tracker_heartbeat exists so the runner can report liveness + last action.
    (Table is also used by tracker.py; we create it here for standalone runner usage.)
    """
    try:
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
            if "sqlite" in dialect:
                conn.execute(
                    text("""
                        INSERT INTO tracker_heartbeat (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
                        VALUES (:n, :sa, :fa, :s, :m)
                        ON CONFLICT(name) DO UPDATE SET
                            last_started_at_utc=COALESCE(excluded.last_started_at_utc, tracker_heartbeat.last_started_at_utc),
                            last_finished_at_utc=COALESCE(excluded.last_finished_at_utc, tracker_heartbeat.last_finished_at_utc),
                            last_status=COALESCE(excluded.last_status, tracker_heartbeat.last_status),
                            last_message=COALESCE(excluded.last_message, tracker_heartbeat.last_message)
                    """),
                    {"n": RUNNER_HEARTBEAT_NAME, "sa": started_at, "fa": finished_at, "s": stv, "m": msg},
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO tracker_heartbeat (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
                        VALUES (:n, :sa, :fa, :s, :m)
                        ON DUPLICATE KEY UPDATE
                            last_started_at_utc=COALESCE(VALUES(last_started_at_utc), last_started_at_utc),
                            last_finished_at_utc=COALESCE(VALUES(last_finished_at_utc), last_finished_at_utc),
                            last_status=COALESCE(VALUES(last_status), last_status),
                            last_message=COALESCE(VALUES(last_message), last_message)
                    """),
                    {"n": RUNNER_HEARTBEAT_NAME, "sa": started_at, "fa": finished_at, "s": stv, "m": msg},
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
        if fs in {"Small Files", "Large Files"} and is_acl:
            nm += " With ACL"
        if is_struct:
            nm += " Structured Files Discovery"
        if is_ols:
            nm += " With OLS"
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
            conn.execute(text('UPDATE test_environments SET version=:v WHERE id=:id'), {'v': bv, 'id': int(env_id)})
            try:
                conn.execute(
                    text('INSERT INTO test_environment_version_history (environment_id, version_value, change_note) VALUES (:eid, :v, :n)'),
                    {'eid': int(env_id), 'v': bv, 'n': 'workflow_runner:/about'}
                )
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


def load_env(engine, env_id: int) -> Dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, env_name, base_url, version, auth_module_path, auth_function_name, classifiers_api_path, is_active
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
    k = str(ds_kind or "").strip().lower()
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
            if sk_enc:
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
        if pw_enc:
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
            if pw_enc:
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

    # Common connector fields for unstructured connectors (s3/smb)
    if ds_kind in {"s3", "smb"}:
        # BigID sometimes stores these as strings "true"/"false"
        ds_conn["metadataAclScanEnabled"] = "true" if is_acl else "false"
        ds_conn["dsAclScanEnabled"] = "true" if is_acl else "false"
        trp = ds_conn.get("typeRequestProperties")
        if isinstance(trp, dict):
            trp["enableStructuredFilesDiscovery"] = bool(is_struct)
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
        cst = item.get("connectionStatusTest") or item.get("connection_status_test")
        if isinstance(cst, dict) and (cst.get("is_success") is True or str(cst.get("is_success") or "").strip().lower() in ("true", "1", "yes")):
            return True
        if item.get("lastTestSuccess") is True or str(item.get("connection_status") or "").strip().lower() in ("connected", "success"):
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
            cst = dc.get("connectionStatusTest") if isinstance(dc, dict) else None
            if cst is None and (_now_utc() >= grace_decide_at):
                # BigID often does not return connectionStatusTest in GET for MSSQL/S3/SMB. Test was already triggered (POST).
                # Default: proceed so workflow continues; set WORKFLOW_DS_TEST_STRICT=1 to require connectionStatusTest.
                if os.getenv("WORKFLOW_DS_TEST_STRICT", "").strip().lower() in ("1", "true", "yes"):
                    return False, "connectionStatusTest missing; cannot confirm success (DS may stay Draft in BigID)"
                return True, "no connectionStatusTest in response; test was triggered, proceeding so workflow can continue"
            if isinstance(cst, dict):
                last_conn = _parse_utc_dt(cst.get("last_connection"))
                if last_conn:
                    last_seen = last_conn
                # only decide once we see an updated last_connection after we started the test (use naive UTC for comparison)
                if last_conn and last_conn >= (_test_started - timedelta(seconds=2)):
                    is_success = cst.get("is_success")
                    if isinstance(is_success, bool):
                        return is_success, json.dumps({k: cst.get(k) for k in ["is_success", "last_connection", "is_connection_ever_succeeded"]}, separators=(",", ":"))[:500]
                    # if it's present but not bool, treat truthy strings
                    s = str(is_success or "").strip().lower()
                    if s in {"true", "1", "yes", "ok", "success"}:
                        return True, "success"
                    if s in {"false", "0", "no", "fail", "failed", "error"}:
                        return False, "failed"
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
                        return is_success, json.dumps({k: cst.get(k) for k in ["is_success", "last_connection", "is_connection_ever_succeeded"]}, separators=(",", ":"))[:500]
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
        return test_ds_connections_by_ids(base_url, token, ids=[did or name], broadcast_event=broadcast_event)
    except Exception:
        allow_fallback = str(os.getenv('WORKFLOW_DS_TEST_ALLOW_CLIENT_FALLBACK', '0') or '0').strip().lower() in {'1','true','yes','y','on'}
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
    candidates = [
        (f"{base}/{sid}/run", None),
        (f"{base}/{sid}/start", None),
        (f"{base}/{sid}/execute", None),
        (f"{base}/run", {"scanProfileId": sid}),
        (f"{base}/start", {"scanProfileId": sid}),
    ]
    last_ex = None
    for path, body in candidates:
        url = build_api_url(base_url, path)
        try:
            resp = _request("POST", url, token, json_body=(body or {}))
            return resp.json() if resp.content else {}
        except Exception as ex:
            last_ex = ex
            continue
    raise RuntimeError(f"Failed to start scan profile via known endpoints: {last_ex}")


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
        items = []

    # First pass: exact name match
    if needle:
        for it in items or []:
            sid = str(it.get("_id") or it.get("id") or "").strip()
            nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
            if sid and nm == needle:
                return sid

    # Second pass: contains match (optionally require wf tag)
    for it in items or []:
        sid = str(it.get("_id") or it.get("id") or "").strip()
        nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
        if not sid:
            continue
        if needle and needle not in nm:
            continue
        if wf and wf not in nm:
            continue
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
        for kk in ["_id", "id", "scanId", "scan_id", "parentScanId", "parent_scan_id"]:
            if isinstance(v.get(kk), str) and v.get(kk).strip():
                return str(v.get(kk)).strip()
    return ""


def _scenario_lock_key(ds_kind: Any, file_set_type: Any) -> str:
    """
    Lock key that prevents running the *same DS kind + file set* in parallel across envs.
    Examples:
      s3::Small Files
      smb::Large Files
    """
    dk = str(ds_kind or "").strip().lower()
    fs = str(file_set_type or "").strip() or "default"
    return f"{dk}::{fs}"


def run_once(engine, *, quiet: bool = False) -> bool:
    """
    Executes at most one step across all runs.
    Returns True if any work was done.
    """
    logger = _get_logger(quiet=quiet)
    logger.info('run_once: tick quiet=%s', bool(quiet))
    ensure_workflow_tables(engine)
    set_runner_heartbeat(engine, status="running", message="tick", started=False)
    ok_lock, lock_msg = acquire_runner_lock(engine, ttl_seconds=120)
    if not ok_lock:
        set_runner_heartbeat(engine, status="idle", message=lock_msg, started=False)
        return False
    now = _now_utc()
    with engine.connect() as conn:
        run_row = conn.execute(
            text("""
                SELECT id, status, cooldown_minutes, next_eligible_at
                FROM workflow_runs
                WHERE status = 'running'
                ORDER BY id ASC
                LIMIT 1
            """)
        ).fetchone()
    if not run_row:
        set_runner_heartbeat(engine, status="idle", message="no running workflows", started=False)
        return False

    run_id = int(run_row[0])
    status = str(run_row[1] or "").strip().lower()
    if status != "running":
        return False
    try:
        cooldown_m = int(run_row[2]) if run_row[2] is not None else 15
    except Exception:
        cooldown_m = 15

    # Reconcile in-flight scans for this run, and prevent starting multiple scans
    # on the same environment at once.
    inflight_envs: set[int] = set()
    try:
        with engine.connect() as conn:
            inflight_rows = conn.execute(
                text("""
                    SELECT ws.id, ws.seq, ws.environment_id, ws.scan_id, ws.status, sp.scan_status
                    FROM workflow_steps ws
                    LEFT JOIN scan_parents sp ON sp.scan_id = ws.scan_id
                    WHERE ws.run_id=:rid
                      AND COALESCE(ws.is_enabled,1)=1
                      AND COALESCE(ws.status,'') NOT IN ('completed','disabled')
                      AND (
                        (ws.scan_id IS NOT NULL AND TRIM(ws.scan_id) <> '')
                        OR COALESCE(ws.status,'') IN ('scan_running','scan_waiting')
                      )
                    ORDER BY ws.updated_at DESC
                """),
                {"rid": int(run_id)},
            ).fetchall()
        completed_any = False
        for rr in inflight_rows or []:
            step_id = int(rr[0])
            seq = int(rr[1])
            env_id = rr[2]
            sid = str(rr[3] or "").strip()
            st_status = str(rr[4] or "").strip().lower()
            scan_status = str(rr[5] or "").strip()
            if env_id is not None:
                try:
                    inflight_envs.add(int(env_id))
                except Exception:
                    pass
            if scan_status and _is_terminal_state(scan_status):
                if _compact(scan_status) in {"completed", "completedwithfailures"}:
                    set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
                else:
                    set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {scan_status}")
                completed_any = True
                set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} scan={sid} terminal={scan_status}", started=False)
            else:
                # Only normalize to scan_running when a scan_id exists; 'running' may just be DS/test/profile work.
                if sid and st_status != "scan_running":
                    set_step_status(engine, step_id, "scan_running")
        if completed_any:
            # After reconciling terminal scans, enforce cooldown timestamp.
            # IMPORTANT: do not return early — we may still be able to run a step on a different env.
            set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
    except Exception:
        pass

    # Find next step for this run.
    # Default behavior: continue past failures (do not retry errors forever).
    # Set WORKFLOW_RETRY_ERROR_STEPS=1 to retry errors instead.
    retry_errors = str(os.getenv("WORKFLOW_RETRY_ERROR_STEPS", "") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    skip_errors = not retry_errors
    if skip_errors:
        # Default: continue past failures and also resume half-done steps.
        # Terminal for our purposes: scan_created/completed/error/disabled.
        # NOTE: Some older rows may have status='disabled' with is_enabled NULL/1; always exclude by status too.
        where_status = "AND COALESCE(status, 'pending') NOT IN ('scan_created', 'completed', 'error', 'disabled') AND COALESCE(is_enabled, 1) = 1"
    else:
        # Retry mode: allow retrying errors.
        where_status = "AND COALESCE(status, 'pending') IN ('pending', 'error') AND COALESCE(status,'') <> 'disabled' AND COALESCE(is_enabled, 1) = 1"

    scan_running_exists = bool(inflight_envs)
    # Capacity guard: when all workflow envs already have scan_running, don't start creating/testing
    # the next DS/scan pipeline items. Only reconcile running scans until a slot frees up.
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(DISTINCT environment_id)
                FROM workflow_steps
                WHERE run_id=:rid
                  AND COALESCE(is_enabled,1)=1
                  AND environment_id IS NOT NULL
            """), {"rid": int(run_id)}).fetchone()
        env_count = int(row[0]) if row and row[0] is not None else 0
    except Exception:
        env_count = 0
    try:
        max_scans = int(os.getenv('WORKFLOW_MAX_CONCURRENT_SCANS', '') or 0)
    except Exception:
        max_scans = 0
    if max_scans <= 0:
        max_scans = env_count if env_count > 0 else 2
    scans_at_capacity = bool(len(inflight_envs) >= int(max_scans))


    # If there are no eligible steps left, mark the run completed immediately
    # (even if next_eligible_at is set in the future).
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f"""
                    SELECT 1
                    FROM workflow_steps
                    WHERE run_id=:rid
                      {where_status}
                    LIMIT 1
                """),
                {"rid": int(run_id)},
            ).fetchone()
        has_eligible = bool(row and row[0] == 1)
    except Exception:
        has_eligible = True
    if not has_eligible:
        with engine.connect() as conn:
            conn.execute(text("UPDATE workflow_runs SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE id=:rid"), {"rid": int(run_id)})
            conn.commit()
        set_runner_heartbeat(engine, status="idle", message=f"run {run_id} completed (no eligible steps)", started=False)
        return True

    # Cooldown gate:
    # If any environment is currently running a scan, do not block reconciliation.
    # Also, allow starting work on a different environment even during cooldown.
    next_eligible = run_row[3]
    if next_eligible is not None and isinstance(next_eligible, datetime):
        if next_eligible > now:
            # If we have an idle environment with eligible work, proceed despite cooldown.
            # Otherwise idle until cooldown expires.
            if not inflight_envs:
                set_runner_heartbeat(engine, status="idle", message=f"cooldown until {next_eligible.isoformat()}Z", started=False)
                return False

    # Scenario parallelism guard: don't start S3 Small Files variants together across envs, etc.
    avoid_parallel = str(os.getenv("WORKFLOW_AVOID_PARALLEL_SAME_SCENARIO", "1") or "1").strip().lower() in {"1", "true", "yes", "y", "on"}
    running_keys: set[str] = set()
    if avoid_parallel:
        try:
            with engine.connect() as conn:
                rrows = conn.execute(
                    text("""
                        SELECT ds_kind, file_set_type
                        FROM workflow_steps
                        WHERE COALESCE(status,'') = 'scan_running'
                    """)
                ).fetchall()
            for rr in rrows or []:
                running_keys.add(_scenario_lock_key(rr[0], rr[1]))
        except Exception:
            running_keys = set()
    with engine.connect() as conn:
        candidates = conn.execute(
            text(f"""
                SELECT
                    id, seq, environment_id, ds_kind, ds_name,
                    scan_kind, file_set_type, is_acl, is_ocr, is_structured_ff, is_ols,
                    feature_flags, notes, ds_id, scan_profile_id, scan_profile_name, build_version, build_prefix, scan_id, status
                FROM workflow_steps
                WHERE run_id = :rid
                  {where_status}
                ORDER BY seq ASC
                LIMIT 25
            """),
            {"rid": int(run_id)},
        ).fetchall()

    def _pick_candidate(*, allow_scan_running: bool) -> Any:
        # Pass 1: prefer non-waiting work on free envs
        for cand in candidates or []:
            cand_status = str(cand[19] or '').strip().lower()
            if cand_status == 'scan_waiting':
                continue
            if (not allow_scan_running) and cand_status == 'scan_running':
                continue
            if scans_at_capacity and (not allow_scan_running) and cand_status != 'scan_running':
                continue
            cand_env = cand[2]
            # Do not start a second scan/step on the same environment while a scan is in-flight.
            if cand_status not in {'scan_running'} and cand_env is not None:
                try:
                    if int(cand_env) in inflight_envs:
                        continue
                except Exception:
                    pass
            if avoid_parallel and running_keys:
                # Prevent starting *new* parallel scans for the same scenario.
                if cand_status not in {'scan_running', 'running'}:
                    key = _scenario_lock_key(cand[3], cand[6])  # ds_kind, file_set_type
                    if key in running_keys:
                        continue
            return cand

        # Pass 2: allow scan_waiting if nothing else is runnable
        for cand in candidates or []:
            cand_status = str(cand[19] or '').strip().lower()
            if cand_status != 'scan_waiting':
                continue
            if (not allow_scan_running) and cand_status == 'scan_running':
                continue
            if scans_at_capacity and (not allow_scan_running) and cand_status != 'scan_running':
                continue
            cand_env = cand[2]
            if cand_env is not None:
                try:
                    if int(cand_env) in inflight_envs:
                        continue
                except Exception:
                    pass
            if avoid_parallel and running_keys:
                key = _scenario_lock_key(cand[3], cand[6])  # ds_kind, file_set_type
                if key in running_keys:
                    continue
            return cand
        return None

    # Prefer running work on a FREE environment first.
    step = _pick_candidate(allow_scan_running=False)
    # If all envs are busy, fall back to selecting scan_running (for reconciliation/visibility).
    if not step:
        step = _pick_candidate(allow_scan_running=True)
    if not step:
        # If we had candidates but none were selectable due to scenario locks, just idle and try again later.
        if candidates and avoid_parallel and running_keys:
            set_runner_heartbeat(engine, status="idle", message="blocked by scenario lock (waiting for other scan_running steps)", started=False)
            return False
        # All done (no remaining eligible steps)
        with engine.connect() as conn:
            conn.execute(text("UPDATE workflow_runs SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE id=:rid"), {"rid": int(run_id)})
            conn.commit()
        set_runner_heartbeat(engine, status="idle", message=f"run {run_id} completed", started=False)
        return True

    step_id = int(step[0])
    seq = int(step[1])
    env_id = step[2]
    env_row = load_env(engine, int(env_id)) if env_id is not None else {}
    if not env_row or not str(env_row.get("base_url") or "").strip():
        set_step_error(engine, step_id, f"Missing environment/base_url for env_id={env_id}")
        set_run_error(engine, run_id, f"Step {seq}: missing environment config")
        return True

    base_url = str(env_row.get("base_url") or "").strip()
    try:
        token = Get_auth_token(base_url, env_row)
        # pipeline_guard: if this env already has a running workflow scan, do not create more DS/profiles.
    except Exception as ex:
        set_step_error(engine, step_id, f"Auth failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: auth failed")
        return True

    # Do not pipeline on a busy environment (prevents creating profiles when scan is actually still running).
    try:
        if env_has_running_scan(base_url, token, run_id=run_id) and str(cur_status or '').strip().lower() not in {'scan_running'}:
            set_step_status(engine, step_id, 'scan_waiting', last_error='Env has a running workflow scan; waiting')
            set_runner_heartbeat(engine, status='idle', message=f'run={run_id} step={seq} env_busy_wait', started=False)
            return True
    except Exception:
        pass

    step_dict = {
        "ds_kind": step[3],
        "ds_name": step[4],
        "scan_kind": step[5],
        "file_set_type": step[6],
        "is_acl": bool(step[7]),
        "is_ocr": bool(step[8]),
        "is_structured_ff": bool(step[9]),
        "is_ols": bool(step[10]),
        "feature_flags": step[11],
        "notes": step[12],
    }

    cur_status = str(step[19] or "").strip().lower()

    # If this step is already in scan_running/scan_waiting, NEVER trigger again.
    # Instead, try to recover the real running scan_id (DB or API) and keep waiting.
    try:
        sid_db = str(step[18] or "").strip()
    except Exception:
        sid_db = ""
    try:
        wf_tag = f"(wf {int(run_id)}-{int(seq)})"
    except Exception:
        wf_tag = "(wf )"

    if cur_status in {"scan_running", "scan_waiting"}:
        # If scan_id already known, just keep it in scan_running.
        if sid_db:
            set_step_status(engine, step_id, "scan_running", scan_id=sid_db)
            set_runner_heartbeat(engine, status="idle", message=f"run={run_id} step={seq} scan_running scan_id_known", started=False)
            return False
        # Try to recover scan_profile_name from DB
        try:
            with engine.connect() as conn:
                rowx = conn.execute(text("SELECT scan_profile_name, ds_name FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
            scan_profile_name_db = str((rowx[0] if rowx else '') or '').strip()
            ds_name_db = str((rowx[1] if rowx else '') or '').strip()
        except Exception:
            scan_profile_name_db = ""
            ds_name_db = ""

        # Prefer exact-name match on scan_profile_name (includes wf tag).
        recovered = ""
        if scan_profile_name_db:
            recovered = find_running_parent_scan_id(base_url, token, exact_name=scan_profile_name_db, wf_tag=wf_tag)
        # Fallback: if profile name missing, try DS name contains wf tag via hardened guess
        if (not recovered) and ds_name_db:
            recovered = guess_latest_scan_id(base_url, token, name_contains=ds_name_db, wf_tag=wf_tag)

        if recovered:
            set_step_status(engine, step_id, "scan_running", scan_id=str(recovered), last_error=None)
            set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} recovered_scan_id={recovered}", started=False)
            return True

        # Still unknown: keep waiting; do NOT pipeline or trigger.
        set_step_status(engine, step_id, "scan_waiting", last_error="scan running but scan_id not yet resolved; waiting")
        set_runner_heartbeat(engine, status="idle", message=f"run={run_id} step={seq} waiting_for_scan_id", started=False)
        return False

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
        sid = str(step[18] or "").strip()
        if sid:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT scan_status FROM scan_parents WHERE scan_id=:sid LIMIT 1"),
                    {"sid": sid},
                ).fetchone()
            stv = str(row[0] or "").strip() if row else ""
            if stv and _is_terminal_state(stv):
                if _compact(stv) in {"completed", "completedwithfailures"}:
                    set_step_status(engine, step_id, "completed", finished_at=_now_utc(), last_error=None)
                else:
                    set_step_status(engine, step_id, "error", finished_at=_now_utc(), last_error=f"Scan ended in state: {stv}")
                set_runner_heartbeat(engine, status="work", message=f"run={run_id} step={seq} scan={sid} terminal={stv}", started=False)
                set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
                return True
    except Exception:
        pass

    # Build prefix should be refreshed when workflow starts / step executes.
    try:
        build_version = resolve_env_build_version(env_row, token)
    except Exception:
        build_version = ""
    try:
        _update_env_version_in_db(engine, env_id, build_version)
    except Exception:
        pass
    try:
        logger.info('env_build_version env_id=%s env_name=%s build_version=%s', env_id, str(env_row.get('env_name') or ''), build_version)
    except Exception:
        pass
    build_prefix = _build_prefix(build_version)

    # IMPORTANT:
    # - DS name must be stable per step (avoid DS spam on retries).
    # - DS name must be unique across BigID.
    #
    # We do NOT rely on timestamps for uniqueness. We use build prefix + scenario + wf run/seq.
    existing_ds_name = str(step_dict.get("ds_name") or "").strip()
    base_ds_name = existing_ds_name or _standard_ds_base_name(step_dict)
    # Always apply build prefix (if we have it).
    base_with_prefix = _apply_build_prefix(base_ds_name, build_prefix) if build_prefix else base_ds_name
    # Ensure uniqueness per workflow run/step without timestamps.
    # Ensure we don't accumulate duplicate workflow suffixes on retries.
    _wf_suffix = f"(wf {run_id}-{seq})"
    base_with_prefix_clean = re.sub(r"(?:\s*\(wf\s+\d+\-\d+\)\s*)+$", "", str(base_with_prefix or '').strip()).strip()
    desired_ds_name = f"{base_with_prefix_clean} {_wf_suffix}".strip()
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

    # --- Pre-step action (feature flags on/off etc) ---
    # If a matching action template exists, run it before DS/scan creation.
    if not _run_is_running(engine, int(run_id)):
        return True
    try:
        action_tpl = ""
        action_key_used = ""
        env_int = int(env_id) if env_id is not None else None
        candidates: List[str] = []
        candidates.extend(_action_pre_step_keys(step_dict, env_int))
        # Backward-compat: for OLS steps, also allow the old pre_ols action key.
        if bool(step_dict.get("is_ols")):
            candidates.extend(_action_pre_ols_key(env_int))
        for k in candidates:
            action_tpl = get_template(engine, k)
            if action_tpl:
                action_key_used = k
                break
        if action_tpl:
            action_raw = _render_template(
                action_tpl,
                {
                    "ENV_ID": str(env_id or ""),
                    "DS_NAME": desired_ds_name,
                    "DS_KIND": str(step_dict.get("ds_kind") or "").strip(),
                    "FILE_SET_TYPE": str(step_dict.get("file_set_type") or "").strip(),
                    "IS_ACL": "1" if bool(step_dict.get("is_acl")) else "0",
                    "IS_OLS": "1" if bool(step_dict.get("is_ols")) else "0",
                    "IS_STRUCTURED_FF": "1" if bool(step_dict.get("is_structured_ff")) else "0",
                    "FEATURE_FLAGS": str(step_dict.get("feature_flags") or "").strip(),
                    "NOTES": str(step_dict.get("notes") or "").strip(),
                    "BUILD_VERSION": build_version,
                    "BUILD_PREFIX": build_prefix,
                    "ACTION_KEY": action_key_used,
                },
            )
            unresolved = _unresolved_placeholders(action_raw)
            if unresolved:
                raise RuntimeError(f"Unresolved action placeholders: {', '.join(unresolved[:20])}")
            action_payload = json.loads(action_raw)
            run_action_template(base_url, token, action_payload)
            set_step_status(engine, step_id, "pre_action_done")
    except Exception as ex:
        # Don't block forever: mark error so user can fix template/payload.
        set_step_error(engine, step_id, f"Pre-step action failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: pre-step action failed")
        # Move on to next step (do not pause entire run).
        set_run_next_eligible(engine, run_id, _now_utc())
        return True

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
        return True
    try:
        secret_vars = resolve_secret_vars(str(step_dict.get("ds_kind") or ""))
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
        ds_payload = _apply_runtime_secrets(str(step_dict.get("ds_kind") or ""), ds_payload, secret_vars)
        # Apply ACL/Structured flags at runtime (so one base template is enough).
        ds_payload = _apply_runtime_flags(step_dict, ds_payload)

        # Match BigID UI: PUT body must include isSavedAsDraft=false so DS is not left in Draft.
        if isinstance(ds_payload, dict):
            ds_payload = dict(ds_payload)
            ds_payload["isSavedAsDraft"] = False

        # Idempotency: if DS already exists for this step (ds_id stored) and name matches,
        # avoid re-PUT to prevent accidental duplicate DS artifacts.
        existing_ds_id = str(step[13] or '').strip()
        existing_ds_name_db = str(step[4] or '').strip()
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
        if ds_id:
            set_step_status(engine, step_id, "ds_created", ds_id=str(ds_id))
        else:
            set_step_status(engine, step_id, "ds_created")
    except Exception as ex:
        set_step_error(engine, step_id, f"DS create failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: DS create failed")
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
            set_step_status(engine, step_id, "ds_test_ok")
    except Exception as ex:
        set_step_error(engine, step_id, f"DS connection test failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: DS connection test failed")
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
            template_id = resolve_default_scan_template_id(engine, int(env_id) if env_id is not None else None, base_url, token)
            if not template_id:
                raise RuntimeError("No default scan template id resolved (expected 'Data source scan')")

            # Scan profile name uniqueness comes from DS name (which is unique per wf step).
            scan_kind_raw = str(step_dict.get("scan_kind") or "").strip().lower().replace(" ", "")
            scan_kind_label = "Full Scan" if scan_kind_raw in {"full_scan", "fullscan", "dsscan", "dsscanprofile", "dsscanjob"} else (str(step_dict.get("scan_kind") or "Scan").strip() or "Scan")
            scan_profile_name = (f"{desired_ds_name} {scan_kind_label}").strip()[:240] or f"{desired_ds_name}".strip()[:240]

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
                set_step_status(engine, step_id, "scan_created", scan_profile_id=str(scan_profile_id), scan_profile_name=scan_profile_name)
    except Exception as ex:
        # If the only issue is "name already in use", try to find and reuse it.
        msg = str(ex)
        if ("already in use" in msg.lower()) and scan_profile_name:
            try:
                found = find_scan_profile_id_by_name(base_url, token, scan_profile_name)
                if found:
                    set_step_status(engine, step_id, "scan_created", scan_profile_id=str(found), scan_profile_name=scan_profile_name)
                else:
                    raise ex
            except Exception:
                set_step_error(engine, step_id, f"Scan profile create failed: {ex}")
                set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
                set_run_next_eligible(engine, run_id, _now_utc())
                return True
        else:
            set_step_error(engine, step_id, f"Scan profile create failed: {ex}")
            set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
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
        except Exception:
            pass

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
            except Exception:
                # Fallback for older deployments: try starting via scanProfileId endpoints.
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
            set_step_status(engine, step_id, "scan_running", scan_id=str(scan_id))
        else:
            # We don't always get scan_id from start response; keep running but runner will still wait if possible.
            set_step_status(engine, step_id, "scan_running")
    except Exception as ex:
        set_step_error(engine, step_id, f"Scan start failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: scan start failed")
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

