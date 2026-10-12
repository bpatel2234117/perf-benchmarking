"""Consolidate fragmented benchmark file-set names to canonical, data-source-prefixed names.

The same logical scenario was historically saved under multiple file_set_types names
(e.g. "Small Files" for older builds, "S3 Small Files" for newer ones), which splits the
trend charts and makes the Data Source x File Size filters produce empty cross-products.

This migration repoints each benchmark's file_set_type_id to the canonical combined name
(see benchmark_scenario_normalizer.canonical_combined_file_set). It is:
  - non-destructive to runs: every benchmark row is kept (a run with a different scan time
    stays a separate row); only its file_set_type_id is corrected.
  - idempotent: a row already on its canonical name is skipped, so it can run on every boot.
  - conservative: rows whose data source can't be classified are left untouched.
"""
from __future__ import annotations

from typing import Dict

from sqlalchemy import text
from sqlalchemy.engine import Engine

try:
    from benchmark_scenario_normalizer import canonical_combined_file_set
except ImportError:  # pragma: no cover
    from v2_api.benchmark_scenario_normalizer import canonical_combined_file_set  # type: ignore


def normalize_benchmark_file_sets(engine: Engine) -> Dict[str, int]:
    """Repoint benchmarks to canonical combined file-set names. Returns counts."""
    repointed = 0
    created = 0
    fs_id_cache: Dict[str, int] = {}

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT b.id, b.file_set_type_id, dst.name AS ds, fst.name AS fs,
                       fst.data_type, fst.total_files, fst.total_folders, fst.total_size_gb
                FROM benchmarks b
                LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
                """
            )
        ).fetchall()

        def resolve_fs_id(name: str, data_type, tf, tfo, tsz) -> int:
            nonlocal created
            if name in fs_id_cache:
                return fs_id_cache[name]
            found = conn.execute(
                text("SELECT id FROM file_set_types WHERE name = :n LIMIT 1"), {"n": name}
            ).fetchone()
            if found:
                fs_id_cache[name] = int(found[0])
                return fs_id_cache[name]
            # Create the canonical row, carrying over the source row's size metadata so
            # Add-Benchmark prefill still has defaults.
            new = conn.execute(
                text(
                    """
                    INSERT INTO file_set_types (name, data_type, total_files, total_folders, total_size_gb)
                    VALUES (:n, :dt, :tf, :tfo, :tsz)
                    RETURNING id
                    """
                ),
                {"n": name, "dt": data_type, "tf": tf, "tfo": tfo, "tsz": tsz},
            ).fetchone()
            created += 1
            fs_id_cache[name] = int(new[0])
            return fs_id_cache[name]

        for r in rows:
            bid, cur_fsid, ds, fs = r[0], r[1], r[2], r[3]
            canonical = canonical_combined_file_set(ds, fs)
            if not canonical or canonical == (fs or ""):
                continue  # unclassifiable or already canonical → leave alone
            new_id = resolve_fs_id(canonical, r[4], r[5], r[6], r[7])
            if new_id != cur_fsid:
                conn.execute(
                    text("UPDATE benchmarks SET file_set_type_id = :fid WHERE id = :bid"),
                    {"fid": new_id, "bid": bid},
                )
                repointed += 1

    return {"repointed": repointed, "created_file_sets": created, "scanned": len(rows)}


if __name__ == "__main__":
    from db_config_pg import DB_CONNECTION_STRING  # type: ignore
    from sqlalchemy import create_engine

    result = normalize_benchmark_file_sets(create_engine(DB_CONNECTION_STRING))
    print(f"file-set normalization: {result}")
