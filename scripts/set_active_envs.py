#!/usr/bin/env python3
"""
One-off: Set the two active BigID environments in test_environments.
Run from repo root: python scripts/set_active_envs.py
"""
import sys
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

from sqlalchemy import create_engine, text
from db_config import DB_CONNECTION_STRING

# Your two active envs (no trailing slash)
ENV1_URL = "http://a62d913d31e6c4deb98c9d4555a3bb2b-355451075.us-east-1.elb.amazonaws.com"
ENV2_URL = "http://a233f16bb07f94c43a6db619483c9120-984930002.us-east-1.elb.amazonaws.com"

ENVS = [
    (ENV1_URL, "e2e-perf"),
    (ENV2_URL, "e2e-perf2"),
]


def main():
    engine = create_engine(DB_CONNECTION_STRING)
    with engine.connect() as conn:
        # Ensure table exists
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS test_environments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                env_name VARCHAR(100) UNIQUE,
                base_url VARCHAR(500),
                version VARCHAR(100),
                auth_module_path VARCHAR(500),
                auth_function_name VARCHAR(100) DEFAULT 'Get_auth_token',
                classifiers_api_path VARCHAR(1200),
                is_active BOOLEAN DEFAULT TRUE,
                is_busy BOOLEAN DEFAULT FALSE,
                busy_by_run_id INT NULL,
                busy_since DATETIME NULL
            )
        """))
        for col, ddl in [
            ("is_busy", "ALTER TABLE test_environments ADD COLUMN is_busy BOOLEAN DEFAULT FALSE"),
            ("busy_by_run_id", "ALTER TABLE test_environments ADD COLUMN busy_by_run_id INT NULL"),
            ("busy_since", "ALTER TABLE test_environments ADD COLUMN busy_since DATETIME NULL"),
        ]:
            try:
                conn.execute(text(ddl))
            except Exception:
                pass
        conn.commit()

        # Deactivate all first
        conn.execute(text("UPDATE test_environments SET is_active = 0"))
        conn.commit()

        for base_url, env_name in ENVS:
            base_url = base_url.strip().rstrip("/")
            # Find by base_url or env_name
            r = conn.execute(
                text("SELECT id, env_name FROM test_environments WHERE base_url = :url OR env_name = :name LIMIT 1"),
                {"url": base_url, "name": env_name},
            ).fetchone()
            if r:
                eid, existing_name = r[0], r[1]
                # Update URL and active; set name only if no conflict (another row with same name but different id)
                conflict = conn.execute(
                    text("SELECT 1 FROM test_environments WHERE env_name = :name AND id != :id LIMIT 1"),
                    {"name": env_name, "id": eid},
                ).fetchone()
                if conflict:
                    conn.execute(
                        text("UPDATE test_environments SET base_url = :url, is_active = 1, is_busy = 0, busy_by_run_id = NULL, busy_since = NULL WHERE id = :id"),
                        {"url": base_url, "id": eid},
                    )
                    print(f"Updated env id={eid}: {existing_name!r} -> {base_url}")
                else:
                    conn.execute(
                        text("UPDATE test_environments SET base_url = :url, env_name = :name, is_active = 1, is_busy = 0, busy_by_run_id = NULL, busy_since = NULL WHERE id = :id"),
                        {"url": base_url, "name": env_name, "id": eid},
                    )
                    print(f"Updated env id={eid}: {env_name} -> {base_url}")
            else:
                conn.execute(
                    text("""
                        INSERT INTO test_environments (env_name, base_url, is_active, is_busy)
                        VALUES (:name, :url, 1, 0)
                    """),
                    {"name": env_name, "url": base_url},
                )
                print(f"Inserted env: {env_name} -> {base_url}")
        conn.commit()

        # List active
        rows = conn.execute(
            text("SELECT id, env_name, base_url, is_active FROM test_environments ORDER BY is_active DESC, env_name")
        ).fetchall()
        print("\nCurrent test_environments:")
        for r in rows:
            print(f"  id={r[0]} name={r[1]!r} active={r[3]} url={r[2][:60]}..." if r[2] and len(r[2]) > 60 else f"  id={r[0]} name={r[1]!r} active={r[3]} url={r[2]}")


if __name__ == "__main__":
    main()
