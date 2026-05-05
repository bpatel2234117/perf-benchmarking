import { useMemo, useState } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ComposedChart,
} from 'recharts';
import { ArrowLeft, Loader2, TrendingUp } from 'lucide-react';
import { API_BASE } from '../apiConfig';

interface ProgressHistoryRow {
  id: number | null;
  scan_id: string;
  scan_name: string | null;
  environment_id: number | null;
  timestamp: string | null;
  captured_at_utc: string | null;
  scan_state: string | null;
  completed_sub_scans: number | null;
  failed_sub_scans: number | null;
  total_sub_scans: number | null;
  completed_parts: number | null;
  failed_parts: number | null;
  total_parts_scanned: number | null;
  total_classifications: number | null;
  newly_added_classifications: number | null;
  false_removed_classifications: number | null;
  objects_scanned: number | null;
  objects_failed: number | null;
  objects_with_pi: number | null;
}

interface ScanDetailMeta {
  scan_id: string;
  parent: {
    scan_id: string;
    scan_name: string | null;
    scan_type: string | null;
    scan_status: string | null;
    environment_id: number | null;
    started_at: string | null;
    completed_at: string | null;
    template_name: string | null;
  } | null;
  environment_name: string | null;
  resolved_environment_id: number | null;
  insights: {
    objects_scanned: number | null;
    total_classification: number | null;
    objects_with_pi: number | null;
    distinct_classification_attributes: number | null;
  } | null;
  latest_snapshot_this_scan: string | null;
  latest_snapshot_global: string | null;
  tracker_heartbeat: {
    last_started_at_utc: string | null;
    last_finished_at_utc: string | null;
    last_status: string | null;
    last_message: string | null;
  } | null;
  manual_sync_disabled: boolean;
}

interface ClassifierHitRow {
  classifier: string;
  new_findings: number;
  removed: number;
}

interface ClassifierHitsResponse {
  scan_id: string;
  rows: ClassifierHitRow[];
  note: string;
  total_count_hint: number | null;
  fallback_classifier_names: string[];
  source: string;
}

interface ScannerSeriesResponse {
  available: boolean;
  scan_id: string;
  environment_id: number | null;
  window_start: string | null;
  window_end: string | null;
  window?: string | null;
  groups: Array<Record<string, unknown>>;
  individuals: Array<Record<string, unknown>>;
  message?: string;
  summary?: { group_samples: number; pod_samples: number };
}

interface LogCorrelationResponse {
  scan_id: string;
  report: string | null;
  timestamp: string | null;
  is_final: boolean | null;
}

interface LogErrorRow {
  log_timestamp: string | null;
  log_source: string | null;
  scan_id: string | null;
  error_context: string;
  ai_analysis: string;
  is_resolved: boolean;
}

const DETAIL_HISTORY_LIMIT = 4000;

function fmtCell(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  if (typeof v === 'number' && Number.isNaN(v)) return '—';
  return String(v);
}

function num(v: number | null | undefined): number {
  if (v == null || Number.isNaN(Number(v))) return 0;
  return Number(v);
}

function rowTime(r: ProgressHistoryRow): string {
  return (r.captured_at_utc || r.timestamp || '').trim();
}

function chartTickLabel(iso: string): string {
  if (!iso) return '';
  const d = Date.parse(iso);
  if (Number.isNaN(d)) return iso.slice(5, 16);
  return new Date(d).toISOString().slice(5, 16).replace('T', ' ');
}

/** Detect lag between 100% parts completion and terminal scan state (DB snapshots). */
function computeCompletionLag(rows: ProgressHistoryRow[]): string | null {
  if (rows.length < 2) return null;
  let hit100: { t: number; objs: number } | null = null;
  const terminalRe = /completed|failed|stopped|aborted|cancel/i;
  for (const r of rows) {
    const tp = num(r.total_parts_scanned);
    const cp = num(r.completed_parts);
    const tt = Date.parse(rowTime(r));
    if (Number.isNaN(tt)) continue;
    if (tp > 0 && cp >= tp && !hit100) {
      hit100 = { t: tt, objs: num(r.objects_scanned) };
      break;
    }
  }
  let lastTerminal: { t: number; objs: number; state: string } | null = null;
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const r = rows[i];
    const st = String(r.scan_state || '');
    if (st && terminalRe.test(st)) {
      const tt = Date.parse(rowTime(r));
      if (!Number.isNaN(tt)) {
        lastTerminal = { t: tt, objs: num(r.objects_scanned), state: st };
        break;
      }
    }
  }
  if (!hit100 || !lastTerminal || lastTerminal.t <= hit100.t + 120000) return null;
  const lagH = (lastTerminal.t - hit100.t) / 3600000;
  if (lagH < 0.05) return null;
  const objDelta = Math.abs(lastTerminal.objs - hit100.objs);
  const t100 = new Date(hit100.t).toISOString().slice(0, 16).replace('T', ' ');
  const tEnd = new Date(lastTerminal.t).toISOString().slice(0, 16).replace('T', ' ');
  return `Reached 100% parts at ${t100} UTC but terminal state (${lastTerminal.state}) was reached at ${tEnd} UTC (lag ≈ ${lagH.toFixed(1)} h). Objects scanned changed by ${objDelta.toLocaleString()} during this window.`;
}

function buildScanProgressMarkdown(params: {
  scanId: string;
  meta: ScanDetailMeta | undefined;
  analysis: {
    objects: number;
    partsPct: number | null;
    failedParts: number;
    falseRemovedDelta: number;
    classDelta: number;
    throughput?: string;
    scanDuration?: string;
  } | null;
  rows: ProgressHistoryRow[];
  hits: ClassifierHitsResponse | undefined;
  correlation: LogCorrelationResponse | undefined;
  lagText: string | null;
}): string {
  const { scanId, meta, analysis, rows, hits, correlation, lagText } = params;
  const p = meta?.parent;
  const lines: string[] = [];
  lines.push(`# Scan Progress Report`);
  lines.push('');
  lines.push(`- **Scan ID:** \`${scanId}\``);
  if (p?.scan_name) lines.push(`- **Name:** ${p.scan_name}`);
  if (meta?.environment_name) lines.push(`- **Environment:** ${meta.environment_name}`);
  lines.push('');
  if (lagText) {
    lines.push(`## Completion lag`);
    lines.push(lagText);
    lines.push('');
  }
  if (analysis) {
    lines.push(`## Summary`);
    lines.push(`- Objects scanned: **${analysis.objects.toLocaleString()}**`);
    lines.push(`- Parts progress: **${analysis.partsPct != null ? `${analysis.partsPct.toFixed(1)}%` : '—'}**`);
    lines.push(`- Failed parts: **${analysis.failedParts}**`);
    lines.push('');
  }
  if (hits?.rows?.length) {
    lines.push(`## Classifier / attribute hits`);
    lines.push('| Classifier | New | Removed |');
    lines.push('| --- | ---: | ---: |');
    for (const r of hits.rows.slice(0, 80)) {
      const nm = String(r.classifier || '').replace(/\|/g, '\\|');
      lines.push(`| ${nm} | ${r.new_findings} | ${r.removed} |`);
    }
    lines.push('');
  }
  if (correlation?.report) {
    lines.push(`## AI correlation`);
    lines.push(String(correlation.report));
    lines.push('');
  }
  lines.push(`## Snapshots`);
  lines.push(`Total rows in export sample: **${rows.length}**`);
  return lines.join('\n');
}

/** Full scan detail when `?scan_id=` is present (used by merged Scan Status page). */
export function ScanProgressDetail({ scanId, token }: { scanId: string; token: string }) {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [hitsLive, setHitsLive] = useState(false);
  const [hitsFilterNonZero, setHitsFilterNonZero] = useState(true);
  const [scannerTab, setScannerTab] = useState<'overview' | 'groups' | 'pods' | 'raw'>('overview');

  const { data: meta, isLoading: metaLoading, error: metaError } = useQuery({
    queryKey: ['scanProgressDetail', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/detail`, authConfig);
      return res.data as ScanDetailMeta;
    },
    refetchInterval: 30000,
  });

  const { data: histData, isLoading: histLoading, error: histError } = useQuery({
    queryKey: ['scanProgressHistory', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/progress-history`, {
        ...authConfig,
        params: { limit: DETAIL_HISTORY_LIMIT },
      });
      return res.data as { items: ProgressHistoryRow[]; total: number; returned: number };
    },
    refetchInterval: 20000,
  });

  const { data: hitsData, isLoading: hitsLoading } = useQuery({
    queryKey: ['scanClassifierHits', token, scanId, hitsLive],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/classifier-hits`, {
        ...authConfig,
        params: hitsLive ? { live: true } : {},
      });
      return res.data as ClassifierHitsResponse;
    },
    refetchInterval: 90000,
  });

  const { data: scannerData } = useQuery({
    queryKey: ['scanScannerSeries', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/scanner-series`, authConfig);
      return res.data as ScannerSeriesResponse;
    },
    refetchInterval: 60000,
  });

  const { data: correlationData } = useQuery({
    queryKey: ['scanLogCorrelation', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/log-correlation`, authConfig);
      return res.data as LogCorrelationResponse;
    },
    refetchInterval: 120000,
  });

  const { data: scanErrorsData } = useQuery({
    queryKey: ['scanLogErrors', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/log-errors`, {
        ...authConfig,
        params: { limit: 35 },
      });
      return res.data as { scan_id: string; items: LogErrorRow[] };
    },
  });

  const syncMutation = useMutation({
    mutationFn: async () => {
      const res = await axios.post<{ ok: boolean; detail?: string }>(
        `${API_BASE}/api/scan/sync-insights`,
        { scan_id: scanId },
        authConfig
      );
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanProgressDetail', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanProgressHistory', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanClassifierHits'] });
      queryClient.invalidateQueries({ queryKey: ['scanScannerSeries', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanLogCorrelation', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanLogErrors', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanUnified'] });
      queryClient.invalidateQueries({ queryKey: ['scanProgressLatest'] });
      setHitsLive(false);
    },
  });

  const rows = histData?.items ?? [];
  const chartPoints = useMemo(() => {
    return rows.map((r) => {
      const tlab = chartTickLabel(rowTime(r));
      return {
        t: tlab,
        partsTotal: num(r.total_parts_scanned),
        partsDone: num(r.completed_parts),
        partsFailed: num(r.failed_parts),
        objects: num(r.objects_scanned),
        objectsFailed: num(r.objects_failed),
        classTotal: num(r.total_classifications),
        newlyAdded: num(r.newly_added_classifications),
        falseRemoved: num(r.false_removed_classifications),
        owpi: num(r.objects_with_pi),
      };
    });
  }, [rows]);

  const analysis = useMemo(() => {
    if (!rows.length) return null;
    const first = rows[0];
    const last = rows[rows.length - 1];
    const tp = num(last.total_parts_scanned);
    const cp = num(last.completed_parts);
    const fp = num(last.failed_parts);
    const partsPct = tp > 0 ? (cp / tp) * 100 : null;
    const classDelta = num(last.total_classifications) - num(first.total_classifications);
    const t0 = Date.parse(rowTime(first));
    const t1 = Date.parse(rowTime(last));
    let throughput = '';
    if (!Number.isNaN(t0) && !Number.isNaN(t1) && t1 > t0) {
      const mins = (t1 - t0) / 60000;
      const objDelta = num(last.objects_scanned) - num(first.objects_scanned);
      if (mins > 0.5 && objDelta >= 0) {
        throughput = `${(objDelta / mins).toFixed(1)} objects/min (first→last snapshot)`;
      }
    }
    let scanDuration = '';
    const durMs = !Number.isNaN(t0) && !Number.isNaN(t1) ? t1 - t0 : 0;
    if (durMs > 0) {
      const h = Math.floor(durMs / 3600000);
      const m = Math.floor((durMs % 3600000) / 60000);
      scanDuration = `${h}h ${m}m`;
    }
    return {
      last,
      partsPct,
      failedParts: fp,
      falseRemovedDelta: num(last.false_removed_classifications) - num(first.false_removed_classifications),
      classDelta,
      throughput,
      scanDuration,
      objects: num(last.objects_scanned),
    };
  }, [rows]);

  const lagText = useMemo(() => computeCompletionLag(rows), [rows]);

  const filteredClassifierRows = useMemo(() => {
    const raw = hitsData?.rows ?? [];
    if (!hitsFilterNonZero) return raw;
    return raw.filter((r) => r.new_findings > 0 || r.removed > 0);
  }, [hitsData?.rows, hitsFilterNonZero]);

  const overviewBars = useMemo(() => {
    const ind = scannerData?.individuals;
    if (!ind || !Array.isArray(ind) || ind.length === 0) return [];
    type Pod = {
      captured_at_utc?: string;
      scanner_id?: string;
      name?: string;
      cpu_pct?: number;
      memory_usage_pct?: number;
      running_threads?: number;
    };
    const latest = new Map<string, Pod>();
    for (const row of ind as Pod[]) {
      const id = String(row.scanner_id || row.name || '').trim() || 'unknown';
      const prev = latest.get(id);
      const t = String(row.captured_at_utc || '');
      const pt = prev ? String(prev.captured_at_utc || '') : '';
      if (!prev || t > pt) latest.set(id, row);
    }
    return [...latest.values()].slice(0, 14).map((r) => ({
      name: (r.name || r.scanner_id || '?').slice(0, 28),
      threads: Number(r.running_threads) || 0,
      cpu: r.cpu_pct != null ? Number(r.cpu_pct) : 0,
      mem: r.memory_usage_pct != null ? Number(r.memory_usage_pct) : 0,
    }));
  }, [scannerData]);

  const groupChartPoints = useMemo(() => {
    const g = scannerData?.groups;
    if (!g || !Array.isArray(g)) return [];
    return (
      g as Array<{
        captured_at_utc?: string;
        running_scanners?: number;
        avg_scan_speed_kib_s?: number;
        used_memory_mb?: number;
      }>
    ).map((row) => ({
      t: chartTickLabel(String(row.captured_at_utc || '')),
      scanners: Number(row.running_scanners) || 0,
      kibs: Number(row.avg_scan_speed_kib_s) || 0,
      memMb: Number(row.used_memory_mb) || 0,
    }));
  }, [scannerData]);

  const podLinesByScanner = useMemo(() => {
    const ind = scannerData?.individuals;
    if (!ind || !Array.isArray(ind)) return [];
    type Pod = { captured_at_utc?: string; scanner_id?: string; cpu_pct?: number };
    const byScanner = new Map<string, Pod[]>();
    for (const row of ind as Pod[]) {
      const sid = String(row.scanner_id || '').trim();
      if (!sid) continue;
      const arr = byScanner.get(sid) || [];
      arr.push(row);
      byScanner.set(sid, arr);
    }
    const top = [...byScanner.entries()]
      .sort((a, b) => b[1].length - a[1].length)
      .slice(0, 4)
      .map(([id]) => id);
    return top.map((id) => {
      const pts = (byScanner.get(id) || [])
        .slice()
        .sort((a, b) => String(a.captured_at_utc).localeCompare(String(b.captured_at_utc)));
      return {
        id,
        points: pts.map((p) => ({
          t: chartTickLabel(String(p.captured_at_utc || '')),
          cpu: p.cpu_pct != null ? Number(p.cpu_pct) : 0,
        })),
      };
    });
  }, [scannerData]);

  const parent = meta?.parent;
  const displayTitle =
    (parent?.scan_name && String(parent.scan_name).trim()) || parent?.scan_name || '';
  const syncDisabled =
    meta?.manual_sync_disabled || syncMutation.isPending || !parent?.environment_id;

  const loading = metaLoading || histLoading;

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.75rem' }}>
        <button
          type="button"
          onClick={() => navigate('/tools/scan-status')}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '0.45rem 0.75rem',
            borderRadius: 8,
            border: '1px solid var(--surface-border)',
            background: 'var(--surface-ground)',
            cursor: 'pointer',
            fontFamily: 'inherit',
            fontWeight: 600,
            fontSize: '0.88rem',
          }}
        >
          <ArrowLeft size={18} aria-hidden /> Back to all scans
        </button>
        <Link
          to="/tools/scan-status"
          style={{ fontSize: '0.88rem', color: 'var(--primary-color)', fontWeight: 600 }}
        >
          Scan Status
        </Link>
      </div>

      <section>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.75rem', flexWrap: 'wrap' }}>
          <TrendingUp size={28} style={{ color: 'var(--primary-color)', flexShrink: 0 }} aria-hidden />
          <div style={{ flex: '1 1 280px' }}>
            <h2 style={{ color: 'var(--text-primary)', fontSize: '1.35rem', fontWeight: 700, margin: 0 }}>
              Scan Progress Details
            </h2>
            {displayTitle ? (
              <p style={{ color: 'var(--text-primary)', margin: '0.35rem 0 0', fontWeight: 600, fontSize: '0.95rem' }}>
                Scan: {displayTitle}
              </p>
            ) : null}
            <p style={{ color: 'var(--text-secondary)', margin: '0.35rem 0 0', fontSize: '0.88rem', fontFamily: 'ui-monospace, monospace' }}>
              Scan ID: <span style={{ color: 'var(--text-primary)' }}>{scanId}</span>
              {meta?.environment_name ? (
                <>
                  {' '}
                  · {meta.environment_name}
                  {meta.resolved_environment_id != null ? ` (#${meta.resolved_environment_id})` : ''}
                </>
              ) : null}
            </p>
          </div>
        </div>

        <div style={{ marginTop: '1rem', display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
          <button
            type="button"
            disabled={Boolean(syncDisabled)}
            onClick={() => syncMutation.mutate()}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: 8,
              border: 'none',
              background: syncDisabled ? '#94a3b8' : 'var(--primary-color, #6366f1)',
              color: '#fff',
              fontWeight: 700,
              cursor: syncDisabled ? 'not-allowed' : 'pointer',
              fontFamily: 'inherit',
              fontSize: '0.88rem',
            }}
          >
            {syncMutation.isPending ? 'Syncing…' : 'Sync now (manual)'}
          </button>
          {meta?.manual_sync_disabled ? (
            <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              Manual sync is disabled because the scan has finished (terminal status).
            </span>
          ) : null}
          {!parent?.environment_id ? (
            <span style={{ fontSize: '0.82rem', color: 'var(--danger-color)' }}>
              Cannot sync: no <code>scan_parents</code> row / environment for this id — use BigID or Re-sync list from Scan Status.
            </span>
          ) : null}
        </div>

        {(metaError || histError) && (
          <div className="glass-panel" style={{ padding: '1rem', color: 'var(--danger-color)', marginTop: '1rem' }}>
            {(metaError as Error)?.message || (histError as Error)?.message || 'Failed to load'}
          </div>
        )}
        {syncMutation.isError && (
          <div className="glass-panel" style={{ padding: '0.75rem 1rem', color: 'var(--danger-color)', fontSize: '0.88rem' }}>
            {axios.isAxiosError(syncMutation.error)
              ? String((syncMutation.error.response?.data as { detail?: string })?.detail ?? syncMutation.error.message)
              : String(syncMutation.error)}
          </div>
        )}
      </section>

      {!loading && meta && (
        <div className="glass-panel" style={{ padding: '1rem 1.15rem', fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
          {meta.latest_snapshot_this_scan ? (
            <div>
              Latest snapshot (this scan): <strong style={{ color: 'var(--text-primary)' }}>{meta.latest_snapshot_this_scan}</strong>
            </div>
          ) : (
            <div>No snapshots in DB for this scan yet — run the tracker.</div>
          )}
          {meta.latest_snapshot_global ? (
            <div style={{ marginTop: 6 }}>
              Latest snapshot (any scan in DB):{' '}
              <strong style={{ color: 'var(--text-primary)' }}>{meta.latest_snapshot_global}</strong>
            </div>
          ) : null}
          {meta.tracker_heartbeat?.last_finished_at_utc ? (
            <div style={{ marginTop: 6 }}>
              Tracker heartbeat: status={meta.tracker_heartbeat.last_status ?? '—'} · finished=
              {meta.tracker_heartbeat.last_finished_at_utc?.slice(11, 16) ?? '—'} UTC
            </div>
          ) : null}
        </div>
      )}

      {loading ? (
        <div style={{ padding: '3rem', textAlign: 'center' }}>
          <Loader2 className="lucide-spin" size={32} />
        </div>
      ) : rows.length === 0 ? (
        <div className="glass-panel" style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
          <p style={{ marginBottom: '0.75rem' }}>No saved snapshots yet for this scan.</p>
          <p style={{ fontSize: '0.88rem' }}>
            Waiting for <code style={{ fontSize: '0.85em' }}>tracker.py</code> to write rows into{' '}
            <code style={{ fontSize: '0.85em' }}>scan_progress</code>.
          </p>
        </div>
      ) : (
        <>
          {lagText ? (
            <div
              className="glass-panel"
              style={{
                padding: '0.85rem 1rem',
                background: 'rgba(254, 226, 226, 0.55)',
                border: '1px solid rgba(248, 113, 113, 0.45)',
                color: '#7f1d1d',
                fontSize: '0.88rem',
                lineHeight: 1.55,
              }}
            >
              <strong>Status update lag detected:</strong> {lagText}
            </div>
          ) : null}

          <section className="glass-panel" style={{ padding: '1.15rem 1.25rem' }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', justifyContent: 'space-between', gap: '1rem' }}>
              <h3 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                Analysis (stats + narrative)
              </h3>
              <button
                type="button"
                onClick={() => {
                  const md = buildScanProgressMarkdown({
                    scanId,
                    meta,
                    analysis,
                    rows,
                    hits: hitsData,
                    correlation: correlationData,
                    lagText,
                  });
                  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement('a');
                  a.href = url;
                  a.download = `scan_progress_${scanId.slice(0, 12)}_${new Date().toISOString().slice(0, 10)}.md`;
                  a.click();
                  URL.revokeObjectURL(url);
                }}
                style={{
                  padding: '0.45rem 0.85rem',
                  borderRadius: 8,
                  border: '1px solid var(--surface-border)',
                  background: 'var(--surface-ground)',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  fontWeight: 600,
                  fontSize: '0.82rem',
                }}
              >
                Download Report (md)
              </button>
            </div>
            {analysis && (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
                  gap: '1rem',
                  marginBottom: '1.25rem',
                }}
              >
                {[
                  ['Objects scanned', analysis.objects.toLocaleString()],
                  ['Parts progress', analysis.partsPct != null ? `${analysis.partsPct.toFixed(1)}%` : '—'],
                  ['Failed parts', String(analysis.failedParts)],
                  ['falseRemoved (Δ)', `${analysis.falseRemovedDelta >= 0 ? '+' : ''}${analysis.falseRemovedDelta}`],
                  ['Classifications (Δ)', `${analysis.classDelta >= 0 ? '+' : ''}${analysis.classDelta}`],
                  ['Scan window', analysis.scanDuration || '—'],
                ].map(([label, val]) => (
                  <div key={label}>
                    <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>
                      {label}
                    </div>
                    <div style={{ fontSize: '1.35rem', fontWeight: 800, color: 'var(--primary-color)', marginTop: 4 }}>{val}</div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '1rem', fontSize: '0.88rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
              <div>
                <strong style={{ color: 'var(--text-primary)' }}>Timing</strong>
                <ul style={{ margin: '0.5rem 0 0', paddingLeft: '1.1rem' }}>
                  <li>
                    Start:{' '}
                    <span style={{ color: '#059669', fontFamily: 'ui-monospace, monospace', fontSize: '0.82rem' }}>
                      {fmtCell(parent?.started_at)}
                    </span>
                  </li>
                  <li>
                    End:{' '}
                    <span style={{ color: '#059669', fontFamily: 'ui-monospace, monospace', fontSize: '0.82rem' }}>
                      {fmtCell(parent?.completed_at)}
                    </span>
                  </li>
                  {analysis?.throughput ? (
                    <li>
                      Throughput: <strong style={{ color: 'var(--text-primary)' }}>{analysis.throughput}</strong>
                    </li>
                  ) : null}
                </ul>
              </div>
              <div>
                <strong style={{ color: 'var(--text-primary)' }}>Objects</strong>
                <p style={{ margin: '0.5rem 0 0' }}>
                  Total classifications changed by{' '}
                  <strong style={{ color: 'var(--text-primary)' }}>
                    {analysis ? `${analysis.classDelta >= 0 ? '+' : ''}${analysis.classDelta}` : '—'}
                  </strong>{' '}
                  from first to latest snapshot.
                  {analysis?.throughput ? (
                    <>
                      {' '}
                      Recent object throughput ≈ <strong style={{ color: 'var(--text-primary)' }}>{analysis.throughput}</strong>.
                    </>
                  ) : null}{' '}
                  Compare classifier deltas below with environment classifier metadata if the scan profile changed.
                </p>
              </div>
              <div>
                <strong style={{ color: 'var(--text-primary)' }}>Insights row (DB)</strong>
                <p style={{ margin: '0.5rem 0 0' }}>
                  {meta?.insights
                    ? `objects_scanned=${meta.insights.objects_scanned ?? '—'}, total_classification=${meta.insights.total_classification ?? '—'}`
                    : 'No scan_insights row yet — click Sync or wait for tracker.'}
                </p>
              </div>
            </div>
          </section>

          <section className="glass-panel" style={{ padding: '1rem 1rem 1.25rem' }}>
            <h3 style={{ margin: '0 0 0.75rem', fontSize: '1.05rem', fontWeight: 700 }}>Progress over time</h3>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
              <div style={{ flex: '1 1 320px', minWidth: 280, height: 260 }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: 6 }}>Parts</div>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartPoints} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                    <XAxis dataKey="t" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line type="monotone" dataKey="partsTotal" name="Parts total" stroke="#2563eb" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="partsDone" name="Parts completed" stroke="#16a34a" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="partsFailed" name="Parts failed" stroke="#dc2626" dot={false} strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div style={{ flex: '1 1 320px', minWidth: 280, height: 260 }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: 6 }}>Objects</div>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartPoints} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                    <XAxis dataKey="t" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line type="monotone" dataKey="objects" name="Objects scanned" stroke="#7c3aed" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="objectsFailed" name="Objects failed" stroke="#ea580c" dot={false} strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div style={{ flex: '1 1 320px', minWidth: 280, height: 260 }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: 6 }}>Classifications</div>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartPoints} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                    <XAxis dataKey="t" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line type="monotone" dataKey="classTotal" name="classifications.total" stroke="#2563eb" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="owpi" name="objectsWithPi" stroke="#0d9488" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="newlyAdded" name="newlyAdded" stroke="#9333ea" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="falseRemoved" name="falseRemoved" stroke="#dc2626" dot={false} strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </section>

          <section className="glass-panel" style={{ padding: '1rem 1.15rem 1.25rem' }}>
            <h3 style={{ margin: '0 0 0.35rem', fontSize: '1.05rem', fontWeight: 700 }}>Classifier / attribute hits</h3>
            <p style={{ margin: '0 0 0.65rem', fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              Rows come from BigID classifier attributes stored in <code style={{ fontSize: '0.85em' }}>scan_insights</code> (Sync refreshes).
              “New findings” is the classifier hit count for this scan.
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center', marginBottom: '0.65rem' }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: '0.82rem', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={hitsFilterNonZero}
                  onChange={(e) => setHitsFilterNonZero(e.target.checked)}
                />
                Only show rows with New &gt; 0 or Removed &gt; 0
              </label>
              <button
                type="button"
                onClick={() => {
                  setHitsLive(true);
                }}
                disabled={hitsLoading}
                style={{
                  padding: '0.35rem 0.75rem',
                  borderRadius: 8,
                  border: '1px solid var(--surface-border)',
                  background: 'var(--surface-ground)',
                  cursor: hitsLoading ? 'wait' : 'pointer',
                  fontSize: '0.82rem',
                  fontWeight: 600,
                }}
              >
                Refresh from BigID (live)
              </button>
              {hitsData?.total_count_hint != null ? (
                <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                  API totalCount hint: <strong>{hitsData.total_count_hint.toLocaleString()}</strong>
                </span>
              ) : null}
              <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>Source: {hitsData?.source ?? '—'}</span>
            </div>
            {hitsData?.note ? (
              <p style={{ margin: '0 0 0.65rem', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{hitsData.note}</p>
            ) : null}
            {!hitsLoading && filteredClassifierRows.length === 0 && (hitsData?.fallback_classifier_names?.length ?? 0) > 0 ? (
              <div style={{ fontSize: '0.85rem', marginBottom: '0.75rem' }}>
                <strong style={{ color: 'var(--text-primary)' }}>Classifier names from insight summary</strong> (no per-attribute table yet)
                <ul style={{ margin: '0.35rem 0 0', paddingLeft: '1.2rem' }}>
                  {(hitsData?.fallback_classifier_names ?? []).slice(0, 40).map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {hitsLoading ? (
              <div style={{ padding: '1rem' }}>
                <Loader2 className="lucide-spin" size={22} />
              </div>
            ) : (
              <div style={{ overflowX: 'auto', maxHeight: 360 }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                  <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                      <th style={{ padding: '8px', textAlign: 'left' }}>Classifier / attribute</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>New findings</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>Removed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredClassifierRows.map((r, idx) => (
                      <tr key={`${idx}-${r.classifier}`} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                        <td style={{ padding: '7px 8px', fontFamily: 'ui-monospace, monospace', fontSize: '0.78rem' }}>{r.classifier}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right' }}>{r.new_findings.toLocaleString()}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right' }}>{r.removed.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!hitsLoading && filteredClassifierRows.length === 0 ? (
                  <p style={{ padding: '0.75rem', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                    No classifier rows yet. Use Sync now or Refresh from BigID (live).
                  </p>
                ) : null}
              </div>
            )}
          </section>

          <section className="glass-panel" style={{ padding: '1rem 1.1rem 1.25rem' }}>
            <h3 style={{ margin: '0 0 0.5rem', fontSize: '1.05rem', fontWeight: 700 }}>Scanner performance metrics</h3>
            {!scannerData?.available ? (
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
                {scannerData?.message ||
                  'Scanner samples unavailable — tracker must populate scanner tables for this environment.'}
              </p>
            ) : (
              <>
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: '0.65rem' }}>
                  Env #{scannerData.environment_id ?? '—'} · window {scannerData.window_start ?? '—'} → {scannerData.window_end ?? '—'} UTC ·
                  samples: groups {scannerData.summary?.group_samples ?? scannerData.groups?.length ?? 0}, pods{' '}
                  {scannerData.summary?.pod_samples ?? scannerData.individuals?.length ?? 0}. Speed in DB is B/s; charts use KiB/s where
                  applicable.
                </p>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: '0.75rem' }}>
                  {(['overview', 'groups', 'pods', 'raw'] as const).map((tab) => (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => setScannerTab(tab)}
                      style={{
                        padding: '0.35rem 0.75rem',
                        borderRadius: 8,
                        border: scannerTab === tab ? '2px solid var(--primary-color)' : '1px solid var(--surface-border)',
                        background: scannerTab === tab ? 'rgba(99,102,241,0.12)' : 'var(--surface-ground)',
                        fontWeight: 600,
                        fontSize: '0.82rem',
                        cursor: 'pointer',
                        textTransform: 'capitalize',
                      }}
                    >
                      {tab}
                    </button>
                  ))}
                </div>
                {scannerTab === 'overview' && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                    <div style={{ flex: '1 1 260px', minWidth: 240, height: 240 }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Latest snapshot — threads</div>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={overviewBars} margin={{ top: 4, right: 8, left: 0, bottom: 24 }}>
                          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                          <XAxis dataKey="name" tick={{ fontSize: 9 }} interval={0} angle={-18} textAnchor="end" height={52} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Bar dataKey="threads" fill="#2563eb" name="Threads" />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                    <div style={{ flex: '1 1 260px', minWidth: 240, height: 240 }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Latest snapshot — CPU %</div>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={overviewBars} margin={{ top: 4, right: 8, left: 0, bottom: 24 }}>
                          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                          <XAxis dataKey="name" tick={{ fontSize: 9 }} interval={0} angle={-18} textAnchor="end" height={52} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Bar dataKey="cpu" fill="#dc2626" name="CPU %" />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                    <div style={{ flex: '1 1 260px', minWidth: 240, height: 240 }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Latest snapshot — memory %</div>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={overviewBars} margin={{ top: 4, right: 8, left: 0, bottom: 24 }}>
                          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                          <XAxis dataKey="name" tick={{ fontSize: 9 }} interval={0} angle={-18} textAnchor="end" height={52} />
                          <YAxis tick={{ fontSize: 10 }} domain={[0, 100]} />
                          <Tooltip />
                          <Bar dataKey="mem" fill="#0d9488" name="Mem %" />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}
                {scannerTab === 'groups' && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                    <div style={{ flex: '1 1 320px', minWidth: 280, height: 260 }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Capacity vs throughput</div>
                      <ResponsiveContainer width="100%" height="100%">
                        <ComposedChart data={groupChartPoints} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                          <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                          <YAxis yAxisId="left" tick={{ fontSize: 10 }} />
                          <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Legend wrapperStyle={{ fontSize: 10 }} />
                          <Line yAxisId="left" type="monotone" dataKey="scanners" name="Running scanners" stroke="#2563eb" dot={false} />
                          <Line yAxisId="right" type="monotone" dataKey="kibs" name="KiB/s" stroke="#16a34a" dot={false} />
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                    <div style={{ flex: '1 1 320px', minWidth: 280, height: 260 }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Group memory (used MB)</div>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={groupChartPoints} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                          <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip />
                          <Area type="monotone" dataKey="memMb" stroke="#9333ea" fill="#c4b5fd" name="Used MB" />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}
                {scannerTab === 'pods' && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                    {podLinesByScanner.map((pl) => (
                      <div key={pl.id} style={{ flex: '1 1 300px', minWidth: 260, height: 220 }}>
                        <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>CPU % — {pl.id.slice(0, 18)}…</div>
                        <ResponsiveContainer width="100%" height="100%">
                          <LineChart data={pl.points} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                            <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                            <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                            <YAxis tick={{ fontSize: 10 }} />
                            <Tooltip />
                            <Line type="monotone" dataKey="cpu" stroke="#dc2626" dot={false} name="CPU %" />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    ))}
                    {!podLinesByScanner.length ? (
                      <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>No per-pod CPU series in this window.</p>
                    ) : null}
                  </div>
                )}
                {scannerTab === 'raw' && (
                  <pre
                    style={{
                      margin: 0,
                      fontSize: '0.68rem',
                      maxHeight: 280,
                      overflow: 'auto',
                      background: '#0f172a',
                      color: '#e2e8f0',
                      padding: '0.75rem',
                      borderRadius: 8,
                    }}
                  >
                    {JSON.stringify({ groups: scannerData.groups?.slice(-8), individuals: scannerData.individuals?.slice(-8) }, null, 2)}
                  </pre>
                )}
              </>
            )}
          </section>

          <section className="glass-panel" style={{ padding: '1rem 1.15rem' }}>
            <h3 style={{ margin: '0 0 0.65rem', fontSize: '1.05rem', fontWeight: 700 }}>AI log analysis & correlation</h3>
            {correlationData?.report ? (
              <div style={{ fontSize: '0.85rem', lineHeight: 1.6, whiteSpace: 'pre-wrap', marginBottom: '1rem' }}>
                {correlationData.report}
              </div>
            ) : (
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '0.75rem' }}>
                No correlation report stored for this scan yet (<code style={{ fontSize: '0.85em' }}>scan_log_correlations</code>).
              </p>
            )}
            <strong style={{ fontSize: '0.88rem' }}>Recent scanner / subsystem errors</strong>
            <div style={{ marginTop: '0.5rem', maxHeight: 260, overflow: 'auto' }}>
              {(scanErrorsData?.items ?? []).length === 0 ? (
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>No rows in scan_log_errors for this scan.</p>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.76rem' }}>
                  <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                      <th style={{ padding: '6px', textAlign: 'left' }}>Time</th>
                      <th style={{ padding: '6px', textAlign: 'left' }}>Source</th>
                      <th style={{ padding: '6px', textAlign: 'left' }}>Context</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(scanErrorsData?.items ?? []).map((e, idx) => (
                      <tr key={`${e.log_timestamp}-${idx}`} style={{ borderBottom: '1px solid var(--surface-border)', verticalAlign: 'top' }}>
                        <td style={{ padding: '6px', whiteSpace: 'nowrap' }}>{fmtCell(e.log_timestamp)}</td>
                        <td style={{ padding: '6px' }}>{fmtCell(e.log_source)}</td>
                        <td style={{ padding: '6px', wordBreak: 'break-word' }}>{(e.error_context || '').slice(0, 420)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          <details open className="glass-panel" style={{ padding: '0.75rem 1rem 1rem' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 700, fontSize: '1.05rem', marginBottom: '0.5rem' }}>
              Raw Progress Data ({histData?.returned ?? rows.length} of {histData?.total ?? rows.length} rows)
            </summary>
            <div style={{ overflowX: 'auto', maxHeight: 420, overflowY: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.72rem' }}>
                <thead style={{ position: 'sticky', top: 0, background: '#f1f5f9', zIndex: 1 }}>
                  <tr>
                    {[
                      'captured_at_utc',
                      'timestamp',
                      'scan_state',
                      'completed_parts',
                      'total_parts_scanned',
                      'objects_scanned',
                      'total_classifications',
                      'newly_added',
                      'false_removed',
                      'objects_failed',
                      'objects_with_pi',
                      'env_id',
                    ].map((h) => (
                      <th key={h} style={{ padding: '6px 8px', textAlign: 'left', borderBottom: '1px solid var(--surface-border)' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id ?? `${r.captured_at_utc}-${r.timestamp}`} style={{ borderBottom: '1px solid #e2e8f0' }}>
                      <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{fmtCell(r.captured_at_utc)}</td>
                      <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{fmtCell(r.timestamp)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.scan_state)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.completed_parts)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.total_parts_scanned)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_scanned)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.total_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.newly_added_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.false_removed_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_failed)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_with_pi)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.environment_id)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>

        </>
      )}
    </div>
  );
}

/** Old `/tools/scan-progress` URL — redirects to Scan Status (`/tools/scan-status`). */
export default function ScanProgressRedirect() {
  const location = useLocation();
  return <Navigate to={`/tools/scan-status${location.search}`} replace />;
}
