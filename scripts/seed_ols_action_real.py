#!/usr/bin/env python3
"""
Seed the 'action::pre_ols' template for enabling OLS feature flags.

This template will be executed BEFORE each OLS scan step to ensure
OLS feature flags are enabled on the target environment.

Run this on BOTH Streamlit and React v2 API databases.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import json

load_dotenv()


def seed_pre_ols_action(db_connection_string, db_name=""):
    """
    Seed the pre-OLS action template.
    
    This action enables OLS-related feature flags on BigID before running OLS scans.
    
    IMPORTANT: Update the API endpoint and payload based on your BigID version.
    The placeholder below needs to be replaced with the actual BigID API call.
    """
    
    engine = create_engine(db_connection_string, pool_pre_ping=True)
    
    # Global action key (applies to all environments)
    action_key = "action::pre_ols"
    
    # TODO: Replace this with the actual BigID API call for enabling OLS
    # Check BigID API documentation or use browser DevTools to find the correct endpoint
    #
    # Common patterns:
    # - PUT /api/v1/system-settings
    # - PUT /api/v1/configurations  
    # - POST /api/v1/feature-toggles
    #
    # Example placeholder (UPDATE THIS):
    action_payload = {
        "method": "PUT",
        "path": "/api/v1/system-settings",  # UPDATE THIS
        "body": {
            # UPDATE THESE SETTINGS
            "ols.enabled": True,
            "objectLevelSecurity.enabled": True,
            "scan.ols.enabled": True
        }
    }
    
    payload_json = json.dumps(action_payload, indent=2)
    
    # Upsert into workflow_templates table
    with engine.begin() as conn:
        # Ensure table exists
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                k VARCHAR(200) PRIMARY KEY,
                template_kind VARCHAR(32) NULL,
                payload_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        # Upsert the template
        conn.execute(text("""
            INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
            VALUES (:k, :kind, :payload, CURRENT_TIMESTAMP)
            ON CONFLICT (k) DO UPDATE SET
                template_kind = EXCLUDED.template_kind,
                payload_json = EXCLUDED.payload_json,
                updated_at = CURRENT_TIMESTAMP
        """), {
            "k": action_key,
            "kind": "action",
            "payload": payload_json
        })
        
        print(f"✓ Seeded pre-OLS action template in {db_name}: {action_key}")
        print(f"  Payload: {payload_json}")


if __name__ == "__main__":
    print("=" * 60)
    print("Seeding Pre-OLS Action Template")
    print("=" * 60)
    
    # Seed for v2 API (React/Postgres)
    try:
        from v2_api.db_config_pg import DB_CONNECTION_STRING as V2_DB
        print("\n[1/2] Seeding v2 API database (React/Postgres)...")
        seed_pre_ols_action(V2_DB, "v2 API")
    except Exception as e:
        print(f"  ✗ Failed to seed v2 API database: {e}")
    
    # Seed for Streamlit (MySQL/SQLite)
    try:
        from db_config import get_db_connection_string
        streamlit_db = get_db_connection_string()
        print("\n[2/2] Seeding Streamlit database (MySQL/SQLite)...")
        seed_pre_ols_action(streamlit_db, "Streamlit")
    except Exception as e:
        print(f"  ✗ Failed to seed Streamlit database: {e}")
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
    print("\nIMPORTANT: The template contains a PLACEHOLDER API call.")
    print("You MUST update it with the actual BigID API endpoint.")
    print("\nTo update:")
    print("1. Find the correct BigID API endpoint (check docs or browser DevTools)")
    print("2. Edit this script and update the action_payload")
    print("3. Run this script again to update the database")
    print("\nOr update directly in Streamlit:")
    print("- Go to Workflow Automation page")
    print("- Click on an OLS step")
    print("- Edit the 'Pre-OLS action (FF change)' JSON")
