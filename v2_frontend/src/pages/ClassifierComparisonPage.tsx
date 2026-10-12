import React, { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, Layers, RefreshCw, ChevronRight, ChevronDown, CheckCircle2, AlertCircle } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import { useUserRole } from '../hooks/useUserRole';

interface ClassifierComparisonPageProps { token: string; }

interface EnvironmentRow { id: number; env_name: string; is_active: boolean; }

interface SnapshotSummary {
    build_version: string;
    snapshot_count: number;
    last_captured: string | null;
}

interface ClassifierSnapshot {
    classifier_name: string;
    bigid_classifier_id: string;
    snapshot: Record<string, any>;
}

const CLASSIFIER_DETAIL_KEYS = [
    "category", "classifierType", "dataType", "original_name",
    "detection_sensitivity", "enabled", "min_length", "max_length",
    "proximity_after", "proximity_before", "nst_proximity_after", "nst_proximity_before",
    "description",
];

const ClassifierComparisonPage: React.FC<ClassifierComparisonPageProps> = ({ token }) => {
    const queryClient = useQueryClient();
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    // refresh-snapshots triggers BigID API calls — backend gates it to "Manage Classifiers" (Admin/Developer).
    const canRefresh = ['Admin', 'Developer'].includes(useUserRole(token));
    const [envId, setEnvId] = useState<string>('');
    const [buildA, setBuildA] = useState('');
    const [buildB, setBuildB] = useState('');
    
    // Deep-dive state
    const [deepBuildA, setDeepBuildA] = useState('');
    const [deepBuildB, setDeepBuildB] = useState('');
    const [showIdentical, setShowIdentical] = useState(false);
    const [expandedClassifiers, setExpandedClassifiers] = useState<Record<string, boolean>>({});

    const { data: envs = [] } = useQuery({
        queryKey: ['environments', token],
        queryFn: async () => {
            const res = await axios.get<EnvironmentRow[]>(`${API_BASE}/api/environments`, authConfig);
            return res.data;
        },
    });

    const { data: mappingData, isLoading: isMappingLoading } = useQuery({
        queryKey: ['classifierMappings', token, envId],
        queryFn: async () => {
            const params: Record<string, string> = {};
            if (envId) params.environment_id = envId;
            const res = await axios.get<{ build_map: Record<string, string[]>; builds: string[] }>(
                `${API_BASE}/api/classifiers/build-mappings`, { ...authConfig, params }
            );
            return res.data;
        },
    });

    const { data: snapshotSummary = [], isLoading: isSnapshotSummaryLoading } = useQuery({
        queryKey: ['classifierSnapshotSummary', token, envId],
        queryFn: async () => {
            const params: Record<string, string> = {};
            if (envId) params.environment_id = envId;
            const res = await axios.get<SnapshotSummary[]>(
                `${API_BASE}/api/classifiers/snapshot-summary`, { ...authConfig, params }
            );
            return res.data;
        },
    });

    const { data: snapshotsA = { snapshots: [] }, isLoading: isSnapshotsALoading } = useQuery({
        queryKey: ['classifierSnapshots', token, envId, deepBuildA],
        queryFn: async () => {
            if (!deepBuildA) return { snapshots: [] };
            const params: Record<string, string> = { build_version: deepBuildA };
            if (envId) params.environment_id = envId;
            const res = await axios.get<{ snapshots: ClassifierSnapshot[] }>(
                `${API_BASE}/api/classifiers/snapshots`, { ...authConfig, params }
            );
            return res.data;
        },
        enabled: !!deepBuildA
    });

    const { data: snapshotsB = { snapshots: [] }, isLoading: isSnapshotsBLoading } = useQuery({
        queryKey: ['classifierSnapshots', token, envId, deepBuildB],
        queryFn: async () => {
            if (!deepBuildB) return { snapshots: [] };
            const params: Record<string, string> = { build_version: deepBuildB };
            if (envId) params.environment_id = envId;
            const res = await axios.get<{ snapshots: ClassifierSnapshot[] }>(
                `${API_BASE}/api/classifiers/snapshots`, { ...authConfig, params }
            );
            return res.data;
        },
        enabled: !!deepBuildB
    });

    const refreshMutation = useMutation({
        mutationFn: async (targetEnvId: number) => {
            const res = await axios.post(`${API_BASE}/api/classifiers/refresh-snapshots`, null, {
                ...authConfig,
                params: { environment_id: targetEnvId }
            });
            return res.data;
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['classifierSnapshotSummary'] });
            queryClient.invalidateQueries({ queryKey: ['classifierSnapshots'] });
            alert("Snapshots refreshed successfully.");
        },
        onError: (err: any) => {
            alert(`Failed to refresh snapshots: ${err?.response?.data?.detail || err.message}`);
        }
    });

    const buildMap = mappingData?.build_map ?? {};
    const builds = mappingData?.builds ?? [];

    const sortedBuilds = useMemo(() => [...builds].sort((a, b) => {
        const na = parseFloat(a) || 0, nb = parseFloat(b) || 0;
        return na - nb;
    }), [builds]);

    const snapshotBuilds = useMemo(() => snapshotSummary.map(s => s.build_version), [snapshotSummary]);

    // Build-by-build diff table
    const diffRows = useMemo(() => {
        const out: { build: string; count: number; added: number | null; removed: number | null; addedList: string[]; removedList: string[] }[] = [];
        let prev: Set<string> | null = null;
        for (const bv of sortedBuilds) {
            const cur = new Set(buildMap[bv] ?? []);
            const added = prev ? [...cur].filter(x => !prev!.has(x)) : [];
            const removed = prev ? [...prev].filter(x => !cur.has(x)) : [];
            out.push({ build: bv, count: cur.size, added: prev ? added.length : null, removed: prev ? removed.length : null, addedList: added, removedList: removed });
            prev = cur;
        }
        return out.reverse();
    }, [sortedBuilds, buildMap]);

    // Pair comparison
    const setA = new Set(buildMap[buildA] ?? []);
    const setB = new Set(buildMap[buildB] ?? []);
    const onlyA = buildA && buildB ? [...setA].filter(x => !setB.has(x)).sort() : [];
    const onlyB = buildA && buildB ? [...setB].filter(x => !setA.has(x)).sort() : [];
    const inBoth = buildA && buildB ? [...setA].filter(x => setB.has(x)).length : 0;

    // Deep-dive comparison
    const deepDiffRows = useMemo(() => {
        if (!deepBuildA || !deepBuildB) return [];
        const mapA = new Map(snapshotsA.snapshots.map(s => [s.classifier_name, s.snapshot]));
        const mapB = new Map(snapshotsB.snapshots.map(s => [s.classifier_name, s.snapshot]));
        const allNames = Array.from(new Set([...mapA.keys(), ...mapB.keys()])).sort();
        
        return allNames.map(name => {
            const sa = mapA.get(name);
            const sb = mapB.get(name);
            const changedKeys: string[] = [];
            
            if (sa && sb) {
                for (const key of CLASSIFIER_DETAIL_KEYS) {
                    const va = String(sa[key] ?? '').trim();
                    const vb = String(sb[key] ?? '').trim();
                    if (va !== vb) changedKeys.push(key);
                }
            }
            
            let status: 'Added' | 'Removed' | 'Changed' | 'Identical' = 'Identical';
            if (!sa) status = 'Added';
            else if (!sb) status = 'Removed';
            else if (changedKeys.length > 0) status = 'Changed';
            
            return { name, status, changedKeys, sa, sb };
        });
    }, [deepBuildA, deepBuildB, snapshotsA, snapshotsB]);

    const filteredDeepDiff = useMemo(() => {
        return showIdentical ? deepDiffRows : deepDiffRows.filter(r => r.status !== 'Identical');
    }, [deepDiffRows, showIdentical]);

    const sel: React.CSSProperties = { padding: '0.45rem 0.65rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%', background: 'white' };

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', paddingBottom: '2rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                    <Layers size={26} style={{ color: 'var(--primary-color)' }} />
                    <h2 style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>Classifier Comparison</h2>
                </div>
                <div style={{ display: 'flex', gap: '0.75rem' }}>
                    {canRefresh && (
                    <button
                        className="btn-secondary"
                        onClick={() => envId ? refreshMutation.mutate(parseInt(envId)) : alert("Select an environment first")}
                        disabled={refreshMutation.isPending || !envId}
                        style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}
                    >
                        {refreshMutation.isPending ? <Loader2 size={16} className="lucide-spin" /> : <RefreshCw size={16} />}
                        Manual Refresh Snapshots
                    </button>
                    )}
                </div>
            </div>
            
            <p style={{ margin: 0, fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                Shows newly added/removed enabled classifiers per build and detailed metadata diffs.
            </p>

            <div className="glass-panel" style={{ padding: '1rem 1.25rem', display: 'flex', flexWrap: 'wrap', gap: '1rem', alignItems: 'flex-end' }}>
                <div className="input-group" style={{ margin: 0, minWidth: 220 }}>
                    <label>Environment Filter</label>
                    <select value={envId} onChange={e => setEnvId(e.target.value)} style={sel}>
                        <option value="">All environments</option>
                        {envs.map(e => <option key={e.id} value={String(e.id)}>{e.env_name}</option>)}
                    </select>
                </div>
            </div>

            {(isMappingLoading || isSnapshotSummaryLoading) && <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={28} /></div>}

            {!isMappingLoading && builds.length === 0 && (
                <div className="glass-panel" style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
                    No classifier data found. Use the Environments page to sync classifiers for an environment.
                </div>
            )}

            {!isMappingLoading && builds.length > 0 && (
                <>
                    {/* Build-by-build summary */}
                    <div className="glass-panel" style={{ padding: '1.25rem' }}>
                        <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem', fontWeight: 700 }}>Build-by-Build Changes (Names)</h3>
                        <div style={{ overflowX: 'auto' }}>
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                                <thead>
                                    <tr style={{ background: '#f8fafc', borderBottom: '2px solid var(--surface-border)' }}>
                                        <th style={{ padding: '10px 12px', textAlign: 'left', color: '#475569' }}>Build</th>
                                        <th style={{ padding: '10px 12px', textAlign: 'right', color: '#475569' }}>Count</th>
                                        <th style={{ padding: '10px 12px', textAlign: 'right', color: '#475569' }}>Added</th>
                                        <th style={{ padding: '10px 12px', textAlign: 'right', color: '#475569' }}>Removed</th>
                                        <th style={{ padding: '10px 12px', textAlign: 'left', color: '#475569' }}>Sample Added</th>
                                        <th style={{ padding: '10px 12px', textAlign: 'left', color: '#475569' }}>Sample Removed</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {diffRows.map(r => (
                                        <tr key={r.build} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                                            <td style={{ padding: '10px 12px', fontFamily: 'ui-monospace, monospace', fontWeight: 600 }}>{r.build}</td>
                                            <td style={{ padding: '10px 12px', textAlign: 'right', fontWeight: 600 }}>{r.count}</td>
                                            <td style={{ padding: '10px 12px', textAlign: 'right', color: r.added ? '#16a34a' : '#94a3b8' }}>
                                                {r.added != null ? (r.added > 0 ? `+${r.added}` : '0') : '—'}
                                            </td>
                                            <td style={{ padding: '10px 12px', textAlign: 'right', color: r.removed ? '#dc2626' : '#94a3b8' }}>
                                                {r.removed != null ? (r.removed > 0 ? `-${r.removed}` : '0') : '—'}
                                            </td>
                                            <td style={{ padding: '10px 12px', fontSize: '0.78rem', color: '#16a34a', maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                {r.addedList.join(', ')}
                                            </td>
                                            <td style={{ padding: '10px 12px', fontSize: '0.78rem', color: '#dc2626', maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                {r.removedList.join(', ')}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    {/* Pair comparison */}
                    <div className="glass-panel" style={{ padding: '1.25rem' }}>
                        <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem', fontWeight: 700 }}>Compare Any Two Builds (Names)</h3>
                        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '1.5rem' }}>
                            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
                                <label>Build A (Baseline)</label>
                                <select value={buildA} onChange={e => setBuildA(e.target.value)} style={sel}>
                                    <option value="">Select build</option>
                                    {sortedBuilds.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>
                            </div>
                            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
                                <label>Build B (Newer)</label>
                                <select value={buildB} onChange={e => setBuildB(e.target.value)} style={sel}>
                                    <option value="">Select build</option>
                                    {sortedBuilds.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>
                            </div>
                        </div>

                        {buildA && buildB && (
                            <div className="animate-in">
                                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '1rem', marginBottom: '1.5rem' }}>
                                    {[
                                        { label: `Only in ${buildA}`, val: onlyA.length, color: '#6366f1' },
                                        { label: `Only in ${buildB}`, val: onlyB.length, color: '#f43f5e' },
                                        { label: 'In both builds', val: inBoth, color: '#10b981' },
                                    ].map(({ label, val, color }) => (
                                        <div key={label} style={{ padding: '1rem', borderRadius: 12, border: '1px solid var(--surface-border)', background: 'var(--surface-card)' }}>
                                            <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>{label}</div>
                                            <div style={{ fontSize: '1.75rem', fontWeight: 800, color }}>{val}</div>
                                        </div>
                                    ))}
                                </div>
                                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.25rem' }}>
                                    {[
                                        { title: `Only in ${buildA}`, items: onlyA, color: '#6366f1' },
                                        { title: `Only in ${buildB}`, items: onlyB, color: '#f43f5e' },
                                    ].map(({ title, items, color }) => (
                                        <div key={title} className="glass-panel" style={{ padding: '0.75rem', border: '1px solid var(--surface-border)' }}>
                                            <div style={{ fontWeight: 700, fontSize: '0.9rem', marginBottom: '0.75rem', color, display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                                <AlertCircle size={16} /> {title} ({items.length})
                                            </div>
                                            <div style={{ maxHeight: 350, overflowY: 'auto', borderRadius: 8, background: 'rgba(0,0,0,0.02)' }}>
                                                {items.length === 0
                                                    ? <div style={{ padding: '1rem', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>None</div>
                                                    : items.map(n => (
                                                        <div key={n} style={{ padding: '8px 12px', fontSize: '0.82rem', borderBottom: '1px solid rgba(0,0,0,0.05)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{n}</div>
                                                    ))}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Deep-dive comparison */}
                    <div className="glass-panel" style={{ padding: '1.25rem' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                            <div>
                                <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 700 }}>🔍 Classifier Deep-Dive Comparison</h3>
                                <p style={{ margin: '4px 0 0', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                                    Compares stored JSON metadata (proximity, sensitivity, etc.)
                                </p>
                            </div>
                            <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.85rem', cursor: 'pointer' }}>
                                <input type="checkbox" checked={showIdentical} onChange={e => setShowIdentical(e.target.checked)} />
                                Show identical classifiers
                            </label>
                        </div>

                        <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', marginBottom: '1.5rem' }}>
                            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
                                <label>Baseline Build</label>
                                <select value={deepBuildA} onChange={e => setDeepBuildA(e.target.value)} style={sel}>
                                    <option value="">Select build</option>
                                    {snapshotBuilds.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>
                            </div>
                            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
                                <label>Newer Build</label>
                                <select value={deepBuildB} onChange={e => setDeepBuildB(e.target.value)} style={sel}>
                                    <option value="">Select build</option>
                                    {snapshotBuilds.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>
                            </div>
                        </div>

                        {(isSnapshotsALoading || isSnapshotsBLoading) ? (
                            <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={28} /></div>
                        ) : (
                            deepBuildA && deepBuildB && (
                                <div className="animate-in">
                                    {filteredDeepDiff.length === 0 ? (
                                        <div style={{ padding: '2rem', textAlign: 'center', border: '1px dashed var(--surface-border)', borderRadius: 12 }}>
                                            <CheckCircle2 size={32} style={{ color: '#10b981', margin: '0 auto 10px' }} />
                                            <div style={{ fontWeight: 600 }}>No differences found</div>
                                            <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>All metadata matches between these two builds.</div>
                                        </div>
                                    ) : (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                                            {filteredDeepDiff.map(r => {
                                                const isExpanded = expandedClassifiers[r.name];
                                                const statusColor = r.status === 'Added' ? '#10b981' : r.status === 'Removed' ? '#f43f5e' : r.status === 'Changed' ? '#f59e0b' : '#64748b';
                                                
                                                return (
                                                    <div key={r.name} className="glass-panel" style={{ padding: 0, overflow: 'hidden', borderLeft: `4px solid ${statusColor}` }}>
                                                        <div 
                                                            style={{ padding: '10px 15px', cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: isExpanded ? 'rgba(0,0,0,0.02)' : 'transparent' }}
                                                            onClick={() => setExpandedClassifiers(prev => ({ ...prev, [r.name]: !prev[r.name] }))}
                                                        >
                                                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                                {isExpanded ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                                                                <span style={{ fontWeight: 700, fontSize: '0.9rem' }}>{r.name}</span>
                                                                <span style={{ 
                                                                    fontSize: '0.7rem', 
                                                                    padding: '2px 8px', 
                                                                    borderRadius: 12, 
                                                                    background: `${statusColor}20`, 
                                                                    color: statusColor, 
                                                                    fontWeight: 700,
                                                                    textTransform: 'uppercase'
                                                                }}>
                                                                    {r.status}
                                                                </span>
                                                            </div>
                                                            {r.status === 'Changed' && (
                                                                <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                                                                    {r.changedKeys.length} keys changed
                                                                </span>
                                                            )}
                                                        </div>
                                                        
                                                        {isExpanded && (
                                                            <div className="animate-in" style={{ padding: '1rem', borderTop: '1px solid var(--surface-border)', background: 'white' }}>
                                                                {r.status === 'Added' ? (
                                                                    <pre style={{ margin: 0, padding: '1rem', background: '#f8fafc', borderRadius: 8, fontSize: '0.8rem', overflowX: 'auto' }}>
                                                                        {JSON.stringify(r.sb, null, 2)}
                                                                    </pre>
                                                                ) : r.status === 'Removed' ? (
                                                                    <pre style={{ margin: 0, padding: '1rem', background: '#f8fafc', borderRadius: 8, fontSize: '0.8rem', overflowX: 'auto' }}>
                                                                        {JSON.stringify(r.sa, null, 2)}
                                                                    </pre>
                                                                ) : (
                                                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                                                        {CLASSIFIER_DETAIL_KEYS.map(key => {
                                                                            const va = String(r.sa![key] ?? '').trim();
                                                                            const vb = String(r.sb![key] ?? '').trim();
                                                                            const isChanged = va !== vb;
                                                                            
                                                                            if (!isChanged && !showIdentical) return null;
                                                                            
                                                                            return (
                                                                                <div key={key} style={{ display: 'grid', gridTemplateColumns: '150px 1fr 1fr', gap: '1rem', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid #f1f5f9' }}>
                                                                                    <span style={{ fontWeight: 600, fontSize: '0.8rem', color: '#64748b' }}>{key}</span>
                                                                                    <div style={{ padding: '4px 8px', borderRadius: 4, background: isChanged ? '#fef2f2' : 'transparent', color: isChanged ? '#991b1b' : 'inherit', fontSize: '0.8rem', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                                                                        {va || <span style={{ fontStyle: 'italic', opacity: 0.5 }}>null</span>}
                                                                                    </div>
                                                                                    <div style={{ padding: '4px 8px', borderRadius: 4, background: isChanged ? '#f0fdf4' : 'transparent', color: isChanged ? '#166534' : 'inherit', fontSize: '0.8rem', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                                                                        {vb || <span style={{ fontStyle: 'italic', opacity: 0.5 }}>null</span>}
                                                                                    </div>
                                                                                </div>
                                                                            );
                                                                        })}
                                                                    </div>
                                                                )}
                                                            </div>
                                                        )}
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )
                        )}
                    </div>
                </>
            )}
        </div>
    );
};

export default ClassifierComparisonPage;
