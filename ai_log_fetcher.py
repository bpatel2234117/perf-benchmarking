import os
import sys
import time
import requests
import json
import re
from datetime import datetime
from sqlalchemy import create_engine, text

# Add the current directory to path so we can import local modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from db_config import DB_CONNECTION_STRING
from bigid_auth import Get_auth_token

# Configurations
FETCH_INTERVAL_MINUTES = int(os.getenv("SCANNER_LOG_FETCH_MINUTES", "5"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
LOG_LEN = int(os.getenv("SCANNER_LOG_FETCH_LEN", "2000"))

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
    engine = create_engine(DB_CONNECTION_STRING, echo=False)
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_log_errors (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    environment_id INT NOT NULL,
                    log_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_context TEXT,
                    ai_analysis TEXT,
                    is_resolved BOOLEAN DEFAULT FALSE,
                    log_source VARCHAR(50) DEFAULT 'scanner',
                    scan_id VARCHAR(50),
                    subscan_id VARCHAR(50),
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
        
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_log_correlations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
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
        SELECT DISTINCT t.id, t.env_name, t.base_url 
        FROM test_environments t
        JOIN scan_parents sp ON sp.environment_id = t.id
        WHERE t.is_active = 1 
          AND sp.scan_status IS NOT NULL 
          AND sp.scan_status != ''
          AND LOWER(REPLACE(sp.scan_status, ' ', '')) NOT IN (
              'completed', 'completedwithfailures', 'failed', 'stopped', 'stop', 'aborted', 'cancelled', 'canceled'
          )
    """
    with engine.connect() as conn:
        result = conn.execute(text(query)).fetchall()
        return [{"id": row[0], "env_name": row[1], "base_url": row[2]} for row in result]

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
    s_match = re.search(r'(?i)(?:scan[_-]?id|scan_profile[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if s_match:
        scan_id = s_match.group(1)
        
    ss_match = re.search(r'(?i)(?:sub[_-]?scan[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if ss_match:
        subscan_id = ss_match.group(1)
        
    return scan_id, subscan_id

def detect_errors_in_logs(log_text):
    """
    Scans logs line by line, keeping a sliding 5-line window.
    Returns a list of 5-line contexts centered around 'ERROR', 'Exception', or 'Fail'.
    """
    lines = log_text.splitlines()
    error_contexts = []
    seen_contexts = set()
    
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if "error" in line_lower or "exception" in line_lower or "fail" in line_lower:
            # We don't want to capture every single error if it's flooded.
            # But let's grab the context: 2 lines before, current, 2 lines after.
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            context = "\\n".join(lines[start:end])
            
            # Deduplicate within the same fetch text
            if context not in seen_contexts:
                seen_contexts.add(context)
                error_contexts.append(context)
                
    return error_contexts

def call_ai_analyzer(error_context, log_source):
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

def store_error_analysis(engine, env_id, error_context, ai_analysis, log_source, scan_id, subscan_id):
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_log_errors (environment_id, log_timestamp, error_context, ai_analysis, log_source, scan_id, subscan_id)
                VALUES (:eid, :lt, :ctx, :ai, :ls, :sid, :ssid)
            """),
            {
                "eid": env_id,
                "lt": datetime.utcnow().isoformat() + "Z",
                "ctx": error_context,
                "ai": ai_analysis,
                "ls": log_source,
                "sid": scan_id,
                "ssid": subscan_id
            }
        )
        conn.commit()

def store_correlation_report(engine, env_id, scan_id, report_text, is_final=False):
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO scan_log_correlations (environment_id, scan_id, correlation_report, is_final)
                VALUES (:eid, :sid, :rep, :fin)
            """),
            {
                "eid": env_id,
                "sid": scan_id,
                "rep": report_text,
                "fin": is_final
            }
        )
        conn.commit()

def run_once(engine):
    environments = get_active_environments(engine)
    print(f"[{datetime.utcnow().isoformat()}] Fetched {len(environments)} active environments.")
    
    for env in environments:
        env_id = env["id"]
        base_url = env["base_url"]
        
        try:
            print(f"[{datetime.utcnow().isoformat()}] Fetching token for {env['env_name']}...")
            token = Get_auth_token(base_url, {"env_name": env["env_name"]})
            
            new_errors_by_scan = {} # {scan_id: [(source, ctx, sid)]}
            
            log_sources = ["scanner", "orch", "orch2", "aci"]
            for log_source in log_sources:
                print(f"[{datetime.utcnow().isoformat()}] Fetching {log_source} logs from {base_url}...")
                log_text = fetch_logs(base_url, token, LOG_LEN, log_source)
                
                print(f"[{datetime.utcnow().isoformat()}] Analyzing {log_source} logs for errors...")
                errors = detect_errors_in_logs(log_text)
                
                # Limit to 3 per source per cycle to avoid flooding
                max_errors_to_process = 3
                processed_errors = 0
                
                for ctx in errors:
                    if processed_errors >= max_errors_to_process:
                        break
                    
                    with engine.connect() as conn:
                        existing = conn.execute(
                            text("SELECT id FROM scan_log_errors WHERE environment_id = :eid AND error_context = :ctx LIMIT 1"),
                            {"eid": env_id, "ctx": ctx}
                        ).fetchone()
                        
                    if existing:
                        continue
                    
                    parsed_scan_id, parsed_subscan_id = extract_scan_ids(ctx)
                    
                    print(f"[{datetime.utcnow().isoformat()}] Found new error in {log_source}. Querying AI...")
                    ai_analysis = call_ai_analyzer(ctx, log_source)
                    
                    store_error_analysis(engine, env_id, ctx, ai_analysis, log_source, parsed_scan_id, parsed_subscan_id)
                    processed_errors += 1
                    
                    # Group for batch correlation analysis
                    if parsed_scan_id:
                        if parsed_scan_id not in new_errors_by_scan:
                            new_errors_by_scan[parsed_scan_id] = []
                        new_errors_by_scan[parsed_scan_id].append((log_source, ctx, parsed_scan_id))
            
            # Per-Scan-ID Batch Correlation
            for sid, batch in new_errors_by_scan.items():
                if len(batch) >= 1: # We correlate even if there's only one, as the prompt is different
                    print(f"[{datetime.utcnow().isoformat()}] Generating correlation report for {sid}...")
                    correlation_text = call_correlation_analyzer(batch)
                    store_correlation_report(engine, env_id, sid, correlation_text, is_final=False)
                    print(f"[{datetime.utcnow().isoformat()}] Saved interval correlation for {sid}.")
                
        except Exception as e:
            print(f"[{datetime.utcnow().isoformat()}] Failed processing env {env['env_name']}: {e}")
            write_heartbeat(engine, status="error", message=str(e))
            return
            
    write_heartbeat(engine, status="ok", message=f"Processed {len(envs)} environments")

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print(f"Starting Scanner AI Log Analyzer (Fetch Interval: {FETCH_INTERVAL_MINUTES} min, LLM: {OLLAMA_MODEL})")
    
    if not check_ollama_ready():
        print(f"CRITICAL ERROR: Ollama is not reachable. Please ensure Ollama is running and the model '{OLLAMA_MODEL}' is downloaded.")
        sys.exit(1)
        
    engine = setup_db_engine()
    
    while True:
        run_once(engine)
        print(f"Sleeping for {FETCH_INTERVAL_MINUTES} minutes...")
        time.sleep(FETCH_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
