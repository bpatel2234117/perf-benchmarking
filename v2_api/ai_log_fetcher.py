import os
import sys
import time
import requests
import json
import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import create_engine, text

from v2_api.pg_compat import (
    sql_auto_increment,
    sql_datetime,
    sql_longtext,
    sql_on_update_current_timestamp,
)

# Force unbuffered output for cleaner logs
sys.stdout.reconfigure(line_buffering=True)

print(f"[{datetime.now().isoformat()}] INFO: Bootstrapping dependencies...")

# Add the current directory to path so we can import local modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def get_db_connection_string():
    from v2_api.db_config_pg import DB_CONNECTION_STRING
    return DB_CONNECTION_STRING

def get_auth_token(base_url, env_row):
    from bigid_auth import Get_auth_token
    return Get_auth_token(base_url, env_row)

# Configurations
FETCH_INTERVAL_MINUTES = int(os.getenv("SCANNER_LOG_FETCH_MINUTES", "5"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
LOG_LEN = int(os.getenv("SCANNER_LOG_FETCH_LEN", "3000"))
DEFAULT_LOG_FETCH_SOURCES = ["scanner", "orch", "orch2", "aci", "api-gateway", "data-manager"]
DEFAULT_LOG_SOURCE_OPTIONS = [
    "web",
    "orch",
    "orch2",
    "scanner",
    "corr-new",
    "clustering",
    "data-catalog",
    "aci",
    "reports",
    "secure-sensitive",
    "ner",
    "metadata-search",
    "catalog-processor",
    "cryptmgr",
    "action-center",
    "reporting-etl",
    "api-gateway",
    "data-manager",
]


def _coerce_to_datetime(value: Any) -> Optional[datetime]:
    """Normalize DB/driver timestamps (datetime, date, str) for comparisons."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_cls):
        return datetime.combine(value, datetime.min.time())
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("T", " ").rstrip("Z")
    if "+" in s and " " in s:
        s = s.split("+", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_log_source_name(value):
    return str(value or "").strip().lower()


def _ordered_unique_log_sources(values):
    seen = set()
    ordered = []
    for raw in values or []:
        item = _normalize_log_source_name(raw)
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def ensure_app_settings_table(engine):
    lt = sql_longtext(engine)
    ou = sql_on_update_current_timestamp(engine)
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key VARCHAR(100) PRIMARY KEY,
                    setting_value {lt},
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP {ou}
                )
            """))
            conn.commit()
    except Exception as e:
        print(f"Error creating app_settings: {e}")


def load_enabled_log_sources(engine):
    ensure_app_settings_table(engine)
    fallback = _ordered_unique_log_sources(DEFAULT_LOG_FETCH_SOURCES)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT setting_value FROM app_settings WHERE setting_key = :k LIMIT 1"),
                {"k": "enabled_log_sources"}
            ).fetchone()
        if not row or not row[0]:
            return fallback
        payload = json.loads(str(row[0]))
        enabled = payload.get("enabled") if isinstance(payload, dict) else payload
        enabled_list = _ordered_unique_log_sources(enabled)
        return enabled_list or fallback
    except Exception as e:
        print(f"Failed to load enabled_log_sources: {e}")
        return fallback

def check_ollama_ready():
    """Verify that Ollama is reachable before starting the analyzer loop."""
    print(f"[{datetime.utcnow().isoformat()}] Checking Ollama connection at {OLLAMA_URL}...")
    try:
        # Just check the base URL status
        base_url = "/".join(OLLAMA_URL.split("/")[:3])
        response = requests.get(base_url, timeout=5)
        if response.status_code == 200:
            print(f"[{datetime.utcnow().isoformat()}] [OK] Ollama is active.")
            return True
        else:
            print(f"[{datetime.utcnow().isoformat()}] [ERROR] Ollama returned status {response.status_code}")
            return False
    except Exception as e:
        print(f"[{datetime.utcnow().isoformat()}] [ERROR] Cannot reach Ollama: {e}")
        return False

def setup_db_engine():
    engine = create_engine(get_db_connection_string(), echo=False)
    ensure_app_settings_table(engine)
    
    dt = sql_datetime(engine)
    ai = sql_auto_increment(engine)
    
    try:
        with engine.connect() as conn:
            # Automatic Migration for error_hash
            try:
                # Version-agnostic way to add column: try it and let the except block catch 'already exists'
                conn.execute(text("ALTER TABLE scan_log_errors ADD COLUMN error_hash VARCHAR(32)"))
                conn.commit()
            except Exception:
                # Column likely already exists, safe to ignore
                pass
            
            # Tracker and Log Fetcher heartbeat table
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS tracker_heartbeat (
                    name VARCHAR(50) PRIMARY KEY,
                    last_started_at_utc {dt},
                    last_finished_at_utc {dt},
                    last_status VARCHAR(20),
                    last_message TEXT
                )
            """))
            conn.commit()
            
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS scan_log_errors (
                    id {ai} PRIMARY KEY,
                    environment_id INT NOT NULL,
                    log_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_context TEXT,
                    ai_analysis TEXT,
                    is_resolved BOOLEAN DEFAULT FALSE,
                    log_source VARCHAR(50) DEFAULT 'scanner',
                    scan_id VARCHAR(50),
                    subscan_id VARCHAR(50),
                    error_hash VARCHAR(32),
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                );
            """))
            conn.commit()
    except Exception as e:
        print(f"Error creating scan_log_errors: {e}")
        
    # Ensure columns exist for users on older schemas
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE scan_log_errors ADD COLUMN log_source VARCHAR(50) DEFAULT 'scanner'"))
            conn.commit()
    except Exception:
        pass 
        
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE scan_log_errors ADD COLUMN scan_id VARCHAR(50)"))
            conn.execute(text("ALTER TABLE scan_log_errors ADD COLUMN subscan_id VARCHAR(50)"))
            conn.commit()
    except Exception:
        pass
        
    ai = sql_auto_increment(engine)
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS scan_log_correlations (
                    id {ai} PRIMARY KEY,
                    environment_id INT NOT NULL,
                    scan_id VARCHAR(50),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    correlation_report TEXT,
                    is_final BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                );
            """))
            conn.commit()
    except Exception:
        pass
    
    ai = sql_auto_increment(engine)
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS scan_system_health (
                    id {ai} PRIMARY KEY,
                    environment_id INT NOT NULL,
                    captured_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    health_summary TEXT,
                    log_window_start TIMESTAMP,
                    log_window_end TIMESTAMP,
                    FOREIGN KEY (environment_id) REFERENCES test_environments(id)
                );
            """))
            conn.commit()
    except Exception as e:
        print(f"Error creating scan_system_health: {e}")
        
    return engine

def write_heartbeat(engine, status="ok", message=""):
    try:
        with engine.connect() as conn:
            # We use the same tracker_heartbeat table but with name='log_fetcher'
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            
            # Check if exists
            exists = conn.execute(
                text("SELECT name FROM tracker_heartbeat WHERE name = 'log_fetcher' LIMIT 1")
            ).fetchone()
            
            if exists:
                conn.execute(
                    text("""
                        UPDATE tracker_heartbeat 
                        SET last_finished_at_utc = :now, 
                            last_status = :status, 
                            last_message = :msg 
                        WHERE name = 'log_fetcher'
                    """),
                    {"now": now, "status": status, "msg": message}
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO tracker_heartbeat (name, last_started_at_utc, last_finished_at_utc, last_status, last_message)
                        VALUES ('log_fetcher', :now, :now, :status, :msg)
                    """),
                    {"now": now, "status": status, "msg": message}
                )
            conn.commit()
    except Exception as e:
        print(f"Failed to write heartbeat: {e}")

def get_active_environments(engine):
    query = """
        SELECT DISTINCT t.id, t.env_name, t.base_url, sp.scan_id, sp.started_at
        FROM test_environments t
        JOIN scan_parents sp ON sp.environment_id = t.id
        WHERE t.is_active = 1 
          AND (
              LOWER(REPLACE(sp.scan_status, ' ', '')) NOT IN (
                  'completed', 'completedwithfailures', 'failed', 'stopped', 'stop', 'aborted', 'cancelled', 'canceled', 'paused', 'pause'
              )
          )
    """
    with engine.connect() as conn:
        result = conn.execute(text(query)).fetchall()
        return [{"id": row[0], "env_name": row[1], "base_url": row[2], "active_scan_id": row[3], "scan_start_time": row[4]} for row in result]

def fetch_logs(base_url, token, num_lines, log_source="scanner"):
    url = f"{base_url.rstrip('/')}/api/v1/logs/{log_source}?len={num_lines}"
    headers = {
        "authorization": token,
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    # User curl uses --insecure
    response = requests.get(url, headers=headers, verify=False, timeout=60)
    response.raise_for_status()
    
    # Sometimes it returns a bare string, sometimes JSON
    try:
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return str(data)
    except json.JSONDecodeError:
        return response.text

def extract_scan_ids(log_text):
    """
    Attempts to extract BigID scan_id and subscan_id (24 char hex strings) from the context.
    """
    scan_id = None
    subscan_id = None
    
    # Common formats: scan_id=69c673df1e63fcb3cc6c69d9, scanId: "..." etc.
    s_match = re.search(r'(?i)(?:scan[_-]?id|scan[_-]?id|scan[_-]?profile[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if not s_match:
        # Handle O365 SDK format: sdk-object-scan-69d62cbf93c857d502eb0099-69d62d0193c857d502ebb349-0
        s_match = re.search(r'sdk-object-scan-([a-f0-9]{24})', log_text)
        
    if s_match:
        scan_id = s_match.group(1)
        
    ss_match = re.search(r'(?i)(?:sub[_-]?scan[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if not ss_match and s_match:
        # Check for subscan ID following the scan ID in the SDK format
        ss_match = re.search(fr'sdk-object-scan-{scan_id}-([a-f0-9]{24})', log_text)
        
    if ss_match:
        subscan_id = ss_match.group(1)
        
    return scan_id, subscan_id


def _skip_info_level_line(line: str) -> bool:
    """
    BigID scanner often logs normal work at [INFO], including URLs whose path contains
    'failed-enumeration-objects' etc. Those must not be treated as errors.
    Set SCANNER_LOG_SKIP_INFO_LEVEL=0 to revert to legacy behavior.
    """
    if os.getenv("SCANNER_LOG_SKIP_INFO_LEVEL", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(re.search(r"\[(?:INFO|DEBUG|TRACE)\]", line, re.I))


def _line_for_error_keyword_scan(line: str) -> str:
    """Strip URLs so path segments like .../failed-enumeration-objects/... do not match 'fail'."""
    return re.sub(r"https?://[^\s]+", " ", line, flags=re.I)


def _looks_like_success_scan_summary(ctx: str) -> bool:
    """
    Drop contexts that are clearly completed scan payloads (often pulled in as trailing JSON)
    when a noisy line above matched substring keywords (e.g. isAbort, preset, throttle).
    """
    if not re.search(r'"scanState"\s*:\s*"Completed"', ctx, re.I):
        return False
    if re.search(r'"totalFailedObjects"\s*:\s*0\b', ctx, re.I):
        return True
    if re.search(r'"total_failed_objects"\s*:\s*0\b', ctx, re.I):
        return True
    return False


def _line_passes_error_level_gate(line: str) -> bool:
    """
    Require a real log severity on the trigger line (defense against keyword-only false positives).
    Default: only [ERROR], [FATAL], [SEVERE]. Set SCANNER_LOG_ERROR_REQUIRE_LEVEL=warn to include
    [WARN]/[WARNING], or any/legacy for keyword-only matching (old behavior).
    """
    mode = os.getenv("SCANNER_LOG_ERROR_REQUIRE_LEVEL", "error_fatal").strip().lower()
    if mode in ("any", "legacy", "all", "0", "off", "false", "no"):
        return True
    if mode in ("warn", "warning"):
        return bool(
            re.search(r"\[(?:ERROR|FATAL|SEVERE|WARN|WARNING)\]", line, re.I)
        )
    # error_fatal (default)
    return bool(re.search(r"\[(?:ERROR|FATAL|SEVERE)\]", line, re.I))


import hashlib

def _parse_log_timestamp(line: str) -> Optional[datetime]:
    """
    Parses BigID log timestamps: [2026-04-15 17:27:04.729Z] or [2026-04-20 10:08:26]
    Returns a UTC datetime object or None.
    """
    match = re.search(r'\[(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\]', line)
    if not match:
        return None
    ts_str = match.group(1).replace('T', ' ').replace('Z', '')
    try:
        # Normalize to avoid fractional second matching issues
        if '.' in ts_str:
            ts_str = ts_str.split('.')[0]
        return datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None

def detect_errors_in_logs(log_text):
    """
    Structural-aware log analysis.
    Finds error entry points (case-insensitive) and captures consecutive indented lines (stack traces).

    By default (SCANNER_LOG_ERROR_REQUIRE_LEVEL=error_fatal) the trigger line must include
    [ERROR], [FATAL], or [SEVERE] — keyword matches alone are ignored. Use warn or any for looser rules.
    """
    lines = log_text.splitlines()
    error_contexts = []
    seen_fingerprints = set()

    i = 0
    # Word-boundary tokens. Unbounded 'abort' matched isAbort; 'reset' matched preset; 'throttl' matched throttle.
    # "failed" only when followed by whitespace so hyphenated API paths do not match.
    ERROR_PATTERN = re.compile(
        r"(?i)(?:"
        r"\berror\b|\bfatal\b|\bfail\b|\bfailed\b(?=\s)|\bfailure\b|\bsevere\b|\bexception\b|\baxioserror\b|\bnohttp\b|"
        r"status\s+[45]\d{2}|\btimeout\b|timed?\s*out|\brefused\b|\bunreachable\b|\breset\b|\bdenied\b|"
        r"\babort(?:ed|ing)?\b|\bexhausted\b|"
        r"\bthrottl(?:ing|ed)\b|\blimitreached\b|\bgraphservice\b|\bactivitylimit\b"
        r")"
    )

    while i < len(lines):
        line = lines[i]

        if _skip_info_level_line(line):
            i += 1
            continue

        scan_line = _line_for_error_keyword_scan(line)

        # 1. Detect starting line of an error block (Pattern-based)
        if ERROR_PATTERN.search(scan_line):
            if not _line_passes_error_level_gate(line):
                i += 1
                continue
            # Context before: capture 2 lines if possible for timestamps/IDs
            start = max(0, i - 2)
            
            # 2. Greedy Multi-line Capture (Stack traces, indented JSON, etc)
            end = i + 1
            while end < len(lines) and (end - i) < 30: # Max 30 lines for context
                next_line = lines[end]

                # CRITICAL FIX: If the next line itself is a NEW error marker, 
                # BREAK the context here so it gets its own record instead of being swallowed.
                if ERROR_PATTERN.search(_line_for_error_keyword_scan(next_line)) and _line_passes_error_level_gate(next_line):
                    break

                # Match common stack trace/indented log markers
                if next_line.startswith((" ", "\t", "at ", "Caused by:", "---", "}", "]")):
                    end += 1
                else: break
            
            # If no indentation, at least grab a tiny bit of following context
            if end == i + 1:
                end = min(len(lines), i + 4)

            # Slice includes trigger line (i) and up to 2 lines before (start) through end-1
            ctx_lines = lines[start:end]
            ctx_text = "\n".join(ctx_lines)
            
            # Extract timestamp from the trigger line or context
            log_ts = _parse_log_timestamp(line)
            if not log_ts:
                # Fallback: check first line of context
                for cl in ctx_lines:
                    log_ts = _parse_log_timestamp(cl)
                    if log_ts: break

            # Deduplicate within this fetch cycle
            if _looks_like_success_scan_summary(ctx_text):
                i = end
                continue

            # IMPROVED FINGERPRINTING: Mask timestamps to avoid 
            # skipping unique errors just because their log prefix is the same.
            fingerprint_text = re.sub(r'\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?', '[TS]', ctx_text)
            fingerprint = fingerprint_text[:300]
            
            if fingerprint not in seen_fingerprints:
                seen_fingerprints.add(fingerprint)
                # Calculate hash for DB deduplication (stable across fetches)
                error_hash = hashlib.md5(fingerprint.encode('utf-8')).hexdigest()
                error_contexts.append({
                    "text": ctx_text,
                    "timestamp": log_ts,
                    "hash": error_hash
                })
                
            i = end # Advance cursor
        else:
            i += 1
                
    return error_contexts

def call_health_summarizer(log_text):
    if not check_ollama_ready():
        return "[⚠️ System Health Review Offline] Ollama is unreachable."
    
    """
    Sends a representative digest of the entire 3000-line window for a proactive health review.
    """
    lines = log_text.splitlines()
    if len(lines) < 10:
        return None
        
    # Take a digest: first 40, middle 40, last 40 lines to stay within typical LLM context tokens (Ollama)
    digest_size = 40
    if len(lines) <= digest_size * 3:
        digest = log_text
    else:
        mid = len(lines) // 2
        digest = (
            "\n".join(lines[:digest_size]) + 
            "\n\n[... middle omitted ...]\n\n" + 
            "\n".join(lines[mid-digest_size//2 : mid+digest_size//2]) + 
            "\n\n[... tail omitted ...]\n\n" + 
            "\n".join(lines[-digest_size:])
        )

    prompt = f"""
You are a site reliability engineer monitoring a BigID system. 
Analyze the following representative log digest (samples from the start, middle, and end of a 3000-line window).

LOG DIGEST:
{digest}

Task:
1. Summarize the general health of this service in 2-3 sentences.
2. Note any subtle warnings, high latencies, or repetitive non-error issues.
3. If everything looks healthy, state that the system appears stable.

Provide a concise, high-level structural health assessment.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=180
        )
        response.raise_for_status()
        return response.json().get("response", "Health summary unavailable")
    except Exception as e:
        return f"Health Review failed: {e}"

def call_ai_analyzer(error_context, log_source):
    if not check_ollama_ready():
        return f"[⚠️ AI Analysis Offline] Ollama is not reachable at {OLLAMA_URL}. Please ensure Ollama is running. Raw error context: {error_context[:200]}..."
    
    prompt = f"""
You are an expert systems engineer and log analyst.
I have extracted a severe error from our '{log_source}' application logs.
Below is the exact error line with two lines of context before and after.

LOG CONTEXT:
{error_context}

Please explain:
1. What exactly went wrong here?
2. Is this a network, authentication, resource, or logic/code bug?
3. What is the recommended fix or area to investigate?

Provide a concise, highly technical answer.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False
            },
            timeout=120
        )
        response.raise_for_status()
        return response.json().get("response", "No response from AI")
    except Exception as e:
        return f"AI Analysis failed: {e}"

def call_correlation_analyzer(error_batch):
    """
    Takes a list of multiple errors found in the current cycle and asks the LLM to correlate them.
    error_batch: list of (source, context, scan_id)
    """
    if not error_batch:
        return None
        
    contexts_str = ""
    for idx, (source, ctx, sid) in enumerate(error_batch):
        contexts_str += f"\n--- Error {idx+1} (Source: {source}, ScanID: {sid}) ---\n{ctx}\n"
        
    prompt = f"""
You are a distributed systems forensic lead. I have detected multiple errors across different log sources during the current 5-minute polling window.

ERRORS DETECTED:
{contexts_str}

Please correlate these events:
1. What is the sequence of failure or the primary trigger?
2. How do these errors relate to each other (e.g., did an Orchestrator timeout cause a Scanner crash)?
3. What is the combined systemic impact?

Provide a high-level, technical correlation report.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False
            },
            timeout=180
        )
        response.raise_for_status()
        return response.json().get("response", "No correlation response from AI")
    except Exception as e:
        return f"Correlation Analysis failed: {e}"

def store_error_analysis(engine, env_id, error_context, ai_analysis, log_source, scan_id, subscan_id, error_hash=None, actual_log_ts=None):
    with engine.connect() as conn:
        ts_to_store = actual_log_ts if actual_log_ts else datetime.utcnow()
        if isinstance(ts_to_store, datetime):
            ts_to_store = ts_to_store.strftime('%Y-%m-%d %H:%M:%S')

        conn.execute(
            text("""
                INSERT INTO scan_log_errors (environment_id, log_timestamp, error_context, ai_analysis, log_source, scan_id, subscan_id, error_hash)
                VALUES (:eid, :lt, :ctx, :ai, :ls, :sid, :ssid, :hash)
            """),
            {
                "eid": env_id,
                "lt": ts_to_store,
                "ctx": error_context,
                "ai": ai_analysis,
                "ls": log_source,
                "sid": scan_id,
                "ssid": subscan_id,
                "hash": error_hash
            }
        )
        conn.commit()

def store_health_summary(engine, env_id, summary):
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_system_health (environment_id, health_summary, captured_at_utc)
                VALUES (:eid, :hs, :cat)
            """),
            {"eid": env_id, "hs": summary, "cat": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}
        )
        conn.commit()

def run_once(engine):
    try:
        envs = get_active_environments(engine)
        if not envs:
            print(f"[{datetime.now().isoformat()}] No active environments to process.")
            write_heartbeat(engine, status="ok", message="No active environments")
            return

        print(f"[{datetime.now().isoformat()}] Processing {len(envs)} environments...")
        
        for env in envs:
            env_id = env['id']
            base_url = env['base_url']
            env_name = env['env_name']
            
            try:
                print(f"[{datetime.now().isoformat()}] [{env_name}] Fetching auth token...")
                token = get_auth_token(base_url, env)
                
                new_errors_by_scan = {}
                log_sources = load_enabled_log_sources(engine)
                
                for log_source in log_sources:
                    try:
                        logs = fetch_logs(base_url, token, 3000, log_source)
                        if not logs: continue
                        
                        errors = detect_errors_in_logs(logs)
                        for err_obj in errors:
                            ctx = err_obj["text"]
                            log_ts = err_obj["timestamp"] or datetime.utcnow()
                            err_hash = err_obj["hash"]
                            
                            # CRITICAL FIX 1: Age Gate (Post-Scan Start Time Only)
                            ts_cmp = _coerce_to_datetime(log_ts) or (
                                log_ts if isinstance(log_ts, datetime) else datetime.utcnow()
                            )
                            scan_start = _coerce_to_datetime(env.get("scan_start_time"))
                            if scan_start is not None and ts_cmp < scan_start:
                                # This is a stale log from April 15th or previous run. SKIP.
                                continue

                            # CRITICAL FIX 2: Global Deduplication (Hash-based)
                            with engine.connect() as conn:
                                # We check hash AND recently (within 24h) to allow same error in future scans, 
                                # but ignore "bursts" of same error in current scan.
                                existing = conn.execute(
                                    text("""
                                        SELECT id FROM scan_log_errors 
                                        WHERE environment_id = :eid 
                                          AND error_hash = :hash 
                                          AND log_timestamp > DATE_SUB(NOW(), INTERVAL 24 HOUR)
                                        LIMIT 1
                                    """),
                                    {"eid": env_id, "hash": err_hash}
                                ).fetchone()
                            if existing: continue
                            
                            parsed_scan_id, parsed_subscan_id = extract_scan_ids(ctx)
                            ai_analysis = call_ai_analyzer(ctx, log_source)
                            
                            # CRITICAL FIX 3: Store REAL log timestamp
                            store_error_analysis(
                                engine, env_id, ctx, ai_analysis, log_source, 
                                parsed_scan_id or env.get("active_scan_id"), 
                                parsed_subscan_id, 
                                error_hash=err_hash,
                                actual_log_ts=log_ts
                            )
                            
                            p_sid = parsed_scan_id or env.get("active_scan_id")
                            if p_sid:
                                if p_sid not in new_errors_by_scan:
                                    new_errors_by_scan[p_sid] = []
                                new_errors_by_scan[p_sid].append((log_source, ctx, p_sid))

                    except Exception as source_err:
                        print(f"[{datetime.now().isoformat()}] [{env_name}] Error processing source {log_source}: {source_err}")
                
            except Exception as env_err:
                print(f"[{datetime.now().isoformat()}] [ERROR] Failed processing environment {env_name}: {env_err}")
                continue

        write_heartbeat(engine, status="ok", message=f"Processed {len(envs)} environments at {datetime.now().isoformat()}")
    except Exception as global_err:
        print(f"[{datetime.now().isoformat()}] CRITICAL: Global run_once failure: {global_err}")

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print(f"Starting Scanner AI Log Analyzer (Fetch Interval: {FETCH_INTERVAL_MINUTES} min, LLM: {OLLAMA_MODEL})")
    
    if not check_ollama_ready():
        print(f"WARNING: Ollama is not reachable at {OLLAMA_URL}. The monitor will continue to detect errors but AI analysis will be skipped until Ollama is active.")
        # DO NOT EXIT - continue to monitor logs even without AI
        
    engine = setup_db_engine()
    
    while True:
        run_once(engine)
        print(f"Sleeping for {FETCH_INTERVAL_MINUTES} minutes...")
        time.sleep(FETCH_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
