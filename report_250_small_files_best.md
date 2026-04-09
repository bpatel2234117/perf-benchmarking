# 250.x best-of (250.1 + 250.9) vs 249.14 — Small Files

## What this report is

- **Candidate builds (250.1 | 250.9):** two (or more) runs on the **250.x** line. For each scenario we first pick the **best single run per build** (same rule as the dashboard **Trend Analysis** chart: shortest `total_scan_hours`, else highest `throughput_cluster_gb_hr`). Then we take the **best of those** so you get **one row per scenario** representing the strongest 250.x result.
- **Baseline (249.14):** same scenario selection — compared on the same axes as the trend charts ([open Trend Analysis](http://10.2.20.89:8501/?page=Small+Files+Trend+Analysis)).

This is **not** “250.1 vs 250.9” as the headline story — it is **best-of-250.x** vs **249.14**.

## Best of 250.x (merged candidates) vs baseline

| Data source | Scan type | ACL | FF | File set | Best files/hr | Best GB/hr | Hours | From build | 249.14 files/hr | 249.14 GB/hr | hrs | Δ files/hr vs baseline | Δ GB/hr vs baseline | scan_id (best) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S3 | Full Scan | 0 | 0 | S3 Small Files | 310,961 | 16.7 | 3.216 | **250.1** | 257,395 | 13.84 | 3.88 | +20.8% | +20.7% | `69c630248e7b6dd2a17bcb59` |
| SMB | Full Scan | 0 | 0 | SMB Small Files | 211,466 | 12.27 | 4.729 | **250.9** |  |  |  | — | — | `69cbbaf68e7b6dd2a1b5f7ed` |
| SMB | Full Scan | 1 | 0 | SMB Small Files | 285,284 | 16.55 | 3.505 | **250.9** |  |  |  | — | — | `69cc78d68e7b6dd2a1d96ddc` |

## Alignment with Trend Analysis charts

On **[Trend Analysis](http://10.2.20.89:8501/?page=Small+Files+Trend+Analysis)**, each point per build is the same “best run” rule within the filtered file set. Use the chart to see **249.14 → 250.1 | 250.9** as a time/build series; this table states the **numeric best-of-250.x** and how it moves versus **249.14** on the same scenarios.

### Paired scenarios (best-of-250.x vs baseline)

- **Files/hr:** median Δ vs 249.14: **+20.8%** (1 scenarios).
- **GB/hr:** median Δ vs 249.14: **+20.7%** (1 scenarios).

## Notes

- Data source: `benchmarks` + `builds` + `configurations` + overlays from `scan_insights` (same as dashboard).
- Empty baseline or empty merged cells mean that scenario had no row for that build filter.
