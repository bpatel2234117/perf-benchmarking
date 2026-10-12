# Docker Integration: How Pusher Works in Containerized Environment

## Architecture: Services Communicating via HTTP

```
┌─────────────────────────────────────────────────────────────────┐
│ Docker Network (default bridge or custom network)               │
│                                                                 │
│  ┌────────────────────────┐         ┌──────────────────────┐  │
│  │ scan_benchmark_app     │         │ incident_intelligence│  │
│  │ (Service)              │         │ (Service)            │  │
│  │                        │         │                      │  │
│  │ ┌──────────────────┐   │         │ ┌────────────────┐   │  │
│  │ │ Pusher Middleware│   │         │ │ API Endpoint   │   │  │
│  │ │ (HTTP POST)      │   │         │ │ POST /api/     │   │  │
│  │ └──────────────────┘   │         │ │ logs/ingest    │   │  │
│  │         │               │         │ └────────────────┘   │  │
│  │         │               │         │         ↑            │  │
│  │         └───────────────┼─────────┼─────────┘            │  │
│  │    HTTP POST            │         │                      │  │
│  │ /api/logs/ingest        │         │ Port: 8080           │  │
│  │                        │         │                      │  │
│  │ Hostname:             │         │ Hostname:           │  │
│  │ localhost:8000        │         │ incident-intelligence│  │
│  │ (within container)    │         │ (or localhost:8080)  │  │
│  └────────────────────────┘         └──────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Points: Why It Works

### 1. Services on Same Docker Network

When you use `docker-compose`, all services are on the same **custom bridge network** by default.

```yaml
# docker-compose.yml
services:
  scan_benchmark_app:
    ports:
      - "8000:8000"  # Only needed for external access
    # NO explicit networking needed - on compose network by default
  
  incident-intelligence:
    ports:
      - "8080:8080"  # Only needed for external access
    # Also on same compose network
```

**Result:** Services can reach each other by **service name** (hostname).

### 2. DNS Resolution in Docker

Docker includes a built-in DNS server:

```
scan_benchmark_app container
    ↓
Resolve: "incident-intelligence"
    ↓
Docker DNS (127.0.0.11:53)
    ↓
Returns: 172.17.0.2 (incident-intelligence IP)
    ↓
HTTP connection established
```

### 3. Pusher Uses HTTP (Not Direct DB Access)

```python
# This is HTTP, not database connection
requests.post(
    self.api_url,  # "http://incident-intelligence:8080/api/logs/ingest"
    json=logs,
    headers=headers,
    timeout=5
)
```

**vs if it were DB access (which would be harder in Docker):**
```python
# This would require network-based PostgreSQL connection
# (possible but more complex - not needed here)
conn = psycopg2.connect("postgresql://user@incident-intelligence:5432/...")
```

---

## Docker Compose Configuration

### Simple Setup (Both Services)

```yaml
version: '3.8'

services:
  # scan_benchmark_app
  scan_benchmark_app:
    build: .
    ports:
      - "8000:8000"
    environment:
      # Existing config
      DATABASE_URL: postgresql://user:pass@db:5432/scan_benchmark_app
      
      # NEW: Enable pusher
      INCIDENT_INTELLIGENCE_ENABLED: "1"
      INCIDENT_INTELLIGENCE_API_URL: "http://incident-intelligence:8080/api/logs/ingest"
    
    depends_on:
      - db
      - incident-intelligence
    
    networks:
      - bigid-network
  
  # incident_intelligence
  incident-intelligence:
    image: incident-intelligence:latest
    # or build from Dockerfile
    # build: ../BIGID_INCIDENT_INTELLIGENCE
    
    ports:
      - "8080:8080"
    
    environment:
      DATABASE_URL: postgresql://user:pass@db:5432/incident_intelligence
    
    depends_on:
      - db
    
    networks:
      - bigid-network
  
  # PostgreSQL (shared)
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: pass
    
    volumes:
      - postgres_data:/var/lib/postgresql/data
    
    networks:
      - bigid-network

# Network
networks:
  bigid-network:
    driver: bridge

volumes:
  postgres_data:
```

### Key: Service Names as Hostnames

```yaml
# In scan_benchmark_app container:
INCIDENT_INTELLIGENCE_API_URL: "http://incident-intelligence:8080/api/logs/ingest"
                                      ↑
                                      Service name from docker-compose.yml
```

Inside the container, Docker's DNS translates `incident-intelligence` to the correct IP.

---

## How Pusher Resolves URL in Docker

### Example: Running in Docker

```bash
# Start services
docker-compose up
```

### Inside scan_benchmark_app container:

```python
# Push middleware runs
pusher = IncidentIntelligencePusher()

# Reads env var
api_url = os.getenv('INCIDENT_INTELLIGENCE_API_URL')
# = "http://incident-intelligence:8080/api/logs/ingest"

# Makes HTTP request
response = requests.post(
    api_url,
    json=logs,
    timeout=5
)

# Docker DNS resolves "incident-intelligence" → 172.17.0.3 (IP)
# Request goes to: http://172.17.0.3:8080/api/logs/ingest
# Response comes back with logs ingested
```

---

## Different Deployment Scenarios

### Scenario 1: Local Development (Both on localhost)

```bash
# Terminal 1: incident_intelligence
cd BIGID_INCIDENT_INTELLIGENCE
python3 serve_ui.py
# Listens on http://localhost:8080

# Terminal 2: scan_benchmark_app
cd scan_benchmark_app
export INCIDENT_INTELLIGENCE_API_URL=http://localhost:8080/api/logs/ingest
python3 v2_api/main.py
# Resolves localhost → 127.0.0.1 → connects to incident_intelligence
```

**Configuration:**
```bash
INCIDENT_INTELLIGENCE_ENABLED=1
INCIDENT_INTELLIGENCE_API_URL=http://localhost:8080/api/logs/ingest
```

### Scenario 2: Docker Compose (Services on Same Network)

```yaml
# docker-compose.yml
services:
  scan_benchmark_app:
    environment:
      INCIDENT_INTELLIGENCE_API_URL: http://incident-intelligence:8080/api/logs/ingest
  
  incident-intelligence:
    ports:
      - "8080:8080"
```

**How it works:**
- Docker DNS translates `incident-intelligence` → Container IP
- Service can reach each other by name

### Scenario 3: Kubernetes (Services via DNS)

```yaml
# kubernetes service discovery
apiVersion: v1
kind: Service
metadata:
  name: incident-intelligence
spec:
  ports:
  - port: 8080

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
        env:
        - name: INCIDENT_INTELLIGENCE_API_URL
          value: http://incident-intelligence:8080/api/logs/ingest
```

**How it works:**
- Kubernetes DNS: `incident-intelligence` → `incident-intelligence.default.svc.cluster.local`
- Pod can reach service by name

### Scenario 4: Multiple Docker Networks

```bash
# Create network
docker network create bigid-network

# Run scan_benchmark_app
docker run \
  --network bigid-network \
  --name scan_benchmark_app \
  -e INCIDENT_INTELLIGENCE_API_URL=http://incident-intelligence:8080/api/logs/ingest \
  scan_benchmark_app:latest

# Run incident_intelligence
docker run \
  --network bigid-network \
  --name incident-intelligence \
  -p 8080:8080 \
  incident-intelligence:latest
```

**How it works:**
- Both on `bigid-network`
- Can reach each other by service name

---

## Network Resolution Summary

| Environment | Hostname Resolution | Example |
|-------------|-------------------|---------|
| **Local** | OS localhost | `http://localhost:8080` |
| **Docker Compose** | Docker embedded DNS | `http://incident-intelligence:8080` |
| **Docker Network** | Docker embedded DNS | `http://incident-intelligence:8080` |
| **Kubernetes** | Kube DNS | `http://incident-intelligence:8080` |
| **Cloud (AWS/GCP)** | Cloud load balancer | `http://incident-intelligence.example.com` |

---

## Configuration by Environment

### Development (.env)

```bash
INCIDENT_INTELLIGENCE_ENABLED=1
INCIDENT_INTELLIGENCE_API_URL=http://localhost:8080/api/logs/ingest
```

### Docker Compose

```yaml
environment:
  INCIDENT_INTELLIGENCE_ENABLED: "1"
  INCIDENT_INTELLIGENCE_API_URL: "http://incident-intelligence:8080/api/logs/ingest"
```

### Kubernetes

```yaml
env:
- name: INCIDENT_INTELLIGENCE_ENABLED
  value: "1"
- name: INCIDENT_INTELLIGENCE_API_URL
  value: "http://incident-intelligence:8080/api/logs/ingest"
```

### Cloud Deployment (e.g., AWS)

```bash
INCIDENT_INTELLIGENCE_ENABLED=1
INCIDENT_INTELLIGENCE_API_URL=https://incident-intelligence.prod.example.com/api/logs/ingest
INCIDENT_INTELLIGENCE_API_KEY=sk-prod-key-xxx
```

---

## Testing Connectivity

### From scan_benchmark_app Container

```bash
# Connect to container
docker exec -it scan_benchmark_app bash

# Test connectivity
curl -X GET http://incident-intelligence:8080/api/docs

# Should return OpenAPI docs if incident_intelligence is running
```

### From Host Machine

```bash
# Check if incident_intelligence is running
curl http://localhost:8080/api/docs

# Check scan_benchmark_app is running
curl http://localhost:8000/health
```

---

## Debugging Network Issues

### Issue: "Cannot reach incident-intelligence"

**Check 1:** Are both services running?
```bash
docker ps
# Both should be listed
```

**Check 2:** Are they on the same network?
```bash
docker network inspect bigid-network
# Both services should be listed
```

**Check 3:** Is the hostname correct?
```bash
# Inside scan_benchmark_app container
docker exec scan_benchmark_app getent hosts incident-intelligence
# Should return IP address
```

**Check 4:** Can you ping?
```bash
# Inside scan_benchmark_app container
docker exec scan_benchmark_app ping incident-intelligence
# Should get responses
```

**Check 5:** Can you telnet to the port?
```bash
# Inside scan_benchmark_app container
docker exec scan_benchmark_app telnet incident-intelligence 8080
# Should connect
```

---

## Performance Considerations

### Latency

- **Local (localhost):** < 1ms
- **Docker Compose (same network):** ~1-5ms
- **Kubernetes (same cluster):** ~5-10ms
- **Cross-datacenter (cloud):** ~50-500ms

**Impact on scan_benchmark_app:**
- Timeout: 5 seconds
- Non-blocking: Doesn't matter if slow
- Fire-and-forget: Doesn't wait for response

### Bandwidth

- Typical log: ~500 bytes
- Batch: 50 logs = ~25 KB
- Frequency: Every 10 seconds
- Total: ~250 KB/min = negligible

---

## Security Considerations

### Within Docker Compose (Same Network)

- ✅ No encryption needed (internal network)
- ✅ No authentication needed (trusted network)
- ✅ Just HTTP is fine

### Cloud Deployment

- ✅ Use HTTPS (encrypted)
- ✅ Use API key (authenticated)
- ✅ Set in `INCIDENT_INTELLIGENCE_API_KEY` env var

```yaml
environment:
  INCIDENT_INTELLIGENCE_API_URL: "https://incident-int.prod.example.com/api/logs/ingest"
  INCIDENT_INTELLIGENCE_API_KEY: ${INCIDENT_INTELLIGENCE_API_KEY}  # From secrets
```

---

## Summary

**Why it works:**

1. **HTTP, not direct DB access** - Services communicate over network
2. **Docker DNS** - Service names resolve automatically
3. **Same network** - Services can reach each other
4. **Non-blocking** - Timeout doesn't affect scan_benchmark_app
5. **Flexible** - Works in dev, Docker Compose, Kubernetes, cloud

**Configuration:**
```
INCIDENT_INTELLIGENCE_API_URL = http://service-name:port/api/logs/ingest
                                  ↑
                                  Service hostname (Docker DNS resolves it)
```

**Result:**
- scan_benchmark_app and incident_intelligence are independent services
- Can run in same or different environments
- Communicate via HTTP (simple, standard, reliable)
- Works in Docker, Kubernetes, cloud, local dev
