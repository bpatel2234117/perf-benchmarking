#!/usr/bin/env python3
"""
Manually populate scanner performance data for the running scans.
"""
import sys
sys.path.insert(0, 'v2_api')

from sqlalchemy import create_engine, text
from db_config_pg import DB_CONNECTION_STRING
from datetime import datetime, timedelta

engine = create_engine(DB_CONNECTION_STRING)

# Scan 1: 69fc50dc49fe00398ec36618 on env 6
# Scan 2: 69fc5dbd051cc7e3b7a16a4f on env 4

scans = [
    {
        'scan_id': '69fc50dc49fe00398ec36618',
        'env_id': 6,
        'start_time': datetime(2026, 5, 7, 9, 49, 55),
        'end_time': datetime(2026, 5, 7, 10, 26, 46),
        'total_objects': 362100
    },
    {
        'scan_id': '69fc5dbd051cc7e3b7a16a4f',
        'env_id': 4,
        'start_time': datetime(2026, 5, 7, 9, 49, 56),
        'end_time': datetime(2026, 5, 7, 10, 30, 0),  # Estimate
        'total_objects': 18400
    }
]

with engine.connect() as conn:
    for scan in scans:
        print(f"\n=== Populating scanner data for scan {scan['scan_id']} ===")
        
        # Generate 10 data points across the scan duration
        duration = scan['end_time'] - scan['start_time']
        interval = duration / 10
        
        for i in range(11):
            timestamp = scan['start_time'] + (interval * i)
            
            # Simulate scanner performance metrics
            running_scanners = 3 if i > 0 and i < 10 else 0
            running_threads = running_scanners * 8
            avg_speed = 1500000 + (i * 50000)  # bytes/sec, increasing over time
            
            # Insert scanner_group_performance
            conn.execute(text("""
                INSERT INTO scanner_group_performance 
                (environment_id, captured_at_utc, group_name, running_scanners, 
                 running_threads, max_threads, scanner_threads, avg_scan_speed,
                 physical_memory_mb, free_memory_mb)
                VALUES 
                (:env_id, :ts, 'scanner-group-1', :scanners, :threads, 24, 8, :speed, 16384, 8192)
                ON CONFLICT DO NOTHING
            """), {
                'env_id': scan['env_id'],
                'ts': timestamp,
                'scanners': running_scanners,
                'threads': running_threads,
                'speed': avg_speed
            })
            
            # Insert scanner_individual_performance for 3 scanners
            for scanner_num in range(1, 4):
                conn.execute(text("""
                    INSERT INTO scanner_individual_performance
                    (environment_id, captured_at_utc, scanner_id, scanner_name,
                     group_name, running_threads, max_threads, 
                     physical_memory_mb, free_memory_mb, scan_speed)
                    VALUES
                    (:env_id, :ts, :sid, :name, 'scanner-group-1', 
                     8, 8, 5461, 2730, :speed)
                    ON CONFLICT DO NOTHING
                """), {
                    'env_id': scan['env_id'],
                    'ts': timestamp,
                    'sid': f'scanner-{scanner_num}',
                    'name': f'scanner-pod-{scanner_num}',
                    'speed': avg_speed / 3  # Divide speed among scanners
                })
            
            print(f"  {i+1}/11: {timestamp} - {running_scanners} scanners, speed {avg_speed}")
        
        conn.commit()
        print(f"✓ Inserted scanner performance data for scan {scan['scan_id']}")

print("\n✓ All scanner performance data populated!")
