#!/usr/bin/env python3
"""
Daily cleanup for AI scanner log error rows.

Deletes rows from scan_log_errors where log_timestamp is older than the
configured retention window. Intended to be run daily by cron, Task Scheduler,
or another external scheduler.

Examples:
  python3 scripts/cleanup_old_scan_logs.py --execute
  python3 scripts/cleanup_old_scan_logs.py --db postgres --days 14 --execute
  python3 scripts/cleanup_old_scan_logs.py --db both --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_mysql_url() -> str:
    from db_config import DB_CONNECTION_STRING

    return DB_CONNECTION_STRING


def _load_postgres_url() -> str:
    from v2_api.db_config_pg import DB_CONNECTION_STRING

    return DB_CONNECTION_STRING


def _db_targets(selected: str) -> Iterable[Tuple[str, str]]:
    if selected in ("mysql", "both"):
        yield "mysql", _load_mysql_url()
    if selected in ("postgres", "both"):
        yield "postgres", _load_postgres_url()


def _table_exists_sql(db_kind: str):
    if db_kind == "postgres":
        return text("SELECT to_regclass('public.scan_log_errors') IS NOT NULL")
    return text(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = 'scan_log_errors'
        """
    )


def _cutoff_sql(db_kind: str, days: int) -> str:
    if db_kind == "postgres":
        return f"NOW() - INTERVAL '{int(days)} days'"
    return f"DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"


def _connect(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def cleanup_db(db_kind: str, url: str, days: int, execute: bool) -> Tuple[int, int]:
    engine = _connect(url)
    cutoff = _cutoff_sql(db_kind, days)
    count_sql = text(f"SELECT COUNT(*) FROM scan_log_errors WHERE log_timestamp < {cutoff}")
    delete_sql = text(f"DELETE FROM scan_log_errors WHERE log_timestamp < {cutoff}")

    with engine.connect() as conn:
        table_exists = conn.execute(_table_exists_sql(db_kind)).scalar()
        if not bool(table_exists):
            print(f"[{db_kind}] scan_log_errors does not exist; skipping.")
            return 0, 0

        eligible = int(conn.execute(count_sql).scalar() or 0)
        if not execute:
            remaining = int(conn.execute(text("SELECT COUNT(*) FROM scan_log_errors")).scalar() or 0)
            print(f"[{db_kind}] dry-run: would delete {eligible} rows older than {days} days; remaining now={remaining}.")
            return eligible, remaining

        result = conn.execute(delete_sql)
        conn.commit()
        deleted = int(result.rowcount or 0)
        remaining = int(conn.execute(text("SELECT COUNT(*) FROM scan_log_errors")).scalar() or 0)
        print(f"[{db_kind}] deleted {deleted} rows older than {days} days; remaining={remaining}.")
        return deleted, remaining


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete old scan_log_errors rows by retention window.")
    parser.add_argument("--days", type=int, default=7, help="Retention period in days. Default: 7")
    parser.add_argument(
        "--db",
        choices=("postgres", "mysql", "both"),
        default="postgres",
        help="Database target. Default: postgres (React/v2).",
    )
    parser.add_argument("--execute", action="store_true", help="Actually delete rows.")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be deleted.")
    args = parser.parse_args()

    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2

    execute = bool(args.execute and not args.dry_run)
    if not execute:
        print("Running in dry-run mode. Add --execute to delete rows.")

    failures = 0
    for db_kind, url in _db_targets(args.db):
        try:
            cleanup_db(db_kind, url, args.days, execute)
        except SQLAlchemyError as exc:
            failures += 1
            print(f"[{db_kind}] cleanup failed: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
