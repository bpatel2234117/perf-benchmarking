import os
from dotenv import load_dotenv

load_dotenv()

# Database Configuration for Postgres
DB_CONFIG = {
    'host': os.getenv('Postgres_host', '10.2.20.89'),
    'port': int(os.getenv('Postgres_port', 5432)),
    'user': os.getenv('Postgres_username', 'postgres'),
    'password': os.getenv('Postgres_password', 'bigid111!'),
    'database': os.getenv('DB_NAME', 'benchmark_db')
}

# Postgres Connection String
DB_CONNECTION_STRING = f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"

# Normalized DDL Statements for PostgreSQL
DDL_STATEMENTS = {
    'data_source_types': """
        CREATE TABLE IF NOT EXISTS data_source_types (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50) UNIQUE NOT NULL
        );
    """,
    'builds': """
        CREATE TABLE IF NOT EXISTS builds (
            id SERIAL PRIMARY KEY,
            version VARCHAR(50) UNIQUE NOT NULL,
            release_milestone INT NULL,
            quarter_label VARCHAR(50) NULL
        );
    """,
    'classifiers': """
        CREATE TABLE IF NOT EXISTS classifiers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL
        );
    """,
    'file_set_types': """
        CREATE TABLE IF NOT EXISTS file_set_types (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) UNIQUE NOT NULL,
            data_type VARCHAR(50),
            total_files BIGINT,
            total_folders BIGINT,
            total_size_gb REAL
        );
    """,
    'classifier_sets': """
        CREATE TABLE IF NOT EXISTS classifier_sets (
            id SERIAL PRIMARY KEY,
            set_hash VARCHAR(64) UNIQUE NOT NULL,
            classifier_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'classifier_set_members': """
        CREATE TABLE IF NOT EXISTS classifier_set_members (
            id SERIAL PRIMARY KEY,
            set_id INT NOT NULL REFERENCES classifier_sets(id) ON DELETE CASCADE,
            classifier_id INT NOT NULL REFERENCES classifiers(id) ON DELETE CASCADE,
            UNIQUE (set_id, classifier_id)
        );
    """,
    'build_templates': """
        CREATE TABLE IF NOT EXISTS build_templates (
            id SERIAL PRIMARY KEY,
            build_id INT NOT NULL REFERENCES builds(id),
            template_name VARCHAR(100) NOT NULL,
            classifier_set_id INT REFERENCES classifier_sets(id),
            UNIQUE (build_id, template_name)
        );
    """,
    'test_environments': """
        CREATE TABLE IF NOT EXISTS test_environments (
            id SERIAL PRIMARY KEY,
            env_name VARCHAR(100) UNIQUE NOT NULL,
            base_url VARCHAR(500),
            version VARCHAR(100),
            auth_module_path VARCHAR(500),
            auth_function_name VARCHAR(100) DEFAULT 'Get_auth_token',
            classifiers_api_path VARCHAR(1200),
            is_active BOOLEAN DEFAULT TRUE,
            is_busy BOOLEAN DEFAULT FALSE,
            busy_by_run_id INT NULL,
            busy_since TIMESTAMP NULL
        );
    """,
    'test_environment_version_history': """
        CREATE TABLE IF NOT EXISTS test_environment_version_history (
            id SERIAL PRIMARY KEY,
            environment_id INT NOT NULL REFERENCES test_environments(id) ON DELETE CASCADE,
            version_value VARCHAR(100),
            classifier_set_id INT REFERENCES classifier_sets(id),
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            change_note VARCHAR(255)
        );
    """,
    'build_classifier_mappings': """
        CREATE TABLE IF NOT EXISTS build_classifier_mappings (
            id SERIAL PRIMARY KEY,
            build_id INT NOT NULL REFERENCES builds(id),
            classifier_id INT NOT NULL REFERENCES classifiers(id),
            environment_id INT REFERENCES test_environments(id),
            last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (build_id, classifier_id, environment_id)
        );
    """,
    'configurations': """
        CREATE TABLE IF NOT EXISTS configurations (
            id SERIAL PRIMARY KEY,
            scanner_count INT,
            thread_count INT,
            is_acl BOOLEAN DEFAULT FALSE,
            is_ocr BOOLEAN DEFAULT FALSE,
            is_feature_flags BOOLEAN DEFAULT FALSE,
            feature_flags TEXT,
            comment TEXT,
            hash VARCHAR(32) UNIQUE
        );
    """,
    'benchmarks': """
        CREATE TABLE IF NOT EXISTS benchmarks (
            id SERIAL PRIMARY KEY,
            build_id INT REFERENCES builds(id),
            config_id INT REFERENCES configurations(id),
            classifier_set_id INT REFERENCES classifier_sets(id),
            run_date VARCHAR(50),
            scenario_type VARCHAR(100),
            scan_type VARCHAR(100),
            data_source_type_id INT REFERENCES data_source_types(id),
            data_source_category VARCHAR(50),
            file_set_type_id INT REFERENCES file_set_types(id),
            total_catalog_items BIGINT,
            avg_file_size VARCHAR(50),
            scan_start_time TIMESTAMP,
            scan_end_time TIMESTAMP,
            enum_start_time TIMESTAMP,
            enum_end_time TIMESTAMP,
            total_scan_hours REAL,
            enumeration_hours REAL,
            throughput_cluster_gb_hr REAL,
            throughput_scanner_gb_hr REAL,
            throughput_thread_gb_hr REAL,
            throughput_cluster_files_hr REAL,
            throughput_scanner_files_hr REAL,
            throughput_thread_files_hr REAL,
            is_active BOOLEAN DEFAULT TRUE,
            is_release BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scan_id VARCHAR(128) NULL
        );
    """,
    'benchmark_findings': """
        CREATE TABLE IF NOT EXISTS benchmark_findings (
            id SERIAL PRIMARY KEY,
            benchmark_id INT REFERENCES benchmarks(id) ON DELETE CASCADE,
            classifier_id INT REFERENCES classifiers(id),
            finding_count BIGINT
        );
    """,
    'classifier_details_snapshots': """
        CREATE TABLE IF NOT EXISTS classifier_details_snapshots (
            id SERIAL PRIMARY KEY,
            environment_id INT REFERENCES test_environments(id),
            build_id INT NOT NULL REFERENCES builds(id),
            bigid_classifier_id VARCHAR(64) NOT NULL,
            classifier_id INT REFERENCES classifiers(id),
            snapshot_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (environment_id, build_id, bigid_classifier_id)
        );
    """,
    'scan_log_errors': """
        CREATE TABLE IF NOT EXISTS scan_log_errors (
            id SERIAL PRIMARY KEY,
            environment_id INT NOT NULL REFERENCES test_environments(id),
            log_timestamp VARCHAR(100),
            error_context TEXT,
            ai_analysis TEXT,
            log_source VARCHAR(50) DEFAULT 'scanner',
            scan_id VARCHAR(50),
            subscan_id VARCHAR(50),
            error_hash VARCHAR(32),
            is_resolved BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'scan_log_correlations': """
        CREATE TABLE IF NOT EXISTS scan_log_correlations (
            id SERIAL PRIMARY KEY,
            environment_id INT NOT NULL REFERENCES test_environments(id),
            scan_id VARCHAR(50),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            correlation_report TEXT,
            is_final BOOLEAN DEFAULT FALSE
        );
    """,
    'app_roles': """
        CREATE TABLE IF NOT EXISTS app_roles (
            id SERIAL PRIMARY KEY,
            role_name VARCHAR(50) UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'app_users': """
        CREATE TABLE IF NOT EXISTS app_users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role_id INT NOT NULL REFERENCES app_roles(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'app_role_pages': """
        CREATE TABLE IF NOT EXISTS app_role_pages (
            id SERIAL PRIMARY KEY,
            role_id INT NOT NULL REFERENCES app_roles(id),
            page_name VARCHAR(100) NOT NULL,
            UNIQUE (role_id, page_name)
        );
    """,
    'app_settings': """
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key VARCHAR(100) PRIMARY KEY,
            setting_value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'workflow_runs': """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NULL,
            status VARCHAR(32) NULL,
            env1_id INT NULL,
            env2_id INT NULL,
            cooldown_minutes INT DEFAULT 15,
            next_eligible_at TIMESTAMP NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scan_template_id VARCHAR(128) NULL
        );
    """,
    'workflow_steps': """
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id SERIAL PRIMARY KEY,
            run_id INT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            seq INT NOT NULL,
            environment_id INT NULL,
            ds_kind VARCHAR(64) NULL,
            ds_name VARCHAR(500) NULL,
            scan_kind VARCHAR(64) NULL,
            file_set_type VARCHAR(150) NULL,
            is_enabled BOOLEAN DEFAULT TRUE,
            is_acl BOOLEAN DEFAULT FALSE,
            is_ocr BOOLEAN DEFAULT FALSE,
            is_structured_ff BOOLEAN DEFAULT FALSE,
            is_ols BOOLEAN DEFAULT FALSE,
            is_lite BOOLEAN DEFAULT FALSE,
            feature_flags VARCHAR(500) NULL,
            notes VARCHAR(1000) NULL,
            ds_id VARCHAR(128) NULL,
            scan_profile_id VARCHAR(128) NULL,
            scan_profile_name VARCHAR(500) NULL,
            build_version VARCHAR(50) NULL,
            build_prefix VARCHAR(20) NULL,
            scan_id VARCHAR(128) NULL,
            status VARCHAR(32) NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, seq)
        );
    """,
    'workflow_templates': """
        CREATE TABLE IF NOT EXISTS workflow_templates (
            k VARCHAR(200) PRIMARY KEY,
            template_kind VARCHAR(32) NULL,
            payload_json TEXT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    # Tracker / Streamlit parity (MySQL→Postgres sync); explicit ids preserved for upserts.
    'scan_parents': """
        CREATE TABLE IF NOT EXISTS scan_parents (
            id SERIAL PRIMARY KEY,
            scan_id VARCHAR(128) NOT NULL UNIQUE,
            environment_id INT NULL REFERENCES test_environments(id),
            scan_name VARCHAR(1000) NULL,
            scan_type VARCHAR(120) NULL,
            started_at VARCHAR(80) NULL,
            completed_at VARCHAR(80) NULL,
            template_name VARCHAR(255) NULL,
            scan_status VARCHAR(120) NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP NULL,
            updated_at TIMESTAMP NULL,
            last_seen_at TIMESTAMP NULL
        );
    """,
    'scan_progress': """
        CREATE TABLE IF NOT EXISTS scan_progress (
            id SERIAL PRIMARY KEY,
            scan_id VARCHAR(128) NOT NULL,
            scan_name VARCHAR(1000) NULL,
            environment_id INT NULL REFERENCES test_environments(id),
            "timestamp" TIMESTAMP NULL,
            captured_at_utc TIMESTAMP NULL,
            scan_state VARCHAR(64) NULL,
            completed_sub_scans BIGINT DEFAULT 0,
            failed_sub_scans BIGINT DEFAULT 0,
            total_sub_scans BIGINT DEFAULT 0,
            completed_parts BIGINT DEFAULT 0,
            failed_parts BIGINT DEFAULT 0,
            total_parts_scanned BIGINT DEFAULT 0,
            total_classifications BIGINT DEFAULT 0,
            newly_added_classifications BIGINT DEFAULT 0,
            false_removed_classifications BIGINT DEFAULT 0,
            objects_scanned BIGINT DEFAULT 0,
            objects_failed BIGINT DEFAULT 0,
            objects_with_pi BIGINT DEFAULT 0
        );
    """,
    # Populated by Scan Status "Sync" (BigID insights API) / tracker parity.
    'scan_insights': """
        CREATE TABLE IF NOT EXISTS scan_insights (
            scan_id VARCHAR(128) PRIMARY KEY,
            distinct_classification_attributes BIGINT NULL,
            classifier_attribute_names TEXT NULL,
            data_source_names TEXT NULL,
            objects_with_pi BIGINT NULL,
            objects_scanned BIGINT NULL,
            objects_enumerated BIGINT NULL,
            total_classification BIGINT NULL,
            new_attributes_count BIGINT NULL,
            removed_attributes_count BIGINT NULL,
            total_size_gb REAL NULL,
            insight_payload TEXT NULL,
            attributes_payload TEXT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
}
