#!/usr/bin/env python3
"""
Start (or stop) the full local stack: background tracker + workflow runner + Streamlit dashboard.

Usage:
  python3 scripts/start_stack.py              # tracker + workflow in background, dashboard in foreground
  python3 scripts/start_stack.py --all-bg     # all three detached (prints PIDs and log paths)
  python3 scripts/start_stack.py --no-ui      # only tracker + workflow_runner + ai_log_fetcher
  python3 scripts/start_stack.py --status     # show PIDs / whether processes respond to signal 0
  python3 scripts/start_stack.py --stop       # stop services started by this script

Environment (optional):
  STREAMLIT_SERVER_PORT   — passed through to Streamlit (default 8501)
  SCAN_TRACKER_NAME     — tracker heartbeat name (default: default)
  TRACKER_INTERVAL_SEC  — tracker poll interval (default: 600)
  WORKFLOW_INTERVAL_SEC — workflow runner tick (default: 30)
  SCAN_LOG_FETCH_MINUTES — log analyzer fetch interval (default: 5)

Pidfiles (repo root): .stack.tracker.<name>.pid, .stack.workflow_runner.pid, .ai_log_fetcher.pid
Logs: logs/stack_tracker.*.log, logs/stack_workflow_runner.log, logs/ai_log_fetcher.out.log
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def logs_dir() -> Path:
    d = repo_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tracker_name() -> str:
    return str(os.getenv("SCAN_TRACKER_NAME", "default") or "default").strip() or "default"


def tracker_pidfile() -> Path:
    return repo_root() / f".stack.tracker.{tracker_name()}.pid"


def workflow_pidfile() -> Path:
    return repo_root() / ".stack.workflow_runner.pid"


def ai_log_pidfile() -> Path:
    return repo_root() / ".ai_log_fetcher.pid"


def dashboard_pidfile() -> Path:
    return repo_root() / ".stack.dashboard.pid"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name != "nt":
            os.kill(pid, 0)
            return True
        out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.STDOUT)
        return str(pid) in out.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False


def _read_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        return int(raw) if raw.isdigit() else None
    except Exception:
        return None


def _write_pid(path: Path, pid: int) -> None:
    try:
        path.write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass


def _open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "ab", buffering=0)


def start_tracker() -> Tuple[bool, str]:
    pf = tracker_pidfile()
    pid = _read_pid(pf)
    if pid and _pid_is_running(pid):
        return True, f"tracker already running (pid={pid})"

    py = sys.executable
    root = repo_root()
    script = root / "tracker.py"
    if not script.is_file():
        return False, f"missing {script}"

    interval = str(os.getenv("TRACKER_INTERVAL_SEC", "600") or "600")
    cmd = [py, str(script), "--quiet", "--interval-seconds", interval]
    env = os.environ.copy()
    env["SCAN_TRACKER_NAME"] = tracker_name()

    log = logs_dir() / f"stack_tracker.{tracker_name()}.log"
    err = logs_dir() / f"stack_tracker.{tracker_name()}.err.log"
    try:
        out_f = _open_log(log)
        err_f = _open_log(err)
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=out_f,
                stderr=err_f,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        _write_pid(pf, p.pid)
        return True, f"tracker started pid={p.pid} log={log}"
    except Exception as ex:
        return False, f"tracker start failed: {ex}"


def start_workflow_runner() -> Tuple[bool, str]:
    pf = workflow_pidfile()
    pid = _read_pid(pf)
    if pid and _pid_is_running(pid):
        return True, f"workflow_runner already running (pid={pid})"

    py = sys.executable
    root = repo_root()
    script = root / "workflow_runner.py"
    if not script.is_file():
        return False, f"missing {script}"

    interval = str(os.getenv("WORKFLOW_INTERVAL_SEC", "30") or "30")
    cmd = [py, str(script), "--quiet", "--interval-seconds", interval]

    log = logs_dir() / "stack_workflow_runner.log"
    err = logs_dir() / "stack_workflow_runner.err.log"
    try:
        out_f = _open_log(log)
        err_f = _open_log(err)
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        _write_pid(pf, p.pid)
        return True, f"workflow_runner started pid={p.pid} log={log}"
    except Exception as ex:
        return False, f"workflow_runner start failed: {ex}"


def start_ai_log_fetcher() -> Tuple[bool, str]:
    if str(os.getenv("DISABLE_LOG_ANALYZER_AUTOSTART", "")).strip().lower() in {"1", "true", "yes", "y"}:
        return False, "log analyzer disabled by disabled_log_analyzer_autostart=1"

    pf = ai_log_pidfile()
    pid = _read_pid(pf)
    if pid and _pid_is_running(pid):
        return True, f"ai_log_fetcher already running (pid={pid})"

    py = sys.executable
    root = repo_root()
    script = root / "ai_log_fetcher.py"
    if not script.is_file():
        return False, f"missing {script}"

    cmd = [py, str(script)]

    log = logs_dir() / "ai_log_fetcher.out.log"
    err = logs_dir() / "ai_log_fetcher.err.log"
    try:
        out_f = _open_log(log)
        err_f = _open_log(err)
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        _write_pid(pf, p.pid)
        return True, f"ai_log_fetcher started pid={p.pid} log={log}"
    except Exception as ex:
        return False, f"ai_log_fetcher start failed: {ex}"


def start_dashboard_background() -> Tuple[bool, str]:
    pf = dashboard_pidfile()
    pid = _read_pid(pf)
    if pid and _pid_is_running(pid):
        return True, f"dashboard already running (pid={pid})"

    py = sys.executable
    root = repo_root()
    dash = root / "dashboard.py"
    if not dash.is_file():
        return False, f"missing {dash}"

    cmd = [py, "-m", "streamlit", "run", str(dash), "--server.headless", "true"]
    log = logs_dir() / "stack_dashboard.log"
    err = logs_dir() / "stack_dashboard.err.log"
    try:
        out_f = _open_log(log)
        err_f = _open_log(err)
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=os.environ.copy(),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        _write_pid(pf, p.pid)
        port = os.getenv("STREAMLIT_SERVER_PORT", "8501")
        return True, f"dashboard started pid={p.pid} http://127.0.0.1:{port} log={log}"
    except Exception as ex:
        return False, f"dashboard start failed: {ex}"


def run_dashboard_foreground() -> int:
    root = repo_root()
    dash = root / "dashboard.py"
    if not dash.is_file():
        print(f"ERROR: missing {dash}", file=sys.stderr)
        return 1
    os.chdir(root)
    try:
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(dash)], env=os.environ.copy())
    except KeyboardInterrupt:
        return 0


def stop_one(label: str, pidfile: Path) -> str:
    pid = _read_pid(pidfile)
    if not pid:
        return f"{label}: no pidfile"
    if not _pid_is_running(pid):
        try:
            pidfile.unlink(missing_ok=True)
        except Exception:
            pass
        return f"{label}: stale pidfile cleared (pid {pid} not running)"
    try:
        if os.name != "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        time.sleep(0.5)
    except Exception as ex:
        return f"{label}: kill failed pid={pid}: {ex}"
    try:
        pidfile.unlink(missing_ok=True)
    except Exception:
        pass
    return f"{label}: sent SIGTERM to pid={pid}"


def cmd_stop() -> int:
    msgs = [
        stop_one("dashboard", dashboard_pidfile()),
        stop_one("ai_log_fetcher", ai_log_pidfile()),
        stop_one("workflow_runner", workflow_pidfile()),
        stop_one("tracker", tracker_pidfile()),
    ]
    print("\n".join(msgs))
    return 0


def cmd_status() -> int:
    root = repo_root()
    print(f"repo: {root}")
    for label, pf in (
        ("tracker", tracker_pidfile()),
        ("workflow_runner", workflow_pidfile()),
        ("ai_log_fetcher", ai_log_pidfile()),
        ("dashboard", dashboard_pidfile()),
    ):
        pid = _read_pid(pf)
        if not pid:
            print(f"{label}: not started (no {pf.name})")
            continue
        alive = _pid_is_running(pid)
        print(f"{label}: pid={pid} alive={alive}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Start tracker + workflow runner + Streamlit dashboard")
    ap.add_argument("--all-bg", action="store_true", help="Detach dashboard too (all services in background)")
    ap.add_argument("--no-ui", action="store_true", help="Only start tracker and workflow_runner")
    ap.add_argument("--no-tracker", action="store_true")
    ap.add_argument("--no-workflow", action="store_true")
    ap.add_argument("--no-ai-log", action="store_true", help="Do not start the AI log fetcher")
    ap.add_argument("--stop", action="store_true", help="Stop services started with this script")
    ap.add_argument("--status", action="store_true", help="Print pidfile / process status")
    args = ap.parse_args()

    if args.stop:
        return cmd_stop()
    if args.status:
        return cmd_status()

    ok_all = True
    if not args.no_tracker:
        ok, msg = start_tracker()
        print(msg)
        ok_all = ok_all and ok
    if not args.no_workflow:
        ok, msg = start_workflow_runner()
        print(msg)
        ok_all = ok_all and ok
    if not args.no_ai_log:
        ok, msg = start_ai_log_fetcher()
        if msg:  # Can be silent if disabled via env var intentionally
            print(msg)
        ok_all = ok_all and ok

    if args.no_ui:
        print("\nBackground services running. Start dashboard manually: streamlit run dashboard.py")
        return 0 if ok_all else 1

    if args.all_bg:
        ok, msg = start_dashboard_background()
        print(msg)
        return 0 if ok_all and ok else 1

    port = os.getenv("STREAMLIT_SERVER_PORT", "8501")
    print(f"\nStarting Streamlit in foreground → http://127.0.0.1:{port}")
    print("Stop dashboard: Ctrl+C  (tracker/workflow keep running; use --stop to kill them)\n")
    run_dashboard_foreground()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
