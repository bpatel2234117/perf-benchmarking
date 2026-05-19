import os
import sys
import time
import requests
import json
import re
import hashlib
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import create_engine, text

# Force unbuffered output for cleaner logs (guard for pythonw.exe where stdout may be None)
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

print(f"[{datetime.now().isoformat()}] INFO: Bootstrapping dependencies...")

# Add the current directory to path so we can import local modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def get_db_connection_string():
    from db_config import DB_CONNECTION_STRING
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
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key VARCHAR(100) PRIMARY KEY,
                    setting_value LONGTEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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

_ollama_cache: dict = {"ts": None, "ready": False}
_OLLAMA_CACHE_TTL_SECS = 120  # re-verify at most once every 2 minutes


def check_ollama_ready(use_cache: bool = True) -> bool:
    """Verify that Ollama is reachable.

    Results are cached for _OLLAMA_CACHE_TTL_SECS seconds so that a large
    batch of errors does not trigger N sequential HTTP probes against a
    server that is already known to be down/up.

    Set use_cache=False (or call from main() startup) to force a fresh check.
    """
    cache = _ollama_cache
    now = datetime.utcnow()
    if use_cache and cache["ts"] is not None:
        elapsed = (now - cache["ts"]).total_seconds()
        if elapsed < _OLLAMA_CACHE_TTL_SECS:
            return cache["ready"]

    print(f"[{now.isoformat()}] Checking Ollama connection at {OLLAMA_URL}...")
    try:
        base_url = "/".join(OLLAMA_URL.split("/")[:3])
        response = requests.get(base_url, timeout=5)
        ready = response.status_code == 200
        if ready:
            print(f"[{now.isoformat()}] [OK] Ollama is active.")
        else:
            print(f"[{now.isoformat()}] [ERROR] Ollama returned status {response.status_code}")
    except Exception as e:
        print(f"[{now.isoformat()}] [ERROR] Cannot reach Ollama: {e}")
        ready = False

    cache["ts"] = now
    cache["ready"] = ready
    return ready

def setup_db_engine():
    engine = create_engine(
        get_db_connection_string(),
        echo=False,
        pool_pre_ping=True,   # verify connection health before each use
        pool_recycle=1800,    # recycle connections after 30 min to avoid stale TCP
    )
    ensure_app_settings_table(engine)
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
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS tracker_heartbeat (
                    name VARCHAR(50) PRIMARY KEY,
                    last_started_at_utc DATETIME,
                    last_finished_at_utc DATETIME,
                    last_status VARCHAR(20),
                    last_message TEXT
                )
            """))
            conn.commit()
            
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
    
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scan_system_health (
                    id INT AUTO_INCREMENT PRIMARY KEY,
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
    """
    Return one row per active environment for the currently running scan only.

    Log tracking runs ONLY while a scan is active. Once a scan reaches a
    terminal state it drops out of this query immediately — no idle polling,
    no re-processing of old logs between scans.

    The final in-flight cycle (the poll that runs just as a scan completes)
    still captures the last errors because run_once uses scan_start→scan_end
    as the time-window filter, so nothing is lost.
    """
    query = """
        SELECT t.id, t.env_name, t.base_url, sp.scan_id, sp.started_at, sp.completed_at,
               sp.updated_at
        FROM test_environments t
        JOIN scan_parents sp ON sp.environment_id = t.id
        WHERE t.is_active = 1
          AND LOWER(REPLACE(sp.scan_status, ' ', '')) NOT IN (
              'completed','completedwithfailures','failed','stopped','stop',
              'aborted','cancelled','canceled','paused','pause'
          )
        ORDER BY t.id, sp.updated_at DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(query)).fetchall()

    # One row per environment — most recently updated running scan wins.
    seen_envs: set = set()
    result = []
    for row in rows:
        env_id = row[0]
        if env_id in seen_envs:
            continue
        seen_envs.add(env_id)
        result.append({
            "id":              row[0],
            "env_name":        row[1],
            "base_url":        row[2],
            "active_scan_id":  row[3],
            "scan_start_time": row[4],
            "scan_end_time":   row[5],   # None while scan is running
        })
    return result

def fetch_logs(base_url, token, num_lines, log_source="scanner"):
    url = f"{base_url.rstrip('/')}/api/v1/logs/{log_source}?len={num_lines}"
    headers = {
        "authorization": token,
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    response = requests.get(url, headers=headers, verify=False, timeout=60)
    response.raise_for_status()
    try:
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return str(data)
    except json.JSONDecodeError:
        return response.text


def _estimate_fetch_lines(
    scan_start: Optional[datetime],
    scan_end: Optional[datetime],
    cursor: Optional[datetime] = None,
) -> int:
    """
    Estimate how many log lines to fetch.

    Two distinct cases:

    RUNNING scan (scan_end is None):
      - If a cursor exists: fetch one poll interval worth of new lines.
        Window = now - cursor, typically FETCH_INTERVAL_MINUTES wide.
      - First cycle (no cursor): scan may have been running for hours already.
        Fetch from scan_start to now, capped at MAX_LINES.
      Regardless of scan duration (2 h or 20 h), each cycle only fetches
      the delta since the last cursor — keeping fetch size small and constant.

    COMPLETED scan (scan_end is set):
      - Fetch the full scan_start→scan_end window (used for one final pass).

    Heuristic: ~10 log lines/sec under load, +20% buffer.
    Clamped between MIN_LINES (200) and MAX_LINES (8000).
    """
    MIN_LINES     = 200
    MAX_LINES     = 8000
    LINES_PER_SEC = 10

    if scan_end is None:
        # Running scan
        if cursor is not None:
            # Normal cycle: estimate lines since last cursor (one interval)
            window_secs = max(0, (datetime.utcnow() - cursor).total_seconds())
        elif scan_start is not None:
            # First cycle: scan may have been running a while — fetch from start
            window_secs = max(0, (datetime.utcnow() - scan_start).total_seconds())
        else:
            # No timing info at all — use one interval as fallback
            window_secs = FETCH_INTERVAL_MINUTES * 60
    else:
        # Completed scan: full window
        start = scan_start if scan_start is not None else scan_end
        window_secs = max(0, (scan_end - start).total_seconds())

    if window_secs == 0:
        return MIN_LINES

    estimated = int(window_secs * LINES_PER_SEC * 1.2)
    return max(MIN_LINES, min(MAX_LINES, estimated))


def _log_cursor_key(env_id: int, log_source: str, scan_id: str) -> str:
    """app_settings key for the last-processed log timestamp cursor, scoped to a specific scan."""
    return f"log_cursor:{env_id}:{log_source}:{scan_id}"


def _get_log_cursor(engine, env_id: int, log_source: str, scan_id: str) -> Optional[datetime]:
    """Return the last successfully processed log timestamp for this env+source+scan, or None."""
    key = _log_cursor_key(env_id, log_source, scan_id)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT setting_value FROM app_settings WHERE setting_key = :k LIMIT 1"),
                {"k": key}
            ).fetchone()
        if row and row[0]:
            return _coerce_to_datetime(row[0])
    except Exception:
        pass
    return None


def _set_log_cursor(engine, env_id: int, log_source: str, scan_id: str, ts: datetime) -> None:
    """Persist the last-processed log timestamp cursor for this env+source+scan."""
    key = _log_cursor_key(env_id, log_source, scan_id)
    val = ts.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES (:k, :v)
                    ON DUPLICATE KEY UPDATE setting_value = :v
                """),
                {"k": key, "v": val}
            )
            conn.commit()
    except Exception as e:
        print(f"[log cursor] Failed to save cursor for {key}: {e}")


def _filter_lines_to_scan_window(
    raw_text: str,
    scan_start: Optional[datetime],
    scan_end: Optional[datetime],
) -> str:
    """
    Keep only log lines whose timestamp falls within [scan_start, scan_end].

    scan_end=None means the scan is still running — no upper bound is applied,
    so all lines from scan_start onward are kept.

    Lines with no parseable timestamp (stack trace continuations, indented JSON)
    are kept only when they immediately follow an in-window line.
    """
    lines = raw_text.splitlines()
    result = []
    last_in_window = False

    for line in lines:
        ts = _parse_log_timestamp(line)
        if ts is not None:
            in_window = True
            if scan_start is not None and ts < scan_start:
                in_window = False
            if scan_end is not None and ts > scan_end:
                in_window = False
            last_in_window = in_window
            if in_window:
                result.append(line)
        else:
            # No timestamp — treat as continuation of the previous line
            if last_in_window:
                result.append(line)

    return "\n".join(result)

def extract_scan_ids(log_text):
    """
    Best-effort extraction of a subscan_id from log context.
    We no longer use this to link errors to a parent scan — that is done
    exclusively via the time-window gate in run_once using scan_start/scan_end.

    Supports 24-char hex MongoDB ObjectIDs and 36-char UUID format.
    Returns (scan_id, subscan_id) — scan_id is kept for legacy callers but
    run_once ignores it in favour of the DB active_scan_id.
    """
    scan_id = None
    subscan_id = None

    # 24-char hex MongoDB ObjectID
    s_match = re.search(r'(?i)(?:scan[_-]?id|scan[_-]?profile[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if not s_match:
        s_match = re.search(r'sdk-object-scan-([a-f0-9]{24})', log_text)
    if s_match:
        scan_id = s_match.group(1)

    ss_match = re.search(r'(?i)(?:sub[_-]?scan[_-]?id)[^\w\n]*?([a-f0-9]{24})', log_text)
    if not ss_match and s_match:
        ss_match = re.search(fr'sdk-object-scan-{scan_id}-([a-f0-9]{24})', log_text)
    if ss_match:
        subscan_id = ss_match.group(1)

    return scan_id, subscan_id


def _skip_info_level_line(line: str) -> bool:
    """
    Skip pure INFO/DEBUG/TRACE lines UNLESS they contain a strong error signal
    that BigID sometimes logs at INFO level (e.g. 401, 403, 500, exception, stacktrace).
    Set SCANNER_LOG_SKIP_INFO_LEVEL=0 to disable all INFO skipping.
    """
    if os.getenv("SCANNER_LOG_SKIP_INFO_LEVEL", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    if not re.search(r"\[(?:INFO|DEBUG|TRACE)\]", line, re.I):
        return False
    # Even on INFO/DEBUG/TRACE — keep the line if it has a strong signal
    STRONG_SIGNALS = re.compile(
        r"(?i)(?:"
        r"\berror\b|\bfatal\b|\bsevere\b|"
        r"\bexception\b|\bstacktrace\b|\bstack.?trace\b|\bnullpointer\b|\bnull.?pointer\b|"
        r"\bout.?of.?memory\b|\boom\b|\bkilled\b|\bpanic\b|\bcrash(?:ed)?\b|"
        r"\b(?:40[0-9]|5[0-9]{2})\b|"          # HTTP 4xx/5xx status codes
        r"\bunauthorized\b|\bforbidden\b|\bauthentication.?fail"
        r")"
    )
    return not bool(STRONG_SIGNALS.search(line))


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
    Default: accepts [ERROR]/[FATAL]/[SEVERE] in brackets OR bare ERROR/FATAL/SEVERE keywords
    (BigID logs use both formats). Set SCANNER_LOG_ERROR_REQUIRE_LEVEL=warn to also include
    WARN/WARNING, or any/legacy for keyword-only matching (old behavior).
    """
    mode = os.getenv("SCANNER_LOG_ERROR_REQUIRE_LEVEL", "error_fatal").strip().lower()
    if mode in ("any", "legacy", "all", "0", "off", "false", "no"):
        return True
    if mode in ("warn", "warning"):
        return bool(
            re.search(r"\[(?:ERROR|FATAL|SEVERE|WARN|WARNING)\]", line, re.I)
            or re.search(r"\b(?:ERROR|FATAL|SEVERE|WARN|WARNING)\b", line, re.I)
        )
    # error_fatal (default): match bracketed [ERROR]/[FATAL]/[SEVERE] or bare keywords
    return bool(
        re.search(r"\[(?:ERROR|FATAL|SEVERE)\]", line, re.I)
        or re.search(r"\b(?:ERROR|FATAL|SEVERE)\b", line, re.I)
    )


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

    Capture strategy (two independent paths — either is sufficient):
      A) Line has a severity tag [ERROR], [FATAL], [SEVERE], [WARN], [WARNING]
         → always captured regardless of message body content.
      B) Line has no severity tag but contains a known error keyword
         (exception, timeout, refused, 4xx/5xx status, etc.)
         → captured only when SCANNER_LOG_ERROR_REQUIRE_LEVEL allows it.

    This ensures lines like:
        [2026-04-29 08:52:34.712Z] [ERROR] scanner_file - <message with no keyword>
    are never silently dropped.
    """
    lines = log_text.splitlines()
    error_contexts = []
    seen_fingerprints = set()

    # Severity-tag pattern — presence alone is enough to capture the line
    LEVEL_PATTERN = re.compile(r"\[(?:ERROR|FATAL|SEVERE|WARN|WARNING)\]", re.I)

    # Keyword pattern — used as a secondary path when no severity tag is present.
    # Covers: HTTP error codes, auth failures, resource exhaustion, connectivity,
    # JVM/runtime crashes, and BigID-specific signals.
    KEYWORD_PATTERN = re.compile(
        r"(?i)(?:"
        # Explicit error/failure words
        r"\berror\b|\bfatal\b|\bfail(?:ed|ure)?\b|\bsevere\b|\bexception\b|"
        r"\baxioserror\b|\bnohttp\b|"
        # HTTP status codes (4xx and 5xx)
        r"\bstatus\s+[45]\d{2}\b|\bhttp\s+[45]\d{2}\b|"
        r"\b(?:400|401|403|404|408|409|429|500|502|503|504)\b|"
        # Auth / access
        r"\bunauthorized\b|\bforbidden\b|\bauthentication.?fail|\baccess.?denied\b|"
        r"\btoken.?(?:expired|invalid|fail)\b|\bcredential\b|"
        # Connectivity
        r"\btimeout\b|timed?\s*out|\brefused\b|\bunreachable\b|\bconnection.?(?:reset|closed|lost|drop)\b|"
        r"\bsocket\b|\beof\b|\bbroken.?pipe\b|"
        # Resource exhaustion / JVM
        r"\bdenied\b|\bexhausted\b|\bout.?of.?memory\b|\boom\b|"
        r"\bnull.?pointer\b|\bnullpointer\b|\bstack.?(?:trace|overflow)\b|\bstacktrace\b|"
        r"\bkilled\b|\bpanic\b|\bcrash(?:ed)?\b|"
        # Abort / throttle / limit
        r"\babort(?:ed|ing)?\b|\bthrottl(?:ing|ed)?\b|\blimitreached\b|"
        r"\bcircuit.?breaker\b|\bretry.?(?:limit|exhaust)\b|"
        # BigID-specific
        r"\bgraphservice\b|\bactivitylimit\b|\breset\b"
        r")"
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        # Skip pure INFO/DEBUG/TRACE lines — they are never errors
        if _skip_info_level_line(line):
            i += 1
            continue

        scan_line = _line_for_error_keyword_scan(line)  # URLs stripped for keyword matching

        # Path A: severity tag present → always a candidate
        has_level_tag = bool(LEVEL_PATTERN.search(line))
        # Path B: keyword match on URL-stripped line (only when no level tag)
        has_keyword   = bool(KEYWORD_PATTERN.search(scan_line)) if not has_level_tag else False

        is_trigger = has_level_tag or (has_keyword and _line_passes_error_level_gate(line))

        if not is_trigger:
            i += 1
            continue

        # Context before: capture 2 lines for timestamps/IDs
        start = max(0, i - 2)

        # Greedy multi-line capture (stack traces, indented JSON, etc.)
        end = i + 1
        while end < len(lines) and (end - i) < 30:
            next_line = lines[end]
            # If the next line is itself a new independent error, stop here
            next_has_level = bool(LEVEL_PATTERN.search(next_line))
            next_scan = _line_for_error_keyword_scan(next_line)
            next_has_kw = bool(KEYWORD_PATTERN.search(next_scan)) if not next_has_level else False
            if next_has_level or (next_has_kw and _line_passes_error_level_gate(next_line)):
                break
            if next_line.startswith((" ", "\t", "at ", "Caused by:", "---", "}", "]")):
                end += 1
            else:
                break

        # If no indented continuation, grab a small trailing context anyway
        if end == i + 1:
            end = min(len(lines), i + 4)

        ctx_lines = lines[start:end]
        ctx_text  = "\n".join(ctx_lines)

        # Extract timestamp from trigger line, fall back to context lines
        log_ts = _parse_log_timestamp(line)
        if not log_ts:
            for cl in ctx_lines:
                log_ts = _parse_log_timestamp(cl)
                if log_ts:
                    break

        if _looks_like_success_scan_summary(ctx_text):
            i = end
            continue

        # Fingerprint with timestamps masked so the same error at different times
        # is still deduplicated within a single fetch cycle
        fingerprint_text = re.sub(
            r'\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?', '[TS]', ctx_text
        )
        fingerprint = fingerprint_text[:300]

        if fingerprint not in seen_fingerprints:
            seen_fingerprints.add(fingerprint)
            error_hash = hashlib.md5(fingerprint.encode('utf-8')).hexdigest()
            error_contexts.append({
                "text":      ctx_text,
                "timestamp": log_ts,
                "hash":      error_hash,
            })

        i = end

    return error_contexts

def call_health_summarizer(log_text, ollama_available: Optional[bool] = None):
    ready = ollama_available if ollama_available is not None else check_ollama_ready()
    if not ready:
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

def call_ai_analyzer(error_context, log_source, ollama_available: Optional[bool] = None):
    ready = ollama_available if ollama_available is not None else check_ollama_ready()
    if not ready:
        return f"[⚠️ AI Analysis Offline] Ollama is not reachable at {OLLAMA_URL}. Please ensure Ollama is running. Raw error context: {error_context[:200]}..."
    
    prompt = f"""You are an expert systems engineer and log analyst specializing in BigID data intelligence platform.
I have extracted a log entry from the '{log_source}' service logs.

LOG CONTEXT:
{error_context}

Analyze this and provide:
1. SEVERITY: Rate as CRITICAL / HIGH / MEDIUM / LOW
2. WHAT FAILED: One sentence describing exactly what went wrong
3. ROOT CAUSE CATEGORY: Choose from: Network | Authentication | Resource | Configuration | Bug | Timeout | Permission | Unknown
4. IMPACT: What does this mean for the running scan or system?
5. RECOMMENDED ACTION: Specific steps to investigate or fix

Be concise and technical. If the log line looks like a normal operational message with no real problem, say "NOT AN ERROR - normal operation"."""
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


def call_llm_log_scanner(log_text, log_source, scan_start=None, scan_end=None,
                         ollama_available: Optional[bool] = None):
    """
    Send the full log window to the LLM and ask it to identify any anomalies,
    errors, or suspicious patterns that regex-based detection may have missed.

    Returns a list of dicts: [{"text": <context>, "severity": <str>, "summary": <str>}]
    or an empty list if nothing found or LLM is offline.
    """
    ready = ollama_available if ollama_available is not None else check_ollama_ready()
    if not ready:
        return []

    lines = log_text.splitlines()
    if len(lines) < 5:
        return []

    # Send up to 200 lines to keep within LLM context limits.
    # Prefer the full window; if larger, sample start + middle + end.
    MAX_LINES = 200
    if len(lines) <= MAX_LINES:
        window = log_text
    else:
        chunk = MAX_LINES // 3
        mid = len(lines) // 2
        window = (
            "\n".join(lines[:chunk]) +
            "\n\n[... middle section ...]\n\n" +
            "\n".join(lines[mid - chunk // 2: mid + chunk // 2]) +
            "\n\n[... tail section ...]\n\n" +
            "\n".join(lines[-chunk:])
        )

    time_ctx = ""
    if scan_start:
        time_ctx = f"Scan started at: {scan_start}"
    if scan_end:
        time_ctx += f"  |  Scan ended at: {scan_end}"

    prompt = f"""You are a senior BigID platform engineer reviewing logs from the '{log_source}' service.
{time_ctx}

Your job is to find ANY of the following in the log window below — even if they are not tagged [ERROR]:
- Authentication failures (401, 403, token expired, login failed)
- HTTP 4xx or 5xx responses
- Exceptions, stack traces, null pointer errors
- Timeouts, connection refused, socket errors
- Out of memory, resource exhaustion, process killed
- Repeated warnings that indicate a degraded state
- Any line that looks abnormal for a healthy scan

LOG WINDOW:
{window}

Respond ONLY as a JSON array. Each item must have:
  "line": the exact log line or short excerpt (max 300 chars)
  "severity": one of CRITICAL / HIGH / MEDIUM / LOW
  "category": one of Network | Authentication | Resource | Configuration | Bug | Timeout | Permission | Unknown
  "summary": one sentence explaining what is wrong

If you find nothing suspicious, respond with an empty array: []
Do not include normal operational log lines. Only real problems.
Respond with valid JSON only, no markdown, no explanation outside the array."""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=180
        )
        response.raise_for_status()
        raw = response.json().get("response", "[]").strip()

        # Strip markdown code fences if LLM wraps in ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)

        findings = json.loads(raw)
        if not isinstance(findings, list):
            return []
        # Validate shape
        result = []
        for f in findings:
            if isinstance(f, dict) and f.get("line") and f.get("summary"):
                result.append({
                    "text":     str(f.get("line", ""))[:500],
                    "severity": str(f.get("severity", "MEDIUM")),
                    "category": str(f.get("category", "Unknown")),
                    "summary":  str(f.get("summary", "")),
                })
        return result
    except Exception as e:
        print(f"[LLM log scanner] Failed for source={log_source}: {e}")
        return []

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
            print(f"[{datetime.now().isoformat()}] No active scans — log tracking idle.")
            write_heartbeat(engine, status="ok", message="No active scans")
            return

        # Check Ollama once per cycle — result is reused for every call_ai_analyzer /
        # call_llm_log_scanner in this cycle to avoid N×1.3 s per-error probe overhead.
        ollama_ok = check_ollama_ready()

        print(f"[{datetime.now().isoformat()}] Processing {len(envs)} environments...")

        for env in envs:
            env_id     = env['id']
            base_url   = env['base_url']
            env_name   = env['env_name']

            try:
                print(f"[{datetime.now().isoformat()}] [{env_name}] Fetching auth token...")
                token = get_auth_token(base_url, env)

                new_errors_by_scan = {}
                log_sources = load_enabled_log_sources(engine)

                for log_source in log_sources:
                    try:
                        scan_start     = _coerce_to_datetime(env.get("scan_start_time"))
                        scan_end       = _coerce_to_datetime(env.get("scan_end_time"))
                        linked_scan_id = env.get("active_scan_id")
                        # get_active_environments only returns running scans, so scan_end
                        # is always None here. Keep the check for safety.
                        is_running = scan_end is None

                        # ── Cursor logic ───────────────────────────────────────────────
                        # Cursor is scoped to (env_id, log_source, scan_id) so a new scan
                        # always starts fresh — it never inherits the previous scan's cursor.
                        if is_running:
                            cursor = _get_log_cursor(engine, env_id, log_source, str(linked_scan_id or ""))
                        else:
                            cursor = None

                        # effective_start = where to begin filtering this cycle.
                        # Guard: if both scan_start and cursor are None we have no anchor —
                        # skip this source rather than processing the entire log file.
                        if scan_start is None and cursor is None:
                            print(f"[{datetime.now().isoformat()}] [{env_name}] [{log_source}] No scan_start or cursor — skipping.")
                            continue

                        if is_running:
                            if scan_start and cursor:
                                effective_start = max(scan_start, cursor)
                            else:
                                effective_start = cursor or scan_start
                        else:
                            effective_start = scan_start  # completed scan: full window

                        # ── Fetch ──────────────────────────────────────────────────────
                        # For a running scan with a cursor, fetch only one interval's worth.
                        # For first cycle (no cursor), fetch from scan_start to now (capped).
                        fetch_count = _estimate_fetch_lines(scan_start, scan_end, cursor=cursor)
                        raw_logs = fetch_logs(base_url, token, fetch_count, log_source)
                        if not raw_logs:
                            continue

                        # ── Filter to scan window ──────────────────────────────────────
                        # effective_start is the lower bound; scan_end is the upper bound
                        # (None = no upper bound for running scans).
                        filtered_logs = _filter_lines_to_scan_window(raw_logs, effective_start, scan_end)
                        if not filtered_logs.strip():
                            # No new lines in window — advance cursor to now so next cycle
                            # doesn't re-fetch the same empty window.
                            if is_running:
                                _set_log_cursor(engine, env_id, log_source,
                                                str(linked_scan_id or ""), datetime.utcnow())
                            continue

                        # ── Helpers (defined here with explicit captures to avoid closure bugs) ──

                        def _line_hash(text: str, _re=re, _md5=hashlib.md5) -> str:
                            """Stable hash for a log line with timestamps masked."""
                            masked = _re.sub(
                                r'\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?',
                                '[TS]', text
                            )
                            return _md5(masked[:300].encode('utf-8')).hexdigest()

                        # Capture loop variables explicitly so closures are safe
                        _env_id         = env_id
                        _linked_scan_id = linked_scan_id
                        _log_source     = log_source

                        def _store_if_new(ctx, log_ts, err_hash, ai_analysis, subscan_id=None,
                                          _eid=_env_id, _sid=_linked_scan_id, _src=_log_source):
                            """Dedup-check against scan_id scope, then store. Returns True if stored."""
                            with engine.connect() as conn:
                                existing = conn.execute(
                                    text("""
                                        SELECT id FROM scan_log_errors
                                        WHERE environment_id = :eid
                                          AND scan_id        = :scan_id
                                          AND error_hash     = :hash
                                        LIMIT 1
                                    """),
                                    {"eid": _eid, "scan_id": str(_sid or ""), "hash": err_hash}
                                ).fetchone()
                            if existing:
                                return False
                            store_error_analysis(
                                engine, _eid, ctx, ai_analysis, _src,
                                _sid, subscan_id,
                                error_hash=err_hash,
                                actual_log_ts=log_ts
                            )
                            return True

                        # ── Track latest timestamp in this batch (for cursor advance) ──
                        latest_ts_in_batch: Optional[datetime] = None

                        def _update_latest(ts_val):
                            nonlocal latest_ts_in_batch
                            ts = _coerce_to_datetime(ts_val)
                            if ts and (latest_ts_in_batch is None or ts > latest_ts_in_batch):
                                latest_ts_in_batch = ts

                        # Seed latest_ts from all lines in the filtered window upfront,
                        # so the cursor advances even if no errors are found.
                        for _line in filtered_logs.splitlines():
                            _update_latest(_parse_log_timestamp(_line))

                        # ── Pass 1: regex-based detection ──────────────────────────────
                        seen_line_hashes: set = set()
                        errors = detect_errors_in_logs(filtered_logs)

                        for err_obj in errors:
                            ctx      = err_obj["text"]
                            log_ts   = err_obj["timestamp"] or datetime.utcnow()
                            err_hash = err_obj["hash"]

                            _update_latest(log_ts)

                            # Mark all lines in this context as seen so LLM skips them
                            for _ln in ctx.splitlines():
                                if _ln.strip():
                                    seen_line_hashes.add(_line_hash(_ln))

                            _, parsed_subscan_id = extract_scan_ids(ctx)
                            ai_analysis = call_ai_analyzer(ctx, log_source, ollama_available=ollama_ok)

                            if _store_if_new(ctx, log_ts, err_hash, ai_analysis, parsed_subscan_id):
                                new_errors_by_scan.setdefault(linked_scan_id, [])
                                new_errors_by_scan[linked_scan_id].append((log_source, ctx, linked_scan_id))

                        # ── Pass 2: LLM on deduplicated remainder ──────────────────────
                        # Only lines not already flagged by regex are sent to the LLM.
                        llm_input_lines = [
                            ln for ln in filtered_logs.splitlines()
                            if ln.strip() and _line_hash(ln) not in seen_line_hashes
                        ]

                        if llm_input_lines:
                            llm_findings = call_llm_log_scanner(
                                "\n".join(llm_input_lines), log_source,
                                scan_start=str(effective_start) if effective_start else None,
                                scan_end=str(scan_end) if scan_end else None,
                                ollama_available=ollama_ok,
                            )
                            for finding in llm_findings:
                                ctx_text = (
                                    f"[LLM-DETECTED | {finding['severity']} | {finding['category']}]\n"
                                    f"{finding['text']}\n\n"
                                    f"AI Summary: {finding['summary']}"
                                )
                                fp = re.sub(
                                    r'\d{4}-\d{2}-\d{2}T?\s?\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?',
                                    '[TS]', ctx_text
                                )
                                err_hash   = hashlib.md5(fp[:300].encode('utf-8')).hexdigest()
                                log_ts     = _parse_log_timestamp(finding["text"]) or datetime.utcnow()
                                ai_detail  = call_ai_analyzer(finding["text"], log_source, ollama_available=ollama_ok)
                                full_analysis = f"[LLM Scanner: {finding['summary']}]\n\n{ai_detail}"

                                if _store_if_new(ctx_text, log_ts, err_hash, full_analysis):
                                    new_errors_by_scan.setdefault(linked_scan_id, [])
                                    new_errors_by_scan[linked_scan_id].append(
                                        (log_source, ctx_text, linked_scan_id)
                                    )
                                    print(f"[{datetime.now().isoformat()}] [{env_name}] "
                                          f"LLM found new issue in {log_source}: {finding['summary'][:80]}")

                        # ── Advance cursor ─────────────────────────────────────────────
                        # Only for running scans. Scoped to scan_id so a new scan starts
                        # fresh. We always advance even if no errors were found, so the
                        # next cycle doesn't re-process the same lines.
                        if is_running and latest_ts_in_batch:
                            _set_log_cursor(engine, env_id, log_source,
                                            str(linked_scan_id or ""), latest_ts_in_batch)

                    except Exception as source_err:
                        print(f"[{datetime.now().isoformat()}] [{env_name}] "
                              f"Error processing source {log_source}: {source_err}")

            except Exception as env_err:
                print(f"[{datetime.now().isoformat()}] [ERROR] Failed processing environment {env_name}: {env_err}")
                continue

        write_heartbeat(engine, status="ok",
                        message=f"Processed {len(envs)} environments at {datetime.now().isoformat()}")
    except Exception as global_err:
        print(f"[{datetime.now().isoformat()}] CRITICAL: Global run_once failure: {global_err}")

def main():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    print(f"Starting Scanner AI Log Analyzer (Fetch Interval: {FETCH_INTERVAL_MINUTES} min, LLM: {OLLAMA_MODEL})")
    
    if not check_ollama_ready(use_cache=False):
        print(f"WARNING: Ollama is not reachable at {OLLAMA_URL}. The monitor will continue to detect errors but AI analysis will be skipped until Ollama is active.")
        # DO NOT EXIT - continue to monitor logs even without AI
        
    engine = setup_db_engine()
    
    while True:
        run_once(engine)
        print(f"Sleeping for {FETCH_INTERVAL_MINUTES} minutes...")
        time.sleep(FETCH_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
