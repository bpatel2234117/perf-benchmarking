#!/usr/bin/env python3
"""
Normalize saved benchmark scenario metadata for OLS / ACL / Structured / CSV rows.

Default mode is a dry run. Use --apply to update DB rows.

Examples:
  python3 scripts/normalize_benchmark_scenarios.py --db mysql
  python3 scripts/normalize_benchmark_scenarios.py --db mysql --build 252.15 --apply
  python3 scripts/normalize_benchmark_scenarios.py --db pg --apply
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import create_engine, text

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from benchmark_scenario_normalizer import canonical_benchmark_fields  # noqa: E402


def _engine(db: str):
    if db == "pg":
        from v2_api.db_config_pg import DB_CONNECTION_STRING  # noqa: E402
    else:
        from db_config import DB_CONNECTION_STRING  # noqa: E402
    return create_engine(DB_CONNECTION_STRING, connect_args={"connect_timeout": 5})


def _get_or_create_mysql_config(conn, row, canon: Dict[str, Any]) -> int:
    scanner_count = int(row.get("scanner_count") or 0)
    thread_count = int(row.get("thread_count") or 0)
    is_ocr = int(row.get("is_ocr") or 0)
    feature_flags = str(row.get("feature_flags") or "").strip()
    if canon["is_feature_flags"] and not feature_flags:
        feature_flags = "Structured"
    comment = str(row.get("comment") or "").strip()
    conf_str = f"{scanner_count}-{thread_count}-{bool(canon['is_acl'])}-{bool(is_ocr)}-{bool(canon['is_feature_flags'])}-{feature_flags}-{comment}"
    conf_hash = hashlib.md5(conf_str.encode()).hexdigest()
    found = conn.execute(text("SELECT id FROM configurations WHERE hash=:h LIMIT 1"), {"h": conf_hash}).fetchone()
    if found:
        return int(found[0])
    conn.execute(
        text("""
            INSERT INTO configurations
            (scanner_count, thread_count, is_acl, is_ocr, is_feature_flags, feature_flags, comment, hash)
            VALUES (:sc, :tc, :acl, :ocr, :ff, :flags, :comment, :hash)
        """),
        {
            "sc": scanner_count,
            "tc": thread_count,
            "acl": int(bool(canon["is_acl"])),
            "ocr": is_ocr,
            "ff": int(bool(canon["is_feature_flags"])),
            "flags": feature_flags or None,
            "comment": comment or None,
            "hash": conf_hash,
        },
    )
    return int(conn.execute(text("SELECT id FROM configurations WHERE hash=:h LIMIT 1"), {"h": conf_hash}).fetchone()[0])


def _get_or_create_pg_id(conn, table: str, name: str) -> int:
    row = conn.execute(text(f"SELECT id FROM {table} WHERE name=:n LIMIT 1"), {"n": name}).fetchone()
    if row:
        return int(row[0])
    return int(conn.execute(text(f"INSERT INTO {table} (name) VALUES (:n) RETURNING id"), {"n": name}).fetchone()[0])


def _get_or_create_pg_config(conn, row, canon: Dict[str, Any]) -> int:
    scanner_count = int(row.get("scanner_count") or 0)
    thread_count = int(row.get("thread_count") or 0)
    is_ocr = bool(row.get("is_ocr") or False)
    feature_flags = str(row.get("feature_flags") or "").strip()
    if canon["is_feature_flags"] and not feature_flags:
        feature_flags = "Structured"
    conf_hash = hashlib.md5(
        f"{scanner_count}|{thread_count}|{bool(canon['is_acl'])}|{is_ocr}|{bool(canon['is_feature_flags'])}|{feature_flags}".encode()
    ).hexdigest()
    found = conn.execute(text("SELECT id FROM configurations WHERE hash=:h LIMIT 1"), {"h": conf_hash}).fetchone()
    if found:
        return int(found[0])
    return int(conn.execute(
        text("""
            INSERT INTO configurations
            (scanner_count, thread_count, is_acl, is_ocr, is_feature_flags, feature_flags, hash)
            VALUES (:sc, :tc, :acl, :ocr, :ff, :flags, :hash)
            RETURNING id
        """),
        {
            "sc": scanner_count,
            "tc": thread_count,
            "acl": bool(canon["is_acl"]),
            "ocr": is_ocr,
            "ff": bool(canon["is_feature_flags"]),
            "flags": feature_flags or None,
            "hash": conf_hash,
        },
    ).fetchone()[0])


def _load_rows(conn, db: str, build: Optional[str]):
    if db == "pg":
        sql = """
            SELECT b.id, bv.version AS build_version, b.scan_id, COALESCE(sp.scan_name,'') AS scan_name,
                   COALESCE(b.scenario_type,'') AS scenario_type, COALESCE(b.scan_type,'') AS scan_type,
                   COALESCE(dst.name,'') AS data_source_type, COALESCE(fst.name,'') AS file_set_type,
                   cfg.id AS config_id, COALESCE(cfg.scanner_count,0) AS scanner_count,
                   COALESCE(cfg.thread_count,0) AS thread_count, COALESCE(cfg.is_acl,FALSE) AS is_acl,
                   COALESCE(cfg.is_ocr,FALSE) AS is_ocr, COALESCE(cfg.is_feature_flags,FALSE) AS is_feature_flags,
                   COALESCE(cfg.feature_flags,'') AS feature_flags
            FROM benchmarks b
            JOIN builds bv ON bv.id=b.build_id
            LEFT JOIN data_source_types dst ON dst.id=b.data_source_type_id
            LEFT JOIN file_set_types fst ON fst.id=b.file_set_type_id
            LEFT JOIN configurations cfg ON cfg.id=b.config_id
            LEFT JOIN scan_parents sp ON sp.scan_id=b.scan_id
            WHERE COALESCE(b.is_active, TRUE)=TRUE
        """
    else:
        sql = """
            SELECT b.id, bu.version AS build_version, b.scan_id, COALESCE(sp.scan_name,'') AS scan_name,
                   COALESCE(b.scenario_type,'') AS scenario_type, COALESCE(b.scan_type,'') AS scan_type,
                   COALESCE(b.data_source_type,'') AS data_source_type, COALESCE(b.file_set_type,'') AS file_set_type,
                   conf.id AS config_id, COALESCE(conf.scanner_count,0) AS scanner_count,
                   COALESCE(conf.thread_count,0) AS thread_count, COALESCE(conf.is_acl,0) AS is_acl,
                   COALESCE(conf.is_ocr,0) AS is_ocr, COALESCE(conf.is_feature_flags,0) AS is_feature_flags,
                   COALESCE(conf.feature_flags,'') AS feature_flags, COALESCE(conf.comment,'') AS comment
            FROM benchmarks b
            JOIN builds bu ON bu.id=b.build_id
            LEFT JOIN configurations conf ON conf.id=b.config_id
            LEFT JOIN scan_parents sp ON sp.scan_id=b.scan_id
            WHERE COALESCE(b.is_active, 1)=1
        """
    params = {}
    if build:
        sql += " AND " + ("bv.version=:build" if db == "pg" else "bu.version=:build")
        params["build"] = build
    return conn.execute(text(sql), params).mappings().fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", choices=["mysql", "pg"], default="mysql")
    ap.add_argument("--build", help="Limit to one build version, e.g. 252.15")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    engine = _engine(args.db)
    changed = 0
    inspected = 0
    with engine.begin() as conn:
        for row in _load_rows(conn, args.db, args.build):
            inspected += 1
            canon = canonical_benchmark_fields(row)
            updates: Dict[str, Any] = {}
            if str(row.get("scan_type") or "") != canon["scan_type"]:
                updates["scan_type"] = canon["scan_type"]
            if str(row.get("scenario_type") or "") != canon["matrix_file_set_type"]:
                updates["scenario_type"] = canon["matrix_file_set_type"]

            if args.db == "pg":
                if str(row.get("data_source_type") or "") != canon["data_source_type"]:
                    updates["data_source_type"] = canon["data_source_type"]
                if str(row.get("file_set_type") or "") != canon["file_set_type"]:
                    updates["file_set_type"] = canon["file_set_type"]
                if bool(row.get("is_acl")) != bool(canon["is_acl"]) or bool(row.get("is_feature_flags")) != bool(canon["is_feature_flags"]):
                    updates["config_id"] = "<new matching config>"
                if args.apply:
                    if "data_source_type" in updates:
                        updates["data_source_type_id"] = _get_or_create_pg_id(conn, "data_source_types", canon["data_source_type"])
                        del updates["data_source_type"]
                    if "file_set_type" in updates:
                        updates["file_set_type_id"] = _get_or_create_pg_id(conn, "file_set_types", canon["file_set_type"])
                        del updates["file_set_type"]
                    if updates.get("config_id") == "<new matching config>":
                        updates["config_id"] = _get_or_create_pg_config(conn, row, canon)
            else:
                if bool(int(row.get("is_acl") or 0)) != bool(canon["is_acl"]) or bool(int(row.get("is_feature_flags") or 0)) != bool(canon["is_feature_flags"]):
                    updates["config_id"] = "<new matching config>"
                if str(row.get("data_source_type") or "") != canon["data_source_type"]:
                    updates["data_source_type"] = canon["data_source_type"]
                if str(row.get("file_set_type") or "") != canon["file_set_type"]:
                    updates["file_set_type"] = canon["file_set_type"]
                if args.apply and updates.get("config_id") == "<new matching config>":
                    updates["config_id"] = _get_or_create_mysql_config(conn, row, canon)

            if not updates:
                continue
            changed += 1
            print(f"benchmark {row['id']} build={row.get('build_version')} scan={row.get('scan_id')}: {updates}")
            if args.apply:
                sets = ", ".join(f"{k}=:{k}" for k in updates)
                updates["id"] = row["id"]
                conn.execute(text(f"UPDATE benchmarks SET {sets} WHERE id=:id"), updates)
    print(f"{'updated' if args.apply else 'would update'} {changed} of {inspected} benchmark rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
