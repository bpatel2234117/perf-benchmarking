#!/usr/bin/env bash
# Pre-flight before starting a new workflow run on EC2.
set -euo pipefail

echo "=== Docker containers ==="
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'spb_|NAMES' || true

echo ""
echo "=== Workflow runner (last 5 lines) ==="
docker logs spb_workflow 2>&1 | tail -5 || true

echo ""
echo "=== DB: running workflows & env locks (via spb_api) ==="
docker exec spb_api python3 - <<'PY'
import json
from sqlalchemy import create_engine, text
from v2_api.db_config_pg import DB_CONNECTION_STRING

e = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)
with e.connect() as c:
    runs = c.execute(text("""
        SELECT id, status, last_error FROM workflow_runs
        WHERE LOWER(TRIM(COALESCE(status,''))) IN ('running','created','paused')
        ORDER BY id DESC LIMIT 5
    """)).fetchall()
    print("Active-ish runs:", runs or "(none)")

    locks = c.execute(text("""
        SELECT id, env_name, is_busy, busy_by_run_id
        FROM test_environments
        WHERE COALESCE(is_busy,FALSE)=TRUE OR busy_by_run_id IS NOT NULL
        ORDER BY id
    """)).fetchall()
    print("Env locks:", locks or "(none — OK)")

    pool = c.execute(text("""
        SELECT id, env_name FROM test_environments
        WHERE COALESCE(is_active,FALSE)=TRUE
          AND COALESCE(include_in_workflow_runs,TRUE)=TRUE
        ORDER BY env_name
    """)).fetchall()
    print("Workflow pool envs:", pool or "(NONE — enable envs for workflow runs)")

    inflight = c.execute(text("""
        SELECT COUNT(*) FROM workflow_steps ws
        JOIN workflow_runs wr ON wr.id = ws.run_id
        WHERE LOWER(TRIM(COALESCE(wr.status,''))) = 'running'
          AND LOWER(TRIM(COALESCE(ws.status,''))) = 'scan_running'
          AND ws.scan_id IS NOT NULL AND TRIM(COALESCE(ws.scan_id,'')) <> ''
    """)).scalar()
    print("Workflow scan_running steps (DB):", int(inflight or 0))
PY

echo ""
echo "=== PostgreSQL boolean SQL audit (no is_active/is_busy = 0/1) ==="
docker exec spb_api python3 v2_api/pg_sql_audit.py 2>&1 || {
  echo "POSTGRES SQL AUDIT FAILED — fix boolean SQL before deploy"
  exit 1
}

echo ""
echo "=== Tracker queue audit (must be ok: true) ==="
docker exec spb_api python3 v2_api/tracker.py --validate-queues 2>&1 || {
  echo "TRACKER QUEUE AUDIT FAILED — fix before starting workflows"
  exit 1
}

echo ""
echo "Done. Deploy latest code then: Create & start from React, or Clear env locks if any locks remain."
