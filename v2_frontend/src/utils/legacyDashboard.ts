/**
 * Absolute URLs into Streamlit `dashboard.py` (same query-param routing as legacy Scan Status links).
 *
 * Base URL (first match wins):
 * - VITE_LEGACY_DASHBOARD_URL in Vite env (.env.local)
 * - LEGACY_DASHBOARD_URL from FastAPI GET /api/ui-config (repo .env), same shape as
 *   http://10.2.20.89:8501/?page=Add+New+Benchmark&scan_id=...
 *
 * Streamlit reads page + scan_id via st.query_params on Add New Benchmark.
 */
export function resolveLegacyDashboardBase(serverBase?: string | null): string {
  const vite = (import.meta.env.VITE_LEGACY_DASHBOARD_URL as string | undefined)?.trim();
  const srv = (serverBase ?? '').trim();
  const raw = vite || srv;
  return raw.replace(/\/$/, '');
}

export function legacyDashboardUrl(
  params: Record<string, string | undefined>,
  serverBase?: string | null,
): string {
  const base = resolveLegacyDashboardBase(serverBase);
  if (!base) return '';
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v != null && String(v).trim() !== '') q.set(k, String(v));
  });
  return `${base}/?${q.toString()}`;
}

/** Streamlit **Add New Benchmark** with optional scan_id prefill (page name matches sidebar). */
export function legacyOpenBenchmarkUrl(scanId: string, serverBase?: string | null): string {
  return legacyDashboardUrl({ page: 'Add New Benchmark', scan_id: scanId }, serverBase);
}

/** Streamlit **Scan Progress** detail (`dashboard.page_scan_progress`). */
export function legacyScanProgressUrl(scanId: string, serverBase?: string | null): string {
  return legacyDashboardUrl({ page: 'Scan Progress', scan_id: scanId }, serverBase);
}
