#!/usr/bin/env python3
"""
One-way migration: workflow_runs + workflow_steps (+ workflow_templates) from legacy MySQL
into PostgreSQL. The v2 API stays Postgres-only; this script uses MySQL only as a **source**
for a manual or scheduled copy.

Connection resolution (override with env vars as needed):

  WORKFLOW_MIGRATE_MYSQL_URI   e.g. mysql+pymysql://user:pass@host:3306/dbname
  WORKFLOW_MIGRATE_POSTGRES_URI  e.g. postgresql+psycopg2://...  (defaults: WORKFLOW_DB_URI, then v2_api.db_config_pg)

Examples:

  python3 scripts/migrate_workflow_mysql_to_postgres.py --dry-run
  python3 scripts/migrate_workflow_mysql_to_postgres.py
  python3 scripts/migrate_workflow_mysql_to_postgres.py --postgres-must-be-empty
"""
from __future__ import annotations

import argparse
import os
import sys

# Repo root on path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine, text  # noqa: E402


def _mysql_source_url() -> str:
    u = (os.getenv("WORKFLOW_MIGRATE_MYSQL_URI") or "").strip()
    if u:
        return u
    try:
        import db_config as dc  # noqa: PLC0415

        cs = str(getattr(dc, "DB_CONNECTION_STRING", "") or "").strip()
        low = cs.lower()
        if cs and ("mysql" in low or "pymysql" in low or "mariadb" in low):
            return cs
    except Exception:
        pass
    raise SystemExit(
        "Set WORKFLOW_MIGRATE_MYSQL_URI to a mysql+pymysql://... URL, or ensure repo db_config.DB_CONNECTION_STRING is MySQL."
    )


def _postgres_target_url() -> str:
    for key in ("WORKFLOW_MIGRATE_POSTGRES_URI", "WORKFLOW_DB_URI"):
        u = (os.getenv(key) or "").strip()
        if u:
            return u
    from v2_api.db_config_pg import DB_CONNECTION_STRING  # noqa: PLC0415

    return DB_CONNECTION_STRING


def _ensure_workflow_ddl(pg_conn) -> None:
    """Create workflow tables on Postgres if missing (subset of v2_api.db_config_pg)."""
    for stmt in (
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NULL,
            status VARCHAR(32) NULL,
            env1_id INT NULL,
            env2_id INT NULL,
            cooldown_minutes INT DEFAULT 15,
            next_eligible_at TIMESTAMP NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scan_template_id VARCHAR(128) NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id SERIAL PRIMARY KEY,
            run_id INT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            seq INT NOT NULL,
            environment_id INT NULL,
            ds_kind VARCHAR(64) NULL,
            ds_name VARCHAR(500) NULL,
            scan_kind VARCHAR(64) NULL,
            file_set_type VARCHAR(150) NULL,
            is_enabled BOOLEAN DEFAULT TRUE,
            is_acl BOOLEAN DEFAULT FALSE,
            is_ocr BOOLEAN DEFAULT FALSE,
            is_structured_ff BOOLEAN DEFAULT FALSE,
            is_ols BOOLEAN DEFAULT FALSE,
            is_lite BOOLEAN DEFAULT FALSE,
            feature_flags VARCHAR(500) NULL,
            notes VARCHAR(1000) NULL,
            ds_id VARCHAR(128) NULL,
            scan_profile_id VARCHAR(128) NULL,
            scan_profile_name VARCHAR(500) NULL,
            build_version VARCHAR(50) NULL,
            build_prefix VARCHAR(20) NULL,
            scan_id VARCHAR(128) NULL,
            status VARCHAR(32) NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, seq)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_templates (
            k VARCHAR(200) PRIMARY KEY,
            template_kind VARCHAR(32) NULL,
            payload_json TEXT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ):
        pg_conn.execute(text(stmt))


def _bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return bool(int(v)) if str(v).isdigit() else bool(v)


def main() -> None:
    ap = argparse.ArgumentParser(description="Copy workflow tables from MySQL to PostgreSQL (one-time / merge).")
    ap.add_argument("--dry-run", action="store_true", help="Print counts and planned actions only.")
    ap.add_argument(
        "--postgres-must-be-empty",
        action="store_true",
        help="Abort unless PostgreSQL has zero workflow_runs (use when you want migrate-only-when-empty).",
    )
    args = ap.parse_args()

    mysql_url = _mysql_source_url()
    pg_url = _postgres_target_url()

    my_eng = create_engine(mysql_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    pg_eng = create_engine(pg_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})

    run_select = """
        SELECT id, name, status, env1_id, env2_id, cooldown_minutes, next_eligible_at, last_error,
               started_at, finished_at, created_at, updated_at, scan_template_id
        FROM workflow_runs ORDER BY id
    """
    step_select = """
        SELECT id, run_id, seq, environment_id, ds_kind, ds_name, scan_kind, file_set_type,
               is_enabled, is_acl, is_ocr, is_structured_ff, is_ols, is_lite,
               feature_flags, notes, ds_id, scan_profile_id, scan_profile_name,
               build_version, build_prefix, scan_id, status, last_error,
               started_at, finished_at, created_at, updated_at
        FROM workflow_steps ORDER BY run_id, seq
    """
    tpl_select = """
        SELECT k, template_kind, payload_json, updated_at FROM workflow_templates ORDER BY k
    """

    with my_eng.connect() as mc:
        m_runs = mc.execute(text(run_select)).mappings().all()
        m_steps = mc.execute(text(step_select)).mappings().all()
        try:
            m_tpl = mc.execute(text(tpl_select)).mappings().all()
        except Exception:
            m_tpl = []

    with pg_eng.connect() as pc:
        try:
            n_pg_runs_chk = pc.execute(text("SELECT COUNT(*) FROM workflow_runs")).scalar() or 0
        except Exception:
            n_pg_runs_chk = 0
        if args.postgres_must_be_empty and int(n_pg_runs_chk) > 0:
            print(f"PostgreSQL already has {n_pg_runs_chk} workflow_runs; aborting (--postgres-must-be-empty).", file=sys.stderr)
            sys.exit(2)

    print(f"MySQL:  {len(m_runs)} workflow_runs, {len(m_steps)} workflow_steps, {len(m_tpl)} workflow_templates")
    print(f"Target: {pg_url.split('@')[-1] if '@' in pg_url else pg_url}")

    with pg_eng.connect() as pc:
        try:
            n_pg_r = pc.execute(text("SELECT COUNT(*) FROM workflow_runs")).scalar() or 0
            n_pg_s = pc.execute(text("SELECT COUNT(*) FROM workflow_steps")).scalar() or 0
        except Exception:
            n_pg_r, n_pg_s = 0, 0
    print(f"Postgres now: {n_pg_r} workflow_runs, {n_pg_s} workflow_steps (0 if tables missing until first migrate).")

    if args.dry_run:
        print("(dry-run) No writes. Re-run without --dry-run to merge-copy missing rows from MySQL into Postgres.")
        return

    ins_run = text(
        """
        INSERT INTO workflow_runs (
            id, name, status, env1_id, env2_id, cooldown_minutes, next_eligible_at, last_error,
            started_at, finished_at, created_at, updated_at, scan_template_id
        ) VALUES (
            :id, :name, :status, :env1_id, :env2_id, :cooldown_minutes, :next_eligible_at, :last_error,
            :started_at, :finished_at, :created_at, :updated_at, :scan_template_id
        )
        """
    )
    ins_step = text(
        """
        INSERT INTO workflow_steps (
            id, run_id, seq, environment_id, ds_kind, ds_name, scan_kind, file_set_type,
            is_enabled, is_acl, is_ocr, is_structured_ff, is_ols, is_lite,
            feature_flags, notes, ds_id, scan_profile_id, scan_profile_name,
            build_version, build_prefix, scan_id, status, last_error,
            started_at, finished_at, created_at, updated_at
        ) VALUES (
            :id, :run_id, :seq, :environment_id, :ds_kind, :ds_name, :scan_kind, :file_set_type,
            :is_enabled, :is_acl, :is_ocr, :is_structured_ff, :is_ols, :is_lite,
            :feature_flags, :notes, :ds_id, :scan_profile_id, :scan_profile_name,
            :build_version, :build_prefix, :scan_id, :status, :last_error,
            :started_at, :finished_at, :created_at, :updated_at
        )
        """
    )
    ins_tpl = text(
        """
        INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
        VALUES (:k, :template_kind, :payload_json, :updated_at)
        """
    )
    q_run_exists = text("SELECT 1 FROM workflow_runs WHERE id = :id LIMIT 1")
    q_step_exists = text(
        "SELECT 1 FROM workflow_steps WHERE id = :id OR (run_id = :rid AND seq = :sq) LIMIT 1"
    )
    q_tpl_exists = text("SELECT 1 FROM workflow_templates WHERE k = :k LIMIT 1")

    runs_inserted = 0
    runs_skipped_exist = 0
    steps_inserted = 0
    steps_skipped_orphan = 0
    steps_skipped_exist = 0
    tpl_inserted = 0
    tpl_skipped_exist = 0

    run_ids = {int(r["id"]) for r in m_runs}

    with pg_eng.begin() as conn:
        _ensure_workflow_ddl(conn)

        for r in m_runs:
            p = dict(r)
            if conn.execute(q_run_exists, {"id": p["id"]}).first():
                runs_skipped_exist += 1
                continue
            conn.execute(ins_run, p)
            runs_inserted += 1

        for s in m_steps:
            rid = int(s["run_id"])
            if rid not in run_ids:
                steps_skipped_orphan += 1
                continue
            row = dict(s)
            row["is_enabled"] = _bool(row.get("is_enabled"))
            row["is_acl"] = _bool(row.get("is_acl"))
            row["is_ocr"] = _bool(row.get("is_ocr"))
            row["is_structured_ff"] = _bool(row.get("is_structured_ff"))
            row["is_ols"] = _bool(row.get("is_ols"))
            row["is_lite"] = _bool(row.get("is_lite"))
            if conn.execute(
                q_step_exists, {"id": row["id"], "rid": row["run_id"], "sq": row["seq"]}
            ).first():
                steps_skipped_exist += 1
                continue
            conn.execute(ins_step, row)
            steps_inserted += 1

        for t in m_tpl:
            td = dict(t)
            if conn.execute(q_tpl_exists, {"k": td["k"]}).first():
                tpl_skipped_exist += 1
                continue
            conn.execute(ins_tpl, td)
            tpl_inserted += 1

        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('workflow_runs', 'id'), "
                "COALESCE((SELECT MAX(id) FROM workflow_runs), 1))"
            )
        )
        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('workflow_steps', 'id'), "
                "COALESCE((SELECT MAX(id) FROM workflow_steps), 1))"
            )
        )

    print(
        f"Done. inserted runs={runs_inserted}, skipped_existing_runs={runs_skipped_exist}; "
        f"inserted steps={steps_inserted}, skipped_existing_steps={steps_skipped_exist}, "
        f"orphan_steps={steps_skipped_orphan}; inserted templates={tpl_inserted}, "
        f"skipped_existing_templates={tpl_skipped_exist}"
    )
    print("Sequences on workflow_runs / workflow_steps updated to MAX(id).")


if __name__ == "__main__":
    main()
