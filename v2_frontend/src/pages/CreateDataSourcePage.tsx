import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { 
    Puzzle, Globe, Info, Play, Save, Loader2, 
    CheckCircle2, AlertCircle, Database, Terminal,
    FileJson, RefreshCw, Send
} from 'lucide-react';
import { API_BASE } from '../apiConfig';

interface CreateDataSourcePageProps { token: string; }

interface Env {
    id: number;
    env_name: string;
    base_url: string;
    is_active: boolean;
}

const CreateDataSourcePage: React.FC<CreateDataSourcePageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const [envId, setEnvId] = useState<number | null>(null);
    const [dsName, setDsName] = useState('');
    const [jsonPayload, setJsonPayload] = useState<string>(JSON.stringify({
        "ds_connection": {
            "name": "",
            "type": "smb_v2",
            "resourceProperties": { "resourceAddress": "" },
            "authStrategy": "basicAuthentication",
            "authenticationProperties": {
                "@authenticationType": "basicAuthentication",
                "username": "",
                "password": ""
            },
            "scanner_group": "default",
            "numberOfParsingThreads": "10",
            "enableLiteMode": true
        },
        "isSavedAsDraft": false
    }, null, 2));
    
    const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
    const [lastResponse, setLastResponse] = useState<any>(null);

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

    // Mutations
    const createDsMut = useMutation({
        mutationFn: async () => {
            const payload = JSON.parse(jsonPayload);
            return await axios.put(`${API_BASE}/api/data-sources`, payload, {
                ...authConfig,
                params: { environment_id: envId, ds_name: dsName || payload.ds_connection?.name }
            });
        },
        onSuccess: (res) => {
            setLastResponse(res.data);
            setBanner({ kind: 'ok', text: 'Data source created/updated successfully.' });
        },
        onError: (e: any) => {
            const msg = e.response?.data?.detail || e.message;
            setBanner({ kind: 'err', text: `Failed: ${msg}` });
        }
    });

    const testConnMut = useMutation({
        mutationFn: async () => {
            const payload = JSON.parse(jsonPayload);
            const broadcastEvent = `${dsName || payload.ds_connection?.name}-test-${Date.now()}`;
            return await axios.post(`${API_BASE}/api/data-sources/test-connection`, {
                ...payload,
                broadcastEvent
            }, {
                ...authConfig,
                params: { environment_id: envId, broadcast_event: broadcastEvent }
            });
        },
        onSuccess: (res) => {
            setLastResponse(res.data);
            setBanner({ kind: 'ok', text: 'Test connection request submitted.' });
        },
        onError: (e: any) => {
            const msg = e.response?.data?.detail || e.message;
            setBanner({ kind: 'err', text: `Test failed: ${msg}` });
        }
    });

    const stepHeader = (num: number, title: string) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1.25rem' }}>
            <div style={{ 
                width: 28, height: 28, borderRadius: '50%', background: 'var(--primary-color)', 
                color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: '0.85rem', fontWeight: 800
            }}>{num}</div>
            <h3 style={{ margin: 0, fontSize: '1.1rem', fontWeight: 700 }}>{title}</h3>
        </div>
    );

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', paddingBottom: '4rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <Puzzle size={28} style={{ color: 'var(--primary-color)' }} />
                <h2 style={{ margin: 0, fontSize: '1.5rem', fontWeight: 800 }}>Create Data Source</h2>
            </div>

            {banner && (
                <div className="animate-in" style={{ 
                    padding: '1rem', borderRadius: 12, 
                    background: banner.kind === 'ok' ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)', 
                    border: `1px solid ${banner.kind === 'ok' ? '#10b981' : '#ef4444'}`, 
                    fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: '0.75rem',
                    color: banner.kind === 'ok' ? '#065f46' : '#991b1b'
                }}>
                    {banner.kind === 'ok' ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
                    {banner.text}
                    <button onClick={() => setBanner(null)} style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', opacity: 0.5 }}>✕</button>
                </div>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem' }}>
                {/* Left Side: Setup */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                    <div className="glass-panel" style={{ padding: '1.5rem' }}>
                        {stepHeader(1, "Environment & Target")}
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                            <div>
                                <label style={{ fontSize: '0.75rem', fontWeight: 700, color: '#64748b', display: 'block', marginBottom: 6 }}>SELECT ENVIRONMENT</label>
                                <select 
                                    style={{ width: '100%', padding: '0.75rem', borderRadius: 10, border: '1px solid var(--surface-border)', fontSize: '0.9rem' }}
                                    value={envId || ''}
                                    onChange={e => setEnvId(Number(e.target.value))}
                                >
                                    {envs.map(e => <option key={e.id} value={e.id}>{e.env_name} ({e.base_url})</option>)}
                                </select>
                            </div>
                            <div>
                                <label style={{ fontSize: '0.75rem', fontWeight: 700, color: '#64748b', display: 'block', marginBottom: 6 }}>DATA SOURCE NAME</label>
                                <input 
                                    style={{ width: '100%', padding: '0.75rem', borderRadius: 10, border: '1px solid var(--surface-border)', fontSize: '0.9rem' }}
                                    placeholder="e.g. SMB_Benchmark_01"
                                    value={dsName}
                                    onChange={e => setDsName(e.target.value)}
                                />
                                <p style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
                                    <Info size={12} /> This will be used in the API path for the PUT request.
                                </p>
                            </div>
                        </div>
                    </div>

                    <div className="glass-panel" style={{ padding: '1.5rem', flex: 1, display: 'flex', flexDirection: 'column' }}>
                        {stepHeader(2, "Connection Payload (JSON)")}
                        <div style={{ position: 'relative', flex: 1, display: 'flex', flexDirection: 'column' }}>
                            <div style={{ 
                                position: 'absolute', top: 12, right: 12, display: 'flex', gap: 8, zIndex: 10
                            }}>
                                <button 
                                    onClick={() => {
                                        try { setJsonPayload(JSON.stringify(JSON.parse(jsonPayload), null, 2)); } catch(e) {}
                                    }}
                                    style={{ padding: '4px 8px', fontSize: '0.7rem', borderRadius: 6, background: 'rgba(0,0,0,0.05)', border: '1px solid #ddd', cursor: 'pointer' }}
                                >
                                    Format
                                </button>
                            </div>
                            <textarea 
                                style={{ 
                                    flex: 1, minHeight: 400, width: '100%', padding: '1rem', 
                                    borderRadius: 10, border: '1px solid var(--surface-border)', 
                                    fontFamily: 'monospace', fontSize: '0.85rem', lineHeight: 1.5,
                                    resize: 'none', background: '#f8fafc'
                                }}
                                value={jsonPayload}
                                onChange={e => setJsonPayload(e.target.value)}
                            />
                        </div>
                        <div style={{ display: 'flex', gap: '1rem', marginTop: '1.5rem' }}>
                            <button 
                                className="btn-primary" 
                                style={{ flex: 1, gap: 8, height: 48 }}
                                onClick={() => createDsMut.mutate()}
                                disabled={createDsMut.isPending}
                            >
                                {createDsMut.isPending ? <Loader2 className="lucide-spin" size={18} /> : <Save size={18} />}
                                Create / Update DS
                            </button>
                            <button 
                                className="btn" 
                                style={{ flex: 1, gap: 8, height: 48, background: 'white' }}
                                onClick={() => testConnMut.mutate()}
                                disabled={testConnMut.isPending}
                            >
                                {testConnMut.isPending ? <Loader2 className="lucide-spin" size={18} /> : <Send size={18} />}
                                Test Connection
                            </button>
                        </div>
                    </div>
                </div>

                {/* Right Side: Response & Preview */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                    <div className="glass-panel" style={{ padding: '1.5rem', flex: 1, display: 'flex', flexDirection: 'column', background: '#1e293b' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1.25rem', color: 'white' }}>
                            <Terminal size={20} style={{ color: '#38bdf8' }} />
                            <h3 style={{ margin: 0, fontSize: '1.1rem', fontWeight: 700 }}>API Response</h3>
                        </div>
                        <div style={{ 
                            flex: 1, background: 'rgba(0,0,0,0.2)', borderRadius: 10, 
                            padding: '1rem', overflowY: 'auto', color: '#cbd5e1',
                            fontFamily: 'monospace', fontSize: '0.8rem', whiteSpace: 'pre-wrap'
                        }}>
                            {lastResponse ? JSON.stringify(lastResponse, null, 2) : "Ready for input..."}
                        </div>
                    </div>

                    <div className="glass-panel" style={{ padding: '1.5rem', background: '#f1f5f9' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1rem' }}>
                            <FileJson size={18} style={{ color: '#6366f1' }} />
                            <h4 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 700 }}>Instructions</h4>
                        </div>
                        <ul style={{ margin: 0, paddingLeft: '1.25rem', fontSize: '0.85rem', color: '#475569', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                            <li>The <strong>Data Source Name</strong> in Step 1 is used for the URL slug.</li>
                            <li>Ensure the <code>ds_connection.name</code> inside the JSON matches if required by your environment.</li>
                            <li><strong>Test Connection</strong> will automatically inject a unique <code>broadcastEvent</code> into the payload.</li>
                            <li>Passwords in JSON should be plain text; the backend handles auth retry and token exchange.</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default CreateDataSourcePage;
