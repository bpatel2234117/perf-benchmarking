#!/usr/bin/env python3
"""
Add ols_enabled column to workflow_runs table.

This column tracks whether OLS feature flags have been enabled for a workflow,
so subsequent DS/scan names can automatically include "OLS" in the name.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()


def add_ols_enabled_column(db_connection_string, db_name=""):
    """Add ols_enabled column to workflow_runs table if it doesn't exist."""
    
    engine = create_engine(db_connection_string, pool_pre_ping=True)
    
    try:
        with engine.begin() as conn:
            # Try to add the column (will fail silently if it already exists)
            try:
                conn.execute(text("""
                    ALTER TABLE workflow_runs 
                    ADD COLUMN ols_enabled BOOLEAN DEFAULT FALSE
                """))
                print(f"✓ Added ols_enabled column to workflow_runs in {db_name}")
            except Exception as e:
                if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                    print(f"  Column already exists in {db_name}")
                else:
                    raise
    except Exception as e:
        print(f"✗ Failed to add column in {db_name}: {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("Adding ols_enabled column to workflow_runs")
    print("=" * 60)
    
    # Add to v2 API (React/Postgres)
    try:
        from v2_api.db_config_pg import DB_CONNECTION_STRING as V2_DB
        print("\n[1/2] Updating v2 API database (React/Postgres)...")
        add_ols_enabled_column(V2_DB, "v2 API")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
    
    # Add to Streamlit (MySQL/SQLite)
    try:
        from db_config import get_db_connection_string
        streamlit_db = get_db_connection_string()
        print("\n[2/2] Updating Streamlit database (MySQL/SQLite)...")
        add_ols_enabled_column(streamlit_db, "Streamlit")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
