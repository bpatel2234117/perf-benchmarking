import React, { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
    Monitor, Server, Cpu, HardDrive,
    Loader2, AlertCircle, RefreshCcw, Info, Calendar, GitBranch, ScanLine
} from 'lucide-react';
import {
    LineChart, Line, XAxis, YAxis, CartesianGrid,
    Tooltip, Legend, ResponsiveContainer
} from 'recharts';
import { API_BASE } from '../apiConfig';

interface ScannerInfraPageProps { token: string; }

interface Env {
    id: number;
    env_name: string;
    is_active: boolean;
}

interface ScannerMetric {
    display_name: string;
    scanner_id: string;
    captured_at_utc: string;
    cpu_pct: number;
    memory_usage_pct: number;
    running_threads?: number;
    /** BigID service status ("Running" / "Failed" / …); "" on older schemas. */
    status?: string;
    /** Snapshot time minus BigID's last heartbeat for this scanner, in seconds. */
    heartbeat_lag_sec?: number | null;
}

interface ScopeScan {
    scan_id: string;
    step_id: number;
    seq: number;
    label: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
}

interface ScopeRun {
    run_id: number;
    name: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    scan_count: number;
    scans: ScopeScan[];
}

/**
 * Parse a UTC timestamp the API sends without a zone suffix ("2026-07-28T10:00:00" or
 * "2026-07-28 10:00:00"). `new Date(...)` would read those as *local* time, which skewed
 * every "last seen" label and any now-vs-sample freshness comparison by the browser offset.
 */
const parseUtc = (s?: string | null): number => {
    if (!s) return 0;
    let v = String(s).trim().replace(' ', 'T');
    if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = new Date(v).getTime();
    return Number.isNaN(t) ? 0 : t;
};

// The data in view is "live" only if the newest sample is this fresh. Tracker snapshots land
// on 20-minute slots, so anything older than ~2 slots is history, not the current fleet.
const FRESH_WINDOW_MS = 45 * 60 * 1000;

// A live scanner's BigID heartbeat lands within ~1 minute of the snapshot (measured: worst
// healthy lag ≈ 83s). A dead or replaced pod keeps being listed by services-status with its
// last values frozen — threads and CPU look busy while the heartbeat is hours old. Anything
// past this cutoff is not online, no matter what its thread count says.
const HEARTBEAT_STALE_SEC = 15 * 60;

// BigID service statuses that mean "up". Anything else reported (Failed, Error, Down…)
// is a failure even when the row still carries a stale thread count.
const HEALTHY_STATUSES = ['running', 'active', 'up', 'ok', 'healthy', 'online'];

const ScannerInfraPage: React.FC<ScannerInfraPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const [envId, setEnvId] = useState<number | null>(null);
    const [days, setDays] = useState(7);
    const [runId, setRunId] = useState<number | null>(null);
    // Free text: one or more scan IDs, comma- or space-separated.
    const [scanInput, setScanInput] = useState<string>('');
    const scanIds = useMemo(
        () => Array.from(new Set(scanInput.split(/[\s,]+/).map(s => s.trim()).filter(Boolean))),
        [scanInput]
    );
    // A run or scan defines its own time span server-side, so the History Window is ignored.
    const scoped = !!runId || scanIds.length > 0;

    // Fetch Environments
    const { data: envs = [] } = useQuery<Env[]>({
        queryKey: ['environments'],
        queryFn: async () => (await axios.get(`${API_BASE}/api/environments`, authConfig)).data,
    });

    // Set default env
    React.useEffect(() => {
        if (!envId && envs.length > 0) {
            const active = envs.find(e => e.is_active);
            if (active) setEnvId(active.id);
            else setEnvId(envs[0].id);
        }
    }, [envs, envId]);

    // Workflow runs / scans available on this env (drives the per-flow + per-scan filters)
    const { data: scopeData } = useQuery<{ runs: ScopeRun[] }>({
        queryKey: ['scanner-scopes', envId, days],
        queryFn: async () => {
            if (!envId) return { runs: [] };
            const res = await axios.get(`${API_BASE}/api/scanner-metrics/scopes`, {
                ...authConfig,
                params: { environment_id: envId, days }
            });
            return res.data;
        },
        enabled: !!envId,
    });

    const runs: ScopeRun[] = scopeData?.runs ?? [];
    const selectedRun = useMemo(() => runs.find(r => r.run_id === runId) ?? null, [runs, runId]);

    // Scan picker: scans of the chosen run, else every scan in the window (prefixed by run).
    const scanOptions = useMemo(() => {
        if (selectedRun) return selectedRun.scans.map(s => ({ ...s, prefix: '' }));
        return runs.flatMap(r => r.scans.map(s => ({ ...s, prefix: `#${r.run_id} · ` })));
    }, [runs, selectedRun]);

    // Changing env (or the window dropping a run) must not leave a dangling run selection.
    React.useEffect(() => {
        if (runId !== null && runs.length > 0 && !runs.some(r => r.run_id === runId)) {
            setRunId(null);
            setScanInput('');
        }
    }, [runs, runId]);

    // Fetch Metrics — scan scope wins over run scope; both ignore the days window server-side,
    // so days is neither sent nor keyed while scoped (sliding it must not refetch).
    const scanKey = scanIds.join(',');
    const { data, isLoading, isError, refetch } = useQuery({
        queryKey: ['scanner-performance', envId, scoped ? null : days, runId, scanKey],
        queryFn: async () => {
            if (!envId) return null;
            const res = await axios.get(`${API_BASE}/api/scanner-metrics/performance`, {
                ...authConfig,
                params: {
                    environment_id: envId,
                    ...(scoped ? {} : { days }),
                    ...(runId ? { run_id: runId } : {}),
                    ...(scanKey ? { scan_id: scanKey } : {}),
                },
            });
            return res.data;
        },
        enabled: !!envId,
    });

    const individualMetrics: ScannerMetric[] = data?.individual ?? [];
    const scope = data?.scope ?? null;
    const windowStart: string | null = data?.window_start ?? null;
    const windowEnd: string | null = data?.window_end ?? null;

    // Group data by display_name for Recharts
    const chartData = useMemo(() => {
        const timeMap: Record<string, any> = {};
        individualMetrics.forEach(m => {
            const ts = parseUtc(m.captured_at_utc);
            const time = new Date(ts).toLocaleString();
            if (!timeMap[time]) timeMap[time] = { time, timestamp: ts };
            timeMap[time][m.display_name + '_cpu'] = m.cpu_pct;
            timeMap[time][m.display_name + '_mem'] = m.memory_usage_pct;
        });
        return Object.values(timeMap).sort((a, b) => a.timestamp - b.timestamp);
    }, [individualMetrics]);

    const scannerNames = useMemo(() => {
        const names = new Set<string>();
        individualMetrics.forEach(m => names.add(m.display_name));
        return Array.from(names);
    }, [individualMetrics]);

    // Timestamp of the most recent tracker snapshot in the selected window. A scanner is part
    // of the *current* fleet only if its latest reading lands in this snapshot; older-but-absent
    // scanners are offline. Active vs Failed comes from BigID status + running_threads.
    const latestTs = useMemo(
        () => individualMetrics.reduce((mx, m) => Math.max(mx, parseUtc(m.captured_at_utc)), 0),
        [individualMetrics]
    );

    // Whether the newest sample in view is recent enough to describe the fleet right now.
    // Per-run / per-scan scopes are usually historical, so the header says so instead of
    // presenting a finished run's last snapshot as the current state.
    const isLiveWindow = latestTs > 0 && Date.now() - latestTs <= FRESH_WINDOW_MS;

    type ScannerStatus = 'ACTIVE' | 'IDLE' | 'FAILED' | 'OFFLINE';

    /**
     * State of one scanner at the end of the selected window. Statuses are derived
     * *relative to the snapshot*, so they stay meaningful for historical scopes.
     *
     * OFFLINE — absent from the final snapshot, or its BigID heartbeat had gone stale
     *           (a dead pod still listed with frozen threads/CPU — never call that online).
     * FAILED  — BigID explicitly reported a non-healthy status.
     * ACTIVE  — heartbeat current and running threads > 0.
     * IDLE    — heartbeat current, no threads (up, just no work in flight).
     */
    const deriveStatus = (last: ScannerMetric | undefined): ScannerStatus => {
        const inLatest = !!last && parseUtc(last.captured_at_utc) === latestTs;
        if (!inLatest || !last) return 'OFFLINE';
        const raw = (last.status ?? '').trim().toLowerCase();
        if (raw && !HEALTHY_STATUSES.includes(raw)) return 'FAILED';
        const lag = last.heartbeat_lag_sec;
        if (typeof lag === 'number' && lag > HEARTBEAT_STALE_SEC) return 'OFFLINE';
        return (last.running_threads ?? 0) > 0 ? 'ACTIVE' : 'IDLE';
    };

    const lastByName = useMemo(() => {
        const map: Record<string, ScannerMetric | undefined> = {};
        scannerNames.forEach(name => {
            const rows = individualMetrics.filter(m => m.display_name === name);
            map[name] = rows[rows.length - 1];
        });
        return map;
    }, [individualMetrics, scannerNames]);

    const statusByName = useMemo(() => {
        const map: Record<string, ScannerStatus> = {};
        scannerNames.forEach(name => { map[name] = deriveStatus(lastByName[name]); });
        return map;
    }, [lastByName, scannerNames, latestTs]);

    // "23h" / "12m" — how far behind BigID's heartbeat was on the last snapshot.
    const fmtLag = (sec?: number | null): string | null => {
        if (typeof sec !== 'number' || sec <= HEARTBEAT_STALE_SEC) return null;
        const h = Math.floor(sec / 3600);
        return h >= 1 ? `${h}h` : `${Math.round(sec / 60)}m`;
    };

    // Fleet tally. `active` counts only scanners with a current heartbeat and live threads;
    // failed / offline are reported separately so a degraded fleet can never look healthy.
    const fleet = useMemo(() => {
        let active = 0, threads = 0, idle = 0, failed = 0, offline = 0;
        scannerNames.forEach(name => {
            const st = statusByName[name];
            if (st === 'FAILED') failed += 1;
            else if (st === 'OFFLINE') offline += 1;
            else if (st === 'IDLE') idle += 1;
            else if (st === 'ACTIVE') {
                active += 1;
                threads += lastByName[name]?.running_threads ?? 0;
            }
        });
        return { active, threads, idle, failed, offline };
    }, [lastByName, scannerNames, statusByName]);

    const colors = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];

    const statusStyles: Record<ScannerStatus, { bg: string; fg: string }> = {
        ACTIVE: { bg: 'rgba(16,185,129,0.1)', fg: '#10b981' },
        IDLE: { bg: 'rgba(99,102,241,0.1)', fg: '#6366f1' },
        FAILED: { bg: 'rgba(239,68,68,0.1)', fg: '#ef4444' },
        OFFLINE: { bg: 'rgba(100,116,139,0.1)', fg: '#64748b' },
    };

    const renderChart = (type: 'cpu' | 'mem', title: string, icon: React.ReactNode, _yLabel: string, yDomain: [number, number | string]) => {
        if (isLoading) return <div style={{ height: 350, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Loader2 className="lucide-spin" size={32} /></div>;
        if (chartData.length === 0) return <div style={{ height: 350, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)' }}><Info size={20} style={{ marginRight: 8 }} /> No data available for this period.</div>;

        return (
            <div className="glass-panel" style={{ padding: '1.5rem', flex: 1 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1.5rem' }}>
                    <div style={{ padding: '0.5rem', background: 'rgba(99,102,241,0.1)', borderRadius: 10, color: 'var(--primary-color)' }}>{icon}</div>
                    <h3 style={{ margin: 0, fontSize: '1.1rem' }}>{title}</h3>
                </div>
                <div style={{ height: 350, width: '100%' }}>
                    <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={chartData}>
                            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                            {/* Scoped to one run/scan the timeline is short enough to label. */}
                            <XAxis
                                dataKey="timestamp"
                                hide={!runId && scanIds.length === 0}
                                type="number"
                                domain={['dataMin', 'dataMax']}
                                scale="time"
                                fontSize={11}
                                tickLine={false}
                                minTickGap={40}
                                tickFormatter={(t: number) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                            />
                            <YAxis domain={yDomain} unit="%" fontSize={12} tickLine={false} axisLine={false} />
                            <Tooltip
                                contentStyle={{ borderRadius: 12, border: 'none', boxShadow: '0 10px 15px -3px rgba(0,0,0,0.1)' }}
                                labelFormatter={(t: any) => (typeof t === 'number' ? new Date(t).toLocaleString() : String(t))}
                                formatter={(v: any) => (typeof v === 'number' ? `${v.toFixed(1)}%` : v)}
                            />
                            <Legend verticalAlign="bottom" height={36} />
                            {scannerNames.map((name, idx) => {
                                // Keep failed/offline samples in the historical chart so
                                // outages, recovery, and replacement scanners remain visible.
                                const st = statusByName[name];
                                const degraded = st === 'FAILED' || st === 'OFFLINE';
                                return (
                                    <Line
                                        key={name}
                                        type="monotone"
                                        dataKey={`${name}_${type}`}
                                        name={degraded ? `${name} (${st.toLowerCase()})` : name}
                                        stroke={degraded ? '#cbd5e1' : colors[idx % colors.length]}
                                        strokeWidth={2}
                                        strokeDasharray={degraded ? '4 3' : undefined}
                                        dot={false}
                                        activeDot={{ r: 6 }}
                                    />
                                );
                            })}
                        </LineChart>
                    </ResponsiveContainer>
                </div>
            </div>
        );
    };

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', paddingBottom: '4rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                    <Monitor size={28} style={{ color: 'var(--primary-color)' }} />
                    <h2 style={{ margin: 0, fontSize: '1.5rem', fontWeight: 800 }}>Scanner Infrastructure</h2>
                </div>
                <button className="btn" onClick={() => refetch()} style={{ padding: '0.5rem 1rem', background: '#fff' }}>
                    <RefreshCcw size={16} style={{ marginRight: 8 }} /> Refresh
                </button>
            </div>

            <p style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-secondary)', maxWidth: 800 }}>
                Real-time performance metrics for BigID scanners across environments. CPU data reflects system-wide load, and memory usage is relative to physical allocation.
                Narrow the view to a single workflow run or one scan to see only the samples captured inside that window.
            </p>

            <div className="glass-panel" style={{ padding: '1.25rem', display: 'flex', flexWrap: 'wrap', gap: '2rem', alignItems: 'center' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <label style={{ fontSize: '0.7rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>Target Environment</label>
                    <select
                        style={{ padding: '0.6rem 1rem', borderRadius: 10, border: '1px solid var(--surface-border)', minWidth: 220, fontSize: '0.9rem' }}
                        value={envId || ''}
                        onChange={e => setEnvId(Number(e.target.value))}
                    >
                        {envs.map(e => <option key={e.id} value={e.id}>{e.env_name} {e.is_active ? '(Active)' : ''}</option>)}
                    </select>
                </div>

                {/* Per-workflow scope. Runs are those with steps on the selected environment. */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <label style={{ fontSize: '0.7rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 6 }}>
                        <GitBranch size={12} /> Workflow Run
                    </label>
                    <select
                        style={{ padding: '0.6rem 1rem', borderRadius: 10, border: '1px solid var(--surface-border)', minWidth: 240, fontSize: '0.9rem' }}
                        value={runId ?? ''}
                        onChange={e => { const v = e.target.value; setRunId(v ? Number(v) : null); setScanInput(''); }}
                    >
                        <option value="">All runs (time window)</option>
                        {runs.map(r => (
                            <option key={r.run_id} value={r.run_id}>
                                #{r.run_id} {r.name || 'workflow'} — {r.status || 'n/a'} ({r.scan_count} scans)
                            </option>
                        ))}
                    </select>
                </div>

                {/* Per-scan scope; overrides the run window when set. */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <label style={{ fontSize: '0.7rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 6 }}>
                        <ScanLine size={12} /> Scan
                    </label>
                    <input
                        type="text"
                        list="scanner-scan-options"
                        placeholder={selectedRun ? 'All scans in run — or paste scan ID(s)' : 'All scans — or paste scan ID(s)'}
                        value={scanInput}
                        onChange={e => setScanInput(e.target.value)}
                        style={{ padding: '0.6rem 1rem', borderRadius: 10, border: '1px solid var(--surface-border)', minWidth: 260, fontSize: '0.9rem' }}
                    />
                    <datalist id="scanner-scan-options">
                        {scanOptions.map(s => (
                            <option key={s.scan_id} value={s.scan_id}>
                                {s.prefix}{s.seq}. {s.label}{s.status ? ` — ${s.status}` : ''}
                            </option>
                        ))}
                    </datalist>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flex: 1, minWidth: 200 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', opacity: scoped ? 0.5 : 1 }}>
                        <label style={{ fontSize: '0.7rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>History Window</label>
                        <span style={{ fontSize: '0.85rem', fontWeight: 600 }}>{scoped ? 'Not used' : `${days} Days`}</span>
                    </div>
                    <input
                        type="range" min="1" max="30" value={days}
                        disabled={scoped}
                        onChange={e => setDays(Number(e.target.value))}
                        style={{ width: '100%', cursor: scoped ? 'not-allowed' : 'pointer', accentColor: 'var(--primary-color)', opacity: scoped ? 0.5 : 1 }}
                    />
                    {scoped && (
                        <span style={{ fontSize: '0.7rem', color: '#b45309' }}>
                            Scoped to {scanIds.length > 0 ? `${scanIds.length} scan${scanIds.length > 1 ? 's' : ''}` : 'a run'} — clear the selection to use the history window.
                        </span>
                    )}
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginTop: 'auto', paddingBottom: 4 }}>
                    <Calendar size={16} style={{ color: '#64748b' }} />
                    <span style={{ fontSize: '0.85rem', color: '#64748b' }}>
                        {windowStart && windowEnd
                            ? `${new Date(parseUtc(windowStart)).toLocaleString()} – ${new Date(parseUtc(windowEnd)).toLocaleString()}`
                            : scoped
                                ? 'Loading scope window…'
                                : `${new Date(Date.now() - days * 86400000).toLocaleDateString()} – ${new Date().toLocaleDateString()}`}
                    </span>
                </div>
            </div>

            {scope && scope.scope !== 'days' && (
                <div className="glass-panel" style={{ padding: '0.85rem 1.25rem', display: 'flex', alignItems: 'center', gap: '0.75rem', fontSize: '0.85rem' }}>
                    <Info size={16} style={{ color: 'var(--primary-color)' }} />
                    <span>
                        Showing scanner metrics for <strong>{scope.label}</strong>
                        {scope.is_running ? ' (still running)' : ''} · {individualMetrics.length} samples
                    </span>
                </div>
            )}

            {isError && (
                <div style={{ padding: '2rem', textAlign: 'center', background: 'rgba(239,68,68,0.05)', borderRadius: 12, border: '1px solid #ef4444', color: '#ef4444' }}>
                    <AlertCircle size={32} style={{ marginBottom: '0.75rem' }} />
                    <p style={{ margin: 0 }}>Failed to load scanner metrics. Please check background tracker status.</p>
                </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1.5rem' }}>
                    {renderChart('cpu', 'CPU Utilization', <Cpu size={20} />, 'CPU %', [0, 'auto'])}
                    {renderChart('mem', 'Memory Usage', <HardDrive size={20} />, 'Mem %', [0, 100])}
                </div>
            </div>

            <div className="glass-panel" style={{ padding: '1.5rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1.25rem' }}>
                    <Server size={20} style={{ color: 'var(--primary-color)' }} />
                    <h3 style={{ margin: 0, fontSize: '1.1rem' }}>Scanner Detail</h3>
                    {latestTs > 0 && (
                        <span style={{ marginLeft: 'auto', fontSize: '0.85rem', color: 'var(--text-secondary)', display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
                            <span style={{ color: isLiveWindow ? undefined : '#b45309' }}>
                                {isLiveWindow ? 'As of' : 'Historical — as of'} {new Date(latestTs).toLocaleString()}
                            </span>
                            <span><strong style={{ color: '#10b981' }}>{fleet.active}</strong> active · <strong>{fleet.threads}</strong> threads</span>
                            {fleet.idle > 0 && <span><strong>{fleet.idle}</strong> idle</span>}
                            {fleet.failed > 0 && <span style={{ color: '#ef4444' }}><strong>{fleet.failed}</strong> failed</span>}
                            {fleet.offline > 0 && <span><strong>{fleet.offline}</strong> offline</span>}
                        </span>
                    )}
                </div>
                <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                        <thead style={{ background: '#f8fafc' }}>
                            <tr>
                                <th style={{ textAlign: 'left', padding: '12px 16px' }}>Scanner Name</th>
                                <th style={{ textAlign: 'left', padding: '12px 16px' }}>Status</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>Threads</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>CPU Avg</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>Mem Avg</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>Last Seen</th>
                            </tr>
                        </thead>
                        <tbody>
                            {scannerNames.map(name => {
                                const metrics = individualMetrics.filter(m => m.display_name === name);
                                const last = lastByName[name];
                                const avgCpu = metrics.reduce((acc, m) => acc + m.cpu_pct, 0) / metrics.length;
                                const avgMem = metrics.reduce((acc, m) => acc + m.memory_usage_pct, 0) / metrics.length;
                                const status = statusByName[name];
                                const running = last?.running_threads ?? 0;
                                return { name, last, avgCpu, avgMem, status, running, statusStyle: statusStyles[status] };
                            })
                                .sort((a, b) => {
                                    const rank = { ACTIVE: 0, IDLE: 1, FAILED: 2, OFFLINE: 3 } as Record<string, number>;
                                    return rank[a.status] - rank[b.status] || a.name.localeCompare(b.name);
                                })
                                .map(({ name, last, avgCpu, avgMem, status, running, statusStyle }) => (
                                    <tr key={name} style={{ borderTop: '1px solid var(--surface-border)', opacity: status === 'OFFLINE' ? 0.65 : 1 }}>
                                        <td style={{ padding: '12px 16px', fontWeight: 600 }}>{name}</td>
                                        <td style={{ padding: '12px 16px' }}>
                                            <span style={{
                                                padding: '2px 8px', borderRadius: 6, fontSize: '0.75rem', fontWeight: 700,
                                                background: statusStyle.bg, color: statusStyle.fg
                                            }}>
                                                {status}
                                            </span>
                                            {/* Explains an offline scanner that still reports threads/CPU. */}
                                            {fmtLag(last?.heartbeat_lag_sec) && (
                                                <div style={{ fontSize: '0.7rem', color: '#94a3b8', marginTop: 2 }}>
                                                    heartbeat {fmtLag(last?.heartbeat_lag_sec)} behind
                                                </div>
                                            )}
                                        </td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>{status === 'OFFLINE' ? '—' : running}</td>
                                        {/* Averages describe the selected window, so they stay meaningful for
                                            historical (per-run / per-scan) scopes; only offline rows have none. */}
                                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>{status === 'OFFLINE' ? '—' : avgCpu.toFixed(1) + '%'}</td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>{status === 'OFFLINE' ? '—' : avgMem.toFixed(1) + '%'}</td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right', color: '#64748b' }}>{last ? new Date(parseUtc(last.captured_at_utc)).toLocaleString() : 'N/A'}</td>
                                    </tr>
                                ))}
                            {scannerNames.length === 0 && !isLoading && (
                                <tr><td colSpan={6} style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>No scanner samples in this window.</td></tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
};

export default ScannerInfraPage;
