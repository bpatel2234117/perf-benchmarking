from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
import os
import hashlib

import sys

# Ensure we can import from v2_api and the root project directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import our normalized Postgres config
from v2_api.db_config_pg import DB_CONNECTION_STRING

app = FastAPI(title="Scan Performance Benchmark API v2")

# Setup CORS for the React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174", "http://localhost:5175"], # Vite default ports
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
            res = conn.execute(text("SELECT id, env_name, base_url, is_active, is_busy FROM test_environments ORDER BY env_name")).fetchall()
            return [{"id": r[0], "env_name": r[1], "base_url": str(r[2]), "is_active": r[3], "is_busy": r[4]} for r in res]
    except SQLAlchemyError:
        return []

@app.get("/api/trends/small-files")
async def get_small_files_trend_data(
    token: str = Depends(oauth2_scheme),
    max_builds: int = Query(150, ge=5, le=600, description="Include rows for up to this many newest builds that have Small Files benchmarks."),
):
    """
    Rows for Small Files trend analysis: matches Streamlit `Small Files Trend Analysis` inputs.

    Rows are constrained by distinct **builds** (newest first), not a flat benchmark row limit, so charts can show
    "last N builds" without being starved when each build inserts many benchmarks.

    `data_segment`: S3/SMB types are always `unstructured`; relational DB types (MSSQL, Postgres, MySQL, …) are `structured`;
    otherwise structured = FF / discovery-style rules (names, feature_flags, etc.).
    """
    try:
        with engine.connect() as conn:
            res = conn.execute(
                text(
                    """
                    WITH recent_build_ids AS (
                        SELECT bv.id AS build_pk
                        FROM builds bv
                        WHERE EXISTS (
                            SELECT 1
                            FROM benchmarks b0
                            INNER JOIN file_set_types fst0 ON b0.file_set_type_id = fst0.id
                            WHERE b0.build_id = bv.id
                              AND fst0.name ILIKE '%Small Files%'
                              AND (b0.is_active IS NULL OR b0.is_active = TRUE)
                        )
                        ORDER BY bv.id DESC
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
                            WHEN LOWER(COALESCE(cfg.feature_flags, '')) LIKE '%structured%' THEN 'structured'
                            WHEN LOWER(COALESCE(b.scenario_type, '')) LIKE '%structured%' THEN 'structured'
                            WHEN LOWER(COALESCE(fst.name, '')) LIKE '%structured%' THEN 'structured'
                            WHEN LOWER(COALESCE(fst.data_type, '')) LIKE '%struct%' THEN 'structured'
                            ELSE 'unstructured'
                        END AS data_segment,
                        b.total_scan_hours::double precision AS total_scan_hours,
                        b.throughput_cluster_gb_hr::double precision AS throughput_cluster_gb_hr,
                        b.throughput_scanner_gb_hr::double precision AS throughput_scanner_gb_hr,
                        b.throughput_thread_gb_hr::double precision AS throughput_thread_gb_hr,
                        b.throughput_cluster_files_hr::double precision AS throughput_cluster_files_hr,
                        b.throughput_scanner_files_hr::double precision AS throughput_scanner_files_hr,
                        b.throughput_thread_files_hr::double precision AS throughput_thread_files_hr,
                        b.total_catalog_items::bigint AS total_catalog_items
                    FROM benchmarks b
                    INNER JOIN recent_build_ids rbi ON b.build_id = rbi.build_pk
                    LEFT JOIN builds bv ON b.build_id = bv.id
                    LEFT JOIN data_source_types dst ON b.data_source_type_id = dst.id
                    LEFT JOIN file_set_types fst ON b.file_set_type_id = fst.id
                    LEFT JOIN configurations cfg ON b.config_id = cfg.id
                    WHERE fst.name IS NOT NULL
                      AND fst.name ILIKE '%Small Files%'
                      AND (b.is_active IS NULL OR b.is_active = TRUE)
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
                }
            )
        return out
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
async def get_log_errors(limit: int = 50, token: str = Depends(oauth2_scheme)):
    try:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT id, environment_id, log_timestamp, error_context, ai_analysis, log_source, is_resolved FROM scan_log_errors ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}).fetchall()
        return [{"id": r[0], "environment_id": r[1], "log_timestamp": r[2], "error_context": r[3], "ai_analysis": r[4], "log_source": r[5], "is_resolved": r[6]} for r in res]
    except SQLAlchemyError:
        return []

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
    limit: int = Query(5000, ge=1, le=10000),
):
    """Latest workflow orchestration runs (workflow_runner / Streamlit)."""
    try:
        with get_workflow_sql_engine().connect() as conn:
            res = conn.execute(
                text(
                    """
                    SELECT id, name, status, env1_id, env2_id, cooldown_minutes, last_error,
                           next_eligible_at, scan_template_id, started_at, finished_at
                    FROM workflow_runs
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).fetchall()
        return [
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
    except SQLAlchemyError:
        return []


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("v2_api.main:app", host="0.0.0.0", port=8000, reload=True)
