import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Ensure we can import from v2_api and the root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load old MySQL config from root
try:
    from db_config import DB_CONNECTION_STRING as MYSQL_URI
except ImportError:
    print("Could not import MySQL configuration from db_config.py")
    sys.exit(1)

# Load new Postgres config
from v2_api.db_config_pg import DB_CONNECTION_STRING as PG_URI, DDL_STATEMENTS

def is_postgresql(conn):
    """Check if connection is PostgreSQL."""
    try:
        return 'postgresql' in str(conn.engine.url).lower()
    except:
        return False

def run_migration():
    print("Starting Database Migration: MySQL -> PostgreSQL")
    
    mysql_engine = create_engine(MYSQL_URI)
    pg_engine = create_engine(PG_URI)
    
    # 1. Initialize Postgres Tables
    print("Initializing PostgreSQL schema...")
    with pg_engine.begin() as pg_conn:
        for table_name, ddl in DDL_STATEMENTS.items():
            try:
                pg_conn.execute(text(ddl))
            except Exception as e:
                print(f"Error creating table {table_name}: {e}")
    
    # 1b. Initialize tracker-specific tables (scan_progress, scanner_performance, etc.)
    print("Initializing tracker tables...")
    try:
        from v2_api.tracker import ensure_scan_tracking_tables, ensure_scan_progress_table
        ensure_scan_tracking_tables(pg_engine)
        ensure_scan_progress_table(pg_engine)
        print("  Tracker tables initialized successfully")
    except Exception as e:
        print(f"  Error initializing tracker tables: {e}")

    # 2. Migrate Data (Table by Table based on dependencies)
    tables_to_migrate = [
        # Core reference tables
        "data_source_types", "builds", "classifiers", "file_set_types",
        "classifier_sets", "classifier_set_members", "build_templates",
        
        # Environment and configuration
        "test_environments", "test_environment_version_history",
        "build_classifier_mappings", "configurations",
        
        # Benchmarks
        "benchmarks", "benchmark_findings", "auto_scan_benchmarks",
        
        # Classifier snapshots
        "classifier_details_snapshots",
        
        # Scan tracking (for React UI scan status page)
        "scan_parents", "scan_progress", "scan_insights",
        "scan_classifier_attributes", "scan_insight_attribute_delta_map",
        
        # Scanner performance (for React UI performance charts)
        "scanner_group_performance", "scanner_individual_performance",
        
        # Logs and errors
        "scan_log_errors", "scan_log_correlations",
        
        # Tracker metadata
        "tracker_heartbeat", "tracker_kv",
        
        # RBAC and settings
        "app_roles", "app_users", "app_role_pages", "app_settings",
        
        # Workflow orchestration
        "workflow_runs", "workflow_steps", "workflow_templates"
    ]
    
    with mysql_engine.connect() as mysql_conn:
        for table in tables_to_migrate:
            print(f"Migrating table: {table}...")
            
            # Use separate transaction per table to avoid cascading failures
            try:
                # Fetch all rows from MySQL
                result = mysql_conn.execute(text(f"SELECT * FROM {table}"))
                rows = result.fetchall()
                
                if not rows:
                    print(f"  No data found in {table}, skipping.")
                    continue
                    
                # Get column names
                columns = result.keys()
                col_names = ", ".join(columns)
                val_placeholders = ", ".join([f":{c}" for c in columns])
                
                # Insert into Postgres (separate transaction per table)
                with pg_engine.begin() as pg_conn:
                    insert_query = text(f"INSERT INTO {table} ({col_names}) VALUES ({val_placeholders})")
                    
                    # Convert rows to dicts for SQLAlchemy execution
                    data_dicts = [dict(zip(columns, row)) for row in rows]
                    
                    # Convert MySQL integer booleans (0/1) to PostgreSQL booleans (True/False)
                    boolean_columns_by_table = {
                        'test_environments': ['is_active', 'is_busy', 'include_in_workflow_runs'],
                        'configurations': ['is_acl', 'is_ocr', 'is_feature_flags'],
                        'benchmarks': ['is_active', 'is_release'],
                        'auto_scan_benchmarks': ['is_active'],
                        'scan_parents': ['is_active'],
                        'scan_progress': [],  # No boolean columns
                        'scan_insights': [],  # No boolean columns
                    }
                    
                    if table in boolean_columns_by_table:
                        for d in data_dicts:
                            for bool_col in boolean_columns_by_table[table]:
                                if bool_col in d and d[bool_col] is not None:
                                    d[bool_col] = bool(d[bool_col])
                    
                    # Handle schema differences (columns that exist in MySQL but not in PostgreSQL)
                    columns_to_remove = {
                        'builds': ['release_milestone', 'quarter_label'],
                        'build_templates': ['classifier_set'],  # Replaced with classifier_set_id
                        'benchmark_findings': ['classifier_attribute', 'attribute_value'],  # Removed in new schema
                    }
                    
                    if table in columns_to_remove:
                        for d in data_dicts:
                            for col in columns_to_remove[table]:
                                d.pop(col, None)
                    
                    # Notice: Because we normalized benchmarks (removed total_files etc), 
                    # we must map old columns to the new schema for benchmarks and findings.
                    # Since this is a generic script, we'll need to handle the normalized tables explicitly if columns don't match.
                    
                    if table == "benchmarks":
                        # Map data_source_type to ID
                        # First get mappings
                        ds_types = pg_conn.execute(text("SELECT id, name FROM data_source_types")).fetchall()
                        ds_map = {r[1]: r[0] for r in ds_types}
                        
                        fs_types = pg_conn.execute(text("SELECT id, name FROM file_set_types")).fetchall()
                        fs_map = {r[1]: r[0] for r in fs_types}
                        
                        mapped_dicts = []
                        for row in rows:
                            d = dict(zip(columns, row))
                            # Remove old columns
                            dst_name = d.pop("data_source_type", None)
                            fst_name = d.pop("file_set_type", None)
                            d.pop("data_type", None)
                            d.pop("total_files", None)
                            d.pop("total_folders", None)
                            d.pop("total_size_gb", None)
                            d.pop("classifier_id", None)
                            d.pop("classifier_list", None)
                            
                            # Add new columns
                            d["data_source_type_id"] = ds_map.get(dst_name) if dst_name else None
                            d["file_set_type_id"] = fs_map.get(fst_name) if fst_name else None
                            # We can't perfectly recover classifier_set_id from raw strings easily here without parsing logic, 
                            # so we leave classifier_set_id NULL for legacy imports
                            d["classifier_set_id"] = None
                            
                            mapped_dicts.append(d)
                            
                        if mapped_dicts:
                            new_cols = list(mapped_dicts[0].keys())
                            col_names = ", ".join(new_cols)
                            val_placeholders = ", ".join([f":{c}" for c in new_cols])
                            insert_query = text(f"INSERT INTO benchmarks ({col_names}) VALUES ({val_placeholders})")
                            pg_conn.execute(insert_query, mapped_dicts)
                        print(f"  Successfully mapped and migrated {len(rows)} rows to benchmarks.")
                        continue
                        
                    elif table == "benchmark_findings":
                        classifiers = pg_conn.execute(text("SELECT id, name FROM classifiers")).fetchall()
                        c_map = {r[1]: r[0] for r in classifiers}
                        
                        mapped_dicts = []
                        for row in rows:
                            d = dict(zip(columns, row))
                            c_name = d.pop("classifier_name", None)
                            d["classifier_id"] = c_map.get(c_name)
                            mapped_dicts.append(d)
                            
                        if mapped_dicts:
                            new_cols = list(mapped_dicts[0].keys())
                            col_names = ", ".join(new_cols)
                            val_placeholders = ", ".join([f":{c}" for c in new_cols])
                            insert_query = text(f"INSERT INTO benchmark_findings ({col_names}) VALUES ({val_placeholders})")
                            pg_conn.execute(insert_query, mapped_dicts)
                        print(f"  Successfully mapped and migrated {len(rows)} rows to benchmark_findings.")
                        continue
                        
                    elif table == "classifier_details_snapshots":
                        classifiers = pg_conn.execute(text("SELECT id, name FROM classifiers")).fetchall()
                        c_map = {r[1]: r[0] for r in classifiers}
                        
                        mapped_dicts = []
                        for row in rows:
                            d = dict(zip(columns, row))
                            c_name = d.pop("classifier_name", None)
                            d["classifier_id"] = c_map.get(c_name)
                            mapped_dicts.append(d)
                            
                        if mapped_dicts:
                            new_cols = list(mapped_dicts[0].keys())
                            col_names = ", ".join(new_cols)
                            val_placeholders = ", ".join([f":{c}" for c in new_cols])
                            insert_query = text(f"INSERT INTO classifier_details_snapshots ({col_names}) VALUES ({val_placeholders})")
                            pg_conn.execute(insert_query, mapped_dicts)
                        print(f"  Successfully mapped and migrated {len(rows)} rows to classifier_details_snapshots.")
                        continue
                        
                    # Default execution for all other normal tables
                    # Use ON CONFLICT DO NOTHING to skip duplicates (for re-running migration)
                    if is_postgresql(pg_conn):
                        # Get primary key or unique constraint to use for ON CONFLICT
                        conflict_keys = {
                            'data_source_types': 'id',
                            'builds': 'id',
                            'classifiers': 'id',
                            'file_set_types': 'id',
                            'classifier_sets': 'id',
                            'classifier_set_members': 'id',
                            'test_environment_version_history': 'id',
                            'build_classifier_mappings': 'id',
                            'classifier_details_snapshots': 'id',
                        }
                        
                        if table in conflict_keys:
                            # Rebuild query with ON CONFLICT
                            new_cols = list(data_dicts[0].keys())
                            col_names = ", ".join(new_cols)
                            val_placeholders = ", ".join([f":{c}" for c in new_cols])
                            insert_query = text(f"INSERT INTO {table} ({col_names}) VALUES ({val_placeholders}) ON CONFLICT ({conflict_keys[table]}) DO NOTHING")
                    
                    pg_conn.execute(insert_query, data_dicts)
                    print(f"  Successfully migrated {len(rows)} rows to {table}.")
                    
            except Exception as e:
                print(f"  Error migrating {table}: {e}")
                    
    print("\nMigration Complete!")

if __name__ == "__main__":
    run_migration()
