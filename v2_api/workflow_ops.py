"""PostgreSQL workflow orchestration helpers for v2_api (Streamlit-equivalent DB operations)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from v2_api.workflow_defaults import default_workflow_steps_for_new_run, normalize_workflow_step_row


def _utc_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def insert_workflow_run_with_steps(
    engine: Engine,
    name: Optional[str],
    cooldown_minutes: int,
    env1_id: Any,
    env2_id: Any,
    steps_override: Optional[List[Dict[str, Any]]] = None,
    created_by: Optional[int] = None,
) -> int:
    nm = (name or "").strip() or f"workflow {_utc_name()}"
    try:
        cd = max(1, min(180, int(cooldown_minutes)))
    except Exception:
        cd = 10

    if steps_override is not None:
        if len(steps_override) < 1:
            raise ValueError("steps must contain at least one row")
        steps = [normalize_workflow_step_row(dict(x)) for x in steps_override]
        if not any(s.get("is_enabled", True) for s in steps):
            raise ValueError("At least one workflow step must be enabled")
    else:
        steps = default_workflow_steps_for_new_run()

    ins_step = text(
        """
        INSERT INTO workflow_steps (
            run_id, seq, environment_id,
            ds_kind, ds_name, scan_kind, file_set_type,
            is_enabled,
            is_acl, is_ocr, is_structured_ff, is_ols, is_lite,
            feature_flags, notes, status
        ) VALUES (
            :rid, :seq, NULL,
            :dk, :dn, :sk, :fs,
            :en,
            :acl, :ocr, :sff, :ols, :islite,
            :ff, :notes, :st
        )
        """
    )

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO workflow_runs (name, status, env1_id, env2_id, cooldown_minutes, created_by)
                VALUES (:n, 'created', :e1, :e2, :cd, :cb)
                RETURNING id
                """
            ),
            {"n": nm, "e1": env1_id, "e2": env2_id, "cd": cd, "cb": created_by},
        ).fetchone()
        if not row:
            raise RuntimeError("INSERT workflow_runs returned no id")
        run_id = int(row[0])

        for i, st in enumerate(steps, start=1):
            enabled = bool(st.get("is_enabled", True))
            conn.execute(
                ins_step,
                {
                    "rid": run_id,
                    "seq": i,
                    "dk": st.get("ds_kind"),
                    "dn": st.get("ds_name"),
                    "sk": st.get("scan_kind"),
                    "fs": st.get("file_set_type"),
                    "en": enabled,
                    "acl": bool(st.get("is_acl")),
                    "ocr": bool(st.get("is_ocr")),
                    "sff": bool(st.get("is_structured_ff")),
                    "ols": bool(st.get("is_ols")),
                    "islite": bool(st.get("is_lite")),
                    "ff": st.get("feature_flags"),
                    "notes": st.get("notes"),
                    "st": "pending" if enabled else "disabled",
                },
            )
    return run_id


def apply_run_env_mapping(conn, run_id: int, e1_id: int, e2_id: int) -> None:
    conn.execute(
        text("UPDATE workflow_runs SET env1_id=:e1, env2_id=:e2, updated_at=CURRENT_TIMESTAMP WHERE id=:rid"),
        {"e1": e1_id, "e2": e2_id, "rid": run_id},
    )
    conn.execute(
        text(
            """
            UPDATE workflow_steps
            SET environment_id = CASE WHEN (seq % 2) = 1 THEN :e1 ELSE :e2 END,
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = :rid AND COALESCE(status, '') NOT IN ('scan_running', 'completed', 'disabled')
            """
        ),
        {"e1": e1_id, "e2": e2_id, "rid": run_id},
    )


def release_workflow_env_slots(conn, run_id: int) -> None:
    conn.execute(
        text(
            """
            UPDATE test_environments
            SET is_busy = FALSE, busy_by_run_id = NULL, busy_since = NULL
            WHERE busy_by_run_id = :rid
            """
        ),
        {"rid": run_id},
    )
    conn.execute(
        text(
            """
            UPDATE workflow_steps
            SET status = 'stopped'
            WHERE run_id = :rid
              AND status NOT IN ('completed', 'error', 'disabled')
            """
        ),
        {"rid": run_id},
    )


def workflow_control(
    engine: Engine,
    run_id: int,
    action: str,
    env1_id: Optional[int] = None,
    env2_id: Optional[int] = None,
) -> Dict[str, Any]:
    """start | pause | resume | stop — mirrors Streamlit query-param behavior (Postgres)."""
    act = str(action or "").strip().lower()
    rid = int(run_id)

    with engine.begin() as conn:
        if act == "start":
            if env1_id is not None or env2_id is not None:
                apply_run_env_mapping(conn, rid, env1_id, env2_id)
            conn.execute(
                text(
                    """
                    UPDATE workflow_runs
                    SET status = 'running',
                        last_error = COALESCE(:e, last_error),
                        next_eligible_at = NULL,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :rid
                    """
                ),
                {"e": None, "rid": rid},
            )
            return {"ok": True, "action": act}

        if act == "resume":
            if env1_id is not None or env2_id is not None:
                apply_run_env_mapping(conn, rid, env1_id, env2_id)
            conn.execute(
                text(
                    """
                    UPDATE workflow_runs
                    SET status = 'running',
                        last_error = COALESCE(:e, last_error),
                        next_eligible_at = NULL,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :rid
                    """
                ),
                {"e": None, "rid": rid},
            )
            return {"ok": True, "action": act}

        if act == "pause":
            conn.execute(
                text(
                    """
                    UPDATE workflow_runs
                    SET status = 'paused', last_error = COALESCE(:e, last_error), updated_at = CURRENT_TIMESTAMP
                    WHERE id = :rid
                    """
                ),
                {"e": None, "rid": rid},
            )
            release_workflow_env_slots(conn, rid)
            return {"ok": True, "action": act}

        if act == "stop":
            conn.execute(
                text(
                    """
                    UPDATE workflow_runs
                    SET status = 'stopped', last_error = COALESCE(:e, last_error), updated_at = CURRENT_TIMESTAMP,
                        finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                    WHERE id = :rid
                    """
                ),
                {"e": None, "rid": rid},
            )
            # Clears env locks + stops non-terminal steps (same pattern as dashboard release_workflow_run_envs)
            release_workflow_env_slots(conn, rid)
            return {"ok": True, "action": act}

        raise ValueError(f"Unknown action: {action}")
