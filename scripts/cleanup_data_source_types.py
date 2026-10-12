"""One-time cleanup: collapse polluted data_source_type values (scan labels that were
stored as data-source types) back to canonical types, deactivate the runs that don't
feed benchmarking/trends, drop the junk reference rows, and rebuild the affected
major-build averages.

Safe to read first: prints a BEFORE/AFTER summary and runs the mutations in one
transaction. Pass --apply to actually write; default is a dry run.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "v2_api"))
from sqlalchemy import create_engine, text
from db_config_pg import DB_CONNECTION_STRING
from benchmark_averages import recalculate_build_major_averages

APPLY = "--apply" in sys.argv

# Canonical target type per polluted benchmark_runs id (decided with the user).
# CSV scans default to S3 (matches add_benchmark_math). 4 runs feed major-246 trends
# and stay ACTIVE; the other 18 have no parsed build version -> deactivate.
CHART_RUNS = {262, 263, 276, 278}          # keep active, relabel only
RUN_TARGET = {
    273: "S3", 272: "S3", 247: "S3",        # 244-10 CSV
    276: "S3", 278: "S3", 262: "S3",        # 246-4 CSV  (chart)
    263: "Postgres",                         # 246-2 Postgres10K insurance (chart)
    292: "S3", 295: "S3",                    # bharat-s3
    296: "Postgres", 275: "Postgres", 291: "Postgres",  # Postgres10K
    286: "MSSQL",                            # MSQL- benchmarktesting
    288: "OneDrive",                         # loadmillOnedrive
    290: "SMB",                              # loadmillSmb
    289: "S3", 293: "S3", 294: "S3",         # CSV
    287: "Other (custom)", 297: "Other (custom)",  # Google Cloud, DS Test
    298: "Other (custom)", 285: "Other (custom)",  # folder, Unknown
}
JUNK_DS_IDS = [13, 14, 15, 16, 17, 18]

# auto_scan_benchmarks (frozen backup) carries the same junk strings; map by string so
# we can drop the junk reference rows without violating FK / leaving stale labels.
def canon_for(label: str) -> str:
    u = (label or "").upper()
    if "BHARAT-S3" in u or " S3" in u or u.startswith("S3") or "CSV" in u:
        return "S3"
    if "POSTGRES" in u:
        return "Postgres"
    if "MSQL" in u or "MSSQL" in u or "MS SQL" in u:
        return "MSSQL"
    if "MYSQL" in u:
        return "MySQL"
    if "ONEDRIVE" in u:
        return "OneDrive"
    if "SMB" in u:
        return "SMB"
    return "Other (custom)"


def main():
    engine = create_engine(DB_CONNECTION_STRING)
    with engine.connect() as c:
        ds_ids = {name: rid for rid, name in c.execute(
            text("SELECT id, name FROM data_source_types")).fetchall()}

        print("=== BEFORE: dropdown DS values (resolved) ===")
        for r in c.execute(text("""
            SELECT COALESCE(dst.name, b.data_source_type, '') ds, count(*)
            FROM benchmark_runs b LEFT JOIN data_source_types dst ON dst.id=b.data_source_type_id
            GROUP BY 1 ORDER BY 2 DESC""")):
            print(f"  {r[1]:>4}  {r[0]!r}")

    if not APPLY:
        print("\n[dry run] re-run with --apply to execute.")
        return

    with engine.begin() as conn:
        # 1) benchmark_runs: relabel + deactivate non-chart runs
        for rid, tname in RUN_TARGET.items():
            conn.execute(text("""
                UPDATE benchmark_runs
                   SET data_source_type = :n, data_source_type_id = :i,
                       is_active = :act, updated_at = CURRENT_TIMESTAMP
                 WHERE id = :rid"""),
                {"n": tname, "i": ds_ids[tname], "act": rid in CHART_RUNS, "rid": rid})

        # 2) auto_scan_benchmarks: relabel any junk (by id or free-text) -> canonical
        valid_names = {n for n, in conn.execute(text(
            "SELECT name FROM data_source_types WHERE id <> ALL(:ids)"), {"ids": JUNK_DS_IDS})}
        all_auto = conn.execute(text("""
            SELECT id, COALESCE(data_source_type, ''), data_source_type_id
            FROM auto_scan_benchmarks""")).fetchall()
        junk_rows = [(aid, label) for aid, label, dsid in all_auto
                     if dsid in JUNK_DS_IDS or label not in valid_names]
        for aid, label in junk_rows:
            tname = canon_for(label)
            conn.execute(text("""UPDATE auto_scan_benchmarks
                   SET data_source_type = :n, data_source_type_id = :i WHERE id = :aid"""),
                {"n": tname, "i": ds_ids[tname], "aid": aid})

        # 3) drop the junk reference rows (now unreferenced)
        conn.execute(text("DELETE FROM data_source_types WHERE id = ANY(:ids)"),
                     {"ids": JUNK_DS_IDS})

        # 4) clear stale averages for the only affected major, then recompute below
        conn.execute(text("DELETE FROM build_major_averages WHERE major_build = '246'"))

    # 5) rebuild major-246 averages from the corrected source rows
    n = recalculate_build_major_averages(engine, "246")
    print(f"\nrebuilt {n} build_major_averages rows for major 246")

    with engine.connect() as c:
        print("\n=== AFTER: dropdown DS values (resolved) ===")
        for r in c.execute(text("""
            SELECT COALESCE(dst.name, b.data_source_type, '') ds,
                   count(*) FILTER (WHERE COALESCE(b.is_active,TRUE)) active, count(*) total
            FROM benchmark_runs b LEFT JOIN data_source_types dst ON dst.id=b.data_source_type_id
            GROUP BY 1 ORDER BY 3 DESC""")):
            print(f"  active={r[1]:>3} total={r[2]:>3}  {r[0]!r}")


if __name__ == "__main__":
    main()
