import React, { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, TrendingUp, Layers, ChevronRight, ChevronDown } from 'lucide-react';
import PageHeader from '../components/PageHeader';
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
import PaginationBar from '../components/Pagination';

interface TrendApiPayload {
  rows: TrendRow[];
  distinct_builds_total: number;
  build_window_cap: number;
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
  is_acl?: boolean;
  data_segment: 'structured' | 'unstructured';
  total_scan_hours: number | null;
  throughput_cluster_gb_hr: number | null;
  throughput_scanner_gb_hr: number | null;
  throughput_thread_gb_hr: number | null;
  throughput_cluster_files_hr: number | null;
  throughput_scanner_files_hr: number | null;
  throughput_thread_files_hr: number | null;
  total_catalog_items: number | null;
  is_release: boolean;
}

interface DashboardProps {
  token: string;
}

function majorVersion(v: string): string {
  return String(v || 'unknown').split('.')[0].trim();
}

/** Structured Discovery (FF) is a real scenario — EXCEPT for CSV, which is always FF in
 *  BigID (inherent, not a distinct scenario), so CSV never counts as Structured Discovery. */
function isStructuredDiscovery(r: TrendRow): boolean {
  return !!r.is_feature_flags && !(r.file_set_type || '').toLowerCase().includes('csv');
}

/** Scenario signature within a (data source + file set): ACL, OLS, Structured Discovery. */
function scenarioLabel(r: TrendRow): string {
  const ols = (r.scan_type || '').toLowerCase().includes('ols');
  const parts: string[] = [];
  if (r.is_acl) parts.push('ACL');
  if (ols) parts.push('OLS');
  if (isStructuredDiscovery(r)) parts.push('Struct');
  return parts.length ? parts.join('+') : 'Full';
}

type AvgRow = TrendRow & { __combo: string };

/** Distinct line colors for multi-scenario comparison mode. */
const SERIES_COLORS = ['#7c3aed', '#2563eb', '#059669', '#db2777', '#d97706', '#0891b2', '#9333ea', '#dc2626'];
const MAX_SERIES = SERIES_COLORS.length;

/** One AVERAGED row per (major build × full scenario). A major build (e.g. "254") covers all
 *  its minor builds; metrics are the mean across ALL runs in that major build FOR THAT
 *  SCENARIO. The scenario (`__combo`) is the full file-set + ACL/OLS/Struct signature, so
 *  different data sources / sizes / modes are never blended into one mean — each becomes its
 *  own line in comparison mode. build_version stays the clean major (e.g. "254"). */
function averageByMajorBuild(rows: TrendRow[]): AvgRow[] {
  const groups = new Map<string, { major: string; combo: string; rs: TrendRow[] }>();
  for (const r of rows) {
    const major = majorVersion(String(r.build_version || 'unknown'));
    const combo = `${r.file_set_type || '—'} · ${scenarioLabel(r)}`;
    const k = `${major}||${combo}`;
    const g = groups.get(k);
    if (g) g.rs.push(r);
    else groups.set(k, { major, combo, rs: [r] });
  }
  const mean = (rs: TrendRow[], key: keyof TrendRow): number | null => {
    const vals = rs.map((r) => r[key] as number | null).filter((v): v is number => v != null && Number.isFinite(v));
    if (!vals.length) return null;
    return vals.reduce((a, b) => a + b, 0) / vals.length;
  };
  return [...groups.values()]
    .map(({ major, combo, rs }) => ({
      ...rs[0],
      build_version: major,
      __combo: combo,
      throughput_cluster_gb_hr: mean(rs, 'throughput_cluster_gb_hr'),
      throughput_scanner_gb_hr: mean(rs, 'throughput_scanner_gb_hr'),
      throughput_thread_gb_hr: mean(rs, 'throughput_thread_gb_hr'),
      throughput_cluster_files_hr: mean(rs, 'throughput_cluster_files_hr'),
      throughput_scanner_files_hr: mean(rs, 'throughput_scanner_files_hr'),
      throughput_thread_files_hr: mean(rs, 'throughput_thread_files_hr'),
      total_scan_hours: mean(rs, 'total_scan_hours'),
      total_catalog_items: mean(rs, 'total_catalog_items'),
    }))
    .sort((a, b) =>
      String(a.build_version).localeCompare(String(b.build_version), undefined, { numeric: true, sensitivity: 'base' }) ||
      a.__combo.localeCompare(b.__combo)
    );
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
  const [sectionDs, setSectionDs] = useState<string[]>([]);
  const [sectionFileSize, setSectionFileSize] = useState<string[]>([]);
  const [sectionScan, setSectionScan] = useState<string[]>([]);
  const [sectionAcl, setSectionAcl] = useState<boolean>(false);  // ACL is a scenario, not "all"; default Off
  const [sectionStruct, setSectionStruct] = useState<boolean>(false);  // Structured Discovery; default Off
  const [showSummary, setShowSummary] = useState(false);

  const sectionDsOptions = useMemo(() => {
    const values = [...new Set(rows.map((r) => r.data_source_type).filter(Boolean))] as string[];
    return values.sort();
  }, [rows]);

  const defaultDs = useMemo(
    () => sectionDsOptions.find((ds) => ds.trim().toUpperCase() === 'S3') || sectionDsOptions[0] || '',
    [sectionDsOptions]
  );

  const sectionFileSizeOptions = useMemo(() => {
    const activeDs = sectionDs.length > 0 ? sectionDs : [defaultDs];
    const values = [
      ...new Set(
        rows
          .filter((r) => activeDs.length === 0 || activeDs.includes(r.data_source_type || ''))
          .map((r) => r.file_set_type)
          .filter(Boolean)
      ),
    ] as string[];
    return values.sort();
  }, [rows, sectionDs, defaultDs]);

  const defaultFileSize = useMemo(
    () => sectionFileSizeOptions.find((fs) => fs.toLowerCase().includes('small')) || sectionFileSizeOptions[0] || '',
    [sectionFileSizeOptions]
  );

  const sectionScanOptions = useMemo(() => {
    const activeDs = sectionDs.length > 0 ? sectionDs : [defaultDs];
    const activeFs = sectionFileSize.length > 0 ? sectionFileSize : [defaultFileSize];
    const values = [
      ...new Set(
        rows
          .filter((r) =>
            (activeDs.length === 0 || activeDs.includes(r.data_source_type || '')) &&
            (activeFs.length === 0 || activeFs.includes(r.file_set_type || ''))
          )
          .map((r) => r.scan_type)
          .filter(Boolean)
      ),
    ] as string[];
    return values.sort();
  }, [rows, sectionDs, sectionFileSize, defaultDs, defaultFileSize]);

  const defaultScan = useMemo(() =>
    sectionScanOptions.find((st) => st.toLowerCase() === 'full scan') || sectionScanOptions[0] || '',
    [sectionScanOptions]
  );

  const sectionRows = useMemo(() => {
    let out = rows;

    // Apply Data Source filter (multi-select)
    const activeDs = sectionDs.length > 0 ? sectionDs : [defaultDs];
    if (activeDs.length > 0) {
      out = out.filter((r) => activeDs.includes(r.data_source_type || ''));
    }

    // Apply File Size filter (multi-select)
    const activeFs = sectionFileSize.length > 0 ? sectionFileSize : [defaultFileSize];
    if (activeFs.length > 0) {
      out = out.filter((r) => activeFs.includes(r.file_set_type || ''));
    }

    // Apply Scan Type filter (multi-select)
    const activeScan = sectionScan.length > 0 ? sectionScan : [defaultScan];
    if (activeScan.length > 0) {
      out = out.filter((r) => activeScan.includes(r.scan_type || ''));
    }

    // Apply ACL + Structured Discovery filters (each a scenario dimension; CSV is never
    // treated as Structured Discovery — its FF flag is inherent).
    out = out.filter((r) => (r.is_acl ?? false) === sectionAcl);
    out = out.filter((r) => isStructuredDiscovery(r) === sectionStruct);

    return out;
  }, [rows, sectionDs, sectionFileSize, sectionScan, sectionAcl, sectionStruct, defaultDs, defaultFileSize, defaultScan]);

  // Auto-select defaults when options load
  useEffect(() => {
    if (sectionDs.length === 0 && defaultDs) {
      setSectionDs([defaultDs]);
    }
  }, [defaultDs, sectionDs]);

  useEffect(() => {
    if (sectionFileSize.length === 0 && defaultFileSize) {
      setSectionFileSize([defaultFileSize]);
    }
  }, [defaultFileSize, sectionFileSize]);

  useEffect(() => {
    if (sectionScan.length === 0 && defaultScan) {
      setSectionScan([defaultScan]);
    }
  }, [defaultScan, sectionScan]);

  const plot = useMemo(() => averageByMajorBuild(sectionRows), [sectionRows]);
  // Window by distinct MAJOR build (not by row count — there can be many scenario rows per build).
  const majorsSorted = useMemo(
    () => [...new Set(plot.map((r) => r.build_version))].sort((a, b) => String(a).localeCompare(String(b), undefined, { numeric: true })),
    [plot]
  );
  const windowMajors = useMemo(
    () => (typeof lastBuildWindow === 'number' && lastBuildWindow > 0 ? majorsSorted.slice(-lastBuildWindow) : majorsSorted),
    [majorsSorted, lastBuildWindow]
  );
  const windowSet = useMemo(() => new Set(windowMajors), [windowMajors]);
  const displayPlot = useMemo(() => plot.filter((r) => windowSet.has(r.build_version)), [plot, windowSet]);
  const combos = useMemo(() => [...new Set(displayPlot.map((r) => r.__combo))].sort(), [displayPlot]);
  const shownCombos = useMemo(() => combos.slice(0, MAX_SERIES), [combos]);
  const multi = combos.length > 1;
  // Single-scenario: keep the detailed cluster/scanner/thread view.
  const chart = useMemo(() => toChartPoints(displayPlot), [displayPlot]);
  // Multi-scenario comparison: one line per scenario, on the headline (cluster) metric.
  const buildSeries = (key: 'throughput_cluster_gb_hr' | 'throughput_cluster_files_hr') =>
    windowMajors.map((b) => {
      const o: Record<string, number | string | null> = { build: b };
      for (const r of displayPlot) if (r.build_version === b && shownCombos.includes(r.__combo)) o[r.__combo] = r[key] ?? null;
      return o;
    });
  const comboGb = useMemo(() => buildSeries('throughput_cluster_gb_hr'), [windowMajors, displayPlot, shownCombos]);
  const comboFiles = useMemo(() => buildSeries('throughput_cluster_files_hr'), [windowMajors, displayPlot, shownCombos]);

  const SUMMARY_PAGE_SIZE = 25;
  const [summaryPage, setSummaryPage] = useState(0);
  const reversedSummary = useMemo(() => [...displayPlot].reverse(), [displayPlot]);
  useEffect(() => {
    setSummaryPage(0);
  }, [displayPlot]);
  const summarySlice = useMemo(() => {
    const start = summaryPage * SUMMARY_PAGE_SIZE;
    return reversedSummary.slice(start, start + SUMMARY_PAGE_SIZE);
  }, [reversedSummary, summaryPage]);

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
    lastBuildWindow === 'all' ? `all ${majorsSorted.length}` : `${windowMajors.length} newest of ${majorsSorted.length}`;

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
        Builds plotted: <strong>{windowHint}</strong> (one point per major build version — averaged across all runs in that build)
      </p>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '1rem',
          alignItems: 'end',
          margin: '0.75rem 0 1rem',
        }}
      >
        <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
          <label htmlFor={`${segment}-trend-ds`}>Data Source (multi-select)</label>
          <select
            id={`${segment}-trend-ds`}
            multiple
            value={sectionDs}
            onChange={(e) => {
              const selected = Array.from(e.target.selectedOptions, (opt) => opt.value);
              setSectionDs(selected);
              // File sets are data-source-prefixed (e.g. "S3 Small Files"), so a selection
              // carried over from another source would match no rows. Reset dependent
              // filters to their defaults for the newly chosen source.
              setSectionFileSize([]);
              setSectionScan([]);
            }}
            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%', minHeight: '80px' }}
          >
            {sectionDsOptions.map((ds) => (
              <option key={`${segment}-ds-${ds}`} value={ds}>
                {ds}
              </option>
            ))}
          </select>
        </div>
        <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
          <label htmlFor={`${segment}-trend-fs`}>File Size (multi-select)</label>
          <select
            id={`${segment}-trend-fs`}
            multiple
            value={sectionFileSize}
            onChange={(e) => {
              const selected = Array.from(e.target.selectedOptions, (opt) => opt.value);
              setSectionFileSize(selected);
              setSectionScan([]);  // reset scan-type selection to valid defaults for this file set
            }}
            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%', minHeight: '80px' }}
          >
            {sectionFileSizeOptions.map((fs) => (
              <option key={`${segment}-fs-${fs}`} value={fs}>
                {fs}
              </option>
            ))}
          </select>
        </div>
        <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
          <label htmlFor={`${segment}-trend-scan`}>Scan Type (multi-select)</label>
          <select
            id={`${segment}-trend-scan`}
            multiple
            value={sectionScan}
            onChange={(e) => {
              const selected = Array.from(e.target.selectedOptions, (opt) => opt.value);
              setSectionScan(selected);
            }}
            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%', minHeight: '80px' }}
          >
            {sectionScanOptions.map((st) => (
              <option key={`${segment}-scan-${st}`} value={st}>
                {st}
              </option>
            ))}
          </select>
        </div>
        <div className="input-group" style={{ margin: 0, minWidth: 120 }}>
          <label htmlFor={`${segment}-trend-acl`}>ACL</label>
          <select
            id={`${segment}-trend-acl`}
            value={sectionAcl ? 'true' : 'false'}
            onChange={(e) => setSectionAcl(e.target.value === 'true')}
            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
          >
            <option value="false">Off</option>
            <option value="true">On</option>
          </select>
        </div>
        <div className="input-group" style={{ margin: 0, minWidth: 150 }}>
          <label htmlFor={`${segment}-trend-struct`}>Structured Discovery</label>
          <select
            id={`${segment}-trend-struct`}
            value={sectionStruct ? 'true' : 'false'}
            onChange={(e) => setSectionStruct(e.target.value === 'true')}
            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
          >
            <option value="false">Off</option>
            <option value="true">On</option>
          </select>
        </div>
      </div>

      {multi && (
        <>
          {combos.length > MAX_SERIES && (
            <p style={{ color: '#b45309', fontSize: '0.85rem', margin: '0 0 8px' }}>
              {combos.length} scenarios match — showing the first {MAX_SERIES}. Narrow Data source / File size / Scan type / ACL for a clearer comparison.
            </p>
          )}
          <div className="trend-chart-grid-2">
            <div className="trend-chart-cell">
              <h4 className="trend-chart-heading">Cluster {segment === 'structured' ? 'Tables' : 'GB'}/hr — by scenario (avg per major build)</h4>
              <div className="chart-container chart-trend">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={comboGb} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                    <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                    <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                    <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                    <Legend />
                    {shownCombos.map((c, i) => (
                      <Line key={c} type="monotone" dataKey={c} name={c} stroke={SERIES_COLORS[i % SERIES_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} connectNulls />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="trend-chart-cell">
              <h4 className="trend-chart-heading">Cluster {segment === 'structured' ? 'tables' : 'files'}/hr — by scenario (avg per major build)</h4>
              <div className="chart-container chart-trend">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={comboFiles} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                    <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                    <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                    <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                    <Legend />
                    {shownCombos.map((c, i) => (
                      <Line key={c} type="monotone" dataKey={c} name={c} stroke={SERIES_COLORS[i % SERIES_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} connectNulls />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </>
      )}

      {!multi && (<>
      <div className="trend-chart-grid-3">
        <div className="trend-chart-cell">
          <h4 className="trend-chart-heading">Data Throughput Trend (GB/hr)</h4>
          <div className="chart-container chart-trend">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                <Legend />
                <Line type="monotone" dataKey="cluster_gb" name="Cluster" stroke="#7c3aed" strokeWidth={3} dot={{ r: 4 }} connectNulls />
                <Line type="monotone" dataKey="scanner_gb" name="Per Scanner" stroke="#6366f1" strokeWidth={2} dot={{ r: 3 }} connectNulls />
                <Line type="monotone" dataKey="thread_gb" name="Per Thread" stroke="#c4b5fd" strokeWidth={2} dot={{ r: 3 }} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="trend-chart-cell">
          <h4 className="trend-chart-heading">{segment === 'structured' ? 'Table Throughput Trend (tables/hr)' : 'File Throughput Trend (files/hr)'}</h4>
          <div className="chart-container chart-trend">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                <XAxis dataKey="build" tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--text-secondary)" />
                <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                <Legend />
                <Line type="monotone" dataKey="cluster_files" name={segment === 'structured' ? 'Cluster tables/hr' : 'Cluster files/hr'} stroke="#059669" strokeWidth={3} dot={{ r: 4 }} connectNulls />
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
                <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
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
              <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
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
              <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
              <Legend />
              <Bar dataKey="cluster_files" name={segment === 'structured' ? 'Cluster tables/hr' : 'Cluster files/hr'} fill="#059669" />
              <Bar dataKey="scanner_files" name={segment === 'structured' ? 'Scanner tables/hr' : 'Scanner files/hr'} fill="#34d399" />
              <Bar dataKey="thread_files" name={segment === 'structured' ? 'Thread tables/hr' : 'Thread files/hr'} fill="#86efac" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      </>)}

      <div
        onClick={() => setShowSummary(!showSummary)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
          cursor: 'pointer',
          margin: '1.5rem 0 0.75rem',
          userSelect: 'none'
        }}
      >
        {showSummary ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
        <h4 className="trend-bar-heading" style={{ margin: 0 }}>Summary</h4>
      </div>

      {showSummary && (
        <div className="animate-in" style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Build</th>
                <th>Scan type</th>
                <th>Data source</th>
                <th>Cluster GB/hr</th>
                <th>{segment === 'structured' ? 'Cluster tables/hr' : 'Cluster files/hr'}</th>
                <th>Scan hours</th>
              </tr>
            </thead>
            <tbody>
              {summarySlice.map((r) => (
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
          {reversedSummary.length > SUMMARY_PAGE_SIZE && (
            <PaginationBar
              page={summaryPage}
              pageSize={SUMMARY_PAGE_SIZE}
              totalItems={reversedSummary.length}
              onPageChange={setSummaryPage}
              ariaLabel={`${title} summary table`}
            />
          )}
        </div>
      )}
    </section>
  );
};

const Dashboard: React.FC<DashboardProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const [filterRelease, setFilterRelease] = useState<boolean>(false);
  const [lastBuildWindow, setLastBuildWindow] = useState<number | 'all'>('all');

  const { data: trendPayload, isLoading: trendLoading, error: trendError } = useQuery({
    queryKey: ['trends-small-files', token],
    queryFn: async (): Promise<TrendApiPayload> => {
      const res = await axios.get<TrendApiPayload>(`${API_BASE}/api/trends/small-files`, {
        ...authConfig,
        params: { max_builds: 600 },
      });
      return res.data;
    },
    refetchInterval: 60000, // Refetch every 60 seconds instead of 20
    staleTime: 30000, // Data is fresh for 30 seconds
  });

  const trendRows = trendPayload?.rows ?? [];

  const filteredTrend = useMemo(() => {
    let t = trendRows;
    if (filterRelease) t = t.filter((r) => r.is_release);
    return t;
  }, [trendRows, filterRelease]);

  const structuredRows = useMemo(() => filteredTrend.filter((r) => r.data_segment === 'structured'), [filteredTrend]);
  const unstructuredRows = useMemo(() => filteredTrend.filter((r) => r.data_segment === 'unstructured'), [filteredTrend]);


  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      <section>
        <PageHeader icon={TrendingUp} title="Small Files Trend Analysis" />

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
          <div className="input-group" style={{ margin: 0, minWidth: 150, display: 'flex', flexDirection: 'row', alignItems: 'center', gap: '0.5rem', alignSelf: 'center', paddingTop: '1.2rem' }}>
            <input
              type="checkbox"
              id="trend-release"
              checked={filterRelease}
              onChange={(e) => setFilterRelease(e.target.checked)}
              style={{ width: '1.1rem', height: '1.1rem', cursor: 'pointer' }}
            />
            <label htmlFor="trend-release" style={{ margin: 0, cursor: 'pointer' }}>Release builds only</label>
          </div>
        </div>

        <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', margin: '0 0 1rem', maxWidth: '52rem', lineHeight: 1.45 }}>
          <strong>Build coverage (this Postgres / API):</strong>{' '}
          {trendPayload != null ? (
            <>
              <strong>{trendPayload.distinct_builds_total}</strong> distinct builds have benchmark graph rows (Small File(s) plus true structured relational/table runs). API caps the newest{' '}
              <strong>{trendPayload.build_window_cap}</strong> builds after semver sort — charts slice further via{' '}
              <em>Charts: build window</em>. If Streamlit on another host lists more builds, compare databases (migration completeness /{' '}
              <code style={{ fontSize: '0.85em' }}>DATABASE_URL</code>), not only this UI.
            </>
          ) : (
            <>Loading build counts…</>
          )}
        </p>
        <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', margin: '0 0 1rem', maxWidth: '52rem', lineHeight: 1.45 }}>
          Each segment (Unstructured / Structured) has its own <strong>Data source</strong> and <strong>Scan type</strong> filters below, allowing you to compare different benchmarks side-by-side. The filters above apply globally to all charts.
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
