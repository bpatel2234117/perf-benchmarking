#!/usr/bin/env python3
"""
Seed the 'ENABLE OLS FEATURE FLAGS' action template into workflow_templates table.

This action template will be executed by workflow_runner when it encounters
an action step with notes="ENABLE OLS FEATURE FLAGS".

The template makes API calls to BigID to enable OLS-related feature flags.
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import json

# Load environment variables
load_dotenv()

# Import DB config
from v2_api.db_config_pg import DB_CONNECTION_STRING


def seed_ols_action_template():
    """
    Seed the ENABLE OLS FEATURE FLAGS action template.
    
    This template enables OLS (Object Level Security) feature flags on BigID.
    The actual API endpoint and payload may need to be adjusted based on BigID version.
    """
    
    engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)
    
    # Action template key (matches the notes field in workflow step)
    action_key = "ENABLE OLS FEATURE FLAGS"
    
    # Action payload - this is a JSON template that will be executed by run_action_template()
    # Variables available: BASE_URL, TOKEN
    # 
    # IMPORTANT: This is a PLACEHOLDER template. You need to update it with the actual
    # BigID API endpoint and payload for enabling OLS feature flags.
    # 
    # Common BigID API patterns for feature flags:
    # - PUT /api/v1/system-settings with {"key": "value"}
    # - PUT /api/v1/configurations with feature flag settings
    # - POST /api/v1/feature-toggles with {"feature": "ols", "enabled": true}
    #
    # Check your BigID API documentation or use the BigID UI network tab to find the correct endpoint.
    #
    # Example placeholder (UPDATE THIS):
    action_payload = [
        {
            "method": "PUT",
            "path": "/api/v1/system-settings",  # UPDATE THIS PATH
            "body": {
                # UPDATE THESE SETTINGS based on your BigID version
                "ols.enabled": True,
                "objectLevelSecurity.enabled": True,
                # Add other OLS-related settings here
            }
        }
    ]
    
    payload_json = json.dumps(action_payload, indent=2)
    
    # Upsert into workflow_templates table
    with engine.begin() as conn:
        # Check if table exists
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
        
        print(f"✓ Seeded action template: {action_key}")
        print(f"  Payload: {payload_json}")


if __name__ == "__main__":
    print("Seeding OLS action template...")
    seed_ols_action_template()
    print("Done!")
