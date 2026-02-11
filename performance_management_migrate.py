import pandas as pd
import hashlib
import re
from sqlalchemy import create_engine, text
from db_config import DB_CONNECTION_STRING, DDL_STATEMENTS

SOURCE_FILE = '/Users/bpatel/.gemini/antigravity/brain/79d089f8-53ee-4d6e-bb5f-66fcd32ab7f4/performance_management_data.xlsx'

def clean_throughput(val):
    if pd.isna(val) or val is None: return None
    if isinstance(val, (int, float)): return val
    s = str(val).lower().replace('~', '').replace(',', '').strip()
    match = re.search(r'[\d\.]+', s)
    if match:
        try: return float(match.group())
        except: return None
    return None

def extract_flags_and_info(row):
    s_str = str(row.get('Scanners', '')).lower()
    scanners = 5
    m_s = re.search(r'scanners\s*:\s*(\d+)', s_str)
    if m_s: scanners = int(m_s.group(1))
    
    threads = 50
    m_t = re.search(r'threads\s*:\s*(\d+)', s_str)
    if m_t: threads = int(m_t.group(1))
    
    comment = str(row.get('Comment', '')).lower()
    is_acl = 1 if 'acl' in comment else 0
    is_ocr = 1 if 'ocr' in comment else 0
    is_ff = 1 if 'ff' in comment or 'feature flag' in comment else 0
    return scanners, threads, is_acl, is_ocr, is_ff

def parse_files_size_gb(val):
    s = str(val).lower()
    files = 0
    m_f = re.search(r'([\d,]+)\s*m', s)
    if m_f: files = int(m_f.group(1).replace(',', '')) * 1_000_000
    else:
        m_f2 = re.search(r'([\d,]+)\s*files', s)
        if m_f2: files = int(m_f2.group(1).replace(',', ''))

    gb = 0.0
    m_g = re.search(r'([\d\.]+)\s*gb', s)
    if m_g: gb = float(m_g.group(1))
    
    avg = "Unknown"
    m_a = re.search(r'([\d\-]+\s*kb)', s)
    if m_a: avg = m_a.group(1)
    
    return files, gb, avg

def migrate_data(engine):
    xl = pd.ExcelFile(SOURCE_FILE)
    count = 0
    
    with engine.connect() as conn:
        print("Dropping and recreating tables...")
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for tbl in ['benchmark_findings', 'benchmarks', 'build_classifier_mappings', 'classifier_set_members', 'classifier_sets', 'configurations', 'classifiers', 'build_templates', 'test_environment_version_history', 'builds', 'test_environments', 'data_source_types', 'file_set_types']:
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        
        for name, ddl in DDL_STATEMENTS.items():
            conn.execute(text(ddl))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.commit()

        for sheet in xl.sheet_names:
            if sheet in ['Action Item', 'Chart1 Data Throughput by BigID']: continue
            print(f"Processing {sheet}...")
            
            # Find the header row dynamically
            temp_df = pd.read_excel(SOURCE_FILE, sheet_name=sheet, nrows=5, header=None)
            header_idx = 0
            for i, row in temp_df.iterrows():
                if any('BigID version' in str(val) for val in row):
                    header_idx = i
                    break
            
            df = pd.read_excel(SOURCE_FILE, sheet_name=sheet, header=header_idx)
            
            # Find throughput columns
            tp_cols = {}
            for col in df.columns:
                c = str(col).lower().replace('\n', ' ')
                if 'cluster' in c and 'gb/hour' in c: tp_cols['c_gb'] = col
                elif 'per scanner' in c and 'gb/hour' in c: tp_cols['s_gb'] = col
                elif 'per thread' in c and 'gb/hour' in c: tp_cols['t_gb'] = col
                elif 'cluster' in c and 'files/hour' in c: tp_cols['c_f'] = col
                elif 'per scanner' in c and 'files/hour' in c: tp_cols['s_f'] = col
                elif 'per thread' in c and 'files/hour' in c: tp_cols['t_f'] = col

            for _, row in df.iterrows():
                # Normalize Build Version
                ver_raw = str(row.get('BigID version', '')).strip().replace('-', '.')
                if not ver_raw or ver_raw.lower() == 'nan': continue
                
                # Further normalize ver_raw (strip trailing .0 if present)
                ver = re.sub(r'\.0$', '', ver_raw)
                
                # Identify Scan Type and Data Source
                scan_type_raw = str(row.get('Scan Type', 'Full Scan'))
                
                # Normalize Scan Type
                scan_type = scan_type_raw.split('\n')[0].strip() # Take first line only
                
                # Identify Data Source Type
                ds_type = 'Unknown'
                if 's3' in sheet.lower() or 's3' in scan_type_raw.lower(): ds_type = 'S3'
                elif 'smb' in sheet.lower() or 'smb' in scan_type_raw.lower(): ds_type = 'SMB'
                elif 'nfs' in sheet.lower() or 'nfs' in scan_type_raw.lower(): ds_type = 'NFS'
                elif 'mssql' in sheet.lower() or 'mssql' in scan_type_raw.lower(): ds_type = 'MSSQL'
                elif 'oracle' in sheet.lower() or 'oracle' in scan_type_raw.lower(): ds_type = 'Oracle'

                scanners, threads_per, is_acl, is_ocr, is_ff = extract_flags_and_info(row)
                
                c_gb = clean_throughput(row.get(tp_cols.get('c_gb')))
                s_gb = clean_throughput(row.get(tp_cols.get('s_gb')))
                t_gb = clean_throughput(row.get(tp_cols.get('t_gb')))
                c_f = clean_throughput(row.get(tp_cols.get('c_f')))
                s_f = clean_throughput(row.get(tp_cols.get('s_f')))
                t_f = clean_throughput(row.get(tp_cols.get('t_f')))
                
                # Skip row if it has absolutely no data
                if c_gb is None and c_f is None and pd.isna(row.get('Scanners')):
                    continue

                if c_gb and scanners > 0:
                    if not s_gb: s_gb = c_gb / scanners
                    if not t_gb and threads_per > 0: t_gb = c_gb / (scanners * threads_per)
                if c_f and scanners > 0:
                    if not s_f: s_f = c_f / scanners
                    if not t_f and threads_per > 0: t_f = c_f / (scanners * threads_per)

                f_col = next((c for c in df.columns if 'number of files' in str(c).lower()), None)
                t_files, t_gb_size, avg_size = parse_files_size_gb(row.get(f_col)) if f_col else (None, None, None)
                
                # Builds
                conn.execute(text("INSERT IGNORE INTO builds (version) VALUES (:v)"), {'v': ver})
                b_id = conn.execute(text("SELECT id FROM builds WHERE version = :v"), {'v': ver}).fetchone()[0]
                
                # Classifiers
                cl_col = next((c for c in df.columns if 'classifier' in str(c).lower()), None)
                cl_name = str(row.get(cl_col, 'Default')).strip() if cl_col else 'Default'
                if re.match(r'^\d+\s+Classifiers\s+from\s+Global\s+list$', cl_name, flags=re.IGNORECASE):
                    cl_name = 'Default'
                conn.execute(text("INSERT IGNORE INTO classifiers (name) VALUES (:n)"), {'n': cl_name})
                cl_id = conn.execute(text("SELECT id FROM classifiers WHERE name = :n"), {'n': cl_name}).fetchone()[0]
                
                # Config
                comment = str(row.get('Comment', ''))
                cf_str = f"{scanners}-{threads_per}-{is_acl}-{is_ocr}-{is_ff}-{comment}"
                cf_h = hashlib.md5(cf_str.encode()).hexdigest()
                res = conn.execute(text("SELECT id FROM configurations WHERE hash = :h"), {'h': cf_h}).fetchone()
                if res: cf_id = res[0]
                else:
                    conn.execute(text("""
                        INSERT INTO configurations (scanner_count, thread_count, is_acl, is_ocr, is_feature_flags, comment, hash)
                        VALUES (:sc, :tc, :acl, :ocr, :ff, :cm, :h)
                    """), {'sc': scanners, 'tc': threads_per, 'acl': is_acl, 'ocr': is_ocr, 'ff': is_ff, 'cm': comment, 'h': cf_h})
                    cf_id = conn.execute(text("SELECT LAST_INSERT_ID()")).fetchone()[0]

                # Identify File Set Type
                file_set = 'Small Files' # Default
                if 'large' in sheet.lower(): file_set = 'Large Files'
                
                # Identify if OLS
                is_ols = False
                if 'ols' in sheet.lower() or 'ols' in scan_type_raw.lower():
                    is_ols = True
                    scan_type = 'OLS Scan' # Force OLS Scan type
                
                # Benchmark
                conn.execute(text("""
                    INSERT INTO benchmarks (
                        build_id, config_id, classifier_id, run_date, scan_type,
                        data_source_type, file_set_type, total_files, total_size_gb, avg_file_size,
                        throughput_cluster_gb_hr, throughput_scanner_gb_hr, throughput_thread_gb_hr,
                        throughput_cluster_files_hr, throughput_scanner_files_hr, throughput_thread_files_hr
                    ) VALUES (
                        :bid, :cid, :clid, :date, :stype, :dstype, :fset, :tfiles, :tgb, :avg,
                        :cgb, :sgb, :tgb_tp, :cf, :sf, :tf
                    )
                """), {
                    'bid': b_id, 'cid': cf_id, 'clid': cl_id,
                    'date': str(row.get('Date', '')),
                    'stype': scan_type,
                    'dstype': ds_type,
                    'fset': file_set,
                    'tfiles': t_files, 'tgb': t_gb_size, 'avg': avg_size,
                    'cgb': c_gb, 'sgb': s_gb, 'tgb_tp': t_gb,
                    'cf': c_f, 'sf': s_f, 'tf': t_f
                })
                count += 1
        conn.commit()
    print(f"Migration Complete. {count} records inserted.")

if __name__ == "__main__":
    engine = create_engine(DB_CONNECTION_STRING)
    migrate_data(engine)
