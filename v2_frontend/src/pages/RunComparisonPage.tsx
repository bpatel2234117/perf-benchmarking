import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, GitCompare } from 'lucide-react';
import {
    ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
} from 'recharts';
import { API_BASE } from '../apiConfig';

interface RunComparisonPageProps { token: string; }

interface BenchmarkRow {
    id: number;
    build_version: string;
    run_date: string | null;
    scenario_type: string | null;
    scan_type: string | null;
    data_source_type: string | null;
    file_set_type: string | null;
    total_scan_hours: number | null;
    throughput_cluster_gb_hr: number | null;
    throughput_scanner_gb_hr: number | null;
    throughput_thread_gb_hr: number | null;
    throughput_cluster_files_hr: number | null;
    throughput_scanner_files_hr: number | null;
    throughput_thread_files_hr: number | null;
    scanner_count: number | null;
    thread_count: number | null;
    is_acl: boolean;
    is_ocr: boolean;
    is_feature_flags: boolean;
    feature_flags: string | null;
    total_catalog_items: number | null;
}

type FlagFilter = 'All' | 'On' | 'Off';

function applyFlag(rows: BenchmarkRow[], field: keyof BenchmarkRow, f: FlagFilter): BenchmarkRow[] {
    if (f === 'All') return rows;
    return rows.filter(r => f === 'On' ? r[field] : !r[field]);
}

function fmt(v: number | null | undefined, dec = 2): string {
    if (v == null || isNaN(Number(v))) return '—';
    return Number(v).toFixed(dec);
}

function pctDelta(a: number | null, b: number | null): string {
    if (a == null || b == null || Math.abs(b) < 1e-9) return '—';
    const p = ((a - b) / b) * 100;
    const sign = p >= 0 ? '+' : '';
    return `${sign}${p.toFixed(1)}%`;
}

const sel: React.CSSProperties = {
    padding: '0.45rem 0.65rem', borderRadius: 6,
    border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%',
};

const RunComparisonPage: React.FC<RunComparisonPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };

    const { data, isLoading } = useQuery({
        queryKey: ['benchmarksList', token],
        queryFn: async () => {
            const res = await axios.get<{ items: BenchmarkRow[]; total: number }>(
                `${API_BASE}/api/benchmarks/list`, { ...authConfig, params: { limit: 2000 } }
            );
            return res.data.items;
        },
    });

    const rows = data ?? [];

    const uniq = (field: keyof BenchmarkRow) =>
        [...new Set(rows.map(r => String(r[field] ?? '')).filter(Boolean))].sort();

    const [verA, setVerA] = useState('');
    const [dsA, setDsA] = useState('');
    const [scanA, setScanA] = useState('');
    const [fsA, setFsA] = useState('');
    const [aclA, setAclA] = useState<FlagFilter>('All');
    const [ffA, setFfA] = useState<FlagFilter>('All');

    const [verB, setVerB] = useState('');
    const [dsB, setDsB] = useState('');
    const [scanB, setScanB] = useState('');
    const [fsB, setFsB] = useState('');
    const [aclB, setAclB] = useState<FlagFilter>('All');
    const [ffB, setFfB] = useState<FlagFilter>('All');

    // Show major versions only (e.g. "253" covers "253.5" and "253.11")
    const majorOf = (v: string) => (v || '').split('.')[0].trim();
    const versions = useMemo(() => {
        const majors = new Set(rows.map(r => majorOf(String(r.build_version ?? ''))).filter(Boolean));
        return [...majors].sort((a, b) => parseInt(b, 10) - parseInt(a, 10));
    }, [rows]);

    const filterRows = (ver: string, ds: string, scan: string, fs: string, acl: FlagFilter, ff: FlagFilter) => {
        let r = rows;
        // Match all sub-versions of the selected major (e.g. 253 matches 253.5 and 253.11)
        if (ver) r = r.filter(x => majorOf(String(x.build_version ?? '')) === ver);
        if (ds) r = r.filter(x => x.data_source_type === ds);
        if (scan) r = r.filter(x => x.scan_type === scan);
        if (fs) r = r.filter(x => x.file_set_type === fs);
        r = applyFlag(r, 'is_acl', acl);
        r = applyFlag(r, 'is_feature_flags', ff);
        return r;
    };

    // Pick best throughput from matched rows (not most recent)
    const pickBest = (r: BenchmarkRow[]): BenchmarkRow | null => {
        if (!r.length) return null;
        return r.reduce((best, cur) =>
            (cur.throughput_cluster_gb_hr ?? -1) > (best.throughput_cluster_gb_hr ?? -1) ? cur : best
        );
    };

    const rowsA = useMemo(() => filterRows(verA, dsA, scanA, fsA, aclA, ffA), [rows, verA, dsA, scanA, fsA, aclA, ffA]);
    const rowsB = useMemo(() => filterRows(verB, dsB, scanB, fsB, aclB, ffB), [rows, verB, dsB, scanB, fsB, aclB, ffB]);

    const ra = pickBest(rowsA);
    const rb = pickBest(rowsB);

    const _isStructuredCmp = [dsA, dsB].some(d => ['postgres','mssql','mysql','oracle'].includes((d ?? '').toLowerCase()));
    const _fUnitCmp = _isStructuredCmp ? 'Tables' : 'Files';
    const metrics = [
        { label: 'Cluster GB/hr', a: ra?.throughput_cluster_gb_hr, b: rb?.throughput_cluster_gb_hr },
        { label: 'Scanner GB/hr', a: ra?.throughput_scanner_gb_hr, b: rb?.throughput_scanner_gb_hr },
        { label: 'Thread GB/hr', a: ra?.throughput_thread_gb_hr, b: rb?.throughput_thread_gb_hr },
        { label: `Cluster ${_fUnitCmp}/hr`, a: ra?.throughput_cluster_files_hr, b: rb?.throughput_cluster_files_hr },
        { label: `Scanner ${_fUnitCmp}/hr`, a: ra?.throughput_scanner_files_hr, b: rb?.throughput_scanner_files_hr },
        { label: 'Scan Hours', a: ra?.total_scan_hours, b: rb?.total_scan_hours },
    ];

    const chartData = metrics.slice(0, 3).map(m => ({
        name: m.label,
        Baseline: m.b ?? 0,
        Candidate: m.a ?? 0,
    }));

    const dsOptions = (ver: string) => [...new Set(rows.filter(r => !ver || majorOf(String(r.build_version ?? '')) === ver).map(r => r.data_source_type ?? '').filter(Boolean))].sort();
    const scanOptions = (ver: string, ds: string) => [...new Set(rows.filter(r => (!ver || majorOf(String(r.build_version ?? '')) === ver) && (!ds || r.data_source_type === ds)).map(r => r.scan_type ?? '').filter(Boolean))].sort();
    const fsOptions = (ver: string, ds: string, scan: string) => [...new Set(rows.filter(r => (!ver || majorOf(String(r.build_version ?? '')) === ver) && (!ds || r.data_source_type === ds) && (!scan || r.scan_type === scan)).map(r => r.file_set_type ?? '').filter(Boolean))].sort();

    const colStyle: React.CSSProperties = { flex: '1 1 340px', minWidth: 280 };
    const labelStyle: React.CSSProperties = { fontSize: '0.78rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 2 };

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <GitCompare size={26} style={{ color: 'var(--primary-color)' }} />
                <h2 style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>Run Comparison Tool</h2>
            </div>
            <p style={{ margin: 0, fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                Head-to-head comparison of two benchmark configurations. Select filters for each side — the most recent matching run is used.
            </p>

            {isLoading && <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={28} /></div>}

            {!isLoading && (
                <>
                    <div style={{ display: 'flex', gap: '1.25rem', flexWrap: 'wrap' }}>
                        {/* Side A */}
                        {[
                            { label: '🅰️ Baseline', ver: verA, setVer: setVerA, ds: dsA, setDs: setDsA, scan: scanA, setScan: setScanA, fs: fsA, setFs: setFsA, acl: aclA, setAcl: setAclA, ff: ffA, setFf: setFfA },
                            { label: '🅱️ Candidate', ver: verB, setVer: setVerB, ds: dsB, setDs: setDsB, scan: scanB, setScan: setScanB, fs: fsB, setFs: setFsB, acl: aclB, setAcl: setAclB, ff: ffB, setFf: setFfB },
                        ].map(({ label, ver, setVer, ds, setDs, scan, setScan, fs, setFs, acl, setAcl, ff, setFf }) => (
                            <div key={label} style={colStyle} className="glass-panel">
                                <div style={{ fontWeight: 700, fontSize: '1rem', marginBottom: '0.75rem', color: 'var(--primary-color)' }}>{label}</div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
                                    {[
                                        { lbl: 'Build Version', val: ver, set: setVer, opts: versions },
                                        { lbl: 'Data Source', val: ds, set: setDs, opts: dsOptions(ver) },
                                        { lbl: 'Scan Type', val: scan, set: setScan, opts: scanOptions(ver, ds) },
                                        { lbl: 'File Set Type', val: fs, set: setFs, opts: fsOptions(ver, ds, scan) },
                                    ].map(({ lbl, val, set, opts }) => (
                                        <div key={lbl} className="input-group" style={{ margin: 0 }}>
                                            <label style={labelStyle}>{lbl}</label>
                                            <select value={val} onChange={e => set(e.target.value)} style={sel}>
                                                <option value="">All</option>
                                                {opts.map(o => <option key={o} value={o}>{o}</option>)}
                                            </select>
                                        </div>
                                    ))}
                                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                                        {[
                                            { lbl: 'ACL', val: acl, set: setAcl },
                                            { lbl: 'FF', val: ff, set: setFf },
                                        ].map(({ lbl, val, set }) => (
                                            <div key={lbl} className="input-group" style={{ margin: 0, flex: 1 }}>
                                                <label style={labelStyle}>{lbl}</label>
                                                <select value={val} onChange={e => set(e.target.value as FlagFilter)} style={sel}>
                                                    {['All', 'On', 'Off'].map(o => <option key={o} value={o}>{o}</option>)}
                                                </select>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>

                    {/* Results */}
                    {(ra || rb) && (
                        <div className="glass-panel" style={{ padding: '1.25rem' }}>
                            <h3 style={{ margin: '0 0 1rem', fontSize: '1rem', fontWeight: 700 }}>Comparison Results</h3>
                            <div style={{ overflowX: 'auto' }}>
                                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.88rem' }}>
                                    <thead>
                                        <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                                            <th style={{ padding: '8px 12px', textAlign: 'left' }}>Metric</th>
                                            <th style={{ padding: '8px 12px', textAlign: 'right' }}>🅱️ Baseline</th>
                                            <th style={{ padding: '8px 12px', textAlign: 'right' }}>🅰️ Candidate</th>
                                            <th style={{ padding: '8px 12px', textAlign: 'right' }}>Delta (Cand vs Base)</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {metrics.map(m => {
                                            const delta = pctDelta(m.a, m.b);
                                            const isPos = delta.startsWith('+');
                                            const isNeg = delta.startsWith('-');
                                            return (
                                                <tr key={m.label} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                                                    <td style={{ padding: '8px 12px', fontWeight: 600 }}>{m.label}</td>
                                                    <td style={{ padding: '8px 12px', textAlign: 'right' }}>{fmt(m.b)}</td>
                                                    <td style={{ padding: '8px 12px', textAlign: 'right' }}>{fmt(m.a)}</td>
                                                    <td style={{
                                                        padding: '8px 12px', textAlign: 'right', fontWeight: 600,
                                                        color: isPos ? '#16a34a' : isNeg ? '#dc2626' : 'inherit'
                                                    }}>
                                                        {delta}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                        <tr style={{ borderBottom: '1px solid var(--surface-border)', background: 'rgba(0,0,0,0.02)' }}>
                                            <td style={{ padding: '8px 12px', fontWeight: 600 }}>Scanners / Threads</td>
                                            <td style={{ padding: '8px 12px', textAlign: 'right' }}>{rb ? `${rb.scanner_count ?? '—'} / ${rb.thread_count ?? '—'}` : '—'}</td>
                                            <td style={{ padding: '8px 12px', textAlign: 'right' }}>{ra ? `${ra.scanner_count ?? '—'} / ${ra.thread_count ?? '—'}` : '—'}</td>
                                            <td style={{ padding: '8px 12px', textAlign: 'right' }}>—</td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>

                            <div style={{ marginTop: '1.5rem', height: 280 }}>
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                                        <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                                        <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                                        <YAxis tick={{ fontSize: 11 }} />
                                        <Tooltip />
                                        <Legend />
                                        <Bar dataKey="Baseline" fill="#6366f1" />
                                        <Bar dataKey="Candidate" fill="#10b981" />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>

                            {rowsA.length > 1 && (
                                <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: '0.5rem' }}>
                                    ℹ️ {rowsA.length} runs match Candidate criteria — using most recent.
                                </p>
                            )}
                            {rowsB.length > 1 && (
                                <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                                    ℹ️ {rowsB.length} runs match Baseline criteria — using most recent.
                                </p>
                            )}
                        </div>
                    )}

                    {!ra && !rb && (
                        <div className="glass-panel" style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
                            Select filters above to compare two configurations.
                        </div>
                    )}
                </>
            )}
        </div>
    );
};

export default RunComparisonPage;
