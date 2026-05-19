import React, { useState } from 'react';
import { Loader2, Plus, TerminalSquare, Pencil, Trash2, Unlock } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { API_BASE } from '../apiConfig';

interface EnvironmentsProps {
  token: string;
}

interface EnvironmentRow {
  id: number;
  env_name: string;
  base_url: string;
  is_active: boolean;
  is_busy: boolean;
  auth_module_path?: string | null;
  auth_function_name?: string | null;
}

const emptyForm = () => ({
  env_name: '',
  base_url: '',
  auth_module_path: '',
  auth_function_name: 'Get_auth_token',
  is_active: true,
});

function errDetail(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const d = err.response?.data;
    if (typeof d?.detail === 'string') return d.detail;
    if (Array.isArray(d?.detail)) return JSON.stringify(d.detail);
  }
  return err instanceof Error ? err.message : String(err);
}

const Environments: React.FC<EnvironmentsProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState(emptyForm());

  const { data: environments, isLoading: envLoading } = useQuery({
    queryKey: ['environments'],
    queryFn: async () => {
      const res = await axios.get<EnvironmentRow[]>(`${API_BASE}/api/environments`, authConfig);
      return res.data;
    },
    refetchInterval: 5000,
  });

  const createMut = useMutation({
    mutationFn: async () => {
      await axios.post(`${API_BASE}/api/environments`, form, authConfig);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setModalOpen(false);
      setEditingId(null);
      setForm(emptyForm());
      setBanner({ kind: 'ok', text: 'Environment created.' });
    },
    onError: (e) => setBanner({ kind: 'err', text: errDetail(e) }),
  });

  const updateMut = useMutation({
    mutationFn: async () => {
      if (editingId == null) return;
      const payload: Record<string, unknown> = {
        env_name: form.env_name.trim(),
        base_url: form.base_url.trim(),
        auth_module_path: form.auth_module_path.trim(),
        auth_function_name: form.auth_function_name.trim() || 'Get_auth_token',
        is_active: form.is_active,
      };
      await axios.put(`${API_BASE}/api/environments/${editingId}`, payload, authConfig);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setModalOpen(false);
      setEditingId(null);
      setForm(emptyForm());
      setBanner({ kind: 'ok', text: 'Environment updated.' });
    },
    onError: (e) => setBanner({ kind: 'err', text: errDetail(e) }),
  });

  const deleteMut = useMutation({
    mutationFn: async (id: number) => {
      await axios.delete(`${API_BASE}/api/environments/${id}`, authConfig);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setBanner({ kind: 'ok', text: 'Environment deleted.' });
    },
    onError: (e) => setBanner({ kind: 'err', text: errDetail(e) }),
  });

  const clearLocksMut = useMutation({
    mutationFn: async () => {
      const res = await axios.post<{ ok?: boolean; cleared_rows?: number; detail?: string }>(
        `${API_BASE}/api/environments/actions/clear-busy-locks`,
        {},
        authConfig
      );
      return res.data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      const n = data?.cleared_rows ?? 0;
      setBanner({
        kind: 'ok',
        text:
          typeof data?.detail === 'string'
            ? `${data.detail} (${n} row${n === 1 ? '' : 's'} cleared.)`
            : `Cleared busy locks (${n} row${n === 1 ? '' : 's'}).`,
      });
    },
    onError: (e) => setBanner({ kind: 'err', text: errDetail(e) }),
  });

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm());
    setModalOpen(true);
    setBanner(null);
  };

  const openEdit = (env: EnvironmentRow) => {
    setEditingId(env.id);
    setForm({
      env_name: env.env_name,
      base_url: env.base_url,
      auth_module_path: env.auth_module_path ?? '',
      auth_function_name: env.auth_function_name?.trim() || 'Get_auth_token',
      is_active: env.is_active,
    });
    setModalOpen(true);
    setBanner(null);
  };

  const save = () => {
    setBanner(null);
    if (!form.env_name.trim() || !form.base_url.trim()) {
      setBanner({ kind: 'err', text: 'Name and base URL are required.' });
      return;
    }
    if (editingId == null) createMut.mutate();
    else updateMut.mutate();
  };

  const requestDelete = (env: EnvironmentRow) => {
    setBanner(null);
    const ok = window.confirm(
      `Delete environment "${env.env_name}"?\n\nIf delete fails (still referenced by workflows or scans), deactivate it instead or remove dependent data first.`
    );
    if (!ok) return;
    deleteMut.mutate(env.id);
  };

  const pending = createMut.isPending || updateMut.isPending || deleteMut.isPending || clearLocksMut.isPending;

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      {banner && (
        <div
          role="status"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: '8px',
            border: `1px solid ${banner.kind === 'ok' ? 'var(--success-muted, #2d6a4f)' : 'var(--danger-color)'}`,
            background: banner.kind === 'ok' ? 'rgba(45, 106, 79, 0.15)' : 'rgba(180, 50, 50, 0.12)',
            color: 'var(--text-primary)',
            fontSize: '0.875rem',
          }}
        >
          {banner.text}
        </div>
      )}

      <section>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem', flexWrap: 'wrap', gap: '0.75rem' }}>
          <h2 style={{ color: 'var(--primary-color)', fontSize: '1.25rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <TerminalSquare size={20} /> Test Environments Setup
          </h2>
          <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn"
              disabled={pending}
              onClick={() => clearLocksMut.mutate()}
              title="Clears is_busy locks left by a crashed or stuck workflow"
            >
              <Unlock size={16} /> Clear workflow locks
            </button>
            <button type="button" className="btn" onClick={openCreate} disabled={pending}>
              <Plus size={16} /> Add environment
            </button>
          </div>
        </div>

        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {envLoading ? (
            <div style={{ padding: '2rem', textAlign: 'center' }}>
              <Loader2 className="lucide-spin" />
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Environment name</th>
                  <th>Base URL</th>
                  <th>Status</th>
                  <th>Activity</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {environments?.map((env) => (
                  <tr key={env.id}>
                    <td style={{ fontWeight: 500 }}>{env.env_name}</td>
                    <td style={{ wordBreak: 'break-all', maxWidth: '280px' }}>{env.base_url}</td>
                    <td>
                      <span className={`badge ${env.is_active ? 'active' : 'inactive'}`}>{env.is_active ? 'Online' : 'Offline'}</span>
                    </td>
                    <td>
                      {env.is_busy ? (
                        <span style={{ color: 'var(--danger-color)', fontSize: '0.875rem', fontWeight: 600 }}>Busy (workflow)</span>
                      ) : (
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>Idle</span>
                      )}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                        <button
                          type="button"
                          className="btn"
                          style={{
                            padding: '0.25rem 0.5rem',
                            fontSize: '0.75rem',
                            background: 'transparent',
                            border: '1px solid var(--surface-border)',
                            color: 'var(--text-primary)',
                          }}
                          disabled={pending}
                          onClick={() => openEdit(env)}
                        >
                          <Pencil size={12} style={{ verticalAlign: 'middle', marginRight: 4 }} />
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn"
                          style={{
                            padding: '0.25rem 0.5rem',
                            fontSize: '0.75rem',
                            background: 'transparent',
                            border: '1px solid var(--danger-color)',
                            color: 'var(--danger-color)',
                          }}
                          disabled={pending}
                          onClick={() => requestDelete(env)}
                        >
                          <Trash2 size={12} style={{ verticalAlign: 'middle', marginRight: 4 }} />
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {modalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.45)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
            padding: '1rem',
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setModalOpen(false);
          }}
        >
          <div className="glass-panel" style={{ width: '100%', maxWidth: '440px', padding: '1.25rem' }}>
            <h3 style={{ marginTop: 0, marginBottom: '1rem', color: 'var(--primary-color)' }}>
              {editingId == null ? 'Add environment' : 'Edit environment'}
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-name">Name</label>
                <input
                  id="env-modal-name"
                  value={form.env_name}
                  onChange={(e) => setForm((f) => ({ ...f, env_name: e.target.value }))}
                  placeholder="e.g. perf-lab-01"
                  autoComplete="off"
                  style={{
                    padding: '0.5rem 0.75rem',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    width: '100%',
                    fontFamily: 'inherit',
                  }}
                />
              </div>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-url">Base URL</label>
                <input
                  id="env-modal-url"
                  value={form.base_url}
                  onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                  placeholder="https://tenant.bigid.cloud"
                  autoComplete="off"
                  style={{
                    padding: '0.5rem 0.75rem',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    width: '100%',
                    fontFamily: 'inherit',
                  }}
                />
              </div>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-apath">Auth module path (optional)</label>
                <input
                  id="env-modal-apath"
                  value={form.auth_module_path}
                  onChange={(e) => setForm((f) => ({ ...f, auth_module_path: e.target.value }))}
                  placeholder="Python module path for token helper"
                  autoComplete="off"
                  style={{
                    padding: '0.5rem 0.75rem',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    width: '100%',
                    fontFamily: 'inherit',
                  }}
                />
              </div>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-afn">Auth function name</label>
                <input
                  id="env-modal-afn"
                  value={form.auth_function_name}
                  onChange={(e) => setForm((f) => ({ ...f, auth_function_name: e.target.value }))}
                  placeholder="Get_auth_token"
                  autoComplete="off"
                  style={{
                    padding: '0.5rem 0.75rem',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    width: '100%',
                    fontFamily: 'inherit',
                  }}
                />
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.875rem', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={form.is_active}
                  onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))}
                />
                Active (online)
              </label>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginTop: '1.25rem' }}>
              <button type="button" className="btn" style={{ background: 'transparent' }} onClick={() => setModalOpen(false)} disabled={pending}>
                Cancel
              </button>
              <button type="button" className="btn" onClick={save} disabled={pending}>
                {pending ? <Loader2 className="lucide-spin" size={16} /> : editingId == null ? 'Create' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Environments;
