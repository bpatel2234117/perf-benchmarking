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
            busy_since TIMESTAMP NULL,
            bigid_username VARCHAR(100) NULL,
            bigid_password_enc TEXT NULL,
            created_by INT NULL,
            include_in_workflow_runs BOOLEAN DEFAULT TRUE,
            version_last_synced_at TIMESTAMP NULL
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
            file_set_type_id INT REFERENCES file_set_types(id),
            total_catalog_items BIGINT,
            avg_file_size VARCHAR(50),
            scan_start_time TIMESTAMP,
            scan_end_time TIMESTAMP,
            total_scan_hours REAL,
            throughput_cluster_gb_hr REAL,
            throughput_scanner_gb_hr REAL,
            throughput_thread_gb_hr REAL,
            throughput_cluster_files_hr REAL,
            throughput_scanner_files_hr REAL,
            throughput_thread_files_hr REAL,
            total_classification BIGINT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            is_release BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scan_id VARCHAR(128) NULL,
            created_by INT NULL
        );
    """,
    # Averaged throughput per major build per test scenario. Derived from benchmarks;
    # never modifies benchmarks. Recalculated by v2_api/benchmark_averages.py.
    'build_major_averages': """
        CREATE TABLE IF NOT EXISTS build_major_averages (
            id SERIAL PRIMARY KEY,
            major_build VARCHAR(20) NOT NULL,
            data_source_type VARCHAR(100) NOT NULL DEFAULT 'Unknown',
            file_set_type VARCHAR(150) NOT NULL DEFAULT 'Unknown',
            scan_type VARCHAR(100) NOT NULL DEFAULT 'unknown',
            is_acl BOOLEAN NOT NULL DEFAULT FALSE,
            is_feature_flags BOOLEAN NOT NULL DEFAULT FALSE,
            run_count INT DEFAULT 0,
            avg_total_scan_hours REAL,
            avg_throughput_cluster_gb_hr REAL,
            avg_throughput_scanner_gb_hr REAL,
            avg_throughput_thread_gb_hr REAL,
            avg_throughput_cluster_files_hr REAL,
            avg_throughput_scanner_files_hr REAL,
            avg_throughput_thread_files_hr REAL,
            min_throughput_cluster_gb_hr REAL,
            max_throughput_cluster_gb_hr REAL,
            stddev_throughput_cluster_gb_hr REAL,
            last_calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (major_build, data_source_type, file_set_type, scan_type, is_acl, is_feature_flags)
        );
    """,
    # Unified benchmark table: the single source of truth merging manual `benchmarks`
    # and legacy `auto_scan_benchmarks` (those two are kept as read-only backups).
    # Populated/maintained idempotently by v2_api/benchmark_unify.py.
    'benchmark_runs': """
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id SERIAL PRIMARY KEY,
            source VARCHAR(10) NOT NULL DEFAULT 'manual',
            legacy_table VARCHAR(32),
            legacy_id INT,
            scan_id VARCHAR(128),
            build_id INT REFERENCES builds(id),
            config_id INT REFERENCES configurations(id),
            classifier_set_id INT REFERENCES classifier_sets(id),
            data_source_type_id INT REFERENCES data_source_types(id),
            file_set_type_id INT REFERENCES file_set_types(id),
            environment_id INT REFERENCES test_environments(id),
            build_version VARCHAR(100),
            data_source_type VARCHAR(100),
            file_set_type VARCHAR(150),
            data_type VARCHAR(50),
            run_date VARCHAR(50),
            scenario_type VARCHAR(100),
            scan_type VARCHAR(100),
            scan_name VARCHAR(1000),
            scan_start_time TIMESTAMP,
            scan_end_time TIMESTAMP,
            total_scan_hours REAL,
            throughput_cluster_gb_hr REAL,
            throughput_scanner_gb_hr REAL,
            throughput_thread_gb_hr REAL,
            throughput_cluster_files_hr REAL,
            throughput_scanner_files_hr REAL,
            throughput_thread_files_hr REAL,
            total_catalog_items BIGINT,
            objects_scanned BIGINT,
            objects_with_pi BIGINT,
            objects_enumerated BIGINT,
            total_classification BIGINT,
            new_attributes INT,
            removed_attributes INT,
            classifier_attribute_names TEXT,
            total_size_gb REAL,
            total_files BIGINT,
            total_folders BIGINT,
            avg_file_size VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE,
            is_release BOOLEAN DEFAULT FALSE,
            created_by INT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (legacy_table, legacy_id)
        );
    """,
    'auto_scan_benchmarks': """
        CREATE TABLE IF NOT EXISTS auto_scan_benchmarks (
            id SERIAL PRIMARY KEY,
            scan_id VARCHAR(128) NOT NULL UNIQUE,
            environment_id INT REFERENCES test_environments(id),
            build_id INT NULL REFERENCES builds(id),
            data_source_type_id INT NULL REFERENCES data_source_types(id),
            file_set_type_id INT NULL REFERENCES file_set_types(id),
            build_version VARCHAR(100),
            run_date DATE,
            scenario_type VARCHAR(100),
            scan_type VARCHAR(100),
            data_source_type VARCHAR(100),
            data_type VARCHAR(50),
            file_set_type VARCHAR(150),
            scan_start_time TIMESTAMP,
            scan_end_time TIMESTAMP,
            total_scan_hours REAL,
            objects_with_pi BIGINT,
            objects_scanned BIGINT,
            objects_enumerated BIGINT,
            total_classification BIGINT,
            new_attributes INT,
            removed_attributes INT,
            classifier_attribute_names TEXT,
            scan_name VARCHAR(1000),
            total_size_gb REAL,
            total_files BIGINT,
            total_folders BIGINT,
            throughput_cluster_gb_hr REAL,
            throughput_cluster_files_hr REAL,
            is_active BOOLEAN DEFAULT TRUE,
            is_release BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INT NULL
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
    'scan_classifier_snapshot': """
        CREATE TABLE IF NOT EXISTS scan_classifier_snapshot (
            id SERIAL PRIMARY KEY,
            scan_id VARCHAR(120) NOT NULL UNIQUE,
            environment_id INT REFERENCES test_environments(id) ON DELETE SET NULL,
            current_build_id INT REFERENCES builds(id) ON DELETE SET NULL,
            previous_build_id INT REFERENCES builds(id) ON DELETE SET NULL,
            current_build_version TEXT,
            previous_build_version TEXT,
            total_count INT NOT NULL DEFAULT 0,
            previous_total_count INT NOT NULL DEFAULT 0,
            added_count INT NOT NULL DEFAULT 0,
            removed_count INT NOT NULL DEFAULT 0,
            changed_count INT NOT NULL DEFAULT 0,
            added_names JSONB DEFAULT '[]',
            removed_names JSONB DEFAULT '[]',
            changed_names JSONB DEFAULT '[]',
            captured_at TIMESTAMPTZ DEFAULT NOW()
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
            log_category VARCHAR(20) DEFAULT 'unknown',
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
            cooldown_minutes INT DEFAULT 10,
            next_eligible_at TIMESTAMP NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scan_template_id VARCHAR(128) NULL,
            created_by INT NULL,
            ols_enabled BOOLEAN DEFAULT FALSE,
            env_pinned BOOLEAN DEFAULT FALSE
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
            scan_id VARCHAR(128) NULL,
            status VARCHAR(32) NULL,
            last_error TEXT NULL,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            scan_waiting_since TIMESTAMP NULL,
            scan_waiting_retry_count INT DEFAULT 0,
            failover_count INT DEFAULT 0,
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
            last_seen_at TIMESTAMP NULL,
            manually_stopped_at TIMESTAMP NULL
        );
    """,
    'scan_progress': """
        CREATE TABLE IF NOT EXISTS scan_progress (
            id SERIAL PRIMARY KEY,
            scan_id VARCHAR(128) NOT NULL,
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
            completed_scan_parts BIGINT DEFAULT 0,
            failed_scan_parts BIGINT DEFAULT 0,
            total_scan_parts BIGINT DEFAULT 0,
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

# Columns removed in schema cleanup — applied by init_db on startup so existing DBs stay in sync.
COLUMN_DROPS = [
    # benchmarks: legacy/migration-only columns never read or written by v2 code
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS data_source_category",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS enum_start_time",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS enum_end_time",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS enumeration_hours",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS classifier_list",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS removed_attributes",
    # benchmarks: volume/data_type moved to file_set_types (canonical); queries JOIN fst.*
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS total_files",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS total_folders",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS total_size_gb",
    "ALTER TABLE benchmarks DROP COLUMN IF EXISTS data_type",
    # configurations: comment was never written by v2; always NULL
    "ALTER TABLE configurations DROP COLUMN IF EXISTS comment",
    # scan_progress: scan_name duplicates scan_parents.scan_name; queries now JOIN scan_parents
    "ALTER TABLE scan_progress DROP COLUMN IF EXISTS scan_name",
    # workflow_steps: build_prefix is derivable from build_version via _build_prefix()
    "ALTER TABLE workflow_steps DROP COLUMN IF EXISTS build_prefix",
]

# ADD COLUMN migrations for FK normalization (idempotent — IF NOT EXISTS)
COLUMN_ADDS = [
    # Set by "Mark stopped" (POST /api/scan/mark-stopped). Upserts keep such a scan at Stopped.
    "ALTER TABLE IF EXISTS scan_parents ADD COLUMN IF NOT EXISTS manually_stopped_at TIMESTAMP NULL",
    "ALTER TABLE IF EXISTS scan_progress ADD COLUMN IF NOT EXISTS completed_scan_parts BIGINT DEFAULT 0",
    "ALTER TABLE IF EXISTS scan_progress ADD COLUMN IF NOT EXISTS failed_scan_parts BIGINT DEFAULT 0",
    "ALTER TABLE IF EXISTS scan_progress ADD COLUMN IF NOT EXISTS total_scan_parts BIGINT DEFAULT 0",
    # Categorize logged scan errors: auth_error | connection_error | scan_error | timeout | parse_error | unknown
    "ALTER TABLE scan_log_errors ADD COLUMN IF NOT EXISTS log_category VARCHAR(20) DEFAULT 'unknown'",
    # scanner_individual_performance is created by the tracker; older tables predate these
    # columns. IF EXISTS guards the case where the API DB doesn't have the table yet (so this
    # never aborts the rest of COLUMN_ADDS). Fixes "column \"name\" does not exist" on the
    # Scanner Infrastructure page.
    "ALTER TABLE IF EXISTS scanner_individual_performance ADD COLUMN IF NOT EXISTS name VARCHAR(255) NULL",
    "ALTER TABLE IF EXISTS scanner_individual_performance ADD COLUMN IF NOT EXISTS cpu_pct DOUBLE PRECISION NULL",
    "ALTER TABLE IF EXISTS scanner_individual_performance ADD COLUMN IF NOT EXISTS available_processors INT NULL",
    # BigID-reported service status per scanner. Without it the Scanner Infrastructure page
    # has to infer health from thread counts, which marks a dead pod (frozen threads/CPU)
    # as active. scanner_performance.build_individual_insert fills it once the column exists.
    "ALTER TABLE IF EXISTS scanner_individual_performance ADD COLUMN IF NOT EXISTS status VARCHAR(64) NULL",
    # scanner_group_performance is likewise created by the tracker; the read endpoints
    # (/api/scanner-metrics/performance and /api/scanner-performance/group) SELECT this
    # column, so its absence 500s the Scanner Infrastructure page ("Failed to load scanner
    # metrics"). IF EXISTS guards the case where the API DB doesn't have the table yet.
    "ALTER TABLE IF EXISTS scanner_group_performance ADD COLUMN IF NOT EXISTS group_scanner_ids_json TEXT NULL",
    "ALTER TABLE auto_scan_benchmarks ADD COLUMN IF NOT EXISTS build_id INT REFERENCES builds(id)",
    "ALTER TABLE auto_scan_benchmarks ADD COLUMN IF NOT EXISTS data_source_type_id INT REFERENCES data_source_types(id)",
    "ALTER TABLE auto_scan_benchmarks ADD COLUMN IF NOT EXISTS file_set_type_id INT REFERENCES file_set_types(id)",
    # Encrypted BigID credentials stored per-environment in DB
    "ALTER TABLE test_environments ADD COLUMN IF NOT EXISTS bigid_username VARCHAR(100) NULL",
    "ALTER TABLE test_environments ADD COLUMN IF NOT EXISTS bigid_password_enc TEXT NULL",
    "ALTER TABLE test_environments ADD COLUMN IF NOT EXISTS created_by INT NULL",
    # workflow_templates.created_by is read/written by main.py but was missing from DDL
    "ALTER TABLE workflow_templates ADD COLUMN IF NOT EXISTS created_by INT NULL",
    "ALTER TABLE test_environments ADD COLUMN IF NOT EXISTS include_in_workflow_runs BOOLEAN DEFAULT TRUE",
    # BUG FIX 4.3: Track when version was last synced (for periodic re-sync every 30 minutes)
    "ALTER TABLE test_environments ADD COLUMN IF NOT EXISTS version_last_synced_at TIMESTAMP NULL",
    # Workflow step scan_waiting tracking (for timeout detection)
    "ALTER TABLE workflow_steps ADD COLUMN IF NOT EXISTS scan_waiting_since TIMESTAMP NULL",
    "ALTER TABLE workflow_steps ADD COLUMN IF NOT EXISTS scan_waiting_retry_count INT DEFAULT 0",
    # Workflow step failover tracking
    "ALTER TABLE workflow_steps ADD COLUMN IF NOT EXISTS failover_count INT DEFAULT 0",
    # env_pinned=TRUE means env1_id/env2_id were chosen by the user at create time: the run
    # executes ONLY on those envs (no substitution, no failover to an unselected env) and no
    # other run may use them. FALSE with empty slots = no selection: the run uses every
    # available pool env, recomputed per tick.
    "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS env_pinned BOOLEAN DEFAULT FALSE",
    # builds: release tagging (MySQL parity — drives Major Release Benchmark Trends report)
    "ALTER TABLE builds ADD COLUMN IF NOT EXISTS release_milestone VARCHAR(50) NULL",
    "ALTER TABLE builds ADD COLUMN IF NOT EXISTS quarter_label VARCHAR(50) NULL",
]

# Backfill FK columns from existing text values (only touches NULL rows — safe to re-run)
BACKFILL_SQL = [
    """UPDATE auto_scan_benchmarks ab
       SET build_id = bu.id
       FROM builds bu
       WHERE bu.version = ab.build_version AND ab.build_id IS NULL""",
    """UPDATE auto_scan_benchmarks ab
       SET data_source_type_id = dst.id
       FROM data_source_types dst
       WHERE dst.name = ab.data_source_type AND ab.data_source_type_id IS NULL""",
    """UPDATE auto_scan_benchmarks ab
       SET file_set_type_id = fst.id
       FROM file_set_types fst
       WHERE fst.name = ab.file_set_type AND ab.file_set_type_id IS NULL""",
    # Data isolation: existing rows predate created_by and were all created by the admin.
    # Assign them to the Admin user (role_id = 1) so they remain visible after isolation
    # is enforced (non-Admins now only see their own rows). Idempotent (WHERE created_by IS NULL).
    """UPDATE test_environments SET created_by = (SELECT MIN(id) FROM app_users WHERE role_id = 1)
       WHERE created_by IS NULL AND EXISTS (SELECT 1 FROM app_users WHERE role_id = 1)""",
    """UPDATE benchmarks SET created_by = (SELECT MIN(id) FROM app_users WHERE role_id = 1)
       WHERE created_by IS NULL AND EXISTS (SELECT 1 FROM app_users WHERE role_id = 1)""",
    """UPDATE auto_scan_benchmarks SET created_by = (SELECT MIN(id) FROM app_users WHERE role_id = 1)
       WHERE created_by IS NULL AND EXISTS (SELECT 1 FROM app_users WHERE role_id = 1)""",
    """UPDATE workflow_runs SET created_by = (SELECT MIN(id) FROM app_users WHERE role_id = 1)
       WHERE created_by IS NULL AND EXISTS (SELECT 1 FROM app_users WHERE role_id = 1)""",
    """UPDATE workflow_templates SET created_by = (SELECT MIN(id) FROM app_users WHERE role_id = 1)
       WHERE created_by IS NULL AND EXISTS (SELECT 1 FROM app_users WHERE role_id = 1)""",
]

# Idempotent reference data for Add Benchmark prefill (inferred Postgres Mixed Tables label).
DATA_SEEDS = [
    """INSERT INTO file_set_types (name, data_type, total_files, total_folders, total_size_gb)
       SELECT 'Postgres Mixed Tables', 'Structured', 10000, 10000, 20.0
       WHERE NOT EXISTS (
         SELECT 1 FROM file_set_types WHERE LOWER(TRIM(name)) = 'postgres mixed tables'
       )""",
    """UPDATE file_set_types
       SET total_size_gb = 20.0, total_files = COALESCE(total_files, 10000),
           total_folders = COALESCE(total_folders, 10000), data_type = COALESCE(data_type, 'Structured')
       WHERE LOWER(TRIM(name)) = 'postgres mixed tables'
         AND (total_size_gb IS NULL OR total_size_gb <= 0)""",
]


# Canonical list of RBAC page permissions (single source of truth). seed_rbac_perms.py
# seeds these into app_role_pages, main.py exposes them via GET /api/rbac/all-pages, and
# the Manage RBAC UI renders a checkbox per entry. Keep this list authoritative so the
# backend seed and the frontend editor never drift apart.
RBAC_ALL_PAGES = [
    "Overview", "Build Analysis", "Comparison", "Build DS Runs", "Data Source Trend Comparison",
    "Small Files Trend Analysis", "Large Files Trend Analysis", "Small Files OLS Trend", "Large Files OLS Trend",
    "OLS vs Full Scan (Small)", "OLS vs Full Scan (Large)", "Discover File FF On vs Off",
    "ACL ON vs OLS (ACL ON)", "ACL OFF vs OLS (ACL OFF)", "Edit Records", "Manage Classifiers",
    "Classifier Changes", "Manage Test Environments", "Version Comparison", "Workflow Automation",
    "Create Data Source", "Scan Status", "Scan Progress",
]
