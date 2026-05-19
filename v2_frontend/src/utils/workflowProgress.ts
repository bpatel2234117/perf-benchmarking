/** Ports `dashboard.page_workflow_automation._pf` progress strings + metric counts (Streamlit parity). */

const ENV_BUSY_STATUSES = new Set(['running', 'pre_action_done', 'ds_created', 'ds_test_ok', 'scan_created', 'scan_running', 'scan_waiting']);

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
  if (dbBusy) return true;
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
    if (s === 'completed') return '✅ Action';
    if (s === 'error') return '❌ Action';
    if (s === 'running' || s === 'pre_action_done') return '⏳ Action';
    return '➖ Action';
  }

  const dsId = String(rd.ds_id ?? '').trim();
  const spid = String(rd.scan_profile_id ?? '').trim();
  const sid = String(rd.scan_id ?? '').trim();

  const hasScan = Boolean(sid) || s === 'scan_running' || s === 'completed';
  let hasProfile = Boolean(spid) || hasScan || ['scan_created', 'scan_running', 'completed'].includes(s);
  const hasTest = ['ds_test_ok', 'scan_created', 'scan_running', 'completed'].includes(s);
  let hasDs =
    Boolean(dsId) ||
    ['ds_created', 'ds_test_ok', 'scan_created', 'scan_running', 'completed'].includes(s) ||
    hasProfile ||
    hasScan;

  if (s === 'pending' || s === '') return '➖ DS';
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
    } else if (
      cStatus === 'error' ||
      (lastErr && cStatus !== 'completed' && cStatus !== 'disabled' && !workflowIsTransientStepMessage(lastErr))
    ) {
      errors += 1;
    } else if (cStatus === 'completed') {
      completed += 1;
    } else if (cStatus === '' || cStatus === 'pending') {
      /* residual pending */
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
