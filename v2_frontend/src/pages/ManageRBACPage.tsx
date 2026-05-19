import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { 
    Shield, Users, Lock, Plus, Save, Loader2, 
    CheckCircle2, AlertCircle, Trash2, Edit2, X
} from 'lucide-react';
import { API_BASE } from '../apiConfig';

interface ManageRBACPageProps { token: string; }

interface User {
    id: number;
    username: string;
    role_name: string;
    role_id: number;
    created_at: string;
}

interface Role {
    id: number;
    role_name: string;
}

const ALL_PAGES = [
    "Overview", "Build Analysis", "Comparison", "Build DS Runs", "Trend Analysis",
    "Edit Records", "Manage Classifiers",
    "Classifier Changes", "Manage Test Environments", "Version Comparison", "Workflow Automation",
    "Create Data Source", "Scan Status", "Scan Progress"
];

function errMsg(e: unknown): string {
    if (axios.isAxiosError(e)) return String((e.response?.data as { detail?: string })?.detail ?? e.message);
    return String(e);
}

const ManageRBACPage: React.FC<ManageRBACPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const qc = useQueryClient();
    const [tab, setTab] = useState<'users' | 'roles' | 'perms'>('users');
    const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

    // Users Tab State
    const [isAddingUser, setIsAddingUser] = useState(false);
    const [newUserEmail, setNewUserEmail] = useState('');
    const [newUserRole, setNewUserRole] = useState<number | ''>('');
    const [editingUser, setEditingUser] = useState<number | null>(null);
    const [editUserRole, setEditUserRole] = useState<number | ''>('');

    // Roles Tab State
    const [isAddingRole, setIsAddingRole] = useState(false);
    const [newRoleName, setNewRoleName] = useState('');

    // Permissions Tab State
    const [selectedRoleForPerms, setSelectedRoleForPerms] = useState<number | null>(null);
    const [selectedPages, setSelectedPages] = useState<string[]>([]);

    // Queries
    const { data: users = [], isLoading: usersLoading } = useQuery<User[]>({
        queryKey: ['rbac_users'],
        queryFn: async () => (await axios.get(`${API_BASE}/api/rbac/users`, authConfig)).data
    });

    const { data: roles = [], isLoading: rolesLoading } = useQuery<Role[]>({
        queryKey: ['rbac_roles'],
        queryFn: async () => (await axios.get(`${API_BASE}/api/rbac/roles`, authConfig)).data
    });

    const { data: rolePerms = [], isLoading: permsLoading, refetch: refetchPerms } = useQuery<string[]>({
        queryKey: ['rbac_perms', selectedRoleForPerms],
        queryFn: async () => {
            if (!selectedRoleForPerms) return [];
            return (await axios.get(`${API_BASE}/api/rbac/roles/${selectedRoleForPerms}/permissions`, authConfig)).data;
        },
        enabled: !!selectedRoleForPerms,
    });

    React.useEffect(() => {
        if (rolePerms) setSelectedPages(rolePerms);
    }, [rolePerms]);

    // Mutations
    const createUserMut = useMutation({
        mutationFn: async () => await axios.post(`${API_BASE}/api/rbac/users`, { username: newUserEmail, role_id: newUserRole }, authConfig),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['rbac_users'] });
            setIsAddingUser(false); setNewUserEmail(''); setNewUserRole('');
            setBanner({ kind: 'ok', text: 'User created successfully.' });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) })
    });

    const updateUserMut = useMutation({
        mutationFn: async () => await axios.put(`${API_BASE}/api/rbac/users/${editingUser}`, { role_id: editUserRole }, authConfig),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['rbac_users'] });
            setEditingUser(null); setEditUserRole('');
            setBanner({ kind: 'ok', text: 'User role updated.' });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) })
    });

    const createRoleMut = useMutation({
        mutationFn: async () => await axios.post(`${API_BASE}/api/rbac/roles`, { role_name: newRoleName }, authConfig),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['rbac_roles'] });
            setIsAddingRole(false); setNewRoleName('');
            setBanner({ kind: 'ok', text: 'Role created successfully.' });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) })
    });

    const updatePermsMut = useMutation({
        mutationFn: async () => await axios.put(`${API_BASE}/api/rbac/roles/${selectedRoleForPerms}/permissions`, selectedPages, authConfig),
        onSuccess: () => {
            setBanner({ kind: 'ok', text: 'Permissions updated successfully.' });
        },
        onError: e => setBanner({ kind: 'err', text: errMsg(e) })
    });

    const tabStyle = (active: boolean): React.CSSProperties => ({
        padding: '0.75rem 1.5rem',
        fontSize: '0.9rem',
        fontWeight: 600,
        borderRadius: '8px 8px 0 0',
        border: '1px solid var(--surface-border)',
        borderBottom: active ? '2px solid var(--primary-color)' : '1px solid var(--surface-border)',
        background: active ? 'white' : 'rgba(0,0,0,0.03)',
        color: active ? 'var(--primary-color)' : 'var(--text-secondary)',
        cursor: 'pointer',
        transition: 'all 0.2s',
        display: 'flex',
        alignItems: 'center',
        gap: '0.65rem'
    });

    const inputStyle: React.CSSProperties = {
        padding: '0.6rem 0.85rem',
        borderRadius: '8px',
        border: '1px solid var(--surface-border)',
        fontSize: '0.9rem',
        width: '100%',
        fontFamily: 'inherit'
    };

    return (
        <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', paddingBottom: '4rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                <Shield size={28} style={{ color: 'var(--primary-color)' }} />
                <h2 style={{ margin: 0, fontSize: '1.5rem', fontWeight: 800 }}>RBAC & User Management</h2>
            </div>

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
                <button style={tabStyle(tab === 'users')} onClick={() => setTab('users')}><Users size={16} /> Users</button>
                <button style={tabStyle(tab === 'roles')} onClick={() => setTab('roles')}><Users size={16} /> Roles</button>
                <button style={tabStyle(tab === 'perms')} onClick={() => setTab('perms')}><Lock size={16} /> Permissions</button>
            </div>

            <div className="glass-panel" style={{ padding: '2rem', marginTop: '-1.5rem', borderRadius: '0 0 12px 12px' }}>
                {tab === 'users' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <h3 style={{ margin: 0, fontSize: '1.1rem' }}>Active System Users</h3>
                            <button className="btn-primary" onClick={() => setIsAddingUser(true)} style={{ gap: 8 }}>
                                <Plus size={16} /> Add User
                            </button>
                        </div>

                        {isAddingUser && (
                            <div className="animate-in" style={{ padding: '1.25rem', background: '#f8fafc', borderRadius: 12, border: '1px solid var(--surface-border)', display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: '1rem', alignItems: 'end' }}>
                                <div>
                                    <label style={{ fontSize: '0.75rem', fontWeight: 700, display: 'block', marginBottom: 6 }}>EMAIL (@bigid.com)</label>
                                    <input style={inputStyle} value={newUserEmail} onChange={e => setNewUserEmail(e.target.value)} placeholder="user@bigid.com" />
                                </div>
                                <div>
                                    <label style={{ fontSize: '0.75rem', fontWeight: 700, display: 'block', marginBottom: 6 }}>ROLE</label>
                                    <select style={inputStyle} value={newUserRole} onChange={e => setNewUserRole(Number(e.target.value))}>
                                        <option value="">Select Role...</option>
                                        {roles.map(r => <option key={r.id} value={r.id}>{r.role_name}</option>)}
                                    </select>
                                </div>
                                <div style={{ display: 'flex', gap: 8 }}>
                                    <button className="btn-primary" onClick={() => createUserMut.mutate()} disabled={createUserMut.isPending}>
                                        {createUserMut.isPending ? <Loader2 className="lucide-spin" size={16} /> : 'Create'}
                                    </button>
                                    <button className="btn" onClick={() => setIsAddingUser(false)} style={{ background: '#fff' }}>Cancel</button>
                                </div>
                            </div>
                        )}

                        <div style={{ overflowX: 'auto', border: '1px solid var(--surface-border)', borderRadius: 12 }}>
                            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                                <thead style={{ background: '#f8fafc' }}>
                                    <tr>
                                        <th style={{ textAlign: 'left', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>USERNAME</th>
                                        <th style={{ textAlign: 'left', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>ROLE</th>
                                        <th style={{ textAlign: 'left', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>CREATED</th>
                                        <th style={{ textAlign: 'right', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>ACTIONS</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {users.map(u => (
                                        <tr key={u.id} style={{ borderTop: '1px solid var(--surface-border)' }}>
                                            <td style={{ padding: '12px 16px', fontWeight: 500 }}>{u.username}</td>
                                            <td style={{ padding: '12px 16px' }}>
                                                {editingUser === u.id ? (
                                                    <select style={{ ...inputStyle, padding: '4px 8px' }} value={editUserRole} onChange={e => setEditUserRole(Number(e.target.value))}>
                                                        {roles.map(r => <option key={r.id} value={r.id}>{r.role_name}</option>)}
                                                    </select>
                                                ) : (
                                                    <span style={{ padding: '2px 8px', background: 'rgba(99,102,241,0.1)', color: 'var(--primary-color)', borderRadius: 6, fontSize: '0.8rem', fontWeight: 600 }}>
                                                        {u.role_name}
                                                    </span>
                                                )}
                                            </td>
                                            <td style={{ padding: '12px 16px', color: '#64748b', fontSize: '0.85rem' }}>{new Date(u.created_at).toLocaleDateString()}</td>
                                            <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                                                {editingUser === u.id ? (
                                                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                                                        <button onClick={() => updateUserMut.mutate()} disabled={updateUserMut.isPending} style={{ color: '#10b981', background: 'none', border: 'none', cursor: 'pointer' }}><Save size={18} /></button>
                                                        <button onClick={() => setEditingUser(null)} style={{ color: '#ef4444', background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} /></button>
                                                    </div>
                                                ) : (
                                                    <button onClick={() => { setEditingUser(u.id); setEditUserRole(u.role_id); }} style={{ color: 'var(--primary-color)', background: 'none', border: 'none', cursor: 'pointer', opacity: 0.7 }}><Edit2 size={16} /></button>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>
                )}

                {tab === 'roles' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <h3 style={{ margin: 0, fontSize: '1.1rem' }}>System Roles</h3>
                            <button className="btn-primary" onClick={() => setIsAddingRole(true)} style={{ gap: 8 }}>
                                <Plus size={16} /> New Role
                            </button>
                        </div>

                        {isAddingRole && (
                            <div className="animate-in" style={{ padding: '1.25rem', background: '#f8fafc', borderRadius: 12, border: '1px solid var(--surface-border)', display: 'flex', gap: '1rem', alignItems: 'end' }}>
                                <div style={{ flex: 1 }}>
                                    <label style={{ fontSize: '0.75rem', fontWeight: 700, display: 'block', marginBottom: 6 }}>ROLE NAME</label>
                                    <input style={inputStyle} value={newRoleName} onChange={e => setNewRoleName(e.target.value)} placeholder="e.g. Developer, Auditor" />
                                </div>
                                <div style={{ display: 'flex', gap: 8 }}>
                                    <button className="btn-primary" onClick={() => createRoleMut.mutate()} disabled={createRoleMut.isPending}>
                                        {createRoleMut.isPending ? <Loader2 className="lucide-spin" size={16} /> : 'Create'}
                                    </button>
                                    <button className="btn" onClick={() => setIsAddingRole(false)} style={{ background: '#fff' }}>Cancel</button>
                                </div>
                            </div>
                        )}

                        <div style={{ maxWidth: 500, border: '1px solid var(--surface-border)', borderRadius: 12 }}>
                            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                                <thead style={{ background: '#f8fafc' }}>
                                    <tr>
                                        <th style={{ textAlign: 'left', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>ID</th>
                                        <th style={{ textAlign: 'left', padding: '12px 16px', fontSize: '0.8rem', color: '#64748b' }}>ROLE NAME</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {roles.map(r => (
                                        <tr key={r.id} style={{ borderTop: '1px solid var(--surface-border)' }}>
                                            <td style={{ padding: '12px 16px', color: '#64748b', fontSize: '0.85rem' }}>#{r.id}</td>
                                            <td style={{ padding: '12px 16px', fontWeight: 600 }}>{r.role_name}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>
                )}

                {tab === 'perms' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                        <div>
                            <label style={{ fontSize: '0.75rem', fontWeight: 700, display: 'block', marginBottom: 8 }}>SELECT ROLE TO CONFIGURE</label>
                            <select style={{ ...inputStyle, maxWidth: 300 }} value={selectedRoleForPerms || ''} onChange={e => setSelectedRoleForPerms(Number(e.target.value))}>
                                <option value="">-- Choose Role --</option>
                                {roles.map(r => <option key={r.id} value={r.id}>{r.role_name}</option>)}
                            </select>
                        </div>

                        {selectedRoleForPerms && (
                            <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '0.75rem' }}>
                                    {ALL_PAGES.map(p => {
                                        const isSelected = selectedPages.includes(p);
                                        return (
                                            <label key={p} style={{ 
                                                display: 'flex', 
                                                alignItems: 'center', 
                                                gap: '0.75rem', 
                                                padding: '0.75rem 1rem', 
                                                borderRadius: 10, 
                                                border: `1px solid ${isSelected ? 'var(--primary-color)' : 'var(--surface-border)'}`,
                                                background: isSelected ? 'rgba(99,102,241,0.05)' : 'white',
                                                cursor: 'pointer',
                                                transition: 'all 0.2s',
                                                fontSize: '0.85rem'
                                            }}>
                                                <input type="checkbox" checked={isSelected} onChange={e => {
                                                    if (e.target.checked) setSelectedPages([...selectedPages, p]);
                                                    else setSelectedPages(selectedPages.filter(x => x !== p));
                                                }} style={{ width: 18, height: 18 }} />
                                                {p}
                                            </label>
                                        );
                                    })}
                                </div>

                                <div style={{ borderTop: '1px solid var(--surface-border)', paddingTop: '1.5rem', display: 'flex', justifyContent: 'flex-end' }}>
                                    <button className="btn-primary" onClick={() => updatePermsMut.mutate()} disabled={updatePermsMut.isPending || permsLoading} style={{ gap: 8, padding: '0.75rem 2rem' }}>
                                        {updatePermsMut.isPending ? <Loader2 className="lucide-spin" size={20} /> : <Save size={20} />} Update Permissions
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};

export default ManageRBACPage;
