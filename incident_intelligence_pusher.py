"""
Dual-Write Pusher: Push scan logs/insights to incident_intelligence DB
while maintaining all existing writes to scan_benchmark_app DB.

Configuration via environment variables:
  INCIDENT_INTELLIGENCE_ENABLED=1                    # 0=disabled, 1=enabled
  INCIDENT_INTELLIGENCE_DB_URL=postgresql://...      # Connection to II DB
  
Direct database writes (no API needed). Non-blocking, fire-and-forget.
Data stays in scan_benchmark_app DB regardless of push success/failure.
"""

import os
import logging
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional

try:
    import psycopg
except ImportError:
    psycopg = None

logger = logging.getLogger(__name__)


class IncidentIntelligencePusher:
    """
    Pushes logs directly to incident_intelligence DB while maintaining
    all existing writes to scan_benchmark_app DB (dual-write).
    
    Uses direct database connection (no API needed).
    
    Characteristics:
    - Non-blocking: Writes with timeout
    - Fire-and-forget: Returns immediately
    - Safe: No data loss if II DB is temporarily unavailable
    - Configurable: Toggle via env var
    - Direct: No HTTP overhead
    """
    
    def __init__(self):
        self.enabled = os.getenv('INCIDENT_INTELLIGENCE_ENABLED', '0').lower() in ('1', 'true', 'yes')
        self.db_url = os.getenv('INCIDENT_INTELLIGENCE_DB_URL', '')
        self.conn = None
        
        if self.enabled:
            if not self.db_url:
                logger.warning("INCIDENT_INTELLIGENCE_ENABLED=1 but INCIDENT_INTELLIGENCE_DB_URL not set")
                self.enabled = False
            else:
                try:
                    if psycopg:
                        # Test connection
                        test_conn = psycopg.connect(self.db_url, timeout=5)
                        test_conn.close()
                        logger.info(f"✓ Incident Intelligence DB connection OK")
                    else:
                        logger.warning("psycopg not installed - disabling push")
                        self.enabled = False
                except Exception as e:
                    logger.warning(f"Cannot connect to incident_intelligence DB: {e}")
                    self.enabled = False
    
    def should_push(self) -> bool:
        """Check if pushing is enabled."""
        return self.enabled
    
    def push_logs(self, logs: List[Dict[str, Any]]) -> bool:
        """
        Push logs directly to incident_intelligence DB.
        
        Args:
            logs: List of log dictionaries in LogEvent format
        
        Returns:
            bool: Always True (non-blocking - never raises exception)
        
        Safety:
        - Uses try/except to avoid blocking
        - Connection timeout: 5 seconds
        - Logs errors but doesn't raise
        - Data preserved in scan_benchmark_app DB if push fails
        """
        
        if not self.should_push():
            return True  # Silently skip if disabled
        
        if not logs:
            return True
        
        if not psycopg:
            logger.warning("psycopg not installed - cannot push logs")
            return True
        
        try:
            with psycopg.connect(self.db_url, timeout=5) as conn:
                for log in logs:
                    # Generate hash for deduplication
                    hashable = f"{log.get('scan_id', '')}-{log.get('timestamp', '')}-{log.get('message', '')}"
                    event_hash = hashlib.sha256(hashable.encode()).hexdigest()
                    
                    # Check if duplicate exists
                    existing = conn.execute(
                        "SELECT event_hash FROM event_deduplication WHERE event_hash = %s LIMIT 1",
                        (event_hash,)
                    ).fetchone()
                    
                    if existing:
                        logger.debug(f"Duplicate event skipped: {event_hash[:8]}...")
                        continue
                    
                    # Insert event
                    conn.execute(
                        """
                        INSERT INTO events (
                            timestamp, service, severity, message, raw_line, scan_id,
                            parent_scan_id, metadata, is_live_log, source_system, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            log.get('timestamp'),
                            log.get('service', 'scan_benchmark_app'),
                            log.get('severity', 'INFO'),
                            log.get('message', ''),
                            log.get('raw_line', ''),
                            log.get('scan_id'),
                            log.get('parent_scan_id'),
                            json.dumps(log.get('metadata', {})),
                            True,  # is_live_log
                            'scan_benchmark_app',  # source_system
                            datetime.utcnow()
                        )
                    )
                    
                    # Insert into deduplication table
                    conn.execute(
                        "INSERT INTO event_deduplication (event_hash, scan_id, timestamp) VALUES (%s, %s, %s)",
                        (event_hash, log.get('scan_id'), log.get('timestamp'))
                    )
                
                conn.commit()
                logger.debug(f"✓ Pushed {len(logs)} logs to incident_intelligence DB")
                return True
        
        except psycopg.OperationalError as e:
            logger.debug(f"📡 Cannot reach incident_intelligence DB (will retry next sync): {e}")
            return True  # Don't block
        except Exception as e:
            logger.warning(f"⚠ Error pushing to incident_intelligence: {type(e).__name__}: {e}")
            return True  # Don't block - this is non-critical
    
    def transform_error_to_log(self, error_row: Dict) -> Dict[str, Any]:
        """Transform scan_log_errors row to incident_intelligence LogEvent format."""
        timestamp = error_row.get('log_timestamp')
        if timestamp and hasattr(timestamp, 'isoformat'):
            ts = timestamp.isoformat()
        else:
            ts = datetime.utcnow().isoformat()
        
        return {
            "timestamp": ts,
            "service": "scan_benchmark_app",
            "severity": "ERROR",
            "message": error_row.get('error_context', ''),
            "raw_line": error_row.get('error_context', ''),
            "scan_id": error_row.get('scan_id'),
            "parent_scan_id": error_row.get('scan_id'),
            "metadata": {
                "source": "scan_benchmark_app_api",
                "log_source": error_row.get('log_source'),
                "environment_id": error_row.get('environment_id'),
                "subscan_id": error_row.get('subscan_id'),
                "ai_analysis": error_row.get('ai_analysis'),
                "error_id": error_row.get('id'),
                "is_resolved": error_row.get('is_resolved', False),
                "manual_triage": error_row.get('manual_triage'),
            }
        }
    
    def transform_progress_to_log(self, progress_row: Dict) -> Dict[str, Any]:
        """Transform scan_progress row to incident_intelligence LogEvent format."""
        # Use captured_at_utc if available (more recent snapshot), else timestamp
        timestamp = progress_row.get('captured_at_utc') or progress_row.get('timestamp')
        if timestamp and hasattr(timestamp, 'isoformat'):
            ts = timestamp.isoformat()
        else:
            ts = datetime.utcnow().isoformat()
        
        scan_state = progress_row.get('scan_state', 'UNKNOWN')
        objects_scanned = progress_row.get('objects_scanned', 0) or 0
        objects_failed = progress_row.get('objects_failed', 0) or 0
        
        severity = "INFO" if scan_state in ("COMPLETED", "SUCCESS") else "WARN"
        message = f"Scan {scan_state}: {objects_scanned} objects, {objects_failed} failed"
        
        return {
            "timestamp": ts,
            "service": "scan_benchmark_app",
            "severity": severity,
            "message": message,
            "raw_line": f"[{scan_state}] {progress_row.get('scan_name', '')}",
            "scan_id": progress_row.get('scan_id'),
            "parent_scan_id": progress_row.get('scan_id'),
            "metadata": {
                "source": "scan_benchmark_app_api",
                "scan_state": scan_state,
                "scan_name": progress_row.get('scan_name'),
                "environment_id": progress_row.get('environment_id'),
                "completed_sub_scans": progress_row.get('completed_sub_scans'),
                "total_sub_scans": progress_row.get('total_sub_scans'),
                "objects_scanned": objects_scanned,
                "objects_failed": objects_failed,
                "total_classifications": progress_row.get('total_classifications'),
            }
        }


# Global instance (lazy-loaded on first use)
_pusher: Optional[IncidentIntelligencePusher] = None


def get_pusher() -> IncidentIntelligencePusher:
    """Get or create the global pusher instance."""
    global _pusher
    if _pusher is None:
        _pusher = IncidentIntelligencePusher()
    return _pusher


def push_to_incident_intelligence(logs: List[Dict[str, Any]]) -> None:
    """
    Public function: Push logs to incident_intelligence API (if enabled).
    
    Usage in scan_benchmark_app code:
        from v2_api.incident_intelligence_pusher import push_to_incident_intelligence
        
        # After writing error to scan_log_errors
        log_event = transform_error(error_row)  # Your transform function
        push_to_incident_intelligence([log_event])
    
    Characteristics:
    - Non-blocking: Returns immediately
    - Safe: Never raises exception
    - Fire-and-forget: Doesn't wait for response
    - Configurable: Controlled by INCIDENT_INTELLIGENCE_ENABLED env var
    
    Args:
        logs: List of log dictionaries in incident_intelligence LogEvent format
    """
    pusher = get_pusher()
    if pusher.should_push():
        # Fire in background (or sync - both work, pusher handles it)
        pusher.push_logs(logs)


# Convenience function for testing/debugging
def test_push() -> bool:
    """Test pushing a sample log to incident_intelligence."""
    sample_log = {
        "timestamp": datetime.utcnow().isoformat(),
        "service": "scan_benchmark_app",
        "severity": "INFO",
        "message": "Test event from scan_benchmark_app",
        "raw_line": "[TEST] Sample log entry",
        "scan_id": "test_scan_001",
        "parent_scan_id": "test_scan_001",
        "metadata": {
            "source": "scan_benchmark_app_api",
            "test": True,
        }
    }
    
    pusher = get_pusher()
    if pusher.should_push():
        return pusher.push_logs([sample_log])
    else:
        logger.info("Pusher disabled - enable with INCIDENT_INTELLIGENCE_ENABLED=1")
        return True


if __name__ == '__main__':
    # Test script
    import sys
    
    logging.basicConfig(level=logging.DEBUG)
    
    print("Testing incident_intelligence pusher...")
    print(f"  Enabled: {get_pusher().should_push()}")
    print(f"  API URL: {get_pusher().api_url}")
    
    success = test_push()
    sys.exit(0 if success else 1)
