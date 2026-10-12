/** Ports `dashboard.page_workflow_automation._pf` progress strings + metric counts (Streamlit parity). */

const ENV_BUSY_STATUSES = new Set(['running', 'pre_action_done', 'ds_created', 'ds_test_ok', 'scan_created', 'scan_running']);

export function workflowNormStepStatus(raw: unknown): string {
  if (raw == null || raw === undefined) return '';
  const s = String(raw).trim().toLowerCase();
  if (s === '' || s === 'nan' || s === 'nat' || s === 'none') return '';
  return s;
}

export function workflowIsTransientStepMessage(msg: string | null | undefined): boolean {
  const t = String(msg ?? '')
    .trim()
    .toLowerCase();
  if (!t) return false;
  const needles = [
    'will retry',
    'waiting for it to clear',
    'no free env this tick',
    'env already has a running scan',
    'env has a running scan',
    'reserved by another workflow run',
    'safety check failed',
    'stale: running',
  ];
  return needles.some((n) => t.includes(n));
}

export function workflowStepImpliesEnvBusy(rd: Record<string, unknown>, runStatus: string | null | undefined): boolean {
  const rs = String(runStatus ?? '')
    .trim()
    .toLowerCase();
  if (rs && ['stopped', 'completed', 'failed'].includes(rs)) return false;

  const c = workflowNormStepStatus(rd.status);
  if (c === 'disabled') return false;

  try {
    if (rd.is_enabled !== null && rd.is_enabled !== undefined) {
      const en = rd.is_enabled;
      if (typeof en === 'boolean' && !en) return false;
      if ((typeof en === 'number' || typeof en === 'string') && !Number(en)) return false;
    }
  } catch {
    /* ignore */
  }

  const dbBusy = Boolean(rd.env_is_busy);
  if (dbBusy && c !== 'completed') return true;
  if (ENV_BUSY_STATUSES.has(c)) return true;
  const sid = String(rd.scan_id ?? '').trim();
  if (sid && c !== 'completed' && c !== 'disabled') return true;
  return false;
}

export function computeProgressEmoji(rd: Record<string, unknown>): string {
  const s = workflowNormStepStatus(rd.status);

  if (s === 'disabled') return '🚫 Disabled';

  // Action steps don't have DS/Profile/Scan pipeline - they just execute API calls
  const dsKind = String(rd.ds_kind ?? '').trim().toLowerCase();
  if (dsKind === 'action') {
    const notes = String(rd.notes ?? '');
    const hasExecMarkers =
      /\[Executed on:/i.test(notes) && /\[Executed env i?ds?:/i.test(notes);
    const err = String(rd.last_error ?? '').trim();
    const staleNoDs = err.toLowerCase().includes('stale: running') && err.toLowerCase().includes('no ds');
    if (s === 'completed') return '✅ Action';
    if (s === 'error') return '❌ Action';
    // Notes show API ran but DB status stuck (stale reset / waiting on last env)
    if (hasExecMarkers && (s === 'pending' || s === 'running') && (staleNoDs || !err || workflowIsTransientStepMessage(err))) {
      return '✅ Action';
    }
    if (s === 'running' || s === 'pre_action_done') return '⏳ Action';
    return '➖ Action';
  }

  const dsId = String(rd.ds_id ?? '').trim();
  const spid = String(rd.scan_profile_id ?? '').trim();
  const sid = String(rd.scan_id ?? '').trim();

  const hasScan = Boolean(sid) || s === 'scan_running' || s === 'completed';
  let hasProfile = Boolean(spid) || hasScan || ['scan_created', 'scan_running', 'completed'].includes(s);
  // If a scan was created/started, connection test must have passed — show ✅Test regardless of status string.
  const hasTest = hasScan || ['ds_test_ok', 'scan_created', 'scan_running', 'completed'].includes(s);
  let hasDs =
    Boolean(dsId) ||
    ['ds_created', 'ds_test_ok', 'scan_created', 'scan_running', 'completed'].includes(s) ||
    hasProfile ||
    hasScan;

  if (s === 'pending' || s === '') {
    const err = String(rd.last_error ?? '').toLowerCase();
    if (err.includes('aborted/deleted') && String(rd.ds_name ?? '').trim()) {
      return '⏳ Scan (re-linking)';
    }
    return '➖ DS';
  }
  if (s === 'scan_waiting') return '⏸ Waiting (env/scan busy)';
  if (s === 'running' || s === 'pre_action_done') return '⏳ DS';

  if (s === 'error') {
    if (sid) return '✅DS ➔ ✅Test ➔ ✅Profile ➔ ⏳Scan ⚠️';
    const errText = String(rd.last_error ?? '').toLowerCase();
    if (errText.includes('scan start failed') || hasProfile) return '✅DS ➔ ✅Test ➔ ✅Profile ❌Scan';
    if (errText.includes('scan profile') || errText.includes('template')) return '✅DS ➔ ✅Test ❌Profile';
    if (errText.includes('connection test failed')) return '✅DS ❌Test';
    if (errText.includes('ds create failed') || !hasDs) return '❌ DS';
    if (hasDs) return '✅DS ❌Test';
    return '❌ Error';
  }

  const parts: string[] = [];
  if (hasDs) parts.push('✅DS');
  if (hasTest) parts.push('✅Test');
  if (hasProfile) parts.push('✅Profile');
  if (hasScan) {
    if (s === 'completed') parts.push('✅Scan');
    else parts.push('⏳Scan');
  }
  return parts.length ? parts.join(' ➔ ') : '➖ DS';
}

export interface StepMetrics {
  completed: number;
  errors: number;
  disabled: number;
  in_prog: number;
  pending: number;
}

export function computeStepMetrics(
  steps: Array<Record<string, unknown>>,
  totalSteps: number
): StepMetrics {
  let completed = 0;
  let errors = 0;
  let disabled = 0;
  let inProg = 0;

  for (const rd of steps) {
    const cStatus = workflowNormStepStatus(rd.status);
    const lastErr = String(rd.last_error ?? '').trim();
    const isAction = String(rd.ds_kind ?? '').trim().toLowerCase() === 'action';
    const actionNotes = String(rd.notes ?? '');
    const actionEffectivelyDone =
      isAction &&
      /\[Executed on:/i.test(actionNotes) &&
      /\[Executed env i?ds?:/i.test(actionNotes) &&
      (cStatus === 'pending' || cStatus === 'running') &&
      lastErr.toLowerCase().includes('stale: running');
    const scanStatusInconclusive =
      !isAction &&
      (cStatus === 'scan_running' || cStatus === 'pending') &&
      lastErr.toLowerCase().includes('status check inconclusive');
    const falseAbortedPending =
      !isAction &&
      cStatus === 'pending' &&
      lastErr.toLowerCase().includes('aborted/deleted') &&
      Boolean(String(rd.ds_name ?? '').trim());
    let isEn = true;
    try {
      if (rd.is_enabled !== null && rd.is_enabled !== undefined) {
        const v = rd.is_enabled;
        if (typeof v === 'boolean') isEn = v;
        else isEn = Boolean(Number(v));
      }
    } catch {
      isEn = true;
    }

    if (!isEn || cStatus === 'disabled') {
      disabled += 1;
    } else if (actionEffectivelyDone) {
      completed += 1;
    } else if (scanStatusInconclusive || falseAbortedPending) {
      inProg += 1;
    } else if (
      cStatus === 'error' ||
      (lastErr && cStatus !== 'completed' && cStatus !== 'disabled' && !workflowIsTransientStepMessage(lastErr))
    ) {
      errors += 1;
    } else if (cStatus === 'completed') {
      completed += 1;
    } else if (cStatus === '' || cStatus === 'pending' || cStatus === 'scan_waiting') {
      /* pending or parked waiting for env — not actively creating DS */
    } else {
      inProg += 1;
    }
  }

  const pending = totalSteps - completed - errors - disabled - inProg;
  return {
    completed,
    errors,
    disabled,
    in_prog: inProg,
    pending: Math.max(0, pending),
  };
}

export function flagsFromStep(rd: Record<string, unknown>): string {
  const bits: string[] = [];
  if (rd.is_acl) bits.push('ACL');
  if (rd.is_structured_ff) bits.push('STRUCT');
  if (rd.is_ols) bits.push('OLS');
  return bits.join(', ');
}

/** Running workflow with next_eligible_at still in the future (between-step cooldown). */
export function isRunInCooldown(run: {
  status?: string | null;
  next_eligible_at?: string | null;
}): boolean {
  return (
    String(run.status ?? '').trim().toLowerCase() === 'running' &&
    !!run.next_eligible_at &&
    new Date(run.next_eligible_at).getTime() > Date.now()
  );
}

export function envHasActiveWorkflowStep(
  envId: number,
  steps: Array<{ environment_id?: number | null; status?: string | null; is_enabled?: boolean; env_is_busy?: boolean; scan_id?: string | null }>,
  runStatus: string | null | undefined
): boolean {
  return steps.some(
    (st) =>
      st.environment_id === envId &&
      workflowStepImpliesEnvBusy(st as unknown as Record<string, unknown>, runStatus)
  );
}

/** Env is tied to a run in cooldown but has no in-flight DS/scan on that host. */
export function findEnvCooldownUntil(
  envId: number,
  runs: Array<{
    id: number;
    status?: string | null;
    next_eligible_at?: string | null;
    env1_id?: number | null;
    env2_id?: number | null;
  }>,
  stepsByRun: Record<string | number, Array<{ environment_id?: number | null; status?: string | null; is_enabled?: boolean; env_is_busy?: boolean; scan_id?: string | null }>>
): string | null {
  for (const run of runs) {
    if (!isRunInCooldown(run) || !run.next_eligible_at) continue;
    const steps = stepsByRun[run.id] ?? stepsByRun[String(run.id)] ?? [];
    if (envHasActiveWorkflowStep(envId, steps, run.status)) continue;
    const linked =
      run.env1_id === envId ||
      run.env2_id === envId ||
      steps.some((st) => st.environment_id === envId);
    if (linked) return run.next_eligible_at;
  }
  return null;
}
