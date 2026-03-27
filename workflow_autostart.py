"""
Auto-start `workflow_runner.py` from Streamlit process.

Goal: when a workflow run is marked `running`, ensure a background runner
process is running on the same machine, without manual commands.

Safety:
- Uses DB check + pidfile to avoid spawning duplicates.
- Can be disabled via DISABLE_WORKFLOW_AUTOSTART=1
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import text


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name != "nt":
            os.kill(pid, 0)
            return True
        out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.STDOUT)
        s = out.decode("utf-8", errors="ignore").lower()
        return str(pid) in s
    except Exception:
        return False


def _read_pidfile(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        return int(raw) if raw.isdigit() else None
    except Exception:
        return None


def _write_pidfile(path: Path, pid: int) -> None:
    try:
        path.write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass


def _has_running_workflow(engine) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM workflow_runs wr
                    WHERE LOWER(TRIM(COALESCE(wr.status, ''))) IN ('running', 'failed')
                      AND EXISTS (
                        SELECT 1 FROM workflow_steps ws
                        WHERE ws.run_id = wr.id
                          AND COALESCE(ws.is_enabled, 1) = 1
                          AND LOWER(TRIM(COALESCE(ws.status, ''))) NOT IN ('completed', 'error', 'disabled')
                      )
                    LIMIT 1
                    """
                )
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def ensure_workflow_runner_running(engine) -> Tuple[bool, str]:
    """
    Ensures workflow runner process is running when any workflow has work left
    (status running, or failed with non-terminal enabled steps — runner will auto-resume).
    Returns: (started_or_running, message)
    """
    if str(os.getenv("DISABLE_WORKFLOW_AUTOSTART", "")).strip().lower() in {"1", "true", "yes", "y"}:
        return False, "workflow autostart disabled"

    if not _has_running_workflow(engine):
        return False, "no running workflows"

    repo_dir = Path(__file__).resolve().parent
    pidfile = repo_dir / ".workflow_runner.pid"

    pid = _read_pidfile(pidfile)
    if pid and _pid_is_running(pid):
        return True, f"pidfile alive (pid={pid})"

    runner_path = repo_dir / "workflow_runner.py"
    if not runner_path.exists():
        return False, "workflow_runner.py not found"

    cmd = [sys.executable, str(runner_path), "--quiet"]
    env = os.environ.copy()

    try:
        logs_dir = repo_dir / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        out_path = logs_dir / "workflow_runner.out.log"
        err_path = logs_dir / "workflow_runner.err.log"
        stdout_f = open(out_path, "ab", buffering=0)
        stderr_f = open(err_path, "ab", buffering=0)
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(repo_dir),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(repo_dir),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
        _write_pidfile(pidfile, int(p.pid))
        return True, f"spawned (pid={p.pid}) logs={logs_dir}"
    except Exception as ex:
        return False, f"spawn failed: {ex}"

