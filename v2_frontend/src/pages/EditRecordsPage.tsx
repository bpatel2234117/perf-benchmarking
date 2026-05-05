import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { 
    Loader2, Wrench, Trash2, Save, Filter, 
    Database, Activity, ChevronRight, ChevronDown,
    CheckCircle2, XCircle, AlertCircle
} from 'lucide-react';
import { API_BASE } from '../apiConfig';
import PaginationBar from '../components/Pagination';

interface EditRecordsPageProps { token: string; }

interface BenchmarkRow {
    id: string | number;
    build_version: string;
    run_date: string | null;
    scenario_type: string | null;
    scan_type: string | null;
    data_source_type: string | null;
    file_set_type: string | null;
    data_type: string | null;
    total_files: number | null;
    total_folders: number | null;
    total_size_gb: number | null;
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
    is_feature_flags: boolean;
    total_catalog_items: number | null;
    is_active: boolean;
    is_release: boolean;
}

const PAGE_SIZE = 50;

function errMsg(e: unknown): string {
    if (axios.isAxiosError(e)) return String((e.response?.data as { detail?: string })?.detail ?? e.message);
    return String(e);
}

const EditRecordsPage: React.FC<EditRecordsPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const qc = useQueryClient();
    const [mode, setMode] = useState<'manual' | 'auto'>('manual');
    const [includeInactive, setIncludeInactive] = useState(false);
    const [search, setSearch] = useState('');
    const [debouncedSearch, setDebouncedSearch] = useState('');
    const [page, setPage] = useState(0);
    const [selected, setSelected] = useState<Set<string | number>>(new Set());
    const [editing, setEditing] = useState<Record<string | number, Partial<BenchmarkRow>>>({});
    const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

    useEffect(() => {
        const t = setTimeout(() => { setDebouncedSearch(search); setPage(0); }, 400);
        return () => clearTimeout(t);
    }, [search]);

    // Reset page and selections when mode/inactive toggles
    useEffect(() => {
        setPage(0);
        setSelected(new Set());
        setEditing({});
    }, [mode, includeInactive]);

    const { data, isLoading } = useQuery({
        queryKey: ['editBenchmarks', token, mode, includeInactive, debouncedSearch, page],
        queryFn: async () => {
            const res = await axios.get<{ items: BenchmarkRow[]; total: number }>(
                `${API_BASE}/api/benchmarks/list`,
                { 
                    ...authConfig, 
                    params: { 
                        mode,
                        include_inactive: includeInactive,
                        search: debouncedSearch || undefined, 
                        limit: PAGE_SIZE, 
                        offset: page * PAGE_SIZE 
                    } 
                }
            );
            return res.data;
        },
    });

    const items = data?.items ?? [];
    const total = data?.total ?? 0;

    const updateMut = useMutation({
        mutationFn: async ({ id, fields }: { id: string | number; fields: Partial<BenchmarkRow> }) => {
            await axios.put(`${API_BASE}/api/benchmarks/${id}`, fields, {
                ...authConfig,
                params: { mode }
            });
        },
        onSuccess: (_, { id }) => {
            qc.invalidateQueries({ queryKey: ['editBenchmarks'] });
            setEditing(prev => { const n = { ...prev }; delete n[id]; return n; });
            setBanner({ kind: 'ok', text: `Saved record #${id}.` });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) }),
    });

    const deleteMut = useMutation({
        mutationFn: async (id: string | number) => {
            await axios.delete(`${API_BASE}/api/benchmarks/${id}`, {
                ...authConfig,
                params: { mode }
            });
        },
        onSuccess: () => { 
            qc.invalidateQueries({ queryKey: ['editBenchmarks'] }); 
            setBanner({ kind: 'ok', text: 'Deleted record.' }); 
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) }),
    });

    const bulkDeleteMut = useMutation({
        mutationFn: async (ids: (string | number)[]) => {
            await axios.post(`${API_BASE}/api/benchmarks/bulk-delete`, { ids }, {
                ...authConfig,
                params: { mode }
            });
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['editBenchmarks'] });
            setSelected(new Set());
            setBanner({ kind: 'ok', text: `Deleted ${selected.size} record(s).` });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) }),
    });

    const toggleSelect = (id: string | number) => setSelected(prev => {
        const n = new Set(prev);
        n.has(id) ? n.delete(id) : n.add(id);
        return n;
    });

    const toggleAll = () => {
        if (selected.size === items.length) setSelected(new Set());
        else setSelected(new Set(items.map(r => r.id)));
    };

    const [expanded, setExpanded] = useState<Set<string | number>>(new Set());

    const toggleExpand = (id: string | number) => setExpanded(prev => {
        const n = new Set(prev);
        n.has(id) ? n.delete(id) : n.add(id);
        return n;
    });

    const setField = (id: string | number, field: keyof BenchmarkRow, val: unknown) => {
        setEditing(prev => ({ ...prev, [id]: { ...prev[id], [field]: val } }));
    };

    const getVal = (row: BenchmarkRow, field: keyof BenchmarkRow) => {
        const e = editing[row.id];
        return (e && field in e) ? e[field] : row[field];
    };

    const inp: React.CSSProperties = { 
        padding: '4px 8px', 
        borderRadius: 4, 
        border: '1px solid var(--surface-border)', 
        fontFamily: 'inherit', 
        fontSize: '0.82rem', 
        width: '100%',
        background: 'white'
    };

    const labelStyle: React.CSSProperties = {
        fontSize: '0.7rem',
        fontWeight: 700,
        color: '#64748b',
        textTransform: 'uppercase',
        marginBottom: 4,
        display: 'block'
    };

    const tabStyle = (active: boolean): React.CSSProperties => ({
        padding: '0.5rem 1.25rem',
        fontSize: '0.9rem',
        fontWeight: 600,
        borderRadius: '8px 8px 0 0',
        border: '1px solid var(--surface-border)',
        borderBottom: active ? '2px solid var(--primary-color)' : '1px solid var(--surface-border)',
        background: active ? 'white' : 'rgba(0,0,0,0.03)',
        color: active ? 'var(--primary-color)' : 'var(--text-secondary)',
        cursor: 'pointer',
        transition: 'all 0.2s'
    });

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', paddingBottom: '3rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                    <Wrench size={26} style={{ color: 'var(--primary-color)' }} />
                    <h2 style={{ margin: 0, fontSize: '1.35rem', fontWeight: 700 }}>Record Management Console</h2>
                </div>
            </div>

            <p style={{ margin: 0, fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
                Edit benchmarks and auto-scans directly. Use the mode toggle to switch between manual and automatic records.
            </p>

            {banner && (
                <div className="animate-in" style={{ 
                    padding: '0.75rem 1rem', 
                    borderRadius: 10, 
                    background: banner.kind === 'ok' ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)', 
                    border: `1px solid ${banner.kind === 'ok' ? '#10b981' : '#ef4444'}`, 
                    fontSize: '0.9rem',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.75rem',
                    color: banner.kind === 'ok' ? '#065f46' : '#991b1b'
                }}>
                    {banner.kind === 'ok' ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
                    {banner.text}
                    <button onClick={() => setBanner(null)} style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', opacity: 0.5 }}>✕</button>
                </div>
            )}

            <div style={{ display: 'flex', borderBottom: '1px solid var(--surface-border)', gap: '4px' }}>
                <button style={tabStyle(mode === 'manual')} onClick={() => setMode('manual')}>
                    <Database size={16} style={{ verticalAlign: 'middle', marginRight: 8 }} />
                    Manual Benchmarks
                </button>
                <button style={tabStyle(mode === 'auto')} onClick={() => setMode('auto')}>
                    <Activity size={16} style={{ verticalAlign: 'middle', marginRight: 8 }} />
                    Auto Scans
                </button>
            </div>

            <div className="glass-panel" style={{ padding: '1.25rem', marginTop: '-1.25rem', borderRadius: '0 0 12px 12px' }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1.25rem', alignItems: 'center' }}>
                    <div className="input-group" style={{ margin: 0, flex: '1 1 300px' }}>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>
                            <Filter size={12} /> Search (Build, Scan Type, DS, ID)
                        </label>
                        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="e.g. 252, S3, full_scan…"
                            style={{ ...inp, padding: '0.65rem 0.85rem', fontSize: '0.9rem' }} />
                    </div>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.65rem', fontSize: '0.9rem', cursor: 'pointer', padding: '0.5rem 0' }}>
                        <input type="checkbox" checked={includeInactive} onChange={e => setIncludeInactive(e.target.checked)} style={{ width: 18, height: 18 }} />
                        Include Inactive Records
                    </label>
                    <div style={{ marginLeft: 'auto', display: 'flex', gap: '0.75rem' }}>
                        {selected.size > 0 && (
                            <button type="button" className="btn"
                                style={{ background: '#ef4444', color: '#fff', display: 'inline-flex', alignItems: 'center', gap: 8, padding: '0.65rem 1rem' }}
                                disabled={bulkDeleteMut.isPending}
                                onClick={() => { if (window.confirm(`Delete ${selected.size} record(s)?`)) bulkDeleteMut.mutate([...selected]); }}>
                                <Trash2 size={16} /> Delete {selected.size}
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {isLoading && <div style={{ padding: '4rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={36} style={{ color: 'var(--primary-color)' }} /></div>}

            {!isLoading && (
                <div className="glass-panel" style={{ padding: 0, overflow: 'hidden' }}>
                    <div style={{ overflowX: 'auto' }}>
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                            <thead>
                                <tr style={{ background: '#f8fafc', borderBottom: '2px solid var(--surface-border)' }}>
                                    <th style={{ padding: '12px 10px', width: 40 }}></th>
                                    <th style={{ padding: '12px 10px', width: 40 }}>
                                        <input type="checkbox" checked={selected.size === items.length && items.length > 0} onChange={toggleAll} />
                                    </th>
                                    <th style={{ padding: '12px 10px', textAlign: 'left', color: '#475569' }}>ID</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'left', color: '#475569' }}>Build</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'left', color: '#475569', minWidth: 120 }}>Run Date</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'left', color: '#475569', minWidth: 140 }}>Scenario / DS Type</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'left', color: '#475569', minWidth: 130 }}>Scan / File Set</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'right', color: '#475569' }}>GB/hr</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'right', color: '#475569' }}>Files/hr</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'center', color: '#475569' }}>Active</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'center', color: '#475569' }}>Rel</th>
                                    <th style={{ padding: '12px 10px', textAlign: 'right', color: '#475569' }}>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {items.map(row => {
                                    const isExpanded = expanded.has(row.id);
                                    const hasEdit = !!editing[row.id] && Object.keys(editing[row.id]).length > 0;
                                    
                                    return (
                                        <React.Fragment key={row.id}>
                                            <tr style={{ 
                                                borderBottom: isExpanded ? 'none' : '1px solid var(--surface-border)', 
                                                background: hasEdit ? 'rgba(99,102,241,0.03)' : undefined,
                                                opacity: row.is_active ? 1 : 0.6
                                            }}>
                                                <td style={{ padding: '10px', cursor: 'pointer' }} onClick={() => toggleExpand(row.id)}>
                                                    {isExpanded ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                                                </td>
                                                <td style={{ padding: '10px' }}>
                                                    <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleSelect(row.id)} />
                                                </td>
                                                <td style={{ padding: '10px', fontFamily: 'ui-monospace, monospace', fontSize: '0.78rem', color: '#64748b' }} title={String(row.id)}>
                                                    {String(row.id).length > 8 ? String(row.id).substring(0, 8) + '…' : row.id}
                                                </td>
                                                <td style={{ padding: '10px', fontWeight: 600 }}>{row.build_version}</td>
                                                <td style={{ padding: '10px' }}>
                                                    <input style={inp} value={String(getVal(row, 'run_date') ?? '')}
                                                        onChange={e => setField(row.id, 'run_date', e.target.value)} />
                                                </td>
                                                <td style={{ padding: '10px' }}>
                                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                                        <input style={{ ...inp, border: 'none', background: 'transparent', padding: 0, fontWeight: 700 }} 
                                                            value={String(getVal(row, 'scenario_type') ?? '')}
                                                            onChange={e => setField(row.id, 'scenario_type', e.target.value)} />
                                                        <input style={{ ...inp, border: 'none', background: 'transparent', padding: 0, fontSize: '0.75rem', color: '#64748b' }} 
                                                            value={String(getVal(row, 'data_source_type') ?? '')}
                                                            onChange={e => setField(row.id, 'data_source_type', e.target.value)} />
                                                    </div>
                                                </td>
                                                <td style={{ padding: '10px' }}>
                                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                                        <input style={{ ...inp, border: 'none', background: 'transparent', padding: 0, fontWeight: 600 }} 
                                                            value={String(getVal(row, 'scan_type') ?? '')}
                                                            onChange={e => setField(row.id, 'scan_type', e.target.value)} />
                                                        <input style={{ ...inp, border: 'none', background: 'transparent', padding: 0, fontSize: '0.75rem', color: '#64748b' }} 
                                                            value={String(getVal(row, 'file_set_type') ?? '')}
                                                            onChange={e => setField(row.id, 'file_set_type', e.target.value)} />
                                                    </div>
                                                </td>
                                                <td style={{ padding: '10px' }}>
                                                    <input type="number" style={{ ...inp, textAlign: 'right', border: 'none', background: 'transparent', fontWeight: 700, color: 'var(--primary-color)' }}
                                                        value={String(getVal(row, 'throughput_cluster_gb_hr') ?? '')}
                                                        onChange={e => setField(row.id, 'throughput_cluster_gb_hr', e.target.value === '' ? null : parseFloat(e.target.value))} />
                                                </td>
                                                <td style={{ padding: '10px' }}>
                                                    <input type="number" style={{ ...inp, textAlign: 'right', border: 'none', background: 'transparent' }}
                                                        value={String(getVal(row, 'throughput_cluster_files_hr') ?? '')}
                                                        onChange={e => setField(row.id, 'throughput_cluster_files_hr', e.target.value === '' ? null : parseFloat(e.target.value))} />
                                                </td>
                                                <td style={{ padding: '10px', textAlign: 'center' }}>
                                                    <input type="checkbox" checked={Boolean(getVal(row, 'is_active'))}
                                                        onChange={e => setField(row.id, 'is_active', e.target.checked)} style={{ width: 16, height: 16 }} />
                                                </td>
                                                <td style={{ padding: '10px', textAlign: 'center' }}>
                                                    <input type="checkbox" checked={Boolean(getVal(row, 'is_release'))}
                                                        onChange={e => setField(row.id, 'is_release', e.target.checked)} style={{ width: 16, height: 16 }} />
                                                </td>
                                                <td style={{ padding: '10px', textAlign: 'right' }}>
                                                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                                                        {hasEdit && (
                                                            <button type="button" className="btn-primary"
                                                                disabled={updateMut.isPending}
                                                                onClick={() => updateMut.mutate({ id: row.id, fields: editing[row.id] })}
                                                                style={{ padding: '5px 10px', fontSize: '0.8rem' }}>
                                                                {updateMut.isPending ? <Loader2 size={14} className="lucide-spin" /> : <Save size={14} />}
                                                            </button>
                                                        )}
                                                        <button type="button" 
                                                            disabled={deleteMut.isPending}
                                                            onClick={() => { if (window.confirm(`Permanently delete record #${row.id}?`)) deleteMut.mutate(row.id); }}
                                                            style={{ background: 'transparent', border: 'none', color: '#ef4444', padding: '5px', cursor: 'pointer', opacity: 0.7 }}>
                                                            <Trash2 size={16} />
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                            {isExpanded && (
                                                <tr style={{ background: '#f8fafc', borderBottom: '1px solid var(--surface-border)' }}>
                                                    <td colSpan={12} style={{ padding: '1.25rem 2rem' }}>
                                                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '1.25rem' }}>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Data Type</label>
                                                                <input style={inp} value={String(getVal(row, 'data_type') ?? '')} onChange={e => setField(row.id, 'data_type', e.target.value)} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Total Files</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'total_files') ?? '')} onChange={e => setField(row.id, 'total_files', e.target.value === '' ? null : parseInt(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Total Size (GB)</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'total_size_gb') ?? '')} onChange={e => setField(row.id, 'total_size_gb', e.target.value === '' ? null : parseFloat(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Scan Hours</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'total_scan_hours') ?? '')} onChange={e => setField(row.id, 'total_scan_hours', e.target.value === '' ? null : parseFloat(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Catalog Items</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'total_catalog_items') ?? '')} onChange={e => setField(row.id, 'total_catalog_items', e.target.value === '' ? null : parseInt(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Avg File Size</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'avg_file_size') ?? '')} onChange={e => setField(row.id, 'avg_file_size', e.target.value === '' ? null : parseFloat(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Scanners</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'scanner_count') ?? '')} onChange={e => setField(row.id, 'scanner_count', e.target.value === '' ? null : parseInt(e.target.value))} />
                                                            </div>
                                                            <div className="input-group" style={{ margin: 0 }}>
                                                                <label style={labelStyle}>Threads</label>
                                                                <input type="number" style={inp} value={String(getVal(row, 'thread_count') ?? '')} onChange={e => setField(row.id, 'thread_count', e.target.value === '' ? null : parseInt(e.target.value))} />
                                                            </div>
                                                        </div>
                                                        {hasEdit && (
                                                            <div style={{ marginTop: '1.25rem', display: 'flex', justifyContent: 'flex-end' }}>
                                                                <button type="button" className="btn-primary"
                                                                    disabled={updateMut.isPending}
                                                                    onClick={() => updateMut.mutate({ id: row.id, fields: editing[row.id] })}
                                                                    style={{ padding: '8px 20px', fontWeight: 700 }}>
                                                                    {updateMut.isPending ? <Loader2 size={16} className="lucide-spin" /> : <Save size={16} />} Save Changes
                                                                </button>
                                                            </div>
                                                        )}
                                                    </td>
                                                </tr>
                                            )}
                                        </React.Fragment>
                                    );
                                })}
                                {items.length === 0 && (
                                    <tr><td colSpan={13} style={{ padding: '4rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
                                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '0.75rem' }}>
                                            <AlertCircle size={32} style={{ opacity: 0.3 }} />
                                            <span>No records found matching your criteria.</span>
                                        </div>
                                    </td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                    {total > PAGE_SIZE && (
                        <div style={{ padding: '1rem', borderTop: '1px solid var(--surface-border)', background: '#f8fafc' }}>
                            <PaginationBar page={page} pageSize={PAGE_SIZE} totalItems={total} onPageChange={setPage} ariaLabel="Edit records pagination" />
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};

export default EditRecordsPage;
