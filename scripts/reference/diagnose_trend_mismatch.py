#!/usr/bin/env python3
"""
Diagnose why /tools/generate-report trend charts disagree with the Dashboard.

Run this ON THE SERVER (10.2.0.93) — it needs to reach Postgres at 10.2.20.89,
which the Mac cannot. Uses only psycopg2 (already in the spb-backend image).

    # from the repo root on the server:
    python scripts/reference/diagnose_trend_mismatch.py
    # or inside the container:
    docker compose -f docker-compose.ec2.yml exec api \
        python scripts/reference/diagnose_trend_mismatch.py

For each major build it prints, for S3 and SMB Small Files / Full Scan (non-OLS):
  n_total   = runs the OLD report averaged together
  n_acl_ff  = ACL-on or Structured-Discovery runs (the ones the Dashboard splits out)
  avg_ALL   = mean cluster GB/hr over ALL runs        (old report value)
  avg_BASE  = mean cluster GB/hr EXCLUDING ACL/FF runs (Dashboard value == my fix)
If avg_ALL != avg_BASE, the mismatch is the ACL/FF blending and my fix corrects it.
If they are equal, the mismatch is something else (build window / sub-build set).
"""
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not available — run inside the api container.")

DSN = dict(
    host=os.getenv("Postgres_host", "10.2.20.89"),
    port=int(os.getenv("Postgres_port", 5432)),
    user=os.getenv("Postgres_username", "postgres"),
    password=os.getenv("Postgres_password", "bigid111!"),
    dbname=os.getenv("DB_NAME", "benchmark_db"),
    connect_timeout=10,
)

# Structured Discovery = is_feature_flags AND file_set is not CSV (CSV's FF is inherent).
STRUCT_DISC = "(COALESCE(cfg.is_feature_flags, FALSE) AND LOWER(COALESCE(fst.name,'')) NOT LIKE '%%csv%%')"
EXCLUDE = f"(COALESCE(cfg.is_acl, FALSE) OR {STRUCT_DISC})"

SQL = f"""
SELECT
    SUBSTRING(bv.version FROM '^([0-9]+)')                              AS major,
    COUNT(*)                                                           AS n_total,
    COUNT(*) FILTER (WHERE {EXCLUDE})                                  AS n_acl_ff,
    ROUND(AVG(b.throughput_cluster_gb_hr)::numeric, 2)                 AS avg_all,
    ROUND(AVG(b.throughput_cluster_gb_hr)
          FILTER (WHERE NOT {EXCLUDE})::numeric, 2)                    AS avg_base
FROM benchmark_runs b
LEFT JOIN builds bv             ON bv.id  = b.build_id
LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
LEFT JOIN file_set_types fst    ON fst.id = b.file_set_type_id
LEFT JOIN configurations cfg    ON cfg.id = b.config_id
WHERE COALESCE(b.is_active, TRUE) = TRUE
  AND dst.name ILIKE %(ds)s
  AND (fst.name ILIKE '%%Small Files%%' OR fst.name ILIKE '%%Small File%%')
  AND LOWER(COALESCE(b.scan_type,'')) LIKE '%%full scan%%'
  AND LOWER(COALESCE(b.scan_type,'')) NOT LIKE '%%ols%%'
GROUP BY 1
ORDER BY NULLIF(SUBSTRING(bv.version FROM '^([0-9]+)'), '')::int;
"""


def main():
    conn = psycopg2.connect(**DSN)
    cur = conn.cursor()
    for ds_label, ds_pat in (("S3", "%S3%"), ("SMB", "%SMB%")):
        print(f"\n===== {ds_label} — Small Files / Full Scan =====")
        print(f"{'major':<7}{'n_total':<9}{'n_acl_ff':<10}{'avg_ALL':<10}{'avg_BASE':<10}{'diff'}")
        cur.execute(SQL, {"ds": ds_pat})
        for major, n_total, n_acl_ff, avg_all, avg_base in cur.fetchall():
            a = float(avg_all) if avg_all is not None else None
            b = float(avg_base) if avg_base is not None else None
            diff = "" if (a is None or b is None) else f"{a - b:+.2f}"
            print(f"{major or '?':<7}{n_total:<9}{n_acl_ff:<10}"
                  f"{'' if a is None else f'{a:<10.2f}'}"
                  f"{'' if b is None else f'{b:<10.2f}'}{diff}")
    cur.close()
    conn.close()
    print("\nDashboard tooltip showed S3 build 255 = 13.33, SMB 255 = 11.01.")
    print("Whichever column (avg_ALL vs avg_BASE) matches those is the logic the Dashboard uses.")


if __name__ == "__main__":
    main()
