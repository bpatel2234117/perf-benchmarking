-- Populate benchmark_runs from the legacy tables, then build build_major_averages.
-- Idempotent: re-running inserts only new legacy rows (ON CONFLICT DO NOTHING) and
-- fully rebuilds the averages. Run directly against benchmark_db.

-- 1) Manual benchmarks -> benchmark_runs
INSERT INTO benchmark_runs (
    source, legacy_table, legacy_id, scan_id, build_id, config_id, classifier_set_id,
    data_source_type_id, file_set_type_id, build_version, run_date, scenario_type, scan_type,
    scan_start_time, scan_end_time, total_scan_hours,
    throughput_cluster_gb_hr, throughput_scanner_gb_hr, throughput_thread_gb_hr,
    throughput_cluster_files_hr, throughput_scanner_files_hr, throughput_thread_files_hr,
    total_catalog_items, total_classification, avg_file_size, is_active, is_release, created_by, updated_at
)
SELECT 'manual', 'benchmarks', b.id, b.scan_id, b.build_id, b.config_id, b.classifier_set_id,
       b.data_source_type_id, b.file_set_type_id, bv.version, b.run_date, b.scenario_type, b.scan_type,
       b.scan_start_time, b.scan_end_time, b.total_scan_hours,
       b.throughput_cluster_gb_hr, b.throughput_scanner_gb_hr, b.throughput_thread_gb_hr,
       b.throughput_cluster_files_hr, b.throughput_scanner_files_hr, b.throughput_thread_files_hr,
       b.total_catalog_items, b.total_classification, b.avg_file_size, b.is_active, b.is_release, b.created_by, CURRENT_TIMESTAMP
FROM benchmarks b
LEFT JOIN builds bv ON bv.id = b.build_id
ON CONFLICT (legacy_table, legacy_id) DO NOTHING;

-- 2) Auto benchmarks -> benchmark_runs (skip scans already represented by a manual row)
INSERT INTO benchmark_runs (
    source, legacy_table, legacy_id, scan_id, build_id, data_source_type_id, file_set_type_id,
    environment_id, build_version, data_source_type, file_set_type, data_type,
    run_date, scenario_type, scan_type, scan_name,
    scan_start_time, scan_end_time, total_scan_hours,
    throughput_cluster_gb_hr, throughput_cluster_files_hr,
    objects_with_pi, objects_scanned, objects_enumerated, total_classification,
    new_attributes, removed_attributes, classifier_attribute_names, total_size_gb, total_files, total_folders,
    is_active, is_release, created_by, updated_at
)
SELECT 'auto', 'auto_scan_benchmarks', a.id, a.scan_id, a.build_id, a.data_source_type_id, a.file_set_type_id,
       a.environment_id, a.build_version, a.data_source_type, a.file_set_type, a.data_type,
       a.run_date::text, a.scenario_type, a.scan_type, a.scan_name,
       a.scan_start_time, a.scan_end_time, a.total_scan_hours,
       a.throughput_cluster_gb_hr, a.throughput_cluster_files_hr,
       a.objects_with_pi, a.objects_scanned, a.objects_enumerated, a.total_classification,
       a.new_attributes, a.removed_attributes, a.classifier_attribute_names, a.total_size_gb, a.total_files, a.total_folders,
       a.is_active, a.is_release, a.created_by, CURRENT_TIMESTAMP
FROM auto_scan_benchmarks a
WHERE NULLIF(TRIM(a.scan_id), '') IS NULL
   OR a.scan_id NOT IN (SELECT scan_id FROM benchmark_runs WHERE scan_id IS NOT NULL)
ON CONFLICT (legacy_table, legacy_id) DO NOTHING;

-- 3) Rebuild build_major_averages from benchmark_runs (averaged per major build per scenario)
DELETE FROM build_major_averages;
INSERT INTO build_major_averages (
    major_build, data_source_type, file_set_type, scan_type, is_acl, is_feature_flags, run_count,
    avg_total_scan_hours,
    avg_throughput_cluster_gb_hr, avg_throughput_scanner_gb_hr, avg_throughput_thread_gb_hr,
    avg_throughput_cluster_files_hr, avg_throughput_scanner_files_hr, avg_throughput_thread_files_hr,
    min_throughput_cluster_gb_hr, max_throughput_cluster_gb_hr, stddev_throughput_cluster_gb_hr, last_calculated_at
)
SELECT SPLIT_PART(bv.version, '.', 1),
       COALESCE(dst.name, 'Unknown'), COALESCE(fst.name, 'Unknown'), COALESCE(b.scan_type, 'unknown'),
       COALESCE(cfg.is_acl, FALSE), COALESCE(cfg.is_feature_flags, FALSE), COUNT(b.id),
       AVG(b.total_scan_hours),
       AVG(b.throughput_cluster_gb_hr), AVG(b.throughput_scanner_gb_hr), AVG(b.throughput_thread_gb_hr),
       AVG(b.throughput_cluster_files_hr), AVG(b.throughput_scanner_files_hr), AVG(b.throughput_thread_files_hr),
       MIN(b.throughput_cluster_gb_hr), MAX(b.throughput_cluster_gb_hr), STDDEV_SAMP(b.throughput_cluster_gb_hr),
       CURRENT_TIMESTAMP
FROM benchmark_runs b
JOIN builds bv ON bv.id = b.build_id
LEFT JOIN data_source_types dst ON dst.id = b.data_source_type_id
LEFT JOIN file_set_types fst ON fst.id = b.file_set_type_id
LEFT JOIN configurations cfg ON cfg.id = b.config_id
WHERE COALESCE(b.is_active, TRUE) = TRUE
  AND bv.version ~ '^[0-9]'
  AND b.throughput_cluster_gb_hr IS NOT NULL
GROUP BY SPLIT_PART(bv.version, '.', 1),
         COALESCE(dst.name, 'Unknown'), COALESCE(fst.name, 'Unknown'), COALESCE(b.scan_type, 'unknown'),
         COALESCE(cfg.is_acl, FALSE), COALESCE(cfg.is_feature_flags, FALSE);

-- Verify
SELECT 'benchmark_runs' AS t, COUNT(*) FROM benchmark_runs
UNION ALL SELECT 'build_major_averages', COUNT(*) FROM build_major_averages;
