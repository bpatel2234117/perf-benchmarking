import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  Cell,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ComposedChart,
} from 'recharts';
import { AlertCircle, ArrowLeft, ChevronRight, FileDown, Loader2, TrendingUp } from 'lucide-react';
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
  completed_scan_parts: number | null;
  failed_scan_parts: number | null;
  total_scan_parts: number | null;
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
    objects_enumerated?: number | null;
    total_classification: number | null;
    objects_with_pi: number | null;
    distinct_classification_attributes: number | null;
  } | null;
  benchmark_context?: {
    file_set_details?: {
      total_files?: number | null;
      preset_table_snapshot?: { total_files?: number | null } | null;
    } | null;
    effective_volumes?: { total_files?: number | null } | null;
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
  prev_new?: number;
  prev_removed?: number;
}

interface ClassifierHitsResponse {
  scan_id: string;
  rows: ClassifierHitRow[];
  note: string;
  total_count_hint: number | null;
  fallback_classifier_names: string[];
  source: string;
  current_build?: string;
  previous_build?: string;
}

interface ClassifierSnapshotResponse {
  scan_id: string;
  current_build_version: string | null;
  previous_build_version: string | null;
  total_count: number;
  previous_total_count: number;
  added_count: number;
  removed_count: number;
  changed_count: number;
  added_names: string[];
  removed_names: string[];
  changed_names: string[];
  cached: boolean;
  error?: string;
  message?: string;
}

interface ServiceStatusSample {
  status: string;
  captured_at: string;
}

interface ServiceStatusRow {
  service_key: string;
  name: string;
  type: string;
  group: string;
  status: string;
  message: string;
  running_threads: number;
  total_threads: number;
  cpu_pct: number | null;
  update_date: string | null;
  created_at: string;
  created_at_is_exact: boolean;
  last_seen: string;
  changes: number;
  is_new: boolean;
  is_stale: boolean;
  not_reported: boolean;
  history: ServiceStatusSample[];
}

interface CaptureSlot {
  at: string;
  outcome: string;
  item_count: number;
  error: string;
}

interface ServicesStatusResponse {
  available: boolean;
  environment_id?: number | null;
  message: string;
  sample_minutes?: number;
  series?: {
    times: string[];
    groups: Record<string, (number | null)[]>;
  };
  capture?: {
    last_at: string | null;
    last_outcome: string;
    last_error: string;
    last_ok_at: string | null;
    is_lagging?: boolean;
    slots: CaptureSlot[];
  };
  services: ServiceStatusRow[];
}

/** Status → colour family. Anything BigID reports that we don't recognise is grey, not green. */
function serviceTone(status: string): 'good' | 'warn' | 'bad' | 'unknown' {
  const st = status.trim().toLowerCase();
  if (!st) return 'unknown';
  if (st === 'active' || st === 'running' || st === 'ok' || st === 'up') return 'good';
  if (st.includes('fail') || st.includes('down') || st.includes('error') || st.includes('dead')) return 'bad';
  if (st.includes('start') || st.includes('pend') || st.includes('restart') || st.includes('init') || st.includes('degrad') || st.includes('warn')) return 'warn';
  return 'unknown';
}

const TONE_STYLE: Record<'good' | 'warn' | 'bad' | 'unknown', { fg: string; bg: string }> = {
  good: { fg: '#15803d', bg: '#dcfce7' },
  warn: { fg: '#b45309', bg: '#fef3c7' },
  bad: { fg: '#b91c1c', bg: '#fee2e2' },
  unknown: { fg: '#475569', bg: '#e2e8f0' },
};

type ServiceGroup = {
  key: string;
  name: string;
  pods: ServiceStatusRow[];
  active: number;
  failed: number;
  other: number;
  gone: number;
  anyNew: boolean;
  timeline: (CaptureSlot & { active: number; failed: number; other: number })[];
};

/**
 * Pods of one BigID service grouped under one row. Counts use only pods that were in the
 * latest successful capture; the timeline is one column per capture attempt, so a failed
 * or empty capture shows as NO DATA instead of disappearing.
 */
/** Stable colour per service name, so a service keeps its line colour when others are toggled. */
function serviceColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i += 1) h = (h * 31 + name.charCodeAt(i)) % 360;
  return `hsl(${h}, 65%, 45%)`;
}

/** Active pods per service over the scan window. One line per service; checkboxes show or hide lines. */
function ActivePodsChart({ series }: { series?: ServicesStatusResponse['series'] }) {
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const names = useMemo(() => Object.keys(series?.groups ?? {}).sort(), [series]);
  const data = useMemo(
    () =>
      (series?.times ?? []).map((at, i) => {
        const row: Record<string, string | number | null> = { time: clockUtc(at) };
        for (const name of names) row[name] = series?.groups[name]?.[i] ?? null;
        return row;
      }),
    [series, names]
  );

  if (!series || names.length === 0 || data.length === 0) return null;

  const toggle = (name: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '0.78rem', marginBottom: 6 }}>
        <strong>Active pods</strong>
        <button type="button" onClick={() => setHidden(new Set())} style={{ fontSize: '0.75rem' }}>All</button>
        <button type="button" onClick={() => setHidden(new Set(names))} style={{ fontSize: '0.75rem' }}>None</button>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 12px', fontSize: '0.75rem', marginBottom: 6 }}>
        {names.map((name) => (
          <label key={name} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
            <input type="checkbox" checked={!hidden.has(name)} onChange={() => toggle(name)} />
            <span style={{ width: 10, height: 10, borderRadius: 2, background: serviceColor(name), display: 'inline-block' }} />
            {name}
          </label>
        ))}
      </div>
      <div style={{ width: '100%', height: 260 }}>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
            <XAxis dataKey="time" tick={{ fontSize: 11 }} minTickGap={24} />
            <YAxis allowDecimals={false} tick={{ fontSize: 11 }} label={{ value: 'Pods', angle: -90, position: 'insideLeft', fontSize: 11 }} />
            <Tooltip />
            {names.map((name) => (
              <Line
                key={name}
                type="stepAfter"
                dataKey={name}
                stroke={serviceColor(name)}
                hide={hidden.has(name)}
                connectNulls={false}
                dot={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function groupServices(services: ServiceStatusRow[], slots: CaptureSlot[]): ServiceGroup[] {
  const byKey = new Map<string, ServiceStatusRow[]>();
  for (const svc of services) {
    const key = svc.type || svc.name || svc.service_key;
    const arr = byKey.get(key) ?? [];
    arr.push(svc);
    byKey.set(key, arr);
  }
  const groups: ServiceGroup[] = [];
  for (const [key, pods] of byKey) {
    const live = pods.filter((p) => !p.not_reported);
    let active = 0;
    let failed = 0;
    let other = 0;
    const gone = pods.length - live.length;
    for (const pod of live) {
      const tone = serviceTone(pod.status);
      if (tone === 'good') active += 1;
      else if (tone === 'bad') failed += 1;
      else other += 1;
    }
    const timeline = slots.slice(-5).map((slot) => {
      const cur = { active: 0, failed: 0, other: 0 };
      for (const pod of pods) {
        const h = pod.history.find((x) => x.captured_at === slot.at);
        if (!h) continue;
        const t = serviceTone(h.status);
        if (t === 'good') cur.active += 1;
        else if (t === 'bad') cur.failed += 1;
        else cur.other += 1;
      }
      return { ...slot, ...cur };
    });
    groups.push({ key, name: key, pods, active, failed, other, gone, anyNew: live.some((p) => p.is_new), timeline });
  }
  // Failing services first, then by name.
  return groups.sort((a, b) => b.failed - a.failed || a.name.localeCompare(b.name));
}

/** "2026-10-08 16:00:00" (naive UTC from the API) → "16:00". */
function clockUtc(ts: string): string {
  return ts && ts.length >= 16 ? ts.slice(11, 16) : ts;
}

interface ScannerSeriesResponse {
  available: boolean;
  scan_id: string;
  environment_id: number | null;
  period_start?: string | null;
  period_end?: string | null;
  period_is_running?: boolean;
  window_start: string | null;
  window_end: string | null;
  window?: string | null;
  groups: Array<Record<string, unknown>>;
  individuals: Array<Record<string, unknown>>;
  message?: string;
  summary?: { group_samples: number; pod_samples: number };
}

interface ScanLogErrorsResponse {
  scan_id: string;
  period_start: string | null;
  period_end: string | null;
  period_is_running?: boolean;
  window_start?: string | null;
  window_end?: string | null;
  items: LogErrorRow[];
}

interface ScanPartRecord {
  id: number | null;
  subscan_id: string | null;
  state: string;
  record_key: string;
  captured_at_utc: string | null;
  payload: unknown;
}

interface ScanPartRecordsResponse {
  count: number;
  new_filter_count?: number;
  items: ScanPartRecord[];
  new_items?: ScanPartRecord[];
}

interface ScanPartsReportErrorsResponse {
  scan_id: string;
  available: boolean;
  error?: string;
  subscan_ids: string[];
  errors: Array<Record<string, string>>;
  error_count: number;
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
const SCAN_PART_DISPLAY_LIMIT = 5;
const SCAN_RECORD_DISPLAY_LIMIT = 15;

function fmtCell(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  if (typeof v === 'number' && Number.isNaN(v)) return '—';
  return String(v);
}

function payloadObject(payload: unknown): Record<string, unknown> {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    return payload as Record<string, unknown>;
  }
  return { value: payload };
}

function fmtPayloadValue(value: unknown): string {
  if (value == null || value === '') return '—';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function num(v: number | null | undefined): number {
  if (v == null || Number.isNaN(Number(v))) return 0;
  return Number(v);
}

function formatRowThroughput(row: ProgressHistoryRow, previousRow?: ProgressHistoryRow): string {
  if (!previousRow) return '—';
  const elapsedMinutes = (parseUtcMs(rowTime(row)) - parseUtcMs(rowTime(previousRow))) / 60000;
  const objectsDelta = num(row.objects_scanned) - num(previousRow.objects_scanned);
  if (!Number.isFinite(elapsedMinutes) || elapsedMinutes <= 0.5 || objectsDelta < 0) return '—';
  return `${(objectsDelta / elapsedMinutes).toFixed(1)} objects/min`;
}

/** Signed change in a cumulative counter: this row minus the previous (older) row; '—' for the first row. */
function formatRowDelta(
  row: ProgressHistoryRow,
  previousRow: ProgressHistoryRow | undefined,
  key: 'completed_parts' | 'objects_scanned',
): string {
  if (!previousRow) return '—';
  return (num(row[key]) - num(previousRow[key])).toLocaleString();
}

type ScannerPodRow = {
  captured_at_utc?: string;
  scanner_id?: string;
  name?: string;
  status?: string;
  running_threads?: number;
  cpu_pct?: number | null;
  memory_usage_pct?: number | null;
};

/** Match BigID Scanner Management: Active vs Failed (status field + thread/CPU fallback). */
function isScannerActive(row: ScannerPodRow): boolean {
  const st = String(row.status || '').trim().toLowerCase();
  if (st.includes('fail')) return false;
  if (st === 'active' || st === 'running') return true;
  const threads = Number(row.running_threads) || 0;
  const cpu = row.cpu_pct != null && !Number.isNaN(Number(row.cpu_pct)) ? Number(row.cpu_pct) : 0;
  return threads > 0 || cpu > 0;
}

function scannerDisplayName(row: ScannerPodRow): string {
  return String(row.name || row.scanner_id || '').trim();
}

function rowTime(r: ProgressHistoryRow): string {
  return (r.captured_at_utc || r.timestamp || '').trim();
}

function chartTickLabel(iso: string): string {
  if (!iso) return '';
  // DB timestamps are UTC but have no 'Z' suffix, so Date.parse treats them as local time.
  // Force UTC interpretation by appending 'Z' if no timezone is present.
  const normalized = /[Zz]$|[+-]\d{2}:\d{2}$/.test(iso) ? iso : iso + 'Z';
  const d = Date.parse(normalized);
  if (Number.isNaN(d)) return iso.slice(5, 16);
  return new Date(d).toISOString().slice(5, 16).replace('T', ' ');
}

/** DB timestamps are UTC but often lack a 'Z'; parse them as UTC, not browser-local. */
function parseUtcMs(s: string): number {
  const v = (s || '').trim();
  if (!v) return NaN;
  const norm = /[Zz]$|[+-]\d{2}:\d{2}$/.test(v) ? v : v + 'Z';
  return Date.parse(norm);
}

function formatDurationMinutes(minutes: number | null): string {
  if (minutes == null || !Number.isFinite(minutes) || minutes < 0) return '—';
  const rounded = Math.max(0, Math.round(minutes));
  const hours = Math.floor(rounded / 60);
  const mins = rounded % 60;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

function formatEtaUtc(timestampMs: number | null, remainingMinutes: number | null): string {
  if (timestampMs == null || remainingMinutes == null || !Number.isFinite(timestampMs)) return '—';
  return new Date(timestampMs + remainingMinutes * 60000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function formatTimestampUtc(timestampMs: number | null): string {
  if (timestampMs == null || !Number.isFinite(timestampMs)) return '—';
  return new Date(timestampMs).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function etaHealthColor(severity: number): string {
  const value = Math.max(0, Math.min(1, severity));
  const green = [22, 163, 74];
  const amber = [234, 179, 8];
  const red = [220, 38, 38];
  const from = value <= 0.5 ? green : amber;
  const to = value <= 0.5 ? amber : red;
  const t = value <= 0.5 ? value * 2 : (value - 0.5) * 2;
  const rgb = from.map((channel, index) => Math.round(channel + (to[index] - channel) * t));
  return `rgb(${rgb.join(', ')})`;
}

/**
 * Detect a genuine STATUS-REPORTING lag: the scan's work was already done (objects
 * settled) but BigID didn't flip to a terminal state until much later.
 *
 * NOTE: parts (enumeration) reaching 100% BEFORE objects finish scanning is normal
 * pipelining, not a lag. So we only flag when objects_scanned was essentially unchanged
 * between the 100%-parts mark and the terminal state; if objects kept growing, the
 * 100%-parts mark was simply premature and we report nothing.
 */
function computeCompletionLag(rows: ProgressHistoryRow[]): string | null {
  if (rows.length < 2) return null;
  let hit100: { t: number; objs: number } | null = null;
  const terminalRe = /completed|failed|stopped|aborted|cancel/i;
  for (const r of rows) {
    const tp = num(r.total_parts_scanned);
    const cp = num(r.completed_parts);
    const tt = parseUtcMs(rowTime(r));
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
      const tt = parseUtcMs(rowTime(r));
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
  // Objects still being scanned after parts hit 100% → premature mark (normal), not a lag.
  const finalObjs = Math.max(lastTerminal.objs, hit100.objs);
  if (objDelta > Math.max(1, 0.01 * finalObjs)) return null;
  const t100 = new Date(hit100.t).toISOString().slice(0, 16).replace('T', ' ');
  const tEnd = new Date(lastTerminal.t).toISOString().slice(0, 16).replace('T', ' ');
  return `Scan work finished at ${t100} UTC but BigID did not report terminal state (${lastTerminal.state}) until ${tEnd} UTC (status-reporting lag ≈ ${lagH.toFixed(1)} h; objects scanned unchanged).`;
}

function buildScanProgressMarkdown(params: {
  scanId: string;
  meta: ScanDetailMeta | undefined;
  analysis: {
    objects: number;
    failedParts: number;
    falseRemovedDelta: number;
    classDelta: number;
    etaUtc: string;
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
    lines.push(`- Estimated completion: **${analysis.etaUtc}**`);
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

/** Panel whose body is hidden until expanded; closed by default. Header actions stay clickable without toggling. */
function CollapsibleSection({
  title,
  headerRight,
  children,
}: {
  title: ReactNode;
  headerRight?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const summaryStyle: CSSProperties = {
    cursor: 'pointer',
    listStyle: 'none',
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '0.75rem',
    padding: '1rem 1.15rem',
  };
  return (
    <section className="glass-panel" style={{ padding: 0 }}>
      <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary style={summaryStyle}>
          <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-primary)' }}>
            <ChevronRight size={18} style={{ flexShrink: 0, transition: 'transform 0.15s', transform: open ? 'rotate(90deg)' : 'none' }} />
            {title}
          </span>
          {headerRight ? (
            <span onClick={(e) => e.preventDefault()} style={{ display: 'flex', alignItems: 'center', gap: '0.65rem' }}>
              {headerRight}
            </span>
          ) : null}
        </summary>
        <div style={{ padding: '0 1.15rem 1.25rem' }}>{children}</div>
      </details>
    </section>
  );
}

/** Full scan detail when `?scan_id=` is present (used by merged Scan Status page). */
export function ScanProgressDetail({ scanId, token }: { scanId: string; token: string }) {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [hitsLive, setHitsLive] = useState(false);
  const [hitsFilterNonZero, setHitsFilterNonZero] = useState(true);
  const [scannerTab, setScannerTab] = useState<'overview' | 'groups' | 'pods' | 'raw'>('overview');
  const [showFailedScanners, setShowFailedScanners] = useState(false);
  const autoSyncAttemptsRef = useRef(0);
  const autoReportTriggeredRef = useRef(false);

  useEffect(() => {
    setScannerTab('overview');
    setShowFailedScanners(false);
    setHitsLive(false);
    setHitsFilterNonZero(true);
    autoSyncAttemptsRef.current = 0;
    autoReportTriggeredRef.current = false;
  }, [scanId]);

  const { data: meta, isLoading: metaLoading, error: metaError } = useQuery({
    queryKey: ['scanProgressDetail', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/detail`, authConfig);
      return res.data as ScanDetailMeta;
    },
    refetchInterval: (query) => {
      const snap = query.state.data?.latest_snapshot_this_scan;
      return snap ? 30000 : 8000;
    },
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
    refetchInterval: (query) => {
      const n = query.state.data?.items?.length ?? 0;
      return n > 0 ? 20000 : 5000;
    },
  });

  const { data: subscanData } = useQuery({
    queryKey: ['scanSubscanIds', token, scanId],
    enabled: Boolean(meta),
    queryFn: async () => {
      const res = await axios.get<{ scan_id: string; count: number; subscan_ids: string[] }>(
        `${API_BASE}/api/scan/${encodeURIComponent(scanId)}/subscan-ids`, authConfig);
      return res.data;
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  const { data: scanPartsData, isLoading: scanPartsLoading } = useQuery({
    queryKey: ['scanPartRecords', token, scanId],
    enabled: Boolean(meta),
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/scan-parts`, {
        ...authConfig,
        params: { limit: SCAN_PART_DISPLAY_LIMIT },
      });
      return res.data as ScanPartRecordsResponse;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const { data: scanRecordsData, isLoading: scanRecordsLoading } = useQuery({
    queryKey: ['scanCollectionRecords', token, scanId],
    enabled: Boolean(meta),
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/scan-records`, {
        ...authConfig,
        params: { limit: SCAN_RECORD_DISPLAY_LIMIT },
      });
      return res.data as ScanPartRecordsResponse;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
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

  const { data: clsSnapshot } = useQuery({
    queryKey: ['scanClassifierSnapshot', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/classifier-snapshot`, authConfig);
      return res.data as ClassifierSnapshotResponse;
    },
    retry: 1,
  });

  const { data: scannerData } = useQuery({
    queryKey: ['scanScannerSeries', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/scanner-series`, authConfig);
      return res.data as ScannerSeriesResponse;
    },
    refetchInterval: 60000,
  });

  // Live BigID services-status for this scan's environment: every service, not only scanners.
  const { data: servicesData, isLoading: servicesLoading } = useQuery({
    queryKey: ['scanServicesStatus', token, scanId],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/scan/${encodeURIComponent(scanId)}/services-status`, authConfig);
      return res.data as ServicesStatusResponse;
    },
    refetchInterval: 60000,
  });

  const [expandedServiceGroups, setExpandedServiceGroups] = useState<Set<string>>(() => new Set());
  const serviceGroups = useMemo(
    () => groupServices(servicesData?.services ?? [], servicesData?.capture?.slots ?? []),
    [servicesData]
  );

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
      return res.data as ScanLogErrorsResponse;
    },
    refetchInterval: 60000,
  });

  const reportErrorsMutation = useMutation({
    mutationFn: async () => {
      const res = await axios.get<ScanPartsReportErrorsResponse>(
        `${API_BASE}/api/scan/${encodeURIComponent(scanId)}/scan-parts-report-errors`,
        authConfig
      );
      return res.data;
    },
  });

  useEffect(() => {
    if (metaLoading || !meta?.parent) return;
    const status = (meta.parent.scan_status || '').trim();
    if (status.toLowerCase() !== 'completedwithfailures') return;
    if (autoReportTriggeredRef.current) return;
    if (reportErrorsMutation.isPending || reportErrorsMutation.data) return;
    autoReportTriggeredRef.current = true;
    reportErrorsMutation.mutate();
  }, [
    metaLoading,
    meta?.parent?.scan_status,
    reportErrorsMutation,
    reportErrorsMutation.isPending,
    reportErrorsMutation.data,
  ]);

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
      queryClient.invalidateQueries({ queryKey: ['scanPartRecords', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanCollectionRecords', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanClassifierHits'] });
      queryClient.invalidateQueries({ queryKey: ['scanScannerSeries', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanLogCorrelation', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanLogErrors', token, scanId] });
      queryClient.invalidateQueries({ queryKey: ['scanUnified'] });
      queryClient.invalidateQueries({ queryKey: ['scanProgressLatest'] });
      setHitsLive(false);
    },
  });

  const collectionRefreshMutation = useMutation({
    mutationFn: async (collection: 'scan_parts' | 'scans' | 'all') => {
      const res = await axios.post<{ ok: boolean; collection?: string; count?: number }>(
        `${API_BASE}/api/scan/${encodeURIComponent(scanId)}/refresh-collection`,
        { collection },
        authConfig
      );
      return res.data;
    },
    onSuccess: (_data, collection) => {
      if (collection === 'all' || collection === 'scan_parts') {
        queryClient.invalidateQueries({ queryKey: ['scanPartRecords', token, scanId] });
      }
      if (collection === 'all' || collection === 'scans') {
        queryClient.invalidateQueries({ queryKey: ['scanCollectionRecords', token, scanId] });
      }
    },
  });

  const rows = histData?.items ?? [];
  const resolvedEnvId = meta?.resolved_environment_id ?? meta?.parent?.environment_id ?? null;
  const scanRecordFields = useMemo(() => {
    const fields = new Set<string>();
    for (const record of scanRecordsData?.items ?? []) {
      Object.keys(payloadObject(record.payload)).forEach((field) => fields.add(field));
    }
    return Array.from(fields).sort((a, b) => a.localeCompare(b));
  }, [scanRecordsData]);

  const newScanParts = useMemo(() => {
    if (Array.isArray(scanPartsData?.new_items)) return scanPartsData.new_items.slice(0, SCAN_PART_DISPLAY_LIMIT);
    return (scanPartsData?.items ?? [])
      .filter((part) => part.state.trim().toLowerCase() === 'new')
      .slice(0, SCAN_PART_DISPLAY_LIMIT);
  }, [scanPartsData]);
  const allScanParts = (scanPartsData?.items ?? []).slice(0, SCAN_PART_DISPLAY_LIMIT);

  useEffect(() => {
    if (metaLoading || meta?.manual_sync_disabled || resolvedEnvId == null) return;
    if (rows.length > 0) return;
    if (syncMutation.isPending) return;
    if (autoSyncAttemptsRef.current >= 8) return;
    const delayMs = autoSyncAttemptsRef.current === 0 ? 400 : 6000;
    const t = window.setTimeout(() => {
      autoSyncAttemptsRef.current += 1;
      syncMutation.mutate();
    }, delayMs);
    return () => window.clearTimeout(t);
  }, [
    metaLoading,
    meta?.manual_sync_disabled,
    rows.length,
    resolvedEnvId,
    scanId,
    syncMutation.isPending,
  ]);

  const chartPoints = useMemo(() => {
    return rows
      .filter((r) => {
        // Skip the start-marker row (scan_state='Started', all counters zero) so it
        // doesn't pollute the first tooltip with all-zero values.
        if ((r.scan_state || '').toLowerCase() !== 'started') return true;
        return (
          num(r.objects_scanned) > 0 ||
          num(r.total_parts_scanned) > 0 ||
          num(r.total_classifications) > 0
        );
      })
      .map((r) => {
        const tlab = chartTickLabel(rowTime(r));
        return {
          t: tlab,
          partsTotal: num(r.total_parts_scanned),
          partsDone: num(r.completed_parts),
          partsFailed: num(r.failed_parts),
          scanPartsTotal: num(r.total_scan_parts),
          scanPartsDone: num(r.completed_scan_parts),
          scanPartsFailed: num(r.failed_scan_parts),
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
    const fp = num(last.failed_parts);
    const classDelta = num(last.total_classifications) - num(first.total_classifications);
    const t0 = parseUtcMs(rowTime(first));
    const t1 = parseUtcMs(rowTime(last));
    let throughput = '';
    let objectRate = 0;
    let initialRate = 0;
    let recentRate = 0;
    const samples = rows
      .map((row) => ({ time: parseUtcMs(rowTime(row)), objects: num(row.objects_scanned) }))
      .filter((sample) => !Number.isNaN(sample.time))
      .sort((a, b) => a.time - b.time);
    // BigID can briefly return a lower cumulative counter on a later snapshot.
    // Do not let that 5-minute sample create a negative rate or move the current
    // progress backwards; retain only the monotonic samples for ETA calculations.
    const monotonicSamples = samples.reduce<typeof samples>((valid, sample) => {
      const previous = valid[valid.length - 1];
      if (!previous || sample.objects >= previous.objects) valid.push(sample);
      return valid;
    }, []);
    const rateBetween = (older: { time: number; objects: number }, newer: { time: number; objects: number }) => {
      const minutes = (newer.time - older.time) / 60000;
      const delta = newer.objects - older.objects;
      return minutes > 0.5 && delta >= 0 ? delta / minutes : 0;
    };
    if (monotonicSamples.length >= 2) {
      initialRate = rateBetween(monotonicSamples[0], monotonicSamples[1]);
      recentRate = rateBetween(monotonicSamples[monotonicSamples.length - 2], monotonicSamples[monotonicSamples.length - 1]);
    }
    const firstSample = monotonicSamples[0];
    const latestSample = monotonicSamples[monotonicSamples.length - 1];
    const rateStart = firstSample?.time ?? t0;
    const rateEnd = latestSample?.time ?? t1;
    const currentObjects = latestSample?.objects ?? num(last.objects_scanned);
    if (rateStart != null && rateEnd != null && rateEnd > rateStart) {
      const mins = (rateEnd - rateStart) / 60000;
      const objDelta = currentObjects - (firstSample?.objects ?? num(first.objects_scanned));
      if (mins > 0.5 && objDelta >= 0) {
        objectRate = objDelta / mins;
        throughput = `${objectRate.toFixed(1)} objects/min (first→last snapshot)`;
      }
    }
    if (!initialRate) initialRate = objectRate;
    if (!recentRate) recentRate = objectRate;
    let scanDuration = '';
    const durMs = !Number.isNaN(t0) && !Number.isNaN(t1) ? t1 - t0 : 0;
    if (durMs > 0) {
      const h = Math.floor(durMs / 3600000);
      const m = Math.floor((durMs % 3600000) / 60000);
      scanDuration = `${h}h ${m}m`;
    }
    const presetTarget = num(meta?.benchmark_context?.file_set_details?.preset_table_snapshot?.total_files);
    const enumeratedTarget = num(meta?.insights?.objects_enumerated);
    const targetObjects = presetTarget > 0 ? presetTarget : enumeratedTarget;
    const objectPct = targetObjects > 0 ? Math.min(100, (currentObjects / targetObjects) * 100) : null;
    const estimatedTotalMinutes = objectRate > 0 && targetObjects > 0 ? targetObjects / objectRate : null;
    const remainingMinutes = objectRate > 0 && targetObjects > 0
      ? Math.max(0, targetObjects - currentObjects) / objectRate
      : null;
    // The tracker may run every five minutes after the near-complete threshold. Use the
    // latest valid interval for remaining time, but never use a regressive cumulative row.
    const etaRate = recentRate > 0 ? recentRate : objectRate;
    const currentRemainingMinutes = etaRate > 0 && targetObjects > 0
      ? Math.max(0, targetObjects - currentObjects) / etaRate
      : remainingMinutes;
    const initialEtaMs = initialRate > 0 && targetObjects > 0 && !Number.isNaN(t0)
      ? t0 + (targetObjects / initialRate) * 60000
      : null;
    const currentEtaMs = etaRate > 0 && targetObjects > 0 && rateEnd != null
      ? rateEnd + currentRemainingMinutes! * 60000
      : null;
    const parentCompletionMs = parseUtcMs(String(meta?.parent?.completed_at || ''));
    const terminalState = /completed|failed|stopped|aborted|cancel/i.test(String(meta?.parent?.scan_status || last.scan_state || ''));
    const completedAtMs = terminalState
      ? (!Number.isNaN(parentCompletionMs) ? parentCompletionMs : t1)
      : null;
    let etaSeverity = 0;
    if (initialEtaMs != null && completedAtMs != null) {
      const deviationMinutes = Math.abs(completedAtMs - initialEtaMs) / 60000;
      etaSeverity = deviationMinutes <= 20 ? 0 : Math.min(1, (deviationMinutes - 20) / 20);
    } else if (initialEtaMs != null && currentEtaMs != null) {
      const delayMinutes = (currentEtaMs - initialEtaMs) / 60000;
      etaSeverity = delayMinutes > 0 ? Math.min(1, delayMinutes / 20) : 0;
      if (recentRate <= 0) etaSeverity = 1;
      else if (initialRate > 0 && recentRate < initialRate) {
        etaSeverity = Math.max(etaSeverity, Math.min(1, (initialRate - recentRate) / initialRate));
      }
    }
    return {
      last,
      scannedParts: num(last.completed_scan_parts),
      failedParts: fp,
      falseRemovedDelta: num(last.false_removed_classifications) - num(first.false_removed_classifications),
      classDelta,
      throughput,
      scanDuration,
      objects: currentObjects,
      targetObjects: targetObjects > 0 ? targetObjects : null,
      objectPct,
      estimatedTotalMinutes,
      remainingMinutes: currentRemainingMinutes,
      etaUtc: completedAtMs != null ? formatTimestampUtc(completedAtMs) : formatEtaUtc(Number.isNaN(t1) ? null : t1, currentRemainingMinutes),
      etaColor: etaHealthColor(etaSeverity),
    };
  }, [rows, meta]);

  const lagText = useMemo(() => computeCompletionLag(rows), [rows]);

  const latestPodsSnapshot = useMemo(() => {
    const ind = (scannerData?.individuals ?? []) as ScannerPodRow[];
    const byKey = new Map<string, ScannerPodRow>();
    for (const row of ind) {
      const key = scannerDisplayName(row);
      if (!key) continue;
      const prev = byKey.get(key);
      // Compare chronologically, not lexically: mixed/blank/ISO timestamp formats can
      // sort wrong as strings and let an older snapshot win. Unparseable timestamps sort oldest.
      const tNew = Date.parse(String(row.captured_at_utc || ''));
      const tPrev = Date.parse(String(prev?.captured_at_utc || ''));
      const tNewMs = Number.isNaN(tNew) ? -Infinity : tNew;
      const tPrevMs = Number.isNaN(tPrev) ? -Infinity : tPrev;
      if (!prev || tNewMs >= tPrevMs) byKey.set(key, row);
    }
    return [...byKey.values()].map((row) => ({
      name: scannerDisplayName(row),
      threads: Number(row.running_threads) || 0,
      cpu: row.cpu_pct != null ? Number(row.cpu_pct) : 0,
      mem: row.memory_usage_pct != null ? Number(row.memory_usage_pct) : 0,
      isActive: isScannerActive(row),
      status: String(row.status || ''),
    }));
  }, [scannerData?.individuals]);

  const latestReportedActiveCount = useMemo(() => {
    const groups = (scannerData?.groups ?? []) as Array<Record<string, unknown>>;
    if (!groups.length) return null;
    const times = groups.map((g) => Date.parse(String(g.captured_at_utc || ''))).filter((t) => !Number.isNaN(t));
    if (!times.length) return null;
    const latestTime = Math.max(...times);
    const latestGroups = groups.filter((g) => Date.parse(String(g.captured_at_utc || '')) === latestTime);
    const count = latestGroups.reduce((sum, g) => sum + (Number(g.running_scanners) || 0), 0);
    return count > 0 ? count : null;
  }, [scannerData?.groups]);

  const visibleLatestPods = useMemo(() => {
    if (showFailedScanners) return latestPodsSnapshot;
    const activePods = latestPodsSnapshot.filter((p) => p.isActive);
    const activeCount = latestReportedActiveCount == null
      ? activePods.length
      : Math.min(latestReportedActiveCount, activePods.length);
    return activePods.slice(0, activeCount);
  }, [latestPodsSnapshot, latestReportedActiveCount, showFailedScanners]);

  const filteredClassifierRows = useMemo(() => {
    const raw = hitsData?.rows ?? [];
    if (!hitsFilterNonZero) return raw;
    return raw.filter((r) => r.new_findings > 0 || r.removed > 0);
  }, [hitsData?.rows, hitsFilterNonZero]);

  // Full time-series pivoted wide: one row per time tick, one field per scanner
  const overviewTimeSeries = useMemo(() => {
    const ind = scannerData?.individuals;
    type IRow = { captured_at_utc?: string; name?: string; scanner_id?: string; cpu_pct?: number | null; memory_usage_pct?: number };
    if (ind && Array.isArray(ind) && ind.length > 0) {
      const scannerNames = visibleLatestPods.map((p) => p.name);
      const byTime = new Map<string, Record<string, number | null | string>>();
      for (const r of ind as IRow[]) {
        const sname = String(r.name || r.scanner_id || '').trim();
        if (!scannerNames.includes(sname)) continue;
        const t = chartTickLabel(String(r.captured_at_utc || ''));
        if (!byTime.has(t)) byTime.set(t, { t });
        const row = byTime.get(t)!;
        row[`cpu__${sname}`] = r.cpu_pct != null ? Math.round(Number(r.cpu_pct) * 10) / 10 : null;
        row[`mem__${sname}`] = r.memory_usage_pct != null ? Math.round(Number(r.memory_usage_pct)) : null;
      }
      let points = [...byTime.values()].sort((a, b) => String(a.t).localeCompare(String(b.t)));
      return { points, scannerNames, fullPoints: points, mode: 'pods' as const, activeByName: Object.fromEntries(visibleLatestPods.map((p) => [p.name, p.isActive])) };
    }
    // Fallback: aggregate group snapshots when per-pod series is missing
    const g = scannerData?.groups;
    if (!g || !Array.isArray(g) || g.length === 0) return { points: [], scannerNames: [] as string[], mode: 'none' as const };
    type GRow = {
      captured_at_utc?: string;
      group_name?: string;
      running_scanners?: number;
      avg_scan_speed_kib_s?: number;
      used_memory_mb?: number;
      physical_memory_mb?: number;
    };
    const byTime = new Map<string, Record<string, number | null | string>>();
    for (const row of g as GRow[]) {
      const t = chartTickLabel(String(row.captured_at_utc || ''));
      if (!t) continue;
      if (!byTime.has(t)) byTime.set(t, { t, scanners: 0, kibs: 0, memPct: 0, n: 0 });
      const agg = byTime.get(t)!;
      const phy = Number(row.physical_memory_mb) || 0;
      const used = Number(row.used_memory_mb) || 0;
      agg.scanners = (Number(agg.scanners) || 0) + (Number(row.running_scanners) || 0);
      agg.kibs = (Number(agg.kibs) || 0) + (Number(row.avg_scan_speed_kib_s) || 0);
      if (phy > 0) agg.memPct = Math.round(((Number(agg.memPct) || 0) * Number(agg.n) + (used / phy) * 100) / (Number(agg.n) + 1));
      agg.n = Number(agg.n) + 1;
    }
    let points = [...byTime.values()].sort((a, b) => String(a.t).localeCompare(String(b.t)));
    return { points, scannerNames: [] as string[], fullPoints: points, mode: 'groups' as const, activeByName: {} as Record<string, boolean> };
  }, [scannerData, visibleLatestPods]);

  const groupChartPoints = useMemo(() => {
    const g = scannerData?.groups;
    if (!g || !Array.isArray(g)) return [];
    type GRow = {
      captured_at_utc?: string;
      running_scanners?: number;
      running_threads?: number;
      avg_scan_speed_kib_s?: number;
      used_memory_mb?: number;
      physical_memory_mb?: number;
      cpu_pct?: number;
    };
    const byTime = new Map<
      string,
      { t: string; scanners: number; threads: number; kibs: number; memPct: number; memWeight: number; cpuPct: number; cpuN: number }
    >();
    for (const row of g as GRow[]) {
      const t = chartTickLabel(String(row.captured_at_utc || ''));
      if (!t) continue;
      const used = Number(row.used_memory_mb) || 0;
      const phy = Number(row.physical_memory_mb) || 0;
      const memPct = phy > 0 ? (used / phy) * 100 : 0;
      const cur =
        byTime.get(t) || { t, scanners: 0, threads: 0, kibs: 0, memPct: 0, memWeight: 0, cpuPct: 0, cpuN: 0 };
      cur.scanners += Number(row.running_scanners) || 0;
      cur.threads += Number(row.running_threads) || 0;
      cur.kibs += Number(row.avg_scan_speed_kib_s) || 0;
      if (memPct > 0) {
        cur.memPct += memPct;
        cur.memWeight += 1;
      }
      const cpu = Number(row.cpu_pct) || 0;
      if (cpu > 0) {
        cur.cpuPct += cpu;
        cur.cpuN += 1;
      }
      byTime.set(t, cur);
    }
    const base = [...byTime.values()]
      .sort((a, b) => a.t.localeCompare(b.t))
      .map((row) => ({
        t: row.t,
        scanners: row.scanners,
        threads: row.threads,
        kibs: row.kibs,
        memPct: row.memWeight > 0 ? Math.round(row.memPct / row.memWeight) : 0,
        cpuPct: row.cpuN > 0 ? Math.round(row.cpuPct / row.cpuN) : 0,
      }));
    const allCpuZero = base.length > 0 && base.every((r) => !r.cpuPct);
    const ind = scannerData?.individuals;
    if (allCpuZero && ind && Array.isArray(ind) && ind.length > 0) {
      const cpuByT = new Map<string, number[]>();
      for (const row of ind as Array<{ captured_at_utc?: string; cpu_pct?: number | null }>) {
        const t = chartTickLabel(String(row.captured_at_utc || ''));
        const cpu = row.cpu_pct != null ? Number(row.cpu_pct) : NaN;
        if (!t || Number.isNaN(cpu)) continue;
        const arr = cpuByT.get(t) || [];
        arr.push(cpu);
        cpuByT.set(t, arr);
      }
      return base.map((row) => {
        const cpus = cpuByT.get(row.t) || [];
        const cpuPct =
          cpus.length > 0 ? Math.round(cpus.reduce((a, b) => a + b, 0) / cpus.length) : row.cpuPct;
        return { ...row, cpuPct };
      });
    }
    return base;
  }, [scannerData]);

  const podLinesByScanner = useMemo(() => {
    const ind = scannerData?.individuals;
    if (!ind || !Array.isArray(ind)) return [];
    const byScanner = new Map<string, ScannerPodRow[]>();
    for (const row of ind as ScannerPodRow[]) {
      const key = scannerDisplayName(row);
      if (!key) continue;
      const arr = byScanner.get(key) || [];
      arr.push(row);
      byScanner.set(key, arr);
    }
    const allowed = new Set(visibleLatestPods.map((p) => p.name));
    const top = [...byScanner.entries()]
      .filter(([id]) => allowed.has(id))
      .sort((a, b) => b[1].length - a[1].length)
      .slice(0, 12)
      .map(([id]) => id);
    return top.map((id) => {
      const pts = (byScanner.get(id) || [])
        .slice()
        .sort((a, b) => String(a.captured_at_utc).localeCompare(String(b.captured_at_utc)));
      const label = String(pts[0]?.name || id).slice(0, 22);
      return {
        id,
        label,
        points: pts.map((p) => ({
          t: chartTickLabel(String(p.captured_at_utc || '')),
          cpu: p.cpu_pct != null ? Number(p.cpu_pct) : null,
          memPct: p.memory_usage_pct != null ? Math.round(Number(p.memory_usage_pct)) : 0,
        })),
      };
    });
  }, [scannerData, visibleLatestPods]);

  const parent = meta?.parent;
  const displayTitle =
    (parent?.scan_name && String(parent.scan_name).trim()) || parent?.scan_name || '';
  // Manual sync is NOT gated on resolvedEnvId: /api/scan/sync-insights resolves the
  // environment server-side from scan_parents OR workflow_steps (bootstrap_scan_tracking).
  // Gating on resolvedEnvId here stranded workflow scans that only exist in workflow_steps
  // (no scan_parents row yet) — the button was disabled, so the page could never self-heal.
  // If there is genuinely no env anywhere, the sync fails with a clear error instead.
  const syncDisabled = syncMutation.isPending;
  const collectionRefreshBusy = (collection: 'scan_parts' | 'scans') => (
    collectionRefreshMutation.isPending && (
      collectionRefreshMutation.variables === collection || collectionRefreshMutation.variables === 'all'
    )
  );
  const collectionRefreshDisabled = (collection: 'scan_parts' | 'scans') => (
    syncMutation.isPending || collectionRefreshBusy(collection)
  );
  const renderScanPartTable = (parts: ScanPartRecord[], accent: string) => (
    parts.length ? (
      <div style={{ maxHeight: 420, overflowY: 'scroll', overflowX: 'hidden', paddingRight: 6, color: accent }}>
        {parts.map((part, index) => {
          const payload = payloadObject(part.payload);
          const fields = Object.keys(payload).sort((a, b) => a.localeCompare(b));
          if (fields.length === 0) fields.push('Payload');
          const documentName = String(payload._id || payload.id || part.record_key || `Document ${index + 1}`);
          return (
            <details
              key={`${part.id ?? part.record_key}`}
              style={{ border: '1px solid var(--surface-border)', borderRadius: 6, marginBottom: 8, overflow: 'hidden' }}
            >
              <summary style={{ cursor: 'pointer', padding: '8px', background: '#f1f5f9', fontSize: '0.78rem', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                Document {index + 1} · {documentName}
              </summary>
              <div style={{ maxHeight: 360, overflowY: 'scroll', overflowX: 'hidden' }}>
                {fields.map((field) => (
                  <div
                    key={field}
                    style={{
                      display: 'grid',
                      gridTemplateColumns: '260px minmax(0, 1fr)',
                      borderBottom: '1px solid var(--surface-border)',
                      fontSize: '0.78rem',
                    }}
                  >
                    <strong style={{ padding: '7px', background: '#f1f5f9', whiteSpace: 'nowrap' }}>{field}</strong>
                    <div style={{ padding: '7px', minWidth: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                      {fields.length === 1 && field === 'Payload' ? '{}' : fmtPayloadValue(payload[field])}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          );
        })}
      </div>
    ) : (
      <pre style={{ margin: 0, padding: '0.8rem', color: '#15803d', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 6, fontSize: '0.8rem' }}>
        {'{}'}
      </pre>
    )
  );

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
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
                gap: '0.75rem 1.5rem',
                marginTop: '0.75rem',
                fontSize: '0.88rem',
              }}
            >
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>Scan</div>
                <div style={{ color: 'var(--text-primary)', fontWeight: 600, marginTop: 2 }}>{displayTitle || '—'}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>Parent scan ID</div>
                <div style={{ color: 'var(--text-primary)', fontFamily: 'ui-monospace, monospace', marginTop: 2 }}>{scanId}</div>
              </div>
              {meta?.environment_name ? (
                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>Environment</div>
                  <div style={{ color: 'var(--text-primary)', marginTop: 2 }}>
                    {meta.environment_name}
                    {meta.resolved_environment_id != null ? ` (#${meta.resolved_environment_id})` : ''}
                  </div>
                </div>
              ) : null}
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>
                  Sub-scan IDs ({subscanData?.count ?? 0})
                </div>
                <div style={{ color: 'var(--text-primary)', fontFamily: 'ui-monospace, monospace', marginTop: 2, wordBreak: 'break-all' }}>
                  {subscanData?.subscan_ids?.length ? subscanData.subscan_ids.join(', ') : '—'}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>Final status</div>
                <div style={{ color: 'var(--text-primary)', fontWeight: 700, marginTop: 2 }}>
                  {parent?.scan_status || rows[rows.length - 1]?.scan_state || 'In progress'}
                </div>
              </div>
            </div>
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
            {syncMutation.isPending ? 'Fetching live progress…' : 'Sync now (manual)'}
          </button>
          {syncMutation.isPending || (rows.length === 0 && resolvedEnvId != null && !meta?.manual_sync_disabled) ? (
            <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              {syncMutation.isPending
                ? 'Pulling parent scan + insights from BigID…'
                : 'Waiting for first snapshot — auto-syncing from BigID.'}
            </span>
          ) : null}
          {resolvedEnvId == null && !metaLoading ? (
            <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              No environment linked yet — open from Scan Status after the workflow assigns env, or re-sync parents.
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
        {collectionRefreshMutation.isError && (
          <div className="glass-panel" style={{ padding: '0.75rem 1rem', color: 'var(--danger-color)', fontSize: '0.88rem' }}>
            Collection refresh failed:{' '}
            {axios.isAxiosError(collectionRefreshMutation.error)
              ? String((collectionRefreshMutation.error.response?.data as { detail?: string })?.detail ?? collectionRefreshMutation.error.message)
              : String(collectionRefreshMutation.error)}
          </div>
        )}
      </section>

      {!loading && meta && (
        <div className="glass-panel" style={{ padding: '0.55rem 1.15rem', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0 1.5rem', alignItems: 'center' }}>
            <span>
              Snapshot (this scan):{' '}
              <strong style={{ color: 'var(--text-primary)' }}>
                {meta.latest_snapshot_this_scan ?? 'none yet'}
              </strong>
            </span>
            {meta.latest_snapshot_global && (
              <span>
                Latest (any):{' '}
                <strong style={{ color: 'var(--text-primary)' }}>{meta.latest_snapshot_global}</strong>
              </span>
            )}
            {meta.tracker_heartbeat?.last_finished_at_utc && (
              <span>
                Tracker heartbeat: status={meta.tracker_heartbeat.last_status ?? '—'} · finished=
                {meta.tracker_heartbeat.last_finished_at_utc.slice(11, 16)} UTC
              </span>
            )}
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ padding: '3rem', textAlign: 'center' }}>
          <Loader2 className="lucide-spin" size={32} />
        </div>
      ) : rows.length === 0 ? (
        <div className="glass-panel" style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
          {syncMutation.isPending ? (
            <Loader2 className="lucide-spin" size={28} style={{ margin: '0 auto 1rem' }} />
          ) : null}
          <p style={{ marginBottom: '0.75rem' }}>
            {syncMutation.isPending ? 'Loading first progress snapshot…' : 'No saved snapshots yet for this scan.'}
          </p>
          <p style={{ fontSize: '0.88rem' }}>
            {resolvedEnvId != null
              ? 'Progress is fetched from BigID as soon as the scan starts (workflow bootstrap + this page auto-sync). Charts appear within a few seconds.'
              : 'Snapshots are captured automatically while the scan is running. Start a scan or wait for the background service to record progress.'}
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

          <CollapsibleSection
            title="Analysis (stats + narrative)"
            headerRight={
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
            }
          >
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
                  { label: 'Objects scanned', value: analysis.objects.toLocaleString() },
                  { label: 'Estimated completion', value: analysis.etaUtc, color: analysis.etaColor, small: true },
                  { label: 'Approx. total scan time', value: formatDurationMinutes(analysis.estimatedTotalMinutes) },
                  { label: 'Estimated remaining', value: formatDurationMinutes(analysis.remainingMinutes) },
                  { label: 'Scanned parts', value: analysis.scannedParts.toLocaleString() },
                  { label: 'Failed parts', value: String(analysis.failedParts) },
                  { label: 'falseRemoved (Δ)', value: `${analysis.falseRemovedDelta >= 0 ? '+' : ''}${analysis.falseRemovedDelta}` },
                  { label: 'Classifications (Δ)', value: `${analysis.classDelta >= 0 ? '+' : ''}${analysis.classDelta}` },
                  { label: 'Scan window', value: analysis.scanDuration || '—' },
                ].map(({ label, value, color, small }) => (
                  <div key={label}>
                    <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>
                      {label}
                    </div>
                    <div style={{ fontSize: small ? '0.95rem' : '1.35rem', fontWeight: 800, color: color || 'var(--primary-color)', marginTop: 4 }}>{value}</div>
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
                  {analysis?.targetObjects != null ? (
                    <li>
                      Object progress:{' '}
                      <strong style={{ color: 'var(--text-primary)' }}>
                        {analysis.objects.toLocaleString()} / {analysis.targetObjects.toLocaleString()}
                        {analysis.objectPct != null ? ` (${analysis.objectPct.toFixed(1)}%)` : ''}
                      </strong>
                    </li>
                  ) : null}
                  {analysis?.etaUtc && analysis.etaUtc !== '—' ? (
                    <li>
                      Estimated completion:{' '}
                      <strong style={{ color: analysis.etaColor }}>{analysis.etaUtc}</strong>
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
                  {analysis?.targetObjects != null ? (
                    <>Estimated full-dataset scan time ≈ <strong style={{ color: 'var(--text-primary)' }}>{formatDurationMinutes(analysis.estimatedTotalMinutes)}</strong>, with <strong style={{ color: 'var(--text-primary)' }}>{formatDurationMinutes(analysis.remainingMinutes)}</strong> remaining at the current rate.</>
                  ) : null}{' '}
                  Compare classifier deltas below with environment classifier metadata if the scan profile changed.
                </p>
              </div>
              <div>
                <strong style={{ color: 'var(--text-primary)' }}>Insights row (DB)</strong>
                <p style={{ margin: '0.5rem 0 0' }}>
                  {meta?.insights
                    ? `objects_scanned=${meta.insights.objects_scanned ?? '—'}, total_classification=${meta.insights.total_classification ?? '—'}`
                    : 'No scan insights available yet — click Sync to fetch from BigID.'}
                </p>
              </div>
            </div>
          </CollapsibleSection>

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
                    <Line type="monotone" dataKey="scanPartsTotal" name="Scanned parts total" stroke="#7c3aed" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="scanPartsDone" name="Scanned parts completed" stroke="#0891b2" dot={false} strokeWidth={2} />
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

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '1rem', alignItems: 'start' }}>
            <CollapsibleSection title={<><code>scan_parts</code> · New + unfiltered</>} headerRight={<>
                  <strong style={{ color: 'var(--primary-color)', fontSize: '1.05rem' }}>
                    {(scanPartsData?.count ?? 0).toLocaleString()}
                  </strong>
                  <button
                    type="button"
                    onClick={() => collectionRefreshMutation.mutate('all')}
                    disabled={collectionRefreshDisabled('scan_parts')}
                    title="Fetch both scan_parts and scans from BigID, save them, and reload both panels."
                    style={{ padding: '0.3rem 0.65rem', borderRadius: 7, border: '1px solid var(--surface-border)', background: 'var(--surface-ground)', cursor: collectionRefreshDisabled('scan_parts') ? 'not-allowed' : 'pointer', fontSize: '0.76rem', fontWeight: 600, opacity: collectionRefreshDisabled('scan_parts') ? 0.55 : 1 }}
                  >
                    {collectionRefreshBusy('scan_parts') ? 'Refreshing…' : 'Refresh both collections'}
                  </button>
              </>}>
              <p style={{ margin: '0 0 0.7rem', color: 'var(--text-secondary)', fontSize: '0.8rem', lineHeight: 1.45 }}>
                First: the latest five records matching <code>parent_scan_id</code> and <code>state: New</code>. Below: the next five latest stored records without a state filter. New-filter matches: <strong>{(scanPartsData?.new_filter_count ?? 0).toLocaleString()}</strong>.
              </p>
              {scanPartsLoading && !scanPartsData ? (
                <div style={{ padding: '0.75rem 0' }}><Loader2 className="lucide-spin" size={20} /></div>
              ) : (
                <>
                  <h4 style={{ margin: '0 0 0.45rem', fontSize: '0.82rem', color: '#991b1b' }}>Latest 5 with state = New</h4>
                  {renderScanPartTable(newScanParts, '#b91c1c')}
                  <hr style={{ margin: '1rem 0', border: 0, borderTop: '1px solid var(--surface-border)' }} />
                  <h4 style={{ margin: '1rem 0 0.45rem', fontSize: '0.82rem' }}>Next 5 without state filter</h4>
                  {renderScanPartTable(allScanParts, '#334155')}
                </>
              )}
            </CollapsibleSection>

            <CollapsibleSection title={<><code>scans</code> · Current scan · Latest 15 records</>} headerRight={<>
                  <strong style={{ color: 'var(--primary-color)', fontSize: '1.05rem' }}>
                    {(scanRecordsData?.count ?? 0).toLocaleString()}
                  </strong>
                  <button
                    type="button"
                    onClick={() => collectionRefreshMutation.mutate('all')}
                    disabled={collectionRefreshDisabled('scans')}
                    title="Fetch both scan_parts and scans from BigID, save them, and reload both panels."
                    style={{ padding: '0.3rem 0.65rem', borderRadius: 7, border: '1px solid var(--surface-border)', background: 'var(--surface-ground)', cursor: collectionRefreshDisabled('scans') ? 'not-allowed' : 'pointer', fontSize: '0.76rem', fontWeight: 600, opacity: collectionRefreshDisabled('scans') ? 0.55 : 1 }}
                  >
                    {collectionRefreshBusy('scans') ? 'Refreshing…' : 'Refresh both collections'}
                  </button>
              </>}>
              <p style={{ margin: '0 0 0.7rem', color: 'var(--text-secondary)', fontSize: '0.8rem', lineHeight: 1.45 }}>
                Latest 15 records from BigID&apos;s <strong>scans</strong> collection for this exact current <code>scan_id</code>. Child scan records are excluded.
              </p>
              {scanRecordsLoading && !scanRecordsData ? (
                <div style={{ padding: '0.75rem 0' }}><Loader2 className="lucide-spin" size={20} /></div>
              ) : scanRecordsData?.items?.length ? (
                <div style={{ maxHeight: 420, overflowY: 'scroll', overflowX: 'hidden', paddingRight: 6 }}>
                  {scanRecordsData.items.map((record, index) => {
                    const payload = payloadObject(record.payload);
                    const fields = [
                      ['Subscan', fmtCell(record.subscan_id)],
                      ['State', fmtCell(record.state)],
                      ['Record', fmtCell(record.record_key)],
                      ['Captured', fmtCell(record.captured_at_utc)],
                      ...scanRecordFields.map((field) => [field, fmtPayloadValue(payload[field])]),
                    ];
                    return (
                      <details
                        key={`${record.id ?? record.record_key}`}
                        style={{
                          border: '1px solid var(--surface-border)',
                          borderRadius: 6,
                          marginBottom: 8,
                          overflow: 'hidden',
                        }}
                      >
                        <summary style={{ cursor: 'pointer', padding: '8px', background: '#f1f5f9', fontSize: '0.78rem', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          Document {index + 1} · {fmtCell(record.record_key)}
                        </summary>
                        <div style={{ maxHeight: 360, overflowY: 'scroll', overflowX: 'hidden' }}>
                          {fields.map(([label, value]) => (
                            <div
                              key={label}
                              style={{
                                display: 'grid',
                                gridTemplateColumns: '260px minmax(0, 1fr)',
                                borderBottom: '1px solid var(--surface-border)',
                                fontSize: '0.78rem',
                              }}
                            >
                              <strong style={{ padding: '7px', background: '#f1f5f9', whiteSpace: 'nowrap' }}>{label}</strong>
                              <div style={{ padding: '7px', minWidth: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value}</div>
                            </div>
                          ))}
                        </div>
                      </details>
                    );
                  })}
                </div>
              ) : (
                <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '0.83rem' }}>
                  No records matching this scan_id have been captured yet.
                </p>
              )}
            </CollapsibleSection>
          </div>

          <CollapsibleSection
            title={
              <>
                Classifier / attribute hits
                {clsSnapshot?.total_count != null ? (
                  <span style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-secondary)', marginLeft: '0.6rem' }}>
                    · <strong>{clsSnapshot.total_count.toLocaleString()}</strong> classifiers
                  </span>
                ) : null}
              </>
            }
          >
            <p style={{ margin: '0 0 0.65rem', fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              Rows come from BigID classifier attributes stored in <code style={{ fontSize: '0.85em' }}>scan_insights</code> (Sync refreshes).
              "New findings" is the classifier hit count for this scan. "Prev New/Removed" shows counts from the most recent prior build scan with the same data source configuration.
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
                    {/* Build version header row with merged cells */}
                    <tr style={{ background: '#ffffff', borderBottom: '2px solid #ddd' }}>
                      <th style={{ padding: '8px', textAlign: 'left', borderRight: '1px solid #ddd' }}></th>
                      <th colSpan={2} style={{ padding: '8px', textAlign: 'center', borderRight: '1px solid #ddd', fontWeight: 700, fontSize: '0.95rem', color: '#1f2937' }}>
                        {hitsData?.current_build ?? '—'}
                      </th>
                      <th colSpan={2} style={{ padding: '8px', textAlign: 'center', fontWeight: 700, fontSize: '0.95rem', color: '#1f2937' }}>
                        {hitsData?.previous_build ?? '—'}
                      </th>
                    </tr>
                    {/* Column headers row */}
                    <tr style={{ background: '#f1f5f9' }}>
                      <th style={{ padding: '8px', textAlign: 'left' }}>Classifier / attribute</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>New findings</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>Removed</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>Prev New</th>
                      <th style={{ padding: '8px', textAlign: 'right' }}>Prev Removed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredClassifierRows.map((r, idx) => (
                      <tr key={`${idx}-${r.classifier}`} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                        <td style={{ padding: '7px 8px', fontFamily: 'ui-monospace, monospace', fontSize: '0.78rem' }}>{r.classifier}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right' }}>{r.new_findings.toLocaleString()}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right' }}>{r.removed.toLocaleString()}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right', color: 'var(--text-secondary)' }}>{(r.prev_new ?? 0).toLocaleString()}</td>
                        <td style={{ padding: '7px 8px', textAlign: 'right', color: 'var(--text-secondary)' }}>{(r.prev_removed ?? 0).toLocaleString()}</td>
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
          </CollapsibleSection>

          <CollapsibleSection title="Services Status">
              <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '0.55rem 0', lineHeight: 1.5 }}>
                One row per service with active and failed pod counts. The last five columns are sampled every{' '}
                {servicesData?.sample_minutes ?? 5} min, showing active / failed pods at each sample. Click a row to see its pods.
                <strong> NEW</strong> = first seen after this scan started. <strong>STALE</strong> = no recent sample.
              </p>
              {servicesData?.capture?.is_lagging ? (
                <div style={{ fontSize: '0.8rem', color: TONE_STYLE.bad.fg, background: TONE_STYLE.bad.bg, borderRadius: 6, padding: '6px 10px', marginBottom: 8 }}>
                  No successful service capture since {clockUtc(servicesData.capture.last_ok_at ?? '')} UTC. The rows below may be out of date.
                </div>
              ) : null}
              {servicesData?.capture && servicesData.capture.last_outcome && servicesData.capture.last_outcome !== 'ok' ? (
                <div style={{ fontSize: '0.8rem', color: TONE_STYLE.warn.fg, background: TONE_STYLE.warn.bg, borderRadius: 6, padding: '6px 10px', marginBottom: 8 }}>
                  Last capture at {clockUtc(servicesData.capture.last_at ?? '')} UTC{' '}
                  {servicesData.capture.last_outcome === 'empty' ? 'returned no services' : 'failed'}
                  {servicesData.capture.last_error ? `: ${servicesData.capture.last_error}` : ''}.{' '}
                  {servicesData.capture.last_ok_at
                    ? `Showing the last good data from ${clockUtc(servicesData.capture.last_ok_at)} UTC.`
                    : 'No successful capture in this window yet.'}
                </div>
              ) : null}
              {servicesLoading ? (
                <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>Loading service status…</p>
              ) : !servicesData?.available ? (
                <p style={{ fontSize: '0.85rem', color: 'var(--danger-color)' }}>{servicesData?.message || 'Service status unavailable.'}</p>
              ) : (
                <>
                  <ActivePodsChart series={servicesData.series} />
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                    <thead>
                      <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--surface-border)' }}>
                        <th style={{ padding: '6px 8px' }}>Service</th>
                        <th style={{ padding: '6px 8px', textAlign: 'center' }}>Active</th>
                        <th style={{ padding: '6px 8px', textAlign: 'center' }}>Failed</th>
                        <th style={{ padding: '6px 8px' }}>Last 5</th>
                      </tr>
                    </thead>
                    <tbody>
                      {serviceGroups.map((group) => {
                        const open = expandedServiceGroups.has(group.key);
                        return (
                          <Fragment key={group.key}>
                            <tr
                              onClick={() =>
                                setExpandedServiceGroups((prev) => {
                                  const next = new Set(prev);
                                  if (next.has(group.key)) next.delete(group.key);
                                  else next.add(group.key);
                                  return next;
                                })
                              }
                              style={{
                                borderBottom: '1px solid var(--surface-border)',
                                background: group.anyNew ? '#eff6ff' : 'transparent',
                                cursor: 'pointer',
                              }}
                            >
                              <td style={{ padding: '8px' }}>
                                <span style={{ display: 'inline-block', width: 14, color: 'var(--text-secondary)' }}>{open ? '▾' : '▸'}</span>
                                <span style={{ fontWeight: 700 }}>{group.name}</span>
                                {group.anyNew ? (
                                  <span style={{ marginLeft: 6, fontSize: '0.68rem', fontWeight: 700, color: '#fff', background: '#2563eb', borderRadius: 4, padding: '1px 5px' }}>NEW</span>
                                ) : null}
                                <span style={{ marginLeft: 8, fontSize: '0.72rem', color: 'var(--text-secondary)' }}>{group.pods.length} pod{group.pods.length === 1 ? '' : 's'}</span>
                              </td>
                              <td style={{ padding: '8px', textAlign: 'center' }}>
                                <span style={{ fontWeight: 700, color: TONE_STYLE.good.fg, background: TONE_STYLE.good.bg, borderRadius: 4, padding: '2px 10px' }}>{group.active}</span>
                              </td>
                              <td style={{ padding: '8px', textAlign: 'center' }}>
                                <span style={{ fontWeight: 700, color: group.failed > 0 ? TONE_STYLE.bad.fg : 'var(--text-secondary)', background: group.failed > 0 ? TONE_STYLE.bad.bg : 'transparent', borderRadius: 4, padding: '2px 10px' }}>{group.failed}</span>
                                {group.other > 0 ? (
                                  <div style={{ fontSize: '0.68rem', color: TONE_STYLE.warn.fg }}>+{group.other} other</div>
                                ) : null}
                                {group.gone > 0 ? (
                                  <div style={{ fontSize: '0.68rem', color: '#92400e' }}>{group.gone} not reported</div>
                                ) : null}
                              </td>
                              <td style={{ padding: '8px', whiteSpace: 'nowrap' }}>
                                <div style={{ display: 'flex', gap: 4 }}>
                                  {group.timeline.map((slot) => {
                                    if (slot.outcome === 'ok' && slot.active + slot.failed + slot.other === 0) {
                                      // Capture succeeded but this service had no pods at that slot.
                                      return (
                                        <span
                                          key={slot.at}
                                          title={`No pods of this service in the capture at ${slot.at} UTC`}
                                          style={{ fontSize: '0.7rem', fontWeight: 700, color: '#334155', background: '#f1f5f9', border: '1px solid #cbd5e1', borderRadius: 4, padding: '2px 6px', textAlign: 'center', minWidth: 70 }}
                                        >
                                          <div>NO PODS</div>
                                          <div style={{ fontWeight: 400, opacity: 0.8 }}>{clockUtc(slot.at)}</div>
                                        </span>
                                      );
                                    }
                                    if (slot.outcome !== 'ok') {
                                      return (
                                        <span
                                          key={slot.at}
                                          title={`${slot.outcome}: ${slot.error || 'no data'} at ${slot.at} UTC`}
                                          style={{ fontSize: '0.7rem', fontWeight: 600, color: TONE_STYLE.unknown.fg, background: TONE_STYLE.unknown.bg, border: '1px dashed #94a3b8', borderRadius: 4, padding: '2px 6px', textAlign: 'center', minWidth: 70 }}
                                        >
                                          <div>NO DATA</div>
                                          <div style={{ fontWeight: 400, opacity: 0.8 }}>{clockUtc(slot.at)}</div>
                                        </span>
                                      );
                                    }
                                    // Any active pod means the service is up, even if other pods have failed.
                                    const tone =
                                      slot.active > 0 ? TONE_STYLE.good : slot.other > 0 ? TONE_STYLE.warn : TONE_STYLE.bad;
                                    return (
                                      <span
                                        key={slot.at}
                                        title={`${slot.active} active, ${slot.failed} failed, ${slot.other} other at ${slot.at} UTC`}
                                        style={{ fontSize: '0.7rem', fontWeight: 600, color: tone.fg, background: tone.bg, borderRadius: 4, padding: '2px 6px', textAlign: 'center', minWidth: 70 }}
                                      >
                                        <div>A {slot.active} · F {slot.failed}</div>
                                        <div style={{ fontWeight: 400, opacity: 0.8 }}>{clockUtc(slot.at)}</div>
                                      </span>
                                    );
                                  })}
                                </div>
                              </td>
                            </tr>
                            {open ? (
                              <tr>
                                <td colSpan={4} style={{ padding: '0 8px 10px 22px', background: '#f8fafc' }}>
                                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem', marginTop: 6 }}>
                                    <thead>
                                      <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--surface-border)' }}>
                                        <th style={{ padding: '4px 6px' }}>Pod</th>
                                        <th style={{ padding: '4px 6px' }}>Status</th>
                                        <th style={{ padding: '4px 6px' }}>Last 5</th>
                                        <th style={{ padding: '4px 6px' }}>Changes</th>
                                        <th style={{ padding: '4px 6px' }}>Created</th>
                                        <th style={{ padding: '4px 6px' }}>Last seen</th>
                                        <th style={{ padding: '4px 6px' }}>Threads</th>
                                        <th style={{ padding: '4px 6px' }}>CPU %</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {group.pods.map((pod) => {
                                        const podTone = TONE_STYLE[serviceTone(pod.status)];
                                        return (
                                          <tr
                                            key={pod.service_key}
                                            style={{ borderBottom: '1px solid var(--surface-border)', background: pod.is_new ? '#eff6ff' : 'transparent', opacity: pod.is_stale || pod.not_reported ? 0.55 : 1 }}
                                          >
                                            <td style={{ padding: '4px 6px', wordBreak: 'break-all' }}>
                                              <div style={{ fontWeight: 600 }}>
                                                {pod.name || '—'}
                                                {pod.is_new ? (
                                                  <span style={{ marginLeft: 6, fontSize: '0.66rem', fontWeight: 700, color: '#fff', background: '#2563eb', borderRadius: 4, padding: '1px 5px' }}>NEW</span>
                                                ) : null}
                                                {pod.not_reported ? (
                                                  <span style={{ marginLeft: 6, fontSize: '0.66rem', fontWeight: 700, color: '#92400e', background: '#fef3c7', borderRadius: 4, padding: '1px 5px' }}>NOT REPORTED</span>
                                                ) : null}
                                                {pod.is_stale ? (
                                                  <span style={{ marginLeft: 6, fontSize: '0.66rem', fontWeight: 700, color: '#475569', background: '#e2e8f0', borderRadius: 4, padding: '1px 5px' }}>STALE</span>
                                                ) : null}
                                              </div>
                                              <div style={{ fontSize: '0.68rem', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace' }} title="BigID service id (the only per-pod identifier BigID returns)">{pod.service_key}</div>
                                              {pod.message ? <div style={{ fontSize: '0.68rem', color: 'var(--text-secondary)' }}>{pod.message}</div> : null}
                                            </td>
                                            <td style={{ padding: '4px 6px' }}>
                                              <span style={{ fontWeight: 700, color: podTone.fg, background: podTone.bg, borderRadius: 4, padding: '2px 8px', whiteSpace: 'nowrap' }}>
                                                {pod.status || 'UNKNOWN'}
                                              </span>
                                            </td>
                                            <td style={{ padding: '4px 6px', whiteSpace: 'nowrap' }}>
                                              <div style={{ display: 'flex', gap: 4 }}>
                                                {pod.history.map((h, i) => {
                                                  const t = TONE_STYLE[serviceTone(h.status)];
                                                  const isLatest = i === pod.history.length - 1;
                                                  return (
                                                    <span
                                                      key={`${h.captured_at}-${i}`}
                                                      title={`${h.status || 'UNKNOWN'} at ${h.captured_at} UTC`}
                                                      style={{ fontSize: '0.68rem', fontWeight: 600, color: t.fg, background: t.bg, border: isLatest ? `1.5px solid ${t.fg}` : '1px solid transparent', borderRadius: 4, padding: '2px 5px', textAlign: 'center', minWidth: 58 }}
                                                    >
                                                      <div>{(h.status || 'UNKNOWN').slice(0, 10)}</div>
                                                      <div style={{ fontWeight: 400, opacity: 0.8 }}>{clockUtc(h.captured_at)}</div>
                                                    </span>
                                                  );
                                                })}
                                              </div>
                                            </td>
                                            <td style={{ padding: '4px 6px', textAlign: 'center', fontWeight: pod.changes > 0 ? 700 : 400 }}>{pod.changes}</td>
                                            <td style={{ padding: '4px 6px', whiteSpace: 'nowrap' }}><span title={pod.created_at_is_exact ? 'BigID createDate' : 'Earliest sample (creation time not stored)'}>{pod.created_at_is_exact ? '' : '≤ '}{clockUtc(pod.created_at)} UTC</span></td>
                                            <td style={{ padding: '4px 6px', whiteSpace: 'nowrap' }}>{clockUtc(pod.last_seen)} UTC</td>
                                            <td style={{ padding: '4px 6px' }}>{pod.running_threads}/{pod.total_threads}</td>
                                            <td style={{ padding: '4px 6px' }}>{pod.cpu_pct != null ? pod.cpu_pct.toFixed(1) : '—'}</td>
                                          </tr>
                                        );
                                      })}
                                    </tbody>
                                  </table>
                                </td>
                              </tr>
                            ) : null}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                </>
              )}
          </CollapsibleSection>

          <CollapsibleSection title="Scanner performance metrics">
            <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '0 0 0.55rem', lineHeight: 1.5 }}>
              Charts use samples captured between this scan&apos;s start and end times only (not other scans on the same environment).
              Each bar/line is one <strong>scanner pod</strong> in the BigID group on this environment (not a separate benchmark env).
              By default only <strong>Active</strong> scanners are shown (matches BigID Scanner Management).
            </p>
            {!scannerData?.available ? (
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
                {scannerData?.message ||
                  'Scanner samples unavailable — tracker must populate scanner tables for this environment.'}
              </p>
            ) : (
              <>
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: '0.65rem' }}>
                  {scannerData.message ? (
                    <span style={{ color: 'var(--text-secondary)', fontStyle: 'italic' }}>{scannerData.message}</span>
                  ) : (
                    <>
                      Env #{scannerData.environment_id ?? '—'} · period {scannerData.period_start ?? scannerData.window_start ?? '—'} →{' '}
                      {scannerData.period_is_running ? 'now' : scannerData.period_end ?? scannerData.window_end ?? '—'} ·
                      samples: groups {scannerData.summary?.group_samples ?? scannerData.groups?.length ?? 0}, pods{' '}
                      {scannerData.summary?.pod_samples ?? scannerData.individuals?.length ?? 0}. Speed in DB is B/s; charts use KiB/s where
                      applicable.
                    </>
                  )}
                </p>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: '0.75rem', alignItems: 'center' }}>
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
                  <label
                    style={{
                      marginLeft: 'auto',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      fontSize: '0.82rem',
                      cursor: 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={showFailedScanners}
                      onChange={(e) => setShowFailedScanners(e.target.checked)}
                    />
                    Show failed scanners
                    <span style={{ color: 'var(--text-secondary)', fontSize: '0.78rem' }}>
                      ({latestReportedActiveCount ?? latestPodsSnapshot.filter((p) => p.isActive).length} active
                      {showFailedScanners ? ` / ${latestPodsSnapshot.length} total` : ''})
                    </span>
                  </label>
                </div>
                {scannerTab === 'overview' && (() => {
                  const COLORS = ['#dc2626', '#2563eb', '#d97706', '#059669', '#7c3aed', '#db2777'];
                  const { points, mode } = overviewTimeSeries;
                  const activeCount = latestReportedActiveCount ?? latestPodsSnapshot.filter((p) => p.isActive).length;
                  const barChartData = visibleLatestPods.map((p, i) => ({
                    ...p,
                    color: COLORS[i % COLORS.length],
                  }));

                  if (mode === 'groups' && points.length) {
                    const latest = points[points.length - 1] as Record<string, number | string>;
                    const chartPts = (overviewTimeSeries.fullPoints || points) as Array<Record<string, number | string>>;
                    return (
                      <>
                        <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '0.75rem' }}>
                          Per-pod metrics unavailable — showing aggregated scanner-group trends ({chartPts.length} time buckets).
                        </p>
                        <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
                          <div style={{ padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                            <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Running scanners (latest)</div>
                            <div style={{ fontSize: '1.4rem', fontWeight: 700 }}>{String(latest.scanners ?? 0)}</div>
                          </div>
                          <div style={{ padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                            <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Throughput KiB/s (latest)</div>
                            <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#16a34a' }}>{Number(latest.kibs ?? 0).toFixed(1)}</div>
                          </div>
                          <div style={{ padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                            <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>Memory % (latest)</div>
                            <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#2563eb' }}>{String(latest.memPct ?? 0)}%</div>
                          </div>
                        </div>
                        <div style={{ height: 280, marginBottom: '1rem' }}>
                          <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={chartPts} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                              <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                              <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                              <YAxis yAxisId="left" tick={{ fontSize: 10 }} />
                              <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10 }} />
                              <Tooltip />
                              <Legend wrapperStyle={{ fontSize: 10 }} />
                              <Line yAxisId="left" type="monotone" dataKey="scanners" name="Running scanners" stroke="#2563eb" dot={{ r: 3 }} strokeWidth={2} />
                              <Line yAxisId="right" type="monotone" dataKey="kibs" name="KiB/s" stroke="#16a34a" dot={{ r: 3 }} strokeWidth={2} />
                            </LineChart>
                          </ResponsiveContainer>
                        </div>
                      </>
                    );
                  }

                  const scannersCount = activeCount;
                  const totalRunningThreads = visibleLatestPods.reduce((sum, p) => sum + (p.threads || 0), 0);
                  const cpuValues = visibleLatestPods.map((p) => p.cpu).filter((v) => v > 0);
                  const memValues = visibleLatestPods.map((p) => p.mem).filter((v) => v > 0);
                  const meanCpu = cpuValues.length > 0 ? (cpuValues.reduce((a, b) => a + b, 0) / cpuValues.length).toFixed(1) : '—';
                  const peakCpu = cpuValues.length > 0 ? Math.max(...cpuValues).toFixed(1) : '—';
                  const meanMem = memValues.length > 0 ? (memValues.reduce((a, b) => a + b, 0) / memValues.length).toFixed(1) : '—';
                  const peakMem = memValues.length > 0 ? Math.max(...memValues).toFixed(1) : '—';

                  const barCell = (entry: (typeof barChartData)[0], fill: string) => (
                    <Cell
                      key={entry.name}
                      fill={entry.isActive ? fill : '#94a3b8'}
                      stroke={showFailedScanners && entry.isActive ? '#16a34a' : undefined}
                      strokeWidth={showFailedScanners && entry.isActive ? 3 : 0}
                    />
                  );

                  if (!barChartData.length) {
                    return (
                      <p style={{ fontSize: '0.83rem', color: 'var(--text-secondary)' }}>
                        No active scanners in the latest snapshot.
                        {latestPodsSnapshot.length > 0 && !showFailedScanners
                          ? ' Enable “Show failed scanners” to include failed pods.'
                          : ''}
                      </p>
                    );
                  }

                  return (
                    <>
                      {/* Summary Metrics */}
                      <div style={{ display: 'flex', gap: '1rem', marginBottom: '1.5rem', flexWrap: 'wrap' }}>
                        <div style={{ flex: '1 1 auto', minWidth: 120, padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>
                            Active scanners (latest)
                          </div>
                          <div style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                            {scannersCount}
                            {showFailedScanners && latestPodsSnapshot.length > scannersCount ? (
                              <span style={{ fontSize: '0.85rem', fontWeight: 500, color: 'var(--text-secondary)' }}>
                                {' '}
                                / {latestPodsSnapshot.length}
                              </span>
                            ) : null}
                          </div>
                        </div>
                        <div style={{ flex: '1 1 auto', minWidth: 120, padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Σ running threads</div>
                          <div style={{ fontSize: '1.5rem', fontWeight: 700, color: '#7c3aed' }}>{String(totalRunningThreads)}</div>
                        </div>
                        <div style={{ flex: '1 1 auto', minWidth: 140, padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Mean / peak CPU %</div>
                          <div style={{ fontSize: '1.3rem', fontWeight: 700, color: '#dc2626' }}>{meanCpu} / {peakCpu}</div>
                        </div>
                        <div style={{ flex: '1 1 auto', minWidth: 140, padding: '0.75rem', background: 'var(--surface-ground)', borderRadius: 8 }}>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Mean / peak mem %</div>
                          <div style={{ fontSize: '1.3rem', fontWeight: 700, color: '#2563eb' }}>{meanMem} / {peakMem}</div>
                        </div>
                      </div>

                      {/* Latest Snapshot - Bar Charts */}
                      <div style={{ marginBottom: '1.5rem' }}>
                        <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '0.75rem', color: 'var(--text-secondary)' }}>
                          Latest snapshot — vertical bars
                          {showFailedScanners ? (
                            <span style={{ fontWeight: 500, marginLeft: 8 }}>Active = green border</span>
                          ) : null}
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1.5rem' }}>
                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 220 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Running threads</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <BarChart data={barChartData} margin={{ top: 4, right: 12, left: 0, bottom: 20 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="name" tick={{ fontSize: 8 }} angle={-45} textAnchor="end" height={60} />
                                <YAxis tick={{ fontSize: 9 }} />
                                <Tooltip />
                                <Bar dataKey="threads" radius={[4, 4, 0, 0]}>
                                  {barChartData.map((entry) => barCell(entry, '#7c3aed'))}
                                </Bar>
                              </BarChart>
                            </ResponsiveContainer>
                          </div>

                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 220 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>CPU %</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <BarChart data={barChartData} margin={{ top: 4, right: 12, left: 0, bottom: 20 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="name" tick={{ fontSize: 8 }} angle={-45} textAnchor="end" height={60} />
                                <YAxis tick={{ fontSize: 9 }} unit="%" />
                                <Tooltip formatter={(v: any) => (v != null ? `${v}%` : '—')} />
                                <Bar dataKey="cpu" radius={[4, 4, 0, 0]}>
                                  {barChartData.map((entry) => barCell(entry, '#dc2626'))}
                                </Bar>
                              </BarChart>
                            </ResponsiveContainer>
                          </div>

                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 220 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Memory %</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <BarChart data={barChartData} margin={{ top: 4, right: 12, left: 0, bottom: 20 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="name" tick={{ fontSize: 8 }} angle={-45} textAnchor="end" height={60} />
                                <YAxis tick={{ fontSize: 9 }} domain={[0, 100]} unit="%" />
                                <Tooltip formatter={(v: any) => (v != null ? `${v}%` : '—')} />
                                <Bar dataKey="mem" radius={[4, 4, 0, 0]}>
                                  {barChartData.map((entry) => barCell(entry, '#2563eb'))}
                                </Bar>
                              </BarChart>
                            </ResponsiveContainer>
                          </div>
                        </div>
                      </div>
                    </>
                  );
                })()}
                {scannerTab === 'groups' && (
                  <>
                    {!groupChartPoints.length ? (
                      <p style={{ fontSize: '0.83rem', color: 'var(--text-secondary)' }}>No group time-series in this scan window yet.</p>
                    ) : (
                      <>
                        {/* Total running threads over time - full width */}
                        <div style={{ marginBottom: '1.5rem' }}>
                          <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Total running scanners over time (all groups)</div>
                          <div style={{ height: 280 }}>
                            <ResponsiveContainer width="100%" height="100%">
                              <LineChart data={groupChartPoints} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                <YAxis tick={{ fontSize: 10 }} />
                                <Tooltip />
                                <Legend wrapperStyle={{ fontSize: 10 }} />
                                <Line type="monotone" dataKey="scanners" name="Running scanners" stroke="#2563eb" dot={{ r: 4 }} strokeWidth={2} connectNulls={true} />
                                <Line type="monotone" dataKey="threads" name="Running threads" stroke="#7c3aed" dot={{ r: 3 }} strokeWidth={2} connectNulls={true} />
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        </div>

                        {/* Three-column layout for other group charts */}
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 260 }}>
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
                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 260 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Group memory (aggregated)</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <AreaChart data={groupChartPoints} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                <YAxis tick={{ fontSize: 10 }} domain={[0, 100]} unit="%" />
                                <Tooltip formatter={(v: any) => [`${v}%`, 'Memory']} />
                                <Area type="monotone" dataKey="memPct" stroke="#9333ea" fill="#c4b5fd" name="Memory %" />
                              </AreaChart>
                            </ResponsiveContainer>
                          </div>
                          <div style={{ flex: '1 1 30%', minWidth: 280, height: 260 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Group CPU (aggregated)</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <LineChart data={groupChartPoints} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                <YAxis tick={{ fontSize: 10 }} unit="%" />
                                <Tooltip formatter={(v: any) => [`${v}%`, 'CPU']} />
                                <Legend wrapperStyle={{ fontSize: 10 }} />
                                <Line type="monotone" dataKey="cpuPct" name="CPU %" stroke="#dc2626" dot={{ r: 4 }} strokeWidth={2} connectNulls={true} />
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        </div>
                      </>
                    )}
                  </>
                )}
                {scannerTab === 'pods' && (
                  <>
                    {!podLinesByScanner.length ? (
                      <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>No per-pod series in this window.</p>
                    ) : (
                      podLinesByScanner.map((pl) => (
                        <div key={pl.id} style={{ marginBottom: '1.25rem' }}>
                          <div style={{ fontSize: '0.85rem', fontWeight: 700, marginBottom: '0.4rem', color: 'var(--text-primary)' }}>
                            {pl.label}
                          </div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
                            <div style={{ flex: '1 1 45%', minWidth: 280, height: 220 }}>
                              <div style={{ fontSize: '0.75rem', fontWeight: 600, marginBottom: 4, color: '#dc2626' }}>CPU %</div>
                              <ResponsiveContainer width="100%" height="100%">
                                <LineChart data={pl.points} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                  <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                  <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                  <YAxis tick={{ fontSize: 10 }} unit="%" />
                                  <Tooltip formatter={(v: any) => [v != null ? `${v}%` : '—', 'CPU %']} />
                                  <Line type="monotone" dataKey="cpu" stroke="#dc2626" dot={{ r: 4 }} strokeWidth={2} name="CPU %" connectNulls={true} />
                                </LineChart>
                              </ResponsiveContainer>
                            </div>
                            <div style={{ flex: '1 1 45%', minWidth: 280, height: 220 }}>
                              <div style={{ fontSize: '0.75rem', fontWeight: 600, marginBottom: 4, color: '#9333ea' }}>Memory %</div>
                              <ResponsiveContainer width="100%" height="100%">
                                <LineChart data={pl.points} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                  <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                  <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                  <YAxis tick={{ fontSize: 10 }} domain={[0, 100]} unit="%" />
                                  <Tooltip formatter={(v: any) => [v != null ? `${v}%` : '—', 'Memory %']} />
                                  <Line type="monotone" dataKey="memPct" stroke="#9333ea" dot={{ r: 4 }} strokeWidth={2} name="Memory %" connectNulls={true} />
                                </LineChart>
                              </ResponsiveContainer>
                            </div>
                          </div>
                        </div>
                      ))
                    )}

                    {/* Scanner-level CPU and Memory over time charts - full time series */}
                    {scannerData?.individuals && scannerData.individuals.length > 0 && (
                      <div style={{ marginTop: '2rem', paddingTop: '1.5rem', borderTop: '1px solid var(--surface-border)' }}>
                        <div style={{ fontSize: '0.85rem', fontWeight: 600, marginBottom: '1rem', color: 'var(--text-secondary)' }}>Over time — CPU % (per scanner)</div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1.25rem' }}>
                          <div style={{ flex: '1 1 45%', minWidth: 320, height: 280 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>
                              CPU % per scanner over time
                              <span style={{ fontWeight: 400, color: 'var(--text-secondary)', marginLeft: 6 }}>(load avg / cores × 100, may exceed 100%)</span>
                            </div>
                            <ResponsiveContainer width="100%" height="100%">
                              <LineChart data={podLinesByScanner.length > 0 ? podLinesByScanner[0]?.points || [] : []} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                <YAxis tick={{ fontSize: 10 }} unit="%" />
                                <Tooltip formatter={(v: any) => [v != null ? `${v}%` : '—', '']} />
                                <Legend wrapperStyle={{ fontSize: 10 }} />
                                {podLinesByScanner.map((pl, i) => {
                                  const COLORS = ['#dc2626', '#2563eb', '#d97706', '#059669', '#7c3aed', '#db2777'];
                                  return (
                                    <Line key={pl.id} type="monotone" dataKey="cpu" name={pl.label}
                                      stroke={COLORS[i % COLORS.length]} dot={{ r: 3 }} strokeWidth={2} connectNulls={true} data={pl.points} isAnimationActive={false} />
                                  );
                                })}
                              </LineChart>
                            </ResponsiveContainer>
                          </div>

                          <div style={{ flex: '1 1 45%', minWidth: 320, height: 280 }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: 600, marginBottom: 4 }}>Memory usage % per scanner over time</div>
                            <ResponsiveContainer width="100%" height="100%">
                              <LineChart data={podLinesByScanner.length > 0 ? podLinesByScanner[0]?.points || [] : []} margin={{ top: 4, right: 12, left: 0, bottom: 0 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
                                <XAxis dataKey="t" tick={{ fontSize: 9 }} />
                                <YAxis tick={{ fontSize: 10 }} domain={[0, 100]} unit="%" />
                                <Tooltip formatter={(v: any) => [v != null ? `${v}%` : '—', '']} />
                                <Legend wrapperStyle={{ fontSize: 10 }} />
                                {podLinesByScanner.map((pl, i) => {
                                  const COLORS = ['#dc2626', '#2563eb', '#d97706', '#059669', '#7c3aed', '#db2777'];
                                  return (
                                    <Line key={pl.id} type="monotone" dataKey="memPct" name={pl.label}
                                      stroke={COLORS[i % COLORS.length]} dot={{ r: 3 }} strokeWidth={2} connectNulls={true} data={pl.points} isAnimationActive={false} />
                                  );
                                })}
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        </div>
                      </div>
                    )}
                  </>
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
          </CollapsibleSection>

          <CollapsibleSection title="AI log analysis & correlation">
            <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '0 0 0.65rem', lineHeight: 1.5 }}>
              Errors shown for log timestamps between{' '}
              <strong>{scanErrorsData?.period_start ?? parent?.started_at ?? '—'}</strong> and{' '}
              <strong>
                {scanErrorsData?.period_is_running || !scanErrorsData?.period_end
                  ? 'now'
                  : scanErrorsData.period_end ?? parent?.completed_at ?? '—'}
              </strong>{' '}
              (this scan&apos;s period only).
            </p>
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
            <div style={{ marginTop: '0.5rem', maxHeight: 560, overflow: 'auto' }}>
              {(scanErrorsData?.items ?? []).length === 0 ? (
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
                  No errors detected for this scan period.{' '}
                  Errors are captured automatically while the scan is running — this does not confirm the system is error-free.
                  Check the Logs page if errors are expected.
                </p>
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
                        <td style={{ padding: '6px' }}>
                          <div style={{ maxHeight: 160, overflow: 'auto', whiteSpace: 'pre', fontFamily: 'monospace', fontSize: '0.72rem', lineHeight: 1.4 }}>
                            {e.error_context || ''}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </CollapsibleSection>

          <CollapsibleSection title="Scan-part report errors">
            <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '0 0 0.65rem', lineHeight: 1.5 }}>
              Download the BigID scan-parts CSV report for each subscan, keep rows with a non-empty error message, and show all fields without persisting the file. This is triggered automatically when the scan status is <strong>CompletedWithFailures</strong>.
            </p>
            <button
              type="button"
              onClick={() => reportErrorsMutation.mutate()}
              disabled={reportErrorsMutation.isPending}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.35rem',
                padding: '0.4rem 0.75rem',
                fontSize: '0.85rem',
                fontWeight: 600,
                borderRadius: 6,
                border: '1px solid var(--surface-border)',
                background: 'var(--surface-color)',
                color: 'var(--text-primary)',
                cursor: reportErrorsMutation.isPending ? 'not-allowed' : 'pointer',
                opacity: reportErrorsMutation.isPending ? 0.7 : 1,
              }}
            >
              {reportErrorsMutation.isPending ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <FileDown size={14} />
              )}
              Load report errors
            </button>
            {reportErrorsMutation.isError && (
              <p style={{ marginTop: '0.65rem', fontSize: '0.82rem', color: 'var(--danger-color)' }}>
                <AlertCircle size={14} style={{ verticalAlign: 'middle', marginRight: 4 }} />
                {reportErrorsMutation.error instanceof Error
                  ? reportErrorsMutation.error.message
                  : 'Failed to load report errors'}
              </p>
            )}
            {reportErrorsMutation.data && (
              <div style={{ marginTop: '0.85rem' }}>
                {!reportErrorsMutation.data.available ? (
                  <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
                    Report errors are not available: {reportErrorsMutation.data.error || 'unknown reason'}
                  </p>
                ) : reportErrorsMutation.data.error_count === 0 ? (
                  <p style={{ fontSize: '0.82rem', color: 'var(--accent-color)' }}>
                    No error rows found in the scan-parts report for subscan(s):{' '}
                    {reportErrorsMutation.data.subscan_ids.join(', ') || '—'}
                  </p>
                ) : (
                  <>
                    <p style={{ fontSize: '0.82rem', margin: '0 0 0.65rem' }}>
                      <strong>{reportErrorsMutation.data.error_count}</strong> error row(s) from subscan(s):{' '}
                      {reportErrorsMutation.data.subscan_ids.join(', ') || '—'}
                    </p>
                    <div
                      style={{
                        maxHeight: 420,
                        overflow: 'auto',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '0.75rem',
                      }}
                    >
                      {reportErrorsMutation.data.errors.map((row, idx) => {
                        const entries = Object.entries(row).filter(([k]) => k !== '_subscan_id');
                        return (
                          <div
                            key={`report-error-${idx}`}
                            style={{
                              border: '1px solid var(--surface-border)',
                              borderRadius: 8,
                              padding: '0.75rem',
                              background: '#f8fafc',
                              fontSize: '0.78rem',
                            }}
                          >
                            {row._subscan_id && (
                              <div
                                style={{ fontWeight: 700, marginBottom: '0.4rem', fontSize: '0.8rem' }}
                              >
                                Subscan: {row._subscan_id}
                              </div>
                            )}
                            {entries.map(([key, value]) => (
                              <div key={key} style={{ marginBottom: '0.25rem', lineHeight: 1.45 }}>
                                <span
                                  style={{
                                    fontWeight: key === 'errorMessage' ? 700 : 600,
                                    color:
                                      key === 'errorMessage'
                                        ? 'var(--danger-color)'
                                        : 'var(--text-secondary)',
                                    marginRight: '0.35rem',
                                  }}
                                >
                                  {key}:
                                </span>
                                <span style={{ wordBreak: 'break-word' }}>{value || '—'}</span>
                              </div>
                            ))}
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>
            )}
          </CollapsibleSection>

          <details className="glass-panel" style={{ padding: '0.75rem 1rem 1rem' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 700, fontSize: '1.05rem', marginBottom: '0.5rem' }}>
              Raw Progress Data ({histData?.returned ?? rows.length} of {histData?.total ?? rows.length} rows)
            </summary>
            <div style={{ overflowX: 'auto', maxHeight: 420, overflowY: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.72rem' }}>
                <thead style={{ position: 'sticky', top: 0, background: '#f1f5f9', zIndex: 1 }}>
                  <tr>
                    {[
                      'time\n(etc)',
                      'scan\nstate',
                      'completed\nparts',
                      'failed\nparts',
                      'total parts\nscanned',
                      'parts done\nthis interval',
                      'objects\nscanned',
                      'objects\nthis interval',
                      '# of\nclassifications',
                      'newly\nadded',
                      'false\nremoved',
                      'objects\nfailed',
                      'objects\nwith_pi',
                      'Data\nthroughput',
                    ].map((h) => (
                      <th key={h} style={{ padding: '6px 8px', textAlign: 'left', whiteSpace: 'pre-line', lineHeight: 1.1, borderBottom: '1px solid var(--surface-border)' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, index) => (
                    <tr key={r.id ?? `${r.captured_at_utc}-${r.timestamp}`} style={{ borderBottom: '1px solid #e2e8f0' }}>
                      <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{fmtCell(rowTime(r))}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.scan_state)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.completed_parts)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.failed_parts)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.total_parts_scanned)}</td>
                      <td style={{ padding: '5px 8px' }}>{formatRowDelta(r, rows[index - 1], 'completed_parts')}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_scanned)}</td>
                      <td style={{ padding: '5px 8px' }}>{formatRowDelta(r, rows[index - 1], 'objects_scanned')}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.total_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.newly_added_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.false_removed_classifications)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_failed)}</td>
                      <td style={{ padding: '5px 8px' }}>{fmtCell(r.objects_with_pi)}</td>
                      <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{formatRowThroughput(r, rows[index - 1])}</td>
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
