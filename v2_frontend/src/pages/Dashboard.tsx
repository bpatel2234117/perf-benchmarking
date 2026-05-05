import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, TrendingUp, Layers } from 'lucide-react';
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { API_BASE } from '../apiConfig';

interface DashboardProps {
  token: string;
}

export interface TrendRow {
  id: number;
  build_version: string;
  run_date: string | null;
  scenario_type: string | null;
  scan_type: string | null;
  data_source_type: string | null;
  file_set_type: string | null;
  is_feature_flags: boolean;
  data_segment: 'structured' | 'unstructured';
  total_scan_hours: number | null;
  throughput_cluster_gb_hr: number | null;
  throughput_scanner_gb_hr: number | null;
  throughput_thread_gb_hr: number | null;
  throughput_cluster_files_hr: number | null;
  throughput_scanner_files_hr: number | null;
  throughput_thread_files_hr: number | null;
  total_catalog_items: number | null;
}

function betterRow(a: TrendRow, b: TrendRow): TrendRow {
  const ta = a.total_scan_hours;
  const tb = b.total_scan_hours;
  if (ta != null && tb != null) return ta <= tb ? a : b;
  if (ta != null) return a;
  if (tb != null) return b;
  const ga = a.throughput_cluster_gb_hr ?? 0;
  const gb = b.throughput_cluster_gb_hr ?? 0;
  return ga >= gb ? a : b;
}

/** One best row per build (same rule as Streamlit trend: min hours, else max GB/hr). */
function pickBestPerBuild(rows: TrendRow[]): TrendRow[] {
  const m = new Map<string, TrendRow>();
  for (const r of rows) {
    const k = String(r.build_version || 'unknown');
    const cur = m.get(k);
    if (!cur) m.set(k, r);
    else m.set(k, betterRow(cur, r));
  }
  return Array.from(m.values()).sort((a, b) =>
    String(a.build_version).localeCompare(String(b.build_version), undefined, { numeric: true, sensitivity: 'base' })
  );
}

/** After ascending sort by build label, keep only the newest N points (slice from end). */
function takeLastBuilds(plot: TrendRow[], lastN: number | 'all'): TrendRow[] {
  if (!plot.length) return plot;
  if (lastN === 'all') return plot;
  if (typeof lastN !== 'number' || lastN < 1) return plot;
  return plot.length <= lastN ? plot : plot.slice(-lastN);
}

function toChartPoints(rows: TrendRow[]) {
  return rows.map((r) => ({
    build: r.build_version,
    cluster_gb: r.throughput_cluster_gb_hr ?? null,
    scanner_gb: r.throughput_scanner_gb_hr ?? null,
    thread_gb: r.throughput_thread_gb_hr ?? null,
    cluster_files: r.throughput_cluster_files_hr ?? null,
    scanner_files: r.throughput_scanner_files_hr ?? null,
    thread_files: r.throughput_thread_files_hr ?? null,
    catalog: r.total_catalog_items != null ? Number(r.total_catalog_items) : null,
  }));
}

export const LAST_BUILD_OPTIONS: { label: string; value: number | 'all' }[] = [
  { label: 'Last 5 builds', value: 5 },
  { label: 'Last 10 builds', value: 10 },
  { label: 'Last 15 builds', value: 15 },
  { label: 'Last 20 builds', value: 20 },
  { label: 'Last 30 builds', value: 30 },
  { label: 'All builds', value: 'all' },
];

const TrendSection: React.FC<{
  title: string;
  subtitle: string;
  rows: TrendRow[];
  loading: boolean;
  segment: 'structured' | 'unstructured';
  lastBuildWindow: number | 'all';
}> = ({ title, subtitle, rows, loading, segment, lastBuildWindow }) => {
  const plot = useMemo(() => pickBestPerBuild(rows), [rows]);
  const displayPlot = useMemo(() => takeLastBuilds(plot, lastBuildWindow), [plot, lastBuildWindow]);
  const chart = useMemo(() => toChartPoints(displayPlot), [displayPlot]);

  if (loading) {
    return (
      <section className="trend-section glass-panel" style={{ padding: '2rem', textAlign: 'center' }}>
        <Loader2 className="lucide-spin" size={28} />
      </section>
    );
  }

  if (!plot.length) {
    const structuredHint =
      segment === 'structured'
        ? ' Structured runs include relational DB connectors (e.g. MSSQL, PostgreSQL, MySQL), plus Structured Discovery / FF (`is_feature_flags`), `feature_flags` or scenario / file-set names mentioning Structured, or structured-oriented file-set types — when not already caught by relational type. S3 and SMB connectors are always under Unstructured data.'
        : '';
    return (
      <section className="trend-section glass-panel" style={{ padding: '2rem' }}>
        <h3 className="trend-section-title">{title}</h3>
        <p className="trend-section-sub">{subtitle}</p>
        <p style={{ color: 'var(--text-secondary)', marginTop: '1rem', maxWidth: '52rem', lineHeight: 1.5 }}>
          No benchmark rows for this segment with the current Data source / Scan type filters.{structuredHint}
        </p>
      </section>
    );
  }

  const windowHint =
    lastBuildWindow === 'all' ? `all ${plot.length}` : `${displayPlot.length} newest of ${plot.length}`;

  return (
    <section className="trend-section glass-panel" style={{ padding: '1.25rem' }}>
      <div className="trend-section-header">
        <Layers size={22} style={{ color: 'var(--primary-color)' }} />
        <div>
          <h3 className="trend-section-title">{title}</h3>
          <p className="trend-section-sub">{subtitle}</p>
        </div>
      </div>
      <p className="trend-caption">
        Builds plotted: <strong>{windowHint}</strong> (best run per build: shortest duration, else highest cluster GB/hr)
      </p>

      <div className="trend-chart-grid-3">
        <div className="trend-chart-cell">
          <h4 className="trend-chart-heading">Data Throughput Trend (GB/hr)</h4>
          <div className="chart-container chart-trend">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="cluster_gb" name="Cluster" stroke="#7c3aed" strokeWidth={3} dot={{ r: 4 }} connectNulls />
                <Line type="monotone" dataKey="scanner_gb" name="Per Scanner" stroke="#6366f1" strokeWidth={2} dot={{ r: 3 }} connectNulls />
                <Line type="monotone" dataKey="thread_gb" name="Per Thread" stroke="#c4b5fd" strokeWidth={2} dot={{ r: 3 }} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="trend-chart-cell">
          <h4 className="trend-chart-heading">File Throughput Trend (files/hr)</h4>
          <div className="chart-container chart-trend">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="cluster_files" name="Cluster" stroke="#059669" strokeWidth={3} dot={{ r: 4 }} connectNulls />
                <Line type="monotone" dataKey="scanner_files" name="Per Scanner" stroke="#34d399" strokeWidth={2} dot={{ r: 3 }} connectNulls />
                <Line type="monotone" dataKey="thread_files" name="Per Thread" stroke="#a7f3d0" strokeWidth={2} dot={{ r: 3 }} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="trend-chart-cell">
          <h4 className="trend-chart-heading">Catalog scale (total_catalog_items)</h4>
          <div className="chart-container chart-trend">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="catalog" name="Total catalog items" stroke="#0284c7" strokeWidth={3} dot={{ r: 4 }} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <h4 className="trend-bar-heading">Throughput Comparison (Grouped)</h4>
      <div className="trend-chart-grid-2">
        <div className="chart-container chart-bar">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
              <XAxis dataKey="build" tick={{ fontSize: 10 }} stroke="var(--text-secondary)" angle={-35} textAnchor="end" height={60} />
              <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
              <Tooltip />
              <Legend />
              <Bar dataKey="cluster_gb" name="Cluster GB/hr" fill="#7c3aed" />
              <Bar dataKey="scanner_gb" name="Scanner GB/hr" fill="#818cf8" />
              <Bar dataKey="thread_gb" name="Thread GB/hr" fill="#c4b5fd" />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="chart-container chart-bar">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
              <XAxis dataKey="build" tick={{ fontSize: 10 }} stroke="var(--text-secondary)" angle={-35} textAnchor="end" height={60} />
              <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
              <Tooltip />
              <Legend />
              <Bar dataKey="cluster_files" name="Cluster files/hr" fill="#059669" />
              <Bar dataKey="scanner_files" name="Scanner files/hr" fill="#34d399" />
              <Bar dataKey="thread_files" name="Thread files/hr" fill="#86efac" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <h4 className="trend-bar-heading">Summary</h4>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Build</th>
              <th>Scan type</th>
              <th>Data source</th>
              <th>Cluster GB/hr</th>
              <th>Cluster files/hr</th>
              <th>Scan hours</th>
            </tr>
          </thead>
          <tbody>
            {[...displayPlot].reverse().map((r) => (
              <tr key={`${title}-${r.id}`}>
                <td>{r.build_version}</td>
                <td>{r.scan_type ?? '—'}</td>
                <td>{r.data_source_type ?? '—'}</td>
                <td>{r.throughput_cluster_gb_hr != null ? r.throughput_cluster_gb_hr.toFixed(2) : '—'}</td>
                <td>{r.throughput_cluster_files_hr != null ? Math.round(r.throughput_cluster_files_hr).toLocaleString() : '—'}</td>
                <td>{r.total_scan_hours != null ? r.total_scan_hours.toFixed(2) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
};

const Dashboard: React.FC<DashboardProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const [filterDs, setFilterDs] = useState<string>('');
  const [filterScan, setFilterScan] = useState<string>('');
  const [lastBuildWindow, setLastBuildWindow] = useState<number | 'all'>(15);

  const { data: trendRows = [], isLoading: trendLoading, error: trendError } = useQuery({
    queryKey: ['trends-small-files', token],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/trends/small-files`, {
        ...authConfig,
        params: { max_builds: 250 },
      });
      return res.data as TrendRow[];
    },
    refetchInterval: 20000,
  });

  const filteredTrend = useMemo(() => {
    let t = trendRows;
    if (filterDs) t = t.filter((r) => r.data_source_type === filterDs);
    if (filterScan) t = t.filter((r) => r.scan_type === filterScan);
    return t;
  }, [trendRows, filterDs, filterScan]);

  const structuredRows = useMemo(() => filteredTrend.filter((r) => r.data_segment === 'structured'), [filteredTrend]);
  const unstructuredRows = useMemo(() => filteredTrend.filter((r) => r.data_segment === 'unstructured'), [filteredTrend]);

  const dsOptions = useMemo(() => {
    const u = [...new Set(trendRows.map((r) => r.data_source_type).filter(Boolean))] as string[];
    return ['', ...u.sort()];
  }, [trendRows]);
  const scanOptions = useMemo(() => {
    const u = [
      ...new Set(trendRows.filter((r) => !filterDs || r.data_source_type === filterDs).map((r) => r.scan_type).filter(Boolean)),
    ] as string[];
    return ['', ...u.sort()];
  }, [trendRows, filterDs]);

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      <section>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.5rem' }}>
          <TrendingUp size={28} style={{ color: 'var(--primary-color)' }} />
          <div>
            <h2 style={{ color: 'var(--text-primary)', fontSize: '1.35rem', fontWeight: 700 }}>Small Files Trend Analysis</h2>
          </div>
        </div>

        {trendError && (
          <div className="glass-panel" style={{ padding: '1rem', color: 'var(--danger-color)', marginBottom: '1rem' }}>
            Trend API failed (is the v2 API running?). {(trendError as Error).message}
          </div>
        )}

        <div className="trend-filters glass-panel" style={{ marginBottom: '1.25rem', padding: '1rem 1.25rem', display: 'flex', flexWrap: 'wrap', gap: '1.5rem', alignItems: 'end' }}>
          <div className="input-group" style={{ margin: 0, minWidth: 220 }}>
            <label htmlFor="trend-last-n">Charts: build window</label>
            <select
              id="trend-last-n"
              value={lastBuildWindow === 'all' ? 'all' : String(lastBuildWindow)}
              onChange={(e) => {
                const raw = e.target.value;
                setLastBuildWindow(raw === 'all' ? 'all' : parseInt(raw, 10));
              }}
              style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
            >
              {LAST_BUILD_OPTIONS.map((o) => (
                <option key={String(o.value)} value={o.value === 'all' ? 'all' : o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
            <label htmlFor="trend-ds">Data source</label>
            <select
              id="trend-ds"
              value={filterDs}
              onChange={(e) => {
                setFilterDs(e.target.value);
                setFilterScan('');
              }}
              style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
            >
              {dsOptions.map((ds) => (
                <option key={ds || 'all-ds'} value={ds}>
                  {ds === '' ? 'All data sources' : ds}
                </option>
              ))}
            </select>
          </div>
          <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
            <label htmlFor="trend-scan">Scan type</label>
            <select
              id="trend-scan"
              value={filterScan}
              onChange={(e) => setFilterScan(e.target.value)}
              style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
            >
              {scanOptions.map((st) => (
                <option key={st || 'all-scan'} value={st}>
                  {st === '' ? 'All scan types' : st}
                </option>
              ))}
            </select>
          </div>
        </div>

        <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', margin: '0 0 1rem', maxWidth: '52rem', lineHeight: 1.45 }}>
          Leave <strong>Data source</strong> and <strong>Scan type</strong> on All so unstructured and structured charts both receive rows. The same filters apply to both sections — picking e.g. only S3 hides structured relational runs. The backend loads Small Files benchmarks across the newest builds (not a flat row cap); each chart still shows at most one point per build version, so if a segment has fewer distinct builds than your window, you will see fewer dots.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
          <TrendSection
            segment="unstructured"
            lastBuildWindow={lastBuildWindow}
            title="Unstructured data"
            subtitle="S3 & SMB and other small-file runs that are not classified as relational DB or structured CSV / discovery (see below)."
            rows={unstructuredRows}
            loading={trendLoading}
          />
          <TrendSection
            segment="structured"
            lastBuildWindow={lastBuildWindow}
            title="Structured data"
            subtitle="Relational DB benchmarks (MSSQL, PostgreSQL, MySQL, Oracle, …), Structured Discovery / FF, and similar signals. S3 & SMB stay under Unstructured."
            rows={structuredRows}
            loading={trendLoading}
          />
        </div>
      </section>
    </div>
  );
};

export default Dashboard;
