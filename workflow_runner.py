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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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

# Conservative request timeout so one hung call can't freeze the workflow.
HTTP_TIMEOUT = float(os.getenv("WORKFLOW_HTTP_TIMEOUT_SECONDS", "45") or "45")

RUNNING_STATES = {"started", "awaiting", "inprogress", "in progress", "running", "queued", "pending", "stoprequested", "stop requested", "stopping"}
TERMINAL_STATES = {"failed", "completedwithfailures", "completed", "stopped", "aborted", "cancelled", "canceled"}


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


def _request(method: str, url: str, token: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> requests.Response:
    headers_raw = {"authorization": token} if token else {}
    headers_bearer = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_raw, timeout=HTTP_TIMEOUT)
    if resp.status_code in (401, 403) and token:
        resp = requests.request(method, url, params=params or {}, json=json_body, headers=headers_bearer, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
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
    resp = _request("GET", url, token)
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


def _getenv_any(names: List[str]) -> str:
    for n in names:
        v = str(os.getenv(n, "") or "").strip()
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
    if k == "smb":
        out["SMB_username"] = _getenv_any(["SMB_username", "SMB_USERNAME", "smb_username", "smb_USERNAME"])
        out["SMB_password"] = _getenv_any(["SMB_password", "SMB_PASSWORD", "smb_password"])
        out["SMB_password_enc"] = _getenv_any(["SMB_password_enc", "SMB_PASSWORD_ENC", "smb_password_enc"])
    elif k == "s3":
        out["S3_accessKey"] = _getenv_any(["S3_accessKey", "S3_ACCESSKEY", "S3_ACCESS_KEY", "s3_accessKey"])
        out["S3_secret_key"] = _getenv_any(["S3_secret_key", "S3_SECRET_KEY", "S3_secretKey", "s3_secret_key"])
        out["S3_secret_key_enc"] = _getenv_any(["S3_secret_key_enc", "S3_SECRET_KEY_ENC", "s3_secret_key_enc"])
        # Support alternate naming the user mentioned (tenantid etc) without forcing it.
        out["S3_tenantid"] = _getenv_any(["S3_tenantid", "S3_TENANTID", "s3_tenantid"])
    elif k == "postgres":
        out["Postgres_username"] = _getenv_any(["Postgres_username", "POSTGRES_username", "POSTGRES_USERNAME", "postgres_username"])
        out["Postgres_password"] = _getenv_any(["Postgres_password", "POSTGRES_password", "POSTGRES_PASSWORD", "postgres_password"])
    elif k == "mssql":
        out["MSSQL_username"] = _getenv_any(["MSSQL_username", "MSSQL_USERNAME", "mssql_username"])
        out["MSSQL_password"] = _getenv_any(["MSSQL_password", "MSSQL_PASSWORD", "mssql_password"])
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


def _apply_build_prefix(name: str, build_prefix: str) -> str:
    nm = str(name or "").strip()
    bp = str(build_prefix or "").strip()
    if not bp:
        return nm
    # Avoid double prefix
    if nm.startswith(bp + " "):
        return nm
    return f"{bp} {nm}".strip()


def set_run_next_eligible(engine, run_id: int, next_dt: datetime) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET next_eligible_at=:t, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
            {"t": next_dt, "rid": int(run_id)},
        )
        conn.commit()


def load_env(engine, env_id: int) -> Dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, env_name, base_url, auth_module_path, auth_function_name, classifiers_api_path, is_active
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
        "auth_module_path": row[3],
        "auth_function_name": row[4],
        "classifiers_api_path": row[5],
        "is_active": row[6],
    }


def _render_template(text_value: str, variables: Dict[str, str]) -> str:
    s = str(text_value or "")
    for k, v in variables.items():
        s = s.replace("{{" + str(k) + "}}", str(v))
    return s


def _action_pre_ols_key(env_id: Optional[int]) -> List[str]:
    """
    Prefer env-specific action, fallback to global.
    """
    keys = []
    if env_id is not None:
        keys.append(f"action::pre_ols::env={int(env_id)}")
    keys.append("action::pre_ols")
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


def create_scan_profile(base_url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = build_api_url(base_url, SCAN_PROFILES_PATH)
    resp = _request("POST", url, token, json_body=payload)
    return resp.json() if resp.content else {}


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


def guess_latest_scan_id(base_url: str, token: str, *, name_contains: str = "", limit: int = 80) -> str:
    """
    If start response doesn't include scan_id, try to find the newest running parent scan
    matching a name substring.
    """
    needle = str(name_contains or "").strip().lower()
    try:
        items = fetch_parent_scans(base_url, token, states=["Started", "Awaiting", "In Progress", "InProgress", "Running", "Queued", "Pending", "Stop Requested", "StopRequested", "Stopping"], limit=int(limit), skip=0)
    except Exception:
        items = []
    for it in items or []:
        sid = str(it.get("_id") or it.get("id") or "").strip()
        nm = str(it.get("name") or it.get("scanName") or it.get("displayName") or "").strip().lower()
        if not sid:
            continue
        if needle and needle not in nm:
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
        time.sleep(max(5, int(poll_seconds)))
    return last_state or "unknown", True


def _extract_id(payload: Dict[str, Any], keys: List[str]) -> str:
    v = _deep_find(payload, keys)
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for kk in ["_id", "id", "scanId", "scan_id", "parentScanId", "parent_scan_id"]:
            if isinstance(v.get(kk), str) and v.get(kk).strip():
                return str(v.get(kk)).strip()
    return ""


def run_once(engine, *, quiet: bool = False) -> bool:
    """
    Executes at most one step across all runs.
    Returns True if any work was done.
    """
    ensure_workflow_tables(engine)
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
        return False

    run_id = int(run_row[0])
    status = str(run_row[1] or "").strip().lower()
    if status != "running":
        return False
    try:
        cooldown_m = int(run_row[2]) if run_row[2] is not None else 15
    except Exception:
        cooldown_m = 15
    next_eligible = run_row[3]
    if next_eligible is not None and isinstance(next_eligible, datetime):
        if next_eligible > now:
            return False

    # Find next pending step for this run
    with engine.connect() as conn:
        step = conn.execute(
            text("""
                SELECT
                    id, seq, environment_id, ds_kind, ds_name,
                    scan_kind, file_set_type, is_acl, is_ocr, is_structured_ff, is_ols,
                    feature_flags, notes, ds_id, scan_profile_id, scan_profile_name, build_version, build_prefix, scan_id, status
                FROM workflow_steps
                WHERE run_id = :rid
                  AND COALESCE(status, 'pending') IN ('pending', 'error')
                ORDER BY seq ASC
                LIMIT 1
            """),
            {"rid": int(run_id)},
        ).fetchone()
    if not step:
        # All done
        with engine.connect() as conn:
            conn.execute(text("UPDATE workflow_runs SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE id=:rid"), {"rid": int(run_id)})
            conn.commit()
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
    except Exception as ex:
        set_step_error(engine, step_id, f"Auth failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: auth failed")
        return True

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

    set_step_status(engine, step_id, "running", started_at=now)

    # Build prefix should be refreshed when workflow starts / step executes.
    try:
        build_version = resolve_env_build_version(env_row, token)
    except Exception:
        build_version = ""
    build_prefix = _build_prefix(build_version)

    desired_ds_name = _apply_build_prefix(str(step_dict.get("ds_name") or f"{step_dict.get('ds_kind') or ''} step{seq}").strip(), build_prefix)
    try:
        # Persist to DB so UI shows build-prefixed names.
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

    # --- Pre-OLS action (feature flag change) ---
    if bool(step_dict.get("is_ols")):
        if not _run_is_running(engine, int(run_id)):
            return True
        try:
            action_tpl = ""
            for k in _action_pre_ols_key(int(env_id) if env_id is not None else None):
                action_tpl = get_template(engine, k)
                if action_tpl:
                    break
            if action_tpl:
                action_raw = _render_template(
                    action_tpl,
                    {
                        "ENV_ID": str(env_id or ""),
                        "DS_NAME": desired_ds_name,
                        "DS_KIND": str(step[3] or "").strip(),
                        "FILE_SET_TYPE": str(step[6] or "").strip(),
                        "BUILD_VERSION": build_version,
                        "BUILD_PREFIX": build_prefix,
                    },
                )
                action_payload = json.loads(action_raw)
                run_action_template(base_url, token, action_payload)
                set_step_status(engine, step_id, "pre_ols_done")
        except Exception as ex:
            # Don't block forever: mark error so user can fix template/payload.
            set_step_error(engine, step_id, f"Pre-OLS action failed: {ex}")
            set_run_error(engine, run_id, f"Step {seq}: pre-OLS action failed")
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
        out = put_ds_connection(base_url, token, desired_ds_name, ds_payload)
        ds_id = _extract_id(out, ["_id", "id", "dsId", "ds_id"])
        if not ds_id:
            # Some responses embed ds_connection
            ds_id = _extract_id(out, ["ds_connection", "_id", "id"])
        if ds_id:
            set_step_status(engine, step_id, "ds_created", ds_id=str(ds_id))
        else:
            set_step_status(engine, step_id, "ds_created")
    except Exception as ex:
        set_step_error(engine, step_id, f"DS create failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: DS create failed")
        return True

    # We need ds_id for scan template variables; prefer stored column now.
    with engine.connect() as conn:
        row = conn.execute(text("SELECT ds_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
    ds_id_val = str(row[0] or "").strip() if row else ""

    # --- Scan profile create ---
    if not _run_is_running(engine, int(run_id)):
        return True
    scan_key = _workflow_scan_key(step_dict)
    scan_tpl = get_template(engine, scan_key)
    if not scan_tpl:
        set_step_error(engine, step_id, f"Missing Scan template for key: {scan_key}")
        set_run_error(engine, run_id, f"Missing Scan template: {scan_key}")
        return True
    scan_profile_name = f"{desired_ds_name} - {str(step[12] or '').strip()}".strip()[:240] or f"workflow run {run_id} step {seq}"
    try:
        scan_payload_raw = _render_template(
            scan_tpl,
            {
                "SCAN_PROFILE_NAME": scan_profile_name,
                "NOTES": str(step[12] or "").strip(),
                "DS_ID": ds_id_val,
                "SCAN_TEMPLATE_ID": resolve_default_scan_template_id(engine, int(env_id) if env_id is not None else None, base_url, token),
                "ENV_ID": str(env_id or ""),
                "DS_NAME": desired_ds_name,
                "DS_KIND": str(step[3] or "").strip(),
                "FILE_SET_TYPE": str(step[6] or "").strip(),
                "IS_ACL": "1" if bool(step_dict.get("is_acl")) else "0",
                "IS_OLS": "1" if bool(step_dict.get("is_ols")) else "0",
                "IS_STRUCTURED_FF": "1" if bool(step_dict.get("is_structured_ff")) else "0",
                "FEATURE_FLAGS": str(step_dict.get("feature_flags") or "").strip(),
                "BUILD_VERSION": build_version,
                "BUILD_PREFIX": build_prefix,
            },
        )
        unresolved = _unresolved_placeholders(scan_payload_raw)
        if unresolved:
            raise RuntimeError(f"Unresolved template placeholders: {', '.join(unresolved[:20])}")
        scan_payload = json.loads(scan_payload_raw)
        out2 = create_scan_profile(base_url, token, scan_payload)
        scan_profile_id = _extract_id(out2, ["_id", "id", "scanProfileId", "scan_profile_id"])
        if not scan_profile_id:
            scan_profile_id = _extract_id(out2, ["scanProfile", "_id", "id"])
        if not scan_profile_id:
            raise RuntimeError("scan profile id not found in response")
        set_step_status(engine, step_id, "scan_created", scan_profile_id=str(scan_profile_id), scan_profile_name=scan_profile_name)
    except Exception as ex:
        set_step_error(engine, step_id, f"Scan profile create failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: scan profile create failed")
        return True

    # --- Start scan profile & capture parent scan id ---
    if not _run_is_running(engine, int(run_id)):
        return True
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT scan_profile_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
        spid = str(row[0] or "").strip() if row else ""
        out3 = start_scan_profile(base_url, token, spid)
        scan_id = _extract_id(out3, ["scanId", "scan_id", "parentScanId", "parent_scan_id", "_id", "id"])
        if not scan_id:
            # Fallback: infer from running parent scans list
            scan_id = guess_latest_scan_id(base_url, token, name_contains=desired_ds_name or scan_profile_name)
        if scan_id:
            set_step_status(engine, step_id, "scan_running", scan_id=str(scan_id))
        else:
            # We don't always get scan_id from start response; keep running but runner will still wait if possible.
            set_step_status(engine, step_id, "scan_running")
    except Exception as ex:
        set_step_error(engine, step_id, f"Scan start failed: {ex}")
        set_run_error(engine, run_id, f"Step {seq}: scan start failed")
        return True

    # Wait for completion if we have scan_id.
    with engine.connect() as conn:
        row = conn.execute(text("SELECT scan_id FROM workflow_steps WHERE id=:id"), {"id": int(step_id)}).fetchone()
    sid = str(row[0] or "").strip() if row else ""
    if sid:
        final_state, done = wait_for_scan_terminal_or_pause(
            engine,
            int(run_id),
            base_url,
            token,
            sid,
            poll_seconds=int(os.getenv("WORKFLOW_SCAN_POLL_SECONDS", "60") or "60"),
        )
        if done and _is_terminal_state(final_state):
            set_step_status(engine, step_id, "completed", finished_at=_now_utc())
            if not quiet:
                print(f"[{_now_utc().isoformat()}Z] run={run_id} step={seq} scan={sid} done state={final_state}", flush=True)
        else:
            # Paused/stopped while scan is running; do not advance.
            if not quiet:
                print(f"[{_now_utc().isoformat()}Z] run={run_id} step={seq} scan={sid} waiting_stopped state={final_state}", flush=True)
            return True
    else:
        # Can't wait; mark as created and rely on reconcile.
        set_step_status(engine, step_id, "scan_running")
        if not quiet:
            print(f"[{_now_utc().isoformat()}Z] run={run_id} step={seq} scan_id_missing (will reconcile)", flush=True)

    # Enforce cooldown before next step
    set_run_next_eligible(engine, run_id, _now_utc() + timedelta(minutes=float(cooldown_m)))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-seconds", type=int, default=30, help="Runner loop interval (default: 30)")
    ap.add_argument("--once", action="store_true", help="Run one step max and exit")
    ap.add_argument("--quiet", action="store_true", help="Minimal stdout")
    args = ap.parse_args()

    engine = create_engine(DB_CONNECTION_STRING)
    # Ensure we can connect
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    if args.once:
        did = run_once(engine, quiet=bool(args.quiet))
        return 0 if did else 0

    while True:
        try:
            did = run_once(engine, quiet=bool(args.quiet))
            if not did:
                time.sleep(max(5, int(args.interval_seconds)))
            else:
                # If we did work, loop quickly to pick up pause/cooldown.
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
        except Exception as ex:
            if not args.quiet:
                print(f"[{_now_utc().isoformat()}Z] runner_error: {ex}", flush=True)
            time.sleep(max(10, int(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())

