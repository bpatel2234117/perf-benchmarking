# scan_benchmark_app → incident_intelligence Integration Guide

## Overview

Send scan logs and insights to BigID Incident Intelligence **without stopping** any existing writes to scan_benchmark_app DB.

**Architecture:** Dual-write with feature flag control via environment variable.

---

## Quick Start

### Step 1: Configure Environment

Add to `.env` or Docker environment:

```bash
# Enable/disable feature
INCIDENT_INTELLIGENCE_ENABLED=1

# Where to send logs
INCIDENT_INTELLIGENCE_API_URL=http://incident-intelligence:8080/api/logs/ingest

# Optional: API authentication
INCIDENT_INTELLIGENCE_API_KEY=

# Optional: Sync interval
INCIDENT_INTELLIGENCE_SYNC_INTERVAL=10
```

### Step 2: Use the Pusher in Your Code

```python
# Import the pusher
from v2_api.incident_intelligence_pusher import push_to_incident_intelligence, get_pusher

# After writing error to scan_log_errors table
error_row = {
    'id': error_id,
    'log_timestamp': datetime.utcnow(),
    'error_context': error_message,
    'log_source': 'scanner',
    'scan_id': scan_id,
    'environment_id': env_id,
    'ai_analysis': ai_result,
}

# Transform and push (if enabled)
pusher = get_pusher()
if pusher.should_push():
    log_event = pusher.transform_error_to_log(error_row)
    push_to_incident_intelligence([log_event])

# scan_benchmark_app DB write is UNAFFECTED by this
```

### Step 3: Test

```bash
# Test pushing
export INCIDENT_INTELLIGENCE_ENABLED=1
export INCIDENT_INTELLIGENCE_API_URL=http://localhost:8080/api/logs/ingest

python3 -c "from v2_api.incident_intelligence_pusher import test_push; test_push()"

# Should see: "✓ Pushed X logs to incident_intelligence"
```

---

## Configuration

### Environment Variables

| Variable | Required | Default | Example |
|----------|----------|---------|---------|
| `INCIDENT_INTELLIGENCE_ENABLED` | No | `0` | `1` (enable) or `0` (disable) |
| `INCIDENT_INTELLIGENCE_API_URL` | No | `http://localhost:8080/api/logs/ingest` | `http://incident-int:8080/api/logs/ingest` |
| `INCIDENT_INTELLIGENCE_API_KEY` | No | `` | `your-api-key-if-needed` |
| `INCIDENT_INTELLIGENCE_SYNC_INTERVAL` | No | `10` | `5` (seconds) |

### Docker Compose

```yaml
services:
  scan_benchmark_app:
    build: .
    environment:
      # Existing (unchanged)
      DATABASE_URL: postgresql://user:pass@db:5432/scan_benchmark_app
      
      # NEW (optional)
      INCIDENT_INTELLIGENCE_ENABLED: "1"
      INCIDENT_INTELLIGENCE_API_URL: "http://incident-intelligence:8080/api/logs/ingest"
    
    depends_on:
      - db
      - incident-intelligence  # Optional

  incident-intelligence:
    image: incident-intelligence:latest
    ports:
      - "8080:8080"
```

### Kubernetes Secrets

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: scan-benchmark-app-config
data:
  INCIDENT_INTELLIGENCE_ENABLED: "1"
  INCIDENT_INTELLIGENCE_API_URL: "http://incident-intelligence:8080/api/logs/ingest"

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scan-benchmark-app
spec:
  template:
    spec:
      containers:
      - name: app
        envFrom:
        - configMapRef:
            name: scan-benchmark-app-config
```

---

## Integration Points

### 1. After scan_log_errors Insert

```python
# File: v2_api/main.py (or wherever you write scan_log_errors)

from v2_api.incident_intelligence_pusher import push_to_incident_intelligence, get_pusher

# ... existing insert code ...

error_id = db.insert_scan_log_error(
    scan_id=scan_id,
    error_context=error_msg,
    log_source="scanner",
    ai_analysis=ai_result,
    environment_id=env_id,
)

# ADDITIONALLY: Push if enabled
pusher = get_pusher()
if pusher.should_push():
    error_row = {
        'id': error_id,
        'log_timestamp': datetime.utcnow(),
        'error_context': error_msg,
        'log_source': 'scanner',
        'scan_id': scan_id,
        'environment_id': env_id,
        'ai_analysis': ai_result,
    }
    log_event = pusher.transform_error_to_log(error_row)
    push_to_incident_intelligence([log_event])
```

### 2. After scan_progress Update

```python
# File: v2_api/tracker.py (or wherever you write scan_progress)

from v2_api.incident_intelligence_pusher import push_to_incident_intelligence, get_pusher

# ... existing update code ...

db.update_scan_progress(
    scan_id=scan_id,
    scan_state=state,
    objects_scanned=count,
)

# ADDITIONALLY: Push if enabled
pusher = get_pusher()
if pusher.should_push():
    progress_row = {
        'scan_id': scan_id,
        'scan_name': scan_name,
        'scan_state': state,
        'objects_scanned': count,
        'timestamp': datetime.utcnow(),
    }
    log_event = pusher.transform_progress_to_log(progress_row)
    push_to_incident_intelligence([log_event])
```

### 3. Periodic Sync Function (Alternative)

Instead of pushing on each write, batch and push periodically:

```python
# File: v2_api/periodic_tasks.py (new or existing)

import asyncio
from datetime import datetime, timedelta
from v2_api.incident_intelligence_pusher import push_to_incident_intelligence, get_pusher

last_sync = datetime.utcnow()

async def sync_logs_to_incident_intelligence():
    """Periodically sync logs to incident_intelligence."""
    global last_sync
    
    pusher = get_pusher()
    if not pusher.should_push():
        return
    
    try:
        # Query recent errors since last sync
        recent_errors = db.query("""
            SELECT * FROM scan_log_errors 
            WHERE created_at > :last_sync
            ORDER BY created_at DESC
            LIMIT 100
        """, {'last_sync': last_sync})
        
        if recent_errors:
            # Transform and push
            logs_to_push = [
                pusher.transform_error_to_log(error)
                for error in recent_errors
            ]
            push_to_incident_intelligence(logs_to_push)
            
            # Update sync time
            last_sync = datetime.utcnow()
    
    except Exception as e:
        logger.error(f"Error syncing logs: {e}")
        # Don't fail - will retry next cycle

# Run every 10 seconds in background
async def start_sync_loop():
    while True:
        await sync_logs_to_incident_intelligence()
        await asyncio.sleep(10)  # INCIDENT_INTELLIGENCE_SYNC_INTERVAL

# Call this on app startup:
# asyncio.create_task(start_sync_loop())
```

---

## Safety Guarantees

| Guarantee | How |
|-----------|-----|
| **Existing writes continue** | Middleware is additive, no changes to existing DB writes |
| **No blocking** | 5-second timeout, exception handling |
| **No data loss** | All data stays in scan_benchmark_app DB |
| **Can disable** | Set `INCIDENT_INTELLIGENCE_ENABLED=0` |
| **Fail-safe** | If incident_intelligence is down, scan_benchmark_app unaffected |
| **Non-critical** | Logging only, doesn't affect scan execution |

---

## Testing

### Test 1: Verify Disabled (Default)

```bash
# Default: disabled
# No INCIDENT_INTELLIGENCE_ENABLED set

python3 -c "
from v2_api.incident_intelligence_pusher import get_pusher
print(f'Enabled: {get_pusher().should_push()}')
# Should print: Enabled: False
"

# Run scan
./run_scan.sh

# Check scan_benchmark_app DB has all data
psql scan_benchmark_app << EOF
SELECT COUNT(*) FROM scan_log_errors;
EOF
# Should have errors
```

### Test 2: Enable and Test

```bash
# Enable
export INCIDENT_INTELLIGENCE_ENABLED=1
export INCIDENT_INTELLIGENCE_API_URL=http://localhost:8080/api/logs/ingest

# Start incident_intelligence
cd ../BIGID_INCIDENT_INTELLIGENCE
python3 serve_ui.py &

# Test push
python3 -c "
from v2_api.incident_intelligence_pusher import test_push
test_push()
"
# Should see: "✓ Pushed X logs"

# Verify both DBs have data
psql scan_benchmark_app << EOF
SELECT COUNT(*) FROM scan_log_errors;
EOF

psql incident_intelligence << EOF
SELECT COUNT(*) FROM events WHERE source_system = 'scan_benchmark_app' AND is_live_log = TRUE;
EOF
```

### Test 3: Resilience (API Down)

```bash
# Keep enabled
export INCIDENT_INTELLIGENCE_ENABLED=1

# Stop incident_intelligence
pkill -f "python3 serve_ui.py"

# Run scan
./run_scan.sh

# Verify scan_benchmark_app DB still has data (unaffected)
psql scan_benchmark_app << EOF
SELECT COUNT(*) FROM scan_log_errors;
EOF
# Should still have errors ✓
```

---

## Implementation Checklist

- [ ] Add environment variables to `.env`
- [ ] Add environment variables to `docker-compose.yml`
- [ ] Import pusher in log write locations (3-5 places)
- [ ] Add `if pusher.should_push():` check
- [ ] Transform and call `push_to_incident_intelligence()`
- [ ] Test with `INCIDENT_INTELLIGENCE_ENABLED=0` (default behavior)
- [ ] Test with `INCIDENT_INTELLIGENCE_ENABLED=1` (push enabled)
- [ ] Test with incident_intelligence API down (resilience)

---

## Troubleshooting

### Logs not appearing in incident_intelligence

**Check 1:** Is env var set?
```bash
echo $INCIDENT_INTELLIGENCE_ENABLED
# Should be 1
```

**Check 2:** Is incident_intelligence running?
```bash
curl http://localhost:8080/api/docs
# Should return OpenAPI docs
```

**Check 3:** Check logs for errors
```bash
tail -f logs/scan_benchmark_app.log | grep "incident_intelligence"
# Look for error messages
```

### Scan benchmark app slowing down

**Check:** Timeout is 5 seconds
- If incident_intelligence API is slow, requests will timeout
- But scan_benchmark_app continues normally (non-blocking)
- Increase timeout if needed in `incident_intelligence_pusher.py`

### Data not in scan_benchmark_app DB

**This should never happen** - verify pusher is being called:

```python
# Add debug logging
pusher = get_pusher()
if pusher.should_push():
    logger.info(f"About to push {len(logs)} logs")
    push_to_incident_intelligence(logs)
else:
    logger.info("Pusher disabled, skipping push")
```

---

## Files

📁 **Middleware:** `/v2_api/incident_intelligence_pusher.py`
- Core logic
- Configuration loading
- Transform functions
- Error handling

📁 **Integration Guide:** This file
- Setup instructions
- Configuration examples
- Testing procedures

---

## Support

For questions about the integration:
1. Check this file
2. Check `incident_intelligence_pusher.py` docstrings
3. Enable debug logging: `logger.setLevel(logging.DEBUG)`

For incident_intelligence side issues:
- See `/BIGID_INCIDENT_INTELLIGENCE/FINAL_INTEGRATION_PLAN.md`
- See `/BIGID_INCIDENT_INTELLIGENCE/LIVE_LOGS_PUSH_API.md`

---

## Summary

✅ Dual-write architecture (both DBs get data)
✅ Feature-flagged (toggle via env var)
✅ Non-blocking (5-sec timeout)
✅ Safe (no data loss)
✅ Backward compatible (defaults to disabled)

**Ready to integrate!**
