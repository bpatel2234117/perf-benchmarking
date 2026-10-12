import React, { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, GitBranch } from 'lucide-react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from 'recharts';
import { API_BASE } from '../apiConfig';
import { useUserRole } from '../hooks/useUserRole';

interface VersionComparisonPageProps { token: string; }
interface BuildRow { id: number; version: string; release_milestone: string | null; quarter_label: string | null; }
interface EnvRow { id: number; env_name: string; }
interface StatRow { build_version: string; env_name: string; classifier_count: number; last_synced_at: string | null; }

function sortKey(v: string): number {
    const p = (v || '').split('.');
    return (parseInt(p[0] || '0', 10) || 0) * 10000 + (parseInt(p[1] || '0', 10) || 0);
}

function errMsg(e: unknown): string {
    if (axios.isAxiosError(e)) return String((e.response?.data as { detail?: string })?.detail ?? e.message);
    return String(e);
}

const sel: React.CSSProperties = { padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' };

const VersionComparisonPage: React.FC<VersionComparisonPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const isAdmin = useUserRole(token) === 'Admin';  // tagging a release is Admin-only (backend require_admin)
    const qc = useQueryClient();
    const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
    const [tagBuild, setTagBuild] = useState('');
    const [tagMilestone, setTagMilestone] = useState('');
    const [tagQuarter, setTagQuarter] = useState('');
    const [filterEnv, setFilterEnv] = useState('');
    const [cmpBase, setCmpBase] = useState('');
    const [cmpCand, setCmpCand] = useState('');

    const { data: builds = [] } = useQuery({
        queryKey: ['builds', token],
        queryFn: async () => {
            const res = await axios.get<BuildRow[]>(`${API_BASE}/api/builds`, authConfig);
            return res.data;
        },
    });

    const { data: envs = [] } = useQuery({
        queryKey: ['environments', token],
        queryFn: async () => {
            const res = await axios.get<EnvRow[]>(`${API_BASE}/api/environments`, authConfig);
            return res.data;
        },
    });

    const { data: stats = [] } = useQuery({
        queryKey: ['classifierStats', token, filterEnv],
        queryFn: async () => {
            const params: Record<string, string> = {};
            if (filterEnv) params.environment_id = filterEnv;
            const res = await axios.get<StatRow[]>(`${API_BASE}/api/classifiers/build-stats`, { ...authConfig, params });
            return res.data;
        },
    });

    const { data: mappingData } = useQuery({
        queryKey: ['classifierMappings', token, filterEnv],
        queryFn: async () => {
            const params: Record<string, string> = {};
            if (filterEnv) params.environment_id = filterEnv;
            const res = await axios.get<{ build_map: Record<string, string[]>; builds: string[] }>(
                `${API_BASE}/api/classifiers/build-mappings`, { ...authConfig, params }
            );
            return res.data;
        },
    });

    const tagMut = useMutation({
        mutationFn: async () => {
            const build = builds.find(b => b.version === tagBuild);
            if (!build) throw new Error('Build not found');
            await axios.put(`${API_BASE}/api/builds/${build.id}/tag`,
                { release_milestone: tagMilestone || null, quarter_label: tagQuarter || null }, authConfig);
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['builds'] });
            setBanner({ kind: 'ok', text: 'Release tag saved.' });
            setTagMilestone(''); setTagQuarter('');
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) }),
    });

    const envNames = useMemo(() => [...new Set(stats.map(r => r.env_name).filter(Boolean))].sort(), [stats]);
    const filteredStats = useMemo(() => filterEnv ? stats.filter(r => r.env_name === envs.find(e => String(e.id) === filterEnv)?.env_name) : stats, [stats, filterEnv, envs]);

    const chartData = useMemo(() => {
        const byBuild = new Map<string, Record<string, number>>();
        for (const r of filteredStats) {
            const cur = byBuild.get(r.build_version) ?? {};
            cur[r.env_name] = r.classifier_count;
            byBuild.set(r.build_version, cur);
        }
        return [...byBuild.entries()]
            .sort((a, b) => sortKey(a[0]) - sortKey(b[0]))
            .map(([build, envMap]) => ({ build, ...envMap }));
    }, [filteredStats]);

    const buildMap = mappingData?.build_map ?? {};
    const cmpBuilds = useMemo(() => [...(mappingData?.builds ?? [])].sort((a, b) => sortKey(a) - sortKey(b)), [mappingData]);
    const setA = new Set(buildMap[cmpBase] ?? []);
    const setB = new Set(buildMap[cmpCand] ?? []);
    const onlyA = cmpBase && cmpCand ? [...setA].filter(x => !setB.has(x)).sort() : [];
    const onlyB = cmpBase && cmpCand ? [...setB].filter(x => !setA.has(x)).sort() : [];
    const inBoth = cmpBase && cmpCand ? [...setA].filter(x => setB.has(x)).length : 0;

    const COLORS = ['#7c3aed', '#2563eb', '#059669', '#dc2626', '#f59e0b'];

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <GitBranch size={26} style={{ color: 'var(--primary-color)' }} />
                <h2 style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>Version Comparison</h2>
            </div>

            {banner && (
                <div style={{ padding: '0.65rem 1rem', borderRadius: 8, background: banner.kind === 'ok' ? 'rgba(22,163,74,0.1)' : 'rgba(220,38,38,0.1)', border: `1px solid ${banner.kind === 'ok' ? '#16a34a' : '#dc2626'}`, fontSize: '0.88rem' }}>
                    {banner.text}
                </div>
            )}

            {/* Release tag editor */}
            <div className="glass-panel" style={{ padding: '1.25rem' }}>
                <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem', fontWeight: 700 }}>🏷️ Release Tags</h3>
                <p style={{ margin: '0 0 0.75rem', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    Link build versions to release milestones (e.g. 250) and quarter labels.
                </p>
                <div style={{ overflowX: 'auto', marginBottom: '1rem' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                        <thead>
                            <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                                {['Version', 'Release Milestone', 'Quarter Label'].map(h => (
                                    <th key={h} style={{ padding: '7px 10px', textAlign: 'left' }}>{h}</th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {builds.filter(b => b.release_milestone || b.quarter_label).map(b => (
                                <tr key={b.id} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                                    <td style={{ padding: '6px 10px', fontFamily: 'ui-monospace, monospace' }}>{b.version}</td>
                                    <td style={{ padding: '6px 10px', fontWeight: 600, color: 'var(--primary-color)' }}>{b.release_milestone ?? '—'}</td>
                                    <td style={{ padding: '6px 10px' }}>{b.quarter_label ?? '—'}</td>
                                </tr>
                            ))}
                            {builds.filter(b => b.release_milestone || b.quarter_label).length === 0 && (
                                <tr><td colSpan={3} style={{ padding: '1rem', color: 'var(--text-secondary)', textAlign: 'center' }}>No release tags yet.</td></tr>
                            )}
                        </tbody>
                    </table>
                </div>
                {isAdmin && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'flex-end' }}>
                    <div className="input-group" style={{ margin: 0, minWidth: 180 }}>
                        <label>Build version</label>
                        <select value={tagBuild} onChange={e => setTagBuild(e.target.value)} style={sel}>
                            <option value="">Select build</option>
                            {builds.map(b => <option key={b.id} value={b.version}>{b.version}</option>)}
                        </select>
                    </div>
                    <div className="input-group" style={{ margin: 0, minWidth: 140 }}>
                        <label>Release milestone</label>
                        <input value={tagMilestone} onChange={e => setTagMilestone(e.target.value)} placeholder="e.g. 250"
                            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }} />
                    </div>
                    <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
                        <label>Quarter label (optional)</label>
                        <input value={tagQuarter} onChange={e => setTagQuarter(e.target.value)} placeholder="e.g. Q1 2026"
                            style={{ padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }} />
                    </div>
                    <button type="button" className="btn" disabled={!tagBuild || !tagMilestone || tagMut.isPending} onClick={() => tagMut.mutate()}>
                        {tagMut.isPending ? <Loader2 size={14} className="lucide-spin" /> : 'Save tag'}
                    </button>
                </div>
                )}
            </div>

            {/* Classifier count trend */}
            <div className="glass-panel" style={{ padding: '1.25rem' }}>
                <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem', fontWeight: 700 }}>Classifier Count Trend</h3>
                <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
                    <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
                        <label>Environment filter</label>
                        <select value={filterEnv} onChange={e => setFilterEnv(e.target.value)} style={sel}>
                            <option value="">All environments</option>
                            {envs.map(e => <option key={e.id} value={String(e.id)}>{e.env_name}</option>)}
                        </select>
                    </div>
                </div>
                {chartData.length > 0 ? (
                    <div style={{ height: 280 }}>
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 40 }}>
                                <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                                <XAxis dataKey="build" tick={{ fontSize: 10 }} angle={-35} textAnchor="end" height={55} />
                                <YAxis tick={{ fontSize: 11 }} />
                                <Tooltip />
                                <Legend />
                                {envNames.map((env, i) => (
                                    <Line key={env} type="monotone" dataKey={env} stroke={COLORS[i % COLORS.length]} strokeWidth={2} dot={{ r: 4 }} connectNulls />
                                ))}
                            </LineChart>
                        </ResponsiveContainer>
                    </div>
                ) : (
                    <p style={{ color: 'var(--text-secondary)', fontSize: '0.88rem' }}>No classifier stats available. Sync classifiers from the Environments page.</p>
                )}
            </div>

            {/* Build-to-build comparison */}
            {cmpBuilds.length >= 2 && (
                <div className="glass-panel" style={{ padding: '1.25rem' }}>
                    <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem', fontWeight: 700 }}>Build-to-Build Comparison</h3>
                    <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
                        {[{ lbl: 'Baseline Build', val: cmpBase, set: setCmpBase }, { lbl: 'Candidate Build', val: cmpCand, set: setCmpCand }].map(({ lbl, val, set }) => (
                            <div key={lbl} className="input-group" style={{ margin: 0, minWidth: 180 }}>
                                <label>{lbl}</label>
                                <select value={val} onChange={e => set(e.target.value)} style={sel}>
                                    <option value="">Select</option>
                                    {cmpBuilds.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>
                            </div>
                        ))}
                    </div>
                    {cmpBase && cmpCand && (
                        <>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '1rem', marginBottom: '1rem' }}>
                                {[
                                    { label: `Only in ${cmpBase}`, val: onlyA.length, color: '#7c3aed' },
                                    { label: `Only in ${cmpCand}`, val: onlyB.length, color: '#dc2626' },
                                    { label: 'In both', val: inBoth, color: '#16a34a' },
                                ].map(({ label, val, color }) => (
                                    <div key={label} style={{ padding: '0.85rem 1rem', borderRadius: 10, border: '1px solid var(--surface-border)' }}>
                                        <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', textTransform: 'uppercase', marginBottom: 4 }}>{label}</div>
                                        <div style={{ fontSize: '1.5rem', fontWeight: 800, color }}>{val}</div>
                                    </div>
                                ))}
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                                {[
                                    { title: `Only in ${cmpBase}`, items: onlyA, color: '#7c3aed' },
                                    { title: `Only in ${cmpCand}`, items: onlyB, color: '#dc2626' },
                                ].map(({ title, items, color }) => (
                                    <div key={title}>
                                        <div style={{ fontWeight: 700, marginBottom: '0.5rem', color }}>{title} ({items.length})</div>
                                        <div style={{ maxHeight: 260, overflowY: 'auto', border: '1px solid var(--surface-border)', borderRadius: 8, padding: '0.5rem' }}>
                                            {items.length === 0
                                                ? <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>None</span>
                                                : items.map(n => <div key={n} style={{ padding: '3px 6px', fontSize: '0.82rem', borderBottom: '1px solid var(--surface-border)' }}>{n}</div>)}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </>
                    )}
                </div>
            )}
        </div>
    );
};

export default VersionComparisonPage;
