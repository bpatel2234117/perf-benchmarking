"""
PostgreSQL DDL and helpers when TRACKER_USE_POSTGRES / TRACKER_DATABASE_URL targets Postgres.
"""
from __future__ import annotations

from sqlalchemy import text

TRACKER_HEARTBEAT_DDL = """
CREATE TABLE IF NOT EXISTS tracker_heartbeat (
    name VARCHAR(64) PRIMARY KEY,
    last_started_at_utc TIMESTAMP NULL,
    last_finished_at_utc TIMESTAMP NULL,
    last_status VARCHAR(16) NULL,
    last_message VARCHAR(2000) NULL
)
"""

TRACKER_KV_DDL = """
CREATE TABLE IF NOT EXISTS tracker_kv (
    k VARCHAR(200) PRIMARY KEY,
    v VARCHAR(2000) NULL,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SCAN_CLASSIFIER_ATTR_PG = """
CREATE TABLE IF NOT EXISTS scan_classifier_attributes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(500) NOT NULL UNIQUE
)
"""

SCAN_INSIGHT_DELTA_PG = """
CREATE TABLE IF NOT EXISTS scan_insight_attribute_delta_map (
    scan_id VARCHAR(128) NOT NULL,
    attribute_id INT NOT NULL,
    is_new BOOLEAN DEFAULT FALSE,
    is_removed BOOLEAN DEFAULT FALSE,
    finding_count BIGINT NULL,
    attribute_value VARCHAR(500) NULL,
    new_count_value BIGINT NULL,
    removed_count_value BIGINT NULL,
    PRIMARY KEY (scan_id, attribute_id),
    FOREIGN KEY (attribute_id) REFERENCES scan_classifier_attributes(id)
)
"""

AUTO_BENCHMARK_PG = """
CREATE TABLE IF NOT EXISTS auto_scan_benchmarks (
    id SERIAL PRIMARY KEY,
    scan_id VARCHAR(128) NOT NULL UNIQUE,
    environment_id INT NULL,
    build_version VARCHAR(100) NULL,
    run_date DATE NULL,
    scenario_type VARCHAR(100) NULL,
    scan_type VARCHAR(100) NULL,
    data_source_type VARCHAR(100) NULL,
    data_type VARCHAR(50) NULL,
    file_set_type VARCHAR(150) NULL,
    scan_start_time TIMESTAMP NULL,
    scan_end_time TIMESTAMP NULL,
    total_scan_hours REAL NULL,
    total_size_gb REAL NULL,
    total_files BIGINT NULL,
    total_folders BIGINT NULL,
    objects_with_pi BIGINT NULL,
    objects_scanned BIGINT NULL,
    objects_enumerated BIGINT NULL,
    total_classification BIGINT NULL,
    new_attributes BIGINT NULL,
    removed_attributes BIGINT NULL,
    classifier_attribute_names TEXT NULL,
    scan_name VARCHAR(1000) NULL,
    throughput_cluster_gb_hr REAL NULL,
    throughput_cluster_files_hr REAL NULL,
    is_active BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SCANNER_GROUP_PG = """
CREATE TABLE IF NOT EXISTS scanner_group_performance (
    id SERIAL PRIMARY KEY,
    environment_id INT NOT NULL,
    captured_at_utc TIMESTAMP NOT NULL,
    group_name VARCHAR(255) NULL,
    running_scanners INT DEFAULT 0,
    running_threads INT DEFAULT 0,
    max_threads INT DEFAULT 0,
    scanner_threads INT DEFAULT 0,
    physical_memory_mb BIGINT DEFAULT 0,
    free_memory_mb BIGINT DEFAULT 0,
    avg_scan_speed REAL DEFAULT 0,
    group_scanner_ids_json TEXT NULL,
    update_date_bigid TIMESTAMP NULL
)
"""

SCANNER_INDIV_PG = """
CREATE TABLE IF NOT EXISTS scanner_individual_performance (
    id SERIAL PRIMARY KEY,
    environment_id INT NOT NULL,
    captured_at_utc TIMESTAMP NOT NULL,
    scanner_id VARCHAR(255) NULL,
    name VARCHAR(255) NULL,
    status VARCHAR(64) NULL,
    group_name VARCHAR(255) NULL,
    running_threads INT DEFAULT 0,
    total_num_of_threads INT DEFAULT 0,
    system_load_average REAL DEFAULT 0,
    available_processors INT NULL,
    physical_memory_mb BIGINT DEFAULT 0,
    free_memory_mb BIGINT DEFAULT 0,
    scanner_threads INT DEFAULT 0,
    cpu_pct DOUBLE PRECISION NULL,
    update_date_bigid TIMESTAMP NULL
)
"""


def ensure_postgresql_tracker_schema(engine) -> None:
    """Create tracker-related tables using PostgreSQL DDL (aligned with v2_api.db_config_pg + migrations)."""
    parts: list[str] = []
    try:
        from v2_api.db_config_pg import DDL_STATEMENTS

        for key in ("scan_parents", "scan_insights", "scan_progress"):
            if key in DDL_STATEMENTS:
                parts.append(DDL_STATEMENTS[key])
    except Exception:
        pass

    parts.extend(
        [
            TRACKER_HEARTBEAT_DDL,
            TRACKER_KV_DDL,
            SCAN_CLASSIFIER_ATTR_PG,
            SCAN_INSIGHT_DELTA_PG,
            AUTO_BENCHMARK_PG,
            SCANNER_GROUP_PG,
            SCANNER_INDIV_PG,
        ]
    )

    with engine.begin() as conn:
        for stmt in parts:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        for alter in (
            "ALTER TABLE scanner_individual_performance ADD COLUMN cpu_pct DOUBLE PRECISION NULL",
            "ALTER TABLE scanner_group_performance ADD COLUMN group_scanner_ids_json TEXT NULL",
            "ALTER TABLE scanner_individual_performance ADD COLUMN available_processors INT NULL",
        ):
            try:
                conn.execute(text(alter))
            except Exception:
                pass
        # Match MySQL uniq_scan_slot (nullable captured_at allows multiple NULL in PG — tracker uses NOT NULL slots for writes)
        try:
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_scan_slot
                    ON scan_progress (scan_id, captured_at_utc)
                    """
                )
            )
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scan_progress_scan ON scan_progress (scan_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_scan_progress_cap ON scan_progress (captured_at_utc)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS idx_scanner_grp_env ON scanner_group_performance (environment_id, captured_at_utc)")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_scanner_ind_env ON scanner_individual_performance (environment_id, captured_at_utc, scanner_id)"
                )
            )
        except Exception:
            pass


def ensure_postgresql_classifier_tables(engine) -> None:
    """DDL for classifier/build mapping tables (tracker parity with migrated Postgres schema)."""
    keys = (
        "builds",
        "classifiers",
        "build_templates",
        "build_classifier_mappings",
        "classifier_details_snapshots",
    )
    try:
        from v2_api.db_config_pg import DDL_STATEMENTS
    except Exception:
        DDL_STATEMENTS = {}

    parts = [DDL_STATEMENTS[k] for k in keys if k in DDL_STATEMENTS]

    with engine.begin() as conn:
        for stmt in parts:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        # Tracker historically stores comma-separated names on MySQL `build_templates.classifier_set` (TEXT).
        # Postgres schema uses classifier_set_id; add TEXT column for parity when missing.
        for alt in (
            "ALTER TABLE build_templates ADD COLUMN classifier_set TEXT NULL",
            "ALTER TABLE classifier_details_snapshots ADD COLUMN classifier_name VARCHAR(500) NULL",
        ):
            try:
                conn.execute(text(alt))
            except Exception:
                pass


def dedupe_scan_progress_postgresql(engine) -> None:
    """Remove duplicate (scan_id, captured_at_utc) rows keeping max(id)."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM scan_progress sp1
                    USING scan_progress sp2
                    WHERE sp1.scan_id = sp2.scan_id
                      AND sp1.captured_at_utc IS NOT NULL
                      AND sp2.captured_at_utc IS NOT NULL
                      AND sp1.captured_at_utc = sp2.captured_at_utc
                      AND sp1.id < sp2.id
                    """
                )
            )
    except Exception:
        pass
