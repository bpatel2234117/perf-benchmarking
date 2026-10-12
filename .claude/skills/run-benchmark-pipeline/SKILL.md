---
name: run-benchmark-pipeline
description: >-
  End-to-end sprint benchmark pipeline for a BigID build. Downloads the build
  (awscp), deploys 3 playground perf envs (deploy_perf), gets their URLs (gu),
  registers them into the benchmark app, creates + auto-starts the full+OLS
  workflow scan matrix, monitors it (via the benchmark-scan-monitor subagent),
  and generates the build report. Use when the user says things like "run the
  benchmark pipeline for 257.3", "kick off the sprint scans for build X", or
  "deploy perf envs and start benchmarks".
---

# Run Benchmark Pipeline

Drive the full sprint benchmark flow for one BigID build, end to end. **Execute
what is safe to execute; stop at the gates (⛔) and wait for the user.**

The build version is the single argument, e.g. `257.3`. If not given, ask for it.

---

## Ground rules (read first)

- **Two hosts are involved.** The build/deploy/URL steps (Phases 1–4) run
  **locally on this Mac** via `kubectl`/`helm`/`aws` against the playground
  cluster. The benchmark app (register/create-run/report, Phases 5–6) runs on
  **EC2** and is reached over HTTP at `SPB_API_BASE`.
- **The aliases are zsh functions in `~/.zprofile`, not on PATH.** Every Bash
  call that uses `awscp` / `deploy_perf` / `gu` MUST start with
  `source ~/.zprofile &&`. Shell cwd does **not** persist between Bash calls, so
  always `cd` explicitly.
- **Never guess credentials or the API base.** Read them from config (below). If
  a required value is missing, ⛔ stop and ask — do not invent one.
- **Confirm before destructive/outward actions**: connecting/deploying to shared
  cluster namespaces and starting scans. Registration and scan-start are gated.
- Fixed namespaces this run uses: `e2e-perf`, `e2e-perf2`, `e2e-perf3`.

### Config resolution

Resolve these once at the start and echo them back (mask secrets):

| Value | Source | Notes |
|---|---|---|
| `BUILD` | skill argument | e.g. `257.3`; must match `^[0-9]+\.[0-9]+$` |
| `SPB_API_BASE` | `.env` key `SPB_API_BASE` | EC2 app API, e.g. `http://<EC2_HOST>:5174/api`. If absent, ⛔ ask. |
| `SPB_API_USER` / `SPB_API_PASS` | `.env` keys | Benchmark-app **Admin** login for `/api/token`. If absent, ⛔ ask. |
| BigID env creds | `.env` keys `BIGID_<NS>_USERNAME` / `_PASSWORD` | `<NS>` = namespace uppercased, `-`→`_` (e.g. `e2e-perf`→`BIGID_E2E_PERF_USERNAME`). |

Read `.env` from the repo root. To get a value without leaking others, grep a
single key, e.g. `grep -E '^SPB_API_BASE=' .env`. **Do not print secret values**
— confirm presence only.

> ⚠️ Known gap: `.env` currently has creds for `e2e-perf` and `e2e-perf2` but
> **not `e2e-perf3`** (`BIGID_E2E_PERF3_USERNAME` / `BIGID_E2E_PERF3_PASSWORD`).
> If they are still missing, ⛔ ask the user to add them before Phase 5.

---

## Phase 0 — Preflight (⛔ gate)

1. Validate `BUILD` format.
2. Confirm config resolves (table above). Report any missing value and stop.
3. Check the kube context points at the playground cluster:
   ```bash
   kubectl config current-context
   ```
4. ⛔ **Ask the user to confirm the VPN is connected and the context is correct**
   before proceeding. The VPN cannot be automated — this is a hard gate.

---

## Phase 1 — Download the build

Runs `awscp` (S3 download + extract into `~/BigID/App/core/<BUILD>/compose`).
This can take several minutes — use a long timeout or run in the background.

```bash
source ~/.zprofile && awscp <BUILD>
```

Verify success: the compose dir with the chart must exist.

```bash
ls -d ~/BigID/App/core/<BUILD>/compose/helm.v3 && echo OK
```

If the build isn't available on S3, `awscp` prints `<BUILD> not avaiable` — ⛔
stop and report; do not continue to deploy.

---

## Phase 2 — Deploy the 3 perf envs

⛔ Confirm with the user before deploying (helm upgrade on shared cluster
namespaces). Then deploy each namespace. `deploy_perf` must run **from the
compose dir** (it uses `./helm.v3`) and ends by calling `gu` twice.

Run for each of `e2e-perf`, `e2e-perf2`, `e2e-perf3`:

```bash
source ~/.zprofile && cd ~/BigID/App/core/<BUILD>/compose && deploy_perf <NS> <BUILD>
```

`helm upgrade` returns once the release is *accepted*, not once pods are ready —
that's Phase 3. Capture any URL that the trailing `gu` prints, but treat Phase 4
as the source of truth for URLs.

---

## Phase 3 — Wait for pods Ready

Poll each namespace until core pods are Running/Ready (fresh envs typically take
~10–20 min). Do not block a single Bash call for the whole time — poll on an
interval.

```bash
kubectl -n <NS> get pods
```

Consider an env ready when the `bigid-ui` pod (and mongodb/rabbitmq/redis) are
Running and Ready. If pods are stuck (CrashLoopBackOff, ImagePullBackOff,
Pending for long), ⛔ stop and surface `kubectl -n <NS> describe pod <pod>` /
logs to the user.

> This wait is a good candidate to delegate to the `benchmark-scan-monitor`
> subagent if you want to free up the main thread — but Phases 1–2 must finish
> first.

---

## Phase 4 — Get the env URLs

`gu <NS>` patches `bigid-ui` to a LoadBalancer and prints the ELB **hostname**
then IP. Capture the hostname line for each namespace — this is the env URL.

```bash
source ~/.zprofile && gu <NS>
```

Build `base_url` per env from the hostname. **Scheme:** default to
`https://<hostname>`. The first time you run this for a fresh cluster, ⛔ confirm
the scheme/port with the user (BigID UI is normally HTTPS on 443 via the LB); if
they say otherwise, use what they specify. The `POST /api/environments` endpoint
strips trailing slashes, so no need to normalize.

If `gu` returns an empty hostname, the ELB isn't provisioned yet — wait and
re-run.

---

## Phase 5 — Register envs + create/start the workflow run (⛔ gate)

All calls go to `SPB_API_BASE` on EC2.

### 5a. Get an app token

```bash
curl -s -X POST "$SPB_API_BASE/api/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "username=$SPB_API_USER" \
  --data-urlencode "password=$SPB_API_PASS"
```

Extract `access_token` from the JSON. Use it as `Authorization: Bearer <token>`
for the rest of Phase 5–6. If login fails, ⛔ stop.

### 5b. Register each env

For each of the 3 namespaces, register with its per-env BigID creds. Use the
namespace as `env_name`. `include_in_workflow_runs` defaults true, which is what
puts it into the workflow pool.

```bash
curl -s -X POST "$SPB_API_BASE/api/environments" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "env_name": "<NS>",
    "base_url": "<BASE_URL_FROM_GU>",
    "is_active": true,
    "include_in_workflow_runs": true,
    "username": "<BIGID_<NS>_USERNAME>",
    "password": "<BIGID_<NS>_PASSWORD>"
  }'
```

- A duplicate `env_name` returns **409** — if the env already exists from a prior
  sprint, `PUT /api/environments/{id}` to update `base_url` (and creds) instead.
  `GET /api/environments` first to find existing ids.
- The response includes `license_sync_ok` / `license_sync_message`. A license
  warning does **not** fail registration, but report it.
- Optionally mirror the UI's post-create sync (populates version + classifiers):
  `GET /api/environments/{id}/build-info` then
  `POST /api/environments/{id}/sync-classifiers`.

Confirm all 3 envs are registered and active before starting the run.

### 5c. Create + auto-start the workflow run

⛔ **Confirm with the user before starting scans.** One call creates the default
full+OLS scan matrix (`workflow_defaults.py`) and starts it. Omitting
`env1_id`/`env2_id` means "all available envs": the run uses every active
`include_in_workflow_runs` env that no other run has selected, so all 3 envs
scan in parallel. Set `env1_id`/`env2_id` only to reserve specific envs — the
run is then confined to them, and they become off-limits to other runs (that is
how two workflows execute side by side on different envs).

```bash
curl -s -X POST "$SPB_API_BASE/api/workflow/runs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Workflow <BUILD>", "auto_start": true}'
```

Response: `{"id": <run_id>, "status": "running"}`. Record `run_id`. The
`spb_workflow` daemon (30s tick) now drives DS→profile→scan-trigger and
`spb_tracker` (60s) records progress; terminal scans auto-save benchmark rows.

---

## Phase 6 — Monitor, then generate the report

### 6a. Monitor (delegate)

Hand off live monitoring to the **`benchmark-scan-monitor`** subagent. Give it:
`SPB_API_BASE`, the bearer token (or the creds to mint one), `run_id`, and
`BUILD`. It polls run/step/scan status, reports progress, and flags stuck/failed
steps back to you. Scans span the sprint — the user monitors over days.

### 6b. Generate the report (⛔ end-of-sprint gate)

Only when the user says the sprint's scans are done. Confirm the current and
previous build labels with the user. All params are **query string**; report is
Admin-only. There is **no email** — the endpoint produces HTML into `reports/`.

```bash
curl -s -X POST "$SPB_API_BASE/api/benchmark/generate-report?current_build=<BUILD>&previous_build=<PREV>&scanner_performance=true&scanner_scope=last&structured=true" \
  -H "Authorization: Bearer $TOKEN"
```

`scanner_scope` controls the scanner-performance chart window: `last` (default)
scopes the per-env charts to the **most recent workflow run for the build** (all
its environments — the sprint you just ran); `all` uses every benchmark scan of
the build. Omit the param to get `last`.

Then list/fetch the produced file:
`GET /api/reports` (find the newest) →
`GET /api/reports/file/{filename}`. Give the user the filename and the served
URL. If they want it sent (email/Slack), that's out of scope today — tell them
sending isn't implemented and offer to add it as a follow-up.

---

## Failure handling & idempotency

- **Re-running is safe up to a point:** `awscp`/`deploy_perf` are idempotent
  (helm upgrade --install); env registration 409s on duplicate → switch to PUT.
- **Env busy locks:** if a run won't start because envs are locked from a prior
  run, `POST /api/environments/actions/clear-busy-locks` (Admin) clears stale
  locks.
- **Stuck steps:** `POST /api/workflow/runs/{run_id}/reconcile-steps` forces a
  terminal-state reconcile; `POST /api/workflow/runs/{run_id}/control {"action":"stop"}`
  stops a run.
- Always report exactly what ran, what succeeded, and what was skipped/gated.
