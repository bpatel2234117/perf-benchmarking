import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { ArrowDown, ArrowUp, ArrowUpDown, Loader2, Satellite } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import { ScanProgressDetail } from './ScanProgressPage';
import PaginationBar from '../components/Pagination';
import PageHeader from '../components/PageHeader';

/** Matches `/api/scan/unified` sort_by keys (`_SCAN_UNIFIED_SORT_SQL` in v2_api/main.py). */
export type UnifiedSort =
  | 'latest'
  | 'running_first'
  | 'started_desc'
  | 'started_asc'
  | 'completed_desc'
  | 'completed_asc'
  | 'scan_id_desc'
  | 'scan_id_asc'
  | 'environment'
  | 'environment_desc'
  | 'status'
  | 'status_desc'
  | 'scan_name_asc'
  | 'scan_name_desc'
  | 'scan_type_asc'
  | 'scan_type_desc'
  | 'template_asc'
  | 'template_desc'
  | 'snapshot_desc'
  | 'snapshot_asc'
  | 'state'
  | 'state_desc'
  | 'objects_desc'
  | 'objects_asc'
  | 'parts_desc'
  | 'parts_asc'
  | 'classify_desc'
  | 'classify_asc'
  | 'subscans_desc'
  | 'subscans_asc';

interface ScanUnifiedRow {
  scan_id: string;
  scan_name: string | null;
  scan_type: string | null;
  started_at: string | null;
  completed_at: string | null;
  template_name: string | null;
  scan_status: string | null;
  environment_id: number | null;
  environment_name: string | null;
  scan_state: string | null;
  completed_sub_scans: number | null;
  failed_sub_scans: number | null;
  total_sub_scans: number | null;
  total_parts_scanned: number | null;
  total_classifications: number | null;
  objects_scanned: number | null;
  objects_failed: number | null;
  objects_with_pi: number | null;
  last_snapshot_at: string | null;
}

interface ScanUnifiedResponse {
  items: ScanUnifiedRow[];
  total: number;
}

interface EnvironmentRow {
  id: number;
  env_name: string;
}

const PAGE_SIZE = 25;

type StatusFilter = 'all' | 'in_progress' | 'completed';

interface ScanStatusPageProps {
  token: string;
}

function fmtCell(v: string | null | undefined): string {
  if (v == null || v === '') return '—';
  const t = String(v).trim();
  return t || '—';
}

/** Strip a leading build prefix (e.g. "246-2 ", "247-12 ") from a scan name for display only.
 *  Mirrors the backend _strip_leading_build_prefix regex. The stored scan_name and all
 *  benchmark/chart data are untouched — this only changes the label in the picker. */
function stripBuildPrefix(v: string | null | undefined): string {
  const t = String(v ?? '').trim();
  const m = t.match(/^2\d{2}[-.]\d{1,3}\s+(.+)$/);
  return m ? m[1].trim() : t;
}

/** Same set as TERMINAL_STATES in v2_api/scan_bigid_sync.py, compacted (lowercase, no spaces). */
const TERMINAL_STATUS_KEYS = new Set(
  ['Failed', 'CompletedWithFailures', 'Completed', 'Stopped', 'Aborted', 'Cancelled', 'Canceled'].map((s) =>
    s.toLowerCase()
  )
);

function compactStatus(v: string | null | undefined): string {
  return String(v ?? '').trim().toLowerCase().replace(/ /g, '');
}

function fmtNumCell(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  if (typeof v === 'number' && Number.isNaN(v)) return '—';
  return String(v);
}

function scanStatusBadge(status: string | null | undefined): React.ReactElement {
  const s = (status ?? '').trim();
  if (!s) return <span style={{ color: 'var(--text-secondary)' }}>—</span>;
  const low = s.toLowerCase();
  let bg = 'var(--surface-hover)';
  let color = 'var(--text-secondary)';
  if (['inprogress', 'running', 'scan_running', 'active'].includes(low)) {
    bg = '#dcfce7'; color = '#166534';
  } else if (['completed', 'done', 'success'].includes(low)) {
    bg = '#dbeafe'; color = '#1e3a8a';
  } else if (['failed', 'error'].includes(low)) {
    bg = '#fee2e2'; color = '#991b1b';
  } else if (['stopped', 'stoprequested', 'stop_requested', 'cancelled', 'canceled'].includes(low)) {
    bg = '#ffedd5'; color = '#9a3412';
  } else if (['pending', 'queued'].includes(low)) {
    bg = '#f3f4f6'; color = '#374151';
  }
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: '0.75rem',
      fontWeight: 600,
      background: bg,
      color,
      whiteSpace: 'nowrap',
    }}>
      {s}
    </span>
  );
}

type UnifiedSortCol =
  | 'environment'
  | 'scan_id'
  | 'name'
  | 'type'
  | 'status'
  | 'started'
  | 'completed'
  | 'template'
  | 'state'
  | 'objects'
  | 'subscans'
  | 'parts'
  | 'classify'
  | 'snapshot';

const UNIFIED_COL_SORT: Record<UnifiedSortCol, { asc: UnifiedSort; desc: UnifiedSort }> = {
  environment: { asc: 'environment', desc: 'environment_desc' },
  scan_id: { asc: 'scan_id_asc', desc: 'scan_id_desc' },
  name: { asc: 'scan_name_asc', desc: 'scan_name_desc' },
  type: { asc: 'scan_type_asc', desc: 'scan_type_desc' },
  status: { asc: 'status', desc: 'status_desc' },
  started: { asc: 'started_asc', desc: 'started_desc' },
  completed: { asc: 'completed_asc', desc: 'completed_desc' },
  template: { asc: 'template_asc', desc: 'template_desc' },
  state: { asc: 'state', desc: 'state_desc' },
  objects: { asc: 'objects_asc', desc: 'objects_desc' },
  subscans: { asc: 'subscans_asc', desc: 'subscans_desc' },
  parts: { asc: 'parts_asc', desc: 'parts_desc' },
  classify: { asc: 'classify_asc', desc: 'classify_desc' },
  snapshot: { asc: 'snapshot_asc', desc: 'snapshot_desc' },
};

function toggleUnifiedSort(current: UnifiedSort, col: UnifiedSortCol): UnifiedSort {
  const pair = UNIFIED_COL_SORT[col];
  if (current === pair.asc) return pair.desc;
  if (current === pair.desc) return pair.asc;
  return pair.asc;
}

function unifiedSortArrow(sort: UnifiedSort, col: UnifiedSortCol): 'asc' | 'desc' | null {
  const pair = UNIFIED_COL_SORT[col];
  if (sort === pair.asc) return 'asc';
  if (sort === pair.desc) return 'desc';
  return null;
}

const thBtn: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  color: 'inherit',
  cursor: 'pointer',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  fontFamily: 'inherit',
  fontWeight: 600,
  fontSize: 'inherit',
  padding: 0,
  textAlign: 'inherit',
  width: '100%',
};

function SortGlyph({ dir }: { dir: 'asc' | 'desc' | null }) {
  if (dir === 'asc') return <ArrowUp size={14} aria-hidden />;
  if (dir === 'desc') return <ArrowDown size={14} aria-hidden />;
  return <ArrowUpDown size={14} style={{ opacity: 0.55 }} aria-hidden />;
}

const ScanStatusPage: React.FC<ScanStatusPageProps> = ({ token }) => {
  const [searchParams] = useSearchParams();
  const scanId = searchParams.get('scan_id')?.trim() ?? '';

  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();

  const syncMutation = useMutation({
    mutationFn: async (scanId: string) => {
      const res = await axios.post<{ ok: boolean; detail?: string }>(
        `${API_BASE}/api/scan/sync-insights`,
        { scan_id: scanId },
        authConfig
      );
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanUnified'] });
    },
  });

  const markStoppedMutation = useMutation({
    mutationFn: async (scanId: string) => {
      const res = await axios.post<{ ok: boolean; changed?: boolean }>(
        `${API_BASE}/api/scan/mark-stopped`,
        { scan_id: scanId },
        authConfig
      );
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanUnified'] });
    },
  });

  const resyncParentsMutation = useMutation({
    mutationFn: async (environmentId: number) => {
      const res = await axios.post<{ ok: boolean; upserted?: number }>(
        `${API_BASE}/api/scan/resync-parents`,
        { environment_id: environmentId, limit: 200 },
        authConfig
      );
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanUnified'] });
    },
  });

  const { data: environments = [], isLoading: envLoading } = useQuery({
    queryKey: ['environments', token],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/environments`, authConfig);
      return res.data as EnvironmentRow[];
    },
  });

  const [envChoice, setEnvChoice] = useState<string>('');
  const [status, setStatus] = useState<StatusFilter>('all');
  const [sortUnified, setSortUnified] = useState<UnifiedSort>('latest');
  const [page, setPage] = useState(0);

  useEffect(() => {
    setPage(0);
  }, [envChoice, status, sortUnified]);

  const { data: unifiedData, isLoading: unifiedLoading, error: unifiedError } = useQuery({
    queryKey: ['scanUnified', token, envChoice, status, sortUnified, page],
    queryFn: async () => {
      const params: Record<string, string | number> = {
        status,
        sort_by: sortUnified,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      };
      const eid = envChoice.trim();
      if (eid && /^\d+$/.test(eid)) params.environment_id = parseInt(eid, 10);
      const res = await axios.get(`${API_BASE}/api/scan/unified`, { ...authConfig, params });
      return res.data as ScanUnifiedResponse;
    },
  });

  const items = unifiedData?.items ?? [];
  const total = unifiedData?.total ?? 0;

  const selectStyle: React.CSSProperties = {
    padding: '0.5rem 0.75rem',
    borderRadius: 6,
    border: '1px solid var(--surface-border)',
    fontFamily: 'inherit',
    minWidth: 200,
    maxWidth: '100%',
  };

  const actionLink: React.CSSProperties = {
    color: 'var(--primary-color)',
    fontWeight: 600,
    fontSize: '0.78rem',
    textDecoration: 'none',
  };

  const actionCellStyle: React.CSSProperties = {
    padding: '9px 10px',
    whiteSpace: 'nowrap',
    fontSize: '0.76rem',
    verticalAlign: 'middle',
    minWidth: 150,
    position: 'sticky',
    left: 0,
    zIndex: 2,
    background: 'var(--surface-ground, #f8fafc)',
    boxShadow: '4px 0 8px -2px rgba(0,0,0,0.08)',
  };

  const actionHeaderStyle: React.CSSProperties = {
    padding: '9px 10px',
    textAlign: 'left',
    fontWeight: 700,
    position: 'sticky',
    left: 0,
    zIndex: 3,
    background: 'var(--primary-color, #0052cc)',
    boxShadow: '4px 0 8px -2px rgba(0,0,0,0.15)',
  };

  function renderScanActions(scanId: string, scanStatus: string | null) {
    const benchPath = `/tools/add-benchmark?scan_id=${encodeURIComponent(scanId)}`;
    const progressDetailPath = `/tools/scan-status?scan_id=${encodeURIComponent(scanId)}`;
    const busy = syncMutation.isPending && syncMutation.variables === scanId;
    const stopBusy = markStoppedMutation.isPending && markStoppedMutation.variables === scanId;
    const canMarkStopped = !TERMINAL_STATUS_KEYS.has(compactStatus(scanStatus));
    return (
      <td style={actionCellStyle}>
        <div style={{ display: 'flex', flexDirection: 'row', flexWrap: 'wrap', gap: '10px', alignItems: 'center' }}>
          <Link to={benchPath} style={actionLink}>
            Open
          </Link>
          <Link to={progressDetailPath} style={actionLink}>
            View
          </Link>
          <button
            type="button"
            disabled={busy}
            title="Fetch parent-scan insights from BigID into Postgres scan_insights for this scan."
            onClick={() => syncMutation.mutate(scanId)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--primary-color)',
              cursor: busy ? 'wait' : 'pointer',
              fontFamily: 'inherit',
              fontWeight: 600,
              fontSize: '0.76rem',
              padding: 0,
              textDecoration: 'underline',
            }}
          >
            {busy ? '…' : 'Sync'}
          </button>
          {canMarkStopped && (
            <button
              type="button"
              disabled={stopBusy}
              title="Mark this scan as stopped. The tracker stops polling it; it does not stop the scan in BigID."
              onClick={() => markStoppedMutation.mutate(scanId)}
              style={{
                background: 'transparent',
                border: 'none',
                color: '#9a3412',
                cursor: stopBusy ? 'wait' : 'pointer',
                fontFamily: 'inherit',
                fontWeight: 600,
                fontSize: '0.76rem',
                padding: 0,
                textDecoration: 'underline',
              }}
            >
              {stopBusy ? '…' : 'Mark stopped'}
            </button>
          )}
        </div>
      </td>
    );
  }

  if (scanId) {
    return <ScanProgressDetail scanId={scanId} token={token} />;
  }

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
      <section>
        <PageHeader icon={Satellite} title="Scan Status" />

        <div
          className="glass-panel"
          style={{
            padding: '1rem 1.2rem',
            marginBottom: '1rem',
            display: 'flex',
            flexWrap: 'wrap',
            gap: '1rem',
            alignItems: 'end',
          }}
        >
          <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
            <label htmlFor="scan-status-env">Environment</label>
            <select
              id="scan-status-env"
              value={envChoice}
              onChange={(e) => setEnvChoice(e.target.value)}
              disabled={envLoading}
              style={selectStyle}
            >
              <option value="">All environments</option>
              {environments.map((e) => (
                <option key={e.id} value={String(e.id)}>
                  {e.env_name}
                </option>
              ))}
            </select>
          </div>
          <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
            <label htmlFor="scan-status-filter">Parent scan status</label>
            <select
              id="scan-status-filter"
              value={status}
              onChange={(e) => setStatus(e.target.value as StatusFilter)}
              style={selectStyle}
            >
              <option value="all">All statuses (incl. stopped/aborted)</option>
              <option value="in_progress">In progress</option>
              <option value="completed">Completed / failed</option>
            </select>
          </div>
          <div className="input-group" style={{ margin: 0, minWidth: 220 }}>
            <label htmlFor="scan-status-resync">Manual list re-sync</label>
            <button
              id="scan-status-resync"
              type="button"
              disabled={!envChoice || resyncParentsMutation.isPending}
              onClick={() => {
                const eid = parseInt(envChoice, 10);
                if (!Number.isNaN(eid)) resyncParentsMutation.mutate(eid);
              }}
              style={{
                padding: '0.5rem 0.85rem',
                borderRadius: 6,
                border: '1px solid var(--surface-border)',
                background: 'var(--surface-ground)',
                fontFamily: 'inherit',
                fontWeight: 600,
                cursor: !envChoice || resyncParentsMutation.isPending ? 'not-allowed' : 'pointer',
                opacity: !envChoice ? 0.55 : 1,
              }}
              title="Pull running + completed parent scans from BigID into Postgres (pick one environment)."
            >
              {resyncParentsMutation.isPending ? 'Re-syncing…' : 'Re-sync list (BigID API)'}
            </button>
          </div>
          <div className="input-group" style={{ margin: 0, minWidth: 120 }}>
            <label htmlFor="scan-status-refresh">Reload list</label>
            <button
              id="scan-status-refresh"
              type="button"
              disabled={unifiedLoading}
              onClick={() => queryClient.invalidateQueries({ queryKey: ['scanUnified'] })}
              style={{
                padding: '0.5rem 0.85rem',
                borderRadius: 6,
                border: '1px solid var(--surface-border)',
                background: 'var(--surface-ground)',
                fontFamily: 'inherit',
                fontWeight: 600,
                cursor: unifiedLoading ? 'wait' : 'pointer',
              }}
              title="Fetch the latest rows from Postgres. The list does not refresh on a timer."
            >
              Refresh
            </button>
          </div>
        </div>

        {unifiedError && (
          <div className="glass-panel" style={{ padding: '1rem', color: 'var(--danger-color)', marginBottom: '1rem' }}>
            {(unifiedError as Error)?.message || 'Could not load scan data.'}
          </div>
        )}

        {(syncMutation.isError || resyncParentsMutation.isError) && (
          <div className="glass-panel" style={{ padding: '0.85rem 1rem', color: 'var(--danger-color)', marginBottom: '1rem', fontSize: '0.88rem', lineHeight: 1.45 }}>
            {syncMutation.isError && (
              <div>
                Sync failed:{' '}
                {axios.isAxiosError(syncMutation.error)
                  ? String((syncMutation.error.response?.data as { detail?: string } | undefined)?.detail ?? syncMutation.error.message)
                  : String(syncMutation.error)}
              </div>
            )}
            {resyncParentsMutation.isError && (
              <div>
                List re-sync failed:{' '}
                {axios.isAxiosError(resyncParentsMutation.error)
                  ? String(
                    (resyncParentsMutation.error.response?.data as { detail?: string } | undefined)?.detail ??
                    resyncParentsMutation.error.message
                  )
                  : String(resyncParentsMutation.error)}
              </div>
            )}
          </div>
        )}

        <div className="glass-panel" style={{ padding: '0.75rem 1rem 1rem' }}>
          {unifiedLoading ? (
            <div style={{ padding: '2rem', textAlign: 'center' }}>
              <Loader2 className="lucide-spin" size={28} />
            </div>
          ) : (
            <>
              <div style={{ overflowX: 'auto', overflowY: 'auto', maxHeight: 'calc(100vh - 260px)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
                  <thead>
                    <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                      <th style={actionHeaderStyle}>Actions</th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button
                          type="button"
                          style={thBtn}
                          onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'environment'))}
                        >
                          Environment
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'environment')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'scan_id'))}>
                          Scan ID
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'scan_id')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'name'))}>
                          Name
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'name')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'type'))}>
                          Type
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'type')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'status'))}>
                          Parent status
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'status')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'started'))}>
                          Started
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'started')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'completed'))}>
                          Completed
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'completed')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'template'))}>
                          Template
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'template')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'right' }}>
                        <button
                          type="button"
                          style={{ ...thBtn, justifyContent: 'flex-end' }}
                          onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'objects'))}
                        >
                          Objects
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'objects')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'right' }}>
                        <button
                          type="button"
                          style={{ ...thBtn, justifyContent: 'flex-end' }}
                          onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'parts'))}
                        >
                          Parts
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'parts')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'right' }}>
                        <button
                          type="button"
                          style={{ ...thBtn, justifyContent: 'flex-end' }}
                          onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'classify'))}
                        >
                          Classify
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'classify')} />
                        </button>
                      </th>
                      <th style={{ padding: '9px 10px', textAlign: 'left' }}>
                        <button type="button" style={thBtn} onClick={() => setSortUnified((s) => toggleUnifiedSort(s, 'snapshot'))}>
                          Last snapshot
                          <SortGlyph dir={unifiedSortArrow(sortUnified, 'snapshot')} />
                        </button>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((row) => (
                      <tr key={row.scan_id} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                        {renderScanActions(row.scan_id, row.scan_status)}
                        <td style={{ padding: '8px 10px', whiteSpace: 'nowrap' }}>{fmtCell(row.environment_name)}</td>
                        <td style={{ padding: '8px 10px', fontFamily: 'ui-monospace, monospace', fontSize: '0.76rem' }}>
                          {fmtCell(row.scan_id)}
                        </td>
                        <td style={{ padding: '8px 10px', maxWidth: 220 }} title={fmtCell(row.scan_name)}>{fmtCell(stripBuildPrefix(row.scan_name))}</td>
                        <td style={{ padding: '8px 10px' }}>{fmtCell(row.scan_type)}</td>
                        <td style={{ padding: '8px 10px' }}>{scanStatusBadge(row.scan_status)}</td>
                        <td style={{ padding: '8px 10px', whiteSpace: 'nowrap' }}>{fmtCell(row.started_at)}</td>
                        <td style={{ padding: '8px 10px', whiteSpace: 'nowrap' }}>{fmtCell(row.completed_at)}</td>
                        <td style={{ padding: '8px 10px' }}>{fmtCell(row.template_name)}</td>
                        <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmtNumCell(row.objects_scanned)}</td>
                        <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmtNumCell(row.total_parts_scanned)}</td>
                        <td style={{ padding: '8px 10px', textAlign: 'right' }}>{fmtNumCell(row.total_classifications)}</td>
                        <td style={{ padding: '8px 10px', whiteSpace: 'nowrap' }}>{fmtNumCell(row.last_snapshot_at)}</td>
                      </tr>
                    ))}
                    {items.length === 0 && (
                      <tr>
                        <td colSpan={12} style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                          No rows match this filter, or <code>scan_parents</code> is empty / not synced yet. Tracker snapshots appear under{' '}
                          <strong style={{ fontWeight: 600 }}>Tracker state</strong> when <code style={{ fontSize: '0.85em' }}>scan_progress</code>{' '}
                          has data.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              {total > 0 && (
                <div style={{ padding: '0.65rem 0 0' }}>
                  <PaginationBar
                    page={page}
                    pageSize={PAGE_SIZE}
                    totalItems={total}
                    onPageChange={setPage}
                    ariaLabel="Scan status pagination"
                  />
                </div>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
};

export default ScanStatusPage;
