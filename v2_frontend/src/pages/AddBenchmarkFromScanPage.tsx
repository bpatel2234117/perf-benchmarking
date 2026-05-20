import React, { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { ArrowLeft, Database, Loader2, CheckCircle } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import { legacyOpenBenchmarkUrl } from '../utils/legacyDashboard';

/** Mirrors dashboard.page_add_benchmark Save Benchmark throughput math. */
function computeThroughputs(
  durationHours: number,
  totalGb: number,
  totalFiles: number,
  scannerCount: number,
  threadCount: number
) {
  const sc = Math.max(1, Math.floor(scannerCount) || 1);
  const tc = Math.max(1, Math.floor(threadCount) || 1);
  const tp_cluster_gb = durationHours > 0 ? totalGb / durationHours : 0;
  const tp_cluster_files = durationHours > 0 ? totalFiles / durationHours : 0;
  return {
    throughput_cluster_gb_hr: tp_cluster_gb,
    throughput_cluster_files_hr: tp_cluster_files,
    throughput_scanner_gb_hr: tp_cluster_gb / sc,
    throughput_scanner_files_hr: tp_cluster_files / sc,
    throughput_thread_gb_hr: tp_cluster_gb / tc,
    throughput_thread_files_hr: tp_cluster_files / tc,
  };
}

interface FindingRow {
  'Classifier Attribute': string;
  'Attribute Value': string;
  Findings: number;
}

interface ScanDetailResponse {
  scan_id?: string;
  parent?: {
    scan_name?: string | null;
    scan_type?: string | null;
    scan_status?: string | null;
    template_name?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
  } | null;
  environment_name?: string | null;
  insights?: Record<string, unknown> | null;
  benchmark_context?: {
    prefill?: Record<string, unknown>;
    timing?: { duration_hours?: number | null };
    timing_fields?: {
      scan_start_date?: string | null;
      scan_start_time?: string | null;
      scan_end_date?: string | null;
      scan_end_time?: string | null;
      enum_end_date?: string | null;
      enum_end_time?: string | null;
    };
    run_metrics?: {
      build_version?: string;
      scenario_type?: string;
      scan_type?: string;
      data_source_type?: string;
      data_type?: string;
      file_set_type?: string;
      is_acl?: boolean;
      is_ff?: boolean;
    };
    file_set_details?: {
      file_set_type?: string;
      total_files?: number | null;
      total_folders?: number | null;
      total_size_gb?: number | null;
      insight_captions?: string[];
      preset_table_snapshot?: Record<string, unknown> | null;
    };
    findings_prefill?: FindingRow[];
    effective_volumes?: { total_files?: number; total_folders?: number; total_gb?: number };
    throughput_defaults?: { scanner_count: number; thread_count: number };
  } | null;
}

const fmtFiles = (n: number) =>
  Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—';

const cellStyle: React.CSSProperties = {
  padding: '8px 10px',
  borderBottom: '1px solid var(--surface-border)',
  fontSize: '0.86rem',
};

const AddBenchmarkFromScanPage: React.FC<{ token: string }> = ({ token }) => {
  const [searchParams] = useSearchParams();
  const scanId = searchParams.get('scan_id')?.trim() ?? '';

  const [scannerCount, setScannerCount] = useState(5);
  const [threadCount, setThreadCount] = useState(50);
  const [appliedApiDefaults, setAppliedApiDefaults] = useState(false);
  const [totalDbSizeGb, setTotalDbSizeGb] = useState<number | ''>('');
  const [totalTables, setTotalTables] = useState<number | ''>('');

  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const { data: uiConfig } = useQuery({
    queryKey: ['uiConfig'],
    queryFn: async () => {
      const res = await axios.get<{ legacy_dashboard_url?: string }>(`${API_BASE}/api/ui-config`);
      return res.data;
    },
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });

  const legacyStreamlitUrl = scanId ? legacyOpenBenchmarkUrl(scanId, uiConfig?.legacy_dashboard_url) : '';

  const { data: detail, isLoading, error } = useQuery({
    queryKey: ['scanDetailBenchmark', token, scanId],
    enabled: Boolean(scanId),
    queryFn: async () => {
      const res = await axios.get<ScanDetailResponse>(
        `${API_BASE}/api/scan/${encodeURIComponent(scanId)}/detail`,
        authConfig
      );
      return res.data;
    },
  });

  useEffect(() => {
    // Reset all prefill state when scan changes so stale values don't carry over
    setAppliedApiDefaults(false);
    setTotalDbSizeGb('');
    setTotalTables('');
  }, [scanId]);

  useEffect(() => {
    const d = detail?.benchmark_context?.throughput_defaults;
    if (d && !appliedApiDefaults) {
      setScannerCount(d.scanner_count);
      setThreadCount(d.thread_count);
      setAppliedApiDefaults(true);
    }
    // Pre-fill Total DB Size and Total Tables for structured DS.
    // Priority: file_set_details → effective_volumes (actual scan insights)
    const fsdLocal = detail?.benchmark_context?.file_set_details;
    const ev = detail?.benchmark_context?.effective_volumes;
    // Total Tables: prefer file_set_details.total_files (configured table count)
    if (fsdLocal?.total_files != null) {
      setTotalTables(fsdLocal.total_files);
    } else if (typeof ev?.total_files === 'number' && ev.total_files > 0) {
      setTotalTables(ev.total_files);
    }
    // Total DB Size: file_set_details rarely has this for Postgres; prefer effective_volumes
    if (fsdLocal?.total_size_gb != null && fsdLocal.total_size_gb > 0) {
      setTotalDbSizeGb(fsdLocal.total_size_gb);
    } else if (typeof ev?.total_gb === 'number' && ev.total_gb > 0) {
      setTotalDbSizeGb(ev.total_gb);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.benchmark_context]);

  const _STRUCTURED_DS = ['postgres', 'mssql', 'mysql', 'oracle'];
  const isStructured =
    (detail?.benchmark_context?.run_metrics?.data_type ?? '').toLowerCase() === 'structured' ||
    _STRUCTURED_DS.includes((detail?.benchmark_context?.run_metrics?.data_source_type ?? '').toLowerCase());

  const throughputLive = useMemo(() => {
    const bc = detail?.benchmark_context;
    const dur = bc?.timing?.duration_hours;
    if (dur == null || dur <= 0) return null;
    const ev = bc?.effective_volumes;
    // For Structured DS use the user-editable DB size / table count
    const totalFiles = isStructured && totalTables !== ''
      ? Number(totalTables)
      : (typeof ev?.total_files === 'number' ? ev.total_files : 0);
    const totalGb = isStructured && totalDbSizeGb !== ''
      ? Number(totalDbSizeGb)
      : (typeof ev?.total_gb === 'number' ? ev.total_gb : 0);
    return computeThroughputs(dur, totalGb, totalFiles, scannerCount, threadCount);
  }, [detail?.benchmark_context, scannerCount, threadCount, isStructured, totalDbSizeGb, totalTables]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const bc = detail?.benchmark_context;
      const rm = bc?.run_metrics;
      const tf = bc?.timing_fields;
      const fsd = bc?.file_set_type_details ?? bc?.file_set_details;
      const ev = bc?.effective_volumes;
      const tp = throughputLive;
      if (!rm?.build_version || !rm?.data_source_type || !rm?.file_set_type) {
        throw new Error('Missing build version, data source type, or file set type.');
      }
      const payload = {
        scan_id: scanId || null,
        build_version: rm.build_version,
        run_date: tf?.scan_end_date ?? (detail?.parent?.completed_at ? String(detail.parent.completed_at).slice(0, 10) : null),
        scenario_type: rm.scenario_type ?? null,
        scan_type: rm.scan_type ?? null,
        data_source_type: rm.data_source_type,
        data_type: rm.data_type ?? null,
        file_set_type: rm.file_set_type,
        scanner_count: scannerCount,
        thread_count: threadCount,
        is_acl: rm.is_acl ?? false,
        is_ocr: false,
        is_feature_flags: rm.is_ff ?? false,
        total_files: rm?.data_type?.toLowerCase() === 'structured' && totalTables !== ''
          ? Number(totalTables)
          : (typeof ev?.total_files === 'number' ? ev.total_files : (fsd as any)?.total_files ?? null),
        total_folders: typeof ev?.total_folders === 'number' ? ev.total_folders : (fsd as any)?.total_folders ?? null,
        total_size_gb: rm?.data_type?.toLowerCase() === 'structured' && totalDbSizeGb !== ''
          ? Number(totalDbSizeGb)
          : (typeof ev?.total_gb === 'number' ? ev.total_gb : (fsd as any)?.total_size_gb ?? null),
        scan_start_time: tf?.scan_start_date && tf?.scan_start_time ? `${tf.scan_start_date} ${tf.scan_start_time}` : null,
        scan_end_time: tf?.scan_end_date && tf?.scan_end_time ? `${tf.scan_end_date} ${tf.scan_end_time}` : null,
        total_scan_hours: bc?.timing?.duration_hours ?? null,
        throughput_cluster_gb_hr: tp?.throughput_cluster_gb_hr ?? null,
        throughput_scanner_gb_hr: tp?.throughput_scanner_gb_hr ?? null,
        throughput_thread_gb_hr: tp?.throughput_thread_gb_hr ?? null,
        throughput_cluster_files_hr: tp?.throughput_cluster_files_hr ?? null,
        throughput_scanner_files_hr: tp?.throughput_scanner_files_hr ?? null,
        throughput_thread_files_hr: tp?.throughput_thread_files_hr ?? null,
        total_catalog_items: null,
        is_release: false,
      };
      const res = await axios.post<{ ok: boolean; id: number; build_version: string }>(
        `${API_BASE}/api/benchmarks/save`,
        payload,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      return res.data;
    },
  });

  if (!scanId) {
    return (
      <div className="animate-in dashboard-root glass-panel" style={{ padding: '1.25rem', maxWidth: '36rem' }}>
        <p style={{ margin: 0, color: 'var(--text-secondary)' }}>
          Missing <code style={{ fontSize: '0.9em' }}>scan_id</code> in the URL. Use Open from Scan Status with a selected scan.
        </p>
        <Link to="/tools/scan-status" style={{ display: 'inline-block', marginTop: '1rem', fontWeight: 600 }}>
          ← Scan Status
        </Link>
      </div>
    );
  }

  const rm = detail?.benchmark_context?.run_metrics;
  const tf = detail?.benchmark_context?.timing_fields;
  const fsd = detail?.benchmark_context?.file_set_details;
  const findings = detail?.benchmark_context?.findings_prefill ?? [];

  const runDate =
    tf?.scan_end_date ??
    (detail?.parent?.completed_at ? String(detail.parent.completed_at).slice(0, 10) : undefined);

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1rem', maxWidth: '52rem' }}>
      <Link
        to="/tools/scan-status"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 8,
          fontWeight: 600,
          color: 'var(--primary-color)',
          fontSize: '0.88rem',
          textDecoration: 'none',
          width: 'fit-content',
        }}
      >
        <ArrowLeft size={18} aria-hidden /> Scan Status
      </Link>

      <div className="glass-panel" style={{ padding: '1.25rem 1.35rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.65rem' }}>
          <Database size={22} style={{ color: 'var(--primary-color)' }} aria-hidden />
          <h2 style={{ margin: 0, fontSize: '1.28rem', fontWeight: 700, color: 'var(--text-primary)' }}>
            Add benchmark from scan
          </h2>
        </div>
        <p style={{ margin: '0 0 0.85rem', color: 'var(--text-secondary)', lineHeight: 1.55, fontSize: '0.92rem' }}>
          Mirrors Streamlit <strong>Add New Benchmark</strong> prefill: scan name rules, <code style={{ fontSize: '0.85em' }}>file_set_types</code>{' '}
          preset row, <code style={{ fontSize: '0.85em' }}>scan_insights</code> overlays, timing split, findings rows (when delta tables exist), and
          throughput breakdown.
        </p>

        <div
          style={{
            padding: '0.75rem 0.85rem',
            background: 'var(--surface-ground, rgba(0,0,0,0.03))',
            borderRadius: 8,
            border: '1px solid var(--surface-border)',
            fontFamily: 'ui-monospace, monospace',
            fontSize: '0.78rem',
            wordBreak: 'break-all',
          }}
        >
          Prefill source scan id: <strong>{scanId}</strong>
          {detail?.parent?.scan_name ? (
            <>
              {' '}
              | name: <strong>{detail.parent.scan_name}</strong>
            </>
          ) : null}
        </div>

        {isLoading && (
          <div style={{ padding: '1.5rem', display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-secondary)' }}>
            <Loader2 className="lucide-spin" size={22} /> Loading…
          </div>
        )}

        {error && (
          <div style={{ marginTop: '0.85rem', color: 'var(--danger-color, #b91c1c)', fontSize: '0.88rem' }}>
            {(error as Error)?.message || 'Could not load scan detail.'}
          </div>
        )}

        {!isLoading && detail && !detail.parent && (
          <div
            style={{
              marginTop: '1rem',
              padding: '0.85rem',
              borderRadius: 8,
              background: 'rgba(220, 38, 38, 0.06)',
              border: '1px solid rgba(220, 38, 38, 0.25)',
              fontSize: '0.88rem',
              lineHeight: 1.55,
            }}
          >
            No <code style={{ fontSize: '0.85em' }}>scan_parents</code> row for this id in the DB your API uses. Local Postgres often differs from a
            remote Streamlit host — sync parents or point the API at the same database as{' '}
            <code style={{ fontSize: '0.85em' }}>10.2.20.89:8501</code> if you need identical rows.
          </div>
        )}

        {!isLoading && detail?.parent && (
          <>
            <h3 style={{ margin: '1.15rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>1. Build information</h3>
            <dl style={{ margin: 0, display: 'grid', gap: '0.35rem 1rem', fontSize: '0.88rem' }}>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Environment</dt>
              <dd style={{ margin: 0 }}>{detail.environment_name ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Build version</dt>
              <dd style={{ margin: 0 }}>{rm?.build_version || '—'}</dd>
            </dl>

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>2. Classifiers</h3>
            <dl style={{ margin: 0, display: 'grid', gap: '0.35rem 1rem', fontSize: '0.88rem' }}>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Template name</dt>
              <dd style={{ margin: 0 }}>Data Source Scan</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Classifiers (comma-separated)</dt>
              <dd style={{ margin: 0 }}>—</dd>
            </dl>

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>3. Configuration &amp; flags</h3>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', marginBottom: '0.35rem' }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.85rem', fontWeight: 600 }}>
                Scanner count
                <input
                  type="number"
                  min={1}
                  value={scannerCount}
                  onChange={(e) => setScannerCount(Number(e.target.value))}
                  style={{ padding: '0.45rem 0.5rem', borderRadius: 6, border: '1px solid var(--surface-border)', maxWidth: 120 }}
                />
              </label>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.85rem', fontWeight: 600 }}>
                Total threads (all scanners)
                <input
                  type="number"
                  min={1}
                  value={threadCount}
                  onChange={(e) => setThreadCount(Number(e.target.value))}
                  style={{ padding: '0.45rem 0.5rem', borderRadius: 6, border: '1px solid var(--surface-border)', maxWidth: 120 }}
                />
              </label>
            </div>
            <p style={{ margin: 0, fontSize: '0.84rem', color: 'var(--text-secondary)' }}>
              ACL {rm?.is_acl ? 'on' : 'off'} · Structured FF {rm?.is_ff ? 'on' : 'off'}
            </p>

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>4. Run metrics</h3>
            <dl style={{ margin: 0, display: 'grid', gap: '0.35rem 1rem', fontSize: '0.88rem' }}>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Run date</dt>
              <dd style={{ margin: 0 }}>{runDate ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scenario type</dt>
              <dd style={{ margin: 0 }}>{rm?.scenario_type || '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scan type</dt>
              <dd style={{ margin: 0 }}>{rm?.scan_type || '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Data source type</dt>
              <dd style={{ margin: 0 }}>{rm?.data_source_type || '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Data type</dt>
              <dd style={{ margin: 0 }}>{rm?.data_type || '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>File set type</dt>
              <dd style={{ margin: 0 }}>{rm?.file_set_type || '—'}</dd>
            </dl>

            <p style={{ margin: '0.65rem 0 0.35rem', fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
              File set type details (auto-filled)
            </p>
            <div style={{ overflowX: 'auto', border: '1px solid var(--surface-border)', borderRadius: 8 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ background: 'var(--surface-ground, rgba(0,0,0,0.04))' }}>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>File set type</th>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>{isStructured ? 'Total tables' : 'Total files'}</th>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>{isStructured ? 'Total schemas' : 'Total folders'}</th>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>Total size (GB)</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td style={cellStyle}>{fsd?.file_set_type ?? '—'}</td>
                    <td style={cellStyle}>{fsd?.total_files != null ? fsd.total_files.toLocaleString() : '—'}</td>
                    <td style={cellStyle}>{fsd?.total_folders != null ? fsd.total_folders.toLocaleString() : '—'}</td>
                    <td style={cellStyle}>
                      {fsd?.total_size_gb != null ? Number(fsd.total_size_gb).toFixed(4) : '—'}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            {(fsd?.insight_captions ?? []).map((c) => (
              <p key={c} style={{ margin: '0.45rem 0 0', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                {c}
              </p>
            ))}

            {isStructured && (
              <div style={{ marginTop: '0.85rem', display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.85rem', fontWeight: 600 }}>
                  Total DB Size (GB)
                  <input
                    type="number"
                    min={0}
                    step={1}
                    value={totalDbSizeGb}
                    onChange={(e) => setTotalDbSizeGb(e.target.value === '' ? '' : Number(e.target.value))}
                    placeholder="e.g. 20"
                    style={{ padding: '0.45rem 0.5rem', borderRadius: 6, border: '1px solid var(--surface-border)', maxWidth: 140 }}
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.85rem', fontWeight: 600 }}>
                  Total Tables
                  <input
                    type="number"
                    min={0}
                    step={100}
                    value={totalTables}
                    onChange={(e) => setTotalTables(e.target.value === '' ? '' : Number(e.target.value))}
                    placeholder="e.g. 10000"
                    style={{ padding: '0.45rem 0.5rem', borderRadius: 6, border: '1px solid var(--surface-border)', maxWidth: 140 }}
                  />
                </label>
              </div>
            )}

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>Scan timing</h3>
            <p style={{ margin: '0 0 0.45rem', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              Enter start and end times — throughput uses duration derived from these fields (same as Streamlit).
            </p>
            <dl style={{ margin: 0, display: 'grid', gap: '0.35rem 1rem', fontSize: '0.88rem', gridTemplateColumns: '160px 1fr' }}>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scan start date</dt>
              <dd style={{ margin: 0 }}>{tf?.scan_start_date ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scan start time</dt>
              <dd style={{ margin: 0 }}>{tf?.scan_start_time ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scan end date</dt>
              <dd style={{ margin: 0 }}>{tf?.scan_end_date ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Scan end time</dt>
              <dd style={{ margin: 0 }}>{tf?.scan_end_time ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Enum end date</dt>
              <dd style={{ margin: 0 }}>{tf?.enum_end_date ?? '—'}</dd>
              <dt style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>Enum end time</dt>
              <dd style={{ margin: 0 }}>{tf?.enum_end_time ?? '—'}</dd>
            </dl>

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>5. Findings files</h3>
            <p style={{ margin: '0 0 0.45rem', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              From <code style={{ fontSize: '0.85em' }}>scan_insight_attribute_delta_map</code> when present; else{' '}
              <code style={{ fontSize: '0.85em' }}>scan_progress.total_classifications</code> aggregate (same fallbacks as Streamlit).
            </p>
            <div style={{ overflowX: 'auto', border: '1px solid var(--surface-border)', borderRadius: 8 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ background: 'var(--surface-ground, rgba(0,0,0,0.04))' }}>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>Classifier attribute</th>
                    <th style={{ ...cellStyle, textAlign: 'left' }}>Attribute value</th>
                    <th style={{ ...cellStyle, textAlign: 'right' }}>Findings</th>
                  </tr>
                </thead>
                <tbody>
                  {findings.length === 0 ? (
                    <tr>
                      <td colSpan={3} style={{ ...cellStyle, color: 'var(--text-secondary)' }}>
                        No rows yet — run Sync on Scan Progress / tracker so attributes land in Postgres.
                      </td>
                    </tr>
                  ) : (
                    findings.map((r, i) => (
                      <tr key={`${r['Classifier Attribute']}-${r['Attribute Value']}-${i}`}>
                        <td style={cellStyle}>{r['Classifier Attribute']}</td>
                        <td style={cellStyle}>{r['Attribute Value']}</td>
                        <td style={{ ...cellStyle, textAlign: 'right' }}>{r.Findings}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <h3 style={{ margin: '1rem 0 0.45rem', fontSize: '1rem', fontWeight: 700 }}>Calculated throughputs</h3>
            {throughputLive ? (
              <div
                style={{
                  padding: '0.85rem 1rem',
                  background: 'rgba(99, 102, 241, 0.06)',
                  borderRadius: 8,
                  border: '1px solid rgba(99, 102, 241, 0.25)',
                  fontSize: '0.88rem',
                  lineHeight: 1.65,
                }}
              >
                <ul style={{ margin: 0, paddingLeft: '1.15rem' }}>
                  <li>
                    Cluster: {throughputLive.throughput_cluster_gb_hr.toFixed(2)} GB/hr,{' '}
                    {fmtFiles(throughputLive.throughput_cluster_files_hr)} {isStructured ? 'tables/hr' : 'files/hr'}
                  </li>
                  <li>
                    Per scanner: {throughputLive.throughput_scanner_gb_hr.toFixed(2)} GB/hr,{' '}
                    {fmtFiles(throughputLive.throughput_scanner_files_hr)} {isStructured ? 'tables/hr' : 'files/hr'}
                  </li>
                  <li>
                    Per thread: {throughputLive.throughput_thread_gb_hr.toFixed(2)} GB/hr,{' '}
                    {fmtFiles(throughputLive.throughput_thread_files_hr)} {isStructured ? 'tables/hr' : 'files/hr'}
                  </li>
                </ul>
              </div>
            ) : (
              <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                Need valid parent start/end times and volumes — sync scan_insights if totals are missing.
              </p>
            )}
          </>
        )}

        {/* Save to Postgres */}
        <div style={{ marginTop: '1.25rem', display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
          <button
            type="button"
            disabled={saveMutation.isPending || !throughputLive || !detail?.parent}
            onClick={() => saveMutation.mutate()}
            style={{
              padding: '0.6rem 1.25rem',
              borderRadius: 8,
              border: 'none',
              background: saveMutation.isSuccess ? '#16a34a' : 'var(--primary-color, #6366f1)',
              color: '#fff',
              fontWeight: 700,
              cursor: (!throughputLive || !detail?.parent || saveMutation.isPending) ? 'not-allowed' : 'pointer',
              fontFamily: 'inherit',
              fontSize: '0.9rem',
              opacity: (!throughputLive || !detail?.parent) ? 0.55 : 1,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            {saveMutation.isPending ? (
              <><Loader2 size={16} className="lucide-spin" /> Saving…</>
            ) : saveMutation.isSuccess ? (
              <><CheckCircle size={16} /> Saved (ID {saveMutation.data?.id})</>
            ) : (
              <><Database size={16} /> Save Benchmark to Postgres</>
            )}
          </button>
          {saveMutation.isError && (
            <span style={{ color: 'var(--danger-color)', fontSize: '0.85rem' }}>
              {axios.isAxiosError(saveMutation.error)
                ? String((saveMutation.error.response?.data as { detail?: string })?.detail ?? saveMutation.error.message)
                : String(saveMutation.error)}
            </span>
          )}
          {!throughputLive && detail?.parent && (
            <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              Need valid scan timing to calculate throughput before saving.
            </span>
          )}
        </div>

        {legacyStreamlitUrl ? (
          <p style={{ margin: '1rem 0 0', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
            <a href={legacyStreamlitUrl} target="_blank" rel="noreferrer noopener" style={{ fontWeight: 600, color: 'var(--primary-color)' }}>
              Open Streamlit Add New Benchmark
            </a>{' '}
            (same flow + Save Benchmark Run)
          </p>
        ) : null}

        <div style={{ marginTop: '1rem' }}>
          <Link to={`/tools/scan-status?scan_id=${encodeURIComponent(scanId)}`} style={{ fontWeight: 600, color: 'var(--primary-color)' }}>
            View scan progress →
          </Link>
        </div>
      </div>
    </div>
  );
};

export default AddBenchmarkFromScanPage;
