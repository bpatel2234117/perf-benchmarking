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
    'file_set_types': """
        CREATE TABLE IF NOT EXISTS file_set_types (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) UNIQUE,
            data_type VARCHAR(50)
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
