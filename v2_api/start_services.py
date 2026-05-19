#!/usr/bin/env python3
"""
Start v2_api background services (PostgreSQL-based) for React UI.

Usage:
    python3 v2_api/start_services.py              # Start all services
    python3 v2_api/start_services.py --stop       # Stop all services
    python3 v2_api/start_services.py --status     # Check status
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# PID file locations
PID_DIR = Path(__file__).parent
TRACKER_PID = PID_DIR / ".v2_tracker.pid"
WORKFLOW_PID = PID_DIR / ".v2_workflow_runner.pid"
LOGFETCH_PID = PID_DIR / ".v2_ai_log_fetcher.pid"

def read_pid(pid_file: Path) -> int | None:
    """Read PID from file, return None if not found or invalid."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None

def is_running(pid: int | None) -> bool:
    """Check if process is running."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def stop_process(name: str, pid_file: Path) -> bool:
    """Stop a process and remove its PID file."""
    pid = read_pid(pid_file)
    if pid is None:
        print(f"  {name}: no PID file")
        return False
    
    if not is_running(pid):
        print(f"  {name}: not running (stale PID {pid})")
        pid_file.unlink(missing_ok=True)
        return False
    
    print(f"  {name}: stopping PID {pid}...")
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 5 seconds for graceful shutdown
        for _ in range(50):
            if not is_running(pid):
                break
            time.sleep(0.1)
        
        if is_running(pid):
            print(f"  {name}: force killing...")
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        
        pid_file.unlink(missing_ok=True)
        print(f"  {name}: stopped")
        return True
    except Exception as e:
        print(f"  {name}: error stopping: {e}")
        return False

def start_process(name: str, script: str, pid_file: Path, args: list[str] | None = None) -> bool:
    """Start a background process."""
    pid = read_pid(pid_file)
    if is_running(pid):
        print(f"  {name}: already running (PID {pid})")
        return True
    
    cmd = [sys.executable, script]
    if args:
        cmd.extend(args)
    
    log_file = PID_DIR / f"{script.replace('.py', '')}.log"
    print(f"  {name}: starting... (log: {log_file.name})")
    
    try:
        with open(log_file, "a") as log:
            log.write(f"\n{'='*60}\n")
            log.write(f"Starting {name} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.write(f"Command: {' '.join(cmd)}\n")
            log.write(f"{'='*60}\n\n")
            log.flush()
            
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=PID_DIR,
                start_new_session=True,
            )
        
        pid_file.write_text(str(proc.pid))
        time.sleep(1)
        
        if is_running(proc.pid):
            print(f"  {name}: started (PID {proc.pid})")
            return True
        else:
            print(f"  {name}: failed to start (check {log_file.name})")
            return False
    except Exception as e:
        print(f"  {name}: error starting: {e}")
        return False

def show_status():
    """Show status of all services."""
    print("\nv2_api Services Status:")
    print("-" * 60)
    
    services = [
        ("Tracker", TRACKER_PID),
        ("Workflow Runner", WORKFLOW_PID),
        ("AI Log Fetcher", LOGFETCH_PID),
    ]
    
    for name, pid_file in services:
        pid = read_pid(pid_file)
        if is_running(pid):
            print(f"  {name:20} ✓ running (PID {pid})")
        else:
            print(f"  {name:20} ✗ not running")
    print()

def main():
    parser = argparse.ArgumentParser(description="Manage v2_api background services")
    parser.add_argument("--stop", action="store_true", help="Stop all services")
    parser.add_argument("--status", action="store_true", help="Show service status")
    parser.add_argument("--no-tracker", action="store_true", help="Don't start tracker")
    parser.add_argument("--no-workflow", action="store_true", help="Don't start workflow runner")
    parser.add_argument("--no-logfetch", action="store_true", help="Don't start log fetcher")
    args = parser.parse_args()
    
    if args.status:
        show_status()
        return 0
    
    if args.stop:
        print("\nStopping v2_api services...")
        stop_process("Tracker", TRACKER_PID)
        stop_process("Workflow Runner", WORKFLOW_PID)
        stop_process("AI Log Fetcher", LOGFETCH_PID)
        print()
        return 0
    
    # Start services
    print("\nStarting v2_api services (PostgreSQL)...")
    print("=" * 60)
    
    success = True
    
    if not args.no_tracker:
        if not start_process("Tracker", "tracker.py", TRACKER_PID, ["--interval-seconds", "30"]):
            success = False
    
    if not args.no_workflow:
        if not start_process("Workflow Runner", "workflow_runner.py", WORKFLOW_PID, ["--interval-seconds", "30"]):
            success = False
    
    if not args.no_logfetch:
        if not start_process("AI Log Fetcher", "ai_log_fetcher.py", LOGFETCH_PID):
            success = False
    
    print("=" * 60)
    show_status()
    
    if success:
        print("✓ All services started successfully")
        print("\nTo stop services: python3 v2_api/start_services.py --stop")
        print("To check status:  python3 v2_api/start_services.py --status")
        return 0
    else:
        print("✗ Some services failed to start (check logs)")
        return 1

if __name__ == "__main__":
    sys.exit(main())
