import React, { useEffect, useMemo, useState } from 'react';
import { Loader2, GitBranch, ChevronDown, ChevronRight } from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  computeProgressEmoji,
  computeStepMetrics,
  flagsFromStep,
  workflowIsTransientStepMessage,
  workflowNormStepStatus,
  workflowStepImpliesEnvBusy,
} from '../utils/workflowProgress';

const API = 'http://localhost:8000';

interface WorkflowProps {
  token: string;
}

export interface WorkflowRun {
  id: number;
  name: string | null;
  status: string | null;
  env1_id: number | null;
  env2_id: number | null;
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

/** First contiguous block of planner `action` rows (e.g. ENABLE OLS FEATURE FLAGS). */
function findConsecutiveActionBlock(
  catalog: StepCatalogRow[]
): { start: number; end: number } | null {
  const start = catalog.findIndex((r) => String(r.ds_kind || '').trim().toLowerCase() === 'action');
  if (start < 0) return null;
  let end = start;
  while (
    end + 1 < catalog.length &&
    String(catalog[end + 1].ds_kind || '').trim().toLowerCase() === 'action'
  ) {
    end++;
  }
  return { start, end };
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

function utcLabel(): string {
  return `Workflow ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC`;
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

const Workflow: React.FC<WorkflowProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const queryClient = useQueryClient();
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [createName, setCreateName] = useState('');
  const [createCooldown, setCreateCooldown] = useState(15);
  const [createEnv1, setCreateEnv1] = useState<string>('');
  const [createEnv2, setCreateEnv2] = useState<string>('');
  const [stepEnabled, setStepEnabled] = useState<boolean[]>([]);

  const {
    data: stepCatalogResp,
    isLoading: catalogLoading,
    isError: catalogError,
  } = useQuery({
    queryKey: ['workflowStepCatalog', token],
    queryFn: async () => {
      const res = await axios.get(`${API}/api/workflow/step-catalog`, authConfig);
      return res.data as { steps: StepCatalogRow[] };
    },
  });

  useEffect(() => {
    const steps = stepCatalogResp?.steps;
    if (!steps?.length) return;
    setStepEnabled((prev) => {
      if (prev.length === steps.length) return prev;
      return steps.map((s) => s.is_enabled !== false);
    });
  }, [stepCatalogResp]);

  const enabledStepCount = useMemo(() => stepEnabled.filter(Boolean).length, [stepEnabled]);

  const { data: runs = [], isLoading: runsLoading, error: runsError } = useQuery({
    queryKey: ['workflowRuns', token],
    queryFn: async () => {
      const res = await axios.get(`${API}/api/workflow/runs`, { ...authConfig, params: { limit: 10000 } });
      return res.data as WorkflowRun[];
    },
    refetchInterval: 8000,
  });

  const runIdsCsv = useMemo(() => runs.map((r) => r.id).join(','), [runs]);

  const { data: stepsByRun = {}, isLoading: stepsLoading } = useQuery({
    queryKey: ['workflowStepsBatch', token, runIdsCsv],
    queryFn: async () => {
      if (!runIdsCsv) return {};
      const res = await axios.get(`${API}/api/workflow/steps/batch`, {
        ...authConfig,
        params: { run_ids: runIdsCsv },
      });
      return res.data as Record<string, WorkflowStepRow[]>;
    },
    enabled: runIdsCsv.length > 0,
    refetchInterval: 5000,
  });

  const { data: envChips = [] } = useQuery({
    queryKey: ['workflowEnvStatus', token],
    queryFn: async () => {
      const res = await axios.get(`${API}/api/workflow/env-status`, authConfig);
      return res.data as EnvChip[];
    },
    refetchInterval: 8000,
  });

  const { data: environments = [] } = useQuery({
    queryKey: ['environments', token],
    queryFn: async () => {
      const res = await axios.get(`${API}/api/environments`, authConfig);
      return res.data as EnvironmentRow[];
    },
  });

  const activeEnvOptions = useMemo(
    () => environments.filter((e) => e.is_active).sort((a, b) => a.env_name.localeCompare(b.env_name)),
    [environments]
  );

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
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        name: (createName || '').trim() || utcLabel(),
        cooldown_minutes: createCooldown,
      };
      const e1 = parseInt(createEnv1, 10);
      const e2 = parseInt(createEnv2, 10);
      if (!Number.isNaN(e1)) body.env1_id = e1;
      if (!Number.isNaN(e2)) body.env2_id = e2;
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
      const res = await axios.post(`${API}/api/workflow/runs`, body, authConfig);
      return res.data as { id: number };
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['workflowRuns', token] });
      queryClient.invalidateQueries({ queryKey: ['workflowStepsBatch'] });
      if (data?.id) setExpandedId(data.id);
      setCreateName('');
    },
  });

  const controlMutation = useMutation({
    mutationFn: async (vars: { runId: number; action: 'start' | 'pause' | 'resume' | 'stop' }) => {
      await axios.post(`${API}/api/workflow/runs/${vars.runId}/control`, { action: vars.action }, authConfig);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflowRuns', token] });
      queryClient.invalidateQueries({ queryKey: ['workflowStepsBatch'] });
      queryClient.invalidateQueries({ queryKey: ['workflowEnvStatus', token] });
    },
  });

  const toggleExpand = (id: number) => {
    setExpandedId((cur) => (cur === id ? null : id));
  };

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <section>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.75rem', marginBottom: '0.5rem' }}>
          <span style={{ fontSize: '1.75rem', lineHeight: 1 }} aria-hidden>
            🧪
          </span>
          <div>
            <h2 style={{ color: 'var(--text-primary)', fontSize: '1.5rem', fontWeight: 700, margin: 0 }}>Workflow Automation</h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: '0.35rem', lineHeight: 1.55, maxWidth: '52rem' }}>
              Workflow data is PostgreSQL only (shared v2 DB, or <code style={{ fontSize: '0.85em' }}>WORKFLOW_DB_URI</code> to point at another Postgres instance).
              Runs use pooled test environments where configured; concurrency is capped by{' '}
              <code style={{ fontSize: '0.85em' }}>WORKFLOW_MAX_CONCURRENT_SCANS</code>.
            </p>
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
                placeholder={utcLabel()}
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
                onChange={(e) => setCreateCooldown(Number(e.target.value) || 15)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              />
            </div>
            <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
              <label htmlFor="wf-e1">Env 1 (optional)</label>
              <select
                id="wf-e1"
                value={createEnv1}
                onChange={(e) => setCreateEnv1(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              >
                <option value="">—</option>
                {activeEnvOptions.map((env) => (
                  <option key={env.id} value={String(env.id)}>
                    {env.env_name}
                  </option>
                ))}
              </select>
            </div>
            <div className="input-group" style={{ margin: 0, minWidth: 160 }}>
              <label htmlFor="wf-e2">Env 2 (optional)</label>
              <select
                id="wf-e2"
                value={createEnv2}
                onChange={(e) => setCreateEnv2(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid var(--surface-border)', fontFamily: 'inherit', width: '100%' }}
              >
                <option value="">—</option>
                {activeEnvOptions.map((env) => (
                  <option key={env.id} value={String(env.id)}>
                    {env.env_name}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="btn"
              disabled={
                createMutation.isPending ||
                createBlockedByCatalog ||
                createBlockedUntilSynced ||
                createBlockedBySteps
              }
              onClick={() => createMutation.mutate()}
              style={{ fontWeight: 600 }}
            >
              {createMutation.isPending ? 'Creating…' : 'Create workflow'}
            </button>
          </div>

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
                <span style={{ fontWeight: 600, fontSize: '0.88rem', color: 'var(--text-primary)' }}>
                  Steps to include ({enabledStepCount}/{stepCatalogResp.steps.length})
                </span>
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
                    gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
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
              {createBlockedBySteps && (
                <p style={{ fontSize: '0.76rem', color: 'var(--danger-color)', marginTop: '0.45rem', marginBottom: 0 }}>
                  Select at least one step.
                </p>
              )}
            </div>
          ) : null}

          <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginTop: '0.75rem', marginBottom: 0 }}>
            Same default planner matrix as Streamlit when all steps stay enabled (or omit catalog on error — server defaults).
          </p>
        </div>

        {runsError && (
          <div className="glass-panel" style={{ padding: '1rem', color: 'var(--danger-color)', marginBottom: '1rem' }}>
            Workflow API error. {(runsError as Error).message}
          </div>
        )}

        <h3 style={{ fontSize: '1.1rem', fontWeight: 700, margin: '0.75rem 0 0.35rem', color: 'var(--text-primary)' }}>Workflow Runs</h3>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.82rem', marginBottom: '0.75rem', maxWidth: '52rem', lineHeight: 1.45 }}>
          Expand a run for steps and throughput-style metrics. This UI reads{' '}
          <strong style={{ fontWeight: 600, color: 'var(--text-primary)' }}>PostgreSQL only</strong>; runs you created in legacy Streamlit/dashboard against MySQL do not appear until you copy them into the same Postgres your v2 API uses (run{' '}
          <code style={{ fontSize: '0.85em' }}>python3 scripts/migrate_workflow_mysql_to_postgres.py</code>
          , see script header for env vars).
        </p>

        {envChips.length > 0 && (
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '10px', lineHeight: 1.6 }}>
            <strong style={{ color: 'var(--text-primary)' }}>Env status:</strong>{' '}
            {envChips.map((e) => (
              <span key={e.id} style={{ marginRight: '0.75rem', whiteSpace: 'nowrap' }}>
                {e.busy ? '🔴' : '🟢'} {e.env_name} ({e.busy ? 'busy' : 'free'})
              </span>
            ))}
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
            <div style={{ overflowX: 'auto' }}>
              <table className="workflow-automation-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.88rem' }}>
                <thead>
                  <tr style={{ background: 'var(--primary-color, #0052cc)', color: '#fff' }}>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Run</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Name</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Status</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Started</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Finished</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Last Error</th>
                    <th style={{ padding: '10px 12px', textAlign: 'left', fontWeight: 600 }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => {
                    const isOpen = expandedId === run.id;
                    const rawSteps = stepsByRun[String(run.id)] ?? stepsByRun[run.id] ?? [];
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
                          <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>{fmtCell(run.started_at)}</td>
                          <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>{fmtCell(run.finished_at)}</td>
                          <td style={{ padding: '10px 12px', maxWidth: 200, color: 'var(--danger-color)', fontSize: '0.82rem' }}>
                            {fmtCell(run.last_error)}
                          </td>
                          <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                            <span style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
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
                                    onClick={() => controlMutation.mutate({ runId: run.id, action: act })}
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
                            </span>
                          </td>
                        </tr>
                        {isOpen && (
                          <tr>
                            <td colSpan={7} style={{ padding: 0, border: 'none', background: 'var(--surface-bg, #faf8ff)' }}>
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
                                {stepsLoading && !rawSteps.length ? (
                                  <Loader2 className="lucide-spin" size={22} />
                                ) : (
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
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>DS Type</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>DS Name</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Scan</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>File Set</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Flags</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Progress</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Scan Id</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Notes</th>
                                          <th style={{ padding: '6px 8px', textAlign: 'left' }}>Error</th>
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
                                              ['scan_waiting', 'scan_running', 'scan_created', 'ds_created', 'ds_test_ok'].includes(cStatus) &&
                                              workflowIsTransientStepMessage(displayErr)
                                            ) {
                                              displayErr = '';
                                            }
                                            if (displayErr && displayErr.includes('No free env this tick')) displayErr = '';

                                            const name = String(st.environment_name ?? '').trim();
                                            const busy = workflowStepImpliesEnvBusy(rd, runStatus);
                                            const envDisplay = name ? `${busy ? '🔴' : '🟢'} ${name}` : '—';

                                            return (
                                              <tr key={st.id} style={{ background: st.seq % 2 === 0 ? '#f9f8ff' : '#ffffff' }}>
                                                <td style={{ padding: '6px 8px', textAlign: 'center', fontWeight: 600 }}>{st.seq}</td>
                                                <td style={{ padding: '6px 8px' }}>{envDisplay}</td>
                                                <td style={{ padding: '6px 8px' }}>{fmtCell(st.ds_kind)}</td>
                                                <td style={{ padding: '6px 8px', fontWeight: 500 }}>{fmtCell(st.ds_name)}</td>
                                                <td style={{ padding: '6px 8px' }}>{fmtCell(st.scan_kind)}</td>
                                                <td style={{ padding: '6px 8px' }}>{fmtCell(st.file_set_type)}</td>
                                                <td style={{ padding: '6px 8px', fontSize: '0.8rem' }}>{flagsFromStep(rd)}</td>
                                                <td style={{ padding: '6px 8px', fontSize: '0.82rem', whiteSpace: 'nowrap' }}>
                                                  {computeProgressEmoji(rd)}
                                                </td>
                                                <td
                                                  style={{
                                                    padding: '6px 8px',
                                                    fontFamily: 'ui-monospace, monospace',
                                                    fontSize: '0.78rem',
                                                  }}
                                                >
                                                  {fmtCell(st.scan_id)}
                                                </td>
                                                <td style={{ padding: '6px 8px', fontSize: '0.78rem', maxWidth: 140 }}>
                                                  {fmtCell(st.notes)}
                                                </td>
                                                <td
                                                  style={{ padding: '6px 8px', color: '#dc2626', fontSize: '0.78rem', maxWidth: 200 }}
                                                  title={st.last_error ?? ''}
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
          )}
        </div>

        <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', maxWidth: '48rem', lineHeight: 1.45 }}>
          <GitBranch size={14} style={{ verticalAlign: 'middle', display: 'inline' }} /> Orchestrator:{' '}
          <code style={{ fontSize: '0.85em' }}>workflow_runner.py</code>
        </p>
      </section>
      <style>{`
        .wf-main-row:hover { background: rgba(99, 102, 241, 0.06) !important; }
      `}</style>
    </div>
  );
};

export default Workflow;
