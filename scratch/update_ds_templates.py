
import json
import sys
import os
sys.path.append(os.getcwd())
from sqlalchemy import create_engine, text
from db_config import DB_CONNECTION_STRING

engine = create_engine(DB_CONNECTION_STRING)

def update_templates():
    with engine.begin() as conn:
        # 1. Fetch base template (using the one we already have)
        res = conn.execute(text("SELECT payload_json FROM workflow_templates WHERE k='ds::s3::CSV Files::acl=0::struct=0'")).fetchone()
        if not res:
            print("Base template not found!")
            return
        
        base_json = json.loads(res[0])
        
        def create_payload(acl, struct, description=None):
            p = json.loads(json.dumps(base_json)) # deep copy
            # Update description
            if description:
                p["ds_connection"]["description"] = description
            else:
                p["ds_connection"]["description"] = "{{DS_NAME}} - {{FILE_SET_TYPE}} (ACL={{IS_ACL}}, Struct={{IS_STRUCTURED_FF}})"
            
            # Update ACL/Struct flags
            # Based on the base JSON structure seen earlier:
            # metadataAclScanEnabled is in ds_connection
            # enableStructuredFilesDiscovery is in ds_connection.typeRequestProperties
            
            p["ds_connection"]["metadataAclScanEnabled"] = "true" if acl else "false"
            p["ds_connection"]["dsAclScanEnabled"] = "true" if acl else "false" # Some versions use this
            
            if "typeRequestProperties" not in p["ds_connection"]:
                p["ds_connection"]["typeRequestProperties"] = {}
            
            p["ds_connection"]["typeRequestProperties"]["enableStructuredFilesDiscovery"] = bool(struct)
            
            # Ensure draft is false
            p["isSavedAsDraft"] = False
            return json.dumps(p)

        # Keys to update/insert
        templates = [
            # S3 CSV
            ("ds::s3::CSV Files::acl=0::struct=0", 0, 0),
            ("ds::s3::CSV Files::acl=0::struct=1", 0, 1),
            ("ds::s3::CSV Files::acl=1::struct=0", 1, 0),
            ("ds::s3::CSV Files::acl=1::struct=1", 1, 1),
            
            # Aliases for file-system (to fix the user's missing DS issue)
            ("ds::file-system::CSV Files::acl=0::struct=0", 0, 0),
            ("ds::file-system::CSV Files::acl=0::struct=1", 0, 1),
            ("ds::file-system::CSV Files::acl=1::struct=0", 1, 0),
            ("ds::file-system::CSV Files::acl=1::struct=1", 1, 1),
        ]
        
        for k, acl, struct in templates:
            payload = create_payload(acl, struct)
            conn.execute(text("""
                INSERT INTO workflow_templates (k, template_kind, payload_json, updated_at)
                VALUES (:k, 'ds', :payload, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE 
                    payload_json = VALUES(payload_json),
                    updated_at = CURRENT_TIMESTAMP
            """), {"k": k, "payload": payload})
            print(f"Updated template: {k}")

if __name__ == "__main__":
    update_templates()
