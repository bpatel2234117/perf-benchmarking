# Database Configuration
# UPDATE THESE VALUES to match your Windows MySQL instance

DB_CONFIG = {
    'host': '10.2.20.89',  # Replace with the IP address of your Windows machine
    'port': 3306,             # Default MySQL port
    'user': 'bigid',           # Your MySQL username
    'password': 'bigid111!',   # Your MySQL password
    'database': 'benchmark_db' # The database name you want to use
}

# Connection String
DB_CONNECTION_STRING = f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"

# Table Definitions (V4 Normalization)
DDL_STATEMENTS = {
    'data_source_types': """
        CREATE TABLE IF NOT EXISTS data_source_types (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(50) UNIQUE
        );
    """,
    'build_templates': """
        CREATE TABLE IF NOT EXISTS build_templates (
            id INT AUTO_INCREMENT PRIMARY KEY,
            build_id INT,
            template_name VARCHAR(100),
            classifier_set TEXT,
            UNIQUE KEY uniq_build_template (build_id, template_name),
            FOREIGN KEY (build_id) REFERENCES builds(id)
        );
    """,
    'test_environments': """
        CREATE TABLE IF NOT EXISTS test_environments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            env_name VARCHAR(100) UNIQUE,
            base_url VARCHAR(500),
            version VARCHAR(100),
            auth_module_path VARCHAR(500),
            auth_function_name VARCHAR(100) DEFAULT 'Get_auth_token',
            classifiers_api_path VARCHAR(1200),
            is_active BOOLEAN DEFAULT TRUE
        );
    """,
    'test_environment_version_history': """
        CREATE TABLE IF NOT EXISTS test_environment_version_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            environment_id INT NOT NULL,
            version_value VARCHAR(100),
            classifier_set_id INT NULL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            change_note VARCHAR(255),
            FOREIGN KEY (environment_id) REFERENCES test_environments(id)
        );
    """,
    'classifier_sets': """
        CREATE TABLE IF NOT EXISTS classifier_sets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            set_hash VARCHAR(64) UNIQUE,
            classifier_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """,
    'classifier_set_members': """
        CREATE TABLE IF NOT EXISTS classifier_set_members (
            id INT AUTO_INCREMENT PRIMARY KEY,
            set_id INT NOT NULL,
            classifier_id INT NOT NULL,
            UNIQUE KEY uniq_set_classifier (set_id, classifier_id),
            FOREIGN KEY (set_id) REFERENCES classifier_sets(id),
            FOREIGN KEY (classifier_id) REFERENCES classifiers(id)
        );
    """,
    'file_set_types': """
        CREATE TABLE IF NOT EXISTS file_set_types (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) UNIQUE,
            data_type VARCHAR(50),
            total_files BIGINT,
            total_folders BIGINT,
            total_size_gb FLOAT
        );
    """,
    'builds': """
        CREATE TABLE IF NOT EXISTS builds (
            id INT AUTO_INCREMENT PRIMARY KEY,
            version VARCHAR(50) UNIQUE
        );
    """,
    'classifiers': """
        CREATE TABLE IF NOT EXISTS classifiers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE
        );
    """,
    'build_classifier_mappings': """
        CREATE TABLE IF NOT EXISTS build_classifier_mappings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            build_id INT NOT NULL,
            classifier_id INT NOT NULL,
            environment_id INT NULL,
            last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_build_classifier (build_id, classifier_id),
            FOREIGN KEY (build_id) REFERENCES builds(id),
            FOREIGN KEY (classifier_id) REFERENCES classifiers(id),
            FOREIGN KEY (environment_id) REFERENCES test_environments(id)
        );
    """,
    'configurations': """
        CREATE TABLE IF NOT EXISTS configurations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            scanner_count INT,
            thread_count INT,
            is_acl BOOLEAN DEFAULT FALSE,
            is_ocr BOOLEAN DEFAULT FALSE,
            is_feature_flags BOOLEAN DEFAULT FALSE,
            feature_flags TEXT,
            comment TEXT,
            hash VARCHAR(32) UNIQUE -- To prevent duplicate configs
        );
    """,
    'benchmarks': """
        CREATE TABLE IF NOT EXISTS benchmarks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            build_id INT,
            config_id INT,
            classifier_id INT,
            classifier_list TEXT,
            run_date VARCHAR(50),
            scenario_type VARCHAR(100),
            scan_type VARCHAR(100),
            data_source_type VARCHAR(50),
            data_source_category VARCHAR(50),
            file_set_type VARCHAR(100),
            data_type VARCHAR(50),
            total_files BIGINT,
            total_folders BIGINT,
            total_size_gb FLOAT,
            total_catalog_items BIGINT,
            avg_file_size VARCHAR(50),
            scan_start_time DATETIME,
            scan_end_time DATETIME,
            enum_start_time DATETIME,
            enum_end_time DATETIME,
            total_scan_hours FLOAT,
            enumeration_hours FLOAT,
            throughput_cluster_gb_hr FLOAT,
            throughput_scanner_gb_hr FLOAT,
            throughput_thread_gb_hr FLOAT,
            throughput_cluster_files_hr FLOAT,
            throughput_scanner_files_hr FLOAT,
            throughput_thread_files_hr FLOAT,
            is_active BOOLEAN DEFAULT TRUE,
            FOREIGN KEY (build_id) REFERENCES builds(id),
            FOREIGN KEY (config_id) REFERENCES configurations(id),
            FOREIGN KEY (classifier_id) REFERENCES classifiers(id)
        );
    """,
    'benchmark_findings': """
        CREATE TABLE IF NOT EXISTS benchmark_findings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            benchmark_id INT,
            classifier_name VARCHAR(255),
            finding_count BIGINT,
            FOREIGN KEY (benchmark_id) REFERENCES benchmarks(id)
        );
    """
}
