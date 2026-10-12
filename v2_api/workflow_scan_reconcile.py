"""
Reconcile workflow_steps stuck in scan_running when BigID / scan_parents already show terminal.
Used by workflow_runner (every tick) and tracker (on terminal upsert).
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _close_scan_progress(engine: Engine, env_id: Any, scan_id: str, terminal_status: str, *, logger: Any = None) -> None:
    """
    The moment a scan is known to be non-in-progress, write its closing scan_progress row.

    The tracker also does this, but only for scans it can reach through the insights API and
    an env-attributed scan_parents row. Doing it here as well means a scan that finishes while
    the tracker is down, or whose insights BigID already purged, still stops looking
    'In Progress' forever in scan_progress. No-ops when a terminal row already exists.
    """
    sid = str(scan_id or "").strip()
    if not sid or not str(terminal_status or "").strip():
        return
    try:
        try:
            from tracker import ensure_terminal_progress_row  # noqa: PLC0415
        except Exception:
            from v2_api.tracker import ensure_terminal_progress_row  # noqa: PLC0415
        ensure_terminal_progress_row(engine, env_id, sid, "", str(terminal_status))
    except Exception as ex:
        if logger:
            try:
                logger.debug("workflow_scan_reconcile: close scan_progress failed scan=%s: %s", sid, ex)
            except Exception:
                pass


def complete_workflow_steps_for_scan(
    engine: Engine,
    scan_id: str,
    terminal_status: str,
    *,
    logger: Any = None,
) -> int:
    """Mark all scan_running workflow steps with this scan_id terminal. Returns rows updated."""
    from v2_api.workflow_runner import (  # noqa: PLC0415
        _complete_step_from_terminal_scan,
        _is_terminal_state,
        _release_stale_env_locks_for_run,
        _sql_bool_true,
    )

    sid = str(scan_id or "").strip()
    stv = str(terminal_status or "").strip()
    if not sid or not stv or not _is_terminal_state(stv):
        return 0
    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT ws.id, ws.run_id, ws.seq, ws.environment_id
                    FROM workflow_steps ws
                    JOIN workflow_runs wr ON wr.id = ws.run_id
                    WHERE ws.scan_id = :sid
                      AND LOWER(TRIM(COALESCE(ws.status,''))) = 'scan_running'
                      AND COALESCE(ws.is_enabled, {bool_true}) = {bool_true}
                      AND LOWER(TRIM(COALESCE(wr.status,''))) IN ('running', 'failed')
                    """
                ),
                {"sid": sid},
            ).fetchall()
    except Exception:
        return 0
    done = 0
    run_ids: set[int] = set()
    for row in rows or []:
        step_id, run_id, seq = int(row[0]), int(row[1]), int(row[2])
        _close_scan_progress(engine, row[3], sid, stv, logger=logger)
        if _complete_step_from_terminal_scan(
            engine,
            run_id=run_id,
            step_id=step_id,
            seq=seq,
            scan_id=sid,
            terminal_status=stv,
        ):
            done += 1
            run_ids.add(run_id)
            if logger:
                try:
                    logger.info(
                        "workflow_scan_reconcile: completed step_id=%s run_id=%s seq=%s scan=%s status=%s",
                        step_id,
                        run_id,
                        seq,
                        sid,
                        stv,
                    )
                except Exception:
                    pass
    for rid in run_ids:
        try:
            _release_stale_env_locks_for_run(engine, rid)
        except Exception:
            pass
    return done


def reconcile_all_scan_running_steps(engine: Engine, *, logger: Any = None) -> int:
    """
    Every tick: for each scan_running step with scan_id, resolve terminal from scan_parents + BigID API.
    Does not depend on updated_at / stale timers (avoids liveness touch blocking completion).
    """
    from v2_api.workflow_runner import (  # noqa: PLC0415
        _fetch_scan_status_for_step,
        _is_terminal_state,
        _complete_step_from_terminal_scan,
        _release_stale_env_locks_for_run,
        _reconcile_running_run_if_idle,
        _sql_bool_true,
        _now_utc,
    )

    bool_true = _sql_bool_true(engine)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT ws.id, ws.run_id, ws.seq, ws.environment_id, ws.scan_id, sp.scan_status
                    FROM workflow_steps ws
                    JOIN workflow_runs wr ON wr.id = ws.run_id
                    LEFT JOIN scan_parents sp ON sp.scan_id = ws.scan_id
                    WHERE LOWER(TRIM(COALESCE(wr.status,''))) IN ('running', 'failed')
                      AND LOWER(TRIM(COALESCE(ws.status,''))) = 'scan_running'
                      AND ws.scan_id IS NOT NULL AND TRIM(COALESCE(ws.scan_id,'')) <> ''
                      AND COALESCE(ws.is_enabled, {bool_true}) = {bool_true}
                    ORDER BY ws.run_id, ws.seq
                    """
                ),
            ).fetchall()
    except Exception as ex:
        if logger:
            try:
                logger.error("reconcile_all_scan_running_steps query failed: %s", ex)
            except Exception:
                pass
        return 0

    completed = 0
    run_ids: set[int] = set()
    now = _now_utc()
    for row in rows or []:
        step_id = int(row[0])
        run_id = int(row[1])
        seq = int(row[2])
        env_id = row[3]
        sid = str(row[4] or "").strip()
        db_st = str(row[5] or "").strip()
        resolved = db_st
        if not resolved or not _is_terminal_state(resolved):
            resolved = _fetch_scan_status_for_step(engine, env_id, sid, run_id=run_id) or ""
        if not resolved or not _is_terminal_state(resolved):
            continue
        # Scan is no longer in progress → close it out in scan_progress before touching the step.
        _close_scan_progress(engine, env_id, sid, resolved, logger=logger)
        if _complete_step_from_terminal_scan(
            engine,
            run_id=run_id,
            step_id=step_id,
            seq=seq,
            scan_id=sid,
            terminal_status=resolved,
        ):
            completed += 1
            run_ids.add(run_id)
    for rid in run_ids:
        try:
            _release_stale_env_locks_for_run(engine, rid)
            _reconcile_running_run_if_idle(engine, rid, now)
        except Exception:
            pass
    if completed and logger:
        try:
            logger.info("reconcile_all_scan_running_steps: completed %s step(s)", completed)
        except Exception:
            pass
    return completed
