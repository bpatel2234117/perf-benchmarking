import sys
import os
from sqlalchemy import create_engine, text

# Add local directory to path
sys.path.append(os.getcwd())

from db_config import DB_CONNECTION_STRING

def cleanup_version_history():
    engine = create_engine(DB_CONNECTION_STRING)
    with engine.connect() as conn:
        print("Starting deduplication of test_environment_version_history...")
        
        # This query identifies and deletes rows where the version_value and environment_id 
        # are identical to the preceding row (by ID).
        # We use a temporary table to handle the deletion safely in MySQL.
        
        clean_query = """
        DELETE h1 FROM test_environment_version_history h1
        JOIN (
            SELECT h_inner.id, 
                   LAG(h_inner.version_value) OVER (PARTITION BY h_inner.environment_id ORDER BY h_inner.id) as prev_version
            FROM test_environment_version_history h_inner
        ) h2 ON h1.id = h2.id
        WHERE h1.version_value = h2.prev_version
        """
        
        try:
            res = conn.execute(text(clean_query))
            conn.commit()
            print(f"Successfully removed {res.rowcount} redundant rows.")
        except Exception as e:
            print(f"Error during cleanup: {e}")

if __name__ == "__main__":
    cleanup_version_history()
