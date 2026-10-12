import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import PaginationBar from '../components/Pagination';
import { Loader2, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  computeProgressEmoji,
  computeStepMetrics,
  envHasActiveWorkflowStep,
  findEnvCooldownUntil,
  flagsFromStep,
  isRunInCooldown,
  workflowIsTransientStepMessage,
  workflowNormStepStatus,
  workflowStepImpliesEnvBusy,
} from '../utils/workflowProgress';
import { API_BASE } from '../apiConfig';
import { useUserRole } from '../hooks/useUserRole';

interface WorkflowProps {
  token: string;
}

export interface WorkflowRun {
  id: number;
  name: string | null;
  status: string | null;
  env1_id: number | null;
  env2_id: number | null;
  /**
   * true when Env 1 / Env 2 were chosen at create: the run executes only on those envs and
   * reserves them. false/absent = no selection, so the run uses all available pool envs.
   */
  env_pinned?: boolean;
  env1_name?: string | null;
  env2_name?: string | null;
  cooldown_minutes: number | null;
  last_error: string | null;
  next_eligible_at: string | null;
  scan_template_id: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowStepRow {
  id: number;
  run_id: number;
  seq: number;
  environment_id: number | null;
  environment_name: string | null;
  env_is_busy: boolean;
  ds_kind: string | null;
  ds_name: string | null;
  scan_kind: string | null;
  file_set_type: string | null;
  is_enabled: boolean;
  build_version: string | null;
  build_prefix: string | null;
  scan_id: string | null;
  status: string | null;
  last_error: string | null;
  started_at: string | null;
  finished_at: string | null;
  // Canonical scan window from scan_parents (BigID-reported). Preferred for elapsed
  // time: started_at can be reset when a step is parked/re-picked; these are not.
  scan_started_at: string | null;
  scan_completed_at: string | null;
  scan_status: string | null;
  ds_id: string | null;
  scan_profile_id: string | null;
  scan_profile_name: string | null;
  is_acl: boolean;
  is_ocr: boolean;
  is_structured_ff: boolean;
  is_ols: boolean;
  is_lite: boolean;
  notes: string | null;
  feature_flags: string | null;
}

interface EnvChip {
  id: number;
  env_name: string;
  busy: boolean;
}

interface EnvironmentRow {
  id: number;
  env_name: string;
  base_url: string;
  is_active: boolean;
  is_busy: boolean;
  version?: string | null;
  include_in_workflow_runs?: boolean;
  is_default?: boolean;
  auth_module_path?: string | null;
  auth_function_name?: string | null;
}

interface StepCatalogRow {
  seq: number;
  ds_kind: string;
  ds_name: string | null;
  scan_kind: string | null;
  file_set_type: string | null;
  is_enabled: boolean;
  is_acl: boolean;
  is_ols: boolean;
  is_lite: boolean;
  is_structured_ff: boolean;
  feature_flags: string | null;
  notes: string | null;
}

/** Planner catalog filters for one-click enable/disable row groups. */
type QuickPreset =
  | 'structured'
  | 's3'
  | 'smb'
  | 'ols'
  | 'sharepoint'
  | 'onedrive';

/** Highlight only pre-flight actions at catalog steps 1–2. Step 17 ENABLE OLS stays in the main grid. */
function findConsecutiveActionBlock(
  catalog: StepCatalogRow[]
): { start: number; end: number } | null {
  if (catalog.length < 2) return null;
  const k0 = String(catalog[0].ds_kind || '').trim().toLowerCase();
  const k1 = String(catalog[1].ds_kind || '').trim().toLowerCase();
  if (k0 !== 'action' || k1 !== 'action') return null;
  const n0 = String(catalog[0].notes || '').toUpperCase();
  const n1 = String(catalog[1].notes || '').toUpperCase();
  if (n0.includes('ENABLE OLS') || n1.includes('ENABLE OLS')) return null;
  return { start: 0, end: 1 };
}

function stepMatchesPreset(row: StepCatalogRow, preset: QuickPreset): boolean {
  const k = String(row.ds_kind || '').trim().toLowerCase();
  switch (preset) {
    case 'structured':
      return !!row.is_structured_ff;
    case 's3':
      return k === 's3';
    case 'smb':
      return k === 'smb';
    case 'ols':
      return !!row.is_ols;
    case 'sharepoint':
      return k === 'sharepoint-online-v2';
    case 'onedrive':
      return k === 'onedrive-v2';
    default:
      return false;
  }
}

const PRESET_GROUPS: { preset: QuickPreset; label: string; hint: string }[] = [
  {
    preset: 'structured',
    label: 'Structured',
    hint: 'Toggle structured discovery steps (CSV). If all matching steps are on, turns them off; otherwise selects all.',
  },
  {
    preset: 's3',
    label: 'S3',
    hint: 'Toggle all S3 steps. If all matching steps are on, turns them off; otherwise selects all.',
  },
  {
    preset: 'smb',
    label: 'SMB',
    hint: 'Toggle all SMB steps. If all matching steps are on, turns them off; otherwise selects all.',
  },
  {
    preset: 'ols',
    label: 'OLS',
    hint: 'Toggle all OLS-flagged steps. If all matching steps are on, turns them off; otherwise selects all.',
  },
  {
    preset: 'sharepoint',
    label: 'SharePoint',
    hint: 'Toggle SharePoint Online steps. If all matching steps are on, turns them off; otherwise selects all.',
  },
  {
    preset: 'onedrive',
    label: 'OneDrive',
    hint: 'Toggle OneDrive v2 steps. If all matching steps are on, turns them off; otherwise selects all.',
  },
];

const PRESET_BTN_STYLE: React.CSSProperties = {
  fontSize: '0.74rem',
  padding: '4px 9px',
  borderRadius: 6,
  border: '1px solid var(--surface-border)',
  background: 'var(--surface-alt, rgba(0,0,0,0.04))',
  cursor: 'pointer',
  fontFamily: 'inherit',
  whiteSpace: 'nowrap',
};

/** Distinct BigID build versions from active workflow-pool environments. */
function workflowBuildLabel(envs: EnvironmentRow[]): string {
  const pool = envs.filter((e) => e.is_active && e.include_in_workflow_runs !== false);
  const versions = [
    ...new Set(pool.map((e) => String(e.version ?? '').trim()).filter((v) => v.length > 0)),
  ].sort();
  if (versions.length === 1) return versions[0];
  if (versions.length > 1) return versions.slice(0, 3).join('+');
  return '';
}

function defaultWorkflowName(envs: EnvironmentRow[]): string {
  const ts = new Date().toISOString().slice(0, 16).replace('T', ' ');
  const build = workflowBuildLabel(envs);
  return build ? `Workflow ${build} ${ts} UTC` : `Workflow ${ts} UTC`;
}

/** Returns a human-readable cooldown label like "⏸ 2m 14s" or "⏸ Starting…". */
function cooldownLabel(nextEligibleAt: string | null | undefined): string {
  if (!nextEligibleAt) return '⏸ Cooldown';
  const eligibleMs = parseBackendTs(nextEligibleAt);
  if (!Number.isFinite(eligibleMs)) return '⏸ Cooldown';
  const diff = eligibleMs - Date.now();
  if (diff <= 0) return '⏸ Starting…';
  const mins = Math.floor(diff / 60000);
  const secs = Math.floor((diff % 60000) / 1000);
  const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  const utcTime = new Date(eligibleMs).toISOString().slice(11, 16);
  return `⏸ Cooldown ${timeStr} (→${utcTime}Z)`;
}

function statusBadge(status: string | null | undefined): React.ReactElement {
  const s = String(status ?? '').trim().toLowerCase();
  const styles: Record<string, { bg: string; fg: string }> = {
    running: { bg: '#dcfce7', fg: '#16a34a' },
    completed: { bg: '#dbeafe', fg: '#2563eb' },
    created: { bg: '#f3f4f6', fg: '#6b7280' },
    paused: { bg: '#fef9c3', fg: '#ca8a04' },
    stopped: { bg: '#fee2e2', fg: '#dc2626' },
    failed: { bg: '#fee2e2', fg: '#dc2626' },
  };
  const { bg, fg } = styles[s] || { bg: '#f3f4f6', fg: '#6b7280' };
  const label = String(status ?? '').trim() || '—';
  return (
    <span
      style={{
        background: bg,
        color: fg,
        padding: '2px 8px',
        borderRadius: '10px',
        fontSize: '0.78rem',
        fontWeight: 600,
        display: 'inline-block',
      }}
    >
      {label}
    </span>
  );
}

function fmtCell(v: string | null | undefined): string {
  if (v == null || v === '') return '—';
  const t = String(v).trim();
  if (!t || t.toLowerCase() === 'nan') return '—';
  return t;
}

/**
 * Environments a run is bound to. 📌 marks an explicit selection (Env 1 / Env 2 chosen at
 * create) — that run touches nothing else, and no other run may take those envs. A run with
 * no selection uses whatever envs are available at the time, so there is nothing to name.
 */
function runEnvSlots(
  run: WorkflowRun,
  envById: Map<number, EnvironmentRow>
): { name: string; url: string | null }[] {
  return [
    { id: run.env1_id, name: run.env1_name },
    { id: run.env2_id, name: run.env2_name },
  ]
    .filter((slot) => slot.id != null || slot.name)
    .map((slot) => {
      const env = slot.id != null ? envById.get(slot.id) : undefined;
      return { name: slot.name || env?.env_name || `env-${slot.id}`, url: env?.base_url || null };
    });
}

/** Elapsed scan time for a step (HH:MM:SS) from its start → finish window.
 *  Falls back to "now" while a started step is still running. */
/**
 * Parse a backend timestamp to epoch ms. Backend stores naive UTC (datetime.utcnow())
 * with no timezone suffix; Date.parse would treat those as LOCAL time, inflating
 * elapsed times by the browser's UTC offset (e.g. +5:30 in IST). Append 'Z' when the
 * string carries no timezone designator so it's always read as UTC.
 */
function parseBackendTs(s: string | null | undefined): number {
  if (!s) return NaN;
  let str = String(s).trim();
  if (!str) return NaN;
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(str);
  if (!hasTz) {
    str = (str.includes('T') ? str : str.replace(' ', 'T')) + 'Z';
  }
  return Date.parse(str);
}

function fmtScanTime(start: string | null | undefined, finish: string | null | undefined): string {
  if (!start) return '—';
  const t0 = parseBackendTs(start);
  const t1 = finish ? parseBackendTs(finish) : Date.now();
  if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 < t0) return '—';
  const sec = Math.floor((t1 - t0) / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function canAction(status: string | null | undefined, action: string): boolean {
  const r = String(status ?? '').trim().toLowerCase();
  switch (action) {
    case 'start':
      return ['created', 'stopped', 'paused', 'failed'].includes(r);
    case 'pause':
      return r === 'running';
    case 'resume':
      return r === 'paused';
    case 'stop':
      return r === 'running' || r === 'paused';
    case 'delete':
      return r !== 'running';
    default:
      return true;
  }
}

const WF_PAGE_SIZE = 25;

const Workflow: React.FC<WorkflowProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const isAdmin = useUserRole(token) === 'Admin';  // clear-busy-locks is Admin-only (backend require_admin)
  const queryClient = useQueryClient();
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [runPage, setRunPage] = useState(0);
  const [createName, setCreateName] = useState('');
  const [createCooldown, setCreateCooldown] = useState(10);
  const [createEnv1, setCreateEnv1] = useState<string>('');
  const [createEnv2, setCreateEnv2] = useState<string>('');
  const [stepEnabled, setStepEnabled] = useState<boolean[]>([]);
  const [stepsExpanded, setStepsExpanded] = useState(false);
  const [startAfterCreate, setStartAfterCreate] = useState(true);
  const [refreshingRunId, setRefreshingRunId] = useState<number | null>(null);

  const {
    data: stepCatalogResp,
    isLoading: catalogLoading,
    isError: catalogError,
  } = useQuery({
    queryKey: ['workflowStepCatalog', token],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/workflow/step-catalog`, authConfig);
      return res.data as { steps: StepCatalogRow[] };
    },
  });

  useEffect(() => {
    const steps = stepCatalogResp?.steps;
    if (!steps?.length) return;
    setStepEnabled((prev) => {
      if (prev.length === steps.length) return prev;
      // Match server catalog defaults (Streamlit: enabled steps are on by default)
      return steps.map((row) => row.is_enabled !== false);
    });
  }, [stepCatalogResp]);

  const enabledStepCount = useMemo(() => stepEnabled.filter(Boolean).length, [stepEnabled]);

  const { data: runsPayload, isLoading: runsLoading, error: runsError } = useQuery({
    queryKey: ['workflowRuns', token, runPage],
    queryFn: async () => {
      const res = await axios.get<{ runs: WorkflowRun[]; total: number }>(`${API_BASE}/api/workflow/runs`, {
        ...authConfig,
        params: { limit: WF_PAGE_SIZE, offset: runPage * WF_PAGE_SIZE },
      });
      return res.data;
    },
    refetchInterval: (query) => {
      // Only poll if there are running workflows on current page
      const data = query.state.data;
      const hasRunning = data?.runs?.some((r: WorkflowRun) =>
        String(r.status || '').trim().toLowerCase() === 'running'
      );
      return hasRunning ? 5000 : false;
    },
  });

  // Memoized: a fresh [] each render would re-run every useMemo that depends on runs.
  const runs = useMemo(() => runsPayload?.runs ?? [], [runsPayload]);
  const runsTotal = runsPayload?.total ?? 0;

  useEffect(() => {
    setExpandedId(null);
  }, [runPage]);

  // Check if any workflows are running
  const hasRunningWorkflows = useMemo(() => {
    return runs.some((r) => String(r.status || '').trim().toLowerCase() === 'running');
  }, [runs]);

  // Fetch steps ONLY for running workflows (to reduce API load)
  // For completed workflows, steps are fetched on-demand when expanded
  const runningRunIdsCsv = useMemo(() => {
    return runs
      .filter((r) => String(r.status || '').trim().toLowerCase() === 'running')
      .map((r) => r.id)
      .join(',');
  }, [runs]);

  const { data: stepsByRun = {}, isLoading: stepsLoading } = useQuery({
    queryKey: ['workflowStepsBatch', token, runningRunIdsCsv],
    queryFn: async () => {
      if (!runningRunIdsCsv) return {};
      const res = await axios.get(`${API_BASE}/api/workflow/steps/batch`, {
        ...authConfig,
        params: { run_ids: runningRunIdsCsv },
      });
      return res.data as Record<string, WorkflowStepRow[]>;
    },
    enabled: runningRunIdsCsv.length > 0,
    refetchInterval: hasRunningWorkflows ? 5000 : false,
  });

  const { data: envChips = [] } = useQuery({
    queryKey: ['workflowEnvStatus', token],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/workflow/env-status`, authConfig);
      return res.data as EnvChip[];
    },
    refetchInterval: hasRunningWorkflows ? 8000 : false,
  });

  const { data: environments = [] } = useQuery({
    queryKey: ['environments', token],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/environments`, authConfig);
      return res.data as EnvironmentRow[];
    },
  });

  const activeEnvOptions = useMemo(
    () => environments.filter((e) => e.is_active).sort((a, b) => a.env_name.localeCompare(b.env_name)),
    [environments]
  );

  const envById = useMemo(() => new Map(environments.map((e) => [e.id, e])), [environments]);

  const defaultCreateName = useMemo(() => defaultWorkflowName(environments), [environments]);

  const stepsSyncedWithCatalog =
    !stepCatalogResp?.steps?.length ||
    stepEnabled.length === stepCatalogResp.steps.length;

  const createBlockedBySteps =
    stepsSyncedWithCatalog &&
    !!stepCatalogResp?.steps?.length &&
    enabledStepCount < 1;

  /** Avoid submitting before checkbox state matches catalog (prevents accidental server-default create). */
  const createBlockedUntilSynced =
    !!stepCatalogResp?.steps?.length &&
    stepEnabled.length !== stepCatalogResp.steps.length;

  const createBlockedByCatalog = catalogLoading && !catalogError;

  /** Toggle one family: if every matching step is enabled, disables all matches; otherwise enables every match. */
  const togglePresetGroup = (preset: QuickPreset) => {
    setStepEnabled((prev) => {
      const cat = stepCatalogResp?.steps;
      if (!cat?.length || prev.length !== cat.length) return prev;
      const indices: number[] = [];
      for (let i = 0; i < cat.length; i++) {
        if (stepMatchesPreset(cat[i], preset)) indices.push(i);
      }
      if (indices.length === 0) return prev;
      const allOn = indices.every((i) => prev[i]);
      const target = !allOn;
      const next = [...prev];
      indices.forEach((i) => {
        next[i] = target;
      });
      return next;
    });
  };

  const createMutation = useMutation({
    mutationFn: async (opts?: { startAfter?: boolean }) => {
      const body: Record<string, unknown> = {
        name: (createName || '').trim() || defaultCreateName,
        cooldown_minutes: createCooldown,
      };
      const e1 = parseInt(createEnv1, 10);
      const e2 = parseInt(createEnv2, 10);
      if (!Number.isNaN(e1)) body.env1_id = e1;
      if (!Number.isNaN(e2)) body.env2_id = e2;
      body.auto_start = opts?.startAfter !== false;
      const cat = stepCatalogResp?.steps;
      if (cat?.length && stepEnabled.length === cat.length) {
        body.steps = cat.map((row, idx) => ({
          ds_kind: row.ds_kind,
          ds_name: row.ds_name ?? '',
          scan_kind: row.scan_kind ?? 'full_scan',
          file_set_type: row.file_set_type ?? null,
          is_enabled: !!stepEnabled[idx],
          is_acl: !!row.is_acl,
          is_ocr: false,
          is_structured_ff: !!row.is_structured_ff,
          is_ols: !!row.is_ols,
          is_lite: !!row.is_lite,
          feature_flags: row.feature_flags ?? null,
          notes: row.notes ?? null,
        }));
      }
      const res = await axios.post(`${API_BASE}/api/workflow/runs`, body, authConfig);
      return res.data as { id: number; status?: string };
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['workflowRuns', token] });
      queryClient.invalidateQueries({ queryKey: ['workflowStepsBatch'] });
      queryClient.invalidateQueries({ queryKey: ['workflowEnvStatus', token] });
      setRunPage(0);
      if (data?.id) setExpandedId(data.id);
      setCreateName('');
    },
  });

  const clearLocksMutation = useMutation({
    mutationFn: async () => {
      const res = await axios.post(`${API_BASE}/api/environments/actions/clear-busy-locks`, {}, authConfig);
      return res.data as { cleared_rows?: number };
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflowEnvStatus', token] });
      queryClient.invalidateQueries({ queryKey: ['environments', token] });
    },
  });

  const controlMutation = useMutation({
    mutationFn: async (vars: {
      runId: number;
      action: 'start' | 'pause' | 'resume' | 'stop';
      env1Id?: number | null;
      env2Id?: number | null;
    }) => {
      await axios.post(`${API_BASE}/api/workflow/runs/${vars.runId}/control`, {
        action: vars.action,
        env1_id: vars.env1Id,
        env2_id: vars.env2Id
      }, authConfig);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflowRuns', token] });
      queryClient.invalidateQueries({ queryKey: ['workflowStepsBatch'] });
      queryClient.invalidateQueries({ queryKey: ['workflowEnvStatus', token] });
    },
    onError: (err: unknown) => {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (err as Error)?.message ||
        'Control action failed';
      window.alert(detail);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (runId: number) => {
      await axios.delete(`${API_BASE}/api/workflow/runs/${runId}`, authConfig);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflowRuns', token] });
      queryClient.invalidateQueries({ queryKey: ['workflowStepsBatch'] });
      setExpandedId(null);
    },
  });

  const toggleExpand = (id: number) => {
    setExpandedId((cur) => (cur === id ? null : id));
  };

  const refreshWorkflowRun = async (runId: number) => {
    setRefreshingRunId(runId);
    try {
      const [runRes, stepsRes] = await Promise.all([
        axios.get<WorkflowRun>(`${API_BASE}/api/workflow/runs/${runId}`, authConfig),
        axios.get<WorkflowStepRow[]>(`${API_BASE}/api/workflow/steps/${runId}`, authConfig),
      ]);
      const updated = runRes.data;
      const steps = stepsRes.data;

      queryClient.setQueryData(
        ['workflowRuns', token, runPage],
        (old: { runs: WorkflowRun[]; total: number } | undefined) => {
          if (!old?.runs) return old;
          return {
            ...old,
            runs: old.runs.map((r) => (r.id === runId ? { ...r, ...updated } : r)),
          };
        }
      );

      const patchSteps = (prev: Record<string | number, WorkflowStepRow[]> | undefined) => ({
        ...(prev || {}),
        [runId]: steps,
        [String(runId)]: steps,
      });

      queryClient.setQueryData(['workflowStepsBatch', token, runningRunIdsCsv], patchSteps);
      queryClient.setQueryData(['workflowSteps', token, runId], steps);
    } catch (err) {
      console.error('Refresh workflow run failed:', err);
    } finally {
      setRefreshingRunId(null);
    }
  };

  // Fetch steps for expanded workflow if not already loaded (on-demand for completed workflows)
  const { data: expandedSteps } = useQuery({
    queryKey: ['workflowSteps', token, expandedId],
    queryFn: async () => {
      if (!expandedId) return [];
      const res = await axios.get(`${API_BASE}/api/workflow/steps/${expandedId}`, authConfig);
      return res.data as WorkflowStepRow[];
    },
    // An empty array [] is truthy, so guard on length: a batch result that returned
    // no steps for this run must still trigger the on-demand fetch.
    enabled: expandedId !== null && !stepsByRun[expandedId]?.length,
  });

  // Merge batch-loaded steps with on-demand expanded steps
  const allStepsByRun = useMemo(() => {
    const merged = { ...stepsByRun };
    if (expandedId && expandedSteps && !merged[expandedId]?.length) {
      merged[expandedId] = expandedSteps;
    }
    return merged;
  }, [stepsByRun, expandedId, expandedSteps]);

  const anyRunInCooldown = useMemo(
    () => runs.some((r) => isRunInCooldown(r)),
    [runs]
  );

  const [cooldownTick, setCooldownTick] = useState(0);
  useEffect(() => {
    if (!anyRunInCooldown) return;
    const id = window.setInterval(() => setCooldownTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [anyRunInCooldown]);

  const envChipLabels = useMemo(() => {
    void cooldownTick;
    return envChips.map((e) => {
      if (e.busy) {
        return { ...e, label: `${e.env_name} (busy)`, icon: '🔴' as const };
      }
      const until = findEnvCooldownUntil(e.id, runs, allStepsByRun);
      if (until) {
        return { ...e, label: `${e.env_name} (${cooldownLabel(until)})`, icon: '🟡' as const };
      }
      return { ...e, label: `${e.env_name} (free)`, icon: '🟢' as const };
    });
  }, [envChips, runs, allStepsByRun, cooldownTick]);

  /**
   * Env ids already selected by a still-active run. The backend rejects selecting these
   * (a selected env belongs to one run only), so mark them in the dropdowns instead of
   * letting the user pick one and get a 400 back.
   */
  const claimedEnvIds = useMemo(() => {
    const claimed = new Map<number, number>(); // env id -> run id
    runs.forEach((r) => {
      const st = String(r.status || '').trim().toLowerCase();
      if (!['created', 'running', 'paused', 'failed'].includes(st)) return;
      [r.env1_id, r.env2_id].forEach((eid) => {
        if (eid != null && !claimed.has(eid)) claimed.set(eid, r.id);
      });
    });
    return claimed;
  }, [runs]);

  /** Dropdown label for an env: live status + the run holding it, if any. */
  const envOptionLabel = (env: EnvironmentRow) => {
    const chip = envChipLabels.find((c) => c.id === env.id);
    const heldBy = claimedEnvIds.get(env.id);
    const parts = [chip ? `${chip.icon} ${chip.label}` : env.env_name];
    if (heldBy != null) parts.push(`— run #${heldBy}`);
    return parts.join(' ');
  };

  /** A run owns each selected env end-to-end, so the two slots must be different envs. */
  const envOptionsFor = (slot: 'e1' | 'e2') => {
    const other = slot === 'e1' ? createEnv2 : createEnv1;
    return activeEnvOptions.filter((env) => String(env.id) !== other);
  };

  const pinnedEnvNames = useMemo(() => {
    return [createEnv1, createEnv2]
      .map((v) => activeEnvOptions.find((e) => String(e.id) === v)?.env_name)
      .filter(Boolean) as string[];
  }, [createEnv1, createEnv2, activeEnvOptions]);

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <section>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.75rem', marginBottom: '0.5rem' }}>
          <span style={{ fontSize: '1.75rem', lineHeight: 1 }} aria-hidden>
            🧪
          </span>
          <div>
            <h2 style={{ color: 'var(--text-primary)', fontSize: '1.5rem', fontWeight: 700, margin: 0 }}>Workflow Automation</h2>
          </div>
        </div>

        <div className="glass-panel" style={{ marginBottom: '1rem', padding: '1rem 1.25rem' }}>
          <div style={{ fontWeight: 600, marginBottom: '0.75rem', color: 'var(--text-primary)' }}>New workflow run</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', alignItems: 'flex-end' }}>
            <div className="input-group" style={{ margin: 0, minWidth: 240, flex: '1 1 200px' }}>
              <label htmlFor="wf-create-name">Name</label>
              <input
                id="wf-create-name"
                type="text"
                placeholder={defaultCreateName}
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', width: '100%', fontFamily: 'inherit' }}
              />
            </div>
            <div className="input-group" style={{ margin: 0, minWidth: 120 }}>
              <label htmlFor="wf-create-cd">Cooldown (min)</label>
              <input
                id="wf-create-cd"
                type="number"
                min={1}
                max={180}
                value={createCooldown}
                onChange={(e) => setCreateCooldown(Number(e.target.value) || 10)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              />
            </div>
            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
              <label htmlFor="wf-e1">Env 1</label>
              <select
                id="wf-e1"
                value={createEnv1}
                onChange={(e) => setCreateEnv1(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              >
                <option value="">— any available —</option>
                {envOptionsFor('e1').map((env) => (
                  <option key={env.id} value={String(env.id)}>
                    {envOptionLabel(env)}
                  </option>
                ))}
              </select>
            </div>
            <div className="input-group" style={{ margin: 0, minWidth: 200 }}>
              <label htmlFor="wf-e2">Env 2</label>
              <select
                id="wf-e2"
                value={createEnv2}
                onChange={(e) => setCreateEnv2(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              >
                <option value="">— any available —</option>
                {envOptionsFor('e2').map((env) => (
                  <option key={env.id} value={String(env.id)}>
                    {envOptionLabel(env)}
                  </option>
                ))}
              </select>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.85rem', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={startAfterCreate}
                onChange={(e) => setStartAfterCreate(e.target.checked)}
              />
              Start immediately after create
            </label>
            <button
              type="button"
              className="btn"
              disabled={
                createMutation.isPending ||
                createBlockedByCatalog ||
                createBlockedUntilSynced ||
                createBlockedBySteps
              }
              onClick={() => createMutation.mutate({ startAfter: startAfterCreate })}
              style={{ fontWeight: 600 }}
            >
              {createMutation.isPending
                ? startAfterCreate
                  ? 'Creating & starting…'
                  : 'Creating…'
                : startAfterCreate
                ? 'Create & start'
                : 'Create workflow'}
            </button>
          </div>

          <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: '0.65rem', marginBottom: 0, lineHeight: 1.5 }}>
            {pinnedEnvNames.length > 0 ? (
              <>
                📌 This run will execute <strong>only</strong> on {pinnedEnvNames.join(' + ')}
                {pinnedEnvNames.length === 1 && ' (single env — steps run one at a time)'}, and no
                other workflow can use {pinnedEnvNames.length === 1 ? 'it' : 'them'} while it runs.
                The remaining environments stay open for other workflows.
              </>
            ) : (
              <>
                No selection: this run uses <strong>all available</strong> environments — the whole
                workflow pool minus any env another run has selected. Pick Env 1 / Env 2 to reserve
                specific environments instead; every step and pre-flight action then runs only
                there, and those envs become off-limits to other workflows.
              </>
            )}
          </p>

          {catalogLoading && !catalogError && (
            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: '0.65rem', marginBottom: 0 }}>
              Loading step catalog…
            </p>
          )}
          {catalogError && (
            <p style={{ fontSize: '0.82rem', color: 'var(--danger-color)', marginTop: '0.65rem', marginBottom: 0 }}>
              Could not load step catalog — create will use server defaults.
            </p>
          )}
          {stepCatalogResp?.steps?.length ? (
            <div style={{ marginTop: '0.85rem' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap', marginBottom: '0.45rem' }}>
                <button
                  type="button"
                  style={{
                    fontWeight: 600,
                    fontSize: '0.88rem',
                    color: 'var(--text-primary)',
                    background: 'transparent',
                    border: 'none',
                    cursor: 'pointer',
                    padding: 0,
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.5rem',
                  }}
                  onClick={() => setStepsExpanded(!stepsExpanded)}
                >
                  <span style={{ fontSize: '1rem' }}>{stepsExpanded ? '▼' : '▶'}</span>
                  Steps to include ({enabledStepCount}/{stepCatalogResp.steps.length})
                </button>
                <button
                  type="button"
                  style={{
                    fontSize: '0.78rem',
                    padding: '4px 10px',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    background: 'var(--surface-alt, rgba(0,0,0,0.04))',
                    cursor: 'pointer',
                    fontFamily: 'inherit',
                  }}
                  onClick={() =>
                    setStepEnabled(stepCatalogResp.steps!.map(() => true))
                  }
                >
                  Enable all
                </button>
                <button
                  type="button"
                  style={{
                    fontSize: '0.78rem',
                    padding: '4px 10px',
                    borderRadius: 6,
                    border: '1px solid var(--surface-border)',
                    background: 'var(--surface-alt, rgba(0,0,0,0.04))',
                    cursor: 'pointer',
                    fontFamily: 'inherit',
                  }}
                  onClick={() =>
                    setStepEnabled(stepCatalogResp.steps!.map(() => false))
                  }
                >
                  Disable all
                </button>
              </div>
              {stepsExpanded && (
                <>
                  <div
                    style={{
                      marginTop: '0.55rem',
                      marginBottom: '0.45rem',
                      display: 'flex',
                      flexWrap: 'wrap',
                      alignItems: 'center',
                      gap: '6px 8px',
                      rowGap: '8px',
                    }}
                  >
                    <span style={{ fontSize: '0.76rem', color: 'var(--text-secondary)', width: '100%', flexBasis: '100%' }}>
                      Toggle groups (all checked in family → clears; otherwise selects all matching steps):
                    </span>
                    {PRESET_GROUPS.map(({ preset, label, hint }) => (
                      <button
                        key={preset}
                        type="button"
                        style={PRESET_BTN_STYLE}
                        title={hint}
                        onClick={() => togglePresetGroup(preset)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div
                    style={{
                      maxHeight: 300,
                      overflowY: 'auto',
                      border: '1px solid var(--surface-border)',
                      borderRadius: 8,
                      padding: '10px 10px',
                      fontSize: '0.82rem',
                      background: 'rgba(0,0,0,0.02)',
                    }}
                  >
                    {(() => {
                      const catalog = stepCatalogResp.steps!;
                      const actionBlock = findConsecutiveActionBlock(catalog);
                      const gridStyle: React.CSSProperties = {
                        display: 'grid',
                        gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
                        gap: '10px 14px',
                      };

                      const renderStep = (row: StepCatalogRow, idx: number) => {
                        const prev = idx > 0 ? catalog[idx - 1] : null;
                        const isActionStep =
                          String(row.ds_kind || '')
                            .trim()
                            .toLowerCase() === 'action';
                        const olsBlockStarts = !!row.is_ols && (!prev || !prev.is_ols);

                        return (
                          <React.Fragment key={row.seq}>
                            {olsBlockStarts ? (
                              <div
                                style={{
                                  gridColumn: '1 / -1',
                                  borderTop: '1px solid var(--surface-border)',
                                  opacity: 0.65,
                                  marginTop: idx > 0 ? 4 : 0,
                                  marginBottom: 2,
                                  height: 0,
                                }}
                                aria-hidden
                              />
                            ) : null}
                            <label
                              style={{
                                display: 'flex',
                                alignItems: 'flex-start',
                                gap: '8px',
                                padding: '8px 10px',
                                cursor: 'pointer',
                                borderRadius: 8,
                                opacity: stepEnabled[idx] === false ? 0.55 : 1,
                                background: isActionStep ? 'rgba(251, 191, 36, 0.22)' : 'transparent',
                                border: isActionStep
                                  ? '1px solid rgba(217, 119, 6, 0.45)'
                                  : '1px solid transparent',
                                boxShadow: isActionStep ? 'inset 0 0 0 1px rgba(255,255,255,0.12)' : undefined,
                              }}
                            >
                              <input
                                type="checkbox"
                                checked={stepEnabled[idx] !== false}
                                disabled={stepEnabled.length !== catalog.length}
                                onChange={(e) => {
                                  const on = e.target.checked;
                                  setStepEnabled((prevEnabled) => {
                                    const next = [...prevEnabled];
                                    next[idx] = on;
                                    return next;
                                  });
                                }}
                                style={{ marginTop: 3 }}
                              />
                              <span style={{ lineHeight: 1.45 }}>
                                <strong style={{ color: 'var(--text-primary)' }}>
                                  {row.seq}. {row.ds_kind}
                                </strong>
                                {row.ds_name ? (
                                  <span style={{ color: 'var(--text-secondary)' }}>{' — '}{row.ds_name}</span>
                                ) : null}
                                {row.notes ? (
                                  <span style={{ display: 'block', color: 'var(--text-secondary)', fontSize: '0.78rem' }}>
                                    {row.notes}
                                  </span>
                                ) : null}
                              </span>
                            </label>
                          </React.Fragment>
                        );
                      };

                      if (!actionBlock) {
                        return <div style={gridStyle}>{catalog.map((row, idx) => renderStep(row, idx))}</div>;
                      }

                      const before = catalog.slice(0, actionBlock.start);
                      const middle = catalog.slice(actionBlock.start, actionBlock.end + 1);
                      const after = catalog.slice(actionBlock.end + 1);

                      return (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                          {before.length > 0 ? (
                            <div style={gridStyle}>{before.map((row, j) => renderStep(row, j))}</div>
                          ) : null}
                          <div
                            style={{
                              display: 'flex',
                              justifyContent: 'center',
                              alignItems: 'stretch',
                              width: '100%',
                              padding: '4px 0 8px',
                            }}
                          >
                            <div
                              style={{
                                width: '100%',
                                maxWidth: 'min(440px, 96%)',
                                boxSizing: 'border-box',
                                display: 'flex',
                                flexDirection: 'column',
                                gap: 10,
                              }}
                            >
                              {middle.map((row, k) => renderStep(row, actionBlock.start + k))}
                            </div>
                          </div>
                          {after.length > 0 ? (
                            <div style={gridStyle}>
                              {after.map((row, j) => renderStep(row, actionBlock.end + 1 + j))}
                            </div>
                          ) : null}
                        </div>
                      );
                    })()}
                  </div>
                </>
              )}
              {createBlockedBySteps && (
                <p style={{ fontSize: '0.76rem', color: 'var(--danger-color)', marginTop: '0.45rem', marginBottom: 0 }}>
                  Select at least one step.
                </p>
              )}
              {createMutation.isError && (
                <p style={{ fontSize: '0.76rem', color: 'var(--danger-color)', marginTop: '0.45rem', marginBottom: 0 }}>
                  Error creating workflow: {(createMutation.error as any)?.response?.data?.detail || (createMutation.error as Error)?.message || 'Unknown error'}
                </p>
              )}
            </div>
          ) : null}

        </div>

        {runsError && (
          <div className="glass-panel" style={{ padding: '1rem', color: 'var(--danger-color)', marginBottom: '1rem' }}>
            Workflow API error. {(runsError as Error).message}
          </div>
        )}

        <h3 style={{ fontSize: '1.1rem', fontWeight: 700, margin: '0.75rem 0 0.35rem', color: 'var(--text-primary)' }}>Workflow Runs</h3>

        {envChips.length > 0 && (
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '10px', lineHeight: 1.6 }}>
            <strong style={{ color: 'var(--text-primary)' }}>Env status:</strong>{' '}
            {envChipLabels.map((e) => (
              <span key={e.id} style={{ marginRight: '0.75rem', whiteSpace: 'nowrap' }}>
                {e.icon} {e.label}
              </span>
            ))}
            {isAdmin && (
            <button
              type="button"
              style={{
                marginLeft: 8,
                fontSize: '0.78rem',
                padding: '4px 10px',
                borderRadius: 6,
                border: '1px solid var(--surface-border)',
                background: 'transparent',
                cursor: clearLocksMutation.isPending ? 'wait' : 'pointer',
                fontFamily: 'inherit',
              }}
              disabled={clearLocksMutation.isPending}
              onClick={() => clearLocksMutation.mutate()}
              title="Clear stale is_busy flags left by a stopped workflow"
            >
              {clearLocksMutation.isPending ? 'Clearing…' : 'Clear env locks'}
            </button>
            )}
          </div>
        )}

        <div className="glass-panel" style={{ overflow: 'hidden', padding: 0 }}>
          {runsLoading ? (
            <div style={{ padding: '3rem', textAlign: 'center' }}>
              <Loader2 className="lucide-spin" />
            </div>
          ) : runs.length === 0 ? (
            <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              No workflow runs.
            </div>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table className="workflow-automation-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.88rem' }}>
                  <thead>
                    <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Run</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Name</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Status</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Envs</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Started</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Finished</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Last Error</th>
                      <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => {
                      const isOpen = expandedId === run.id;
                      const rawSteps = allStepsByRun[String(run.id)] ?? allStepsByRun[run.id] ?? [];
                      const runStatus = run.status;
                      const metrics = computeStepMetrics(rawSteps as unknown as Record<string, unknown>[], rawSteps.length);
                      return (
                        <React.Fragment key={run.id}>
                          <tr
                            className="wf-main-row"
                            onClick={() => toggleExpand(run.id)}
                            style={{
                              cursor: 'pointer',
                              background: isOpen ? 'rgba(99, 102, 241, 0.08)' : 'transparent',
                              borderBottom: '1px solid var(--surface-border)',
                            }}
                          >
                            <td style={{ padding: '10px 12px', fontWeight: 600, whiteSpace: 'nowrap' }}>
                              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                                {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                                #{run.id}
                              </span>
                            </td>
                            <td style={{ padding: '10px 12px', maxWidth: 220 }}>{fmtCell(run.name)}</td>
                            <td style={{ padding: '10px 12px' }}>{statusBadge(run.status)}</td>
                            <td
                              style={{ padding: '10px 12px', fontSize: '0.82rem', whiteSpace: 'nowrap' }}
                              title={
                                run.env_pinned
                                  ? 'Selected at create — this run uses only these environments, and no other run may take them'
                                  : 'No selection — uses every available environment in the workflow pool'
                              }
                            >
                              {(() => {
                                const slots = runEnvSlots(run, envById);
                                if (!slots.length) return 'all available';
                                return (
                                  <>
                                    {run.env_pinned ? '📌 ' : ''}
                                    {slots.map((slot, i) => (
                                      <React.Fragment key={`${slot.name}-${i}`}>
                                        {i > 0 ? ', ' : ''}
                                        {slot.url ? (
                                          <a
                                            href={slot.url}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            onClick={(e) => e.stopPropagation()}
                                            style={{ color: 'var(--primary-color)', textDecoration: 'underline' }}
                                          >
                                            {slot.name}
                                          </a>
                                        ) : (
                                          slot.name
                                        )}
                                      </React.Fragment>
                                    ))}
                                  </>
                                );
                              })()}
                            </td>
                            <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>{fmtCell(run.started_at)}</td>
                            <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>{fmtCell(run.finished_at)}</td>
                            <td style={{ padding: '10px 12px', maxWidth: 200, color: 'var(--danger-color)', fontSize: '0.82rem' }}>
                              {fmtCell(run.last_error)}
                            </td>
                            <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                              <span style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                                <button
                                  type="button"
                                  title="Refresh this run"
                                  disabled={refreshingRunId === run.id}
                                  onClick={() => refreshWorkflowRun(run.id)}
                                  style={{
                                    background: 'transparent',
                                    border: 'none',
                                    cursor: refreshingRunId === run.id ? 'wait' : 'pointer',
                                    padding: '0 2px',
                                    lineHeight: 1,
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    opacity: refreshingRunId === run.id ? 0.5 : 1,
                                  }}
                                >
                                  <RefreshCw
                                    size={16}
                                    className={refreshingRunId === run.id ? 'lucide-spin' : undefined}
                                  />
                                </button>
                                {(
                                  [
                                    ['start', '▶️'],
                                    ['pause', '⏸'],
                                    ['resume', '↩️'],
                                    ['stop', '⏹'],
                                  ] as const
                                ).map(([act, emoji]) => {
                                  const enabled = canAction(run.status, act);
                                  return (
                                    <button
                                      key={act}
                                      type="button"
                                      title={act}
                                      disabled={!enabled || controlMutation.isPending}
                                      onClick={() => controlMutation.mutate({
                                        runId: run.id,
                                        action: act,
                                        env1Id: run.env1_id,
                                        env2Id: run.env2_id
                                      })}
                                      style={{
                                        background: 'transparent',
                                        border: 'none',
                                        cursor: enabled ? 'pointer' : 'default',
                                        fontSize: '1.05rem',
                                        opacity: enabled ? 1 : 0.28,
                                        padding: '0 2px',
                                        lineHeight: 1,
                                      }}
                                    >
                                      {emoji}
                                    </button>
                                  );
                                })}
                                <button
                                  type="button"
                                  title="Delete run"
                                  disabled={!canAction(run.status, 'delete') || deleteMutation.isPending}
                                  onClick={() => {
                                    if (window.confirm(`Delete workflow run #${run.id}?`)) {
                                      deleteMutation.mutate(run.id);
                                    }
                                  }}
                                  style={{
                                    background: 'transparent',
                                    border: 'none',
                                    cursor: canAction(run.status, 'delete') ? 'pointer' : 'default',
                                    fontSize: '1.05rem',
                                    opacity: canAction(run.status, 'delete') ? 1 : 0.28,
                                    padding: '0 2px',
                                    lineHeight: 1,
                                  }}
                                >
                                  🗑️
                                </button>
                              </span>
                            </td>
                          </tr>
                          {isOpen && (
                            <tr>
                              <td colSpan={8} style={{ padding: 0, border: 'none', background: 'var(--surface-bg, #faf8ff)' }}>
                                <div
                                  style={{
                                    borderLeft: '4px solid var(--primary-color, #6366f1)',
                                    margin: '0 0 0 12px',
                                    padding: '12px 16px',
                                  }}
                                >
                                  <div style={{ fontWeight: 700, fontSize: '0.95rem', color: 'var(--primary-color)', marginBottom: 8 }}>
                                    Step Progress — Run {run.id}
                                  </div>
                                  {String(runStatus || '').trim().toLowerCase() === 'created' && (
                                    <div
                                      style={{
                                        marginBottom: 12,
                                        padding: '10px 12px',
                                        borderRadius: 8,
                                        background: '#fef3c7',
                                        border: '1px solid #f59e0b',
                                        color: '#92400e',
                                        fontSize: '0.88rem',
                                      }}
                                    >
                                      Workflow is <strong>not started</strong> — the runner only dispatches steps when status is{' '}
                                      <em>running</em>. Click <strong>▶ Start</strong> in the row above (or use Create &amp; start).
                                    </div>
                                  )}
                                  {isRunInCooldown(run) && (
                                    <div
                                      style={{
                                        marginBottom: 12,
                                        padding: '10px 12px',
                                        borderRadius: 8,
                                        background: '#fffbeb',
                                        border: '1px solid #fbbf24',
                                        color: '#92400e',
                                        fontSize: '0.88rem',
                                        fontWeight: 600,
                                      }}
                                    >
                                      {cooldownLabel(run.next_eligible_at)} — next step dispatch after cooldown
                                    </div>
                                  )}
                                  {stepsLoading && !rawSteps.length ? (
                                    <Loader2 className="lucide-spin" size={22} />
                                  ) : (
                                    <>
                                      {(() => {
                                        void cooldownTick;
                                        const isCooldown = isRunInCooldown(run);
                                        const firstPendingStepId = isCooldown
                                          ? (rawSteps.find(
                                              (st) =>
                                                st.is_enabled &&
                                                workflowNormStepStatus(st.status) !== 'disabled' &&
                                                ['', 'pending'].includes(workflowNormStepStatus(st.status))
                                            )?.id ?? null)
                                          : null;

                                        return (
                                          <>
                                            <div style={{ display: 'flex', gap: 24, marginBottom: 12, flexWrap: 'wrap' }}>
                                              {(
                                                [
                                                  ['Pending', metrics.pending, '#6b7280'],
                                                  ['In Progress', metrics.in_prog, '#ca8a04'],
                                                  ['Completed', metrics.completed, '#16a34a'],
                                                  ['Disabled', metrics.disabled, '#6b7280'],
                                                  ['Errors', metrics.errors, '#dc2626'],
                                                ] as const
                                              ).map(([label, val, color]) => (
                                                <div key={label}>
                                                  <div style={{ fontSize: '0.78rem', color: '#6b7280' }}>{label}</div>
                                                  <strong style={{ fontSize: '1.05rem', color }}>{val}</strong>
                                                </div>
                                              ))}
                                            </div>
                                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                                              <thead>
                                                <tr style={{ background: '#e8e3f5' }}>
                                                  <th style={{ padding: '6px 8px', textAlign: 'center' }}>Step</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Env</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>DS Name</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Scan Status</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Flags</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Progress</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Scan Time</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Scan Id</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>View Progress</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Open</th>
                                                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Notes / Error</th>
                                                </tr>
                                              </thead>
                                              <tbody>
                                                {rawSteps
                                                  .filter((st) => {
                                                    const cs = workflowNormStepStatus(st.status);
                                                    return cs !== 'disabled' && st.is_enabled;
                                                  })
                                                  .map((st) => {
                                                    const rd = st as unknown as Record<string, unknown>;
                                                    const cStatus = workflowNormStepStatus(st.status);
                                                    let displayErr = String(st.last_error ?? '').trim();
                                                    if (
                                                      ['scan_running', 'scan_created', 'ds_created', 'ds_test_ok'].includes(cStatus) &&
                                                      workflowIsTransientStepMessage(displayErr)
                                                    ) {
                                                      displayErr = '';
                                                    }
                                                    if (displayErr && displayErr.includes('No free env this tick')) displayErr = '';

                                                    const name = String(st.environment_name ?? '').trim();
                                                    const envId = st.environment_id;
                                                    const busy = workflowStepImpliesEnvBusy(rd, runStatus);
                                                    let envDisplay = '—';
                                                    if (name) {
                                                      if (busy) {
                                                        envDisplay = `🔴 ${name}`;
                                                      } else if (
                                                        envId != null &&
                                                        isCooldown &&
                                                        !envHasActiveWorkflowStep(envId, rawSteps, runStatus)
                                                      ) {
                                                        envDisplay = `🟡 ${name} (${cooldownLabel(run.next_eligible_at)})`;
                                                      } else {
                                                        envDisplay = `🟢 ${name}`;
                                                      }
                                                    }

                                                    const isCooldownStep = isCooldown && st.id === firstPendingStepId;
                                                    const progressCell = isCooldownStep ? (
                                                      <span style={{ color: '#b45309', fontWeight: 600 }}>
                                                        {cooldownLabel(run.next_eligible_at)}
                                                      </span>
                                                    ) : (
                                                      computeProgressEmoji(rd)
                                                    );

                                                    return (
                                                      <tr
                                                        key={st.id}
                                                        style={{
                                                          background: isCooldownStep
                                                            ? 'rgba(254, 249, 195, 0.45)'
                                                            : st.seq % 2 === 0
                                                            ? '#f9f8ff'
                                                            : '#ffffff',
                                                        }}
                                                      >
                                                        <td style={{ padding: '6px 8px', textAlign: 'center', fontWeight: 600 }}>{st.seq}</td>
                                                        <td style={{ padding: '6px 8px' }}>{envDisplay}</td>
                                                        <td style={{ padding: '6px 8px', fontWeight: 500 }}>{fmtCell(st.ds_name)}</td>
                                                        <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>{fmtCell(st.scan_status)}</td>
                                                        <td style={{ padding: '6px 8px', fontSize: '0.8rem' }}>{flagsFromStep(rd)}</td>
                                                        <td style={{ padding: '6px 8px', fontSize: '0.82rem', whiteSpace: 'nowrap' }}>
                                                          {progressCell}
                                                        </td>
                                                        <td style={{ padding: '6px 8px', fontFamily: 'ui-monospace, monospace', fontSize: '0.8rem', whiteSpace: 'nowrap' }}>
                                                          {fmtScanTime(st.scan_started_at ?? st.started_at, st.scan_completed_at ?? st.finished_at)}
                                                        </td>
                                                        <td style={{ padding: '6px 8px', fontFamily: 'ui-monospace, monospace', fontSize: '0.78rem' }}>
                                                          {fmtCell(st.scan_id)}
                                                        </td>
                                                        <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>
                                                          {st.scan_id ? (
                                                            <Link
                                                              to={`/tools/scan-status?scan_id=${encodeURIComponent(st.scan_id)}`}
                                                              style={{ color: 'var(--primary-color)', fontSize: '0.8rem', textDecoration: 'none', fontWeight: 500 }}
                                                            >
                                                              View Progress
                                                            </Link>
                                                          ) : (
                                                            '—'
                                                          )}
                                                        </td>
                                                        <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>
                                                          {st.scan_id ? (
                                                            <Link
                                                              to={`/tools/add-benchmark?scan_id=${encodeURIComponent(st.scan_id)}`}
                                                              style={{ color: 'var(--primary-color)', fontSize: '0.8rem', textDecoration: 'none', fontWeight: 500 }}
                                                            >
                                                              Open
                                                            </Link>
                                                          ) : (
                                                            '—'
                                                          )}
                                                        </td>
                                                        <td
                                                          style={{ padding: '6px 8px', color: displayErr ? '#dc2626' : 'inherit', fontSize: '0.78rem', maxWidth: 240 }}
                                                          title={displayErr || ''}
                                                        >
                                                          {displayErr ? displayErr.slice(0, 200) : ''}
                                                        </td>
                                                      </tr>
                                                    );
                                                  })}
                                              </tbody>
                                            </table>
                                            {rawSteps.length === 0 && !stepsLoading && (
                                              <div style={{ color: '#6b7280', fontStyle: 'italic' }}>No steps found for this run.</div>
                                            )}
                                          </>
                                        );
                                      })()}
                                    </>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {runsTotal > WF_PAGE_SIZE && (
                <div style={{ padding: '0.65rem 1rem 1rem' }}>
                  <PaginationBar
                    page={runPage}
                    pageSize={WF_PAGE_SIZE}
                    totalItems={runsTotal}
                    onPageChange={setRunPage}
                    ariaLabel="Workflow runs pages"
                  />
                </div>
              )}
            </>
          )}
        </div>

      </section>
      <style>{`
        .wf-main-row:hover { background: rgba(99, 102, 241, 0.06) !important; }
      `}</style>
    </div>
  );
};

export default Workflow;
