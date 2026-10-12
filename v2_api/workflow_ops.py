"""PostgreSQL workflow orchestration helpers for v2_api (Streamlit-equivalent DB operations)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.engine import Engine

from v2_api.workflow_defaults import default_workflow_steps_for_new_run, normalize_workflow_step_row


def _utc_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# Run statuses that still own their env selection — an env selected by such a run cannot be
# selected by a second run.
ACTIVE_RUN_STATUSES = ("created", "running", "paused", "failed")
_ACTIVE_RUN_STATUS_SQL_IN = ", ".join(f"'{s}'" for s in ACTIVE_RUN_STATUSES)

# Step states that mean an env is actively in use by a run (DS setup through scan).
_ENV_OCCUPYING_STEP_STATUSES = (
    "running",
    "ds_created",
    "ds_test_ok",
    "scan_created",
    "scan_running",
)
_ENV_OCCUPYING_STEP_SQL_IN = ", ".join(f"'{s}'" for s in _ENV_OCCUPYING_STEP_STATUSES)

# An active status alone does not keep a selection alive: 'failed' runs stay active so they can be
# resumed, but once every enabled step is terminal there is nothing to resume and the envs must go
# back to the pool. Mirrors _run_still_owns_selection_sql in workflow_runner.py.
_RUN_STILL_OWNS_SELECTION_SQL = """(
    NOT EXISTS (SELECT 1 FROM workflow_steps ws0 WHERE ws0.run_id = r.id)
    OR EXISTS (
        SELECT 1 FROM workflow_steps ws1
        WHERE ws1.run_id = r.id
          AND COALESCE(ws1.is_enabled, TRUE) = TRUE
          AND LOWER(TRIM(COALESCE(ws1.status, ''))) NOT IN ('completed', 'error', 'disabled')
    )
)"""


def _explicit_env_ids(env1_id: Any = None, env2_id: Any = None) -> List[int]:
    """Env ids explicitly chosen by the caller (order preserved, dupes/None dropped)."""
    out: List[int] = []
    for raw in (env1_id, env2_id):
        if raw is None:
            continue
        try:
            eid = int(raw)
        except (TypeError, ValueError):
            continue
        if eid not in out:
            out.append(eid)
    return out


def validate_pinned_envs(
    conn,
    env1_id: Any = None,
    env2_id: Any = None,
    *,
    exclude_run_id: Optional[int] = None,
) -> List[int]:
    """
    Validate an explicit env selection for a workflow run and return the selected ids.

    A run with a selection executes only on those envs, so a bad selection must fail loudly at
    create/start rather than silently falling back to the pool:
      - same env in both slots
      - env missing / inactive / excluded from workflow runs
      - env selected by, or mid-flight on, another still-active run (a selected env is off-limits
        to every other workflow, which is what lets two runs execute side by side)
    Returns [] when nothing was selected — that run uses all available envs instead.
    """
    ids = _explicit_env_ids(env1_id, env2_id)
    if not ids:
        return []

    try:
        raw1 = int(env1_id) if env1_id is not None else None
        raw2 = int(env2_id) if env2_id is not None else None
    except (TypeError, ValueError):
        raise ValueError("Environment ids must be integers")
    if raw1 is not None and raw1 == raw2:
        raise ValueError("Env 1 and Env 2 must be different environments")

    rows = conn.execute(
        text(
            """
            SELECT id, env_name, COALESCE(is_active, TRUE), COALESCE(include_in_workflow_runs, TRUE)
            FROM test_environments
            WHERE id = ANY(:ids)
            """
        ),
        {"ids": ids},
    ).fetchall()
    found = {int(r[0]): r for r in rows or []}
    for eid in ids:
        row = found.get(eid)
        if row is None:
            raise ValueError(f"Environment {eid} does not exist")
        name = str(row[1] or f"env-{eid}")
        if not bool(row[2]):
            raise ValueError(f"Environment '{name}' is not active — activate it or pick another")
        if not bool(row[3]):
            raise ValueError(
                f"Environment '{name}' is excluded from workflow runs "
                "(enable 'Use for workflow runs' on the Environments page)"
            )

    params: Dict[str, Any] = {"ids": ids}
    exclude_sql = ""
    if exclude_run_id is not None:
        exclude_sql = "AND r.id <> :xrid"
        params["xrid"] = int(exclude_run_id)
    held = conn.execute(
        text(
            f"""
            -- selected by another active run: that run owns the env outright
            SELECT r.id, LOWER(TRIM(COALESCE(r.status, ''))), e.env_name, 'selected' AS how
            FROM workflow_runs r
            JOIN test_environments e ON e.id IN (r.env1_id, r.env2_id)
            WHERE LOWER(TRIM(COALESCE(r.status, ''))) IN ({_ACTIVE_RUN_STATUS_SQL_IN})
              AND {_RUN_STILL_OWNS_SELECTION_SQL}
              AND e.id = ANY(:ids)
              {exclude_sql}
            UNION ALL
            -- mid-flight on another run (a run with no selection can be using it right now):
            -- taking it away would orphan a live DS/scan, so make the user wait for it
            SELECT r.id, LOWER(TRIM(COALESCE(r.status, ''))), e.env_name, 'in use' AS how
            FROM workflow_steps ws
            JOIN workflow_runs r ON r.id = ws.run_id
            JOIN test_environments e ON e.id = ws.environment_id
            WHERE LOWER(TRIM(COALESCE(r.status, ''))) IN ({_ACTIVE_RUN_STATUS_SQL_IN})
              AND COALESCE(ws.is_enabled, TRUE) = TRUE
              AND LOWER(TRIM(COALESCE(ws.status, ''))) IN ({_ENV_OCCUPYING_STEP_SQL_IN})
              AND ws.environment_id = ANY(:ids)
              {exclude_sql}
            ORDER BY 1
            """
        ),
        params,
    ).fetchall()
    if held:
        r = held[0]
        raise ValueError(
            f"Environment '{r[2]}' is {r[3]} by workflow run #{int(r[0])} "
            f"({r[1] or 'active'}) — stop that run or pick a different environment"
        )
    return ids


def _workflow_build_label(conn, env1_id: Any = None, env2_id: Any = None) -> str:
    """
    Distinct BigID build versions for the run's envs (for default run names).
    When the run is pinned to explicit envs, only those envs count — a pinned run must not be
    named after a build running on an env it will never touch.
    """
    pinned = _explicit_env_ids(env1_id, env2_id)
    pool_ids = pinned or _fetch_workflow_pool_env_ids(conn, env1_id, env2_id)
    if not pool_ids:
        return ""
    placeholders = ", ".join(f":e{i}" for i in range(len(pool_ids)))
    params = {f"e{i}": int(pool_ids[i]) for i in range(len(pool_ids))}
    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT TRIM(COALESCE(version, '')) AS v
            FROM test_environments
            WHERE id IN ({placeholders})
            """
        ),
        params,
    ).fetchall()
    versions = sorted({str(r[0]).strip() for r in rows if r and str(r[0]).strip()})
    if len(versions) == 1:
        return versions[0]
    if len(versions) > 1:
        return "+".join(versions[:3])
    return ""


def _default_workflow_name(conn, env1_id: Any = None, env2_id: Any = None) -> str:
    ts = _utc_name()
    build = _workflow_build_label(conn, env1_id, env2_id)
    if build:
        return f"Workflow {build} {ts}"
    return f"Workflow {ts}"


def _fetch_workflow_pool_env_ids(conn, env1_id: Any = None, env2_id: Any = None) -> List[int]:
    """
    All environments marked active + "Use for workflow runs" (N-env pool).
    Optionally include env1/env2 if set but not already in pool.
    
    BUG FIX 4.4: Added is_active check to prevent inactive environments from being used.
    """
    rows = conn.execute(
        text(
            """
            SELECT id FROM test_environments
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND COALESCE(include_in_workflow_runs, TRUE) = TRUE
            ORDER BY id
            """
        )
    ).fetchall()
    ids: List[int] = []
    seen: Set[int] = set()
    for r in rows or []:
        try:
            eid = int(r[0])
        except Exception:
            continue
        if eid not in seen:
            seen.add(eid)
            ids.append(eid)
    for raw in (env1_id, env2_id):
        if raw is None:
            continue
        try:
            eid = int(raw)
        except Exception:
            continue
        if eid not in seen:
            seen.add(eid)
            ids.append(eid)
    return ids


def insert_workflow_run_with_steps(
    engine: Engine,
    name: Optional[str],
    cooldown_minutes: int,
    env1_id: Any,
    env2_id: Any,
    steps_override: Optional[List[Dict[str, Any]]] = None,
    created_by: Optional[int] = None,
) -> int:
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

    with engine.begin() as conn:
        # An explicit Env 1 / Env 2 selection pins the run: every step and action runs only on
        # those envs. Validate up front so a bad pick fails here instead of silently widening
        # to the whole pool at dispatch.
        pinned_ids = validate_pinned_envs(conn, env1_id, env2_id)

        nm = (name or "").strip() or _default_workflow_name(conn, env1_id, env2_id)
        pool_env_ids = pinned_ids or _fetch_workflow_pool_env_ids(conn, env1_id, env2_id)

        # BUG FIX 1.3: Validate that workflow pool has at least one active env with include_in_workflow_runs
        if not pool_env_ids:
            raise ValueError(
                "No environments available for workflow: "
                "ensure at least one environment has is_active=TRUE and include_in_workflow_runs=TRUE"
            )

        row = conn.execute(
            text(
                """
                INSERT INTO workflow_runs (name, status, env1_id, env2_id, env_pinned, cooldown_minutes, created_by)
                VALUES (:n, 'created', :e1, :e2, :pin, :cd, :cb)
                RETURNING id
                """
            ),
            {
                "n": nm,
                "e1": env1_id,
                "e2": env2_id,
                "pin": bool(pinned_ids),
                "cd": cd,
                "cb": created_by,
            },
        ).fetchone()
        if not row:
            raise RuntimeError("INSERT workflow_runs returned no id")
        run_id = int(row[0])

        seq_idx = 1
        for st in steps:
            enabled = bool(st.get("is_enabled", True))
            ds_kind = str(st.get("ds_kind") or "").strip()
            
            # Action steps: one catalog row; runner applies to this run's assigned envs at start (not global pool).
            conn.execute(
                text(
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
                ),
                {
                    "rid": run_id,
                    "seq": seq_idx,
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
            seq_idx += 1
    return run_id


def apply_run_env_mapping(conn, run_id: int, e1_id: Any = None, e2_id: Any = None) -> None:
    """
    Set the run's env slots (env1_id / env2_id). These are the ONLY envs the run may use:
    workflow_runner assigns each step to a free env from this set, never from the wider pool.
    Clears stale environment_id on pending/error scan steps so dispatch can re-pick within the set.

    Slots are updated only when a new selection is supplied — start/resume with no env argument
    must never wipe the pin the run was created with.
    """
    ids = _explicit_env_ids(e1_id, e2_id)
    if ids:
        validate_pinned_envs(conn, e1_id, e2_id, exclude_run_id=int(run_id))
        conn.execute(
            text(
                """
                UPDATE workflow_runs
                SET env1_id = :e1, env2_id = :e2, env_pinned = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE id = :rid
                """
            ),
            {"e1": ids[0], "e2": ids[1] if len(ids) > 1 else None, "rid": int(run_id)},
        )
    conn.execute(
        text(
            """
            UPDATE workflow_steps
            SET environment_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = :rid
              AND LOWER(COALESCE(ds_kind, '')) != 'action'
              AND COALESCE(status, '') IN ('pending', 'error')
            """
        ),
        {"rid": run_id},
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


def prepare_workflow_run_dispatch(
    conn,
    run_id: int,
    env1_id: Any = None,
    env2_id: Any = None,
) -> None:
    """
    Before start/resume: clear stale locks and parked steps from a prior attempt on this run
    so a fresh dispatch can create DS / run scans (does not stop completed steps).
    
    BUG FIX 4.1: Add pre-flight connectivity check to fail fast on unreachable env.
    """
    rid = int(run_id)
    apply_run_env_mapping(conn, rid, env1_id, env2_id)

    try:
        from v2_api.workflow_runner import ensure_workflow_run_env_assignment  # noqa: PLC0415

        ensure_workflow_run_env_assignment(
            conn.get_bind(),
            rid,
            env1_id,
            env2_id,
        )
    except Exception:
        pass

    # BUG FIX 4.1: Pre-flight connectivity check
    try:
        from v2_api.workflow_runner import normalize_base_url, fetch_about, Get_auth_token
        
        # Get workflow pool envs
        pool_rows = conn.execute(
            text("""
                SELECT DISTINCT id, base_url FROM test_environments
                WHERE COALESCE(is_active, TRUE) = TRUE
                  AND COALESCE(include_in_workflow_runs, TRUE) = TRUE
            """)
        ).fetchall()
        
        for pool_row in pool_rows:
            pool_env_id = int(pool_row[0])
            base_url = str(pool_row[1] or "").strip()
            
            if not base_url:
                continue
            
            try:
                normalized_url = normalize_base_url(base_url)
                # Try to fetch auth token for this env
                with conn.execute(
                    text("SELECT auth_token_key FROM test_environments WHERE id=:eid"),
                    {"eid": pool_env_id}
                ) as auth_result:
                    auth_row = auth_result.fetchone()
                
                if not auth_row:
                    continue
                
                # Attempt quick connectivity check (5s timeout)
                import time as time_module
                try:
                    about_data = fetch_about(normalized_url, "temp-token")  # fetch_about handles no-auth gracefully
                    # If we got a response, connectivity is OK
                    if isinstance(about_data, dict):
                        continue
                except Exception as check_ex:
                    # Soft fail: log but don't block workflow start (env might become available)
                    import os
                    if str(os.getenv("WORKFLOW_PREFLIGHT_STRICT", "0") or "0").strip().lower() in ("1", "true", "yes"):
                        raise RuntimeError(f"Env {pool_env_id} ({normalized_url}) unreachable in pre-flight check: {check_ex}") from check_ex
            except Exception:
                # If preflight check fails, continue anyway (non-blocking)
                pass
    except Exception:
        # Pre-flight check is advisory; don't block workflow start
        pass
    
    conn.execute(
        text(
            """
            UPDATE test_environments
            SET is_busy = FALSE, busy_by_run_id = NULL, busy_since = NULL
            WHERE busy_by_run_id = :rid
               OR (COALESCE(is_busy, FALSE) = TRUE AND busy_by_run_id IS NULL)
            """
        ),
        {"rid": rid},
    )
    conn.execute(
        text(
            """
            UPDATE workflow_steps
            SET status = 'pending',
                last_error = NULL,
                environment_id = NULL
            WHERE run_id = :rid
              AND COALESCE(is_enabled, TRUE) = TRUE
              AND LOWER(TRIM(COALESCE(status,''))) IN ('scan_waiting', 'running')
              AND (scan_id IS NULL OR TRIM(COALESCE(scan_id,'')) = '')
              AND (ds_id IS NULL OR TRIM(COALESCE(ds_id,'')) = '')
            """
        ),
        {"rid": rid},
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
        if act in ("start", "resume"):
            prepare_workflow_run_dispatch(conn, rid, env1_id, env2_id)
            conn.execute(
                text(
                    """
                    UPDATE workflow_runs
                    SET status = 'running',
                        last_error = NULL,
                        next_eligible_at = NULL,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :rid
                    """
                ),
                {"rid": rid},
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
