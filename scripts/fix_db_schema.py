#!/usr/bin/env python3
"""Apply schema patches to the real Postgres (10.2.20.89) and print row counts."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

# Force data host — never the empty EC2-local copy
os.environ["Postgres_host"] = os.environ.get("Postgres_host", "10.2.20.89")
if os.environ["Postgres_host"] in ("10.2.0.93", "127.0.0.1", "localhost"):
    print("WARNING: Postgres_host points at empty local DB; forcing 10.2.20.89")
    os.environ["Postgres_host"] = "10.2.20.89"

from sqlalchemy import create_engine, text

from v2_api.db_config_pg import BACKFILL_SQL, COLUMN_ADDS, DB_CONFIG, DB_CONNECTION_STRING
from v2_api.schema_bootstrap import apply_schema_migrations

def main() -> None:
    print(f"Connecting: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    engine = create_engine(DB_CONNECTION_STRING, connect_args={"connect_timeout": 10})
    apply_schema_migrations(engine, force=True)
    with engine.begin() as conn:
        for stmt in BACKFILL_SQL:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
    with engine.connect() as conn:
        env_n = conn.execute(text("SELECT COUNT(*) FROM test_environments")).scalar()
        bench_n = conn.execute(text("SELECT COUNT(*) FROM benchmarks")).scalar()
        builds_n = conn.execute(text("SELECT COUNT(*) FROM builds")).scalar()
    print(f"test_environments={env_n} benchmarks={bench_n} builds={builds_n}")
    if int(env_n or 0) == 0:
        print("ERROR: no environments — API will show empty tables")
        sys.exit(1)
    print("OK")

if __name__ == "__main__":
    main()
