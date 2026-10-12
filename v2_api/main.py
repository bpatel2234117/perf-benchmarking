from fastapi import FastAPI, Depends, HTTPException, Query, Body, status, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional
from datetime import datetime, timedelta, timezone
import json
import os
import hashlib
import threading
from pathlib import Path
import logging

import sys

logger = logging.getLogger(__name__)

# Ensure we can import from v2_api and the root project directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load repo-root .env before importing db_config (LEGACY_DASHBOARD_URL, Postgres_*, etc.)
try:
    from dotenv import load_dotenv

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
except Exception:
    pass

# Import our normalized Postgres config
from v2_api.db_config_pg import DB_CONNECTION_STRING
from v2_api.add_benchmark_math import (
    DEFAULT_SCANNER_COUNT,
    DEFAULT_THREAD_COUNT,
    compute_throughputs,
    duration_hours_between,
    infer_add_benchmark_defaults_from_scan_name,
    split_iso_for_display,
)
from v2_api.add_benchmark_prefetch import build_form_sections_meta
from v2_api.scan_bigid_sync import (
    _is_terminal_state,
    _load_subscan_ids_from_db,
    fetch_scan_parts_report_errors,
    resync_scan_parents_for_environment,
    sync_scan_insights,
)
from v2_api.tracker import sync_scan_part_records_for_scan
from bigid_auth import Get_auth_token
from v2_api.scan_progress_helpers import (
    fetch_live_classifier_hits_from_bigid,
    load_classifier_hits_from_db,
    load_scan_log_correlation,
    load_scan_log_errors_for_scan,
    load_scanner_performance_series,
    pg_column_exists,
    resolve_env_id_for_scan,
    resolve_scan_period_window,
)
from benchmark_scenario_normalizer import canonical_benchmark_fields

app = FastAPI(title="Scan Performance Benchmark API v2")
REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def _normalize_env_base_url(url: Optional[str]) -> str:
    from bigid_auth import normalize_bigid_base_url
    return normalize_bigid_base_url(str(url or "").strip())


def _repair_stored_base_url(engine, environment_id: int) -> str:
    """Persist canonical base_url (no trailing /, no double scheme) if DB value differs."""
    eid = int(environment_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT base_url FROM test_environments WHERE id = :eid LIMIT 1"),
            {"eid": eid},
        ).fetchone()
    if not row:
        return ""
    raw = str(row[0] or "").strip()
    canon = _normalize_env_base_url(raw)
    if canon and canon != raw:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE test_environments SET base_url = :u WHERE id = :eid"),
                {"u": canon, "eid": eid},
            )
    return canon or raw

# Setup CORS for the React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://10.2.20.89:5173",
        "http://10.2.20.89:5174",
        "http://10.2.20.89:5175",
        "http://10.2.0.93:5173",
        "http://10.2.0.93:5174",
        "http://10.2.0.93:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def api_health():
    """DB connectivity + schema patch status (for deploy debugging)."""
    out = {"ok": True, "schema": "unknown", "environments_count": None}
    try:
        _ensure_schema()
        out["schema"] = "applied"
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM test_environments")).scalar()
            out["environments_count"] = int(n or 0)
            b = conn.execute(text("SELECT COUNT(*) FROM benchmarks")).scalar()
            out["benchmarks_count"] = int(b or 0)
    except Exception as ex:
        out["ok"] = False
        out["schema"] = "failed"
        out["error"] = str(ex)[:500]
    return out


@app.get("/api/ui-config")
async def ui_config():
    """
    Public SPA config (no auth): Streamlit origin for legacy Open links (Add New Benchmark + scan_id).
    Match Streamlit URL shape: http://HOST:8501/?page=Add+New+Benchmark&scan_id=<id>
    Set LEGACY_DASHBOARD_URL in repo .env (alongside Postgres_*).
    """
    url = (
        os.getenv("LEGACY_DASHBOARD_URL", "").strip()
        or os.getenv("STREAMLIT_URL", "").strip()
        or os.getenv("STREAMLIT_DASHBOARD_URL", "").strip()
    )
    return {"legacy_dashboard_url": url}


# Database Setup (PostgreSQL — v2 API system of record for React + benchmarks)
engine = create_engine(
    DB_CONNECTION_STRING, 
    pool_pre_ping=True, 
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_size=10,
    max_overflow=20,
    isolation_level="AUTOCOMMIT",  # Each query auto-commits, no lingering transactions
    connect_args={"connect_timeout": 3}
)


def _ensure_schema() -> None:
    from v2_api.schema_bootstrap import apply_schema_migrations

    apply_schema_migrations(engine)


@app.on_event("startup")
def _apply_schema_on_startup() -> None:
    try:
        from v2_api.db_config_pg import DB_CONFIG

        print(
            f"[startup] DB target host={DB_CONFIG['host']} db={DB_CONFIG['database']}",
            flush=True,
        )
        _ensure_schema()
        print("[startup] schema migrations applied", flush=True)
    except Exception as ex:
        print(f"[startup] schema migration failed: {ex}", flush=True)


@app.middleware("http")
async def _ensure_schema_middleware(request: Request, call_next):
    if not getattr(app.state, "schema_ready", False):
        try:
            _ensure_schema()
            app.state.schema_ready = True
        except Exception as ex:
            print(f"[middleware] schema migration failed: {ex}", flush=True)
    return await call_next(request)


def _fetch_environments_rows(u: dict) -> list:
    """Load environment rows; caller must handle SQLAlchemyError."""
    where_clause = ""
    params: dict = {}
    if u["role"] not in ["Admin", "Developer"]:
        where_clause = " WHERE created_by = :uid "
        params["uid"] = u["id"]
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"""
                SELECT te.id, te.env_name, te.base_url, te.is_active, te.is_busy,
                       NULLIF(TRIM(COALESCE(te.auth_module_path, '')), ''),
                       NULLIF(TRIM(COALESCE(te.auth_function_name, '')), ''),
                       te.created_by,
                       te.bigid_username,
                       te.version,
                       (SELECT COUNT(DISTINCT bcm.classifier_id)
                        FROM build_classifier_mappings bcm
                        WHERE bcm.environment_id = te.id) AS classifier_count,
                       COALESCE(te.include_in_workflow_runs, TRUE)
                FROM test_environments te {where_clause} ORDER BY te.env_name
                """
            ),
            params,
        ).fetchall()


# Middleware to handle database transaction errors
@app.middleware("http")
async def db_session_middleware(request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        # If there's a database error, try to rollback any pending transactions
        try:
            with engine.connect() as conn:
                conn.rollback()
        except:
            pass
        raise e

# Optional alternate Postgres URL for workflow tables only (still Postgres — never legacy MySQL).
_workflow_optional_engine = None


def get_workflow_sql_engine():
    """PostgreSQL only: workflow_runs / workflow_steps / pooled envs. Uses WORKFLOW_DB_URI when set, else shared v2 API engine."""
    global _workflow_optional_engine

    explicit = os.getenv("WORKFLOW_DB_URI", "").strip()
    if explicit:
        if _workflow_optional_engine is None:
            _workflow_optional_engine = create_engine(explicit, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        return _workflow_optional_engine
    return engine


_WORKFLOW_ENV_STATUS_SQL = text(
    """
    WITH busy_ids AS (
        SELECT DISTINCT ws.environment_id AS eid
        FROM workflow_steps ws
        INNER JOIN workflow_runs wr ON wr.id = ws.run_id
        WHERE LOWER(COALESCE(wr.status, '')) = 'running'
          AND ws.environment_id IS NOT NULL
          AND COALESCE(ws.is_enabled, TRUE) = TRUE
          AND LOWER(TRIM(COALESCE(ws.status, ''))) IN (
              'running',
              'ds_created',
              'ds_test_ok',
              'scan_created',
              'scan_running',
              'scan_waiting'
          )
        UNION
        SELECT id AS eid FROM test_environments WHERE COALESCE(is_busy, FALSE) = TRUE
    )
    SELECT te.id, te.env_name,
           EXISTS (SELECT 1 FROM busy_ids b WHERE b.eid = te.id) AS busy
    FROM test_environments te
    WHERE COALESCE(te.is_active, TRUE) = TRUE
    ORDER BY te.env_name
    """
)


# Auth Setup
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/token")

def hash_password(password: str) -> str:
    """Return the SHA256 hash of a password."""
    if not password:
        return ""
    return hashlib.sha256(password.strip().encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not plain_password or not hashed_password:
        return False
    return hash_password(plain_password) == hashed_password

async def get_current_user(token: str = Depends(oauth2_scheme)):
    email = token.replace("fake-jwt-token-for-", "")
    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                SELECT u.id, u.username, r.role_name, u.role_id
                FROM app_users u
                JOIN app_roles r ON u.role_id = r.id
                WHERE u.username = :u
            """), {"u": email}).fetchone()
            if not res:
                # Fallback for dev/first run if user doesn't exist yet but token is valid
                if email == "bpatel@bigid.com":
                    return {"id": 1, "username": email, "role": "Admin", "role_id": 1}
                raise HTTPException(status_code=401, detail="Invalid user session")
            return {"id": res[0], "username": res[1], "role": res[2], "role_id": res[3]}
    except SQLAlchemyError:
        if email == "bpatel@bigid.com":
            return {"id": 1, "username": email, "role": "Admin", "role_id": 1}
        raise HTTPException(status_code=503, detail="Auth DB error")




# Models
class Token(BaseModel):
    access_token: str
    token_type: str

class UserProfile(BaseModel):
    id: int
    username: str
    role: str
    role_id: int

@app.get("/api/me", response_model=UserProfile)
async def get_me(u=Depends(get_current_user)):
    return u


@app.get("/api/reports")
async def list_generated_reports(u=Depends(get_current_user)):
    """List generated HTML reports from the repo reports/ directory."""
    if not REPORTS_DIR.exists():
        return {"items": []}
    items = []
    for path in sorted(REPORTS_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "name": path.name,
            "url": f"/api/reports/file/{path.name}",
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        })
    return {"items": items}


@app.get("/api/reports/file/{filename}")
async def get_generated_report_file(filename: str):
    """Serve a generated HTML report by safe filename."""
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.endswith(".html"):
        raise HTTPException(status_code=400, detail="Invalid report filename")
    path = (REPORTS_DIR / safe_name).resolve()
    reports_root = REPORTS_DIR.resolve()
    if reports_root not in path.parents or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="text/html", filename=safe_name)


# ── Benchmark Report Generation ────────────────────────────────────────────────

@app.get("/api/benchmark/builds")
async def get_benchmark_builds(token: str = Depends(oauth2_scheme)):
    """List all builds with benchmark data for report generation."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT bu.version, COUNT(*) AS cnt
                FROM benchmarks b
                JOIN builds bu ON bu.id = b.build_id
                WHERE COALESCE(b.is_active, TRUE) = TRUE
                GROUP BY bu.version
                ORDER BY 
                  COALESCE(NULLIF(SUBSTRING(bu.version FROM '^([0-9]+)'), '')::INTEGER, 0) DESC,
                  COALESCE(NULLIF(SUBSTRING(bu.version FROM '^[0-9]+[.\\-]([0-9]+)'), '')::INTEGER, 0) DESC
            """)).fetchall()
        builds = [{"version": str(r[0]), "count": int(r[1] or 0)} for r in rows]
        return {"builds": builds}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/benchmark/generate-report")
async def generate_benchmark_report(
    current_build: Optional[str] = Query(None),
    previous_build: Optional[str] = Query(None),
    environment_id: Optional[int] = Query(None),
    structured: Optional[bool] = Query(False),
    scanner_performance: Optional[bool] = Query(False),
    errors: Optional[bool] = Query(False),
    test_env: Optional[bool] = Query(False),
    test_env_environments: Optional[str] = Query(None),
    test_env_scanners: Optional[int] = Query(None),
    test_env_threads_per_scanner: Optional[int] = Query(None),
    test_env_scan_setting_threads: Optional[int] = Query(None),
    test_env_ds_lite_mode: Optional[str] = Query(None),
    test_env_scanner_jobs_thresholds: Optional[str] = Query(None),
    test_env_file_count: Optional[str] = Query(None),
    test_env_file_type: Optional[str] = Query(None),
    output_filename: Optional[str] = Query(None),
    u=Depends(get_current_user),
):
    """
    Generate a benchmark report by running the generate_benchmark_email_report.py script.
    Admin users only.
    """
    # Check admin access
    if u.role != "Admin":
        raise HTTPException(status_code=403, detail="Admin role required to generate reports")
    
    import subprocess
    import sys
    from datetime import datetime
    
    try:
        # Auto-generate filename if not provided
        if not output_filename:
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            build_slug = current_build.replace('.', '_') if current_build else "latest"
            output_filename = f"{build_slug}_{timestamp}.html"
        
        # Ensure reports directory exists
        REPORTS_DIR.mkdir(exist_ok=True, parents=True)
        output_path = REPORTS_DIR / output_filename
        
        # Build command — current_build is optional; script defaults to latest if omitted
        cmd = [
            sys.executable,
            "scripts/generate_benchmark_email_report.py",
        ]
        if current_build:
            cmd.extend(["--current-build", current_build])
        
        if previous_build:
            cmd.extend(["--previous-build", previous_build])
        
        if environment_id and environment_id > 0:
            cmd.extend(["--environment-id", str(environment_id)])
        
        if structured:
            cmd.append("--structured")
        
        if scanner_performance:
            cmd.append("--scanner-performance")
        
        if errors:
            cmd.append("--errors")
        
        if test_env:
            cmd.append("--test-env")
            if test_env_environments:
                cmd.extend(["--test-env-environments", test_env_environments])
            if test_env_scanners:
                cmd.extend(["--test-env-scanners", str(test_env_scanners)])
            if test_env_threads_per_scanner:
                cmd.extend(["--test-env-threads-per-scanner", str(test_env_threads_per_scanner)])
            if test_env_scan_setting_threads:
                cmd.extend(["--test-env-scan-setting-threads", str(test_env_scan_setting_threads)])
            if test_env_ds_lite_mode:
                cmd.extend(["--test-env-ds-lite-mode", test_env_ds_lite_mode])
            if test_env_scanner_jobs_thresholds:
                cmd.extend(["--test-env-scanner-jobs-thresholds", test_env_scanner_jobs_thresholds])
            if test_env_file_count:
                cmd.extend(["--test-env-file-count", test_env_file_count])
            if test_env_file_type:
                cmd.extend(["--test-env-file-type", test_env_file_type])
        
        cmd.extend(["--out", str(output_path)])
        
        # Run the script with timeout
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=Path(__file__).resolve().parent.parent)
        
        if result.returncode == 0:
            return {
                "success": True,
                "message": f"Report generated: {output_filename}",
                "filename": output_filename,
                "url": f"/api/reports/file/{output_filename}"
            }
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            raise HTTPException(status_code=500, detail=f"Report generation failed: {error_msg}")
    
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Report generation timeout (exceeded 10 minutes)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating report: {str(e)}")


class Environment(BaseModel):
    id: int
    env_name: str
    base_url: str
    is_active: bool
    is_busy: bool
    auth_module_path: Optional[str] = None
    auth_function_name: Optional[str] = None
    created_by: Optional[int] = None
    bigid_username: Optional[str] = None
    version: Optional[str] = None
    classifier_count: int = 0
    include_in_workflow_runs: bool = True
    license_sync_ok: Optional[bool] = None
    license_sync_message: Optional[str] = None


class EnvironmentCreate(BaseModel):
    env_name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1, max_length=500)
    auth_module_path: Optional[str] = ""
    auth_function_name: Optional[str] = "Get_auth_token"
    is_active: bool = True
    include_in_workflow_runs: bool = True
    username: Optional[str] = None
    password: Optional[str] = None


class EnvironmentUpdate(BaseModel):
    env_name: Optional[str] = Field(None, min_length=1, max_length=100)
    base_url: Optional[str] = Field(None, max_length=500)
    auth_module_path: Optional[str] = None
    auth_function_name: Optional[str] = None
    is_active: Optional[bool] = None
    include_in_workflow_runs: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None


def _license_fields_for_response(environment_id: int) -> Dict[str, Any]:
    from v2_api.bigid_license import sync_environment_license

    lic = sync_environment_license(engine, int(environment_id))
    if lic.get("ok"):
        s = lic.get("license_summary") or {}
        parts = []
        if s.get("license_name"):
            parts.append(str(s["license_name"]))
        if s.get("license_expires"):
            parts.append(f"exp {s['license_expires']}")
        msg = "License applied" + (f" ({', '.join(parts)})" if parts else "")
        return {"license_sync_ok": True, "license_sync_message": msg}
    err = lic.get("error") or "License apply failed"
    return {"license_sync_ok": False, "license_sync_message": str(err)}


class Classifier(BaseModel):
    id: int
    name: str

class BenchmarkCreate(BaseModel):
    build_id: Optional[int] = None
    config_id: Optional[int] = None
    classifier_set_id: Optional[int] = None
    scenario_type: str
    scan_type: str
    data_source_type_id: int
    file_set_type_id: int


class WorkflowStepPayload(BaseModel):
    """One row aligned with dashboard planner fields; omitted keys become defaults via normalize."""

    ds_kind: str = Field(..., min_length=1)
    ds_name: str = ""
    scan_kind: str = "full_scan"
    file_set_type: Optional[str] = None
    is_enabled: bool = True
    is_acl: bool = False
    is_ocr: bool = False
    is_structured_ff: bool = False
    is_ols: bool = False
    is_lite: bool = False
    feature_flags: Optional[str] = None
    notes: Optional[str] = None


class WorkflowCreatePayload(BaseModel):
    name: str = ""
    cooldown_minutes: int = 15
    env1_id: Optional[int] = None
    env2_id: Optional[int] = None
    steps: Optional[List[WorkflowStepPayload]] = None
    auto_start: bool = False


class WorkflowControlPayload(BaseModel):
    action: Literal["start", "pause", "resume", "stop"]
    env1_id: Optional[int] = None
    env2_id: Optional[int] = None


class ScanInsightsSyncPayload(BaseModel):
    scan_id: str = Field(..., min_length=1, max_length=256)


class ResyncParentsPayload(BaseModel):
    environment_id: int = Field(..., ge=1)
    limit: int = Field(200, ge=10, le=1000)


class MarkScanStoppedPayload(BaseModel):
    scan_id: str = Field(..., min_length=1, max_length=256)


class BenchmarkSavePayload(BaseModel):
    """Full benchmark save — mirrors Streamlit 'Save Benchmark Run'."""
    scan_id: Optional[str] = None
    build_version: str = Field(..., min_length=1, max_length=50)
    run_date: Optional[str] = None
    scenario_type: Optional[str] = None
    scan_type: Optional[str] = None
    data_source_type: str = Field(..., min_length=1)
    data_type: Optional[str] = None
    file_set_type: str = Field(..., min_length=1)
    scanner_count: int = Field(5, ge=1)
    thread_count: int = Field(50, ge=1)
    is_acl: bool = False
    is_ocr: bool = False
    is_feature_flags: bool = False
    feature_flags: Optional[str] = None
    total_files: Optional[int] = None
    total_folders: Optional[int] = None
    total_size_gb: Optional[float] = None
    scan_start_time: Optional[str] = None
    scan_end_time: Optional[str] = None
    total_scan_hours: Optional[float] = None
    throughput_cluster_gb_hr: Optional[float] = None
    throughput_scanner_gb_hr: Optional[float] = None
    throughput_thread_gb_hr: Optional[float] = None
    throughput_cluster_files_hr: Optional[float] = None
    throughput_scanner_files_hr: Optional[float] = None
    throughput_thread_files_hr: Optional[float] = None
    total_catalog_items: Optional[int] = None
    is_release: bool = False
    notes: Optional[str] = None


class BuildAnalysisRow(BaseModel):
    build_version: str
    data_source_type: Optional[str]
    file_set_type: Optional[str]
    scan_type: Optional[str]
    is_acl: bool
    is_feature_flags: bool
    throughput_cluster_gb_hr: Optional[float]
    throughput_cluster_files_hr: Optional[float]
    throughput_scanner_gb_hr: Optional[float]
    throughput_thread_gb_hr: Optional[float]
    total_scan_hours: Optional[float]
    total_catalog_items: Optional[int]
    objects_scanned: Optional[int]
    run_date: Optional[str]
    scan_id: Optional[str]
    is_release: bool


# Routes
async def _parse_login_credentials(request: Request) -> tuple[str, str]:
    """Accept OAuth2 form body or JSON {username, password}."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        username = str(body.get("username") or body.get("email") or "").strip()
        password = str(body.get("password") or "")
        return username, password

    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    return username, password


@app.post("/api/token", response_model=Token)
async def login_for_access_token(request: Request):
    _ensure_schema()
    username, password = await _parse_login_credentials(request)
    email = username.lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid @bigid.com email is required.")
    if not email.endswith("@bigid.com"):
        raise HTTPException(status_code=400, detail="Only @bigid.com emails are allowed.")
    if not password:
        raise HTTPException(status_code=401, detail="Password is required.")

    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    SELECT u.id, r.role_name, u.role_id, u.password_hash
                    FROM app_users u
                    JOIN app_roles r ON u.role_id = r.id
                    WHERE u.username = :u
                    """
                ),
                {"u": email},
            ).fetchone()

            if not res:
                raise HTTPException(
                    status_code=401,
                    detail="Account not found. Contact an Admin to create your account.",
                )
            user_id, _role_name, _role_id, pwd_hash = res
            if pwd_hash == "PASSWORDLESS":
                conn.execute(
                    text("UPDATE app_users SET password_hash = :h WHERE id = :id"),
                    {"h": hash_password(password), "id": user_id},
                )
            elif not verify_password(password, pwd_hash):
                raise HTTPException(status_code=401, detail="Incorrect password.")
        return {"access_token": f"fake-jwt-token-for-{email}", "token_type": "bearer"}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        raise HTTPException(status_code=503, detail=f"Database connection failed: {str(e)}") from e

@app.get("/api/users/me", response_model=UserProfile)
async def read_users_me(u=Depends(get_current_user)):
    return UserProfile(id=u["id"], username=u["username"], role=u["role"], role_id=u["role_id"])

@app.get("/api/environments", response_model=List[Environment])
async def get_environments(u=Depends(get_current_user)):
    try:
        _ensure_schema()
        res = _fetch_environments_rows(u)
        out = []
        for r in res:
                out.append(
                    {
                        "id": r[0],
                        "env_name": r[1],
                        "base_url": str(r[2] or ""),
                        "is_active": bool(r[3]) if r[3] is not None else True,
                        "is_busy": bool(r[4]) if r[4] is not None else False,
                        "auth_module_path": r[5],
                        "auth_function_name": r[6],
                        "created_by": r[7],
                        "bigid_username": r[8],
                        "version": r[9],
                        "classifier_count": int(r[10]) if r[10] is not None else 0,
                        "include_in_workflow_runs": bool(r[11]) if r[11] is not None else True,
                    }
                )
        return out
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=f"Database error loading environments: {ex}") from ex


@app.post("/api/environments", response_model=Environment)
async def create_environment(payload: EnvironmentCreate, u=Depends(get_current_user)):
    _ensure_schema()
    from v2_api.credentials_crypto import encrypt_password
    auth_fn = (payload.auth_function_name or "").strip() or "Get_auth_token"
    auth_path = (payload.auth_module_path or "").strip() or None
    bigid_user = (payload.username or "").strip() or None
    bigid_pass_enc = encrypt_password(payload.password.strip()) if payload.password and payload.password.strip() else None
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO test_environments
                        (env_name, base_url, auth_module_path, auth_function_name, is_active,
                         include_in_workflow_runs, created_by, bigid_username, bigid_password_enc)
                    VALUES (:name, :url, :apath, :afn, :active, :iwf, :uid, :buser, :bpass)
                    RETURNING id, env_name, base_url, is_active, is_busy, created_by, bigid_username,
                              include_in_workflow_runs
                    """
                ),
                {
                    "name": payload.env_name.strip(),
                    "url": _normalize_env_base_url(payload.base_url),
                    "apath": auth_path,
                    "afn": auth_fn,
                    "active": payload.is_active,
                    "iwf": payload.include_in_workflow_runs,
                    "uid": u["id"],
                    "buser": bigid_user,
                    "bpass": bigid_pass_enc,
                },
            ).fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Insert returned no row")
    except IntegrityError as ex:
        raise HTTPException(status_code=409, detail="Environment name already exists or constraint violation.") from ex
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex

    out = {
        "id": row[0],
        "env_name": row[1],
        "base_url": _repair_stored_base_url(engine, int(row[0])),
        "is_active": bool(row[3]) if row[3] is not None else True,
        "is_busy": bool(row[4]) if row[4] is not None else False,
        "auth_module_path": auth_path,
        "auth_function_name": auth_fn,
        "created_by": row[5],
        "bigid_username": row[6],
        "version": None,
        "classifier_count": 0,
        "include_in_workflow_runs": bool(row[7]) if row[7] is not None else True,
    }
    out.update(_license_fields_for_response(int(row[0])))
    return out


@app.put("/api/environments/{environment_id}", response_model=Environment)
async def update_environment(environment_id: int, payload: EnvironmentUpdate, u=Depends(get_current_user)):
    _ensure_schema()
    from v2_api.credentials_crypto import encrypt_password
    eid = int(environment_id)

    # Ownership check
    if u["role"] not in ["Admin", "Developer"]:
        with engine.connect() as conn:
            owner = conn.execute(text("SELECT created_by FROM test_environments WHERE id = :eid"), {"eid": eid}).scalar()
            if owner != u["id"]:
                raise HTTPException(status_code=403, detail="You do not own this environment")

    sets: List[str] = []
    params: dict = {"eid": eid}
    if payload.env_name is not None:
        sets.append("env_name = :env_name")
        params["env_name"] = payload.env_name.strip()
    if payload.base_url is not None:
        sets.append("base_url = :base_url")
        params["base_url"] = _normalize_env_base_url(payload.base_url)
    if payload.auth_module_path is not None:
        sets.append("auth_module_path = :auth_module_path")
        v = payload.auth_module_path.strip()
        params["auth_module_path"] = v if v else None
    if payload.auth_function_name is not None:
        sets.append("auth_function_name = :auth_function_name")
        fn = payload.auth_function_name.strip()
        params["auth_function_name"] = fn if fn else "Get_auth_token"
    if payload.is_active is not None:
        sets.append("is_active = :is_active")
        params["is_active"] = payload.is_active
    if payload.include_in_workflow_runs is not None:
        sets.append("include_in_workflow_runs = :include_in_workflow_runs")
        params["include_in_workflow_runs"] = payload.include_in_workflow_runs
    if payload.username is not None:
        sets.append("bigid_username = :bigid_username")
        params["bigid_username"] = payload.username.strip() or None
    if payload.password is not None and payload.password.strip():
        sets.append("bigid_password_enc = :bigid_password_enc")
        params["bigid_password_enc"] = encrypt_password(payload.password.strip())
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    base_url_changed = payload.base_url is not None
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(f"UPDATE test_environments SET {', '.join(sets)} WHERE id = :eid RETURNING id"),
                params,
            )
            if res.rowcount == 0:
                raise HTTPException(status_code=404, detail="Environment not found")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, env_name, base_url, is_active, is_busy,
                           NULLIF(TRIM(COALESCE(auth_module_path, '')), ''),
                           NULLIF(TRIM(COALESCE(auth_function_name, '')), ''),
                           created_by,
                           bigid_username,
                           COALESCE(include_in_workflow_runs, TRUE)
                    FROM test_environments WHERE id = :eid
                    """
                ),
                {"eid": eid},
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Environment not found")
    except IntegrityError as ex:
        raise HTTPException(status_code=409, detail="Duplicate name or constraint violation.") from ex
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex

    out = {
        "id": row[0],
        "env_name": row[1],
        "base_url": _repair_stored_base_url(engine, eid),
        "is_active": bool(row[3]) if row[3] is not None else True,
        "is_busy": bool(row[4]) if row[4] is not None else False,
        "auth_module_path": row[5],
        "auth_function_name": row[6],
        "created_by": row[7],
        "bigid_username": row[8],
        "include_in_workflow_runs": bool(row[9]) if row[9] is not None else True,
    }
    out.update(_license_fields_for_response(eid))
    if base_url_changed and not out.get("license_sync_ok"):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Base URL saved but license was not applied: {out.get('license_sync_message')}. "
                "Set BIGID_LICENSE_TOKEN in .env and save again."
            ),
        )
    return out


@app.delete("/api/environments/{environment_id}")
async def delete_environment(environment_id: int, u=Depends(get_current_user)):
    eid = int(environment_id)

    # Ownership check
    if u["role"] not in ["Admin", "Developer"]:
        with engine.connect() as conn:
            owner = conn.execute(text("SELECT created_by FROM test_environments WHERE id = :eid"), {"eid": eid}).scalar()
            if owner != u["id"]:
                raise HTTPException(status_code=403, detail="You do not own this environment")

    try:
        with engine.begin() as conn:
            # Verify env exists
            exists = conn.execute(text("SELECT id FROM test_environments WHERE id = :eid"), {"eid": eid}).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="Environment not found")

            # NULL out nullable FK references so the delete can proceed
            for tbl in ("build_classifier_mappings", "auto_scan_benchmarks",
                        "classifier_details_snapshots", "scan_parents", "scan_progress"):
                conn.execute(text(f"UPDATE {tbl} SET environment_id = NULL WHERE environment_id = :eid"), {"eid": eid})

            # Delete rows where environment_id is NOT NULL (can't be NULLed)
            conn.execute(text("DELETE FROM scan_log_errors WHERE environment_id = :eid"), {"eid": eid})
            conn.execute(text("DELETE FROM scan_log_correlations WHERE environment_id = :eid"), {"eid": eid})

            # Delete the environment (version_history cascades automatically)
            conn.execute(text("DELETE FROM test_environments WHERE id = :eid"), {"eid": eid})

        return {"ok": True, "id": eid}
    except HTTPException:
        raise
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex


@app.post("/api/environments/actions/clear-busy-locks")
async def clear_all_environment_busy_locks(token: str = Depends(oauth2_scheme)):
    """
    Clears `is_busy` / busy_by_run_id / busy_since on all environments.
    Use when workflow runner left stale locks before starting a new workflow run.
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE test_environments
                    SET is_busy = FALSE,
                        busy_by_run_id = NULL,
                        busy_since = NULL
                    WHERE COALESCE(is_busy, FALSE) = TRUE
                    """
                )
            )
            n = int(result.rowcount or 0)
        return {
            "ok": True,
            "cleared_rows": n,
            "detail": "Cleared workflow busy locks on environments that were marked busy.",
        }
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex

@app.get("/api/trends/small-files")
async def get_small_files_trend_data(
    token: str = Depends(oauth2_scheme),
    max_builds: int = Query(400, ge=5, le=600, description="Include rows for up to this many newest builds that have Small Files benchmarks."),
):
    """
    Rows for Small Files trend analysis: matches Streamlit `Small Files Trend Analysis` inputs.

    Rows are constrained by distinct **builds** (newest first), not a flat benchmark row limit, so charts can show
    "last N builds" without being starved when each build inserts many benchmarks.

    Align with dashboard `load_data` / `render_trend_analysis`:
    - Small Files match uses substring semantics like pandas `.contains('Small Files')` plus singular `Small File` rows.
    - Active rows: `COALESCE(is_active, TRUE)` (same intent as Streamlit `COALESCE(b.is_active, 1) = 1`).
    - Builds ordered by parsed major.minor from `builds.version`, then `id` — avoids missing newer semver when `id` order diverges after migration.

    `data_segment`: S3/SMB types are always `unstructured`; relational DB types (MSSQL, Postgres, MySQL, …) are `structured`;
    otherwise structured = FF / discovery-style rules (names, feature_flags, etc.).

    Response includes `distinct_builds_total` (how many builds in DB match Small Files) and `build_window_cap`.
    If Streamlit on another host shows more points, that database likely has more benchmark rows — compare `distinct_builds_total` locally vs re-run migration.
    """
    small_files_fst = """(
        fst.name ILIKE '%Small Files%'
        OR fst.name ILIKE '%Small File%'
    )"""
    small_files_fst0 = """(
        fst0.name ILIKE '%Small Files%'
        OR fst0.name ILIKE '%Small File%'
    )"""
    structured_expr = """(
        (
            dst.name IS NOT NULL AND (
                LOWER(TRIM(dst.name::text)) LIKE '%postgres%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%mssql%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%mysql%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%sql server%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%microsoft sql%'
                OR LOWER(TRIM(dst.name::text)) LIKE 'rdb-%'
                OR LOWER(TRIM(dst.name::text)) LIKE '%oracle%'
            )
        )
        OR COALESCE(cfg.is_feature_flags, FALSE)
        OR (
            LOWER(COALESCE(cfg.feature_flags, '')) LIKE '%structured%'
            AND LOWER(COALESCE(cfg.feature_flags, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(b.scenario_type, '')) LIKE '%structured%'
            AND LOWER(COALESCE(b.scenario_type, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(fst.name, '')) LIKE '%structured%'
            AND LOWER(COALESCE(fst.name, '')) NOT LIKE '%unstructured%'
        )
        OR LOWER(TRIM(COALESCE(fst.data_type, ''))) IN ('structured', 'structure')
    )"""
    structured_expr0 = """(
        (
            dst0.name IS NOT NULL AND (
                LOWER(TRIM(dst0.name::text)) LIKE '%postgres%'
                OR LOWER(TRIM(dst0.name::text)) LIKE '%mssql%'
                OR LOWER(TRIM(dst0.name::text)) LIKE '%mysql%'
                OR LOWER(TRIM(dst0.name::text)) LIKE '%sql server%'
                OR LOWER(TRIM(dst0.name::text)) LIKE '%microsoft sql%'
                OR LOWER(TRIM(dst0.name::text)) LIKE 'rdb-%'
                OR LOWER(TRIM(dst0.name::text)) LIKE '%oracle%'
            )
        )
        OR COALESCE(cfg0.is_feature_flags, FALSE)
        OR (
            LOWER(COALESCE(cfg0.feature_flags, '')) LIKE '%structured%'
            AND LOWER(COALESCE(cfg0.feature_flags, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(b0.scenario_type, '')) LIKE '%structured%'
            AND LOWER(COALESCE(b0.scenario_type, '')) NOT LIKE '%unstructured%'
        )
        OR (
            LOWER(COALESCE(fst0.name, '')) LIKE '%structured%'
            AND LOWER(COALESCE(fst0.name, '')) NOT LIKE '%unstructured%'
        )
        OR LOWER(TRIM(COALESCE(fst0.data_type, ''))) IN ('structured', 'structure')
    )"""

    try:
        with engine.connect() as conn:
            cnt_row = conn.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT bv.id)
                    FROM builds bv
                    WHERE EXISTS (
                        SELECT 1
                        FROM benchmarks b0
                        LEFT JOIN data_source_types dst0 ON b0.data_source_type_id = dst0.id
                        LEFT JOIN file_set_types fst0 ON b0.file_set_type_id = fst0.id
                        LEFT JOIN configurations cfg0 ON b0.config_id = cfg0.id
                        WHERE b0.build_id = bv.id
                          AND ({small_files_fst0} OR {structured_expr0})
                          AND COALESCE(b0.is_active, TRUE) = TRUE
                    )
                    """
                )
            ).fetchone()
            distinct_total = int(cnt_row[0] or 0) if cnt_row else 0

            res = conn.execute(
                text(
                    f"""
                    WITH recent_build_ids AS (
                        SELECT bv.id AS build_pk
                        FROM builds bv
                        WHERE EXISTS (
                            SELECT 1
                            FROM benchmarks b0
                            LEFT JOIN data_source_types dst0 ON b0.data_source_type_id = dst0.id
                            LEFT JOIN file_set_types fst0 ON b0.file_set_type_id = fst0.id
                            LEFT JOIN configurations cfg0 ON b0.config_id = cfg0.id
                            WHERE b0.build_id = bv.id
                              AND ({small_files_fst0} OR {structured_expr0})
                              AND COALESCE(b0.is_active, TRUE) = TRUE
                        )
                        ORDER BY
                          COALESCE(NULLIF(SUBSTRING(bv.version FROM '^([0-9]{{3}})'), '')::INTEGER, 0) DESC,
                          COALESCE(NULLIF(SUBSTRING(bv.version FROM '^[0-9]{{3}}[.\\-]([0-9]+)'), '')::INTEGER, 0) DESC,
                          bv.id DESC
                        LIMIT :max_builds
                    )
                    SELECT
                        b.id,
                        COALESCE(bv.version::text, '') AS build_version,
                        b.run_date,
                        b.scenario_type,
                        b.scan_type,
                        dst.name AS data_source_type,
                        fst.name AS file_set_type,
                        COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
                        CASE
                            WHEN dst.name IS NOT NULL AND (
                                LOWER(TRIM(dst.name::text)) ~ '^(s3|smb)(\\s|$|_)'
                                OR LOWER(TRIM(dst.name::text)) LIKE 'aws s3%'
                                OR LOWER(TRIM(dst.name::text)) LIKE 'amazon%s3%'
                                OR LOWER(TRIM(dst.name::text)) IN ('s3', 'smb', 'amazons3', 'aws s3', 'amazon s3')
                            )
                                THEN 'unstructured'
                            WHEN dst.name IS NOT NULL AND (
                                LOWER(TRIM(dst.name::text)) LIKE '%postgres%'
                                OR LOWER(TRIM(dst.name::text)) LIKE '%mssql%'
                                OR LOWER(TRIM(dst.name::text)) LIKE '%mysql%'
                                OR LOWER(TRIM(dst.name::text)) LIKE '%sql server%'
                                OR LOWER(TRIM(dst.name::text)) LIKE '%microsoft sql%'
                                OR LOWER(TRIM(dst.name::text)) LIKE 'rdb-%'
                                OR LOWER(TRIM(dst.name::text)) LIKE '%oracle%'
                            )
                                THEN 'structured'
                            WHEN COALESCE(cfg.is_feature_flags, FALSE) THEN 'structured'
                            WHEN LOWER(COALESCE(cfg.feature_flags, '')) LIKE '%structured%'
                              AND LOWER(COALESCE(cfg.feature_flags, '')) NOT LIKE '%unstructured%' THEN 'structured'
                            WHEN LOWER(COALESCE(b.scenario_type, '')) LIKE '%structured%'
                              AND LOWER(COALESCE(b.scenario_type, '')) NOT LIKE '%unstructured%' THEN 'structured'
                            WHEN LOWER(COALESCE(fst.name, '')) LIKE '%structured%'
                              AND LOWER(COALESCE(fst.name, '')) NOT LIKE '%unstructured%' THEN 'structured'
                            WHEN LOWER(TRIM(COALESCE(fst.data_type, ''))) IN ('structured', 'structure') THEN 'structured'
                            ELSE 'unstructured'
                        END AS data_segment,
                        b.total_scan_hours::double precision AS total_scan_hours,
                        b.throughput_cluster_gb_hr::double precision AS throughput_cluster_gb_hr,
                        b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr,
                        b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr,
                        b.throughput_cluster_files_hr::double precision AS throughput_cluster_files_hr,
                        b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr,
                        b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr,
                        b.total_catalog_items::bigint AS total_catalog_items,
                        COALESCE(b.is_release, FALSE) AS is_release
                    FROM benchmarks b
                    INNER JOIN recent_build_ids rbi ON b.build_id = rbi.build_pk
                    LEFT JOIN builds bv ON b.build_id = bv.id
                    LEFT JOIN data_source_types dst ON b.data_source_type_id = dst.id
                    LEFT JOIN file_set_types fst ON b.file_set_type_id = fst.id
                    LEFT JOIN configurations cfg ON b.config_id = cfg.id
                    WHERE fst.name IS NOT NULL
                      AND ({small_files_fst} OR {structured_expr})
                      AND COALESCE(b.is_active, TRUE) = TRUE
                    ORDER BY b.id DESC
                    """
                ),
                {"max_builds": max_builds},
            ).fetchall()
        out = []
        for r in res:
            out.append(
                {
                    "id": r[0],
                    "build_version": (r[1] or "").strip() or "unknown",
                    "run_date": r[2],
                    "scenario_type": r[3],
                    "scan_type": r[4],
                    "data_source_type": r[5],
                    "file_set_type": r[6],
                    "is_feature_flags": bool(r[7]),
                    "data_segment": r[8],
                    "total_scan_hours": r[9],
                    "throughput_cluster_gb_hr": r[10],
                    "throughput_scanner_gb_hr": r[11],
                    "throughput_thread_gb_hr": r[12],
                    "throughput_cluster_files_hr": r[13],
                    "throughput_scanner_files_hr": r[14],
                    "throughput_thread_files_hr": r[15],
                    "total_catalog_items": r[16],
                    "is_release": bool(r[17]) if len(r) > 17 else False,
                }
            )
        return {
            "rows": out,
            "distinct_builds_total": distinct_total,
            "build_window_cap": max_builds,
        }
    except SQLAlchemyError as e:
        try:
            import traceback

            traceback.print_exc()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/benchmarks")
async def get_benchmarks(token: str = Depends(oauth2_scheme), limit: int = 50):
    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                SELECT b.id, b.run_date, b.scenario_type, b.scan_type, 
                       t.name as data_source_type, f.name as file_set_type,
                       f.total_files, b.throughput_cluster_files_hr
                FROM benchmarks b
                LEFT JOIN data_source_types t ON b.data_source_type_id = t.id
                LEFT JOIN file_set_types f ON b.file_set_type_id = f.id
                ORDER BY b.id DESC LIMIT :limit
            """), {"limit": limit}).fetchall()
            
        benchmarks = []
        for r in res:
            benchmarks.append({
                "id": r[0], "run_date": r[1], "scenario_type": r[2], "scan_type": r[3],
                "data_source_type": r[4], "file_set_type": r[5], "total_files": r[6], "throughput_cluster_files_hr": r[7]
            })
        return benchmarks
    except SQLAlchemyError:
        return []

@app.post("/api/benchmarks", response_model=dict)
async def create_benchmark(benchmark: BenchmarkCreate, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            res = conn.execute(text("""
                INSERT INTO benchmarks 
                (build_id, config_id, classifier_set_id, scenario_type, scan_type, data_source_type_id, file_set_type_id, is_active) 
                VALUES (:b_id, :c_id, :cs_id, :st, :sct, :dst, :fst, TRUE)
                RETURNING id
            """), {
                "b_id": benchmark.build_id, "c_id": benchmark.config_id, "cs_id": benchmark.classifier_set_id,
                "st": benchmark.scenario_type, "sct": benchmark.scan_type, "dst": benchmark.data_source_type_id, "fst": benchmark.file_set_type_id
            }).fetchone()
        return {"message": "Benchmark created", "id": res[0]}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workflow/stop/{benchmark_id}")
async def stop_workflow(benchmark_id: int, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            res = conn.execute(text("UPDATE benchmarks SET is_active = FALSE WHERE id = :id AND is_active = TRUE"), {"id": benchmark_id})
            if res.rowcount == 0:
                raise HTTPException(status_code=404, detail="Active benchmark not found")
        return {"message": f"Workflow {benchmark_id} stopped"}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs/errors")
async def get_log_errors(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None),
    environment_id: Optional[int] = Query(None),
    scan_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    token: str = Depends(oauth2_scheme),
):
    """Paginated AI / scanner errors (PostgreSQL scan_log_errors) with optional filters."""
    try:
        with engine.connect() as conn:
            where_parts = []
            params: dict = {}

            if source:
                where_parts.append("sle.log_source = :source")
                params["source"] = source
            if environment_id is not None:
                where_parts.append("sle.environment_id = :env_id")
                params["env_id"] = environment_id
            if scan_id:
                where_parts.append("sle.scan_id = :scan_id")
                params["scan_id"] = scan_id
            if search:
                where_parts.append(
                    "(sle.error_context ILIKE :search OR sle.ai_analysis ILIKE :search OR sle.scan_id ILIKE :search)"
                )
                params["search"] = f"%{search}%"

            where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

            total_row = conn.execute(
                text(f"SELECT COUNT(*) FROM scan_log_errors sle {where_clause}"),
                params
            ).scalar()
            total = int(total_row or 0)

            res = conn.execute(
                text(
                    f"""
                    SELECT
                        sle.id,
                        sle.environment_id,
                        COALESCE(te.env_name, '#' || sle.environment_id::text) AS environment_name,
                        sle.log_timestamp,
                        sle.scan_id,
                        sle.error_context,
                        sle.ai_analysis,
                        sle.log_source,
                        sle.subscan_id,
                        sle.is_resolved,
                        sle.manual_triage
                    FROM scan_log_errors sle
                    LEFT JOIN test_environments te ON te.id = sle.environment_id
                    {where_clause}
                    ORDER BY sle.log_timestamp DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            ).fetchall()

            sources = [
                r[0] for r in conn.execute(
                    text("SELECT DISTINCT log_source FROM scan_log_errors WHERE log_source IS NOT NULL ORDER BY log_source")
                ).fetchall()
            ]

            environments = [
                {"id": r[0], "name": r[1]} for r in conn.execute(
                    text("""
                        SELECT DISTINCT sle.environment_id, COALESCE(te.env_name, '#' || sle.environment_id::text) AS env_name
                        FROM scan_log_errors sle
                        LEFT JOIN test_environments te ON te.id = sle.environment_id
                        ORDER BY env_name, sle.environment_id
                    """)
                ).fetchall()
            ]

        items = [
            {
                "id": r[0],
                "environment_id": r[1],
                "environment_name": r[2],
                "log_timestamp": r[3],
                "scan_id": r[4],
                "error_context": r[5],
                "ai_analysis": r[6],
                "log_source": r[7],
                "subscan_id": r[8],
                "is_resolved": bool(r[9]) if r[9] is not None else False,
                "manual_triage": r[10],
            }
            for r in res
        ]
        return {"items": items, "total": total, "sources": sources, "environments": environments}
    except SQLAlchemyError as e:
        import traceback
        print(f"ERROR in get_log_errors: {e}")
        traceback.print_exc()
        return {"items": [], "total": 0, "sources": [], "environments": []}


@app.get("/api/logs/errors/unique")
async def get_unique_log_errors(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    window_hours: int = Query(24, ge=1, le=720),
    source: Optional[str] = Query(None),
    environment_id: Optional[int] = Query(None),
    scan_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    token: str = Depends(oauth2_scheme),
):
    """Grouped/unique errors within a time window, with optional filters."""
    try:
        with engine.connect() as conn:
            where_parts = ["sle.log_timestamp > NOW() - INTERVAL '%d hours'" % window_hours]
            params: dict = {}

            if source:
                where_parts.append("sle.log_source = :source")
                params["source"] = source
            if environment_id is not None:
                where_parts.append("sle.environment_id = :env_id")
                params["env_id"] = environment_id
            if scan_id:
                where_parts.append("sle.scan_id = :scan_id")
                params["scan_id"] = scan_id
            if search:
                where_parts.append(
                    "(sle.error_context ILIKE :search OR sle.ai_analysis ILIKE :search OR sle.scan_id ILIKE :search)"
                )
                params["search"] = f"%{search}%"

            where_clause = "WHERE " + " AND ".join(where_parts)

            total_row = conn.execute(
                text(f"""
                    SELECT COUNT(DISTINCT MD5(COALESCE(sle.error_context, '')))
                    FROM scan_log_errors sle
                    {where_clause}
                """),
                params
            ).scalar()
            total = int(total_row or 0)

            res = conn.execute(
                text(f"""
                    WITH error_groups AS (
                        SELECT
                            MD5(COALESCE(sle.error_context, '')) AS group_key,
                            COUNT(*) AS occurrences,
                            MIN(sle.log_timestamp) AS first_seen,
                            MAX(sle.log_timestamp) AS last_seen,
                            COUNT(DISTINCT sle.scan_id) AS scan_count,
                            COUNT(DISTINCT sle.environment_id) AS env_count,
                            sle.error_context,
                            sle.log_source,
                            MIN(sle.environment_id) AS first_env_id
                        FROM scan_log_errors sle
                        {where_clause}
                        GROUP BY sle.error_context, sle.log_source
                    )
                    SELECT
                        group_key,
                        occurrences,
                        first_seen,
                        last_seen,
                        scan_count,
                        env_count,
                        error_context,
                        log_source,
                        COALESCE(te.env_name, '#' || first_env_id::text) AS environment_name,
                        (SELECT sle.scan_id FROM scan_log_errors sle
                         WHERE MD5(COALESCE(sle.error_context, '')) = group_key LIMIT 1) AS scan_id
                    FROM error_groups
                    LEFT JOIN test_environments te ON te.id = first_env_id
                    ORDER BY occurrences DESC, last_seen DESC
                    LIMIT :limit OFFSET :offset
                """),
                {**params, "limit": limit, "offset": offset},
            ).fetchall()

        items = [
            {
                "group_key": r[0],
                "occurrences": r[1],
                "first_seen": r[2].isoformat() if r[2] else None,
                "last_seen": r[3].isoformat() if r[3] else None,
                "scan_count": r[4],
                "env_count": r[5],
                "error_context": r[6],
                "log_source": r[7],
                "environment_name": r[8],
                "scan_id": r[9],
            }
            for r in res
        ]
        return {"items": items, "total": total}
    except SQLAlchemyError as e:
        import traceback
        print(f"ERROR in get_unique_log_errors: {e}")
        traceback.print_exc()
        return {"items": [], "total": 0}


@app.post("/api/logs/errors/{log_id}/triage")
async def set_log_error_triage(
    log_id: int,
    triage: str = Body(..., embed=True),
    apply_to_signature: bool = Body(False, embed=True),
    token: str = Depends(oauth2_scheme),
):
    """Set manual triage note for a log error (optionally for all with same signature)."""
    try:
        with engine.begin() as conn:
            if apply_to_signature:
                error_context = conn.execute(
                    text("SELECT error_context FROM scan_log_errors WHERE id = :id"),
                    {"id": log_id}
                ).scalar()
                if error_context:
                    conn.execute(
                        text("""
                            UPDATE scan_log_errors
                            SET manual_triage = :triage
                            WHERE error_context = :error_context
                        """),
                        {"triage": triage if triage.strip() else None, "error_context": error_context}
                    )
            else:
                conn.execute(
                    text("""
                        UPDATE scan_log_errors
                        SET manual_triage = :triage
                        WHERE id = :id
                    """),
                    {"triage": triage if triage.strip() else None, "id": log_id}
                )
        return {"message": "Triage updated"}
    except SQLAlchemyError:
        raise HTTPException(status_code=500, detail="Error updating triage")


@app.delete("/api/admin/logs/purge")
async def purge_log_errors(
    days: int = Query(15, ge=0),
    keep_triaged: bool = Query(False),
    u=Depends(get_current_user),
):
    """Delete scan_log_errors rows older than `days` (0 = delete all). Admin only."""
    if u["role"] != "Admin":
        raise HTTPException(status_code=403, detail="Admin role required to purge logs")
    # log_timestamp is VARCHAR in some deployments and TIMESTAMP in others; cast so both work.
    where_parts = []
    params: dict = {}
    if days > 0:
        where_parts.append("CAST(log_timestamp AS TIMESTAMP) < NOW() - make_interval(days => :days)")
        params["days"] = days
    if keep_triaged:
        where_parts.append("manual_triage IS NULL")
    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    try:
        with engine.begin() as conn:
            res = conn.execute(text(f"DELETE FROM scan_log_errors {where_clause}"), params)
            deleted = res.rowcount or 0
        return {"total_deleted": deleted, "deleted": {"scan_log_errors": deleted}}
    except SQLAlchemyError:
        raise HTTPException(status_code=500, detail="Error purging logs")


def _scan_parents_where_clause(
    environment_id: Optional[int],
    status_key: Literal["all", "in_progress", "completed"],
    scan_id: Optional[str] = None,
) -> tuple[str, dict]:
    """SQL fragment + bind params (PostgreSQL). Instant-scan-name noise excluded always."""
    clauses = [
        "LOWER(TRIM(COALESCE(sp.scan_name, ''))) NOT LIKE 'instant scan data%'",
    ]
    # Streamlit hides stopped/aborted in multi-env loaders; listing "All" without them looked empty whenever
    # every scan ended that way — include them here for status=all only.
    if status_key != "all":
        clauses.append("COALESCE(TRIM(sp.scan_status), '') NOT IN ('Stopped', 'Aborted')")
    params: dict = {}
    if environment_id is not None:
        clauses.append("sp.environment_id = :eid")
        params["eid"] = int(environment_id)
    sid_f = str(scan_id or "").strip()
    if sid_f:
        clauses.append("sp.scan_id = :scan_sid")
        params["scan_sid"] = sid_f
    if status_key == "in_progress":
        clauses.append(
            "TRIM(COALESCE(sp.scan_status,'')) IN ("
            "'Started','Awaiting','In Progress','InProgress','Running',"
            "'Stop Requested','StopRequested','Stopping')"
        )
    elif status_key == "completed":
        clauses.append(
            "TRIM(COALESCE(sp.scan_status,'')) IN "
            "('Failed','CompletedWithFailures','Completed')"
        )
    return " AND ".join(clauses), params


_SCAN_PARENTS_SORT_SQL = {
    # Default: most recently touched first (matches “latest on top”).
    "latest": (
        "COALESCE(sp.updated_at, sp.last_seen_at, sp.created_at) DESC NULLS LAST, sp.scan_id DESC"
    ),
    "started_desc": "sp.started_at DESC NULLS LAST, sp.scan_id DESC",
    "started_asc": "sp.started_at ASC NULLS LAST, sp.scan_id ASC",
    "completed_desc": "sp.completed_at DESC NULLS LAST, sp.scan_id DESC",
    "completed_asc": "sp.completed_at ASC NULLS LAST, sp.scan_id ASC",
    "scan_id_desc": "sp.scan_id DESC",
    "scan_id_asc": "sp.scan_id ASC",
    "environment": "LOWER(COALESCE(te.env_name, '')) ASC, sp.scan_id ASC",
    "environment_desc": "LOWER(COALESCE(te.env_name, '')) DESC NULLS LAST, sp.scan_id DESC",
    "status": "LOWER(COALESCE(sp.scan_status, '')) ASC, sp.scan_id ASC",
    "status_desc": "LOWER(COALESCE(sp.scan_status, '')) DESC NULLS LAST, sp.scan_id DESC",
    "scan_name_asc": "LOWER(COALESCE(sp.scan_name::text, '')) ASC NULLS LAST, sp.scan_id ASC",
    "scan_name_desc": "LOWER(COALESCE(sp.scan_name::text, '')) DESC NULLS LAST, sp.scan_id DESC",
    "scan_type_asc": "LOWER(COALESCE(sp.scan_type::text, '')) ASC NULLS LAST, sp.scan_id ASC",
    "scan_type_desc": "LOWER(COALESCE(sp.scan_type::text, '')) DESC NULLS LAST, sp.scan_id DESC",
    "template_asc": "LOWER(COALESCE(sp.template_name::text, '')) ASC NULLS LAST, sp.scan_id ASC",
    "template_desc": "LOWER(COALESCE(sp.template_name::text, '')) DESC NULLS LAST, sp.scan_id DESC",
    # Legacy: running-like scans first, then by activity (Streamlit-style).
    "running_first": (
        "CASE WHEN TRIM(COALESCE(sp.scan_status,'')) IN ("
        "'Started','Awaiting','In Progress','InProgress','Running',"
        "'Stop Requested','StopRequested','Stopping') THEN 0 ELSE 1 END, "
        "COALESCE(sp.updated_at, sp.last_seen_at, sp.created_at) DESC NULLS LAST, sp.scan_id DESC"
    ),
}


@app.get("/api/scan/parents")
async def get_scan_parents(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None),
    scan_id: Optional[str] = Query(None),
    status: Literal["all", "in_progress", "completed"] = Query("all"),
    sort_by: str = Query("latest"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Parent scans from Postgres `scan_parents` (+ env names), filtered like Streamlit Scan Status helpers."""
    where_sql, binds = _scan_parents_where_clause(environment_id, status, scan_id)
    binds = {**binds, "limit": int(limit), "offset": int(offset)}
    sb = str(sort_by or "").strip()
    if sb not in _SCAN_PARENTS_SORT_SQL:
        sb = "latest"
    order_clause = _SCAN_PARENTS_SORT_SQL[sb]
    sel = text(
        f"""
        SELECT sp.scan_id, sp.scan_name, sp.scan_type, sp.started_at, sp.completed_at,
               sp.template_name, sp.scan_status, sp.environment_id,
               COALESCE(te.env_name, '#' || sp.environment_id::text) AS environment_name
        FROM scan_parents sp
        LEFT JOIN test_environments te ON te.id = sp.environment_id
        WHERE {where_sql}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
        """
    )
    count_sql = text(f"SELECT COUNT(*) FROM scan_parents sp WHERE {where_sql}")
    try:
        with engine.connect() as conn:
            total = int(conn.execute(count_sql, {k: v for k, v in binds.items() if k != "limit" and k != "offset"}).scalar() or 0)
            rows = conn.execute(sel, binds).fetchall()
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="scan_parents unavailable (table missing or schema mismatch?).")
    items = []
    for r in rows:
        items.append(
            {
                "scan_id": str(r[0] or ""),
                "scan_name": r[1],
                "scan_type": r[2],
                "started_at": _maybe_ts(r[3]),
                "completed_at": _maybe_ts(r[4]),
                "template_name": r[5],
                "scan_status": r[6],
                "environment_id": r[7],
                "environment_name": r[8],
            }
        )
    return {"items": items, "total": total}


# Combined Scan Status: `scan_parents` + latest `scan_progress` row per scan (LEFT JOIN LATERAL).
_SCAN_UNIFIED_SORT_SQL: Dict[str, str] = {
    **_SCAN_PARENTS_SORT_SQL,
    "snapshot_desc": 'COALESCE(prog.captured_at_utc, prog.prog_ts) DESC NULLS LAST, sp.scan_id DESC',
    "snapshot_asc": 'COALESCE(prog.captured_at_utc, prog.prog_ts) ASC NULLS LAST, sp.scan_id ASC',
    "state": "LOWER(COALESCE(prog.scan_state::text, '')) ASC NULLS LAST, sp.scan_id ASC",
    "state_desc": "LOWER(COALESCE(prog.scan_state::text, '')) DESC NULLS LAST, sp.scan_id DESC",
    "objects_desc": "COALESCE(prog.objects_scanned, 0) DESC NULLS LAST, sp.scan_id DESC",
    "objects_asc": "COALESCE(prog.objects_scanned, 0) ASC NULLS LAST, sp.scan_id ASC",
    "parts_desc": "COALESCE(prog.total_parts_scanned, 0) DESC NULLS LAST, sp.scan_id DESC",
    "parts_asc": "COALESCE(prog.total_parts_scanned, 0) ASC NULLS LAST, sp.scan_id ASC",
    "classify_desc": "COALESCE(prog.total_classifications, 0) DESC NULLS LAST, sp.scan_id DESC",
    "classify_asc": "COALESCE(prog.total_classifications, 0) ASC NULLS LAST, sp.scan_id ASC",
    "subscans_desc": "COALESCE(prog.total_sub_scans, 0) DESC NULLS LAST, sp.scan_id DESC",
    "subscans_asc": "COALESCE(prog.total_sub_scans, 0) ASC NULLS LAST, sp.scan_id ASC",
}


@app.get("/api/scan/unified")
async def get_scan_status_unified(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None),
    scan_id: Optional[str] = Query(None),
    status: Literal["all", "in_progress", "completed"] = Query("all"),
    sort_by: str = Query("latest"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    One row per parent scan: columns from `scan_parents` plus latest tracker snapshot from `scan_progress`
    (when present). Replaces separate parents + progress lists on Scan Status.
    """
    where_sql, binds = _scan_parents_where_clause(environment_id, status, scan_id)
    binds = {**binds, "limit": int(limit), "offset": int(offset)}
    sb = str(sort_by or "").strip()
    if sb not in _SCAN_UNIFIED_SORT_SQL:
        sb = "latest"
    order_clause = _SCAN_UNIFIED_SORT_SQL[sb]
    sel = text(
        f"""
        SELECT
            sp.scan_id,
            sp.scan_name,
            sp.scan_type,
            sp.started_at,
            sp.completed_at,
            sp.template_name,
            sp.scan_status,
            sp.environment_id,
            COALESCE(te.env_name, '#' || sp.environment_id::text) AS environment_name,
            prog.scan_state,
            prog.completed_sub_scans,
            prog.failed_sub_scans,
            prog.total_sub_scans,
            prog.total_parts_scanned,
            prog.total_classifications,
            prog.objects_scanned,
            prog.objects_failed,
            prog.objects_with_pi,
            COALESCE(prog.captured_at_utc::text, prog.prog_ts::text) AS last_snapshot_at
        FROM scan_parents sp
        LEFT JOIN test_environments te ON te.id = sp.environment_id
        LEFT JOIN LATERAL (
            SELECT
                pg.scan_state,
                pg.completed_sub_scans,
                pg.failed_sub_scans,
                pg.total_sub_scans,
                pg.total_parts_scanned,
                pg.total_classifications,
                pg.objects_scanned,
                pg.objects_failed,
                pg.objects_with_pi,
                pg.captured_at_utc,
                pg."timestamp" AS prog_ts
            FROM scan_progress pg
            WHERE pg.scan_id = sp.scan_id
            ORDER BY COALESCE(pg.captured_at_utc, pg."timestamp") DESC NULLS LAST, pg.id DESC
            LIMIT 1
        ) prog ON TRUE
        WHERE {where_sql}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
        """
    )
    count_sql = text(f"SELECT COUNT(*) FROM scan_parents sp WHERE {where_sql}")
    try:
        with engine.connect() as conn:
            total = int(conn.execute(count_sql, {k: v for k, v in binds.items() if k != "limit" and k != "offset"}).scalar() or 0)
            rows = conn.execute(sel, binds).fetchall()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="scan unified view unavailable (scan_parents / scan_progress schema mismatch?).",
        )
    items = []
    for r in rows:
        items.append(
            {
                "scan_id": str(r[0] or ""),
                "scan_name": r[1],
                "scan_type": r[2],
                "started_at": _maybe_ts(r[3]),
                "completed_at": _maybe_ts(r[4]),
                "template_name": r[5],
                "scan_status": r[6],
                "environment_id": r[7],
                "environment_name": r[8],
                "scan_state": r[9],
                "completed_sub_scans": int(r[10] or 0) if r[10] is not None else None,
                "failed_sub_scans": int(r[11] or 0) if r[11] is not None else None,
                "total_sub_scans": int(r[12] or 0) if r[12] is not None else None,
                "total_parts_scanned": int(r[13] or 0) if r[13] is not None else None,
                "total_classifications": int(r[14] or 0) if r[14] is not None else None,
                "objects_scanned": int(r[15] or 0) if r[15] is not None else None,
                "objects_failed": int(r[16] or 0) if r[16] is not None else None,
                "objects_with_pi": int(r[17] or 0) if r[17] is not None else None,
                "last_snapshot_at": r[18],
            }
        )
    return {"items": items, "total": total}


def _compact_scan_status(s: Optional[str]) -> str:
    return str(s or "").strip().lower().replace(" ", "")


def _is_terminal_parent_scan_status(scan_status: Optional[str]) -> bool:
    """Align with Streamlit Scan Progress: manual sync disabled when scan has finished."""
    cs = _compact_scan_status(scan_status)
    return cs in {
        "failed",
        "completed",
        "completedwithfailures",
        "stopped",
        "aborted",
        "cancelled",
        "canceled",
    }


@app.get("/api/scan/{scan_id}/detail")
async def get_scan_progress_detail_meta(scan_id: str, token: str = Depends(oauth2_scheme)):
    """
    Metadata for Scan Progress Details (React): parent row, env name, optional insights row,
    snapshot timestamps, tracker heartbeat — mirrors Streamlit DB-only context.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")

    parent: Optional[dict] = None
    environment_name: Optional[str] = None
    resolved_env_id: Optional[int] = None

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT sp.scan_id, sp.scan_name, sp.scan_type, sp.scan_status, sp.environment_id,
                           sp.started_at, sp.completed_at, sp.template_name,
                           COALESCE(te.env_name, '#' || sp.environment_id::text)
                    FROM scan_parents sp
                    LEFT JOIN test_environments te ON te.id = sp.environment_id
                    WHERE sp.scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
            if row:
                resolved_env_id = int(row[4]) if row[4] is not None else None
                environment_name = str(row[8] or "").strip() or None
                completed_at = row[6]

                # If scan is completed, use parent scan status (it has the final state)
                # Otherwise use latest progress history (most accurate for in-progress scans)
                if completed_at:
                    scan_status = row[3]  # Use parent scan status for completed scans
                else:
                    scan_status = _get_latest_progress_state(engine, sid) or row[3]

                parent = {
                    "scan_id": str(row[0] or ""),
                    "scan_name": row[1],
                    "scan_type": row[2],
                    "scan_status": scan_status,
                    "environment_id": row[4],
                    "started_at": _maybe_ts(row[5]),
                    "completed_at": _maybe_ts(row[6]),
                    "template_name": row[7],
                }
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="scan_parents unavailable.")

    # Workflow fallback for environment (same idea as dashboard._env_id_from_workflow_for_scan_cached)
    if resolved_env_id is None:
        try:
            with engine.connect() as conn:
                wr = conn.execute(
                    text(
                        "SELECT environment_id FROM workflow_steps WHERE scan_id = :sid ORDER BY id DESC LIMIT 1"
                    ),
                    {"sid": sid},
                ).fetchone()
                if wr and wr[0] is not None:
                    resolved_env_id = int(wr[0])
                    er = conn.execute(
                        text("SELECT env_name FROM test_environments WHERE id = :eid LIMIT 1"),
                        {"eid": resolved_env_id},
                    ).fetchone()
                    if er and er[0]:
                        environment_name = str(er[0])
        except SQLAlchemyError:
            pass

    insights: Optional[dict] = None
    try:
        with engine.connect() as conn:
            ir = conn.execute(
                text(
                    """
                    SELECT objects_scanned, objects_enumerated, total_classification, objects_with_pi,
                           distinct_classification_attributes, total_size_gb, new_attributes_count,
                           removed_attributes_count, classifier_attribute_names, data_source_names
                    FROM scan_insights
                    WHERE scan_id = :sid
                    LIMIT 1
                    """
                ),
                {"sid": sid},
            ).fetchone()
            if ir:
                insights = {
                    "objects_scanned": ir[0],
                    "objects_enumerated": ir[1],
                    "total_classification": ir[2],
                    "objects_with_pi": ir[3],
                    "distinct_classification_attributes": ir[4],
                    "total_size_gb": ir[5],
                    "new_attributes_count": ir[6],
                    "removed_attributes_count": ir[7],
                    "classifier_attribute_names": ir[8],
                    "data_source_names": ir[9],
                }
    except SQLAlchemyError:
        # Older schemas may lack data_source_names or other cols — load core metrics only.
        try:
            with engine.connect() as conn:
                ir = conn.execute(
                    text(
                        """
                        SELECT objects_scanned, objects_enumerated, total_classification, objects_with_pi,
                               distinct_classification_attributes, total_size_gb, new_attributes_count,
                               removed_attributes_count, classifier_attribute_names
                        FROM scan_insights
                        WHERE scan_id = :sid
                        LIMIT 1
                        """
                    ),
                    {"sid": sid},
                ).fetchone()
                if ir:
                    insights = {
                        "objects_scanned": ir[0],
                        "objects_enumerated": ir[1],
                        "total_classification": ir[2],
                        "objects_with_pi": ir[3],
                        "distinct_classification_attributes": ir[4],
                        "total_size_gb": ir[5],
                        "new_attributes_count": ir[6],
                        "removed_attributes_count": ir[7],
                        "classifier_attribute_names": ir[8],
                        "data_source_names": None,
                    }
        except SQLAlchemyError:
            insights = None

    latest_this: Optional[str] = None
    latest_global: Optional[str] = None
    try:
        with engine.connect() as conn:
            r1 = conn.execute(
                text(
                    """
                    SELECT MAX(COALESCE(captured_at_utc, "timestamp"))
                    FROM scan_progress WHERE scan_id = :sid
                    """
                ),
                {"sid": sid},
            ).fetchone()
            if r1 and r1[0] is not None:
                latest_this = str(r1[0])
            r2 = conn.execute(
                text('SELECT MAX(COALESCE(captured_at_utc, "timestamp")) FROM scan_progress')
            ).fetchone()
            if r2 and r2[0] is not None:
                latest_global = str(r2[0])
    except SQLAlchemyError:
        pass

    heartbeat: Optional[dict] = None
    try:
        with engine.connect() as conn:
            hb = conn.execute(
                text(
                    """
                    SELECT last_started_at_utc, last_finished_at_utc, last_status, last_message
                    FROM tracker_heartbeat
                    WHERE name = 'default'
                    LIMIT 1
                    """
                )
            ).fetchone()
            if hb:
                heartbeat = {
                    "last_started_at_utc": str(hb[0]) if hb[0] is not None else None,
                    "last_finished_at_utc": str(hb[1]) if hb[1] is not None else None,
                    "last_status": str(hb[2] or "") if hb[2] is not None else None,
                    "last_message": str(hb[3] or "") if hb[3] is not None else None,
                }
    except SQLAlchemyError:
        heartbeat = None

    manual_sync_disabled = _is_terminal_parent_scan_status((parent or {}).get("scan_status")) if parent else False

    benchmark_context: Optional[dict] = None
    if parent:
        scan_name = parent.get("scan_name") or ""
        prefill = infer_add_benchmark_defaults_from_scan_name(scan_name)
        ins = insights or {}
        form_sections = build_form_sections_meta(engine, sid, parent, ins if insights else None)
        ev = form_sections.get("effective_volumes") or {}
        try:
            total_files_f = float(ev.get("total_files") or 0)
        except Exception:
            total_files_f = 0.0
        try:
            total_gb_f = float(ev.get("total_gb") or 0)
        except Exception:
            total_gb_f = 0.0

        dur_h = duration_hours_between(parent.get("started_at"), parent.get("completed_at"))

        tp_preview = None
        if dur_h is not None and dur_h > 0:
            tp_preview = compute_throughputs(
                total_gb=total_gb_f,
                total_files=total_files_f,
                duration_hours=dur_h,
                scanner_count=DEFAULT_SCANNER_COUNT,
                thread_count=DEFAULT_THREAD_COUNT,
            )

        ts = split_iso_for_display(parent.get("started_at"))
        te = split_iso_for_display(parent.get("completed_at"))

        benchmark_context = {
            "prefill": prefill,
            "timing": {
                "started_at": parent.get("started_at"),
                "completed_at": parent.get("completed_at"),
                "duration_hours": dur_h,
            },
            "timing_fields": {
                "scan_start_date": ts.get("date"),
                "scan_start_time": ts.get("time"),
                "scan_end_date": te.get("date"),
                "scan_end_time": te.get("time"),
                "enum_end_date": te.get("date"),
                "enum_end_time": te.get("time"),
            },
            "run_metrics": form_sections.get("run_metrics"),
            "file_set_details": form_sections.get("file_set_details"),
            "findings_prefill": form_sections.get("findings_prefill"),
            "effective_volumes": ev,
            "volumes_for_throughput": {
                "total_files": int(ev.get("total_files") or 0),
                "total_gb": total_gb_f,
                "note": "File-set preset from Postgres + scan_insights overlay — matches Streamlit.",
            },
            "throughput_preview": tp_preview,
            "throughput_defaults": {
                "scanner_count": DEFAULT_SCANNER_COUNT,
                "thread_count": DEFAULT_THREAD_COUNT,
            },
        }

    return {
        "scan_id": sid,
        "parent": parent,
        "environment_name": environment_name,
        "resolved_environment_id": resolved_env_id,
        "insights": insights,
        "benchmark_context": benchmark_context,
        "latest_snapshot_this_scan": latest_this,
        "latest_snapshot_global": latest_global,
        "tracker_heartbeat": heartbeat,
        "manual_sync_disabled": manual_sync_disabled,
    }


@app.get("/api/scan/{scan_id}/classifier-hits")
async def get_scan_classifier_hits(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    live: bool = Query(
        False,
        description="When true, refresh counts from BigID attributes API (live), overriding cached DB rows.",
    ),
):
    """
    Per-classifier / attribute hits (same data path as legacy Scan Progress).
    If DB has no rows, automatically tries one live BigID fetch when environment is configured.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")

    rows, note, hint, fallback_names, cur_build, prev_build = load_classifier_hits_from_db(engine, sid)
    source = "db"

    if not rows:
        live_rows, live_note, live_hint = fetch_live_classifier_hits_from_bigid(engine, sid)
        if live_rows:
            rows, note, hint = live_rows, live_note, live_hint
            source = "live"
    elif live:
        live_rows, live_note, live_hint = fetch_live_classifier_hits_from_bigid(engine, sid)
        if live_rows:
            rows, note, hint = live_rows, live_note, live_hint
            source = "live"

    return {
        "scan_id": sid,
        "rows": rows,
        "note": note,
        "total_count_hint": hint,
        "fallback_classifier_names": fallback_names,
        "source": source,
        "current_build": cur_build,
        "previous_build": prev_build,
    }


def _is_active_service_status(status: Any) -> bool:
    """Same 'active' set as serviceTone() in ScanProgressPage.tsx."""
    return str(status or "").strip().lower() in ("active", "running", "ok", "up")


@app.get("/api/scan/{scan_id}/services-status")
async def get_scan_services_status(scan_id: str, token: str = Depends(oauth2_scheme)):
    """
    Per-service status history for the scan's environment (kubectl-style view).

    Slots = every capture attempt in service_capture_log, plus any sample time the log
    doesn't cover (rows written before the log existed). Every pod's "current" state is
    taken from the latest successful slot, so the counts and the Last 5 chips agree.
    A pod with no sample at that slot is NOT REPORTED and is left out of the counts.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    env_id = resolve_env_id_for_scan(engine, sid)
    if env_id is None:
        return {"available": False, "message": "Could not resolve environment for this scan.", "services": []}

    period = resolve_scan_period_window(engine, sid)
    now = datetime.utcnow()
    is_running = bool(period.get("is_running"))
    start_b = period.get("period_start")
    end_b = period.get("period_end") or now
    if not isinstance(start_b, datetime):
        start_b = now - timedelta(hours=24)
    if not isinstance(end_b, datetime):
        end_b = now
    try:
        slot_min = max(1, min(int(os.getenv("TRACKER_SERVICE_STATUS_MINUTES", "5") or "5"), 60))
    except ValueError:
        slot_min = 5
    # Samples are stamped at the start of their slot, so the slot that contains the scan start
    # must be included; otherwise the first sample of a scan is dropped by the BETWEEN filter.
    if isinstance(start_b, datetime):
        start_b = start_b.replace(second=0, microsecond=0) - timedelta(minutes=start_b.minute % slot_min)

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT service_key, captured_at_utc, name, service_type, group_name, status, message,
                       running_threads, total_threads, cpu_pct, update_date_bigid, created_at_bigid
                FROM service_status_samples
                WHERE environment_id = :eid AND captured_at_utc BETWEEN :start AND :end
                ORDER BY service_key, captured_at_utc ASC
            """), {"eid": env_id, "start": start_b, "end": end_b}).fetchall()
            try:
                caps = conn.execute(text("""
                    SELECT captured_at_utc, outcome, item_count, error
                    FROM service_capture_log
                    WHERE environment_id = :eid AND captured_at_utc BETWEEN :start AND :end
                    ORDER BY captured_at_utc ASC
                """), {"eid": env_id, "start": start_b, "end": end_b}).fetchall()
            except SQLAlchemyError:
                caps = []  # log table not created yet
    except SQLAlchemyError as e:
        return {"available": False, "message": f"Service status history unavailable: {e}", "services": []}

    # Samples by slot time, and the set of service keys present at each.
    keys_at: Dict[datetime, set] = {}
    for r in rows:
        keys_at.setdefault(r[1], set()).add(str(r[0]))
    log_by_at: Dict[datetime, tuple] = {}
    for c in caps:
        log_by_at[c[0]] = (c[1] or "", int(c[2] or 0), c[3] or "")

    all_times = sorted(set(keys_at) | set(log_by_at))
    slots = []
    for at in all_times:
        if at in log_by_at:
            oc, cnt, err = log_by_at[at]
        else:
            oc, cnt, err = "ok", len(keys_at.get(at, ())), ""
        slots.append({"at": str(at), "outcome": oc, "item_count": cnt, "error": err})
    ok_times = [at for at in all_times if (log_by_at.get(at, ("ok",))[0] == "ok")]
    ref_dt = ok_times[-1] if ok_times else (all_times[-1] if all_times else None)
    last_slot = slots[-1] if slots else None
    capture = {
        "last_at": last_slot["at"] if last_slot else None,
        "last_outcome": last_slot["outcome"] if last_slot else "",
        "last_error": last_slot["error"] if last_slot else "",
        "last_ok_at": str(ok_times[-1]) if ok_times else None,
        # True when a scan is still running and no successful capture has landed for longer than
        # ~2.5 slots. Sampling only runs while a scan is active, so a finished scan is not a stall.
        "is_lagging": is_running and ((not ok_times) or (now - ok_times[-1]) > timedelta(minutes=slot_min * 2.5)),
        "slots": slots[-10:],
    }

    if not rows:
        return {
            "available": bool(slots),
            "environment_id": env_id,
            "message": (
                "No services were reported in this window." if slots
                else "No service samples in this window yet. The tracker records one sample per service every few minutes."
            ),
            "capture": capture,
            "services": [],
        }

    by_key: Dict[str, List[Any]] = {}
    for r in rows:
        by_key.setdefault(str(r[0]), []).append(r)

    # Line chart: active pods per service (type) at every capture slot. A failed or missing
    # capture is None (a gap), not 0, so an outage is not read as "no pods".
    active_at: Dict[tuple, int] = {}
    series_names = set()
    for r in rows:
        grp = str(r[3] or r[2] or r[0])
        series_names.add(grp)
        if _is_active_service_status(r[5]):
            active_at[(r[1], grp)] = active_at.get((r[1], grp), 0) + 1
    series_groups: Dict[str, List[Optional[int]]] = {}
    for name in sorted(series_names):
        series_groups[name] = [
            active_at.get((at, name), 0) if log_by_at.get(at, ("ok",))[0] == "ok" else None
            for at in all_times
        ]

    stale_after = timedelta(minutes=slot_min * 2.5)
    # A pod is stale when it is missing from the latest successful capture (or its last sample is
    # more than ~2.5 slots behind it). Judged against captures, not the wall clock, so pods that are
    # still reported in the latest capture are never marked stale. A stopped sampler is shown by
    # capture.is_lagging instead.
    ref_time = ref_dt if ref_dt is not None else (now if is_running else end_b)
    first_seen_slack = timedelta(minutes=slot_min * 1.5)
    services = []
    for key, samples in by_key.items():
        at_ref = next((r for r in samples if r[1] == ref_dt), None) if ref_dt is not None else None
        current = at_ref or samples[-1]
        first = samples[0]
        changes = sum(1 for a, b in zip(samples, samples[1:]) if (a[5] or "") != (b[5] or ""))
        services.append({
            "service_key": key,
            "name": current[2] or key,
            "type": current[3] or "",
            "group": current[4] or "",
            "status": current[5] or "",
            "message": current[6] or "",
            "running_threads": current[7] or 0,
            "total_threads": current[8] or 0,
            "cpu_pct": current[9],
            "update_date": str(current[10]) if current[10] else None,
            # BigID's createDate is the pod's real creation time. Rows written before it was stored
            # fall back to the first sample, which is only an upper bound.
            "created_at": str(current[11] or first[1]),
            "created_at_is_exact": current[11] is not None,
            "last_seen": str(samples[-1][1]),
            "changes": changes,
            "is_new": (first[1] - start_b) > first_seen_slack,
            "is_stale": (ref_time - samples[-1][1]) > stale_after,
            # No sample at the reference slot: the pod was not in the latest good capture.
            "not_reported": ref_dt is not None and at_ref is None,
            "history": [{"status": r[5] or "", "captured_at": str(r[1])} for r in samples[-5:]],
        })
    services.sort(key=lambda svc: (not svc["is_new"], svc["name"].lower()))
    return {
        "available": True,
        "environment_id": env_id,
        "message": "",
        "window_start": str(start_b),
        "window_end": str(end_b),
        "period_is_running": is_running,
        "sample_minutes": slot_min,
        "capture": capture,
        "services": services,
        "series": {
            "times": [str(at) for at in all_times],
            "groups": series_groups,
        },
    }


@app.get("/api/scan/{scan_id}/scanner-series")
async def get_scanner_performance_route(scan_id: str, token: str = Depends(oauth2_scheme)):
    """Time-series scanner group + pod samples for charts (requires tracker tables in DB)."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    
    try:
        return load_scanner_performance_series(engine, sid)
    except SQLAlchemyError as e:
        # Rollback any failed transaction
        try:
            with engine.connect() as conn:
                conn.rollback()
        except:
            pass
        # Return empty data instead of error
        return {
            "available": False,
            "scan_id": sid,
            "environment_id": None,
            "window_start": None,
            "window_end": None,
            "groups": [],
            "individuals": [],
            "message": f"Database error: {str(e)}"
        }


@app.get("/api/scan/{scan_id}/log-correlation")
async def get_scan_log_correlation_route(scan_id: str, token: str = Depends(oauth2_scheme)):
    """Latest AI correlation / root-cause narrative row for this scan (scan_log_correlations)."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return load_scan_log_correlation(engine, sid)


@app.get("/api/scan/{scan_id}/log-errors")
async def get_scan_log_errors_for_scan_route(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    limit: int = Query(40, ge=1, le=200),
):
    """Scanner / subsystem errors for this scan's start→end period (time window, not scan_id alone)."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return load_scan_log_errors_for_scan(engine, sid, limit=limit)


@app.get("/api/scan/{scan_id}/progress-history")
async def get_scan_progress_history(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    limit: int = Query(3000, ge=1, le=8000),
):
    """Full `scan_progress` time series for one scan (tracker snapshots), oldest → newest."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    lim = int(limit)
    sql = text(
        """
        SELECT
            pg.id,
            pg.scan_id,
            spar.scan_name,
            pg.environment_id,
            pg."timestamp",
            pg.captured_at_utc,
            pg.scan_state,
            pg.completed_sub_scans,
            pg.failed_sub_scans,
            pg.total_sub_scans,
            pg.completed_parts,
            pg.failed_parts,
            pg.total_parts_scanned,
            pg.total_classifications,
            pg.newly_added_classifications,
            pg.false_removed_classifications,
            pg.objects_scanned,
            pg.objects_failed,
            pg.objects_with_pi
        FROM scan_progress pg
        LEFT JOIN scan_parents spar ON spar.scan_id = pg.scan_id
        WHERE pg.scan_id = :sid
        ORDER BY COALESCE(pg.captured_at_utc, pg."timestamp") ASC NULLS LAST, pg.id ASC
        LIMIT :lim
        """
    )
    # Get current scan status from scan_parents
    scan_status_overall = ""
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"sid": sid, "lim": lim}).fetchall()
            cnt = conn.execute(
                text("SELECT COUNT(*) FROM scan_progress WHERE scan_id = :sid"), {"sid": sid}
            ).scalar()
            total = int(cnt or 0)

            # Get overall scan status
            sp_row = conn.execute(
                text("SELECT scan_status FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid}
            ).fetchone()
            scan_status_overall = str(sp_row[0] or "") if sp_row else ""
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=f"scan_progress unavailable: {ex}") from ex

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r[0]) if r[0] is not None else None,
                "scan_id": str(r[1] or ""),
                "scan_name": r[2],
                "environment_id": r[3],
                "timestamp": str(r[4]) if r[4] is not None else None,
                "captured_at_utc": str(r[5]) if r[5] is not None else None,
                "scan_state": r[6],
                "completed_sub_scans": int(r[7] or 0) if r[7] is not None else None,
                "failed_sub_scans": int(r[8] or 0) if r[8] is not None else None,
                "total_sub_scans": int(r[9] or 0) if r[9] is not None else None,
                "completed_parts": int(r[10] or 0) if r[10] is not None else None,
                "failed_parts": int(r[11] or 0) if r[11] is not None else None,
                "total_parts_scanned": int(r[12] or 0) if r[12] is not None else None,
                "total_classifications": int(r[13] or 0) if r[13] is not None else None,
                "newly_added_classifications": int(r[14] or 0) if r[14] is not None else None,
                "false_removed_classifications": int(r[15] or 0) if r[15] is not None else None,
                "objects_scanned": int(r[16] or 0) if r[16] is not None else None,
                "objects_failed": int(r[17] or 0) if r[17] is not None else None,
                "objects_with_pi": int(r[18] or 0) if r[18] is not None else None,
            }
        )
    return {"items": items, "total": total, "returned": len(items), "scan_status": scan_status_overall}


def _scan_progress_where_clause(environment_id: Optional[int], scan_id: Optional[str]) -> tuple[str, dict]:
    parts: List[str] = []
    params: dict = {}
    if environment_id is not None:
        parts.append("sp.environment_id = :eid")
        params["eid"] = int(environment_id)
    sid = str(scan_id or "").strip()
    if sid:
        parts.append("sp.scan_id = :scan_sid")
        params["scan_sid"] = sid
    if not parts:
        return "", {}
    return " AND " + " AND ".join(parts), params


_SCAN_PROGRESS_OUTER_ORDER_SQL = {
    "snapshot_desc": "COALESCE(r.captured_at_utc, r.ts_col) DESC NULLS LAST, r.scan_id DESC",
    "snapshot_asc": "COALESCE(r.captured_at_utc, r.ts_col) ASC NULLS LAST, r.scan_id ASC",
    "scan_id_desc": "r.scan_id DESC",
    "scan_id_asc": "r.scan_id ASC",
    "environment": "LOWER(COALESCE(te.env_name, '')) ASC, r.scan_id ASC",
    "environment_desc": "LOWER(COALESCE(te.env_name, '')) DESC NULLS LAST, r.scan_id DESC",
    "state": "LOWER(COALESCE(r.scan_state::text, '')) ASC, r.scan_id ASC",
    "state_desc": "LOWER(COALESCE(r.scan_state::text, '')) DESC NULLS LAST, r.scan_id DESC",
    "objects_desc": "COALESCE(r.objects_scanned, 0) DESC NULLS LAST, r.scan_id DESC",
    "objects_asc": "COALESCE(r.objects_scanned, 0) ASC NULLS LAST, r.scan_id ASC",
    "scan_name_asc": "LOWER(COALESCE(r.scan_name::text, '')) ASC NULLS LAST, r.scan_id ASC",
    "scan_name_desc": "LOWER(COALESCE(r.scan_name::text, '')) DESC NULLS LAST, r.scan_id DESC",
    "parts_desc": "COALESCE(r.total_parts_scanned, 0) DESC NULLS LAST, r.scan_id DESC",
    "parts_asc": "COALESCE(r.total_parts_scanned, 0) ASC NULLS LAST, r.scan_id ASC",
    "classify_desc": "COALESCE(r.total_classifications, 0) DESC NULLS LAST, r.scan_id DESC",
    "classify_asc": "COALESCE(r.total_classifications, 0) ASC NULLS LAST, r.scan_id ASC",
    "subscans_desc": "COALESCE(r.total_sub_scans, 0) DESC NULLS LAST, r.scan_id DESC",
    "subscans_asc": "COALESCE(r.total_sub_scans, 0) ASC NULLS LAST, r.scan_id ASC",
}


@app.get("/api/scan/progress/latest")
async def get_scan_progress_latest(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None),
    scan_id: Optional[str] = Query(None),
    sort_by: str = Query("snapshot_desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Latest snapshot per scan from `scan_progress` (tracker parity), one row per scan_id."""
    wc, wp = _scan_progress_where_clause(environment_id, scan_id)
    sb = str(sort_by or "").strip()
    if sb not in _SCAN_PROGRESS_OUTER_ORDER_SQL:
        sb = "snapshot_desc"
    order_outer = _SCAN_PROGRESS_OUTER_ORDER_SQL[sb]
    ranked = f"""
WITH ranked AS (
    SELECT
        sp.id,
        sp.scan_id,
        spar.scan_name,
        sp.environment_id,
        sp.scan_state,
        sp.completed_sub_scans,
        sp.failed_sub_scans,
        sp.total_sub_scans,
        sp.total_parts_scanned,
        sp.total_classifications,
        sp.objects_scanned,
        sp.objects_failed,
        sp.objects_with_pi,
        sp.captured_at_utc,
        sp."timestamp" AS ts_col,
        ROW_NUMBER() OVER (
            PARTITION BY sp.scan_id
            ORDER BY COALESCE(sp.captured_at_utc, sp."timestamp") DESC NULLS LAST, sp.id DESC
        ) AS rn
    FROM scan_progress sp
    LEFT JOIN scan_parents spar ON spar.scan_id = sp.scan_id
    WHERE 1 = 1
    {wc}
)
"""
    count_sql = text(ranked + " SELECT COUNT(*) FROM ranked WHERE rn = 1 ")
    page_sql = text(
        ranked
        + f"""
SELECT
    r.scan_id,
    r.scan_name,
    r.environment_id,
    COALESCE(te.env_name, '#' || r.environment_id::text) AS environment_name,
    r.scan_state,
    r.completed_sub_scans,
    r.failed_sub_scans,
    r.total_sub_scans,
    r.total_parts_scanned,
    r.total_classifications,
    r.objects_scanned,
    r.objects_failed,
    r.objects_with_pi,
    COALESCE(r.captured_at_utc::text, r.ts_col::text) AS last_snapshot_at
FROM ranked r
LEFT JOIN test_environments te ON te.id = r.environment_id
WHERE r.rn = 1
ORDER BY {order_outer}
LIMIT :limit OFFSET :offset
"""
    )
    binds = {**wp, "limit": int(limit), "offset": int(offset)}
    cb = {k: v for k, v in binds.items() if k not in ("limit", "offset")}
    try:
        with engine.connect() as conn:
            total = int(conn.execute(count_sql, cb).scalar() or 0)
            rows = conn.execute(page_sql, binds).fetchall()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="scan_progress unavailable (sync tracker data or create table in Postgres).",
        )
    items = [
        {
            "scan_id": str(r[0] or ""),
            "scan_name": r[1],
            "environment_id": r[2],
            "environment_name": r[3],
            "scan_state": r[4],
            "completed_sub_scans": int(r[5] or 0) if r[5] is not None else None,
            "failed_sub_scans": int(r[6] or 0) if r[6] is not None else None,
            "total_sub_scans": int(r[7] or 0) if r[7] is not None else None,
            "total_parts_scanned": int(r[8] or 0) if r[8] is not None else None,
            "total_classifications": int(r[9] or 0) if r[9] is not None else None,
            "objects_scanned": int(r[10] or 0) if r[10] is not None else None,
            "objects_failed": int(r[11] or 0) if r[11] is not None else None,
            "objects_with_pi": int(r[12] or 0) if r[12] is not None else None,
            "last_snapshot_at": r[13],
        }
        for r in rows
    ]
    return {"items": items, "total": total}


@app.post("/api/scan/sync-insights")
async def post_sync_scan_insights(payload: ScanInsightsSyncPayload, token: str = Depends(oauth2_scheme)):
    """
    Fetch parent-scan insights + classifier attributes from BigID and upsert into Postgres `scan_insights`.
    Requires `scan_parents.environment_id` pointing at a row in `test_environments` with a valid `base_url`.
    Also captures classifier snapshot on first sync for manually-started scans (no workflow).
    """
    try:
        sid = payload.scan_id.strip()

        # First, resync parent scan status from BigID to get latest status
        # This ensures manual sync updates the scan status, not just insights
        try:
            with engine.connect() as conn:
                env_row = conn.execute(
                    text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                    {"sid": sid}
                ).fetchone()
                if env_row and env_row[0]:
                    env_id = int(env_row[0])
                    resync_scan_parents_for_environment(engine, env_id, limit=100)
        except Exception:
            pass  # Continue even if resync fails

        msg = sync_scan_insights(engine, sid)

        # Also sync collections (scan_parts and scans) even for terminal scans
        try:
            with engine.connect() as conn:
                env_row = conn.execute(
                    text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                    {"sid": sid}
                ).fetchone()
                if env_row and env_row[0]:
                    env_id = int(env_row[0])
                    env_base_url = conn.execute(
                        text("SELECT base_url FROM test_environments WHERE id = :eid LIMIT 1"),
                        {"eid": env_id}
                    ).fetchone()
                    if env_base_url and env_base_url[0]:
                        env_dict = {"id": env_id}
                        tok = Get_auth_token(env_base_url[0], env_dict)
                        sync_scan_part_records_for_scan(
                            engine,
                            env_base_url[0],
                            env_dict,
                            tok,
                            sid,
                            allow_terminal=True  # Allow fetching for completed scans
                        )
        except Exception as ex:
            logger.error(f"Failed to sync collections for {sid}: {ex}", exc_info=True)

        # Lazily capture classifier snapshot on first sync for non-workflow scans.
        try:
            with engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM scan_classifier_snapshot WHERE scan_id = :sid LIMIT 1"),
                    {"sid": sid},
                ).fetchone()
                if not exists:
                    data = _compute_classifier_snapshot(conn, sid)
                    if "error" not in data:
                        _write_classifier_snapshot_cache(conn, sid, data)
        except Exception:
            pass
        return {"ok": True, "detail": msg}
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except RuntimeError as ex:
        raise HTTPException(status_code=502, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=502, detail=str(ex))


@app.post("/api/scan/resync-parents")
async def post_resync_scan_parents(payload: ResyncParentsPayload, token: str = Depends(oauth2_scheme)):
    """
    Pull running + completed parent scans from BigID for one environment and upsert `scan_parents`
    (Streamlit **Manual Re-sync (API)** parity).
    """
    try:
        out = resync_scan_parents_for_environment(engine, int(payload.environment_id), int(payload.limit))
        return {"ok": True, **out}
    except RuntimeError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=502, detail=str(ex))


@app.post("/api/scan/mark-stopped")
async def post_mark_scan_stopped(payload: MarkScanStoppedPayload, token: str = Depends(oauth2_scheme)):
    """
    Mark a parent scan as stopped in Postgres. The tracker then skips it (is_active = FALSE) and
    BigID resyncs keep it at Stopped until a terminal state arrives. Scans already in a terminal
    state are left as they are. This does not stop the scan in BigID.
    """
    sid = payload.scan_id.strip()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT scan_status FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid},
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Scan {sid} is not in scan_parents")
            current = str(row[0] or "").strip()
            if _is_terminal_state(current):
                return {"ok": True, "scan_id": sid, "scan_status": current, "changed": False}
            conn.execute(
                text(
                    """
                    UPDATE scan_parents
                    SET scan_status = 'Stopped',
                        is_active = FALSE,
                        manually_stopped_at = CURRENT_TIMESTAMP,
                        last_seen_at = CURRENT_TIMESTAMP
                    WHERE scan_id = :sid
                    """
                ),
                {"sid": sid},
            )
        # Same close-out the tracker does on a terminal upsert: scan_running steps become
        # 'error' and the run stops waiting on this scan.
        try:
            from v2_api.workflow_scan_reconcile import complete_workflow_steps_for_scan  # noqa: PLC0415

            complete_workflow_steps_for_scan(engine, sid, "Stopped")
        except Exception:
            logger.debug("mark-stopped: workflow step close-out failed", exc_info=True)
        return {"ok": True, "scan_id": sid, "scan_status": "Stopped", "changed": True}
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


def _read_scan_collection_rows(sid: str, collection: str, *, limit: int, new_only: bool = False) -> List[Dict[str, Any]]:
    """Latest rows the tracker stored in scan_part_records for one collection, newest first."""
    state_clause = "AND LOWER(TRIM(state)) = 'new'" if new_only else ""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT id, subscan_id, state, record_key, captured_at_utc, record_payload
                FROM scan_part_records
                WHERE scan_id = :sid AND collection = :collection {state_clause}
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT :limit
            """),
            {"sid": sid, "collection": collection, "limit": int(limit)},
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        payload = r[5]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                pass
        captured = r[4]
        out.append({
            "id": r[0],
            "subscan_id": r[1],
            "state": r[2],
            "record_key": r[3],
            "captured_at_utc": captured.isoformat() if hasattr(captured, "isoformat") else captured,
            "payload": payload,
        })
    return out


def _count_scan_collection_rows(sid: str, collection: str, *, new_only: bool = False) -> int:
    state_clause = "AND LOWER(TRIM(state)) = 'new'" if new_only else ""
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT COUNT(*) FROM scan_part_records WHERE scan_id = :sid AND collection = :collection {state_clause}"),
            {"sid": sid, "collection": collection},
        ).fetchone()
    return int(row[0] or 0) if row else 0


@app.get("/api/scan/{scan_id}/scan-parts")
def get_scan_parts(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    limit: int = Query(5, ge=1, le=50),
):
    """scan_parts snapshot the tracker stored: latest rows (any state) and latest rows with state New."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return {
        "count": _count_scan_collection_rows(sid, "scan_parts"),
        "new_filter_count": _count_scan_collection_rows(sid, "scan_parts", new_only=True),
        "items": _read_scan_collection_rows(sid, "scan_parts", limit=limit),
        "new_items": _read_scan_collection_rows(sid, "scan_parts", limit=limit, new_only=True),
    }


@app.get("/api/scan/{scan_id}/subscan-ids")
async def get_scan_subscan_ids(scan_id: str, token: str = Depends(oauth2_scheme)):
    """Parent scan ID plus the sub-scan IDs captured for it in scan_part_records."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    subscan_ids = _load_subscan_ids_from_db(engine, sid)
    return {"scan_id": sid, "count": len(subscan_ids), "subscan_ids": subscan_ids}


@app.get("/api/scan/{scan_id}/scan-records")
def get_scan_records(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    limit: int = Query(15, ge=1, le=100),
):
    """Latest records the tracker stored from BigID's scans collection for this scan_id."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return {
        "count": _count_scan_collection_rows(sid, "scans"),
        "new_filter_count": 0,
        "items": _read_scan_collection_rows(sid, "scans", limit=limit),
        "new_items": [],
    }


@app.get("/api/scan/{scan_id}/scan-parts-report-errors")
def get_scan_parts_report_errors(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
):
    """Error rows from each subscan's BigID scan-parts CSV report (downloaded, not stored)."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    try:
        return fetch_scan_parts_report_errors(engine, sid)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=502, detail=str(ex))


@app.post("/api/scan/{scan_id}/refresh-collection")
async def post_refresh_scan_collection(
    scan_id: str,
    payload: dict = Body(...),
    token: str = Depends(oauth2_scheme),
):
    """
    Trigger a refresh of scan collection data (scan_parts or scans) from BigID.
    Returns refresh status.
    """
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")

    collection = payload.get("collection", "all")
    if collection not in ("scan_parts", "scans", "all"):
        raise HTTPException(status_code=400, detail="collection must be 'scan_parts', 'scans', or 'all'")

    try:
        with engine.connect() as conn:
            env_row = conn.execute(
                text("SELECT environment_id FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                {"sid": sid}
            ).fetchone()
            if not env_row or not env_row[0]:
                raise HTTPException(status_code=404, detail="scan or environment not found")

            env_id = int(env_row[0])
            env_base_url = conn.execute(
                text("SELECT base_url FROM test_environments WHERE id = :eid LIMIT 1"),
                {"eid": env_id}
            ).fetchone()

            if not env_base_url or not env_base_url[0]:
                raise HTTPException(status_code=404, detail="environment base_url not found")

            env_dict = {"id": env_id}
            tok = Get_auth_token(env_base_url[0], env_dict)

            count = sync_scan_part_records_for_scan(
                engine,
                env_base_url[0],
                env_dict,
                tok,
                sid,
                collection=collection,
                allow_terminal=True
            )

            return {
                "ok": True,
                "collection": collection,
                "count": count,
                "message": f"Fetched {count} records for {collection}",
            }
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"Failed to refresh collections for {sid}: {ex}", exc_info=True)
        return {
            "ok": False,
            "error": str(ex),
        }


def _get_latest_progress_state(engine, scan_id: str) -> Optional[str]:
    """Get latest scan state from progress history (more accurate than parent scan status)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT scan_state FROM scan_progress
                    WHERE scan_id = :sid
                    ORDER BY captured_at_utc DESC LIMIT 1
                """),
                {"sid": scan_id}
            ).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        pass
    return None


def _load_env_row_by_id(engine, env_id: int) -> Optional[dict]:
    """Load environment row by ID."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, env_name, base_url FROM test_environments WHERE id = :eid LIMIT 1"),
            {"eid": env_id}
        ).fetchone()
        if row:
            return {"id": row[0], "env_name": row[1], "base_url": row[2]}
    return None


def _fetch_live_scan_status_from_bigid(env_row: dict, scan_id: str) -> Optional[str]:
    """
    Fetch current scan status from BigID API.
    TODO: BigID's parent-scans API may not return real-time status updates for "Stop Requested" state.
    The status is returned from database as a fallback. Consider using child-scans or workflow API
    to get real-time "Stop Requested" state that reflects actual scan stopping progress.
    """
    # Disabled for now - BigID API not returning real-time status
    # Will be enhanced to fetch from insights/child-scans API
    return None


def _maybe_ts(val) -> Optional[str]:
    if val is None:
        return None
    return str(val)


def _workflow_step_row_dict(r: tuple) -> dict:
    """Single row from workflow_steps + test_environments join (see _WORKFLOW_STEP_SELECT column order)."""
    def _bool(v, default: bool = True) -> bool:
        if v is None:
            return default
        try:
            return bool(v)
        except Exception:
            return default

    return {
        "id": r[0],
        "run_id": r[1],
        "seq": r[2],
        "environment_id": r[3],
        "environment_name": r[4],
        "env_is_busy": _bool(r[5], False),
        "ds_kind": r[6],
        "ds_name": r[7],
        "scan_kind": r[8],
        "file_set_type": r[9],
        "is_enabled": _bool(r[10], True),
        "build_version": r[11],
        "build_prefix": r[12],
        "scan_id": r[13],
        "status": r[14],
        "last_error": r[15],
        "started_at": _maybe_ts(r[16]),
        "finished_at": _maybe_ts(r[17]),
        "ds_id": r[18],
        "scan_profile_id": r[19],
        "scan_profile_name": r[20],
        "is_acl": _bool(r[21], False),
        "is_ocr": _bool(r[22], False),
        "is_structured_ff": _bool(r[23], False),
        "is_ols": _bool(r[24], False),
        "is_lite": _bool(r[25], False),
        "notes": r[26],
        "feature_flags": r[27],
    }


_WORKFLOW_STEP_SELECT = """
    SELECT
        ws.id, ws.run_id, ws.seq, ws.environment_id,
        te.env_name AS environment_name,
        COALESCE(te.is_busy, FALSE) AS environment_is_busy,
        ws.ds_kind, ws.ds_name, ws.scan_kind, ws.file_set_type,
        ws.is_enabled, ws.build_version,
        CASE
            WHEN ws.build_version ~ '^\d+\.\d+'
            THEN SPLIT_PART(ws.build_version, '.', 1) || '-' || SPLIT_PART(ws.build_version, '.', 2)
            ELSE COALESCE(ws.build_version, '')
        END AS build_prefix,
        ws.scan_id, ws.status, ws.last_error,
        ws.started_at, ws.finished_at,
        ws.ds_id, ws.scan_profile_id, ws.scan_profile_name,
        ws.is_acl, ws.is_ocr, ws.is_structured_ff, ws.is_ols, ws.is_lite,
        ws.notes, ws.feature_flags
    FROM workflow_steps ws
    LEFT JOIN test_environments te ON ws.environment_id = te.id
"""


@app.get("/api/workflow/runs")
async def get_workflow_runs(
    u=Depends(get_current_user),
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated workflow orchestration runs (workflow_runner / Streamlit parity)."""
    try:
        wf = get_workflow_sql_engine()
        with wf.connect() as conn:
            # RBAC: Filter by user unless Admin or Developer
            user_filter = ""
            params = {"limit": limit, "offset": offset}
            if u["role"] not in ["Admin", "Developer"]:
                user_filter = "WHERE created_by = :uid"
                params["uid"] = u["id"]

            total_row = conn.execute(text(f"SELECT COUNT(*) FROM workflow_runs {user_filter}"), params).scalar()
            total = int(total_row or 0)
            res = conn.execute(
                text(
                    f"""
                    SELECT id, name, status, env1_id, env2_id, cooldown_minutes, last_error,
                           next_eligible_at, scan_template_id, started_at, finished_at
                    FROM workflow_runs
                    {user_filter}
                    ORDER BY id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).fetchall()
        runs = [
            {
                "id": r[0],
                "name": r[1],
                "status": r[2],
                "env1_id": r[3],
                "env2_id": r[4],
                "cooldown_minutes": r[5],
                "last_error": r[6],
                "next_eligible_at": _maybe_ts(r[7]),
                "scan_template_id": r[8],
                "started_at": _maybe_ts(r[9]),
                "finished_at": _maybe_ts(r[10]),
            }
            for r in res
        ]
        return {"runs": runs, "total": total}
    except SQLAlchemyError:
        return {"runs": [], "total": 0}


@app.get("/api/workflow/step-catalog")
async def workflow_step_catalog(token: str = Depends(oauth2_scheme)):
    """Default multi-step matrix (same as legacy planner) for checkbox UI before POST /api/workflow/runs."""
    from v2_api.workflow_defaults import default_workflow_steps_for_new_run  # noqa: PLC0415

    rows = default_workflow_steps_for_new_run()
    return {"steps": [{**row, "seq": i + 1} for i, row in enumerate(rows)]}


# Static path MUST be registered before `/api/workflow/steps/{run_id}` — otherwise `{run_id}` captures "batch" → 422.
@app.get("/api/workflow/steps/batch")
async def get_workflow_steps_batch(
    token: str = Depends(oauth2_scheme),
    run_ids: str = Query("", description="Comma-separated workflow run ids, max 10000."),
):
    """All steps for many runs — used so the SPA can mirror Streamlit metrics without per-row waterfalls."""
    try:
        parts = [p.strip() for p in (run_ids or "").split(",") if p.strip().isdigit()]
        ids = [int(x) for x in parts][:10000]
        if not ids:
            return {}
        stmt = text(
            _WORKFLOW_STEP_SELECT + " WHERE ws.run_id IN :ids ORDER BY ws.run_id ASC, ws.seq ASC"
        ).bindparams(bindparam("ids", expanding=True))
        with get_workflow_sql_engine().connect() as conn:
            rows = conn.execute(stmt, {"ids": ids}).fetchall()
        out: dict = {}
        for r in rows:
            rowd = _workflow_step_row_dict(r)
            rid = int(rowd["run_id"])
            out.setdefault(rid, []).append(rowd)
        return out
    except SQLAlchemyError:
        return {}


@app.get("/api/workflow/steps/{run_id}")
async def get_workflow_steps(run_id: int, u=Depends(get_current_user)):
    """Steps for a workflow run — joined with env names/busy flags (Streamlit parity)."""
    try:
        wf_engine = get_workflow_sql_engine()
        
        # Ownership check
        if u["role"] not in ["Admin", "Developer"]:
            with wf_engine.connect() as conn:
                owner = conn.execute(text("SELECT created_by FROM workflow_runs WHERE id = :rid"), {"rid": run_id}).scalar()
                if owner != u["id"]:
                    raise HTTPException(status_code=403, detail="You do not own this workflow")

        with wf_engine.connect() as conn:
            res = conn.execute(
                text(_WORKFLOW_STEP_SELECT + " WHERE ws.run_id = :r ORDER BY ws.seq"),
                {"r": run_id},
            ).fetchall()
        return [_workflow_step_row_dict(r) for r in res]
    except HTTPException:
        raise
    except SQLAlchemyError:
        return []


@app.get("/api/workflow/env-status")
async def get_workflow_env_status(u=Depends(get_current_user)):
    """
    Env 🟢/🔴 strip: active envs marked busy when `test_environments.is_busy`
    OR a running workflow references the env in an active step-state
    (running, ds_created, ds_test_ok, scan_created, scan_running, scan_waiting).
    Excludes pending/pre_action_done to avoid false positives.
    """
    try:
        with get_workflow_sql_engine().connect() as conn:
            res = conn.execute(_WORKFLOW_ENV_STATUS_SQL).fetchall()
        return [{"id": r[0], "env_name": r[1], "busy": bool(r[2])} for r in res]
    except SQLAlchemyError:
        return []


@app.post("/api/workflow/runs")
async def create_workflow_run_endpoint(payload: WorkflowCreatePayload, u=Depends(get_current_user)):
    """Create workflow run + step matrix (default from catalog or client-selected steps)."""
    try:
        from v2_api.workflow_ops import insert_workflow_run_with_steps  # noqa: PLC0415
        import logging
        logger = logging.getLogger(__name__)

        eng = get_workflow_sql_engine()
        steps_kw = None
        if payload.steps is not None and len(payload.steps) > 0:
            steps_kw = [s.model_dump() for s in payload.steps]
            enabled_count = sum(1 for s in steps_kw if s.get("is_enabled", True))
            logger.info(f"Creating workflow with {len(steps_kw)} steps ({enabled_count} enabled)")
        else:
            logger.info("Creating workflow with default steps (no steps provided in payload)")
        
        rid = insert_workflow_run_with_steps(
            eng,
            payload.name,
            payload.cooldown_minutes,
            payload.env1_id,
            payload.env2_id,
            steps_override=steps_kw,
            created_by=u["id"]
        )
        logger.info(f"Successfully created workflow run_id={rid}")
        if payload.auto_start:
            try:
                from v2_api.workflow_ops import workflow_control  # noqa: PLC0415

                workflow_control(eng, rid, "start", payload.env1_id, payload.env2_id)
                return {"id": rid, "message": "created", "status": "running"}
            except ValueError as e:
                # Run stays 'created' so it can be started manually from the UI once the env is free
                logger.warning(f"Auto-start failed for run_id={rid}: {e}")
                return {"id": rid, "message": "created", "status": "created", "start_error": str(e)}
        return {"id": rid, "message": "created", "status": "created"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workflow/runs/{run_id}/control")
async def workflow_run_control_endpoint(
    run_id: int,
    payload: WorkflowControlPayload,
    u=Depends(get_current_user),
):
    """Start / pause / resume / stop — updates DB like Streamlit query actions (no Streamlit required)."""
    try:
        from v2_api.workflow_ops import workflow_control  # noqa: PLC0415
        wf_engine = get_workflow_sql_engine()

        # Ownership check
        if u["role"] not in ["Admin", "Developer"]:
            with wf_engine.connect() as conn:
                owner = conn.execute(text("SELECT created_by FROM workflow_runs WHERE id = :rid"), {"rid": run_id}).scalar()
                if owner != u["id"]:
                    raise HTTPException(status_code=403, detail="You do not own this workflow")

        return workflow_control(
            wf_engine,
            run_id,
            payload.action,
            payload.env1_id,
            payload.env2_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/workflow/runs/{run_id}")
async def delete_workflow_run(run_id: int, u=Depends(get_current_user)):
    """Delete a workflow run and its steps (only when not running)."""
    wf_engine = get_workflow_sql_engine()
    try:
        # Ownership check
        if u["role"] not in ["Admin", "Developer"]:
            with wf_engine.connect() as conn:
                owner = conn.execute(text("SELECT created_by FROM workflow_runs WHERE id = :rid"), {"rid": run_id}).scalar()
                if owner != u["id"]:
                    raise HTTPException(status_code=403, detail="You do not own this workflow")

        with wf_engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM workflow_runs WHERE id = :rid"),
                {"rid": run_id},
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow run not found")
        if str(row[0] or "").strip().lower() == "running":
            raise HTTPException(status_code=409, detail="Cannot delete a running workflow. Stop it first.")
        with wf_engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_steps WHERE run_id = :rid"), {"rid": run_id})
            conn.execute(text("DELETE FROM workflow_runs WHERE id = :rid"), {"rid": run_id})
        return {"ok": True, "deleted_run_id": run_id}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Benchmark save (full, mirrors Streamlit Save Benchmark Run) ──────────────

def _get_or_create_id(conn, table: str, name_col: str, name_val: str) -> int:
    """Upsert a lookup row and return its id."""
    row = conn.execute(
        text(f"SELECT id FROM {table} WHERE {name_col} = :v"),
        {"v": name_val},
    ).fetchone()
    if row:
        return int(row[0])
    r2 = conn.execute(
        text(f"INSERT INTO {table} ({name_col}) VALUES (:v) RETURNING id"),
        {"v": name_val},
    ).fetchone()
    return int(r2[0])


def _get_or_create_build(conn, version: str) -> int:
    row = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": version}).fetchone()
    if row:
        return int(row[0])
    r2 = conn.execute(
        text("INSERT INTO builds (version) VALUES (:v) RETURNING id"), {"v": version}
    ).fetchone()
    return int(r2[0])


def _get_or_create_config(conn, scanner_count: int, thread_count: int, is_acl: bool,
                           is_ocr: bool, is_feature_flags: bool, feature_flags: Optional[str]) -> int:
    import hashlib as _hl
    ff = (feature_flags or "").strip()
    h = _hl.md5(f"{scanner_count}|{thread_count}|{is_acl}|{is_ocr}|{is_feature_flags}|{ff}".encode()).hexdigest()
    row = conn.execute(text("SELECT id FROM configurations WHERE hash = :h"), {"h": h}).fetchone()
    if row:
        return int(row[0])
    r2 = conn.execute(
        text("""
            INSERT INTO configurations
              (scanner_count, thread_count, is_acl, is_ocr, is_feature_flags, feature_flags, hash)
            VALUES (:sc, :tc, :acl, :ocr, :ff_bool, :ff, :h)
            RETURNING id
        """),
        {"sc": scanner_count, "tc": thread_count, "acl": is_acl, "ocr": is_ocr,
         "ff_bool": is_feature_flags, "ff": ff or None, "h": h},
    ).fetchone()
    return int(r2[0])


@app.post("/api/benchmarks/save")
async def save_benchmark(payload: BenchmarkSavePayload, u=Depends(get_current_user)):
    """
    Full benchmark save — equivalent to Streamlit 'Save Benchmark Run'.
    Creates/reuses lookup rows (builds, data_source_types, file_set_types, configurations)
    then inserts into benchmarks.
    """
    try:
        with engine.begin() as conn:
            scan_name = ""
            if payload.scan_id:
                row = conn.execute(
                    text("SELECT scan_name FROM scan_parents WHERE scan_id = :sid LIMIT 1"),
                    {"sid": payload.scan_id},
                ).fetchone()
                scan_name = str(row[0] or "") if row else ""
            canon = canonical_benchmark_fields({
                "scan_name": scan_name,
                "scenario_type": payload.scenario_type,
                "scan_type": payload.scan_type,
                "data_source_type": payload.data_source_type,
                "file_set_type": payload.file_set_type,
                "is_acl": payload.is_acl,
                "is_feature_flags": payload.is_feature_flags,
                "feature_flags": payload.feature_flags,
            })
            save_scenario_type = str(canon.get("matrix_file_set_type") or payload.scenario_type).strip()
            save_scan_type = str(canon.get("scan_type") or payload.scan_type).strip()
            save_data_source_type = str(canon.get("data_source_type") or payload.data_source_type).strip()
            save_file_set_type = str(canon.get("file_set_type") or payload.file_set_type).strip()
            save_is_acl = bool(canon.get("is_acl"))
            save_is_ff = bool(canon.get("is_feature_flags"))
            save_feature_flags = payload.feature_flags
            if save_is_ff and not str(save_feature_flags or "").strip():
                save_feature_flags = "Structured"

            build_id = _get_or_create_build(conn, payload.build_version.strip())
            dst_id = _get_or_create_id(conn, "data_source_types", "name", save_data_source_type)

            # file_set_types may have extra columns — upsert name only, update sizes if provided
            fst_row = conn.execute(
                text("SELECT id FROM file_set_types WHERE name = :n"), {"n": save_file_set_type}
            ).fetchone()
            if fst_row:
                fst_id = int(fst_row[0])
                if payload.total_files is not None or payload.total_size_gb is not None:
                    sets = []
                    p: dict = {"fid": fst_id}
                    if payload.total_files is not None:
                        sets.append("total_files = :tf"); p["tf"] = payload.total_files
                    if payload.total_folders is not None:
                        sets.append("total_folders = :tfo"); p["tfo"] = payload.total_folders
                    if payload.total_size_gb is not None:
                        sets.append("total_size_gb = :tsg"); p["tsg"] = payload.total_size_gb
                    if sets:
                        conn.execute(text(f"UPDATE file_set_types SET {', '.join(sets)} WHERE id = :fid"), p)
            else:
                r2 = conn.execute(
                    text("""
                        INSERT INTO file_set_types (name, data_type, total_files, total_folders, total_size_gb)
                        VALUES (:n, :dt, :tf, :tfo, :tsg) RETURNING id
                    """),
                    {"n": save_file_set_type,
                     "dt": payload.data_type or None,
                     "tf": payload.total_files, "tfo": payload.total_folders,
                     "tsg": payload.total_size_gb},
                ).fetchone()
                fst_id = int(r2[0])

            config_id = _get_or_create_config(
                conn, payload.scanner_count, payload.thread_count,
                save_is_acl, payload.is_ocr, save_is_ff, save_feature_flags
            )

            # Parse timestamps
            def _parse_ts(v: Optional[str]):
                if not v or not v.strip():
                    return None
                try:
                    from datetime import datetime as _dt
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try:
                            return _dt.strptime(v.strip(), fmt)
                        except Exception:
                            continue
                except Exception:
                    pass
                return None

            row = conn.execute(
                text("""
                    INSERT INTO benchmarks (
                        build_id, config_id, run_date, scenario_type, scan_type,
                        data_source_type_id, file_set_type_id,
                        scan_start_time, scan_end_time, total_scan_hours,
                        throughput_cluster_gb_hr, throughput_scanner_gb_hr, throughput_thread_gb_hr,
                        throughput_cluster_files_hr, throughput_scanner_files_hr, throughput_thread_files_hr,
                        total_catalog_items, scan_id, is_active, is_release, updated_at, created_by
                    ) VALUES (
                        :bid, :cid, :rd, :st, :sct,
                        :dst, :fst,
                        :ss, :se, :tsh,
                        :tcg, :tsg2, :ttg,
                        :tcf, :tsf, :ttf,
                        :tci, :sid, TRUE, :isr, CURRENT_TIMESTAMP, :cb
                    ) RETURNING id
                """),
                {
                    "bid": build_id, "cid": config_id,
                    "rd": payload.run_date, "st": save_scenario_type, "sct": save_scan_type,
                    "dst": dst_id, "fst": fst_id,
                    "ss": _parse_ts(payload.scan_start_time), "se": _parse_ts(payload.scan_end_time),
                    "tsh": payload.total_scan_hours,
                    "tcg": payload.throughput_cluster_gb_hr, "tsg2": payload.throughput_scanner_gb_hr,
                    "ttg": payload.throughput_thread_gb_hr,
                    "tcf": payload.throughput_cluster_files_hr, "tsf": payload.throughput_scanner_files_hr,
                    "ttf": payload.throughput_thread_files_hr,
                    "tci": payload.total_catalog_items,
                    "sid": payload.scan_id or None,
                    "isr": payload.is_release,
                    "cb": u["id"]
                },
            ).fetchone()
            new_id = int(row[0])
        return {"ok": True, "id": new_id, "build_version": payload.build_version}
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail=f"Duplicate or constraint violation: {e}")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Build Analysis ────────────────────────────────────────────────────────────

@app.get("/api/build-analysis/builds")
async def get_build_analysis_builds(token: str = Depends(oauth2_scheme)):
    """List all build versions that have benchmark data, sorted newest first."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT bv.version, COUNT(b.id) AS cnt
                FROM benchmarks b
                JOIN builds bv ON bv.id = b.build_id
                WHERE COALESCE(b.is_active, TRUE) = TRUE
                GROUP BY bv.version
                ORDER BY
                  COALESCE(NULLIF(SUBSTRING(bv.version FROM '^([0-9]+)'), '')::INTEGER, 0) DESC,
                  COALESCE(NULLIF(SUBSTRING(bv.version FROM '^[0-9]+[.\\-]([0-9]+)'), '')::INTEGER, 0) DESC,
                  bv.id DESC
            """)).fetchall()
        return [{"version": r[0], "count": int(r[1])} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/build-analysis/data")
async def get_build_analysis_data(
    token: str = Depends(oauth2_scheme),
    major: Optional[str] = Query(None, description="Filter to this major version prefix (e.g. '252')"),
):
    """
    Return all benchmark rows for build analysis — scenario matrix, vs-prev-major comparison.
    Optionally filter to a major version prefix.
    """
    try:
        where = "WHERE COALESCE(b.is_active, TRUE) = TRUE"
        params: dict = {}
        if major:
            where += " AND bv.version LIKE :major_prefix"
            params["major_prefix"] = f"{major.strip()}%"

        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    b.id,
                    bv.version AS build_version,
                    COALESCE(bv.release_milestone, 0) AS is_release,
                    dst.name AS data_source_type,
                    fst.name AS file_set_type,
                    COALESCE(b.scan_type, '') AS scan_type,
                    COALESCE(cfg.is_acl, FALSE) AS is_acl,
                    COALESCE(cfg.is_feature_flags, FALSE) AS is_feature_flags,
                    COALESCE(cfg.feature_flags, '') AS feature_flags,
                    b.throughput_cluster_gb_hr::double precision,
                    b.throughput_cluster_files_hr::double precision,
                    b.throughput_scanner_gb_hr::double precision,
                    b.throughput_thread_gb_hr::double precision,
                    b.total_scan_hours::double precision,
                    COALESCE(si.objects_scanned, b.total_catalog_items, 0)::bigint AS objects_scanned,
                    b.run_date,
                    b.scan_id,
                    COALESCE(sp.scan_name, '') AS scan_name
                FROM benchmarks b
                JOIN builds bv ON bv.id = b.build_id
                LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
                LEFT JOIN configurations cfg ON cfg.id = b.config_id
                LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
                LEFT JOIN scan_parents sp ON sp.scan_id = b.scan_id
                {where}
                ORDER BY bv.id DESC, b.id DESC
            """), params).fetchall()

        out = []
        for r in rows:
            raw = {
                "id": r[0],
                "build_version": str(r[1] or ""),
                "is_release": bool(r[2]),
                "data_source_type": r[3],
                "file_set_type": r[4],
                "scan_type": r[5],
                "is_acl": bool(r[6]),
                "is_feature_flags": bool(r[7]),
                "feature_flags": r[8],
                "throughput_cluster_gb_hr": r[9],
                "throughput_cluster_files_hr": r[10],
                "throughput_scanner_gb_hr": r[11],
                "throughput_thread_gb_hr": r[12],
                "total_scan_hours": r[13],
                "objects_scanned": r[14],
                "run_date": str(r[15]) if r[15] else None,
                "scan_id": r[16],
                "scan_name": r[17],
            }
            canon = canonical_benchmark_fields(raw)
            raw.update({
                "data_source_type": canon["data_source_type"],
                "file_set_type": canon["matrix_file_set_type"],
                "scan_type": canon["scan_type"],
                "is_acl": bool(canon["is_acl"]),
                "is_feature_flags": bool(canon["is_feature_flags"]),
                "scenario_label": canon["scenario_label"],
            })
            out.append(raw)
        return {"rows": out, "total": len(out)}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Benchmarks: list with filters, update, delete ────────────────────────────

@app.get("/api/benchmarks/list")
async def list_benchmarks(
    u=Depends(get_current_user),
    search: Optional[str] = Query(None),
    mode: Literal["manual", "auto"] = Query("manual"),
    include_inactive: bool = Query(False),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """Paginated benchmark list for Edit Records page (supports Manual and Auto modes)."""
    try:
        if mode == "auto":
            where_clauses = []
            params: dict = {"limit": limit, "offset": offset}
            
            if not include_inactive:
                where_clauses.append("COALESCE(ab.is_active, TRUE) = TRUE")
            
            # Exclude noise like in Streamlit load_auto_trend_dataset
            where_clauses.append("LOWER(COALESCE(sp.scan_status, '')) NOT IN ('stopped', 'aborted')")
            where_clauses.append("LOWER(COALESCE(sp.scan_name, '')) NOT LIKE 'instant scan data%'")
            where_clauses.append("LOWER(COALESCE(ab.scan_name, '')) NOT LIKE 'instant scan data%'")
            
            if search and search.strip():
                where_clauses.append("(ab.scan_id ILIKE :s OR ab.build_version ILIKE :s OR ab.scan_type ILIKE :s OR ab.scan_name ILIKE :s)")
                params["s"] = f"%{search.strip()}%"
            
            # RBAC: Filter by user unless Admin
            if u["role"] != "Admin":
                where_clauses.append("ab.created_by = :uid")
                params["uid"] = u["id"]
            
            where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
            
            with engine.connect() as conn:
                total = conn.execute(text(f"""
                    SELECT COUNT(*) 
                    FROM auto_scan_benchmarks ab
                    LEFT JOIN scan_parents sp ON sp.scan_id = ab.scan_id
                    {where}
                """), params).scalar() or 0
                
                rows = conn.execute(text(f"""
                    SELECT ab.scan_id AS id,
                           COALESCE(bu_fk.version, ab.build_version) AS build_version,
                           ab.run_date, ab.scenario_type, ab.scan_type,
                           COALESCE(dst.name, ab.data_source_type) AS data_source_type,
                           COALESCE(fst.name, ab.file_set_type) AS file_set_type,
                           ab.total_scan_hours, 
                           NULL::float AS throughput_cluster_gb_hr, NULL::float AS throughput_scanner_gb_hr,
                           NULL::float AS throughput_thread_gb_hr, NULL::float AS throughput_cluster_files_hr,
                           NULL::float AS throughput_scanner_files_hr, NULL::float AS throughput_thread_files_hr,
                           5 AS scanner_count, 50 AS thread_count, FALSE AS is_acl, FALSE AS is_ocr,
                           FALSE AS is_feature_flags, NULL AS feature_flags,
                           ab.objects_scanned AS total_catalog_items, ab.is_active, 
                           CASE WHEN TRIM(COALESCE(bu_fk.release_milestone, bu.release_milestone, '')) != '' THEN TRUE ELSE FALSE END as is_release,
                           ab.scan_id, NULL AS config_id, COALESCE(ab.build_id, bu.id) AS build_id,
                           COALESCE(fst.total_files, ab.total_files) AS total_files,
                           COALESCE(fst.total_folders, ab.total_folders) AS total_folders,
                           COALESCE(fst.total_size_gb, ab.total_size_gb) AS total_size_gb,
                           COALESCE(fst.data_type, ab.data_type) AS data_type
                    FROM auto_scan_benchmarks ab
                    LEFT JOIN builds bu_fk ON bu_fk.id = ab.build_id
                    LEFT JOIN data_source_types dst ON dst.id = ab.data_source_type_id
                    LEFT JOIN file_set_types fst ON fst.id = ab.file_set_type_id
                    LEFT JOIN scan_parents sp ON sp.scan_id = ab.scan_id
                    LEFT JOIN test_environments te ON te.id = ab.environment_id
                    LEFT JOIN builds bu ON bu.version = ab.build_version
                    {where}
                    ORDER BY ab.run_date DESC, ab.updated_at DESC
                    LIMIT :limit OFFSET :offset
                """), params).fetchall()
            
            cols = ["id","build_version","run_date","scenario_type","scan_type","data_source_type",
                    "file_set_type","total_scan_hours","throughput_cluster_gb_hr","throughput_scanner_gb_hr",
                    "throughput_thread_gb_hr","throughput_cluster_files_hr","throughput_scanner_files_hr",
                    "throughput_thread_files_hr","scanner_count","thread_count","is_acl","is_ocr",
                    "is_feature_flags","feature_flags","total_catalog_items","is_active",
                    "is_release","scan_id","config_id","build_id",
                    "total_files", "total_folders", "total_size_gb", "data_type"]
            
            items = []
            for r in rows:
                d = dict(zip(cols, r))
                d["is_active"] = bool(d["is_active"]) if d["is_active"] is not None else True
                items.append(d)
                
            return {"items": items, "total": int(total)}
            
        else: # Manual mode
            where_clauses = []
            params: dict = {"limit": limit, "offset": offset}
            
            if not include_inactive:
                where_clauses.append("COALESCE(b.is_active, TRUE) = TRUE")
            
            if search and search.strip():
                where_clauses.append("(bv.version ILIKE :s OR b.scan_type ILIKE :s OR dst.name ILIKE :s OR b.id::text = :sid)")
                params["s"] = f"%{search.strip()}%"
                params["sid"] = search.strip()
            
            # RBAC: Filter by user unless Admin
            if u["role"] != "Admin":
                where_clauses.append("b.created_by = :uid")
                params["uid"] = u["id"]

            where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
            
            with engine.connect() as conn:
                total = conn.execute(text(f"""
                    SELECT COUNT(*) FROM benchmarks b
                    JOIN builds bv ON bv.id = b.build_id
                    LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                    {where}
                """), params).scalar() or 0
                
                rows = conn.execute(text(f"""
                    SELECT b.id, bv.version AS build_version, b.run_date, b.scenario_type, b.scan_type,
                           dst.name AS data_source_type, fst.name AS file_set_type,
                           b.total_scan_hours, b.throughput_cluster_gb_hr, b.throughput_scanner_gb_hr,
                           b.throughput_thread_gb_hr, b.throughput_cluster_files_hr,
                           b.throughput_scanner_files_hr, b.throughput_thread_files_hr,
                           cfg.scanner_count, cfg.thread_count, cfg.is_acl, cfg.is_ocr,
                           cfg.is_feature_flags, cfg.feature_flags,
                           b.total_catalog_items, b.is_active, b.is_release, b.scan_id,
                           b.config_id, b.build_id,
                           fst.total_files, fst.total_folders, fst.total_size_gb, fst.data_type
                    FROM benchmarks b
                    JOIN builds bv ON bv.id = b.build_id
                    LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                    LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
                    LEFT JOIN configurations cfg ON cfg.id = b.config_id
                    {where}
                    ORDER BY b.id DESC
                    LIMIT :limit OFFSET :offset
                """), params).fetchall()
            
            cols = ["id","build_version","run_date","scenario_type","scan_type","data_source_type",
                    "file_set_type","total_scan_hours","throughput_cluster_gb_hr","throughput_scanner_gb_hr",
                    "throughput_thread_gb_hr","throughput_cluster_files_hr","throughput_scanner_files_hr",
                    "throughput_thread_files_hr","scanner_count","thread_count","is_acl","is_ocr",
                    "is_feature_flags","feature_flags","total_catalog_items","is_active",
                    "is_release","scan_id","config_id","build_id",
                    "total_files", "total_folders", "total_size_gb", "data_type"]
            
            items = []
            for r in rows:
                d = dict(zip(cols, r))
                d["is_active"] = bool(d["is_active"]) if d["is_active"] is not None else True
                d["is_release"] = bool(d["is_release"]) if d["is_release"] is not None else False
                items.append(d)
                
            return {"items": items, "total": int(total)}
            
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BenchmarkUpdate(BaseModel):
    run_date: Optional[str] = None
    scenario_type: Optional[str] = None
    scan_type: Optional[str] = None
    data_source_type: Optional[str] = None
    data_type: Optional[str] = None
    file_set_type: Optional[str] = None
    total_files: Optional[int] = None
    total_folders: Optional[int] = None
    total_size_gb: Optional[float] = None
    total_scan_hours: Optional[float] = None
    throughput_cluster_gb_hr: Optional[float] = None
    throughput_scanner_gb_hr: Optional[float] = None
    throughput_thread_gb_hr: Optional[float] = None
    throughput_cluster_files_hr: Optional[float] = None
    throughput_scanner_files_hr: Optional[float] = None
    throughput_thread_files_hr: Optional[float] = None
    total_catalog_items: Optional[int] = None
    avg_file_size: Optional[float] = None
    is_active: Optional[bool] = None
    is_release: Optional[bool] = None


@app.put("/api/benchmarks/{benchmark_id}")
async def update_benchmark(
    benchmark_id: str, 
    payload: BenchmarkUpdate, 
    mode: Literal["manual", "auto"] = Query("manual"),
    token: str = Depends(oauth2_scheme)
):
    """Update a benchmark record. Handles Manual and Auto tables."""
    data = payload.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        with engine.begin() as conn:
            if mode == "auto":
                sets = []
                params = {"sid": benchmark_id}
                
                # Map payload fields to auto_scan_benchmarks columns
                field_map = {
                    "scenario_type": "scenario_type",
                    "scan_type": "scan_type",
                    "data_source_type": "data_source_type",
                    "data_type": "data_type",
                    "file_set_type": "file_set_type",
                    "total_files": "total_files",
                    "total_folders": "total_folders",
                    "total_size_gb": "total_size_gb",
                    "total_catalog_items": "total_classification",
                    "is_active": "is_active"
                }
                
                for field, col in field_map.items():
                    if field in data:
                        sets.append(f"{col} = :{field}")
                        val = data[field]
                        if field == "is_active": val = 1 if val else 0
                        params[field] = val

                # Resolve FK columns when text dimensions are updated
                if "data_source_type" in data and data["data_source_type"]:
                    dst_row = conn.execute(text("SELECT id FROM data_source_types WHERE name = :n"), {"n": data["data_source_type"]}).fetchone()
                    if dst_row:
                        sets.append("data_source_type_id = :dst_id")
                        params["dst_id"] = int(dst_row[0])
                if "file_set_type" in data and data["file_set_type"]:
                    fst_row = conn.execute(text("SELECT id FROM file_set_types WHERE LOWER(TRIM(name)) = LOWER(TRIM(:n))"), {"n": data["file_set_type"]}).fetchone()
                    if fst_row:
                        sets.append("file_set_type_id = :fst_id")
                        params["fst_id"] = int(fst_row[0])

                if not sets:
                    return {"ok": True, "message": "No auto-scan fields changed"}
                
                sets.append("updated_at = CURRENT_TIMESTAMP")
                r = conn.execute(text(f"UPDATE auto_scan_benchmarks SET {', '.join(sets)} WHERE scan_id = :sid"), params)
                if r.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Auto-scan benchmark not found")
            else:
                sets = []
                params = {"bid": int(benchmark_id)}
                # Fields that map 1:1 to benchmarks table
                manual_fields = [
                    "run_date", "scenario_type", "scan_type", "total_scan_hours",
                    "throughput_cluster_gb_hr", "throughput_scanner_gb_hr", "throughput_thread_gb_hr",
                    "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr",
                    "total_catalog_items", "avg_file_size", "is_active", "is_release",
                ]
                for field in manual_fields:
                    if field in data:
                        sets.append(f"{field} = :{field}")
                        params[field] = data[field]

                # Volume/data_type fields live on file_set_types (normalized); redirect updates there
                fst_fields = {k: data[k] for k in ("total_files", "total_folders", "total_size_gb", "data_type") if k in data}
                if fst_fields:
                    fst_sets = [f"{k} = :{k}" for k in fst_fields]
                    fst_params = {**fst_fields, "bid": int(benchmark_id)}
                    conn.execute(text(f"""
                        UPDATE file_set_types fst
                        SET {', '.join(fst_sets)}
                        FROM benchmarks b
                        WHERE b.id = :bid AND b.file_set_type_id = fst.id
                    """), fst_params)

                if not sets and not fst_fields:
                    return {"ok": True, "message": "No manual benchmark fields changed"}

                if sets:
                    sets.append("updated_at = CURRENT_TIMESTAMP")
                    r = conn.execute(text(f"UPDATE benchmarks SET {', '.join(sets)} WHERE id = :bid"), params)
                    if r.rowcount == 0:
                        raise HTTPException(status_code=404, detail="Benchmark not found")
            
        return {"ok": True, "id": benchmark_id}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid benchmark ID format")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/benchmarks/{benchmark_id}")
async def delete_benchmark(
    benchmark_id: str, 
    mode: Literal["manual", "auto"] = Query("manual"),
    token: str = Depends(oauth2_scheme)
):
    """Delete a benchmark record from Manual or Auto tables."""
    try:
        with engine.begin() as conn:
            if mode == "auto":
                r = conn.execute(text("DELETE FROM auto_scan_benchmarks WHERE scan_id = :id"), {"id": benchmark_id})
            else:
                bid = int(benchmark_id)
                conn.execute(text("DELETE FROM benchmark_findings WHERE benchmark_id = :id"), {"id": bid})
                r = conn.execute(text("DELETE FROM benchmarks WHERE id = :id"), {"id": bid})
            
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="Record not found")
        return {"ok": True}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid benchmark ID format")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BulkDeletePayload(BaseModel):
    ids: List[str]

@app.post("/api/benchmarks/bulk-delete")
async def bulk_delete_benchmarks(
    payload: BulkDeletePayload, 
    mode: Literal["manual", "auto"] = Query("manual"),
    token: str = Depends(oauth2_scheme)
):
    if not payload.ids:
        return {"ok": True, "deleted": 0}
    try:
        with engine.begin() as conn:
            if mode == "auto":
                r = conn.execute(
                    text("DELETE FROM auto_scan_benchmarks WHERE scan_id = ANY(:ids)"),
                    {"ids": payload.ids},
                )
            else:
                int_ids = [int(i) for i in payload.ids]
                conn.execute(
                    text("DELETE FROM benchmark_findings WHERE benchmark_id = ANY(:ids)"),
                    {"ids": int_ids},
                )
                r = conn.execute(
                    text("DELETE FROM benchmarks WHERE id = ANY(:ids)"),
                    {"ids": int_ids},
                )
        return {"ok": True, "deleted": r.rowcount}
    except (ValueError, SQLAlchemyError) as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Builds: list, update release tags ────────────────────────────────────────

@app.get("/api/lookup/data-source-types")
async def lookup_data_source_types(token: str = Depends(oauth2_scheme)):
    """All distinct data source types for dropdowns."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, name FROM data_source_types ORDER BY name")).fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lookup/file-set-types")
async def lookup_file_set_types(token: str = Depends(oauth2_scheme)):
    """All distinct file set types with their volume metadata for dropdowns."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, name, data_type, total_files, total_folders, total_size_gb
                FROM file_set_types ORDER BY name
            """)).fetchall()
        return [{"id": r[0], "name": r[1], "data_type": r[2],
                 "total_files": r[3], "total_folders": r[4], "total_size_gb": r[5]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/builds")
async def list_builds(token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, version, release_milestone, quarter_label
                FROM builds
                ORDER BY
                  COALESCE(NULLIF(SUBSTRING(version FROM '^([0-9]+)'), '')::INTEGER, 0) DESC,
                  COALESCE(NULLIF(SUBSTRING(version FROM '^[0-9]+[.\\-]([0-9]+)'), '')::INTEGER, 0) DESC,
                  id DESC
            """)).fetchall()
        return [{"id": r[0], "version": r[1], "release_milestone": r[2], "quarter_label": r[3]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BuildTagUpdate(BaseModel):
    release_milestone: Optional[str] = None
    quarter_label: Optional[str] = None


@app.put("/api/builds/{build_id}/tag")
async def update_build_tag(build_id: int, payload: BuildTagUpdate, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text("UPDATE builds SET release_milestone = :m, quarter_label = :q WHERE id = :id RETURNING id"),
                {"m": payload.release_milestone, "q": payload.quarter_label, "id": build_id},
            )
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="Build not found")
        return {"ok": True}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Classifier comparison ─────────────────────────────────────────────────────

@app.get("/api/classifiers/build-mappings")
async def get_classifier_build_mappings(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None),
):
    """Return {build_version: [classifier_name, ...]} for classifier comparison page."""
    try:
        with engine.connect() as conn:
            if environment_id is not None:
                rows = conn.execute(text("""
                    SELECT bv.version, c.name
                    FROM build_classifier_mappings bcm
                    JOIN builds bv ON bv.id = bcm.build_id
                    JOIN classifiers c ON c.id = bcm.classifier_id
                    WHERE bcm.environment_id = :eid
                    ORDER BY bv.version, c.name
                """), {"eid": environment_id}).fetchall()
            else:
                rows = conn.execute(text("""
                    SELECT bv.version, c.name
                    FROM build_classifier_mappings bcm
                    JOIN builds bv ON bv.id = bcm.build_id
                    JOIN classifiers c ON c.id = bcm.classifier_id
                    ORDER BY bv.version, c.name
                """)).fetchall()
        build_map: dict = {}
        for ver, name in rows:
            v, n = str(ver or "").strip(), str(name or "").strip()
            if v and n:
                build_map.setdefault(v, []).append(n)
        return {"build_map": build_map, "builds": sorted(build_map.keys())}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/classifiers/build-stats")
async def get_classifier_build_stats(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None),
):
    """Classifier count per build per environment for Version Comparison trend chart."""
    try:
        with engine.connect() as conn:
            if environment_id is not None:
                rows = conn.execute(text("""
                    SELECT bv.version, te.env_name, COUNT(DISTINCT bcm.classifier_id) AS cnt,
                           MAX(bcm.last_synced_at) AS last_synced_at
                    FROM build_classifier_mappings bcm
                    JOIN builds bv ON bv.id = bcm.build_id
                    LEFT JOIN test_environments te ON te.id = bcm.environment_id
                    WHERE bcm.environment_id = :eid
                    GROUP BY bv.version, te.env_name
                    ORDER BY bv.version, te.env_name
                """), {"eid": environment_id}).fetchall()
            else:
                rows = conn.execute(text("""
                    SELECT bv.version, COALESCE(te.env_name, 'All') AS env_name,
                           COUNT(DISTINCT bcm.classifier_id) AS cnt,
                           MAX(bcm.last_synced_at) AS last_synced_at
                    FROM build_classifier_mappings bcm
                    JOIN builds bv ON bv.id = bcm.build_id
                    LEFT JOIN test_environments te ON te.id = bcm.environment_id
                    GROUP BY bv.version, te.env_name
                    ORDER BY bv.version, te.env_name
                """)).fetchall()
        return [{"build_version": r[0], "env_name": r[1], "classifier_count": int(r[2]),
                 "last_synced_at": str(r[3]) if r[3] else None} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/classifiers/snapshot-summary")
async def get_classifier_snapshot_summary(
    token: str = Depends(oauth2_scheme),
    environment_id: Optional[int] = Query(None)
):
    """List build versions that have metadata snapshots for the selected environment."""
    try:
        where = ""
        params = {}
        if environment_id is not None:
            where = "WHERE cds.environment_id = :eid"
            params["eid"] = environment_id
            
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT b.version, COUNT(*) as snapshot_count, MAX(cds.created_at) as last_captured
                FROM classifier_details_snapshots cds
                JOIN builds b ON b.id = cds.build_id
                {where}
                GROUP BY b.version
                ORDER BY b.version DESC
            """), params).fetchall()
            
        return [
            {
                "build_version": r[0],
                "snapshot_count": r[1],
                "last_captured": str(r[2]) if r[2] else None
            }
            for r in rows
        ]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/classifiers/snapshots")
async def get_classifier_snapshots(
    build_version: str,
    environment_id: Optional[int] = Query(None),
    token: str = Depends(oauth2_scheme)
):
    """Return all classifier snapshots for a build version + environment."""
    try:
        with engine.connect() as conn:
            # Resolve build_id
            b_row = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": build_version}).fetchone()
            if not b_row:
                return {"snapshots": []}
            bid = int(b_row[0])
            
            where = "WHERE build_id = :bid"
            params = {"bid": bid}
            if environment_id is not None:
                where += " AND environment_id = :eid"
                params["eid"] = environment_id
                
            rows = conn.execute(text(f"""
                SELECT classifier_name, bigid_classifier_id, snapshot_json
                FROM classifier_details_snapshots
                {where}
            """), params).fetchall()
            
        import json
        out = []
        for r in rows:
            try:
                snap = json.loads(r[2]) if isinstance(r[2], str) else r[2]
            except Exception:
                snap = {}
            out.append({
                "classifier_name": r[0],
                "bigid_classifier_id": r[1],
                "snapshot": snap
            })
        return {"snapshots": out}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/classifiers/refresh-snapshots")
async def refresh_classifier_snapshots(
    environment_id: int,
    token: str = Depends(oauth2_scheme)
):
    """Trigger manual refresh of classifier snapshots for an environment."""
    try:
        from v2_api.tracker import sync_classifier_details_for_env_build, fetch_about, _extract_build_version_from_about_payload
        from bigid_auth import Get_auth_token
        
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, env_name, base_url, auth_module_path, auth_function_name, version, bigid_username, bigid_password_enc FROM test_environments WHERE id = :eid"
            ), {"eid": environment_id}).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Environment not found")
            env_row = dict(row._mapping)
            
        base_url = str(env_row.get("base_url") or "").strip()
        if not base_url:
             raise HTTPException(status_code=400, detail="Environment base_url is missing")
             
        env_token = Get_auth_token(base_url, env_row)
        if not env_token:
            raise HTTPException(status_code=401, detail="Failed to get BigID auth token for environment")
            
        about = fetch_about(base_url, env_token)
        if not about:
            raise HTTPException(status_code=502, detail="Failed to fetch /about from BigID")

        build_version = _extract_build_version_from_about_payload(about)
        if not build_version:
            # Try releaseVersion as-is (e.g. "staging", "master") for non-numeric envs
            try:
                build_version = (
                    about.get("data", {}).get("about", {}).get("releaseVersion")
                    or about.get("data", {}).get("releaseVersion")
                    or about.get("releaseVersion")
                    or ""
                )
            except Exception:
                build_version = ""
        if not build_version:
            build_version = env_row.get("version") or ""
        if not build_version:
            raise HTTPException(status_code=400, detail="Could not determine build version for snapshot capture")
             
        ok = sync_classifier_details_for_env_build(engine, env_row, env_token, build_version, force=True)
        return {"ok": ok, "build_version": build_version}
    except HTTPException:
        raise
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))


_CLASSIFIER_DETAIL_KEYS = [
    "classification_name", "category", "classifierType", "dataType",
    "detection_sensitivity", "enabled", "min_length", "max_length",
    "proximity_after", "proximity_before", "nst_proximity_after", "nst_proximity_before",
]


def _compute_classifier_snapshot(conn, scan_id: str) -> dict:
    """
    Compute classifier diff between the build active at scan start and the previous build
    for the same environment.  Returns a dict ready to return from the API or write to DB.
    """
    parent_row = conn.execute(text(
        "SELECT environment_id, started_at FROM scan_parents WHERE scan_id = :sid LIMIT 1"
    ), {"sid": scan_id}).fetchone()
    if not parent_row or not parent_row[0]:
        return {"error": "no_environment", "message": "Scan has no environment attached."}

    env_id = parent_row[0]
    scan_started_at_raw = parent_row[1]

    # Normalize scan_started_at to a naive datetime for comparison with DB datetimes
    scan_started_at = None
    if scan_started_at_raw:
        try:
            from datetime import datetime, timezone
            if isinstance(scan_started_at_raw, datetime):
                scan_started_at = scan_started_at_raw.replace(tzinfo=None) if scan_started_at_raw.tzinfo else scan_started_at_raw
            else:
                s = str(scan_started_at_raw).strip().rstrip("Z").replace("T", " ")
                scan_started_at = datetime.fromisoformat(s)
        except Exception:
            scan_started_at = None

    # Builds synced for this env, most recently synced first
    build_rows = conn.execute(text("""
        SELECT bcm.build_id, bv.version, MAX(bcm.last_synced_at) AS last_sync
        FROM build_classifier_mappings bcm
        JOIN builds bv ON bv.id = bcm.build_id
        WHERE bcm.environment_id = :eid
        GROUP BY bcm.build_id, bv.version
        ORDER BY last_sync DESC
    """), {"eid": env_id}).fetchall()

    if not build_rows:
        return {"error": "no_classifier_data", "message": "No classifier mappings found for this environment."}

    # Identify current build: most recent sync at or before scan start; else the latest overall
    current_build_id, current_build_version = build_rows[0][0], str(build_rows[0][1] or "")
    if scan_started_at:
        for br in build_rows:
            br_sync = br[2]
            if br_sync is None:
                continue
            # Normalize br_sync to naive datetime
            try:
                if hasattr(br_sync, "tzinfo") and br_sync.tzinfo:
                    br_sync = br_sync.replace(tzinfo=None)
            except Exception:
                pass
            try:
                if br_sync <= scan_started_at:
                    current_build_id = br[0]
                    current_build_version = str(br[1] or "")
                    break
            except TypeError:
                pass

    # Previous build: the next entry in the list after current
    previous_build_id, previous_build_version = None, None
    found = False
    for br in build_rows:
        if found:
            previous_build_id = br[0]
            previous_build_version = str(br[1] or "")
            break
        if br[0] == current_build_id:
            found = True

    def _get_names(bid):
        return set(r[0] for r in conn.execute(text("""
            SELECT c.name FROM build_classifier_mappings bcm
            JOIN classifiers c ON c.id = bcm.classifier_id
            WHERE bcm.environment_id = :eid AND bcm.build_id = :bid
        """), {"eid": env_id, "bid": bid}).fetchall())

    curr_names = _get_names(current_build_id)
    total_count = len(curr_names)

    if not previous_build_id:
        return {
            "current_build_version": current_build_version,
            "previous_build_version": None,
            "total_count": total_count,
            "previous_total_count": 0,
            "added_count": 0, "removed_count": 0, "changed_count": 0,
            "added_names": [], "removed_names": [], "changed_names": [],
            "_env_id": env_id, "_cur_bid": current_build_id, "_prev_bid": None,
            "message": "No previous build to compare.",
        }

    prev_names = _get_names(previous_build_id)
    previous_total_count = len(prev_names)
    added_names = sorted(curr_names - prev_names)
    removed_names = sorted(prev_names - curr_names)

    # Metadata diff for classifiers present in both builds (best-effort — needs snapshots)
    changed_names: List[str] = []
    common = curr_names & prev_names
    if common:
        def _get_snaps(bid):
            rows = conn.execute(text("""
                SELECT c.name, cds.snapshot_json
                FROM classifier_details_snapshots cds
                JOIN classifiers c ON c.id = cds.classifier_id
                WHERE cds.environment_id = :eid AND cds.build_id = :bid
            """), {"eid": env_id, "bid": bid}).fetchall()
            out = {}
            for name, snap_raw in rows:
                try:
                    out[name] = json.loads(snap_raw) if isinstance(snap_raw, str) else (snap_raw or {})
                except Exception:
                    out[name] = {}
            return out

        curr_snaps = _get_snaps(current_build_id)
        prev_snaps = _get_snaps(previous_build_id)
        for name in common:
            c_snap = curr_snaps.get(name)
            p_snap = prev_snaps.get(name)
            if not c_snap or not p_snap:
                continue
            for k in _CLASSIFIER_DETAIL_KEYS:
                if str(c_snap.get(k, "")) != str(p_snap.get(k, "")):
                    changed_names.append(name)
                    break
        changed_names.sort()

    return {
        "current_build_version": current_build_version,
        "previous_build_version": previous_build_version,
        "total_count": total_count,
        "previous_total_count": previous_total_count,
        "added_count": len(added_names),
        "removed_count": len(removed_names),
        "changed_count": len(changed_names),
        "added_names": added_names,
        "removed_names": removed_names,
        "changed_names": changed_names,
        "_env_id": env_id,
        "_cur_bid": current_build_id,
        "_prev_bid": previous_build_id,
    }


def _write_classifier_snapshot_cache(conn, scan_id: str, data: dict) -> None:
    env_id = data.get("_env_id")
    cur_bid = data.get("_cur_bid")
    prev_bid = data.get("_prev_bid")
    conn.execute(text("""
        INSERT INTO scan_classifier_snapshot
            (scan_id, environment_id, current_build_id, previous_build_id,
             current_build_version, previous_build_version,
             total_count, previous_total_count,
             added_count, removed_count, changed_count,
             added_names, removed_names, changed_names)
        VALUES
            (:sid, :eid, :cbid, :pbid, :cbv, :pbv,
             :tot, :ptot, :a, :r, :c,
             CAST(:an AS JSONB), CAST(:rn AS JSONB), CAST(:cn AS JSONB))
        ON CONFLICT (scan_id) DO NOTHING
    """), {
        "sid": scan_id,
        "eid": env_id,
        "cbid": cur_bid,
        "pbid": prev_bid,
        "cbv": data.get("current_build_version"),
        "pbv": data.get("previous_build_version"),
        "tot": data.get("total_count", 0),
        "ptot": data.get("previous_total_count", 0),
        "a": data.get("added_count", 0),
        "r": data.get("removed_count", 0),
        "c": data.get("changed_count", 0),
        "an": json.dumps(data.get("added_names", [])),
        "rn": json.dumps(data.get("removed_names", [])),
        "cn": json.dumps(data.get("changed_names", [])),
    })
    conn.commit()


@app.get("/api/scan/{scan_id}/classifier-snapshot")
async def get_scan_classifier_snapshot(
    scan_id: str,
    token: str = Depends(oauth2_scheme),
    refresh: bool = Query(False),
):
    """
    Return classifier state comparison at scan-start time:
    total count, classifiers added/removed since previous build, and
    classifiers whose metadata or regex changed between builds.
    Result is computed once and cached in scan_classifier_snapshot.
    Pass ?refresh=true to force recompute.
    """
    try:
        with engine.connect() as conn:
            # Create the cache table if it was never initialised (safe on repeated calls)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_classifier_snapshot (
                    id SERIAL PRIMARY KEY,
                    scan_id VARCHAR(120) NOT NULL UNIQUE,
                    environment_id INT REFERENCES test_environments(id) ON DELETE SET NULL,
                    current_build_id INT REFERENCES builds(id) ON DELETE SET NULL,
                    previous_build_id INT REFERENCES builds(id) ON DELETE SET NULL,
                    current_build_version TEXT,
                    previous_build_version TEXT,
                    total_count INT NOT NULL DEFAULT 0,
                    previous_total_count INT NOT NULL DEFAULT 0,
                    added_count INT NOT NULL DEFAULT 0,
                    removed_count INT NOT NULL DEFAULT 0,
                    changed_count INT NOT NULL DEFAULT 0,
                    added_names JSONB DEFAULT '[]',
                    removed_names JSONB DEFAULT '[]',
                    changed_names JSONB DEFAULT '[]',
                    captured_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            conn.commit()

            if not refresh:
                cached = conn.execute(text("""
                    SELECT current_build_version, previous_build_version,
                           total_count, previous_total_count,
                           added_count, removed_count, changed_count,
                           added_names, removed_names, changed_names,
                           captured_at
                    FROM scan_classifier_snapshot
                    WHERE scan_id = :sid
                """), {"sid": scan_id}).fetchone()
                if cached:
                    def _jlist(v):
                        if v is None:
                            return []
                        if isinstance(v, list):
                            return v
                        try:
                            return json.loads(v)
                        except Exception:
                            return []
                    return {
                        "scan_id": scan_id,
                        "current_build_version": cached[0],
                        "previous_build_version": cached[1],
                        "total_count": cached[2],
                        "previous_total_count": cached[3],
                        "added_count": cached[4],
                        "removed_count": cached[5],
                        "changed_count": cached[6],
                        "added_names": _jlist(cached[7]),
                        "removed_names": _jlist(cached[8]),
                        "changed_names": _jlist(cached[9]),
                        "captured_at": str(cached[10]) if cached[10] else None,
                        "cached": True,
                    }

            data = _compute_classifier_snapshot(conn, scan_id)
            if "error" in data:
                return {"scan_id": scan_id, **data}

            try:
                _write_classifier_snapshot_cache(conn, scan_id, data)
            except Exception:
                pass

            clean = {k: v for k, v in data.items() if not k.startswith("_")}
            return {"scan_id": scan_id, **clean, "cached": False}

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Scanner Infrastructure ────────────────────────────────────────────────────

@app.get("/api/scanner-performance/individual")
async def get_scanner_individual_perf(
    token: str = Depends(oauth2_scheme),
    environment_id: int = Query(...),
    days_back: int = Query(7, ge=1, le=30),
):
    try:
        from datetime import datetime as _dt, timedelta as _td
        end_dt = _dt.utcnow()
        start_dt = end_dt - _td(days=days_back)
        has_cpu = pg_column_exists(engine, "scanner_individual_performance", "cpu_pct")
        has_mem = pg_column_exists(engine, "scanner_individual_performance", "memory_usage_pct")
        has_disp = pg_column_exists(engine, "scanner_individual_performance", "display_name")
        cpu_col = "cpu_pct" if has_cpu else "system_load_average AS cpu_pct"
        mem_col = "memory_usage_pct" if has_mem else "NULL::float AS memory_usage_pct"
        name_col = "display_name" if has_disp else "name AS display_name"
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT captured_at_utc, scanner_id, {name_col}, status,
                       {cpu_col}, {mem_col}, running_threads
                FROM scanner_individual_performance
                WHERE environment_id = :eid
                  AND captured_at_utc BETWEEN :start AND :end
                ORDER BY captured_at_utc ASC
            """), {"eid": environment_id, "start": start_dt, "end": end_dt}).fetchall()
        return [{"captured_at_utc": str(r[0]), "scanner_id": r[1], "display_name": r[2],
                 "status": r[3], "cpu_pct": r[4], "memory_usage_pct": r[5],
                 "running_threads": r[6]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/scanner-performance/group")
async def get_scanner_group_perf(
    token: str = Depends(oauth2_scheme),
    environment_id: int = Query(...),
    days_back: int = Query(7, ge=1, le=30),
):
    try:
        from datetime import datetime as _dt, timedelta as _td
        end_dt = _dt.utcnow()
        start_dt = end_dt - _td(days=days_back)
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT captured_at_utc, group_name, running_scanners, running_threads,
                       avg_scan_speed, physical_memory_mb, free_memory_mb,
                       COALESCE(group_scanner_ids_json, '[]') AS scanner_ids_json
                FROM scanner_group_performance
                WHERE environment_id = :eid
                  AND captured_at_utc BETWEEN :start AND :end
                ORDER BY captured_at_utc ASC
            """), {"eid": environment_id, "start": start_dt, "end": end_dt}).fetchall()
        return [{"captured_at_utc": str(r[0]), "group_name": r[1], "running_scanners": r[2],
                 "running_threads": r[3], "avg_scan_speed": r[4], "physical_memory_mb": r[5],
                 "free_memory_mb": r[6], "scanner_ids_json": r[7]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# Consolidated RBAC section moved to bottom of file.



# ── Workflow templates (Create Data Source config) ────────────────────────────

@app.get("/api/workflow-templates")
async def get_workflow_templates(u=Depends(get_current_user), key: Optional[str] = Query(None)):
    try:
        with engine.connect() as conn:
            user_filter = ""
            params = {}
            if u["role"] not in ["Admin", "Developer"]:
                user_filter = " AND created_by = :uid "
                params["uid"] = u["id"]

            if key:
                params["k"] = key
                row = conn.execute(
                    text(f"SELECT k, template_kind, payload_json FROM workflow_templates WHERE k = :k {user_filter}"),
                    params,
                ).fetchone()
                if not row:
                    return {"items": []}
                return {"items": [{"key": row[0], "kind": row[1], "payload": row[2]}]}
            
            # List all
            base_where = ""
            if user_filter:
                base_where = " WHERE 1=1 " + user_filter

            rows = conn.execute(
                text(f"SELECT k, template_kind, payload_json FROM workflow_templates {base_where} ORDER BY k"),
                params
            ).fetchall()
        return {"items": [{"key": r[0], "kind": r[1], "payload": r[2]} for r in rows]}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class WorkflowTemplateUpsert(BaseModel):
    key: str = Field(..., min_length=1)
    kind: str = "config"
    payload: str = ""


@app.post("/api/workflow-templates")
async def upsert_workflow_template(payload: WorkflowTemplateUpsert, u=Depends(get_current_user)):
    try:
        with engine.begin() as conn:
            # Check ownership on conflict
            if u["role"] not in ["Admin", "Developer"]:
                existing = conn.execute(
                    text("SELECT created_by FROM workflow_templates WHERE k = :k"),
                    {"k": payload.key}
                ).fetchone()
                if existing and existing[0] != u["id"]:
                    raise HTTPException(status_code=403, detail="Template key owned by another user")

            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json, created_by)
                    VALUES (:k, :kind, :p, :cb)
                    ON CONFLICT (k) DO UPDATE SET template_kind = EXCLUDED.template_kind,
                                                   payload_json = EXCLUDED.payload_json,
                                                   updated_at = CURRENT_TIMESTAMP
                """),
                {"k": payload.key, "kind": payload.kind, "p": payload.payload, "cb": u["id"]},
            )
        return {"ok": True}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Environment build-info proxy (Create Data Source) ────────────────────────

@app.get("/api/environments/{environment_id}/build-info")
async def get_environment_build_info(environment_id: int, token: str = Depends(oauth2_scheme)):
    """Fetch build version from BigID /about API for the given environment."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""SELECT base_url, auth_module_path, auth_function_name, version,
                               env_name, bigid_username, bigid_password_enc
                        FROM test_environments WHERE id = :eid"""),
                {"eid": environment_id},
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Environment not found")
        base_url, auth_path, auth_fn, stored_version, env_name, bigid_user, bigid_pass_enc = row
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        from bigid_auth import Get_auth_token as _get_token
        env_row = {
            "env_name": env_name,
            "base_url": base_url,
            "auth_module_path": auth_path,
            "auth_function_name": auth_fn,
            "bigid_username": bigid_user,
            "bigid_password_enc": bigid_pass_enc,
        }
        tok = _get_token(base_url, env_row)
    except Exception:
        tok = ""

    build_version = ""
    try:
        import requests as _req, re as _re
        about_url = f"{str(base_url or '').rstrip('/')}/api/v1/about"
        resp = _req.get(about_url, headers={"authorization": tok}, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            nested = (data.get("data") or {})
            about_nested = nested.get("about") or nested
            for key in ["releaseVersion", "coreVersion", "version", "buildVersion"]:
                v = str(data.get(key) or about_nested.get(key) or nested.get(key) or "").strip()
                if v:
                    m = _re.search(r"\b(\d{3})[\.\-](\d{1,3})\b", v)
                    if m:
                        build_version = f"{m.group(1)}.{m.group(2)}"
                    else:
                        build_version = v  # use as-is for staging/non-numeric envs
                    break
    except Exception:
        pass

    if build_version:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE test_environments SET version = :v WHERE id = :eid"),
                    {"v": build_version, "eid": environment_id},
                )
        except Exception:
            pass

    return {"build_version": build_version or stored_version or "", "environment_id": environment_id}


@app.post("/api/environments/{environment_id}/sync-classifiers")
async def sync_environment_classifiers(environment_id: int, token: str = Depends(oauth2_scheme)):
    """Fetch classifiers from BigID and store in classifiers + build_classifier_mappings."""
    import re as _re, requests as _req
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT env_name, base_url, auth_module_path, auth_function_name, version,
                       bigid_username, bigid_password_enc
                FROM test_environments WHERE id = :eid
            """), {"eid": environment_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Environment not found")
        env_name, base_url, auth_path, auth_fn, stored_version, bigid_user, bigid_pass_enc = row
        base_url = str(base_url or "").strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="Environment base_url is missing")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Auth
    try:
        from bigid_auth import Get_auth_token as _get_token
        env_row = {"env_name": env_name, "base_url": base_url, "auth_module_path": auth_path,
                   "auth_function_name": auth_fn, "bigid_username": bigid_user, "bigid_password_enc": bigid_pass_enc}
        tok = _get_token(base_url, env_row)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {e}")

    # Determine build version (needed for build_classifier_mappings)
    build_version = stored_version or ""
    if not build_version:
        try:
            about_url = f"{base_url.rstrip('/')}/api/v1/about"
            r = _req.get(about_url, headers={"authorization": tok}, timeout=10, verify=False)
            if r.status_code == 200:
                d = r.json()
                nested = (d.get("data") or {})
                about_nested = nested.get("about") or nested
                for key in ["releaseVersion", "coreVersion", "version", "buildVersion"]:
                    v = str(d.get(key) or about_nested.get(key) or nested.get(key) or "").strip()
                    if v:
                        m = _re.search(r"\b(\d{3})[\.\-](\d{1,3})\b", v)
                        build_version = f"{m.group(1)}.{m.group(2)}" if m else v
                        break
        except Exception:
            pass
    if not build_version:
        build_version = "unknown"

    # Fetch classifiers from BigID
    classifiers_api = (
        "/api/v1/all-classifications?skip=0&limit=600&requireTotalCount=true&"
        "filter=%5B%7B%22field%22%3A%22enabled%22%2C%22value%22%3A%5Btrue%2Cnull%5D%2C%22operator%22%3A%22in%22%7D%2C"
        "%7B%22field%22%3A%22type%22%2C%22value%22%3A%5B%22data%22%2C%22columnName%22%5D%2C%22operator%22%3A%22in%22%7D%5D"
    )
    try:
        cls_url = f"{base_url.rstrip('/')}{classifiers_api}"
        cr = _req.get(cls_url, headers={"authorization": tok}, timeout=30, verify=False)
        cr.raise_for_status()
        payload = cr.json()
        # BigID response: {"status":..., "data": {"classifications": [...]}} or flat list
        data_block = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data_block, dict):
            data_list = (data_block.get("classifications")
                         or data_block.get("data")
                         or data_block.get("items")
                         or data_block.get("results")
                         or [])
        elif isinstance(data_block, list):
            data_list = data_block
        else:
            data_list = []
        raw_names = []
        for item in (data_list or []):
            name = (item.get("classification_name") or item.get("name")
                    or item.get("classifierName") or item.get("displayName") or "")
            if name:
                raw_names.append(str(name).strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch classifiers from BigID: {e}")

    # Deduplicate
    seen, cleaned = set(), []
    for n in raw_names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            cleaned.append(n)

    if not cleaned:
        return {"ok": False, "classifier_count": 0, "build_version": build_version, "detail": "No classifiers returned by BigID"}

    # Store in DB
    try:
        with engine.begin() as conn:
            # Ensure build row exists
            conn.execute(text("INSERT INTO builds (version) VALUES (:v) ON CONFLICT (version) DO NOTHING"), {"v": build_version})
            build_id = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {"v": build_version}).scalar()

            # Upsert classifiers
            for name in cleaned:
                conn.execute(text("INSERT INTO classifiers (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"), {"n": name})

            # Fetch ids
            rows = conn.execute(text("SELECT id, name FROM classifiers WHERE name = ANY(:names)"),
                                {"names": cleaned}).fetchall()
            name_to_id = {r[1]: r[0] for r in rows}

            # Upsert build_classifier_mappings
            new_links = 0
            for name, cid in name_to_id.items():
                result = conn.execute(text("""
                    INSERT INTO build_classifier_mappings (build_id, classifier_id, environment_id)
                    VALUES (:b, :c, :e)
                    ON CONFLICT (build_id, classifier_id, environment_id) DO NOTHING
                """), {"b": build_id, "c": cid, "e": environment_id})
                new_links += result.rowcount

            # Update version on env row
            conn.execute(text("UPDATE test_environments SET version = :v WHERE id = :eid"),
                         {"v": build_version, "eid": environment_id})

        return {"ok": True, "classifier_count": len(cleaned), "build_version": build_version, "new_links": new_links}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------------------
# RBAC MANAGEMENT
# ------------------------------------------------------------------------------

@app.get("/api/rbac/users")
async def list_users(u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    with engine.connect() as conn:
        users = conn.execute(text("""
            SELECT u.id, u.username, r.role_name, u.role_id, u.created_at
            FROM app_users u
            JOIN app_roles r ON u.role_id = r.id
            ORDER BY u.id ASC
        """)).fetchall()
        return [dict(r._mapping) for r in users]

@app.post("/api/rbac/users")
async def create_user(data: dict, u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    username = data.get("username", "").strip().lower()
    role_id = data.get("role_id")
    password = data.get("password", "").strip()
    if not username or "@bigid.com" not in username:
        raise HTTPException(status_code=400, detail="Valid @bigid.com email required")
    if not role_id:
        raise HTTPException(status_code=400, detail="Role required")
    if not password:
        raise HTTPException(status_code=400, detail="Password is required.")
    pwd_hash = hash_password(password)
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, :p, :r)"),
                     {"u": username, "p": pwd_hash, "r": role_id})
        conn.commit()
    return {"status": "ok"}

@app.put("/api/rbac/users/{user_id}")
async def update_user(user_id: int, data: dict, u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    role_id = data.get("role_id")
    password = data.get("password", "").strip()
    if not role_id:
        raise HTTPException(status_code=400, detail="Role required")
    with engine.connect() as conn:
        if password:
            pwd_hash = hash_password(password)
            conn.execute(text("UPDATE app_users SET role_id = :r, password_hash = :p WHERE id = :id"),
                         {"r": role_id, "p": pwd_hash, "id": user_id})
        else:
            conn.execute(text("UPDATE app_users SET role_id = :r WHERE id = :id"),
                         {"r": role_id, "id": user_id})
        conn.commit()
    return {"status": "ok"}

@app.get("/api/rbac/roles")
async def list_roles(u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    with engine.connect() as conn:
        roles = conn.execute(text("SELECT id, role_name FROM app_roles ORDER BY id ASC")).fetchall()
        return [dict(r._mapping) for r in roles]

@app.post("/api/rbac/roles")
async def create_role(data: dict, u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    role_name = data.get("role_name", "").strip()
    if not role_name:
        raise HTTPException(status_code=400, detail="Role name required")
    
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO app_roles (role_name) VALUES (:r)"), {"r": role_name})
        conn.commit()
    return {"status": "ok"}

@app.get("/api/rbac/roles/{role_id}/permissions")
async def get_role_permissions(role_id: int, u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    with engine.connect() as conn:
        perms = conn.execute(text("SELECT page_name FROM app_role_pages WHERE role_id = :rid"), {"rid": role_id}).fetchall()
        return [r[0] for r in perms]

@app.put("/api/rbac/roles/{role_id}/permissions")
async def update_role_permissions(role_id: int, pages: list[str] = Body(...), u=Depends(get_current_user)):
    if u["role"] not in ["Admin", "Developer"]:
        raise HTTPException(status_code=403, detail="Admin or Developer access required")
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM app_role_pages WHERE role_id = :rid"), {"rid": role_id})
        for p in pages:
            conn.execute(text("INSERT INTO app_role_pages (role_id, page_name) VALUES (:rid, :p)"),
                         {"rid": role_id, "p": p})
        conn.commit()
    return {"status": "ok"}

# ------------------------------------------------------------------------------
# SCANNER INFRASTRUCTURE
# ------------------------------------------------------------------------------

def _iso(v):
    return v.isoformat() if v else None


def _resolve_scanner_window(conn, environment_id: int, days: int, run_id: Optional[int], scan_ids: List[str]):
    """Time window and scope descriptor for the Scanner Infrastructure charts.

    Scan(s) win over a run, a run wins over the days window. A scan or run window spans from
    its start to its end, or to now while it is still in progress.
    """
    now = datetime.utcnow()

    if scan_ids:
        rows = conn.execute(text("""
            SELECT scan_id, seq, ds_name, scan_kind, started_at, finished_at
            FROM workflow_steps
            WHERE environment_id = :eid AND scan_id IN :sids
        """).bindparams(bindparam("sids", expanding=True)), {"eid": environment_id, "sids": scan_ids}).fetchall()
        found = {r[0]: r for r in rows}
        missing = [s for s in scan_ids if s not in found]
        if missing:
            raise HTTPException(status_code=404, detail=f"Scan(s) not found on this environment: {', '.join(missing)}")
        picked = [found[s] for s in scan_ids]
        if any(r[4] is None for r in picked):
            raise HTTPException(status_code=400, detail="Selected scan has not started yet")
        start = min(r[4] for r in picked)
        running = any(r[5] is None for r in picked)
        end = now if running else max(r[5] for r in picked)
        label = f"Scan {picked[0][1]}. {picked[0][2] or picked[0][3] or picked[0][0]}" if len(picked) == 1 else f"{len(picked)} scans"
        return start, end, {"scope": "scan", "label": label, "is_running": running}

    if run_id:
        row = conn.execute(text("""
            SELECT id, name, started_at, finished_at, created_at
            FROM workflow_runs WHERE id = :rid
        """), {"rid": run_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow run not found")
        start = row[2] or row[4]
        if start is None:
            raise HTTPException(status_code=400, detail="Workflow run has no start time")
        running = row[3] is None
        end = now if running else row[3]
        return start, end, {"scope": "run", "label": f"Run #{row[0]} {row[1] or 'workflow'}", "is_running": running}

    return now - timedelta(days=days), now, {"scope": "days", "label": f"Last {days} days", "is_running": False}


@app.get("/api/scanner-metrics/scopes")
async def get_scanner_scopes(environment_id: int, days: int = 7, u=Depends(get_current_user)):
    """Workflow runs with steps on this environment (started within `days`), each with its scans."""
    since = datetime.utcnow() - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT r.id, r.name, r.status, r.started_at, r.finished_at, r.created_at,
                   s.id, s.seq, s.scan_id, s.ds_name, s.scan_kind, s.status, s.started_at, s.finished_at
            FROM workflow_steps s
            JOIN workflow_runs r ON r.id = s.run_id
            WHERE s.environment_id = :eid
              AND COALESCE(r.started_at, r.created_at) >= :since
            ORDER BY r.id DESC, s.seq ASC
        """), {"eid": environment_id, "since": since}).fetchall()

    runs = {}
    for r in rows:
        run = runs.setdefault(r[0], {
            "run_id": r[0], "name": r[1], "status": r[2],
            "started_at": _iso(r[3] or r[5]), "finished_at": _iso(r[4]),
            "scan_count": 0, "scans": [],
        })
        if r[8]:
            run["scans"].append({
                "scan_id": r[8], "step_id": r[6], "seq": r[7],
                "label": r[9] or r[10] or "scan", "status": r[11],
                "started_at": _iso(r[12]), "finished_at": _iso(r[13]),
            })
            run["scan_count"] += 1
    return {"runs": list(runs.values())}


@app.get("/api/scanner-metrics/performance")
async def get_scanner_performance(
    environment_id: int,
    days: int = 7,
    run_id: Optional[int] = None,
    scan_id: Optional[str] = None,
    u=Depends(get_current_user)
):
    scan_ids = list(dict.fromkeys(s.strip() for s in (scan_id or "").split(",") if s.strip()))

    with engine.connect() as conn:
        start_dt, end_dt, scope = _resolve_scanner_window(conn, environment_id, days, run_id, scan_ids)

        # Group Performance (to find effective running scanners if possible)
        group_rows = conn.execute(text("""
            SELECT captured_at_utc, group_name, running_scanners, running_threads,
                   max_threads, scanner_threads, physical_memory_mb, free_memory_mb,
                   avg_scan_speed, group_scanner_ids_json
            FROM scanner_group_performance
            WHERE environment_id = :eid AND captured_at_utc BETWEEN :start AND :end
            ORDER BY captured_at_utc ASC
        """), {"eid": environment_id, "start": start_dt, "end": end_dt}).fetchall()

        # Individual Performance. Deployments differ on the thread/load column names (tracker
        # writes max_threads or total_num_of_threads; system_load_average may be absent), so
        # select only what exists to avoid a 500 on the Scanner Infrastructure page.
        threads_expr = (
            "max_threads AS total_num_of_threads"
            if pg_column_exists(engine, "scanner_individual_performance", "max_threads")
            else "total_num_of_threads"
        )
        load_expr = (
            "system_load_average"
            if pg_column_exists(engine, "scanner_individual_performance", "system_load_average")
            else "NULL::float AS system_load_average"
        )
        heartbeat_expr = (
            "update_date_bigid"
            if pg_column_exists(engine, "scanner_individual_performance", "update_date_bigid")
            else "NULL::timestamp AS update_date_bigid"
        )
        indiv_rows = conn.execute(text(f"""
            SELECT id, captured_at_utc, scanner_id, name, status, group_name,
                   running_threads, {threads_expr}, {load_expr},
                   available_processors, cpu_pct, physical_memory_mb, free_memory_mb,
                   {heartbeat_expr}
            FROM scanner_individual_performance
            WHERE environment_id = :eid AND captured_at_utc BETWEEN :start AND :end
            ORDER BY captured_at_utc ASC
        """), {"eid": environment_id, "start": start_dt, "end": end_dt}).fetchall()

    # Process Individual Rows
    def clean_mem(v):
        if isinstance(v, str):
            v = v.upper().replace("MB", "").replace(" ", "").strip()
            try: return float(v)
            except: return 0.0
        return float(v or 0.0)

    processed_indiv = []
    for r in indiv_rows:
        row = dict(r._mapping)
        sid = str(row["scanner_id"] or "")
        # Rows written before the name column existed have no name; label them by scanner id
        # instead of a bare "Unknown" so each pod still gets its own line.
        base = str(row["name"] or "").strip() or "Unnamed scanner"
        row["display_name"] = f"{base} [{sid[:4]}]" if sid else base

        phys = clean_mem(row["physical_memory_mb"])
        free = clean_mem(row["free_memory_mb"])
        used = phys - free
        row["memory_usage_pct"] = (used / phys * 100) if phys > 0 else 0.0
        row["cpu_pct"] = float(row["cpu_pct"] or 0.0)
        row["status"] = str(row["status"] or "")

        # Snapshot time minus BigID's last heartbeat for this scanner. A dead pod keeps being
        # listed with frozen values, so the heartbeat lag is what tells it apart.
        udb = row.pop("update_date_bigid", None)
        try:
            row["heartbeat_lag_sec"] = (row["captured_at_utc"] - udb).total_seconds() if udb else None
        except TypeError:
            row["heartbeat_lag_sec"] = None

        processed_indiv.append(row)

    return {
        "individual": processed_indiv,
        "groups": [dict(r._mapping) for r in group_rows],
        "scope": scope,
        "window_start": _iso(start_dt),
        "window_end": _iso(end_dt),
    }


# ------------------------------------------------------------------------------
# DATA SOURCE CREATION
# ------------------------------------------------------------------------------

@app.get("/api/data-sources/scanner-groups")
async def get_ds_scanner_groups(environment_id: int, u=Depends(get_current_user)):
    with engine.connect() as conn:
        env = conn.execute(text("SELECT * FROM test_environments WHERE id = :id"), {"id": environment_id}).fetchone()
        if not env: raise HTTPException(status_code=404, detail="Environment not found")
        env_dict = dict(env._mapping)
        
    try:
        # Reusing existing logic from dashboard.py style (internal helpers)
        # Note: In a real migration we'd move these helpers to a shared lib.
        # For now, we'll implement the call here.
        base_url = env_dict.get("base_url")
        auth_module = env_dict.get("auth_module_path")
        auth_func = env_dict.get("auth_function_name")
        
        # We'll use a simplified version of fetch_scanner_groups_with_auth_retry
        from dashboard import fetch_scanner_groups_with_auth_retry
        items, total, url = fetch_scanner_groups_with_auth_retry(env_dict)
        return {"items": items, "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/data-sources")
async def create_update_ds(environment_id: int, ds_name: str, payload: dict, u=Depends(get_current_user)):
    with engine.connect() as conn:
        env = conn.execute(text("SELECT * FROM test_environments WHERE id = :id"), {"id": environment_id}).fetchone()
        if not env: raise HTTPException(status_code=404, detail="Environment not found")
        env_dict = dict(env._mapping)
        
    try:
        from dashboard import put_ds_connection_with_auth_retry
        out, url = put_ds_connection_with_auth_retry(env_dict, ds_name, payload)
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/data-sources/test-connection")
async def test_ds_connection(environment_id: int, broadcast_event: str, payload: dict, u=Depends(get_current_user)):
    with engine.connect() as conn:
        env = conn.execute(text("SELECT * FROM test_environments WHERE id = :id"), {"id": environment_id}).fetchone()
        if not env: raise HTTPException(status_code=404, detail="Environment not found")
        env_dict = dict(env._mapping)
        
    try:
        from dashboard import post_ds_connection_test_with_auth_retry
        out, url = post_ds_connection_test_with_auth_retry(env_dict, broadcast_event, payload)
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("v2_api.main:app", host="0.0.0.0", port=8000, reload=True)
