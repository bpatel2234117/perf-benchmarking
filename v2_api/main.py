from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from pydantic import BaseModel, Field
from typing import Dict, List, Literal, Optional
import os
import hashlib

import sys

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
from v2_api.scan_bigid_sync import resync_scan_parents_for_environment, sync_scan_insights
from v2_api.scan_progress_helpers import (
    fetch_live_classifier_hits_from_bigid,
    load_classifier_hits_from_db,
    load_scan_log_correlation,
    load_scan_log_errors_for_scan,
    load_scanner_performance_series,
)

app = FastAPI(title="Scan Performance Benchmark API v2")

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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True, connect_args={"connect_timeout": 3})

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

# Models
class Token(BaseModel):
    access_token: str
    token_type: str

class UserProfile(BaseModel):
    username: str
    role: str
    role_id: int

class Environment(BaseModel):
    id: int
    env_name: str
    base_url: str
    is_active: bool
    is_busy: bool
    auth_module_path: Optional[str] = None
    auth_function_name: Optional[str] = None


class EnvironmentCreate(BaseModel):
    env_name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1, max_length=500)
    auth_module_path: Optional[str] = ""
    auth_function_name: Optional[str] = "Get_auth_token"
    is_active: bool = True


class EnvironmentUpdate(BaseModel):
    env_name: Optional[str] = Field(None, min_length=1, max_length=100)
    base_url: Optional[str] = Field(None, max_length=500)
    auth_module_path: Optional[str] = None
    auth_function_name: Optional[str] = None
    is_active: Optional[bool] = None

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


class WorkflowControlPayload(BaseModel):
    action: Literal["start", "pause", "resume", "stop"]
    env1_id: Optional[int] = None
    env2_id: Optional[int] = None


class ScanInsightsSyncPayload(BaseModel):
    scan_id: str = Field(..., min_length=1, max_length=256)


class ResyncParentsPayload(BaseModel):
    environment_id: int = Field(..., ge=1)
    limit: int = Field(200, ge=10, le=1000)


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
    classifier_list: Optional[str] = None
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
@app.post("/api/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    email = str(form_data.username).strip().lower()
    if not email.endswith("@bigid.com"):
        raise HTTPException(status_code=400, detail="Only @bigid.com emails are allowed.")

    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                SELECT u.id, r.role_name, u.role_id, u.password_hash 
                FROM app_users u
                JOIN app_roles r ON u.role_id = r.id
                WHERE u.username = :u
            """), {"u": email}).fetchone()
            
            if res:
                user_id, role_name, role_id, pwd_hash = res
                if role_name == "Admin" and not verify_password(form_data.password, pwd_hash):
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
            else:
                # Auto-create as Viewer
                viewer_role = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Viewer'")).fetchone()
                v_role_id = viewer_role[0] if viewer_role else 1
                conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, 'PASSWORDLESS', :r)"),
                             {"u": email, "r": v_role_id})
                conn.commit()
    except SQLAlchemyError as e:
        # Fallback fake login for testing UI if Postgres is down
        if email == "bpatel@bigid.com" and form_data.password == "bigid123":
            return {"access_token": f"fake-jwt-token-for-{email}", "token_type": "bearer"}
        raise HTTPException(status_code=503, detail=f"Database connection failed: {str(e)}")

    access_token = f"fake-jwt-token-for-{email}"
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/users/me", response_model=UserProfile)
async def read_users_me(token: str = Depends(oauth2_scheme)):
    email = token.replace("fake-jwt-token-for-", "")
    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                SELECT u.username, r.role_name, u.role_id
                FROM app_users u
                JOIN app_roles r ON u.role_id = r.id
                WHERE u.username = :u
            """), {"u": email}).fetchone()
            if not res:
                raise HTTPException(status_code=401, detail="Invalid token")
            return UserProfile(username=res[0], role=res[1], role_id=res[2])
    except SQLAlchemyError:
        # Mock profile if DB down but token is valid (for local dev)
        if email == "bpatel@bigid.com":
            return UserProfile(username=email, role="Admin", role_id=1)
        raise HTTPException(status_code=503, detail="Database connection failed")

@app.get("/api/environments", response_model=List[Environment])
async def get_environments(token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            res = conn.execute(
                text(
                    """
                    SELECT id, env_name, base_url, is_active, is_busy,
                           NULLIF(TRIM(COALESCE(auth_module_path, '')), ''),
                           NULLIF(TRIM(COALESCE(auth_function_name, '')), '')
                    FROM test_environments ORDER BY env_name
                    """
                )
            ).fetchall()
            out = []
            for r in res:
                auth_path = r[5] if len(r) > 5 else None
                auth_fn = r[6] if len(r) > 6 else None
                out.append(
                    {
                        "id": r[0],
                        "env_name": r[1],
                        "base_url": str(r[2] or ""),
                        "is_active": bool(r[3]) if r[3] is not None else True,
                        "is_busy": bool(r[4]) if r[4] is not None else False,
                        "auth_module_path": auth_path,
                        "auth_function_name": auth_fn or None,
                    }
                )
            return out
    except SQLAlchemyError:
        return []


@app.post("/api/environments", response_model=Environment)
async def create_environment(payload: EnvironmentCreate, token: str = Depends(oauth2_scheme)):
    auth_fn = (payload.auth_function_name or "").strip() or "Get_auth_token"
    auth_path = (payload.auth_module_path or "").strip() or None
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO test_environments (env_name, base_url, auth_module_path, auth_function_name, is_active)
                    VALUES (:name, :url, :apath, :afn, :active)
                    RETURNING id, env_name, base_url, is_active, is_busy
                    """
                ),
                {
                    "name": payload.env_name.strip(),
                    "url": payload.base_url.strip(),
                    "apath": auth_path,
                    "afn": auth_fn,
                    "active": payload.is_active,
                },
            ).fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Insert returned no row")
            return {
                "id": row[0],
                "env_name": row[1],
                "base_url": str(row[2] or ""),
                "is_active": bool(row[3]) if row[3] is not None else True,
                "is_busy": bool(row[4]) if row[4] is not None else False,
                "auth_module_path": auth_path,
                "auth_function_name": auth_fn,
            }
    except IntegrityError as ex:
        raise HTTPException(status_code=409, detail="Environment name already exists or constraint violation.") from ex
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex


@app.put("/api/environments/{environment_id}", response_model=Environment)
async def update_environment(environment_id: int, payload: EnvironmentUpdate, token: str = Depends(oauth2_scheme)):
    eid = int(environment_id)
    sets: List[str] = []
    params: dict = {"eid": eid}
    if payload.env_name is not None:
        sets.append("env_name = :env_name")
        params["env_name"] = payload.env_name.strip()
    if payload.base_url is not None:
        sets.append("base_url = :base_url")
        params["base_url"] = payload.base_url.strip()
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
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
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
                           NULLIF(TRIM(COALESCE(auth_function_name, '')), '')
                    FROM test_environments WHERE id = :eid
                    """
                ),
                {"eid": eid},
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Environment not found")
            return {
                "id": row[0],
                "env_name": row[1],
                "base_url": str(row[2] or ""),
                "is_active": bool(row[3]) if row[3] is not None else True,
                "is_busy": bool(row[4]) if row[4] is not None else False,
                "auth_module_path": row[5],
                "auth_function_name": row[6],
            }
    except IntegrityError as ex:
        raise HTTPException(status_code=409, detail="Duplicate name or constraint violation.") from ex
    except SQLAlchemyError as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex


@app.delete("/api/environments/{environment_id}")
async def delete_environment(environment_id: int, token: str = Depends(oauth2_scheme)):
    eid = int(environment_id)
    try:
        with engine.begin() as conn:
            r = conn.execute(text("DELETE FROM test_environments WHERE id = :eid RETURNING id"), {"eid": eid})
            row = r.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Environment not found")
        return {"ok": True, "id": eid}
    except IntegrityError as ex:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: environment is still referenced by benchmarks, workflows, scans, or history. "
            "Deactivate it instead or remove dependent rows.",
        ) from ex
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
    token: str = Depends(oauth2_scheme),
):
    """Paginated AI / scanner errors (PostgreSQL scan_log_errors)."""
    try:
        with engine.connect() as conn:
            total_row = conn.execute(text("SELECT COUNT(*) FROM scan_log_errors")).scalar()
            total = int(total_row or 0)
            res = conn.execute(
                text(
                    """
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
                        sle.is_resolved
                    FROM scan_log_errors sle
                    LEFT JOIN test_environments te ON te.id = sle.environment_id
                    ORDER BY sle.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"limit": limit, "offset": offset},
            ).fetchall()
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
            }
            for r in res
        ]
        return {"items": items, "total": total}
    except SQLAlchemyError:
        return {"items": [], "total": 0}


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
                parent = {
                    "scan_id": str(row[0] or ""),
                    "scan_name": row[1],
                    "scan_type": row[2],
                    "scan_status": row[3],
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

    rows, note, hint, fallback_names = load_classifier_hits_from_db(engine, sid)
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
    }


@app.get("/api/scan/{scan_id}/scanner-series")
async def get_scanner_performance_route(scan_id: str, token: str = Depends(oauth2_scheme)):
    """Time-series scanner group + pod samples for charts (requires tracker tables in DB)."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return load_scanner_performance_series(engine, sid)


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
    """Scanner / subsystem error rows from scan_log_errors filtered by scan_id."""
    sid = str(scan_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="scan_id required")
    return {"scan_id": sid, "items": load_scan_log_errors_for_scan(engine, sid, limit=limit)}


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
            id,
            scan_id,
            scan_name,
            environment_id,
            "timestamp",
            captured_at_utc,
            scan_state,
            completed_sub_scans,
            failed_sub_scans,
            total_sub_scans,
            completed_parts,
            failed_parts,
            total_parts_scanned,
            total_classifications,
            newly_added_classifications,
            false_removed_classifications,
            objects_scanned,
            objects_failed,
            objects_with_pi
        FROM scan_progress
        WHERE scan_id = :sid
        ORDER BY COALESCE(captured_at_utc, "timestamp") ASC NULLS LAST, id ASC
        LIMIT :lim
        """
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"sid": sid, "lim": lim}).fetchall()
            cnt = conn.execute(
                text("SELECT COUNT(*) FROM scan_progress WHERE scan_id = :sid"), {"sid": sid}
            ).scalar()
            total = int(cnt or 0)
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
    return {"items": items, "total": total, "returned": len(items)}


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
        sp.scan_name,
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
    """
    try:
        msg = sync_scan_insights(engine, payload.scan_id.strip())
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
        ws.is_enabled, ws.build_version, ws.build_prefix, ws.scan_id, ws.status, ws.last_error,
        ws.started_at, ws.finished_at,
        ws.ds_id, ws.scan_profile_id, ws.scan_profile_name,
        ws.is_acl, ws.is_ocr, ws.is_structured_ff, ws.is_ols, ws.is_lite,
        ws.notes, ws.feature_flags
    FROM workflow_steps ws
    LEFT JOIN test_environments te ON ws.environment_id = te.id
"""


@app.get("/api/workflow/runs")
async def get_workflow_runs(
    token: str = Depends(oauth2_scheme),
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated workflow orchestration runs (workflow_runner / Streamlit parity)."""
    try:
        wf = get_workflow_sql_engine()
        with wf.connect() as conn:
            total_row = conn.execute(text("SELECT COUNT(*) FROM workflow_runs")).scalar()
            total = int(total_row or 0)
            res = conn.execute(
                text(
                    """
                    SELECT id, name, status, env1_id, env2_id, cooldown_minutes, last_error,
                           next_eligible_at, scan_template_id, started_at, finished_at
                    FROM workflow_runs
                    ORDER BY id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"limit": limit, "offset": offset},
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
async def get_workflow_steps(run_id: int, token: str = Depends(oauth2_scheme)):
    """Steps for a workflow run — joined with env names/busy flags (Streamlit parity)."""
    try:
        with get_workflow_sql_engine().connect() as conn:
            res = conn.execute(
                text(_WORKFLOW_STEP_SELECT + " WHERE ws.run_id = :r ORDER BY ws.seq"),
                {"r": run_id},
            ).fetchall()
        return [_workflow_step_row_dict(r) for r in res]
    except SQLAlchemyError:
        return []


@app.get("/api/workflow/env-status")
async def get_workflow_env_status(token: str = Depends(oauth2_scheme)):
    """
    Env 🟢/🔴 strip: active envs marked busy when `test_environments.is_busy`
    OR a running workflow references the env (mirrors Streamlit union, without per-step pandas derivations).
    """
    try:
        with get_workflow_sql_engine().connect() as conn:
            res = conn.execute(_WORKFLOW_ENV_STATUS_SQL).fetchall()
        return [{"id": r[0], "env_name": r[1], "busy": bool(r[2])} for r in res]
    except SQLAlchemyError:
        return []


@app.post("/api/workflow/runs")
async def create_workflow_run_endpoint(payload: WorkflowCreatePayload, token: str = Depends(oauth2_scheme)):
    """Create workflow run + step matrix (default from catalog or client-selected steps)."""
    try:
        from v2_api.workflow_ops import insert_workflow_run_with_steps  # noqa: PLC0415

        eng = get_workflow_sql_engine()
        steps_kw = None
        if payload.steps is not None and len(payload.steps) > 0:
            steps_kw = [s.model_dump() for s in payload.steps]
        rid = insert_workflow_run_with_steps(
            eng,
            payload.name,
            payload.cooldown_minutes,
            payload.env1_id,
            payload.env2_id,
            steps_override=steps_kw,
        )
        return {"id": rid, "message": "created"}
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
    token: str = Depends(oauth2_scheme),
):
    """Start / pause / resume / stop — updates DB like Streamlit query actions (no Streamlit required)."""
    try:
        from v2_api.workflow_ops import workflow_control  # noqa: PLC0415

        return workflow_control(
            get_workflow_sql_engine(),
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
async def delete_workflow_run(run_id: int, token: str = Depends(oauth2_scheme)):
    """Delete a workflow run and its steps (only when not running)."""
    wf_engine = get_workflow_sql_engine()
    try:
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
async def save_benchmark(payload: BenchmarkSavePayload, token: str = Depends(oauth2_scheme)):
    """
    Full benchmark save — equivalent to Streamlit 'Save Benchmark Run'.
    Creates/reuses lookup rows (builds, data_source_types, file_set_types, configurations)
    then inserts into benchmarks.
    """
    try:
        with engine.begin() as conn:
            build_id = _get_or_create_build(conn, payload.build_version.strip())
            dst_id = _get_or_create_id(conn, "data_source_types", "name", payload.data_source_type.strip())

            # file_set_types may have extra columns — upsert name only, update sizes if provided
            fst_row = conn.execute(
                text("SELECT id FROM file_set_types WHERE name = :n"), {"n": payload.file_set_type.strip()}
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
                    {"n": payload.file_set_type.strip(),
                     "dt": payload.data_type or None,
                     "tf": payload.total_files, "tfo": payload.total_folders,
                     "tsg": payload.total_size_gb},
                ).fetchone()
                fst_id = int(r2[0])

            config_id = _get_or_create_config(
                conn, payload.scanner_count, payload.thread_count,
                payload.is_acl, payload.is_ocr, payload.is_feature_flags, payload.feature_flags
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
                        total_catalog_items, scan_id, is_active, is_release, updated_at
                    ) VALUES (
                        :bid, :cid, :rd, :st, :sct,
                        :dst, :fst,
                        :ss, :se, :tsh,
                        :tcg, :tsg2, :ttg,
                        :tcf, :tsf, :ttf,
                        :tci, :sid, TRUE, :isr, CURRENT_TIMESTAMP
                    ) RETURNING id
                """),
                {
                    "bid": build_id, "cid": config_id,
                    "rd": payload.run_date, "st": payload.scenario_type, "sct": payload.scan_type,
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
                    b.scan_id
                FROM benchmarks b
                JOIN builds bv ON bv.id = b.build_id
                LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
                LEFT JOIN configurations cfg ON cfg.id = b.config_id
                LEFT JOIN scan_insights si ON si.scan_id = b.scan_id
                {where}
                ORDER BY bv.id DESC, b.id DESC
            """), params).fetchall()

        out = []
        for r in rows:
            out.append({
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
            })
        return {"rows": out, "total": len(out)}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Benchmarks: list with filters, update, delete ────────────────────────────

@app.get("/api/benchmarks/list")
async def list_benchmarks(
    token: str = Depends(oauth2_scheme),
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """Paginated benchmark list for Edit Records page."""
    try:
        where = "WHERE COALESCE(b.is_active, TRUE) = TRUE"
        params: dict = {"limit": limit, "offset": offset}
        if search and search.strip():
            where += """ AND (
                bv.version ILIKE :s OR b.scan_type ILIKE :s OR dst.name ILIKE :s OR b.id::text = :sid
            )"""
            params["s"] = f"%{search.strip()}%"
            params["sid"] = search.strip()
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
                       cfg.is_feature_flags, cfg.feature_flags, cfg.comment,
                       b.total_catalog_items, b.is_active, b.is_release, b.scan_id,
                       b.config_id, b.build_id
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
                "is_feature_flags","feature_flags","comment","total_catalog_items","is_active",
                "is_release","scan_id","config_id","build_id"]
        return {"items": [dict(zip(cols, r)) for r in rows], "total": int(total)}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BenchmarkUpdate(BaseModel):
    run_date: Optional[str] = None
    scenario_type: Optional[str] = None
    scan_type: Optional[str] = None
    total_scan_hours: Optional[float] = None
    throughput_cluster_gb_hr: Optional[float] = None
    throughput_scanner_gb_hr: Optional[float] = None
    throughput_thread_gb_hr: Optional[float] = None
    throughput_cluster_files_hr: Optional[float] = None
    throughput_scanner_files_hr: Optional[float] = None
    throughput_thread_files_hr: Optional[float] = None
    total_catalog_items: Optional[int] = None
    is_active: Optional[bool] = None
    is_release: Optional[bool] = None


@app.put("/api/benchmarks/{benchmark_id}")
async def update_benchmark(benchmark_id: int, payload: BenchmarkUpdate, token: str = Depends(oauth2_scheme)):
    sets, params = [], {"bid": benchmark_id}
    for field, val in payload.model_dump(exclude_none=True).items():
        sets.append(f"{field} = :{field}")
        params[field] = val
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets.append("updated_at = CURRENT_TIMESTAMP")
    try:
        with engine.begin() as conn:
            r = conn.execute(text(f"UPDATE benchmarks SET {', '.join(sets)} WHERE id = :bid RETURNING id"), params)
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="Benchmark not found")
        return {"ok": True, "id": benchmark_id}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/benchmarks/{benchmark_id}")
async def delete_benchmark(benchmark_id: int, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM benchmark_findings WHERE benchmark_id = :id"), {"id": benchmark_id})
            r = conn.execute(text("DELETE FROM benchmarks WHERE id = :id RETURNING id"), {"id": benchmark_id})
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="Benchmark not found")
        return {"ok": True}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BulkDeletePayload(BaseModel):
    ids: List[int]


@app.post("/api/benchmarks/bulk-delete")
async def bulk_delete_benchmarks(payload: BulkDeletePayload, token: str = Depends(oauth2_scheme)):
    if not payload.ids:
        return {"ok": True, "deleted": 0}
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM benchmark_findings WHERE benchmark_id = ANY(:ids)"),
                {"ids": payload.ids},
            )
            r = conn.execute(
                text("DELETE FROM benchmarks WHERE id = ANY(:ids)"),
                {"ids": payload.ids},
            )
        return {"ok": True, "deleted": r.rowcount}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Builds: list, update release tags ────────────────────────────────────────

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
            row = conn.execute(text("SELECT id, env_name, base_url, auth_module_path, auth_function_name, version FROM test_environments WHERE id = :eid"), {"eid": environment_id}).fetchone()
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
            build_version = env_row.get("version")
            
        if not build_version:
             raise HTTPException(status_code=400, detail="Could not determine build version for snapshot capture")
             
        ok = sync_classifier_details_for_env_build(engine, env_row, env_token, build_version, force=True)
        return {"ok": ok, "build_version": build_version}
    except HTTPException:
        raise
    except Exception as e:
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
        has_cpu = _db_has_column(engine, "scanner_individual_performance", "cpu_pct")
        has_mem = _db_has_column(engine, "scanner_individual_performance", "memory_usage_pct")
        has_disp = _db_has_column(engine, "scanner_individual_performance", "display_name")
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


# ── RBAC management ───────────────────────────────────────────────────────────

@app.get("/api/rbac/users")
async def list_users(token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT u.id, u.username, r.role_name, u.role_id, u.created_at
                FROM app_users u JOIN app_roles r ON u.role_id = r.id
                ORDER BY u.username
            """)).fetchall()
        return [{"id": r[0], "username": r[1], "role_name": r[2], "role_id": r[3],
                 "created_at": str(r[4]) if r[4] else None} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3)
    role_id: int


class UserRoleUpdate(BaseModel):
    role_id: int


@app.post("/api/rbac/users")
async def create_user(payload: UserCreate, token: str = Depends(oauth2_scheme)):
    email = payload.username.strip().lower()
    if "@bigid.com" not in email:
        raise HTTPException(status_code=400, detail="Only @bigid.com emails allowed")
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, 'PASSWORDLESS', :r) RETURNING id"),
                {"u": email, "r": payload.role_id},
            ).fetchone()
        return {"ok": True, "id": r[0]}
    except IntegrityError:
        raise HTTPException(status_code=409, detail="User already exists")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/rbac/users/{user_id}/role")
async def update_user_role(user_id: int, payload: UserRoleUpdate, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text("UPDATE app_users SET role_id = :rid WHERE id = :uid RETURNING id"),
                {"rid": payload.role_id, "uid": user_id},
            )
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="User not found")
        return {"ok": True}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/rbac/users/{user_id}")
async def delete_user(user_id: int, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            r = conn.execute(text("DELETE FROM app_users WHERE id = :id RETURNING id"), {"id": user_id})
            if r.rowcount == 0:
                raise HTTPException(status_code=404, detail="User not found")
        return {"ok": True}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rbac/roles")
async def list_roles(token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, role_name FROM app_roles ORDER BY role_name")).fetchall()
        return [{"id": r[0], "role_name": r[1]} for r in rows]
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class RoleCreate(BaseModel):
    role_name: str = Field(..., min_length=1)


@app.post("/api/rbac/roles")
async def create_role(payload: RoleCreate, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text("INSERT INTO app_roles (role_name) VALUES (:n) RETURNING id"),
                {"n": payload.role_name.strip()},
            ).fetchone()
        return {"ok": True, "id": r[0]}
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Role already exists")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rbac/roles/{role_id}/permissions")
async def get_role_permissions(role_id: int, token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT page_name FROM app_role_pages WHERE role_id = :rid ORDER BY page_name"),
                {"rid": role_id},
            ).fetchall()
        return {"role_id": role_id, "pages": [r[0] for r in rows]}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class PermissionsUpdate(BaseModel):
    pages: List[str]


@app.put("/api/rbac/roles/{role_id}/permissions")
async def update_role_permissions(role_id: int, payload: PermissionsUpdate, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_role_pages WHERE role_id = :rid"), {"rid": role_id})
            for page in payload.pages:
                conn.execute(
                    text("INSERT INTO app_role_pages (role_id, page_name) VALUES (:rid, :p)"),
                    {"rid": role_id, "p": page},
                )
        return {"ok": True, "pages": len(payload.pages)}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Workflow templates (Create Data Source config) ────────────────────────────

@app.get("/api/workflow-templates")
async def get_workflow_templates(token: str = Depends(oauth2_scheme), key: Optional[str] = Query(None)):
    try:
        with engine.connect() as conn:
            if key:
                row = conn.execute(
                    text("SELECT k, template_kind, payload_json FROM workflow_templates WHERE k = :k"),
                    {"k": key},
                ).fetchone()
                if not row:
                    return {"items": []}
                return {"items": [{"key": row[0], "kind": row[1], "payload": row[2]}]}
            rows = conn.execute(
                text("SELECT k, template_kind, payload_json FROM workflow_templates ORDER BY k")
            ).fetchall()
        return {"items": [{"key": r[0], "kind": r[1], "payload": r[2]} for r in rows]}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


class WorkflowTemplateUpsert(BaseModel):
    key: str = Field(..., min_length=1)
    kind: str = "config"
    payload: str = ""


@app.post("/api/workflow-templates")
async def upsert_workflow_template(payload: WorkflowTemplateUpsert, token: str = Depends(oauth2_scheme)):
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO workflow_templates (k, template_kind, payload_json)
                    VALUES (:k, :kind, :p)
                    ON CONFLICT (k) DO UPDATE SET template_kind = EXCLUDED.template_kind,
                                                   payload_json = EXCLUDED.payload_json,
                                                   updated_at = CURRENT_TIMESTAMP
                """),
                {"k": payload.key, "kind": payload.kind, "p": payload.payload},
            )
        return {"ok": True}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Environment build-info proxy (Create Data Source) ────────────────────────

@app.get("/api/environments/{environment_id}/build-info")
async def get_environment_build_info(environment_id: int, token: str = Depends(oauth2_scheme)):
    """Fetch build version from BigID /about API for the given environment."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT base_url, auth_module_path, auth_function_name, version FROM test_environments WHERE id = :eid"),
                {"eid": environment_id},
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Environment not found")
        base_url, auth_path, auth_fn, stored_version = row
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        from bigid_auth import Get_auth_token as _get_token
        env_row = {"base_url": base_url, "auth_module_path": auth_path, "auth_function_name": auth_fn}
        tok = _get_token(env_row)
    except Exception:
        tok = ""

    build_version = ""
    try:
        import requests as _req
        about_url = f"{str(base_url or '').rstrip('/')}/api/v1/about"
        resp = _req.get(about_url, headers={"authorization": tok}, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            for key in ["releaseVersion", "coreVersion", "version", "buildVersion"]:
                v = (data.get(key) or (data.get("data") or {}).get(key) or "")
                if v:
                    import re as _re
                    m = _re.search(r"\b(\d{3})[\.\-](\d{1,3})\b", str(v))
                    if m:
                        build_version = f"{m.group(1)}.{m.group(2)}"
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("v2_api.main:app", host="0.0.0.0", port=8000, reload=True)
