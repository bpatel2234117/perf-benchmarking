import os
import sys
import time
import hashlib
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# Ensure parent directory is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v2_api.db_config_pg import DB_CONNECTION_STRING, DDL_STATEMENTS, COLUMN_DROPS, COLUMN_ADDS, BACKFILL_SQL

# Set database URL env variable for seed_rbac_perms before importing it
os.environ['TRACKER_DATABASE_URL'] = DB_CONNECTION_STRING
from v2_api.seed_rbac_perms import ROLES, ALL_PAGES

def hash_password(password: str) -> str:
    return hashlib.sha256(password.strip().encode()).hexdigest()

def init():
    print("Connecting to PostgreSQL...")
    engine = create_engine(DB_CONNECTION_STRING, connect_args={"connect_timeout": 5})
    
    # Wait for DB to become available
    retries = 20
    connected = False
    for i in range(retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                connected = True
                print("Database is ready!")
                break
        except OperationalError:
            print(f"Waiting for database to start... ({i+1}/{retries})")
            time.sleep(2)
            
    if not connected:
        print("Could not connect to database. Exiting.")
        sys.exit(1)

    # 1. Initialize Tables
    print("Creating tables...")
    with engine.begin() as conn:
        for table_name, ddl in DDL_STATEMENTS.items():
            try:
                conn.execute(text(ddl))
                print(f"  Table '{table_name}' verified/created.")
            except Exception as e:
                print(f"  Error creating table '{table_name}': {e}")
                
    # 2. Apply schema migrations (idempotent: add columns, drop columns, backfill FKs)
    with engine.begin() as conn:
        for stmt in COLUMN_ADDS:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
    with engine.begin() as conn:
        for stmt in COLUMN_DROPS:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
    with engine.begin() as conn:
        for stmt in BACKFILL_SQL:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass

    # 3. Seed Roles
    print("Seeding app_roles...")
    with engine.begin() as conn:
        for role_id, role_name in ROLES.items():
            try:
                res = conn.execute(text("SELECT id FROM app_roles WHERE id = :id"), {"id": role_id}).fetchone()
                if not res:
                    conn.execute(
                        text("INSERT INTO app_roles (id, role_name) VALUES (:id, :name)"),
                        {"id": role_id, "name": role_name}
                    )
                    print(f"  Role '{role_name}' (ID {role_id}) inserted.")
            except Exception as e:
                print(f"  Error seeding role '{role_name}': {e}")

    # 4. Seed Page Permissions
    print("Seeding app_role_pages...")
    try:
        from v2_api.seed_rbac_perms import seed as seed_perms
        seed_perms()
    except Exception as e:
        print(f"  Error seeding permissions: {e}")

    # 5. Seed Default Admin User if not exists
    print("Checking default Admin user...")
    admin_user = "bpatel@bigid.com"
    admin_pass = "Benchmark@2026"
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text("SELECT id FROM app_users WHERE username = :u"),
                {"u": admin_user}
            ).fetchone()
            if not res:
                pwd_hash = hash_password(admin_pass)
                conn.execute(
                    text("INSERT INTO app_users (username, password_hash, role_id) VALUES (:u, :p, 1)"),
                    {"u": admin_user, "p": pwd_hash}
                )
                print(f"  Default admin user '{admin_user}' created.")
            else:
                print(f"  Admin user '{admin_user}' already exists.")
    except Exception as e:
        print(f"  Error seeding Admin user: {e}")

    print("Database initialization complete!")

if __name__ == "__main__":
    init()
