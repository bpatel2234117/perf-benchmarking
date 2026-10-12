"""Consolidate `benchmarks` (manual) + `auto_scan_benchmarks` (legacy) into `benchmark_runs`.

`benchmark_runs` is the single source of truth. This migration is idempotent (keyed on
legacy_table+legacy_id via ON CONFLICT) and runs on startup, so it also folds in any rows
added to the old tables later. The old tables are never modified — they remain backups.

Merge policy (confirmed): one unified row per scan_id. Every manual row is preserved; an
auto row whose scan_id matches a manual row is ABSORBED into it (manual wins curated fields:
throughput tiers, config/classifier, is_release, total_catalog_items, avg_file_size; the auto
row fills auto-only scan metrics where the manual is null). Auto rows with no manual match are
inserted on their own. No run is dropped.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

try:
    from benchmark_scenario_normalizer import canonical_combined_file_set, normalize_ds_name
except ImportError:  # pragma: no cover
    from v2_api.benchmark_scenario_normalizer import (  # type: ignore
        canonical_combined_file_set,
        normalize_ds_name,
    )

# Column order for the unified insert (everything except id/updated_at).
_COLS: List[str] = [
    "source", "legacy_table", "legacy_id", "scan_id",
    "build_id", "config_id", "classifier_set_id", "data_source_type_id",
    "file_set_type_id", "environment_id",
    "build_version", "data_source_type", "file_set_type", "data_type",
    "run_date", "scenario_type", "scan_type", "scan_name",
    "scan_start_time", "scan_end_time", "total_scan_hours",
    "throughput_cluster_gb_hr", "throughput_scanner_gb_hr", "throughput_thread_gb_hr",
    "throughput_cluster_files_hr", "throughput_scanner_files_hr", "throughput_thread_files_hr",
    "total_catalog_items", "objects_scanned", "objects_with_pi", "objects_enumerated",
    "total_classification", "new_attributes", "removed_attributes", "classifier_attribute_names",
    "total_size_gb", "total_files", "total_folders", "avg_file_size",
    "is_active", "is_release", "created_by",
]

# Auto-only metric fields the merge pulls from an auto row into a matching manual row
# (only when the manual value is null) so nothing is lost on absorb.
_AUTO_FILL = [
    "scan_name", "data_type", "environment_id",
    "objects_scanned", "objects_with_pi", "objects_enumerated",
    "new_attributes", "removed_attributes", "classifier_attribute_names",
    "total_size_gb", "total_files", "total_folders",
    "throughput_cluster_gb_hr", "throughput_cluster_files_hr", "total_classification",
]


_MARKER_KEY = "benchmark_runs_consolidated"


def _insert_sql() -> str:
    cols = ", ".join(_COLS)
    binds = ", ".join(f":{c}" for c in _COLS)
    # DO NOTHING (not DO UPDATE): this is a ONE-TIME backfill of the frozen legacy tables.
    # After it runs, all new writes go straight to benchmark_runs, and user edits/deletes
    # there must never be clobbered by a re-import of the (read-only) legacy rows.
    return (
        f"INSERT INTO benchmark_runs ({cols}, updated_at) VALUES ({binds}, CURRENT_TIMESTAMP) "
        f"ON CONFLICT (legacy_table, legacy_id) DO NOTHING"
    )


def consolidate_benchmark_runs(engine: Engine, *, force: bool = False) -> Dict[str, int]:
    """One-time backfill of legacy benchmark tables into benchmark_runs.

    Idempotent and safe to call on startup: it self-skips once the marker is set (so it
    can't undo later edits/deletes). Pass force=True (CLI) to re-import after, e.g., a
    fresh migrate_data load — already-present legacy rows are left untouched (DO NOTHING).
    """
    with engine.connect() as check:
        done = check.execute(
            text("SELECT setting_value FROM app_settings WHERE setting_key = :k"),
            {"k": _MARKER_KEY},
        ).fetchone()
        has_rows = check.execute(text("SELECT 1 FROM benchmark_runs LIMIT 1")).fetchone()
    # Self-heal: only skip when the marker is set AND the table already has data. If a prior
    # run set the marker but left benchmark_runs empty (e.g. it errored), repopulate.
    if done and has_rows and not force:
        return {"skipped": True}

    inserted = 0
    merged = 0
    fs_cache: Dict[str, Optional[int]] = {}
    ds_cache: Dict[str, Optional[int]] = {}

    with engine.begin() as conn:
        def resolve_fs_id(canonical: Optional[str]) -> Optional[int]:
            if not canonical:
                return None
            if canonical in fs_cache:
                return fs_cache[canonical]
            row = conn.execute(
                text("SELECT id FROM file_set_types WHERE name = :n LIMIT 1"), {"n": canonical}
            ).fetchone()
            if not row:
                row = conn.execute(
                    text("INSERT INTO file_set_types (name) VALUES (:n) RETURNING id"), {"n": canonical}
                ).fetchone()
            fs_cache[canonical] = int(row[0])
            return fs_cache[canonical]

        def resolve_ds_id(ds_name: Optional[str]) -> Optional[int]:
            norm = normalize_ds_name(ds_name)
            if not norm:
                return None
            if norm in ds_cache:
                return ds_cache[norm]
            row = conn.execute(
                text("SELECT id FROM data_source_types WHERE name = :n LIMIT 1"), {"n": norm}
            ).fetchone()
            if not row:
                row = conn.execute(
                    text("INSERT INTO data_source_types (name) VALUES (:n) RETURNING id"), {"n": norm}
                ).fetchone()
            ds_cache[norm] = int(row[0])
            return ds_cache[norm]

        def blank_record() -> Dict[str, Any]:
            return {c: None for c in _COLS}

        # ---- manual rows ----
        manual_rows = conn.execute(
            text(
                """
                SELECT b.id, b.scan_id, b.build_id, b.config_id, b.classifier_set_id,
                       dst.name AS ds_name, fst.name AS fs_name,
                       b.run_date, b.scenario_type, b.scan_type,
                       b.scan_start_time, b.scan_end_time, b.total_scan_hours,
                       b.throughput_cluster_gb_hr, b.throughput_scanner_gb_hr, b.throughput_thread_gb_hr,
                       b.throughput_cluster_files_hr, b.throughput_scanner_files_hr, b.throughput_thread_files_hr,
                       b.total_catalog_items, b.total_classification, b.avg_file_size,
                       b.is_active, b.is_release, b.created_by, bv.version AS build_version
                FROM benchmarks b
                LEFT JOIN builds bv ON bv.id = b.build_id
                LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
                LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
                """
            )
        ).fetchall()

        records: List[Dict[str, Any]] = []
        manual_by_scan: Dict[str, Dict[str, Any]] = {}
        for r in manual_rows:
            ds_name, fs_name = r[5], r[6]
            canonical = canonical_combined_file_set(ds_name, fs_name) or (fs_name or None)
            rec = blank_record()
            rec.update(
                source="manual", legacy_table="benchmarks", legacy_id=int(r[0]),
                scan_id=(str(r[1]).strip() or None) if r[1] else None,
                build_id=r[2], config_id=r[3], classifier_set_id=r[4],
                data_source_type_id=resolve_ds_id(ds_name), file_set_type_id=resolve_fs_id(canonical),
                build_version=r[25], data_source_type=normalize_ds_name(ds_name) or ds_name,
                file_set_type=canonical, run_date=(str(r[7]) if r[7] is not None else None), scenario_type=r[8], scan_type=r[9],
                scan_start_time=r[10], scan_end_time=r[11], total_scan_hours=r[12],
                throughput_cluster_gb_hr=r[13], throughput_scanner_gb_hr=r[14], throughput_thread_gb_hr=r[15],
                throughput_cluster_files_hr=r[16], throughput_scanner_files_hr=r[17], throughput_thread_files_hr=r[18],
                total_catalog_items=r[19], total_classification=r[20], avg_file_size=r[21],
                is_active=r[22], is_release=r[23], created_by=r[24],
            )
            records.append(rec)
            if rec["scan_id"] and rec["scan_id"] not in manual_by_scan:
                manual_by_scan[rec["scan_id"]] = rec

        # ---- auto rows: absorb into matching manual, else insert standalone ----
        auto_rows = conn.execute(
            text(
                """
                SELECT a.id, a.scan_id, a.build_id, a.data_source_type_id, a.file_set_type_id,
                       dst.name AS ds_name, fst.name AS fs_name,
                       a.data_source_type, a.file_set_type, a.data_type, a.build_version, a.environment_id,
                       a.run_date, a.scenario_type, a.scan_type, a.scan_name,
                       a.scan_start_time, a.scan_end_time, a.total_scan_hours,
                       a.throughput_cluster_gb_hr, a.throughput_cluster_files_hr,
                       a.objects_with_pi, a.objects_scanned, a.objects_enumerated, a.total_classification,
                       a.new_attributes, a.removed_attributes, a.classifier_attribute_names,
                       a.total_size_gb, a.total_files, a.total_folders,
                       a.is_active, a.is_release, a.created_by
                FROM auto_scan_benchmarks a
                LEFT JOIN data_source_types dst ON dst.id = a.data_source_type_id
                LEFT JOIN file_set_types fst ON fst.id = a.file_set_type_id
                """
            )
        ).fetchall()

        for r in auto_rows:
            scan_id = (str(r[1]).strip() or None) if r[1] else None
            ds_name = r[5] or r[7]
            fs_name = r[6] or r[8]
            canonical = canonical_combined_file_set(ds_name, fs_name) or (fs_name or None)
            auto_vals = {
                "scan_name": r[15], "data_type": r[9], "environment_id": r[11],
                "objects_with_pi": r[21], "objects_scanned": r[22], "objects_enumerated": r[23],
                "total_classification": r[24], "new_attributes": r[25], "removed_attributes": r[26],
                "classifier_attribute_names": r[27], "total_size_gb": r[28], "total_files": r[29],
                "total_folders": r[30], "throughput_cluster_gb_hr": r[19], "throughput_cluster_files_hr": r[20],
            }
            if scan_id and scan_id in manual_by_scan:
                target = manual_by_scan[scan_id]
                for f in _AUTO_FILL:
                    if target.get(f) is None and auto_vals.get(f) is not None:
                        target[f] = auto_vals[f]
                merged += 1
                continue
            rec = blank_record()
            rec.update(
                source="auto", legacy_table="auto_scan_benchmarks", legacy_id=int(r[0]),
                scan_id=scan_id, build_id=r[2],
                data_source_type_id=resolve_ds_id(ds_name), file_set_type_id=resolve_fs_id(canonical),
                environment_id=r[11], build_version=r[10],
                data_source_type=normalize_ds_name(ds_name) or ds_name, file_set_type=canonical,
                data_type=r[9], run_date=(str(r[12]) if r[12] is not None else None), scenario_type=r[13], scan_type=r[14], scan_name=r[15],
                scan_start_time=r[16], scan_end_time=r[17], total_scan_hours=r[18],
                throughput_cluster_gb_hr=r[19], throughput_cluster_files_hr=r[20],
                objects_with_pi=r[21], objects_scanned=r[22], objects_enumerated=r[23],
                total_classification=r[24], new_attributes=r[25], removed_attributes=r[26],
                classifier_attribute_names=r[27], total_size_gb=r[28], total_files=r[29], total_folders=r[30],
                is_active=r[31], is_release=r[32], created_by=r[33],
            )
            records.append(rec)

        sql = text(_insert_sql())
        failed = 0
        sample_error: Optional[str] = None
        for rec in records:
            # Per-row SAVEPOINT so one bad row can't abort the whole backfill (which would
            # leave benchmark_runs empty and break every read that now points at it).
            try:
                with conn.begin_nested():
                    result = conn.execute(sql, rec)
                    inserted += int(result.rowcount or 0)
            except Exception as ex:
                failed += 1
                if sample_error is None:
                    sample_error = f"{type(ex).__name__}: {str(ex)[:300]}"

        conn.execute(
            text(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (:k, '1', CURRENT_TIMESTAMP)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = '1', updated_at = CURRENT_TIMESTAMP
                """
            ),
            {"k": _MARKER_KEY},
        )

    return {
        "inserted": inserted,
        "failed": failed,
        "sample_error": sample_error,
        "merged_auto_into_manual": merged,
        "scanned_manual": len(manual_rows),
        "scanned_auto": len(auto_rows),
    }


if __name__ == "__main__":
    from sqlalchemy import create_engine

    try:
        from db_config_pg import DB_CONNECTION_STRING  # type: ignore
    except ImportError:
        from v2_api.db_config_pg import DB_CONNECTION_STRING  # type: ignore

    # CLI forces a (re)run so it can repopulate even if the marker was set.
    print(f"benchmark consolidation: {consolidate_benchmark_runs(create_engine(DB_CONNECTION_STRING), force=True)}")
