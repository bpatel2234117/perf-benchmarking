import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, BarChart2, ChevronRight, ChevronDown } from 'lucide-react';
import {
    ResponsiveContainer,
    BarChart,
    Bar,
    XAxis,
    YAxis,
    CartesianGrid,
    Tooltip,
} from 'recharts';
import { API_BASE } from '../apiConfig';

interface BuildAnalysisPageProps {
    token: string;
}

interface BenchmarkRow {
    id: number;
    build_version: string;
    is_release: boolean;
    data_source_type: string | null;
    file_set_type: string | null;
    scan_type: string;
    is_acl: boolean;
    is_feature_flags: boolean;
    feature_flags: string;
    throughput_cluster_gb_hr: number | null;
    throughput_cluster_files_hr: number | null;
    throughput_scanner_gb_hr: number | null;
    throughput_thread_gb_hr: number | null;
    total_scan_hours: number | null;
    objects_scanned: number | null;
    run_date: string | null;
    scan_id: string | null;
    scan_name?: string | null;
    scenario_label?: string | null;
}

interface BuildOption {
    version: string;
    count: number;
}

// ── Scenario classification (mirrors dashboard.py _classify_scenario) ────────
function classifyScenario(row: BenchmarkRow): string {
    if (row.scenario_label) return row.scenario_label;
    const scanType = (row.scan_type || '').toLowerCase();
    const text = `${row.scan_type || ''} ${row.feature_flags || ''} ${row.scan_name || ''}`.toLowerCase();
    const hasOls = scanType.includes('ols');
    const isAcl = row.is_acl || (text.includes('acl') && !text.includes('without acl') && !text.includes('no acl'));
    const isFf = row.is_feature_flags || text.includes('structured discovery') || text.includes('structured file');
    if (hasOls && isAcl && isFf) return 'ACL + OLS + Structured FF';
    if (hasOls && isAcl) return 'ACL + OLS Scan';
    if (hasOls && isFf) return 'OLS + Structured FF';
    if (hasOls) return 'OLS Scan';
    if (isAcl && isFf) return 'ACL + Structured FF';
    if (isAcl) return 'ACL On';
    if (isFf) return 'Structured FF';
    return 'Base (Full Scan)';
}

const SCENARIO_ORDER = [
    'Base (Full Scan)',
    'ACL On',
    'Structured FF',
    'ACL + Structured FF',
    'OLS Scan',
    'ACL + OLS Scan',
    'OLS + Structured FF',
    'ACL + OLS + Structured FF',
];

function majorOf(v: string): string {
    return (v || '').split('.')[0];
}

function sortKey(v: string): number {
    const parts = (v || '').split('.');
    const major = parseInt(parts[0] || '0', 10) || 0;
    const minor = parseInt(parts[1] || '0', 10) || 0;
    return major * 10000 + minor;
}

function fileSetRank(fs: string): number {
    const s = (fs || '').toLowerCase();
    if (s.includes('small')) return 0;
    if (s.includes('large')) return 1;
    if (s.includes('csv')) return 2;
    return 3;
}

function fmt(v: number | null | undefined, dec = 2): string {
    if (v == null || isNaN(Number(v))) return '—';
    return Number(v).toFixed(dec);
}

function fmtPct(v: number | null): string {
    if (v == null) return '';
    const sign = v >= 0 ? '+' : '';
    return `${sign}${v.toFixed(1)}%`;
}

function pctDelta(cur: number | null, prev: number | null): number | null {
    if (cur == null || prev == null || Math.abs(prev) < 1e-9) return null;
    return ((cur - prev) / prev) * 100;
}

// ── Best row per (ds, fileSet, scenario) ─────────────────────────────────────
function bestRows(rows: BenchmarkRow[]): Map<string, BenchmarkRow> {
    const m = new Map<string, BenchmarkRow>();
    for (const r of rows) {
        const scen = classifyScenario(r);
        const key = `${r.data_source_type}||${r.file_set_type}||${scen}`;
        const cur = m.get(key);
        if (!cur) { m.set(key, r); continue; }
        const curTp = cur.throughput_cluster_gb_hr ?? -1;
        const newTp = r.throughput_cluster_gb_hr ?? -1;
        if (newTp > curTp) m.set(key, r);
    }
    return m;
}

// ── Trend chart data ──────────────────────────────────────────────────────────
function buildTrendData(
    allRows: BenchmarkRow[],
    ds: string,
    fileSetFilter: string,
): { build: string; gb: number | null; files: number | null }[] {
    const filtered = allRows.filter(r =>
        r.data_source_type === ds &&
        (fileSetFilter === '' || r.file_set_type === fileSetFilter) &&
        !r.is_acl && !r.is_feature_flags &&
        !(r.scan_type || '').toLowerCase().includes('ols')
    );
    // Group by major version (e.g. "253" covers "253.5" and "253.11"), pick highest throughput.
    const byMajor = new Map<string, BenchmarkRow>();
    for (const r of filtered) {
        const major = majorOf(r.build_version);
        const cur = byMajor.get(major);
        if (!cur || (r.throughput_cluster_gb_hr ?? -1) > (cur.throughput_cluster_gb_hr ?? -1)) {
            byMajor.set(major, r);
        }
    }
    return [...byMajor.entries()]
        .sort((a, b) => sortKey(a[0]) - sortKey(b[0]))
        .map(([major, r]) => ({
            build: major,
            gb: r.throughput_cluster_gb_hr,
            files: r.throughput_cluster_files_hr,
        }));
}

// ── Component ─────────────────────────────────────────────────────────────────
const BuildAnalysisPage: React.FC<BuildAnalysisPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };

    const [selectedBuild, setSelectedBuild] = useState<string>('');
    const [activeTab, setActiveTab] = useState<'matrix' | 'trend' | 'overview'>('matrix');
    const [showOverviewTable, setShowOverviewTable] = useState(false);

    const { data: buildsData, isLoading: buildsLoading } = useQuery({
        queryKey: ['buildAnalysisBuilds', token],
        queryFn: async () => {
            const res = await axios.get<BuildOption[]>(`${API_BASE}/api/build-analysis/builds`, authConfig);
            return res.data;
        },
    });

    const builds = buildsData ?? [];

    // Auto-select latest build
    const effectiveBuild = selectedBuild || (builds[0]?.version ?? '');
    const currentMajor = majorOf(effectiveBuild);

    const { data: analysisData, isLoading: dataLoading } = useQuery({
        queryKey: ['buildAnalysisData', token],
        queryFn: async () => {
            const res = await axios.get<{ rows: BenchmarkRow[]; total: number }>(
                `${API_BASE}/api/build-analysis/data`,
                authConfig,
            );
            return res.data;
        },
        enabled: builds.length > 0,
    });

    const allRows = analysisData?.rows ?? [];

    // Rows for selected build's major
    const curMajorRows = useMemo(
        () => allRows.filter(r => majorOf(r.build_version) === currentMajor),
        [allRows, currentMajor],
    );

    // Previous major
    const allMajors = useMemo(() => {
        const s = new Set(allRows.map(r => majorOf(r.build_version)).filter(Boolean));
        return [...s].sort((a, b) => parseInt(b, 10) - parseInt(a, 10));
    }, [allRows]);
    const prevMajor = allMajors.find(m => m !== currentMajor) ?? null;
    const prevMajorRows = useMemo(
        () => (prevMajor ? allRows.filter(r => majorOf(r.build_version) === prevMajor) : []),
        [allRows, prevMajor],
    );

    // Best rows for current and previous major
    const curBest = useMemo(() => bestRows(curMajorRows), [curMajorRows]);
    const prevBest = useMemo(() => bestRows(prevMajorRows), [prevMajorRows]);

    // Distinct DS types in current major
    const dsTypes = useMemo(() => {
        const s = new Set(curMajorRows.map(r => r.data_source_type).filter(Boolean) as string[]);
        return [...s].sort();
    }, [curMajorRows]);

    // Distinct file sets per DS
    const fileSetsForDs = (ds: string) => {
        const s = new Set(
            curMajorRows.filter(r => r.data_source_type === ds).map(r => r.file_set_type).filter(Boolean) as string[]
        );
        return [...s].sort((a, b) => fileSetRank(a) - fileSetRank(b));
    };

    // Overview KPIs
    const avgTp = useMemo(() => {
        const vals = curMajorRows.map(r => r.throughput_cluster_gb_hr).filter(v => v != null) as number[];
        return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    }, [curMajorRows]);

    const loading = buildsLoading || dataLoading;

    const tabStyle = (t: string): React.CSSProperties => ({
        padding: '0.5rem 1.1rem',
        borderRadius: '8px 8px 0 0',
        border: '1px solid var(--surface-border)',
        borderBottom: activeTab === t ? '1px solid var(--surface-ground, #fff)' : '1px solid var(--surface-border)',
        background: activeTab === t ? 'var(--surface-ground, #fff)' : 'transparent',
        fontWeight: activeTab === t ? 700 : 400,
        cursor: 'pointer',
        fontFamily: 'inherit',
        fontSize: '0.88rem',
        color: activeTab === t ? 'var(--primary-color)' : 'var(--text-secondary)',
        marginBottom: -1,
    });

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <BarChart2 size={26} style={{ color: 'var(--primary-color)' }} />
                <h2 style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                    Build Depth Analysis
                </h2>
            </div>

            {/* Build selector */}
            <div className="glass-panel" style={{ padding: '1rem 1.25rem', display: 'flex', flexWrap: 'wrap', gap: '1rem', alignItems: 'flex-end' }}>
                <div className="input-group" style={{ margin: 0, minWidth: 220 }}>
                    <label htmlFor="ba-build">Select Build</label>
                    <select
                        id="ba-build"
                        value={effectiveBuild}
                        onChange={e => setSelectedBuild(e.target.value)}
                        disabled={buildsLoading}
                        style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
                    >
                        {builds.map(b => (
                            <option key={b.version} value={b.version}>
                                {b.version} ({b.count} runs)
                            </option>
                        ))}
                    </select>
                </div>
                {effectiveBuild && (
                    <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                        Major: <strong style={{ color: 'var(--text-primary)' }}>{currentMajor}.x</strong>
                        {prevMajor && (
                            <> · Comparing vs <strong style={{ color: 'var(--text-primary)' }}>{prevMajor}.x</strong></>
                        )}
                        {' '}· <strong>{curMajorRows.length}</strong> runs in this major
                    </div>
                )}
            </div>

            {loading && (
                <div style={{ padding: '3rem', textAlign: 'center' }}>
                    <Loader2 className="lucide-spin" size={32} />
                </div>
            )}

            {!loading && effectiveBuild && (
                <>
                    {/* Overview KPIs */}
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '1rem' }}>
                        {[
                            ['Current Major', `${currentMajor}.x`],
                            ['Prev Major', prevMajor ? `${prevMajor}.x` : '—'],
                            ['Total Runs', String(curMajorRows.length)],
                            ['DS Types', String(dsTypes.length)],
                            ['Avg GB/hr', avgTp != null ? avgTp.toFixed(2) : '—'],
                        ].map(([label, val]) => (
                            <div key={label} className="glass-panel" style={{ padding: '0.85rem 1rem' }}>
                                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }}>{label}</div>
                                <div style={{ fontSize: '1.4rem', fontWeight: 800, color: 'var(--primary-color)' }}>{val}</div>
                            </div>
                        ))}
                    </div>

                    {/* Tabs */}
                    <div>
                        <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--surface-border)', marginBottom: 0 }}>
                            {(['matrix', 'trend', 'overview'] as const).map(t => (
                                <button key={t} type="button" style={tabStyle(t)} onClick={() => setActiveTab(t)}>
                                    {t === 'matrix' ? '🧩 Scenario Matrix' : t === 'trend' ? '📈 Trend Charts' : '📊 Overview'}
                                </button>
                            ))}
                        </div>

                        <div className="glass-panel" style={{ borderRadius: '0 8px 8px 8px', padding: '1.25rem' }}>

                            {/* ── Scenario Matrix ── */}
                            {activeTab === 'matrix' && (
                                <div>
                                    <p style={{ margin: '0 0 1rem', fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                                        Best result per scenario across <strong>{currentMajor}.x</strong>.
                                        {prevMajor && <> Compared against best of <strong>{prevMajor}.x</strong>.</>}
                                    </p>
                                    {dsTypes.length === 0 && (
                                        <p style={{ color: 'var(--text-secondary)' }}>No data for this major version.</p>
                                    )}
                                    {dsTypes.map(ds => {
                                        const fileSets = fileSetsForDs(ds);
                                        return (
                                            <div key={ds} style={{ marginBottom: '1.5rem' }}>
                                                <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 0.75rem' }}>
                                                    {ds}
                                                </h3>
                                                {fileSets.map(fs => {
                                                    const rows = SCENARIO_ORDER.map(scen => {
                                                        const key = `${ds}||${fs}||${scen}`;
                                                        return { scen, row: curBest.get(key) ?? null };
                                                    }).filter(x => x.row !== null);

                                                    if (rows.length === 0) return null;

                                                    const baseRow = rows.find(x => x.scen === 'Base (Full Scan)')?.row ?? null;
                                                    const baseTp = baseRow?.throughput_cluster_gb_hr ?? null;

                                                    const prevColHeader = prevMajor ? `vs ${prevMajor}.x` : null;

                                                    return (
                                                        <details key={fs} style={{ marginBottom: '0.75rem', border: '1px solid var(--surface-border)', borderRadius: 10, overflow: 'hidden' }}>
                                                            <summary style={{ padding: '0.75rem 1rem', fontWeight: 700, cursor: 'pointer', background: 'var(--surface-ground, rgba(0,0,0,0.03))', fontSize: '0.92rem' }}>
                                                                {ds} — {fs} ({rows.length} scenario{rows.length !== 1 ? 's' : ''})
                                                            </summary>
                                                            <div style={{ overflowX: 'auto' }}>
                                                                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.84rem' }}>
                                                                    <thead>
                                                                        <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'left' }}>Scenario</th>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'right' }}>GB/hr</th>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'right' }}>{['postgres','mssql','mysql','oracle'].includes(ds.toLowerCase()) ? 'Tables/hr' : 'Files/hr'}</th>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'right' }}>Scan Hrs</th>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'right' }}>Objects</th>
                                                                            <th style={{ padding: '8px 10px', textAlign: 'right' }}>vs Base</th>
                                                                            {prevColHeader && <th style={{ padding: '8px 10px', textAlign: 'right' }}>{prevColHeader}</th>}
                                                                        </tr>
                                                                    </thead>
                                                                    <tbody>
                                                                        {rows.map(({ scen, row }) => {
                                                                            if (!row) return null;
                                                                            const tp = row.throughput_cluster_gb_hr;
                                                                            const vsBase = scen !== 'Base (Full Scan)' ? pctDelta(tp, baseTp) : null;
                                                                            const prevKey = `${ds}||${fs}||${scen}`;
                                                                            const prevRow = prevBest.get(prevKey) ?? null;
                                                                            const vsPrev = pctDelta(tp, prevRow?.throughput_cluster_gb_hr ?? null);
                                                                            const vsBaseColor = vsBase == null ? '' : vsBase >= 0 ? '#16a34a' : '#dc2626';
                                                                            const vsPrevColor = vsPrev == null ? '' : vsPrev >= 0 ? '#16a34a' : '#dc2626';
                                                                            return (
                                                                                <tr key={scen} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                                                                                    <td style={{ padding: '8px 10px', fontWeight: scen === 'Base (Full Scan)' ? 700 : 400 }}>{scen}</td>
                                                                                    <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmt(tp)}</td>
                                                                                    <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmt(row.throughput_cluster_files_hr, 0)}</td>
                                                                                    <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmt(row.total_scan_hours)}</td>
                                                                                    <td style={{ padding: '8px 10px', textAlign: 'right' }}>
                                                                                        {row.objects_scanned ? Number(row.objects_scanned).toLocaleString() : '—'}
                                                                                    </td>
                                                                                    <td style={{ padding: '8px 10px', textAlign: 'right', color: vsBaseColor, fontWeight: vsBase != null ? 600 : 400 }}>
                                                                                        {vsBase != null ? fmtPct(vsBase) : ''}
                                                                                    </td>
                                                                                    {prevColHeader && (
                                                                                        <td style={{ padding: '8px 10px', textAlign: 'right', color: vsPrevColor, fontWeight: vsPrev != null ? 600 : 400 }}>
                                                                                            {vsPrev != null ? fmtPct(vsPrev) : (prevMajor ? 'N/A' : '—')}
                                                                                        </td>
                                                                                    )}
                                                                                </tr>
                                                                            );
                                                                        })}
                                                                    </tbody>
                                                                </table>
                                                            </div>
                                                        </details>
                                                    );
                                                })}
                                            </div>
                                        );
                                    })}
                                </div>
                            )}

                            {/* ── Trend Charts ── */}
                            {activeTab === 'trend' && (
                                <div>
                                    <p style={{ margin: '0 0 1rem', fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                                        Base (Full Scan) throughput trend across all builds. Small Files preferred.
                                    </p>
                                    {dsTypes.map(ds => {
                                        const trendData = buildTrendData(allRows, ds, '');
                                        if (trendData.length === 0) return null;
                                        return (
                                            <div key={ds} style={{ marginBottom: '2rem' }}>
                                                <h3 style={{ fontSize: '1rem', fontWeight: 700, margin: '0 0 0.75rem' }}>{ds} Trend</h3>
                                                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                                                    <div>
                                                        <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: 6, color: 'var(--text-secondary)' }}>Data Throughput (GB/hr)</div>
                                                        <div style={{ height: 220 }}>
                                                            <ResponsiveContainer width="100%" height="100%">
                                                                <BarChart data={trendData} margin={{ top: 4, right: 8, left: 0, bottom: 40 }}>
                                                                    <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                                                                    <XAxis dataKey="build" tick={{ fontSize: 10 }} angle={-35} textAnchor="end" height={55} />
                                                                    <YAxis tick={{ fontSize: 10 }} />
                                                                    <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                                                                    <Bar dataKey="gb" name="GB/hr" fill="#7c3aed" />
                                                                </BarChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                    </div>
                                                    <div>
                                        <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: 6, color: 'var(--text-secondary)' }}>{['postgres','mssql','mysql','oracle'].includes(ds.toLowerCase()) ? 'Table Throughput (tables/hr)' : 'File Throughput (files/hr)'}</div>
                                        <div style={{ height: 220 }}>
                                            <ResponsiveContainer width="100%" height="100%">
                                                <BarChart data={trendData} margin={{ top: 4, right: 8, left: 0, bottom: 40 }}>
                                                    <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                                                    <XAxis dataKey="build" tick={{ fontSize: 10 }} angle={-35} textAnchor="end" height={55} />
                                                    <YAxis tick={{ fontSize: 10 }} />
                                                    <Tooltip formatter={(v: any) => (typeof v === 'number' ? v.toFixed(2) : v)} />
                                                    <Bar dataKey="files" name={['postgres','mssql','mysql','oracle'].includes(ds.toLowerCase()) ? 'Tables/hr' : 'Files/hr'} fill="#059669" />
                                                                </BarChart>
                                                            </ResponsiveContainer>
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}

                            {/* ── Overview table ── */}
                            {activeTab === 'overview' && (
                                <div>
                                    <div 
                                        onClick={() => setShowOverviewTable(!showOverviewTable)}
                                        style={{ 
                                            display: 'flex', 
                                            alignItems: 'center', 
                                            gap: '0.5rem', 
                                            cursor: 'pointer', 
                                            marginBottom: '1rem',
                                            userSelect: 'none',
                                            padding: '0.5rem 0.75rem',
                                            background: 'var(--surface-ground, rgba(0,0,0,0.03))',
                                            borderRadius: 8,
                                            border: '1px solid var(--surface-border)'
                                        }}
                                    >
                                        {showOverviewTable ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                                        <span style={{ fontWeight: 700, fontSize: '0.92rem' }}>
                                            Show Detailed Run Log ({curMajorRows.length} rows)
                                        </span>
                                    </div>

                                    {showOverviewTable && (
                                        <div className="animate-in" style={{ overflowX: 'auto' }}>
                                            <p style={{ margin: '0 0 0.75rem', fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                                                All runs in <strong>{currentMajor}.x</strong> — {curMajorRows.length} rows.
                                            </p>
                                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                                                <thead>
                                                    <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                                                        {['Build', 'DS Type', 'File Set', 'Scenario', 'GB/hr', 'Files/hr', 'Scan Hrs', 'Run Date'].map(h => (
                                                            <th key={h} style={{ padding: '8px 10px', textAlign: 'left', whiteSpace: 'nowrap' }}>{h}</th>
                                                        ))}
                                                    </tr>
                                                </thead>
                                                <tbody>
                                                    {[...curMajorRows]
                                                        .sort((a, b) => sortKey(b.build_version) - sortKey(a.build_version))
                                                        .slice(0, 200)
                                                        .map(r => (
                                                            <tr key={r.id} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                                                                <td style={{ padding: '7px 10px', fontFamily: 'ui-monospace, monospace', fontSize: '0.78rem' }}>{r.build_version}</td>
                                                                <td style={{ padding: '7px 10px' }}>{r.data_source_type ?? '—'}</td>
                                                                <td style={{ padding: '7px 10px' }}>{r.file_set_type ?? '—'}</td>
                                                                <td style={{ padding: '7px 10px' }}>{classifyScenario(r)}</td>
                                                                <td style={{ padding: '7px 10px', textAlign: 'right' }}>{fmt(r.throughput_cluster_gb_hr)}</td>
                                                                <td style={{ padding: '7px 10px', textAlign: 'right' }}>{fmt(r.throughput_cluster_files_hr, 0)}</td>
                                                                <td style={{ padding: '7px 10px', textAlign: 'right' }}>{fmt(r.total_scan_hours)}</td>
                                                                <td style={{ padding: '7px 10px', whiteSpace: 'nowrap' }}>{r.run_date ?? '—'}</td>
                                                            </tr>
                                                        ))}
                                                </tbody>
                                            </table>
                                            {curMajorRows.length > 200 && (
                                                <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: '0.5rem' }}>
                                                    Showing first 200 of {curMajorRows.length} rows.
                                                </p>
                                            )}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>
                </>
            )}
        </div>
    );
};

export default BuildAnalysisPage;
