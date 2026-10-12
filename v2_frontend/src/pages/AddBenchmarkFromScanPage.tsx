import React, { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  ArrowLeft,
  Database,
  Loader2,
  CheckCircle2,
  Bookmark,
  Gauge,
  Folder,
  Clock,
  Server,
  Settings,
  PieChart,
  FileText,
  PlayCircle,
  Circle,
  BarChart3,
  ClipboardList,
  ExternalLink,
  Info,
  type LucideIcon,
} from 'lucide-react';
import { API_BASE } from '../apiConfig';

/** Canonical structured presets when API/DB has no size (Postgres Mixed Tables label). */
function resolveStructuredTotalSizeGb(
  dataSourceType: string | undefined,
  dataType: string | undefined,
  totalFiles: number | null | undefined,
  apiGb: number | null | undefined
): { gb: number | null; caption?: string } {
  if (typeof apiGb === 'number' && apiGb > 0) {
    return { gb: apiGb };
  }
  if ((dataType || '').toLowerCase() !== 'structured') {
    return { gb: null };
  }
  const ds = (dataSourceType || '').toLowerCase();
  const tables = totalFiles ?? null;
  if ((ds.includes('postgres') || ds === 'postgresql') && tables === 10000) {
    return {
      gb: 20,
      caption: 'Prefilled from preset PostgreSQL-10K-Tables-17GB (20 GB total_size_gb)',
    };
  }
  if (ds.includes('mssql') && tables === 10000) {
    return {
      gb: 10,
      caption: 'Prefilled from preset MSSQL-10K-Tables-9GB (10 GB total_size_gb)',
    };
  }
  return { gb: null };
}

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

interface ThroughputSet {
  throughput_cluster_gb_hr: number;
  throughput_cluster_files_hr: number;
  throughput_scanner_gb_hr: number;
  throughput_scanner_files_hr: number;
  throughput_thread_gb_hr: number;
  throughput_thread_files_hr: number;
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
  existing_benchmark?: { id: number; updated_at?: string | null } | null;
  benchmark_context?: {
    prefill?: Record<string, unknown>;
    timing?: BenchmarkTiming;
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
    throughput_preview?: Partial<ThroughputSet>;
    throughput_defaults?: { scanner_count: number; thread_count: number };
  } | null;
}

const fmtFiles = (n: number) =>
  Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—';

const fmtInt = (n: number | null | undefined) =>
  n == null || !Number.isFinite(n) ? '—' : Math.round(n).toLocaleString();

function toNum(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/** Normalize an ISO / DB timestamp to "YYYY-MM-DD HH:MM:SS" for display. */
function fmtTs(v?: string | null): string {
  if (!v) return '—';
  const s = String(v).trim();
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
  if (m) return `${m[1]} ${m[2]}`;
  return s.length > 19 ? s.slice(0, 19).replace('T', ' ') : s;
}

interface BenchmarkTiming {
  duration_hours?: number | null;
  duration_seconds?: number | null;
  running_time_hms?: string | null;
  running_time_label?: string | null;
  source?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

/** Fallback if API did not send running_time_hms (parent start → end). */
function runningTimeFromTiming(timing?: BenchmarkTiming | null): {
  hms: string;
  label: string;
  hours: number | null;
} | null {
  if (!timing) return null;
  const hms = String(timing.running_time_hms ?? '').trim();
  const label = String(timing.running_time_label ?? '').trim();
  const hours = timing.duration_hours;
  if (hms && label) {
    return { hms, label, hours: typeof hours === 'number' && hours > 0 ? hours : null };
  }
  const start = timing.started_at;
  const end = timing.completed_at;
  if (!start || !end) return null;
  const t0 = Date.parse(String(start));
  const t1 = Date.parse(String(end));
  if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) return null;
  const sec = Math.round((t1 - t0) / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const hmsLocal = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  const parts: string[] = [];
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (s || !parts.length) parts.push(`${s}s`);
  return { hms: hmsLocal, label: parts.join(' '), hours: sec / 3600 };
}

/* ---------- presentation primitives (token-driven, no new CSS files) ---------- */

const SCOPED_CSS = `
.abs2 { --card-bg: var(--surface-color, #fff); --card-bd: var(--surface-border, #e2e8f0); }
.abs2 input:focus-visible {
  outline: none;
  border-color: var(--primary-color, #0052cc);
  box-shadow: 0 0 0 3px rgba(0, 82, 204, 0.18);
}
.abs2-card {
  background: var(--card-bg);
  border: 1px solid var(--card-bd);
  border-radius: 14px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.06);
}
.abs2-btn { transition: transform .12s ease, box-shadow .12s ease, filter .12s ease; }
.abs2-btn:not(:disabled):hover { transform: translateY(-1px); filter: brightness(1.04); box-shadow: 0 6px 18px rgba(0,0,0,.16); }
.abs2-btn:not(:disabled):active { transform: translateY(0); box-shadow: none; }
.abs2-link { text-decoration: none; transition: opacity .12s ease; }
.abs2-link:hover { text-decoration: underline; opacity: .85; }
.abs2-find:hover { background: rgba(99,102,241,.06); }
.abs2-kpi { transition: box-shadow .15s ease, transform .15s ease; }
.abs2-kpi:hover { box-shadow: 0 8px 22px rgba(15,23,42,.10); transform: translateY(-2px); }
`;

/** Decorative throughput sparkline — a fixed upward trend motif (not time-series data). */
const SPARK = [0.30, 0.26, 0.40, 0.34, 0.50, 0.44, 0.58, 0.52, 0.64, 0.60, 0.70, 0.66, 0.62, 0.80, 0.88, 1.0];
const Sparkline: React.FC<{ color: string }> = ({ color }) => {
  const w = 240;
  const h = 52;
  const pad = 4;
  const n = SPARK.length;
  const pts = SPARK.map((v, i) => {
    const x = pad + (i * (w - 2 * pad)) / (n - 1);
    const y = pad + (1 - v) * (h - 2 * pad);
    return [x, y] as const;
  });
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${w - pad},${h - pad} L${pad},${h - pad} Z`;
  const gid = `abs2spark-${color.replace('#', '')}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" aria-hidden style={{ display: 'block' }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
};

const CardHeading: React.FC<{ icon: LucideIcon; children: React.ReactNode; right?: React.ReactNode }> = ({
  icon: Icon,
  children,
  right,
}) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: '0.9rem' }}>
    <Icon size={17} style={{ color: 'var(--primary-color)', flexShrink: 0 }} aria-hidden />
    <span
      style={{
        fontSize: '0.76rem',
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '0.07em',
        color: 'var(--text-secondary)',
      }}
    >
      {children}
    </span>
    {right ? <span style={{ marginLeft: 'auto' }}>{right}</span> : null}
  </div>
);

const Field: React.FC<{ label: string; children: React.ReactNode; mono?: boolean }> = ({ label, children, mono }) => (
  <div
    style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'baseline',
      gap: '1rem',
      fontSize: '0.85rem',
      padding: '0.42rem 0',
      borderBottom: '1px solid var(--surface-border)',
    }}
  >
    <span style={{ color: 'var(--text-secondary)', fontWeight: 500, whiteSpace: 'nowrap' }}>{label}</span>
    <span
      style={{
        color: 'var(--text-primary)',
        fontWeight: 600,
        textAlign: 'right',
        fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, monospace' : 'inherit',
        wordBreak: 'break-word',
      }}
    >
      {children}
    </span>
  </div>
);

const cardPad: React.CSSProperties = { padding: '1.25rem 1.35rem' };

const numInput: React.CSSProperties = {
  padding: '0.3rem 0.45rem',
  borderRadius: 7,
  border: '1px solid var(--surface-border)',
  fontFamily: 'inherit',
  fontSize: '0.86rem',
  fontWeight: 700,
  color: 'var(--text-primary)',
  background: 'var(--surface-color)',
  width: 76,
  textAlign: 'right',
  boxSizing: 'border-box',
};

/* KPI card */
const KpiCard: React.FC<{
  icon: LucideIcon;
  iconBg: string;
  value: string;
  unit?: string;
  label: string;
}> = ({ icon: Icon, iconBg, value, unit, label }) => (
  <div className="abs2-card abs2-kpi" style={{ ...cardPad, display: 'flex', flexDirection: 'column', gap: '0.9rem', minWidth: 0 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.9rem' }}>
      <span
        style={{
          width: 46,
          height: 46,
          borderRadius: '50%',
          background: iconBg,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <Icon size={22} color="#fff" aria-hidden />
      </span>
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 5, flexWrap: 'wrap' }}>
          <span style={{ fontSize: '1.65rem', fontWeight: 800, color: 'var(--text-primary)', lineHeight: 1, letterSpacing: '-0.01em' }}>
            {value}
          </span>
          {unit ? <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>{unit}</span> : null}
        </div>
      </div>
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span
        style={{
          fontSize: '0.68rem',
          fontWeight: 700,
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          color: 'var(--text-secondary)',
        }}
      >
        {label}
      </span>
      <Info size={12} style={{ color: 'var(--text-secondary)', opacity: 0.6 }} aria-hidden />
    </div>
  </div>
);

/* Dataset summary tile */
const SummaryTile: React.FC<{ icon: LucideIcon; value: string; label: string; color: string }> = ({
  icon: Icon,
  value,
  label,
  color,
}) => (
  <div
    style={{
      border: '1px solid var(--surface-border)',
      borderRadius: 12,
      padding: '1rem 0.5rem',
      textAlign: 'center',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      gap: '0.55rem',
      background: 'var(--surface-color)',
      minWidth: 0,
    }}
  >
    <Icon size={26} style={{ color }} aria-hidden />
    <div style={{ fontSize: '1.02rem', fontWeight: 800, color: 'var(--text-primary)', lineHeight: 1.1, wordBreak: 'break-word' }}>{value}</div>
    <div style={{ fontSize: '0.72rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</div>
  </div>
);

/* Calculated throughput tile */
const ThroughputTile: React.FC<{
  label: string;
  color: string;
  gbHr: number;
  filesHr: number;
  volumeUnit: string;
}> = ({ label, color, gbHr, filesHr, volumeUnit }) => (
  <div
    style={{
      border: '1px solid var(--surface-border)',
      borderRadius: 12,
      padding: '0.95rem 1rem 1.1rem',
      display: 'flex',
      flexDirection: 'column',
      gap: '0.5rem',
      background: 'var(--surface-color)',
      minWidth: 0,
    }}
  >
    <div style={{ fontSize: '0.68rem', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color, textAlign: 'center' }}>
      {label}
    </div>
    <div style={{ textAlign: 'center' }}>
      <span style={{ fontSize: '1.5rem', fontWeight: 800, color, lineHeight: 1 }}>{gbHr.toFixed(2)}</span>
      <span style={{ fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-secondary)', marginLeft: 4 }}>GB/hr</span>
    </div>
    <Sparkline color={color} />
    <div style={{ textAlign: 'center', fontSize: '0.82rem', color: 'var(--text-secondary)', fontWeight: 600 }}>
      {fmtFiles(filesHr)} <span style={{ fontWeight: 500 }}>{volumeUnit}</span>
    </div>
  </div>
);

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const done = /complet|done|success/i.test(status);
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '0.28rem 0.7rem',
        borderRadius: 999,
        fontSize: '0.78rem',
        fontWeight: 700,
        background: 'rgba(255,255,255,0.95)',
        color: done ? '#047857' : '#4338ca',
      }}
    >
      {done ? <CheckCircle2 size={15} /> : <Circle size={15} />}
      {status}
    </span>
  );
};

/* Timeline row */
const TimelineRow: React.FC<{ label: string; ts: string; kind: 'start' | 'dot' | 'done'; last?: boolean }> = ({
  label,
  ts,
  kind,
  last,
}) => {
  const color = kind === 'done' ? '#10b981' : kind === 'start' ? '#4f46e5' : '#94a3b8';
  return (
    <div style={{ display: 'flex', gap: '0.85rem', position: 'relative', paddingBottom: last ? 0 : '1.1rem' }}>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0 }}>
        {kind === 'start' ? (
          <PlayCircle size={20} color={color} aria-hidden />
        ) : kind === 'done' ? (
          <CheckCircle2 size={20} color={color} aria-hidden />
        ) : (
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: color, margin: '4px 0' }} />
        )}
        {!last && <span style={{ width: 2, flex: 1, background: 'var(--surface-border)', marginTop: 4, minHeight: 14 }} />}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '1rem', flex: 1, minWidth: 0 }}>
        <span style={{ fontSize: '0.9rem', fontWeight: 600, color: 'var(--text-primary)' }}>{label}</span>
        <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace', whiteSpace: 'nowrap' }}>{ts}</span>
      </div>
    </div>
  );
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
  const queryClient = useQueryClient();

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

  const _STRUCTURED_DS = ['postgres', 'mssql', 'mysql', 'oracle'];
  const isStructured =
    (detail?.benchmark_context?.run_metrics?.data_type ?? '').toLowerCase() === 'structured' ||
    _STRUCTURED_DS.includes((detail?.benchmark_context?.run_metrics?.data_source_type ?? '').toLowerCase());

  const structuredSize = useMemo(() => {
    const bc = detail?.benchmark_context;
    const fsd = bc?.file_set_details;
    const ev = bc?.effective_volumes;
    const rm = bc?.run_metrics;
    const tables = fsd?.total_files ?? ev?.total_files ?? null;
    const apiGb =
      fsd?.total_size_gb != null && fsd.total_size_gb > 0
        ? fsd.total_size_gb
        : typeof ev?.total_gb === 'number' && ev.total_gb > 0
          ? ev.total_gb
          : null;
    return resolveStructuredTotalSizeGb(rm?.data_source_type, rm?.data_type, tables, apiGb);
  }, [detail?.benchmark_context]);

  const displayTotalSizeGb = structuredSize.gb;

  useEffect(() => {
    const d = detail?.benchmark_context?.throughput_defaults;
    if (d && !appliedApiDefaults) {
      setScannerCount(d.scanner_count);
      setThreadCount(d.thread_count);
      setAppliedApiDefaults(true);
    }
    const fsdLocal = detail?.benchmark_context?.file_set_details;
    const ev = detail?.benchmark_context?.effective_volumes;
    if (fsdLocal?.total_files != null) {
      setTotalTables(fsdLocal.total_files);
    } else if (typeof ev?.total_files === 'number' && ev.total_files > 0) {
      setTotalTables(ev.total_files);
    }
    if (structuredSize.gb != null) {
      setTotalDbSizeGb(structuredSize.gb);
    } else if (fsdLocal?.total_size_gb != null && fsdLocal.total_size_gb > 0) {
      setTotalDbSizeGb(fsdLocal.total_size_gb);
    } else if (typeof ev?.total_gb === 'number' && ev.total_gb > 0) {
      setTotalDbSizeGb(ev.total_gb);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.benchmark_context, structuredSize.gb]);

  const throughputLive = useMemo(() => {
    const bc = detail?.benchmark_context;
    const dur = bc?.timing?.duration_hours;
    if (dur == null || dur <= 0) return null;
    const ev = bc?.effective_volumes;
    const totalFiles = isStructured && totalTables !== ''
      ? Number(totalTables)
      : (typeof ev?.total_files === 'number' ? ev.total_files : 0);
    const totalGb =
      isStructured && totalDbSizeGb !== ''
        ? Number(totalDbSizeGb)
        : displayTotalSizeGb ?? (typeof ev?.total_gb === 'number' ? ev.total_gb : 0);
    return computeThroughputs(dur, totalGb, totalFiles, scannerCount, threadCount);
  }, [detail?.benchmark_context, scannerCount, threadCount, isStructured, totalDbSizeGb, totalTables, displayTotalSizeGb]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const bc = detail?.benchmark_context;
      const rm = bc?.run_metrics;
      const tf = bc?.timing_fields;
      const fsd = bc?.file_set_details;
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanDetailBenchmark', token, scanId] });
    },
  });

  useEffect(() => {
    setAppliedApiDefaults(false);
    setTotalDbSizeGb('');
    setTotalTables('');
    saveMutation.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId]);

  const existingBenchmarkId =
    (saveMutation.isSuccess ? saveMutation.data?.id : null) ??
    detail?.existing_benchmark?.id ??
    null;
  const benchmarkAlreadySaved = existingBenchmarkId != null;
  const savedAt = detail?.existing_benchmark?.updated_at;

  const backLink = (
    <Link
      to="/tools/scan-status"
      className="abs2-link"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontWeight: 600,
        color: 'var(--primary-color)',
        fontSize: '0.9rem',
        width: 'fit-content',
      }}
    >
      <ArrowLeft size={18} aria-hidden /> Scan Status
    </Link>
  );

  if (!scanId) {
    return (
      <div className="animate-in dashboard-root abs2" style={{ display: 'flex', flexDirection: 'column', gap: '1rem', maxWidth: '75rem' }}>
        <style>{SCOPED_CSS}</style>
        {backLink}
        <div className="abs2-card" style={{ ...cardPad }}>
          <p style={{ margin: 0, color: 'var(--text-secondary)' }}>
            Missing <code style={{ fontSize: '0.9em' }}>scan_id</code> in the URL. Use Open from Scan Status with a selected scan.
          </p>
        </div>
      </div>
    );
  }

  const rm = detail?.benchmark_context?.run_metrics;
  const tf = detail?.benchmark_context?.timing_fields;
  const fsd = detail?.benchmark_context?.file_set_details;
  const ev = detail?.benchmark_context?.effective_volumes;
  const ins = detail?.insights ?? {};
  const findings = detail?.benchmark_context?.findings_prefill ?? [];
  const preview = detail?.benchmark_context?.throughput_preview;

  const runDate =
    tf?.scan_end_date ??
    (detail?.parent?.completed_at ? String(detail.parent.completed_at).slice(0, 10) : undefined);

  const runningTime = runningTimeFromTiming(detail?.benchmark_context?.timing);
  const scanStatus = String(detail?.parent?.scan_status ?? '').trim();
  // Scan ended abnormally (completed with failures / stopped / paused) — flag the save action red.
  const scanEndedAbnormally = /fail|stop|paus|abort|cancel/i.test(scanStatus.replace(/\s+/g, ''));
  const volumeUnit = isStructured ? 'tables/hr' : 'files/hr';

  // Throughput for headline + tiles: prefer live (reacts to scanner/thread edits), else API preview.
  const tp: ThroughputSet | null = throughputLive
    ? throughputLive
    : preview
      ? {
          throughput_cluster_gb_hr: toNum(preview.throughput_cluster_gb_hr),
          throughput_cluster_files_hr: toNum(preview.throughput_cluster_files_hr),
          throughput_scanner_gb_hr: toNum(preview.throughput_scanner_gb_hr),
          throughput_scanner_files_hr: toNum(preview.throughput_scanner_files_hr),
          throughput_thread_gb_hr: toNum(preview.throughput_thread_gb_hr),
          throughput_thread_files_hr: toNum(preview.throughput_thread_files_hr),
        }
      : null;

  // Volumes for tiles
  const totalFilesDisplay = fsd?.total_files ?? ev?.total_files ?? null;
  const totalFoldersDisplay = fsd?.total_folders ?? ev?.total_folders ?? null;
  const totalSizeDisplay = displayTotalSizeGb ?? fsd?.total_size_gb ?? (typeof ev?.total_gb === 'number' ? ev.total_gb : null);
  const objectsScanned = toNum(ins.objects_scanned);
  const distinctClassifiers = toNum(ins.distinct_classification_attributes);

  // Findings: bar width relative to the largest finding; % relative to objects scanned (fallback: sum).
  const maxFinding = findings.reduce((m, r) => Math.max(m, toNum(r.Findings)), 0);
  const findingsSum = findings.reduce((s, r) => s + toNum(r.Findings), 0);
  const pctBase = objectsScanned > 0 ? objectsScanned : findingsSum;

  // Hero title/subtitle
  const heroTitle =
    [rm?.data_source_type, rm?.scenario_type].filter(Boolean).join(' ').trim()
      ? `${[rm?.data_source_type, rm?.scenario_type].filter(Boolean).join(' ')} Benchmark`
      : detail?.parent?.scan_name || 'Benchmark from Scan';
  // Hero subtitle: show the actual scan name (build/env/scan/file-set already live in Overview).
  const heroSubParts = [detail?.parent?.scan_name].filter(Boolean) as string[];

  return (
    <div className="animate-in dashboard-root abs2" style={{ display: 'flex', flexDirection: 'column', gap: '1.15rem', maxWidth: '78rem' }}>
      <style>{SCOPED_CSS}</style>
      {backLink}

      {/* ---------- HERO ---------- */}
      <div
        style={{
          borderRadius: 16,
          padding: '1.5rem 1.75rem',
          background: 'linear-gradient(120deg, #4f46e5 0%, #6366f1 55%, #7c7ff2 100%)',
          color: '#fff',
          display: 'flex',
          flexWrap: 'wrap',
          gap: '1rem',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          boxShadow: '0 10px 30px rgba(79, 70, 229, 0.28)',
        }}
      >
        <div style={{ display: 'flex', gap: '1rem', alignItems: 'center', minWidth: 0 }}>
          <span
            style={{
              width: 52,
              height: 52,
              borderRadius: 14,
              background: 'rgba(255,255,255,0.18)',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <Database size={26} color="#fff" aria-hidden />
          </span>
          <div style={{ minWidth: 0 }}>
            <h1 style={{ margin: 0, fontSize: '1.55rem', fontWeight: 800, letterSpacing: '-0.01em', lineHeight: 1.15 }}>{heroTitle}</h1>
            {heroSubParts.length > 0 && (
              <div style={{ marginTop: '0.4rem', fontSize: '0.9rem', color: 'rgba(255,255,255,0.9)', display: 'flex', flexWrap: 'wrap', gap: '0.2rem 0.55rem' }}>
                {heroSubParts.map((p, i) => (
                  <React.Fragment key={p}>
                    {i > 0 && <span style={{ opacity: 0.6 }}>•</span>}
                    <span>{p}</span>
                  </React.Fragment>
                ))}
              </div>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.7rem' }}>
          {scanStatus ? <StatusBadge status={scanStatus} /> : null}
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', color: 'rgba(255,255,255,0.92)' }}>
            <span>{benchmarkAlreadySaved ? `Benchmark ID: ${existingBenchmarkId}` : 'Not yet saved'}</span>
            <Bookmark size={16} fill={benchmarkAlreadySaved ? '#fff' : 'transparent'} color="#fff" aria-hidden />
          </div>
        </div>
      </div>

      {isLoading && (
        <div className="abs2-card" style={{ ...cardPad, display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-secondary)' }}>
          <Loader2 className="lucide-spin" size={22} /> Loading scan detail…
        </div>
      )}

      {error && (
        <div className="abs2-card" style={{ ...cardPad, color: 'var(--danger-color, #b91c1c)', fontSize: '0.9rem' }}>
          {(error as Error)?.message || 'Could not load scan detail.'}
        </div>
      )}

      {!isLoading && detail && !detail.parent && (
        <div
          className="abs2-card"
          style={{
            ...cardPad,
            background: 'rgba(220, 38, 38, 0.06)',
            border: '1px solid rgba(220, 38, 38, 0.25)',
            fontSize: '0.9rem',
            lineHeight: 1.55,
          }}
        >
          No <code style={{ fontSize: '0.85em' }}>scan_parents</code> row for this id in the DB your API uses. Sync this scan on Scan
          Status so timing and volumes are available.
        </div>
      )}

      {!isLoading && detail?.parent && (
        <>
          {/* ---------- KPI ROW ---------- */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))', gap: '1.15rem' }}>
            <KpiCard
              icon={Gauge}
              iconBg="#2563eb"
              value={tp ? tp.throughput_cluster_gb_hr.toFixed(2) : '—'}
              unit="GB/hr"
              label="Cluster throughput"
            />
            <KpiCard
              icon={Folder}
              iconBg="#2563eb"
              value={tp ? fmtInt(tp.throughput_cluster_files_hr) : '—'}
              unit={isStructured ? 'tables/hr' : 'files/hr'}
              label={isStructured ? 'Cluster tables throughput' : 'Cluster files throughput'}
            />
            <KpiCard
              icon={Clock}
              iconBg="#10b981"
              value={runningTime?.label ?? '—'}
              label="Total runtime"
            />
            {/* Save benchmark action lives in the KPI row (single source of truth). */}
            {benchmarkAlreadySaved ? (
              <div
                className="abs2-card abs2-kpi"
                style={{
                  ...cardPad,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '0.45rem',
                  minWidth: 0,
                  border: '1px solid rgba(16,185,129,0.4)',
                  background: 'rgba(16,185,129,0.06)',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <CheckCircle2 size={18} color="#10b981" aria-hidden />
                  <span style={{ fontSize: '0.68rem', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: '#047857' }}>
                    Benchmark saved
                  </span>
                </div>
                <div style={{ fontSize: '1.65rem', fontWeight: 800, color: 'var(--text-primary)', lineHeight: 1 }}>#{existingBenchmarkId}</div>
                {savedAt && <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)' }}>Saved {fmtTs(savedAt)}</div>}
                <Link
                  to="/tools/records-edit"
                  className="abs2-link"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 'auto', fontWeight: 700, color: 'var(--primary-color)', fontSize: '0.82rem' }}
                >
                  View Benchmark <ExternalLink size={14} aria-hidden />
                </Link>
              </div>
            ) : (
              <div className="abs2-card abs2-kpi" style={{ ...cardPad, display: 'flex', flexDirection: 'column', gap: '0.6rem', minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Database size={16} style={{ color: 'var(--primary-color)' }} aria-hidden />
                  <span style={{ fontSize: '0.68rem', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: 'var(--text-secondary)' }}>
                    Save benchmark
                  </span>
                </div>
                <button
                  type="button"
                  className="abs2-btn"
                  disabled={saveMutation.isPending || !tp || !detail?.parent}
                  onClick={() => { if (!saveMutation.isPending) saveMutation.mutate(); }}
                  style={{
                    padding: '0.6rem 1rem',
                    borderRadius: 9,
                    border: 'none',
                    background: scanEndedAbnormally ? 'var(--danger-color, #de350b)' : 'var(--primary-color, #0052cc)',
                    color: '#fff',
                    fontWeight: 700,
                    cursor: !tp || !detail?.parent || saveMutation.isPending ? 'not-allowed' : 'pointer',
                    fontFamily: 'inherit',
                    fontSize: '0.85rem',
                    opacity: !tp || !detail?.parent ? 0.6 : 1,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 8,
                    width: '100%',
                  }}
                >
                  {saveMutation.isPending ? (
                    <><Loader2 size={16} className="lucide-spin" /> Saving…</>
                  ) : (
                    <><Database size={16} /> Save Benchmark</>
                  )}
                </button>
                {saveMutation.isError && (
                  <span style={{ color: 'var(--danger-color)', fontSize: '0.76rem', lineHeight: 1.4 }}>
                    {axios.isAxiosError(saveMutation.error)
                      ? (() => {
                          const d = saveMutation.error.response?.data as { detail?: string | { message?: string } };
                          const det = d?.detail;
                          if (typeof det === 'string') return det;
                          if (det && typeof det === 'object' && det.message) return det.message;
                          return saveMutation.error.message;
                        })()
                      : String(saveMutation.error)}
                  </span>
                )}
                {!tp && detail?.parent && (
                  <span style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                    Need valid scan timing to calculate throughput before saving.
                  </span>
                )}
              </div>
            )}
          </div>

          {/* ---------- OVERVIEW + TIMELINE ---------- */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 360px), 1fr))', gap: '1.15rem', alignItems: 'start' }}>
            {/* Overview */}
            <div className="abs2-card" style={{ ...cardPad }}>
              <CardHeading icon={Database}>Overview</CardHeading>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, 1fr)', gap: '0 1.75rem' }}>
                <div>
                  <Field label="Environment">{detail.environment_name ?? '—'}</Field>
                  <Field label="Build version" mono>{rm?.build_version || '—'}</Field>
                  <Field label="Run date" mono>{runDate ?? '—'}</Field>
                  <Field label="Scenario type">{rm?.scenario_type || '—'}</Field>
                  <Field label="Scan type">{rm?.scan_type || '—'}</Field>
                  <Field label="Data source">{rm?.data_source_type || '—'}</Field>
                  <Field label="Data type">{rm?.data_type || '—'}</Field>
                  <Field label="File set type">{rm?.file_set_type || '—'}</Field>
                  <Field label="Source scan ID" mono>
                    <span style={{ fontSize: '0.78rem', wordBreak: 'break-all' }}>{scanId}</span>
                  </Field>
                  <Field label="Template">{detail.parent?.template_name || 'Data Source Scan'}</Field>
                  <Field label="Classifiers" mono>{distinctClassifiers > 0 ? distinctClassifiers : '—'}</Field>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.75rem', padding: '0.42rem 0', borderBottom: '1px solid var(--surface-border)' }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: '0.85rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
                      <Server size={15} aria-hidden /> Scanner count
                    </span>
                    {benchmarkAlreadySaved ? (
                      <span style={{ fontWeight: 700 }}>{scannerCount}</span>
                    ) : (
                      <input
                        type="number"
                        min={1}
                        value={scannerCount}
                        onChange={(e) => { const v = Math.floor(Number(e.target.value)); setScannerCount(Number.isFinite(v) && v >= 1 ? v : 1); }}
                        style={numInput}
                      />
                    )}
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.75rem', padding: '0.42rem 0', borderBottom: '1px solid var(--surface-border)' }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: '0.85rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
                      <Settings size={15} aria-hidden /> Total threads
                    </span>
                    {benchmarkAlreadySaved ? (
                      <span style={{ fontWeight: 700 }}>{threadCount}</span>
                    ) : (
                      <input
                        type="number"
                        min={1}
                        value={threadCount}
                        onChange={(e) => { const v = Math.floor(Number(e.target.value)); setThreadCount(Number.isFinite(v) && v >= 1 ? v : 1); }}
                        style={numInput}
                      />
                    )}
                  </div>
                  <Field label="ACL">
                    <span style={{ color: rm?.is_acl ? '#047857' : 'var(--text-secondary)' }}>{rm?.is_acl ? 'On' : 'Off'}</span>
                  </Field>
                  <Field label="Feature Flags (FF)">
                    <span style={{ color: rm?.is_ff ? '#047857' : 'var(--text-secondary)' }}>{rm?.is_ff ? 'On' : 'Off'}</span>
                  </Field>
                  {!benchmarkAlreadySaved && (
                    <p style={{ margin: '0.6rem 0 0', fontSize: '0.76rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                      Per-scanner and per-thread throughput derive from these counts.
                    </p>
                  )}
                </div>
              </div>
            </div>

            {/* Timeline */}
            <div className="abs2-card" style={{ ...cardPad }}>
              <CardHeading icon={Clock}>Scan timeline</CardHeading>
              <div>
                <TimelineRow label="Scan started" ts={fmtTs(detail.parent?.started_at)} kind="start" />
                <TimelineRow label="Enumeration started" ts={fmtTs(detail.parent?.started_at)} kind="dot" />
                <TimelineRow label="Scanning started" ts={fmtTs(detail.parent?.started_at)} kind="dot" />
                <TimelineRow label="Scan completed" ts={fmtTs(detail.parent?.completed_at)} kind="done" last />
              </div>
              {runningTime ? (
                <div
                  style={{
                    marginTop: '1.1rem',
                    padding: '0.85rem 1rem',
                    borderRadius: 10,
                    border: '1px solid rgba(16,185,129,0.4)',
                    background: 'rgba(16,185,129,0.08)',
                  }}
                >
                  <div style={{ fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '0.25rem' }}>Total runtime</div>
                  <div style={{ fontSize: '1.5rem', fontWeight: 800, color: '#047857', lineHeight: 1 }}>{runningTime.label}</div>
                </div>
              ) : (
                <div
                  style={{
                    marginTop: '1.1rem',
                    padding: '0.7rem 0.9rem',
                    borderRadius: 8,
                    border: '1px solid rgba(217, 119, 6, 0.4)',
                    background: 'rgba(251, 191, 36, 0.12)',
                    fontSize: '0.82rem',
                    color: '#92400e',
                    lineHeight: 1.5,
                  }}
                >
                  Running time unavailable — sync this scan so <code>started_at</code>/<code>completed_at</code> are set.
                </div>
              )}
            </div>
          </div>

          {/* ---------- DATASET SUMMARY + TOP FINDINGS ---------- */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 360px), 1fr))', gap: '1.15rem', alignItems: 'start' }}>
            {/* Dataset summary */}
            <div className="abs2-card" style={{ ...cardPad }}>
              <CardHeading icon={Database}>Dataset summary</CardHeading>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(90px, 1fr))', gap: '0.7rem' }}>
                <SummaryTile icon={FileText} color="#2563eb" value={fmtInt(totalFilesDisplay)} label={isStructured ? 'Total Tables' : 'Total Files'} />
                <SummaryTile icon={Folder} color="#2563eb" value={fmtInt(totalFoldersDisplay)} label={isStructured ? 'Total Schemas' : 'Total Folders'} />
                <SummaryTile icon={PieChart} color="#6366f1" value={totalSizeDisplay != null ? `${Number(totalSizeDisplay).toFixed(totalSizeDisplay >= 100 ? 0 : 1)} GB` : '—'} label="Total Size" />
                <SummaryTile icon={Server} color="#8b5cf6" value={String(scannerCount)} label="Scanners" />
                <SummaryTile icon={Settings} color="#8b5cf6" value={String(threadCount)} label="Threads" />
              </div>

              {isStructured && !benchmarkAlreadySaved && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', marginTop: '1rem', paddingTop: '0.9rem', borderTop: '1px solid var(--surface-border)' }}>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 5, fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                    Total DB Size (GB)
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={totalDbSizeGb}
                      onChange={(e) => setTotalDbSizeGb(e.target.value === '' ? '' : Number(e.target.value))}
                      placeholder="e.g. 20"
                      style={{ ...numInput, width: 120, textAlign: 'left', fontWeight: 600 }}
                    />
                  </label>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 5, fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                    Total Tables
                    <input
                      type="number"
                      min={0}
                      step={100}
                      value={totalTables}
                      onChange={(e) => setTotalTables(e.target.value === '' ? '' : Number(e.target.value))}
                      placeholder="e.g. 10000"
                      style={{ ...numInput, width: 120, textAlign: 'left', fontWeight: 600 }}
                    />
                  </label>
                </div>
              )}

              {totalFilesDisplay != null && (
                <p style={{ margin: '1rem 0 0', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                  Prefilled from scan_insights: {isStructured ? 'Total Tables' : 'Total Files'} = {fmtInt(totalFilesDisplay)}
                </p>
              )}
              {structuredSize.caption ? (
                <p style={{ margin: '0.3rem 0 0', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{structuredSize.caption}</p>
              ) : null}
            </div>

            {/* Top findings */}
            <div className="abs2-card" style={{ ...cardPad }}>
              <CardHeading icon={ClipboardList}>Top findings</CardHeading>
              {findings.length === 0 ? (
                <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                  No classifier findings yet — run Sync on Scan Progress so attributes land in Postgres.
                </p>
              ) : (
                <>
                  <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.83rem' }}>
                      <thead>
                        <tr style={{ color: 'var(--text-secondary)', textAlign: 'left' }}>
                          <th style={{ padding: '0 0 0.5rem', fontWeight: 600 }}>Classifier</th>
                          <th style={{ padding: '0 0 0.5rem', fontWeight: 600, width: '46%' }}>Findings</th>
                          <th style={{ padding: '0 0 0.5rem', fontWeight: 600, textAlign: 'right', whiteSpace: 'nowrap' }}>% of Total</th>
                        </tr>
                      </thead>
                      <tbody>
                        {findings.slice(0, 10).map((r, i) => {
                          const f = toNum(r.Findings);
                          const barPct = maxFinding > 0 ? (f / maxFinding) * 100 : 0;
                          const pctText = pctBase > 0 ? (f / pctBase) * 100 : 0;
                          const name = String(r['Classifier Attribute'] ?? '').replace(/^classifier\./i, '');
                          return (
                            <tr className="abs2-find" key={`${name}-${i}`}>
                              <td style={{ padding: '0.42rem 0.6rem 0.42rem 0', color: 'var(--text-primary)', fontWeight: 500 }}>{name || '—'}</td>
                              <td style={{ padding: '0.42rem 0.6rem 0.42rem 0' }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <span style={{ minWidth: 62, fontWeight: 600, color: 'var(--text-primary)' }}>{fmtInt(f)}</span>
                                  <span style={{ flex: 1, height: 8, borderRadius: 999, background: 'rgba(99,102,241,0.12)', overflow: 'hidden', minWidth: 40 }}>
                                    <span style={{ display: 'block', height: '100%', width: `${Math.max(2, barPct)}%`, background: 'linear-gradient(90deg,#6366f1,#818cf8)', borderRadius: 999 }} />
                                  </span>
                                </div>
                              </td>
                              <td style={{ padding: '0.42rem 0 0.42rem 0.6rem', textAlign: 'right', color: 'var(--text-secondary)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                                {pctText.toFixed(2)}%
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <Link
                    to={`/tools/scan-status?scan_id=${encodeURIComponent(scanId)}`}
                    className="abs2-link"
                    style={{ display: 'inline-block', marginTop: '0.9rem', fontWeight: 700, color: 'var(--primary-color)', fontSize: '0.85rem' }}
                  >
                    View all findings →
                  </Link>
                </>
              )}
            </div>
          </div>

          {/* ---------- CALCULATED THROUGHPUT (full width) ---------- */}
          <div className="abs2-card" style={{ ...cardPad }}>
            <CardHeading icon={BarChart3}>Calculated throughput</CardHeading>
            {tp ? (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '0.8rem' }}>
                <ThroughputTile label="Cluster (Total)" color="#2563eb" gbHr={tp.throughput_cluster_gb_hr} filesHr={tp.throughput_cluster_files_hr} volumeUnit={volumeUnit} />
                <ThroughputTile label="Per Scanner (Avg)" color="#10b981" gbHr={tp.throughput_scanner_gb_hr} filesHr={tp.throughput_scanner_files_hr} volumeUnit={volumeUnit} />
                <ThroughputTile label="Per Thread (Avg)" color="#8b5cf6" gbHr={tp.throughput_thread_gb_hr} filesHr={tp.throughput_thread_files_hr} volumeUnit={volumeUnit} />
              </div>
            ) : (
              <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                Need valid parent start/end times and volumes — sync scan_insights if totals are missing.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default AddBenchmarkFromScanPage;
