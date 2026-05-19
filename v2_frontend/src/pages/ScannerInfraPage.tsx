import React, { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { 
    Monitor, Server, Cpu, HardDrive, Filter, 
    Loader2, AlertCircle, RefreshCcw, Info, Calendar
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
}

const ScannerInfraPage: React.FC<ScannerInfraPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const [envId, setEnvId] = useState<number | null>(null);
    const [days, setDays] = useState(7);
    const [showInactive, setShowInactive] = useState(false);

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

    // Fetch Metrics
    const { data, isLoading, isError, refetch } = useQuery({
        queryKey: ['scanner-performance', envId, days],
        queryFn: async () => {
            if (!envId) return null;
            const res = await axios.get(`${API_BASE}/api/scanner-metrics/performance`, {
                ...authConfig,
                params: { environment_id: envId, days }
            });
            return res.data;
        },
        enabled: !!envId,
    });

    const individualMetrics: ScannerMetric[] = data?.individual ?? [];

    // Group data by display_name for Recharts
    const chartData = useMemo(() => {
        const timeMap: Record<string, any> = {};
        individualMetrics.forEach(m => {
            const time = new Date(m.captured_at_utc).toLocaleString();
            if (!timeMap[time]) timeMap[time] = { time, timestamp: new Date(m.captured_at_utc).getTime() };
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

    const colors = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];

    const renderChart = (type: 'cpu' | 'mem', title: string, icon: React.ReactNode, yLabel: string, yDomain: [number, number | string]) => {
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
                            <XAxis dataKey="time" hide />
                            <YAxis domain={yDomain} unit="%" fontSize={12} tickLine={false} axisLine={false} />
                            <Tooltip contentStyle={{ borderRadius: 12, border: 'none', boxShadow: '0 10px 15px -3px rgba(0,0,0,0.1)' }} />
                            <Legend verticalAlign="bottom" height={36} />
                            {scannerNames.map((name, idx) => (
                                <Line 
                                    key={name} 
                                    type="monotone" 
                                    dataKey={`${name}_${type}`} 
                                    name={name} 
                                    stroke={colors[idx % colors.length]} 
                                    strokeWidth={2}
                                    dot={false}
                                    activeDot={{ r: 6 }}
                                />
                            ))}
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

                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flex: 1, minWidth: 200 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <label style={{ fontSize: '0.7rem', fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>History Window</label>
                        <span style={{ fontSize: '0.85rem', fontWeight: 600 }}>{days} Days</span>
                    </div>
                    <input 
                        type="range" min="1" max="30" value={days} 
                        onChange={e => setDays(Number(e.target.value))}
                        style={{ width: '100%', cursor: 'pointer', accentColor: 'var(--primary-color)' }} 
                    />
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginTop: 'auto', paddingBottom: 4 }}>
                    <Calendar size={16} style={{ color: '#64748b' }} />
                    <span style={{ fontSize: '0.85rem', color: '#64748b' }}>
                        {new Date(Date.now() - days * 86400000).toLocaleDateString()} – {new Date().toLocaleDateString()}
                    </span>
                </div>
            </div>

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
                    <h3 style={{ margin: 0, fontSize: '1.1rem' }}>Active Scanners Detail</h3>
                </div>
                <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                        <thead style={{ background: '#f8fafc' }}>
                            <tr>
                                <th style={{ textAlign: 'left', padding: '12px 16px' }}>Scanner Name</th>
                                <th style={{ textAlign: 'left', padding: '12px 16px' }}>Status</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>CPU Avg</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>Mem Avg</th>
                                <th style={{ textAlign: 'right', padding: '12px 16px' }}>Last Seen</th>
                            </tr>
                        </thead>
                        <tbody>
                            {scannerNames.map(name => {
                                const metrics = individualMetrics.filter(m => m.display_name === name);
                                const last = metrics[metrics.length - 1];
                                const avgCpu = metrics.reduce((acc, m) => acc + m.cpu_pct, 0) / metrics.length;
                                const avgMem = metrics.reduce((acc, m) => acc + m.memory_usage_pct, 0) / metrics.length;
                                
                                return (
                                    <tr key={name} style={{ borderTop: '1px solid var(--surface-border)' }}>
                                        <td style={{ padding: '12px 16px', fontWeight: 600 }}>{name}</td>
                                        <td style={{ padding: '12px 16px' }}>
                                            <span style={{ 
                                                padding: '2px 8px', borderRadius: 6, fontSize: '0.75rem', fontWeight: 700,
                                                background: last?.memory_usage_pct > 90 ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.1)',
                                                color: last?.memory_usage_pct > 90 ? '#ef4444' : '#10b981'
                                            }}>
                                                ONLINE
                                            </span>
                                        </td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>{avgCpu.toFixed(1)}%</td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right' }}>{avgMem.toFixed(1)}%</td>
                                        <td style={{ padding: '12px 16px', textAlign: 'right', color: '#64748b' }}>{last ? new Date(last.captured_at_utc).toLocaleTimeString() : 'N/A'}</td>
                                    </tr>
                                );
                            })}
                            {scannerNames.length === 0 && !isLoading && (
                                <tr><td colSpan={5} style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>No active scanners detected.</td></tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
};

export default ScannerInfraPage;
