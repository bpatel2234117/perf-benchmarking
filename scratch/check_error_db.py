import sys
import os
from sqlalchemy import create_engine, text

# Add current dir to path
sys.path.append(os.getcwd())

try:
    from db_config import DB_CONNECTION_STRING
except ImportError:
    print("Could not import DB_CONNECTION_STRING from db_config.py")
    sys.exit(1)

engine = create_engine(DB_CONNECTION_STRING)

query = """
SELECT id, environment_id, log_timestamp, error_context, ai_analysis 
FROM scan_log_errors 
WHERE error_context LIKE :pat OR ai_analysis LIKE :pat 
ORDER BY id DESC LIMIT 10;
"""

try:
    with engine.connect() as conn:
        result = conn.execute(text(query), {"pat": "%socket hang up%"}).fetchall()
        if not result:
            print("No matching errors found in scan_log_errors.")
        else:
            print(f"Found {len(result)} matching error(s):")
            for row in result:
                print("-" * 50)
                print(f"ID: {row[0]}")
                print(f"Env ID: {row[1]}")
                print(f"Timestamp: {row[2]}")
                print(f"Context: {row[3][:500]}...")
                print(f"AI Analysis: {str(row[4])[:500]}...")
except Exception as e:
    print(f"Error querying database: {e}")
