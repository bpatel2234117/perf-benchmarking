import sys
import os
from sqlalchemy import create_engine, text

# Add current dir to path
sys.path.append(os.getcwd())

try:
    from db_config import DB_CONNECTION_STRING
    engine = create_engine(DB_CONNECTION_STRING)
    
    query = """
    SELECT id, log_source, log_timestamp, error_context 
    FROM scan_log_errors 
    WHERE log_source = 'orch2' AND log_timestamp > DATE_SUB(NOW(), INTERVAL 1 DAY)
    ORDER BY id DESC LIMIT 50;
    """
    
    with engine.connect() as conn:
        result = conn.execute(text(query)).fetchall()
        print(f"Total results: {len(result)}")
        for row in result:
            print(f"ID: {row[0]}")
            print(f"Timestamp: {row[2]}")
            print(f"Context Start: {str(row[3])[:100]}...")
            print("-" * 20)
            
except Exception as e:
    print(f"FAILED: {e}")
