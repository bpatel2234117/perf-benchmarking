import sys
import os
from sqlalchemy import create_engine, text

sys.path.append(os.getcwd())

try:
    from db_config import DB_CONNECTION_STRING
    engine = create_engine(DB_CONNECTION_STRING)
    
    # Query for the last failed steps
    query = """
    SELECT id, run_id, step_seq, ds_name, cur_status, last_error, updated_at 
    FROM workflow_steps 
    WHERE cur_status = 'failed'
    ORDER BY id DESC LIMIT 10;
    """
    
    with engine.connect() as conn:
        result = conn.execute(text(query)).fetchall()
        print(f"Total failed steps: {len(result)}")
        for row in result:
            print(f"ID: {row[0]}, RunID: {row[1]}, Seq: {row[2]}, DS: {row[3]}")
            print(f"Status: {row[4]}")
            print(f"Error: {row[5]}")
            print(f"Updated: {row[6]}")
            print("-" * 20)
            
except Exception as e:
    print(f"FAILED: {e}")
