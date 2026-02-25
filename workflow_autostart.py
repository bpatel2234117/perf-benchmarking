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
                text("SELECT id FROM workflow_runs WHERE status = 'running' ORDER BY id ASC LIMIT 1")
            ).fetchone()
        return bool(row and row[0] is not None)
    except Exception:
        return False


def ensure_workflow_runner_running(engine) -> Tuple[bool, str]:
    """
    Ensures workflow runner process is running if any workflow_runs.status='running'.
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
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(repo_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(repo_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        _write_pidfile(pidfile, int(p.pid))
        return True, f"spawned (pid={p.pid})"
    except Exception as ex:
        return False, f"spawn failed: {ex}"

