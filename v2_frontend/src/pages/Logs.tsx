import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, Terminal, AlertTriangle, Search, X, Download, Trash2 } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import PaginationBar from '../components/Pagination';
import { useUserRole } from '../hooks/useUserRole';

const LOG_RETENTION_DAYS = 15;

// Quick presets for the cleanup control. 0 = wipe everything (new sprint / fresh build run).
const PURGE_DAY_PRESETS: { label: string; days: number }[] = [
  { label: 'All logs', days: 0 },
  { label: '> 7 days', days: 7 },
  { label: '> 15 days', days: 15 },
  { label: '> 30 days', days: 30 },
  { label: '> 60 days', days: 60 },
];

interface LogsProps {
  token: string;
}

const LOG_PAGE_SIZE = 25;

/** RFC-4180 cell escaping for CSV export. */
function csvCell(v: string | number | null | undefined): string {
  const s = v == null ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

interface LogErrRow {
  id: number;
  environment_id: number;
  environment_name: string | null;
  log_timestamp: string | null;
  scan_id: string | null;
  error_context: string | null;
  ai_analysis: string | null;
  log_source: string | null;
  subscan_id: string | null;
  is_resolved: boolean;
  manual_triage: string | null;
}

/** Inline-editable manual triage cell. When set, the analyzer skips Ollama for this signature. */
const TriageCell: React.FC<{ row: LogErrRow; saving: boolean; onSave: (id: number, triage: string) => void }> = ({ row, saving, onSave }) => {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(row.manual_triage ?? '');
  useEffect(() => { setVal(row.manual_triage ?? ''); }, [row.manual_triage]);

  if (editing) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        <textarea
          value={val}
          onChange={(e) => setVal(e.target.value)}
          rows={3}
          autoFocus
          placeholder="Triage note (applies to all occurrences of this error; skips AI)"
          style={{ width: '100%', fontFamily: 'inherit', fontSize: '0.78rem', padding: '0.35rem 0.45rem', borderRadius: 6, border: '1px solid var(--surface-border)', resize: 'vertical' }}
        />
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            type="button"
            disabled={saving}
            onClick={() => onSave(row.id, val)}
            style={{ padding: '0.25rem 0.6rem', borderRadius: 6, border: 'none', background: 'var(--primary-color)', color: '#fff', fontSize: '0.74rem', fontWeight: 700, cursor: saving ? 'wait' : 'pointer' }}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => { setVal(row.manual_triage ?? ''); setEditing(false); }}
            style={{ padding: '0.25rem 0.6rem', borderRadius: 6, border: '1px solid var(--surface-border)', background: '#fff', fontSize: '0.74rem', fontWeight: 600, cursor: 'pointer', color: 'var(--text-secondary)' }}
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  if (row.manual_triage) {
    return (
      <div
        onClick={() => setEditing(true)}
        title="Click to edit triage"
        style={{ cursor: 'pointer', fontSize: '0.78rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word', padding: '2px 4px', borderLeft: '3px solid var(--accent-color, #10b981)', paddingLeft: 8 }}
      >
        {row.manual_triage}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      style={{ padding: '0.25rem 0.55rem', borderRadius: 6, border: '1px dashed var(--surface-border)', background: 'transparent', fontSize: '0.74rem', fontWeight: 600, cursor: 'pointer', color: 'var(--text-secondary)' }}
    >
      + Triage
    </button>
  );
};

interface UniqueErrRow {
  group_key: string;
  occurrences: number;
  first_seen: string | null;
  last_seen: string | null;
  scan_count: number;
  env_count: number;
  error_context: string | null;
  log_source: string | null;
  environment_name: string | null;
  scan_id: string | null;
}

const WINDOW_OPTIONS: { label: string; hours: number }[] = [
  { label: 'Last 1h', hours: 1 },
  { label: 'Last 24h', hours: 24 },
  { label: 'Last 7d', hours: 168 },
  { label: 'Last 30d', hours: 720 },
];

const Logs: React.FC<LogsProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();
  const isAdmin = useUserRole(token) === 'Admin';
  const [page, setPage] = useState(0);
  const [source, setSource] = useState('');
  const [envId, setEnvId] = useState('');
  const [scanIdInput, setScanIdInput] = useState('');
  const [scanId, setScanId] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [purgeDays, setPurgeDays] = useState(LOG_RETENTION_DAYS);
  const [keepTriaged, setKeepTriaged] = useState(false);
  const [groupErrors, setGroupErrors] = useState(false);
  const [windowHours, setWindowHours] = useState(24);

  // Debounce the free-text search so we don't refetch on every keystroke. Only send
  // queries of >= 2 chars (or empty to clear) — a 1-char term can't use the trigram
  // index and would force a full scan.
  useEffect(() => {
    const t = setTimeout(() => {
      const q = searchInput.trim();
      setSearch(q.length >= 2 ? q : '');
    }, 350);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Debounce the Scan ID exact-match filter (typically pasted).
  useEffect(() => {
    const t = setTimeout(() => setScanId(scanIdInput.trim()), 350);
    return () => clearTimeout(t);
  }, [scanIdInput]);

  const anyFilter = Boolean(source || envId || scanId || search);

  // Raw rows — always runs (also supplies the Source/Environment dropdown option lists).
  const { data, isLoading } = useQuery({
    queryKey: ['logs', token, page, source, envId, scanId, search, groupErrors],
    queryFn: async () => {
      const res = await axios.get<{ items: LogErrRow[]; total: number; sources?: string[]; environments?: { id: number; name: string }[] }>(`${API_BASE}/api/logs/errors`, {
        ...authConfig,
        params: {
          limit: LOG_PAGE_SIZE,
          offset: groupErrors ? 0 : page * LOG_PAGE_SIZE,
          ...(source ? { source } : {}),
          ...(envId ? { environment_id: envId } : {}),
          ...(scanId ? { scan_id: scanId } : {}),
          ...(search ? { search } : {}),
        },
      });
      return res.data;
    },
    // Pause the 10s auto-refresh while a free-text/scan-id search or grouping is active
    // so a heavier query can't stack up on the poll interval.
    refetchInterval: search || scanId || groupErrors ? false : 10000,
  });

  // Grouped/unique rows — only when "Group identical errors" is on.
  const { data: uniqueData, isLoading: uniqueLoading } = useQuery({
    enabled: groupErrors,
    queryKey: ['logs-unique', token, page, source, envId, scanId, search, windowHours],
    queryFn: async () => {
      const res = await axios.get<{ items: UniqueErrRow[]; total: number }>(`${API_BASE}/api/logs/errors/unique`, {
        ...authConfig,
        params: {
          window_hours: windowHours,
          limit: LOG_PAGE_SIZE,
          offset: page * LOG_PAGE_SIZE,
          ...(source ? { source } : {}),
          ...(envId ? { environment_id: envId } : {}),
          ...(scanId ? { scan_id: scanId } : {}),
          ...(search ? { search } : {}),
        },
      });
      return res.data;
    },
    refetchInterval: search || scanId ? false : 15000,
  });

  const logs = data?.items ?? [];
  const sources = data?.sources ?? [];
  const environments = data?.environments ?? [];
  const uniqueRows = uniqueData?.items ?? [];
  const total = groupErrors ? (uniqueData?.total ?? 0) : (data?.total ?? 0);
  const loading = groupErrors ? uniqueLoading : isLoading;

  useEffect(() => {
    setPage(0);
  }, [token]);

  // Reset to first page whenever a filter, mode, or window changes.
  useEffect(() => {
    setPage(0);
  }, [source, envId, scanId, search, groupErrors, windowHours]);

  const clearFilters = () => { setSource(''); setEnvId(''); setScanIdInput(''); setSearchInput(''); };

  // Export the loaded page (up to 25 rows) to CSV — excludes the AI analysis column.
  const downloadCsv = () => {
    let headers: string[];
    let dataLines: string[];
    if (groupErrors) {
      if (!uniqueRows.length) return;
      headers = ['Occurrences', 'Error', 'Source', 'Environment', 'Scans', 'Envs', 'First seen', 'Last seen'];
      dataLines = uniqueRows.map((g) => [
        g.occurrences, g.error_context ?? '', g.log_source ?? '', g.environment_name ?? '',
        g.scan_count, g.env_count, g.first_seen ?? '', g.last_seen ?? '',
      ].map(csvCell).join(','));
    } else {
      if (!logs.length) return;
      headers = ['Time', 'Environment', 'Scan ID', 'Source', 'Error'];
      dataLines = logs.map((log) => [
        log.log_timestamp ?? '',
        log.environment_name ?? log.environment_id ?? '',
        log.scan_id ?? '',
        log.log_source ?? '',
        log.error_context ?? '',
      ].map(csvCell).join(','));
    }
    const lines = [headers.join(','), ...dataLines];
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `logs-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // Save manual triage for a log error (applies to the whole error signature).
  const triageMut = useMutation({
    mutationFn: async ({ id, triage }: { id: number; triage: string }) => {
      const res = await axios.post(`${API_BASE}/api/logs/errors/${id}/triage`, { triage, apply_to_signature: true }, authConfig);
      return res.data;
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['logs'] }); },
  });

  // Admin-only: purge log rows older than the chosen window (0 = wipe all logs).
  const purge = useMutation({
    mutationFn: async () => {
      const res = await axios.delete<{ total_deleted: number; deleted: Record<string, number> }>(
        `${API_BASE}/api/admin/logs/purge`, { ...authConfig, params: { days: purgeDays, keep_triaged: keepTriaged } });
      return res.data;
    },
    onSuccess: () => {
      setConfirmPurge(false);
      setPage(0);
      queryClient.invalidateQueries({ queryKey: ['logs'] });
    },
  });

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      <section>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
          <Terminal size={24} color="var(--primary-color)" />
          <h2 style={{ color: 'var(--text-primary)' }}>AI Log Analysis</h2>
        </div>
        <p style={{ color: 'var(--text-secondary)', marginBottom: '1rem' }}>
          Real-time AI diagnostic errors caught during active scans.
        </p>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center', marginBottom: '1rem' }}>
          <div style={{ position: 'relative', flex: '1 1 280px', maxWidth: 420 }}>
            <Search
              size={15}
              style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-secondary)' }}
              aria-hidden
            />
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search error, AI analysis, scan id, environment…"
              aria-label="Search logs"
              style={{
                width: '100%',
                padding: '0.5rem 2rem 0.5rem 2rem',
                borderRadius: 8,
                border: '1px solid var(--surface-border)',
                fontFamily: 'inherit',
                fontSize: '0.86rem',
                color: 'var(--text-primary)',
              }}
            />
            {searchInput && (
              <button
                type="button"
                onClick={() => setSearchInput('')}
                aria-label="Clear search"
                style={{
                  position: 'absolute', right: 6, top: '50%', transform: 'translateY(-50%)',
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--text-secondary)', padding: 4,
                }}
              >
                <X size={14} />
              </button>
            )}
          </div>
          <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 8 }}>
            Source
            <select
              value={source}
              onChange={(e) => setSource(e.target.value)}
              style={{ padding: '0.45rem 0.6rem', borderRadius: 8, border: '1px solid var(--surface-border)', fontFamily: 'inherit', fontSize: '0.86rem', background: '#fff', color: 'var(--text-primary)' }}
            >
              <option value="">All sources</option>
              {sources.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 8 }}>
            Environment
            <select
              value={envId}
              onChange={(e) => setEnvId(e.target.value)}
              style={{ padding: '0.45rem 0.6rem', borderRadius: 8, border: '1px solid var(--surface-border)', fontFamily: 'inherit', fontSize: '0.86rem', background: '#fff', color: 'var(--text-primary)' }}
            >
              <option value="">All environments</option>
              {environments.map((e) => (
                <option key={e.id} value={e.id}>{e.name}</option>
              ))}
            </select>
          </label>
          <div style={{ position: 'relative', flex: '0 1 220px' }}>
            <input
              type="text"
              value={scanIdInput}
              onChange={(e) => setScanIdInput(e.target.value)}
              placeholder="Scan ID (exact)"
              aria-label="Filter by scan id"
              style={{
                width: '100%', padding: '0.5rem 1.9rem 0.5rem 0.6rem', borderRadius: 8,
                border: '1px solid var(--surface-border)', fontFamily: 'ui-monospace, monospace',
                fontSize: '0.82rem', color: 'var(--text-primary)',
              }}
            />
            {scanIdInput && (
              <button
                type="button"
                onClick={() => setScanIdInput('')}
                aria-label="Clear scan id filter"
                style={{ position: 'absolute', right: 6, top: '50%', transform: 'translateY(-50%)', display: 'inline-flex', border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--text-secondary)', padding: 4 }}
              >
                <X size={14} />
              </button>
            )}
          </div>
          {anyFilter && (
            <button
              type="button"
              onClick={clearFilters}
              style={{ padding: '0.45rem 0.8rem', borderRadius: 8, border: '1px solid var(--surface-border)', background: '#fff', cursor: 'pointer', fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-secondary)' }}
            >
              Clear filters
            </button>
          )}
          <label
            title="Collapse repeated identical errors into one row with an occurrence count, within the time window"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.82rem', fontWeight: 600, color: groupErrors ? 'var(--primary-color)' : 'var(--text-secondary)', cursor: 'pointer' }}
          >
            <input type="checkbox" checked={groupErrors} onChange={(e) => setGroupErrors(e.target.checked)} />
            Group identical errors
          </label>
          {groupErrors && (
            <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 8 }}>
              Window
              <select
                value={windowHours}
                onChange={(e) => setWindowHours(Number(e.target.value))}
                style={{ padding: '0.45rem 0.6rem', borderRadius: 8, border: '1px solid var(--surface-border)', fontFamily: 'inherit', fontSize: '0.86rem', background: '#fff', color: 'var(--text-primary)' }}
              >
                {WINDOW_OPTIONS.map((w) => (
                  <option key={w.hours} value={w.hours}>{w.label}</option>
                ))}
              </select>
            </label>
          )}
          <span style={{ marginLeft: 'auto', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
            {total.toLocaleString()} {groupErrors ? (total === 1 ? 'unique error' : 'unique errors') : (total === 1 ? 'entry' : 'entries')}
          </span>
          {(() => {
            const hasRows = groupErrors ? uniqueRows.length > 0 : logs.length > 0;
            return (
              <button
                type="button"
                onClick={downloadCsv}
                disabled={!hasRows}
                title={groupErrors ? 'Download the unique errors (up to 25) as CSV' : 'Download the shown rows (up to 25) as CSV — excludes AI analysis'}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '0.45rem 0.8rem', borderRadius: 8,
                  border: '1px solid var(--surface-border)', background: '#fff',
                  cursor: hasRows ? 'pointer' : 'not-allowed', opacity: hasRows ? 1 : 0.55,
                  fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-primary)',
                }}
              >
                <Download size={14} /> Download CSV
              </button>
            );
          })()}
          {isAdmin && (
            confirmPurge ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: '0.8rem', color: 'var(--danger-color)', fontWeight: 600 }}>
                  {purgeDays <= 0
                    ? 'Delete ALL logs? This cannot be undone.'
                    : `Delete logs older than ${purgeDays} day${purgeDays === 1 ? '' : 's'}?`}
                  {keepTriaged ? ' (triaged errors kept)' : ''}
                </span>
                <button
                  type="button"
                  onClick={() => purge.mutate()}
                  disabled={purge.isPending}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 6, padding: '0.45rem 0.8rem', borderRadius: 8,
                    border: 'none', background: 'var(--danger-color, #ef4444)', color: '#fff', fontSize: '0.82rem',
                    fontWeight: 700, cursor: purge.isPending ? 'wait' : 'pointer',
                  }}
                >
                  {purge.isPending ? <Loader2 size={14} className="lucide-spin" /> : <Trash2 size={14} />} Confirm delete
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmPurge(false)}
                  disabled={purge.isPending}
                  style={{ padding: '0.45rem 0.7rem', borderRadius: 8, border: '1px solid var(--surface-border)', background: '#fff', cursor: 'pointer', fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-secondary)' }}
                >
                  Cancel
                </button>
              </span>
            ) : (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  Cleanup
                  <select
                    value={PURGE_DAY_PRESETS.some((p) => p.days === purgeDays) ? String(purgeDays) : 'custom'}
                    onChange={(e) => { if (e.target.value !== 'custom') setPurgeDays(Number(e.target.value)); }}
                    style={{ padding: '0.45rem 0.6rem', borderRadius: 8, border: '1px solid var(--surface-border)', fontFamily: 'inherit', fontSize: '0.86rem', background: '#fff', color: 'var(--text-primary)' }}
                  >
                    {PURGE_DAY_PRESETS.map((p) => (
                      <option key={p.days} value={p.days}>{p.label}</option>
                    ))}
                    {!PURGE_DAY_PRESETS.some((p) => p.days === purgeDays) && (
                      <option value="custom">{`> ${purgeDays} days`}</option>
                    )}
                  </select>
                </label>
                <input
                  type="number"
                  min={0}
                  max={3650}
                  value={purgeDays}
                  onChange={(e) => {
                    const n = Math.max(0, Math.min(3650, Math.floor(Number(e.target.value) || 0)));
                    setPurgeDays(n);
                  }}
                  aria-label="Delete logs older than this many days (0 = all)"
                  title="Number of days to keep — logs older than this are deleted (0 = delete all)"
                  style={{ width: 64, padding: '0.45rem 0.5rem', borderRadius: 8, border: '1px solid var(--surface-border)', fontFamily: 'inherit', fontSize: '0.84rem', color: 'var(--text-primary)' }}
                />
                <label
                  title="Keep manually-triaged errors so they aren't deleted and won't need re-triaging"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', fontWeight: 600, color: keepTriaged ? 'var(--primary-color)' : 'var(--text-secondary)', cursor: 'pointer' }}
                >
                  <input type="checkbox" checked={keepTriaged} onChange={(e) => setKeepTriaged(e.target.checked)} />
                  Keep triaged
                </label>
                <button
                  type="button"
                  onClick={() => { purge.reset(); setConfirmPurge(true); }}
                  title={purgeDays <= 0
                    ? 'Admin only — permanently delete ALL logs (new sprint / fresh build reset)'
                    : `Admin only — permanently delete log entries older than ${purgeDays} days`}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 6, padding: '0.45rem 0.8rem', borderRadius: 8,
                    border: '1px solid var(--danger-color, #ef4444)', background: '#fff', color: 'var(--danger-color, #ef4444)',
                    cursor: 'pointer', fontSize: '0.82rem', fontWeight: 600,
                  }}
                >
                  <Trash2 size={14} /> {purgeDays <= 0 ? 'Delete ALL logs' : `Delete logs > ${purgeDays} days`}
                </button>
              </span>
            )
          )}
          {purge.isSuccess && (
            <span style={{ fontSize: '0.8rem', color: 'var(--accent-color, #10b981)', fontWeight: 600 }}>
              Deleted {purge.data.total_deleted.toLocaleString()} log{purge.data.total_deleted === 1 ? '' : 's'}.
            </span>
          )}
          {purge.isError && (
            <span style={{ fontSize: '0.8rem', color: 'var(--danger-color)', fontWeight: 600 }}>
              {axios.isAxiosError(purge.error) && purge.error.response?.status === 403
                ? 'Admin privileges required.'
                : 'Delete failed.'}
            </span>
          )}
        </div>

        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {loading ? (
            <div style={{ padding: '3rem', textAlign: 'center' }}>
              <Loader2 className="lucide-spin" size={32} />
            </div>
          ) : (
            <>
              {groupErrors ? (
                <div style={{ overflowX: 'auto' }}>
                  <table className="logs-errors-table" style={{ tableLayout: 'fixed', width: '100%', minWidth: 960 }}>
                    <colgroup>
                      <col style={{ width: 104 }} />
                      <col />
                      <col style={{ width: 88 }} />
                      <col style={{ width: 150 }} />
                      <col style={{ width: 72 }} />
                      <col style={{ width: 150 }} />
                      <col style={{ width: 150 }} />
                    </colgroup>
                    <thead>
                      <tr>
                        {['Occurrences', 'Error', 'Source', 'Environment', 'Scans', 'First seen', 'Last seen'].map((h) => (
                          <th
                            key={h}
                            style={{
                              padding: '0.55rem 0.45rem',
                              textAlign: h === 'Occurrences' || h === 'Scans' ? 'right' : 'left',
                              fontSize: '0.76rem', fontWeight: 600, color: 'var(--text-secondary)',
                              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                              borderBottom: '1px solid var(--surface-border)',
                            }}
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {uniqueRows.map((g) => (
                        <tr key={g.group_key}>
                          <td style={{ textAlign: 'right', padding: '0.55rem 0.45rem', verticalAlign: 'top' }}>
                            <span style={{ display: 'inline-block', minWidth: 30, padding: '2px 8px', borderRadius: 999, background: 'rgba(239,68,68,0.12)', color: 'var(--danger-color)', fontWeight: 700, fontSize: '0.8rem' }}>
                              {g.occurrences.toLocaleString()}
                            </span>
                          </td>
                          <td style={{ padding: '0.55rem 0.65rem', verticalAlign: 'top' }}>
                            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.4rem', minWidth: 0 }}>
                              <AlertTriangle size={14} color="var(--danger-color)" style={{ flexShrink: 0, marginTop: '3px' }} />
                              <pre style={{ margin: 0, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '0.8rem', lineHeight: 1.45, whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text-primary)', fontWeight: 400 }}>
                                {g.error_context || '—'}
                              </pre>
                            </div>
                          </td>
                          <td style={{ fontWeight: 500, fontSize: '0.78rem', padding: '0.55rem 0.45rem', verticalAlign: 'top' }}>{g.log_source ?? '—'}</td>
                          <td
                            style={{ fontSize: '0.78rem', padding: '0.55rem 0.45rem', verticalAlign: 'top', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                            title={g.environment_name ?? ''}
                          >
                            {g.environment_name ?? '—'}{g.env_count > 1 ? ` +${g.env_count - 1}` : ''}
                          </td>
                          <td style={{ textAlign: 'right', fontSize: '0.78rem', padding: '0.55rem 0.45rem', verticalAlign: 'top' }}>{g.scan_count.toLocaleString()}</td>
                          <td style={{ fontSize: '0.74rem', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace', padding: '0.55rem 0.45rem', verticalAlign: 'top', whiteSpace: 'nowrap' }}>{g.first_seen ?? '—'}</td>
                          <td style={{ fontSize: '0.74rem', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace', padding: '0.55rem 0.45rem', verticalAlign: 'top', whiteSpace: 'nowrap' }}>{g.last_seen ?? '—'}</td>
                        </tr>
                      ))}
                      {uniqueRows.length === 0 && (
                        <tr>
                          <td colSpan={7} style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>
                            No errors in the selected window / filters.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div style={{ overflowX: 'auto' }}>
                  <table
                    className="logs-errors-table"
                    style={{ tableLayout: 'fixed', width: '100%', minWidth: 960 }}
                  >
                    <colgroup>
                      <col style={{ width: 132 }} />
                      <col style={{ width: 124 }} />
                      <col style={{ width: 116 }} />
                      <col style={{ width: 88 }} />
                      <col />
                      <col style={{ width: 240 }} />
                      <col style={{ width: 200 }} />
                    </colgroup>
                    <thead>
                      <tr>
                        {['Time', 'Environment', 'Scan ID', 'Source', 'Error', 'AI analysis', 'Triage'].map((h) => (
                          <th
                            key={h}
                            style={{
                              padding: '0.55rem 0.45rem',
                              textAlign: 'left',
                              fontSize: '0.76rem',
                              fontWeight: 600,
                              color: 'var(--text-secondary)',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              borderBottom: '1px solid var(--surface-border)',
                            }}
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {logs.map((log) => (
                        <tr key={log.id}>
                          <td
                            style={{
                              whiteSpace: 'nowrap',
                              fontSize: '0.78rem',
                              padding: '0.55rem 0.45rem',
                              verticalAlign: 'top',
                            }}
                          >
                            {log.log_timestamp ?? '—'}
                          </td>
                          <td
                            style={{
                              fontSize: '0.78rem',
                              padding: '0.55rem 0.45rem',
                              verticalAlign: 'top',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                            title={String(log.environment_name ?? log.environment_id ?? '')}
                          >
                            {log.environment_name ?? log.environment_id}
                          </td>
                          <td
                            style={{
                              fontSize: '0.72rem',
                              color: 'var(--text-secondary)',
                              fontFamily: 'ui-monospace, monospace',
                              padding: '0.55rem 0.45rem',
                              verticalAlign: 'top',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                            title={log.scan_id ?? undefined}
                          >
                            {log.scan_id ?? '—'}
                          </td>
                          <td
                            style={{
                              fontWeight: 500,
                              fontSize: '0.78rem',
                              padding: '0.55rem 0.45rem',
                              verticalAlign: 'top',
                            }}
                          >
                            {log.log_source ?? '—'}
                          </td>
                          <td style={{ padding: '0.55rem 0.65rem', verticalAlign: 'top' }}>
                            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.4rem', minWidth: 0 }}>
                              <AlertTriangle
                                size={14}
                                color="var(--danger-color)"
                                style={{ flexShrink: 0, marginTop: '3px' }}
                              />
                              <pre
                                style={{
                                  margin: 0,
                                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                                  fontSize: '0.8rem',
                                  lineHeight: 1.45,
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  color: 'var(--text-primary)',
                                  fontWeight: 400,
                                }}
                              >
                                {log.error_context || '—'}
                              </pre>
                            </div>
                          </td>
                          <td
                            style={{
                              fontSize: '0.8rem',
                              color: 'var(--text-secondary)',
                              padding: '0.55rem 0.65rem',
                              verticalAlign: 'top',
                              whiteSpace: 'pre-wrap',
                              wordBreak: 'break-word',
                            }}
                          >
                            {log.ai_analysis || '—'}
                          </td>
                          <td style={{ padding: '0.55rem 0.5rem', verticalAlign: 'top' }}>
                            <TriageCell
                              row={log}
                              saving={triageMut.isPending && triageMut.variables?.id === log.id}
                              onSave={(id, triage) => triageMut.mutate({ id, triage })}
                            />
                          </td>
                        </tr>
                      ))}
                      {logs.length === 0 && (
                        <tr>
                          <td colSpan={7} style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>
                            {anyFilter ? 'No logs match your filters.' : 'No recent scanner errors found.'}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
              {total > LOG_PAGE_SIZE && (
                <div style={{ padding: '0 1rem 1rem' }}>
                  <PaginationBar page={page} pageSize={LOG_PAGE_SIZE} totalItems={total} onPageChange={setPage} ariaLabel="Log error pages" />
                </div>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
};

export default Logs;
