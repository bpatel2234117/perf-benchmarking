#!/usr/bin/env python3
import sys
sys.path.insert(0, 'v2_api')
from sqlalchemy import create_engine, text
from db_config_pg import DB_CONNECTION_STRING

engine = create_engine(DB_CONNECTION_STRING)

print('=' * 80)
print('SCANNER PERFORMANCE DATA SUMMARY')
print('=' * 80)

with engine.connect() as conn:
    for scan_id, scan_name in [
        ('69fc50dc49fe00398ec36618', 'Scan on Env 6 (S3)'),
        ('69fc5dbd051cc7e3b7a16a4f', 'Scan on Env 4 (SMB)')
    ]:
        print(f'\n📊 {scan_name}')
        print(f'   Scan ID: {scan_id}')
        
        # Get environment
        result = conn.execute(text('''
            SELECT environment_id FROM scan_parents WHERE scan_id = :sid
        '''), {'sid': scan_id})
        env_row = result.fetchone()
        if not env_row:
            print('   ⚠️  No environment found')
            continue
        env_id = env_row[0]
        
        # Scan progress data
        result = conn.execute(text('''
            SELECT COUNT(*), MIN(captured_at_utc), MAX(captured_at_utc),
                   MAX(objects_scanned), MAX(total_classifications)
            FROM scan_progress WHERE scan_id = :sid
        '''), {'sid': scan_id})
        sp = result.fetchone()
        print(f'   Environment: {env_id}')
        print(f'   Scan Progress Snapshots: {sp[0]}')
        print(f'   Time Range: {sp[1]} to {sp[2]}')
        print(f'   Objects Scanned: {sp[3]:,}')
        print(f'   Classifications: {sp[4]}')
        
        # Scanner group performance
        result = conn.execute(text('''
            SELECT COUNT(*), 
                   AVG(running_scanners), 
                   AVG(running_threads),
                   AVG(avg_scan_speed)
            FROM scanner_group_performance 
            WHERE environment_id = :eid
              AND captured_at_utc BETWEEN :start AND :end
        '''), {'eid': env_id, 'start': sp[1], 'end': sp[2]})
        sgp = result.fetchone()
        print(f'   Scanner Group Snapshots: {sgp[0]}')
        if sgp[0] > 0:
            print(f'   Avg Running Scanners: {sgp[1]:.1f}')
            print(f'   Avg Running Threads: {sgp[2]:.1f}')
            print(f'   Avg Scan Speed: {sgp[3]:,.0f} B/s ({sgp[3]/1024/1024:.2f} MB/s)')
        
        # Scanner individual performance
        result = conn.execute(text('''
            SELECT COUNT(*), COUNT(DISTINCT scanner_id)
            FROM scanner_individual_performance 
            WHERE environment_id = :eid
              AND captured_at_utc BETWEEN :start AND :end
        '''), {'eid': env_id, 'start': sp[1], 'end': sp[2]})
        sip = result.fetchone()
        print(f'   Scanner Individual Snapshots: {sip[0]}')
        print(f'   Unique Scanners: {sip[1]}')
        
        # List scanners
        if sip[1] > 0:
            result = conn.execute(text('''
                SELECT DISTINCT scanner_id, scanner_name
                FROM scanner_individual_performance 
                WHERE environment_id = :eid
                  AND captured_at_utc BETWEEN :start AND :end
                ORDER BY scanner_id
            '''), {'eid': env_id, 'start': sp[1], 'end': sp[2]})
            print(f'   Scanner Pods:')
            for r in result.fetchall():
                print(f'     - {r[0]} ({r[1]})')

print('\n' + '=' * 80)
print('✅ All data is ready for React UI charts!')
print('=' * 80)
print('\n📝 Next Steps:')
print('1. Start FastAPI: python3 -m uvicorn main:app --host 127.0.0.1 --port 8000')
print('2. Refresh React UI to see the charts populated')
print('3. Charts will show:')
print('   - Progress over time (Objects, Classifications)')
print('   - Scanner performance (Group metrics)')
print('   - Individual scanner pod metrics')
