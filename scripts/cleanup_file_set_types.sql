-- Final file_set_types consolidation. Run top-to-bottom in benchmark_db.
-- References are repointed BEFORE deletes (no FK orphans). Idempotent-ish: ids already
-- gone simply match nothing.

-- 1) POSTGRES -> single canonical "Postgres Tables" (keep id 19 "Postgres Mixed Tables", 20GB).
--    Fold in: 20 (Postgres Tables/null), 18 (PostgreSQL-10K-Tables-17GB), 24 (Mixed Files = Postgres).
UPDATE benchmark_runs       SET file_set_type_id=19 WHERE file_set_type_id IN (20,18,24);
UPDATE auto_scan_benchmarks SET file_set_type_id=19 WHERE file_set_type_id IN (20,18,24);
UPDATE benchmarks           SET file_set_type_id=19 WHERE file_set_type_id IN (20,18,24);
DELETE FROM file_set_types WHERE id IN (20,18,24);
UPDATE file_set_types SET name='Postgres Tables' WHERE id=19;

-- 2) MSSQL -> fold "MSSQL-10K-Tables-9GB" (17) into "MSSQL Tables" (21).
UPDATE benchmark_runs       SET file_set_type_id=21 WHERE file_set_type_id=17;
UPDATE auto_scan_benchmarks SET file_set_type_id=21 WHERE file_set_type_id=17;
UPDATE benchmarks           SET file_set_type_id=21 WHERE file_set_type_id=17;
DELETE FROM file_set_types WHERE id=17;

-- 3) CSV -> fold generic "CSV Files" (23) into "S3 CSV Files" (5).
UPDATE benchmark_runs       SET file_set_type_id=5 WHERE file_set_type_id=23;
UPDATE auto_scan_benchmarks SET file_set_type_id=5 WHERE file_set_type_id=23;
UPDATE benchmarks           SET file_set_type_id=5 WHERE file_set_type_id=23;
DELETE FROM file_set_types WHERE id=23;

-- 4) REMOVE entirely (not real scenarios): "S3 Mixed Files" (22), "Large Files" (11).
--    Deletes their benchmark rows in all tables (raw rows remain in your DB backup).
DELETE FROM benchmark_findings   WHERE benchmark_id IN (SELECT id FROM benchmarks WHERE file_set_type_id IN (22,11));
DELETE FROM benchmark_runs       WHERE file_set_type_id IN (22,11);
DELETE FROM auto_scan_benchmarks WHERE file_set_type_id IN (22,11);
DELETE FROM benchmarks           WHERE file_set_type_id IN (22,11);
DELETE FROM file_set_types WHERE id IN (22,11);

-- 5) Resync denormalized text to the canonical names
UPDATE benchmark_runs r       SET file_set_type=f.name FROM file_set_types f
  WHERE r.file_set_type_id=f.id AND COALESCE(r.file_set_type,'')<>f.name;
UPDATE auto_scan_benchmarks a SET file_set_type=f.name FROM file_set_types f
  WHERE a.file_set_type_id=f.id AND COALESCE(a.file_set_type,'')<>f.name;

-- 6) Result
SELECT id, name, total_size_gb, total_files, total_folders FROM file_set_types ORDER BY name;
