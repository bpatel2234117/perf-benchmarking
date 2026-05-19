#!/usr/bin/env python3
"""
Delete manual benchmark rows (and benchmark_findings) for builds matching a version prefix.

Usage:
  python3 scratch/delete_benchmarks_by_build_prefix.py --prefix 251 --dry-run
  python3 scratch/delete_benchmarks_by_build_prefix.py --prefix 251
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from db_config import DB_CONNECTION_STRING


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--prefix",
        default="251",
        help="Match builds.version starting with this (e.g. 251 → 251.3, 251.10)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    prefix = str(args.prefix or "").strip()
    if not prefix:
        print("prefix required")
        return 1

    like_pat = prefix + "%"
    engine = create_engine(DB_CONNECTION_STRING)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT b.id AS benchmark_id, bu.version AS build_version, b.scenario_type, b.data_source_type, b.file_set_type
                FROM benchmarks b
                JOIN builds bu ON bu.id = b.build_id
                WHERE bu.version LIKE :pat
                ORDER BY bu.version, b.id
                """
            ),
            {"pat": like_pat},
        ).fetchall()

    print(f"Prefix {prefix!r} ({like_pat!r}): {len(rows)} benchmark row(s)")
    for r in rows[:50]:
        print(f"  id={r[0]} build={r[1]!r} scenario={r[2]!r} ds={r[3]!r} fs={r[4]!r}")
    if len(rows) > 50:
        print(f"  ... and {len(rows) - 50} more")

    if args.dry_run:
        print("Dry run: no deletes performed.")
        return 0

    if not rows:
        return 0

    with engine.begin() as conn:
        # benchmark_findings(benchmark_id) → benchmarks
        conn.execute(
            text(
                """
                DELETE bf FROM benchmark_findings bf
                INNER JOIN benchmarks b ON b.id = bf.benchmark_id
                INNER JOIN builds bu ON bu.id = b.build_id
                WHERE bu.version LIKE :pat
                """
            ),
            {"pat": like_pat},
        )
        r2 = conn.execute(
            text(
                """
                DELETE b FROM benchmarks b
                INNER JOIN builds bu ON bu.id = b.build_id
                WHERE bu.version LIKE :pat
                """
            ),
            {"pat": like_pat},
        )
        try:
            n = r2.rowcount
        except Exception:
            n = -1
        print(f"Deleted benchmarks (approx rows affected): {n}")

    print("Done. builds rows for those versions are unchanged; re-add benchmarks via Add New Benchmark.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
