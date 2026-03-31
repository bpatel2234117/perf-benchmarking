# PCT
BigID Core Performance Comparision tool

## Scanner performance (Scan Progress)

Snapshots come from:

| API | Stored in | Notes |
|-----|-----------|--------|
| `GET /api/v1/scanner-groups` | `scanner_group_performance` | `runningScanners` + `scannersIDs` = running pod set; `avgScanSpeed` is **bytes/s** (dashboard charts use **KiB/s**); `maxThreads` vs `scannerThreads` = pool cap vs UI-style cap |
| `GET /api/v1/health-checks/services-status` | `scanner_individual_performance` | Per-scanner `status`, threads, load, **physical/free memory**, **`available_processors`**; **CPU %** matches BigID UI (`load × 100 ÷ processors`) unless a native cpu field exists. Scan Progress **Per-scanner** + **Summary** tabs chart **CPU %** and **memory %** per pod. |

**Env (optional)**

- `BIGID_SCANNER_CPU_DERIVE_FROM_LOAD` — default `1`: fill `cpu_pct` from load÷processors when API has no cpu field. Set `0` to disable.
- `BIGID_SCANNER_DEFAULT_PROCESSORS` — default `7`: when **displaying** old DB rows with `system_load_average` but **NULL** `available_processors`, charts derive CPU % using this divisor (matches many BigID scanner pods).
- `BIGID_SCANNER_CPU_JSON_KEY` — if your build sends CPU under a custom JSON key, set it here.
- `BIGID_SCANNER_GROUPS_API_PATH` / `BIGID_SERVICES_STATUS_API_PATH` — override paths if needed.

Updates: **tracker** (per cycle) or **Scan Progress → Sync now (manual)**.

**Scan Progress charts (default):** only pods in BigID’s **running** set (`scannersIDs` from `scanner-groups`), or — if that JSON isn’t in the DB yet — scanners whose **latest** row has **running_threads > 0** (non-failed). Enable **“Show all pods from services-status”** to see every historical UUID (noisy legends).

**Scanner Infrastructure** menu page uses the **same** `scanner_individual_performance` / `scanner_group_performance` tables (not `scanner_pod_metrics`, unless you use the **Legacy** section at the bottom). That’s why you see **CPU %** (often ≫100%) instead of mislabeled “load as CPU”.
