import React, { useState } from 'react';
import { Loader2, Plus, TerminalSquare, Pencil, Trash2, Unlock, RefreshCw } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { API_BASE } from '../apiConfig';
import { useUserRole } from '../hooks/useUserRole';

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
  bigid_username?: string | null;
  version?: string | null;
  classifier_count?: number;
  include_in_workflow_runs: boolean;
  license_sync_ok?: boolean;
  license_sync_message?: string;
}

function normalizeBaseUrlInput(url: string): string {
  let s = url.trim();
  while (s.endsWith('/')) {
    s = s.slice(0, -1);
  }
  return s;
}

const emptyForm = () => ({
  env_name: '',
  base_url: '',
  username: '',
  password: '',
  auth_module_path: '',
  auth_function_name: 'Get_auth_token',
  is_active: true,
  include_in_workflow_runs: true,
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
  const isAdmin = useUserRole(token) === 'Admin';  // clear-busy-locks is Admin-only (backend require_admin)
  const queryClient = useQueryClient();
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState(emptyForm());
  const [syncingVersionId, setSyncingVersionId] = useState<number | null>(null);

  const {
    data: environments,
    isLoading: envLoading,
    isError: envError,
    error: envQueryError,
    refetch: refetchEnvironments,
  } = useQuery({
    queryKey: ['environments', token, API_BASE],
    queryFn: async () => {
      const res = await axios.get<EnvironmentRow[]>(`${API_BASE}/api/environments`, authConfig);
      return res.data;
    },
    refetchInterval: 5000,
    retry: 1,
  });

  const syncVersionForEnv = async (envId: number, envName: string) => {
    setSyncingVersionId(envId);
    try {
      // 1. Fetch & store version from BigID /about
      const res = await axios.get<{ version?: string; build_version?: string }>(
        `${API_BASE}/api/environments/${envId}/build-info`,
        authConfig
      );
      const ver = res.data?.version || res.data?.build_version;

      // 2. Fetch classifiers from BigID and store in DB
      let classifierNote = '';
      try {
        const cr = await axios.post<{ ok: boolean; classifier_count?: number; removed_links?: number }>(
          `${API_BASE}/api/environments/${envId}/sync-classifiers`,
          {},
          authConfig
        );
        const count = cr.data?.classifier_count ?? 0;
        const removed = cr.data?.removed_links ?? 0;
        classifierNote = cr.data?.ok
          ? ` ${count} classifiers synced${removed ? ` (${removed} stale removed)` : ''}.`
          : ' No classifiers found.';
      } catch {
        classifierNote = ' Classifier sync skipped.';
      }

      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setBanner({
        kind: 'ok',
        text: ver
          ? `${envName}: version synced — ${ver}.${classifierNote}`
          : `${envName}: saved.${classifierNote}`,
      });
    } catch {
      setBanner({ kind: 'ok', text: `${envName}: saved. Sync skipped (BigID unreachable).` });
    } finally {
      setSyncingVersionId(null);
    }
  };

  const createMut = useMutation({
    mutationFn: async () => {
      const res = await axios.post<EnvironmentRow>(`${API_BASE}/api/environments`, {
        env_name: form.env_name,
        base_url: normalizeBaseUrlInput(form.base_url),
        is_active: form.is_active,
        include_in_workflow_runs: form.include_in_workflow_runs,
        auth_module_path: form.auth_module_path,
        auth_function_name: form.auth_function_name,
        username: form.username || undefined,
        password: form.password || undefined,
      }, authConfig);
      return res.data;
    },
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setModalOpen(false);
      setEditingId(null);
      setForm(emptyForm());
      const lic = created.license_sync_message
        ? ` ${created.license_sync_ok ? created.license_sync_message : `License: ${created.license_sync_message}`}`
        : '';
      if (created.license_sync_ok === false) {
        setBanner({
          kind: 'err',
          text: `Environment "${created.env_name}" created but license was NOT applied.${lic} Set BIGID_LICENSE_TOKEN in .env and edit/save again.`,
        });
        return;
      }
      setBanner({
        kind: 'ok',
        text: `Environment "${created.env_name}" created.${lic} Syncing version…`,
      });
      syncVersionForEnv(created.id, created.env_name);
    },
    onError: (e) => setBanner({ kind: 'err', text: errDetail(e) }),
  });

  const updateMut = useMutation({
    mutationFn: async () => {
      if (editingId == null) return null;
      const payload: Record<string, unknown> = {
        env_name: form.env_name.trim(),
        base_url: normalizeBaseUrlInput(form.base_url),
        auth_module_path: form.auth_module_path.trim(),
        auth_function_name: form.auth_function_name.trim() || 'Get_auth_token',
        is_active: form.is_active,
        include_in_workflow_runs: form.include_in_workflow_runs,
        username: form.username || undefined,
        password: form.password || undefined,
      };
      const res = await axios.put<EnvironmentRow>(
        `${API_BASE}/api/environments/${editingId}`,
        payload,
        authConfig
      );
      return res.data;
    },
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: ['environments'] });
      setModalOpen(false);
      setEditingId(null);
      setForm(emptyForm());
      if (updated) {
        const lic = updated.license_sync_message
          ? ` ${updated.license_sync_ok ? updated.license_sync_message : `License: ${updated.license_sync_message}`}`
          : '';
        if (updated.license_sync_ok === false) {
          setBanner({
            kind: 'err',
            text: `Environment "${updated.env_name}" saved but license was NOT applied.${lic} Fix BIGID_LICENSE_TOKEN / auth, then save again before running workflow.`,
          });
          return;
        }
        setBanner({
          kind: 'ok',
          text: `Environment "${updated.env_name}" updated.${lic} Syncing version…`,
        });
        syncVersionForEnv(updated.id, updated.env_name);
      }
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
      base_url: normalizeBaseUrlInput(env.base_url),
      username: env.bigid_username || '',
      password: '',  // never pre-fill password
      auth_module_path: env.auth_module_path ?? '',
      auth_function_name: env.auth_function_name?.trim() || 'Get_auth_token',
      is_active: env.is_active,
      include_in_workflow_runs: env.include_in_workflow_runs ?? true,
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
            {isAdmin && (
            <button
              type="button"
              className="btn"
              disabled={pending}
              onClick={() => clearLocksMut.mutate()}
              title="Clears is_busy locks left by a crashed or stuck workflow"
            >
              <Unlock size={16} /> Clear workflow locks
            </button>
            )}
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
          ) : envError ? (
            <div style={{ padding: '1.5rem', color: 'var(--danger-color)' }}>
              <p style={{ margin: '0 0 0.75rem 0', fontWeight: 600 }}>Could not load environments from the API.</p>
              <p style={{ margin: '0 0 0.75rem 0', fontSize: '0.875rem' }}>{errDetail(envQueryError)}</p>
              <p style={{ margin: '0 0 0.75rem 0', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                API: {API_BASE}/api/environments — ensure <code>spb_api</code> is running and rebuild <code>spb_ui</code> after deploy.
              </p>
              <button type="button" className="btn" onClick={() => refetchEnvironments()}>
                Retry
              </button>
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Environment name</th>
                  <th>Base URL</th>
                  <th>Username</th>
                  <th>Version</th>
                  <th>Classifiers</th>
                  <th>Status</th>
                  <th>Activity</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {!environments?.length ? (
                  <tr>
                    <td colSpan={8} style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
                      No environments yet. Click <strong>Add environment</strong> to create one.
                    </td>
                  </tr>
                ) : null}
                {environments?.map((env) => (
                  <tr key={env.id}>
                    <td style={{ fontWeight: 500 }}>{env.env_name}</td>
                    <td style={{ wordBreak: 'break-all', maxWidth: '280px' }}>{env.base_url}</td>
                    <td style={{ fontSize: '0.8rem', color: env.bigid_username ? 'var(--text-primary)' : 'var(--text-secondary)' }}>
                      {env.bigid_username || <em>default</em>}
                    </td>
                    <td style={{ fontSize: '0.8rem', fontFamily: 'monospace', color: env.version ? 'var(--primary-color)' : 'var(--text-secondary)' }}>
                      {env.version || '—'}
                    </td>
                    <td style={{ fontSize: '0.8rem', textAlign: 'center', color: (env.classifier_count ?? 0) > 0 ? 'var(--text-primary)' : 'var(--text-secondary)' }}>
                      {(env.classifier_count ?? 0) > 0 ? env.classifier_count : '—'}
                    </td>
                    <td>
                      <span className={`badge ${env.is_active ? 'active' : 'inactive'}`}>{env.is_active ? 'Online' : 'Offline'}</span>
                    </td>
                    <td>
                      {env.is_busy ? (
                        <span style={{ color: 'var(--danger-color)', fontSize: '0.875rem', fontWeight: 600 }}>Busy (workflow)</span>
                      ) : env.include_in_workflow_runs === false ? (
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', fontStyle: 'italic' }}>Excluded from workflows</span>
                      ) : (
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>Idle</span>
                      )}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid var(--surface-border)', color: 'var(--text-primary)' }}
                          disabled={pending || syncingVersionId === env.id}
                          onClick={() => openEdit(env)}
                        >
                          <Pencil size={12} style={{ verticalAlign: 'middle', marginRight: 4 }} />
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid var(--surface-border)', color: 'var(--text-secondary)' }}
                          disabled={pending || syncingVersionId === env.id}
                          title="Sync version (BigID /about) and enabled classifiers from BigID"
                          onClick={() => {
                            setBanner(null);
                            syncVersionForEnv(env.id, env.env_name);
                          }}
                        >
                          {syncingVersionId === env.id
                            ? <Loader2 size={12} className="lucide-spin" style={{ verticalAlign: 'middle', marginRight: 4 }} />
                            : <RefreshCw size={12} style={{ verticalAlign: 'middle', marginRight: 4 }} />}
                          Sync version
                        </button>
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid var(--danger-color)', color: 'var(--danger-color)' }}
                          disabled={pending || syncingVersionId === env.id}
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
            <h3 style={{ marginTop: 0, marginBottom: '0.25rem', color: 'var(--primary-color)' }}>
              {editingId == null ? 'Add environment' : 'Edit environment'}
            </h3>
            <p style={{ margin: '0 0 1rem 0', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
              Version and classifier details will be fetched automatically after saving.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-name">Name <span style={{ color: 'var(--danger-color)' }}>*</span></label>
                <input
                  id="env-modal-name"
                  value={form.env_name}
                  onChange={(e) => setForm((f) => ({ ...f, env_name: e.target.value }))}
                  placeholder="e.g. perf-lab-01"
                  autoComplete="off"
                  style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', width: '100%', fontFamily: 'inherit' }}
                />
              </div>
              <div className="input-group" style={{ margin: 0 }}>
                <label htmlFor="env-modal-url">Base URL <span style={{ color: 'var(--danger-color)' }}>*</span></label>
                <input
                  id="env-modal-url"
                  value={form.base_url}
                  onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                  placeholder="http://your-bigid-host.elb.amazonaws.com (no trailing /)"
                  autoComplete="off"
                  style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', width: '100%', fontFamily: 'inherit' }}
                />
              </div>
              <div style={{ display: 'flex', gap: '0.75rem' }}>
                <div className="input-group" style={{ margin: 0, flex: 1 }}>
                  <label htmlFor="env-modal-user">Username (optional)</label>
                  <input
                    id="env-modal-user"
                    value={form.username}
                    onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
                    placeholder="bigid"
                    autoComplete="off"
                    style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', width: '100%', fontFamily: 'inherit' }}
                  />
                </div>
                <div className="input-group" style={{ margin: 0, flex: 1 }}>
                  <label htmlFor="env-modal-pass">Password (optional)</label>
                  <input
                    id="env-modal-pass"
                    type="password"
                    value={form.password}
                    onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                    placeholder="••••••••"
                    autoComplete="new-password"
                    style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', width: '100%', fontFamily: 'inherit' }}
                  />
                </div>
              </div>
              <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.875rem', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={form.is_active}
                    onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))}
                  />
                  Active (online)
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.875rem', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={form.include_in_workflow_runs}
                    onChange={(e) => setForm((f) => ({ ...f, include_in_workflow_runs: e.target.checked }))}
                  />
                  Use for workflow runs
                </label>
              </div>
              {!form.include_in_workflow_runs && (
                <p style={{ margin: 0, fontSize: '0.75rem', color: 'var(--text-secondary)', paddingLeft: '0.25rem' }}>
                  Workflow runner will skip this environment. It remains active for version/classifier sync and tracking.
                </p>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginTop: '1.25rem' }}>
              <button
                type="button"
                className="btn"
                style={{ background: 'transparent', border: '1px solid var(--surface-border)', color: 'var(--text-primary)' }}
                onClick={() => setModalOpen(false)}
                disabled={pending}
              >
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
