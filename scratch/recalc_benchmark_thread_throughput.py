#!/usr/bin/env python3
"""
Recalculate per-scanner and per-thread throughput for benchmarks after fixing
scanner/thread counts using scanner_group_performance peaks (same window as workflow_runner).

Usage:
  python3 scratch/recalc_benchmark_thread_throughput.py [--date YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from db_config import DB_CONNECTION_STRING


def parse_iso_datetime(dt_value):
    s = str(dt_value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def get_peaks(conn, scan_id: str) -> tuple[int, int]:
    row = conn.execute(
        text("""
            SELECT sp.started_at, sp.completed_at, sp.environment_id
            FROM scan_parents sp
            WHERE sp.scan_id = :sid
            LIMIT 1
        """),
        {"sid": str(scan_id).strip()},
    ).fetchone()
    if not row:
        return (0, 0)
    scan_start = parse_iso_datetime(row[0])
    scan_end = parse_iso_datetime(row[1]) or scan_start
    eid = row[2]
    if not scan_start or eid is None:
        return (0, 0)
    if not scan_end:
        scan_end = scan_start
    gp_row = conn.execute(
        text("""
            SELECT MAX(running_scanners), MAX(scanner_threads)
            FROM scanner_group_performance
            WHERE environment_id = :eid
              AND captured_at_utc BETWEEN :start AND :end
        """),
        {"eid": int(eid), "start": scan_start, "end": scan_end},
    ).fetchone()
    if gp_row and gp_row[0]:
        return (
            max(0, int(gp_row[0] or 0)),
            max(0, int(gp_row[1] or 0)),
        )
    return (0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-04-20", help="Run date filter (local MySQL DATE match)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    target = args.date

    engine = create_engine(DB_CONNECTION_STRING)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT b.id, b.scan_id, b.config_id,
                       b.throughput_cluster_gb_hr, b.throughput_cluster_files_hr,
                       c.scanner_count, c.thread_count, c.is_acl, c.is_ocr, c.is_feature_flags,
                       IFNULL(c.feature_flags, ''), IFNULL(c.comment, ''),
                       b.run_date
                FROM benchmarks b
                JOIN configurations c ON c.id = b.config_id
                WHERE b.scan_id IS NOT NULL AND TRIM(b.scan_id) != ''
                  AND (b.run_date = :d2 OR b.run_date LIKE :pat)
                ORDER BY b.id
            """),
            {"d2": target, "pat": f"%{target}%"},
        ).fetchall()

    print(f"Found {len(rows)} benchmark row(s) for date filter {target!r}")
    if not rows:
        return 0

    updates = []
    for r in rows:
        bid, scan_id, cid = r[0], r[1], r[2]
        tpcg, tpcf = r[3], r[4]
        old_sc, old_tc = r[5], r[6]
        is_acl, is_ocr, is_ff = r[7], r[8], r[9]
        fflags, comment = r[10], r[11]
        run_date = r[12]

        with engine.connect() as conn:
            peak_sc, peak_tc = get_peaks(conn, str(scan_id))

        if peak_sc <= 0 or peak_tc <= 0:
            print(f"  SKIP id={bid} scan_id={scan_id!r}: no scanner_group_performance peaks")
            continue
        if int(old_sc or 0) == peak_sc and int(old_tc or 0) == peak_tc:
            print(f"  OK   id={bid} scan_id={scan_id!r}: already sc={peak_sc} tc={peak_tc}")
            continue

        new_hash = hashlib.md5(
            f"{peak_sc}-{peak_tc}-{bool(is_acl)}-{bool(is_ocr)}-{bool(is_ff)}-{fflags}-{comment}".encode()
        ).hexdigest()

        tpsg = (float(tpcg or 0) / peak_sc) if peak_sc else 0.0
        tptg = (float(tpcg or 0) / peak_tc) if peak_tc else 0.0
        tpsf = (float(tpcf or 0) / peak_sc) if peak_sc else 0.0
        tptf = (float(tpcf or 0) / peak_tc) if peak_tc else 0.0

        print(
            f"  FIX  id={bid} scan_id={scan_id!r} run_date={run_date!r} "
            f"sc {old_sc}->{peak_sc} tc {old_tc}->{peak_tc} "
            f"thread_gb/hr {(float(tpcg or 0) / int(old_tc or 1)):.4f} -> {tptg:.4f}"
        )
        updates.append(
            {
                "cid": cid,
                "sc": peak_sc,
                "tc": peak_tc,
                "h": new_hash,
                "bid": bid,
                "tpsg": tpsg,
                "tptg": tptg,
                "tpsf": tpsf,
                "tptf": tptf,
            }
        )

    if args.dry_run:
        print(f"Dry run: would apply {len(updates)} update(s)")
        return 0

    with engine.begin() as conn:
        for u in updates:
            cid = u["cid"]
            new_hash = u["h"]
            existing = conn.execute(
                text("SELECT id FROM configurations WHERE hash = :h LIMIT 1"),
                {"h": new_hash},
            ).fetchone()
            if existing:
                exid = int(existing[0])
                if exid != cid:
                    conn.execute(
                        text("UPDATE benchmarks SET config_id = :tcid WHERE id = :bid"),
                        {"tcid": exid, "bid": u["bid"]},
                    )
            else:
                conn.execute(
                    text("""
                        UPDATE configurations
                        SET scanner_count = :sc, thread_count = :tc, hash = :h
                        WHERE id = :cid
                    """),
                    {"sc": u["sc"], "tc": u["tc"], "h": new_hash, "cid": cid},
                )
            conn.execute(
                text("""
                    UPDATE benchmarks
                    SET throughput_scanner_gb_hr = :tpsg,
                        throughput_thread_gb_hr = :tptg,
                        throughput_scanner_files_hr = :tpsf,
                        throughput_thread_files_hr = :tptf
                    WHERE id = :bid
                """),
                {
                    "tpsg": u["tpsg"],
                    "tptg": u["tptg"],
                    "tpsf": u["tpsf"],
                    "tptf": u["tptf"],
                    "bid": u["bid"],
                },
            )

    print(f"Applied {len(updates)} configuration + benchmark update(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
