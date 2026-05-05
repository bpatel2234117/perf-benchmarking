import os
import re

files_to_migrate = [
    'tracker.py',
    'workflow_runner.py',
    'ai_log_fetcher.py'
]

for filename in files_to_migrate:
    src_path = os.path.join('/Users/bpatel/AI/Scan_Performance_Benchmark', filename)
    dest_path = os.path.join('/Users/bpatel/AI/Scan_Performance_Benchmark/v2_api', filename)
    
    if not os.path.exists(src_path):
        print(f"File {src_path} not found.")
        continue
        
    with open(src_path, 'r') as f:
        content = f.read()
        
    # Replace imports
    # from db_config import ...
    content = re.sub(r'from db_config import', 'from v2_api.db_config_pg import', content)
    # import db_config
    content = re.sub(r'import db_config', 'from v2_api import db_config_pg as db_config', content)
    # db_config.
    content = re.sub(r'db_config\.', 'db_config.', content) # Handles the 'as db_config' alias
    
    # Also handle some SQL syntax differences in standard python code if possible, 
    # but we'll start with just the DB config replacement.
    # MySQL uses %s for parametrization in pymysql, Postgres uses %(name)s or %s (psycopg2).
    # SQLAlchemy uses :name for text(), which is engine agnostic. The original code uses text("... :param").
    
    with open(dest_path, 'w') as f:
        f.write(content)
        
    print(f"Migrated {filename} to v2_api/")
