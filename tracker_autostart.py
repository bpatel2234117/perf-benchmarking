"""
Auto-start `tracker.py` from Streamlit process.

Goal: If user is running Streamlit, ensure the background tracker process is
started once on the same machine, without requiring manual commands.

Safety:
- Uses DB heartbeat + a pidfile to avoid spawning duplicates.
- Can be disabled via DISABLE_TRACKER_AUTOSTART=1
"""

from __future__ import annotations

import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import text


def _now_epoch() -> float:
    return time.time()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name != "nt":
            os.kill(pid, 0)
            return True
        # On Windows, check tasklist output.
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


def _get_heartbeat_age_minutes(engine, tracker_name: str) -> Optional[float]:
    """
    Returns minutes since last_finished_at_utc, or None if no heartbeat row.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT last_finished_at_utc
                    FROM tracker_heartbeat
                    WHERE name = :n
                    LIMIT 1
                """),
                {"n": tracker_name},
            ).fetchone()
        if not row or not row[0]:
            return None
        # row[0] is a datetime from DB driver. Treat naive timestamps as UTC.
        last = row[0]
        now_utc = datetime.now(timezone.utc)
        if getattr(last, "tzinfo", None) is None:
            last_utc = last.replace(tzinfo=timezone.utc)
        else:
            last_utc = last.astimezone(timezone.utc)
        age_sec = (now_utc - last_utc).total_seconds()
        return max(0.0, age_sec) / 60.0
    except Exception:
        return None


def ensure_tracker_running(engine, tracker_name: str = "default") -> Tuple[bool, str]:
    """
    Ensures tracker process is running (best-effort).
    Returns: (started_or_running, message)
    """
    if str(os.getenv("DISABLE_TRACKER_AUTOSTART", "")).strip() in {"1", "true", "yes", "y"}:
        return False, "autostart disabled"

    tracker_name = str(os.getenv("SCAN_TRACKER_NAME", tracker_name) or tracker_name).strip() or "default"

    repo_dir = Path(__file__).resolve().parent
    pidfile = repo_dir / f".tracker.{tracker_name}.pid"

    # If heartbeat is fresh, assume running.
    hb_age = _get_heartbeat_age_minutes(engine, tracker_name)
    if hb_age is not None and hb_age < 15:
        return True, f"heartbeat fresh ({hb_age:.1f} min)"

    # If pidfile exists and process seems alive, keep it.
    pid = _read_pidfile(pidfile)
    if pid and _pid_is_running(pid):
        return True, f"pidfile alive (pid={pid})"

    # Spawn tracker detached
    tracker_path = repo_dir / "tracker.py"
    if not tracker_path.exists():
        return False, "tracker.py not found"

    cmd = [sys.executable, str(tracker_path), "--quiet"]
    env = os.environ.copy()
    env["SCAN_TRACKER_NAME"] = tracker_name

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

