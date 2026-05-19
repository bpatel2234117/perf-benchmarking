#!/usr/bin/env python3
"""
Add more scan_progress snapshots for better time-series charts.
"""
import sys
sys.path.insert(0, 'v2_api')

from sqlalchemy import create_engine, text
from db_config_pg import DB_CONNECTION_STRING
from datetime import datetime, timedelta

engine = create_engine(DB_CONNECTION_STRING)

# Scan 2 needs more data points
scan_id = '69fc5dbd051cc7e3b7a16a4f'
env_id = 4
scan_name = '252-11 SMB Small Files (wf 68-4) Full Scan'
start_time = datetime(2026, 5, 7, 9, 49, 56)
total_objects = 18400
total_classifications = 9

with engine.connect() as conn:
    print(f"=== Adding scan_progress snapshots for scan {scan_id} ===")
    
    # Generate 10 snapshots over 40 minutes
    for i in range(10):
        timestamp = start_time + timedelta(minutes=i * 4)
        objects = int(total_objects * (i + 1) / 10)
        classifications = int(total_classifications * (i + 1) / 10)
        
        conn.execute(text("""
            INSERT INTO scan_progress
            (scan_id, scan_name, environment_id, timestamp, captured_at_utc,
             scan_state, completed_sub_scans, failed_sub_scans, total_sub_scans,
             completed_parts, failed_parts, total_parts_scanned,
             total_classifications, newly_added_classifications, 
             false_removed_classifications, objects_scanned, objects_failed, objects_with_pi)
            VALUES
            (:sid, :sname, :eid, :ts, :ts, 'Running', 0, 0, 0,
             0, 0, 0, :classifications, :classifications, 0, :objects, 0, 0)
            ON CONFLICT DO NOTHING
        """), {
            'sid': scan_id,
            'sname': scan_name,
            'eid': env_id,
            'ts': timestamp,
            'objects': objects,
            'classifications': classifications
        })
        
        print(f"  {i+1}/10: {timestamp} - {objects} objects, {classifications} classifications")
    
    conn.commit()
    print(f"✓ Added scan_progress snapshots for scan {scan_id}")

print("\n✓ Scan progress data populated!")
