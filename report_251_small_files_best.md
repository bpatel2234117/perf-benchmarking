# 250.x best-of (251.3 + 251.10 + 251.14) vs 250.9 — Small Files

## What this report is

- **Candidate builds (251.3 | 251.10 | 251.14):** two (or more) runs on the **250.x** line. For each scenario we first pick the **best single run per build** (same rule as the dashboard **Trend Analysis** chart: shortest `total_scan_hours`, else highest `throughput_cluster_gb_hr`). Then we take the **best of those** so you get **one row per scenario** representing the strongest 250.x result.
- **Baseline (250.9):** same scenario selection — compared on the same axes as the trend charts ([open Trend Analysis](http://10.2.20.89:8501/?page=Small+Files+Trend+Analysis)).

This is **not** “250.1 vs 250.9” as the headline story — it is **best-of-250.x** vs **250.9**.

## Best of 250.x (merged candidates) vs baseline

| Data source | Scan type | ACL | FF | File set | Best files/hr | Best GB/hr | Hours | From build | 250.9 files/hr | 250.9 GB/hr | hrs | Δ files/hr vs baseline | Δ GB/hr vs baseline | scan_id (best) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S3 | Full Scan | 0 | 0 | S3 Small Files | 244,184 | 13.11 | 4.095 | **251.14** |  |  |  | — | — | `69e75900ca4712c95775427b` |
| SMB | Full Scan | 0 | 0 | SMB Small Files | 115,468 | 6.711 | 8.642 | **251.14** | 211,466 | 12.27 | 4.729 | -45.4% | -45.3% | `69e7590d377799b88e1a5692` |
| SMB | Full Scan | 1 | 0 | SMB Small Files | 230,453 | 13.39 | 4.333 | **251.14** | 285,284 | 16.55 | 3.505 | -19.2% | -19.1% | `69e7d653377799b88e7fdd45` |

## Alignment with Trend Analysis charts

On **[Trend Analysis](http://10.2.20.89:8501/?page=Small+Files+Trend+Analysis)**, each point per build is the same “best run” rule within the filtered file set. Use the chart to see **250.9 → 251.3 | 251.10 | 251.14** as a time/build series; this table states the **numeric best-of-250.x** and how it moves versus **250.9** on the same scenarios.

### Paired scenarios (best-of-250.x vs baseline)

- **Files/hr:** median Δ vs 250.9: **-32.3%** (2 scenarios).
- **GB/hr:** median Δ vs 250.9: **-32.2%** (2 scenarios).

## Notes

- Data source: `benchmarks` + `builds` + `configurations` + overlays from `scan_insights` (same as dashboard).
- Empty baseline or empty merged cells mean that scenario had no row for that build filter.
