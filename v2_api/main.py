from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import create_engine, text
from pydantic import BaseModel
from typing import List, Optional
import os
import hashlib
from datetime import datetime, timedelta

# Import our normalized Postgres config
from v2_api.db_config_pg import DB_CONNECTION_STRING

app = FastAPI(title="Scan Performance Benchmark API v2")

# Setup CORS for the React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], # Vite default ports
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database Setup
engine = create_engine(DB_CONNECTION_STRING)

# Auth Setup
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/token")

def hash_password(password: str) -> str:
    """Return the SHA256 hash of a password (Legacy compat with dashboard_auth.py)."""
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

# Routes
@app.post("/api/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    email = str(form_data.username).strip().lower()
    if not email.endswith("@bigid.com"):
        raise HTTPException(status_code=400, detail="Only @bigid.com emails are allowed.")

    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT u.id, r.role_name, u.role_id, u.password_hash 
            FROM app_users u
            JOIN app_roles r ON u.role_id = r.id
            WHERE u.username = :u
        """), {"u": email}).fetchone()
        
        if res:
            user_id, role_name, role_id, pwd_hash = res
            
            # If user is Admin, verify password
            if role_name == "Admin":
                if not verify_password(form_data.password, pwd_hash):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Incorrect email or password",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        else:
            # Auto-create as Viewer (Ported from Streamlit logic)
            viewer_role = conn.execute(text("SELECT id FROM app_roles WHERE role_name = 'Viewer'")).fetchone()
            v_role_id = viewer_role[0] if viewer_role else 1
            conn.execute(text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, 'PASSWORDLESS', :r)"),
                         {"u": email, "r": v_role_id})
            conn.commit()

    # Note: In a real JWT setup, we'd use python-jose to encode this. 
    # For now, mimicking the token passing.
    access_token = f"fake-jwt-token-for-{email}"
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/users/me", response_model=UserProfile)
async def read_users_me(token: str = Depends(oauth2_scheme)):
    # Very basic mock validation to extract email from fake token
    email = token.replace("fake-jwt-token-for-", "")
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

@app.get("/api/environments", response_model=List[Environment])
async def get_environments(token: str = Depends(oauth2_scheme)):
    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT id, env_name, base_url, is_active, is_busy 
            FROM test_environments 
            ORDER BY env_name
        """)).fetchall()
        
    return [{"id": r[0], "env_name": r[1], "base_url": str(r[2]), "is_active": r[3], "is_busy": r[4]} for r in res]

@app.get("/api/benchmarks")
async def get_benchmarks(token: str = Depends(oauth2_scheme), limit: int = 50):
    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT b.id, b.run_date, b.scenario_type, b.scan_type, 
                   t.name as data_source_type, f.name as file_set_type
            FROM benchmarks b
            LEFT JOIN data_source_types t ON b.data_source_type_id = t.id
            LEFT JOIN file_set_types f ON b.file_set_type_id = f.id
            ORDER BY b.id DESC LIMIT :limit
        """), {"limit": limit}).fetchall()
        
    benchmarks = []
    for r in res:
        benchmarks.append({
            "id": r[0],
            "run_date": r[1],
            "scenario_type": r[2],
            "scan_type": r[3],
            "data_source_type": r[4],
            "file_set_type": r[5]
        })
    return benchmarks

class Classifier(BaseModel):
    id: int
    name: str

class BenchmarkCreate(BaseModel):
    build_id: int
    config_id: int
    classifier_set_id: int
    scenario_type: str
    scan_type: str
    data_source_type_id: int
    file_set_type_id: int

@app.post("/api/benchmarks", response_model=dict)
async def create_benchmark(benchmark: BenchmarkCreate, token: str = Depends(oauth2_scheme)):
    with engine.begin() as conn:
        res = conn.execute(text("""
            INSERT INTO benchmarks 
            (build_id, config_id, classifier_set_id, scenario_type, scan_type, data_source_type_id, file_set_type_id, is_active) 
            VALUES (:b_id, :c_id, :cs_id, :st, :sct, :dst, :fst, TRUE)
            RETURNING id
        """), {
            "b_id": benchmark.build_id,
            "c_id": benchmark.config_id,
            "cs_id": benchmark.classifier_set_id,
            "st": benchmark.scenario_type,
            "sct": benchmark.scan_type,
            "dst": benchmark.data_source_type_id,
            "fst": benchmark.file_set_type_id
        }).fetchone()
        
    return {"message": "Benchmark created", "id": res[0]}

@app.get("/api/benchmarks/{benchmark_id}")
async def get_benchmark(benchmark_id: int, token: str = Depends(oauth2_scheme)):
    with engine.connect() as conn:
        b_res = conn.execute(text("""
            SELECT b.id, b.run_date, b.scenario_type, b.scan_type, b.is_active,
                   t.name as data_source_type, f.name as file_set_type
            FROM benchmarks b
            LEFT JOIN data_source_types t ON b.data_source_type_id = t.id
            LEFT JOIN file_set_types f ON b.file_set_type_id = f.id
            WHERE b.id = :id
        """), {"id": benchmark_id}).fetchone()
        
        if not b_res:
            raise HTTPException(status_code=404, detail="Benchmark not found")
            
        f_res = conn.execute(text("""
            SELECT c.name, bf.finding_count
            FROM benchmark_findings bf
            JOIN classifiers c ON bf.classifier_id = c.id
            WHERE bf.benchmark_id = :id
        """), {"id": benchmark_id}).fetchall()
        
    benchmark = {
        "id": b_res[0],
        "run_date": b_res[1],
        "scenario_type": b_res[2],
        "scan_type": b_res[3],
        "is_active": b_res[4],
        "data_source_type": b_res[5],
        "file_set_type": b_res[6],
        "findings": [{"classifier": r[0], "count": r[1]} for r in f_res]
    }
    return benchmark

@app.get("/api/classifiers", response_model=List[Classifier])
async def get_classifiers(token: str = Depends(oauth2_scheme)):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT id, name FROM classifiers ORDER BY name")).fetchall()
    return [{"id": r[0], "name": r[1]} for r in res]

@app.get("/api/logs/errors")
async def get_log_errors(limit: int = 50, token: str = Depends(oauth2_scheme)):
    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT id, environment_id, log_timestamp, error_context, ai_analysis, log_source, is_resolved
            FROM scan_log_errors 
            ORDER BY created_at DESC LIMIT :limit
        """), {"limit": limit}).fetchall()
        
    return [{
        "id": r[0], 
        "environment_id": r[1], 
        "log_timestamp": r[2], 
        "error_context": r[3],
        "ai_analysis": r[4],
        "log_source": r[5],
        "is_resolved": r[6]
    } for r in res]

@app.post("/api/workflow/stop/{benchmark_id}")
async def stop_workflow(benchmark_id: int, token: str = Depends(oauth2_scheme)):
    with engine.begin() as conn:
        res = conn.execute(text("""
            UPDATE benchmarks SET is_active = FALSE 
            WHERE id = :id AND is_active = TRUE
        """), {"id": benchmark_id})
        
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active benchmark not found")
            
    return {"message": f"Workflow {benchmark_id} stopped"}

@app.post("/api/environments/{env_id}/busy")
async def set_environment_busy(env_id: int, is_busy: bool, run_id: Optional[int] = None, token: str = Depends(oauth2_scheme)):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE test_environments 
            SET is_busy = :busy, busy_by_run_id = :run_id, busy_since = CURRENT_TIMESTAMP
            WHERE id = :id
        """), {"busy": is_busy, "run_id": run_id, "id": env_id})
    return {"message": "Environment status updated"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("v2_api.main:app", host="0.0.0.0", port=8000, reload=True)
