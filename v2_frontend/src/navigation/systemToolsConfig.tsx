/**
 * Entries for System Tools flyout (legacy Streamlit tools + routed React screens).
 */
import type { LucideIcon } from 'lucide-react';
import {
  Globe,
  GitCompare,
  GitBranch,
  LayoutDashboard,
  Layers,
  Monitor,
  Puzzle,
  PlusSquare,
  Satellite,
  ShieldCheck,
  TerminalSquare,
  TrendingUp,
  Wrench,
} from 'lucide-react';

export type AppNavPath =
  | '/'
  | '/environments'
  | '/logs';

export type SystemToolEntry =
  | { kind: 'app'; path: AppNavPath; label: string; icon: LucideIcon }
  | { kind: 'legacy'; slug: string; label: string; icon: LucideIcon };

/** System Tools flyout (Workflow lives as a top-level sidebar item, not here). */
export const SYSTEM_TOOLS_ENTRIES: readonly SystemToolEntry[] = [
  { kind: 'app', path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { kind: 'legacy', slug: 'run-comparison', label: 'Run Comparison Tool', icon: GitCompare },
  { kind: 'legacy', slug: 'classifier-comparison', label: 'Classifier Comparison', icon: Layers },
  { kind: 'legacy', slug: 'data-record-add', label: 'Add New Data Record', icon: PlusSquare },
  { kind: 'legacy', slug: 'records-edit', label: 'Edit Existing Records', icon: Wrench },
  { kind: 'app', path: '/environments', label: 'Manage Test Environments', icon: Globe },
  { kind: 'legacy', slug: 'version-comparison', label: 'Version Comparison', icon: GitBranch },
  { kind: 'legacy', slug: 'data-source-create', label: 'Create Data Source', icon: Puzzle },
  { kind: 'legacy', slug: 'scan-status', label: 'Scan Status', icon: Satellite },
  { kind: 'legacy', slug: 'scan-progress', label: 'Scan Progress', icon: TrendingUp },
  { kind: 'legacy', slug: 'scanner-infra', label: 'Scanner Infrastructure', icon: Monitor },
  { kind: 'legacy', slug: 'manage-rbac', label: 'Manage RBAC', icon: ShieldCheck },
  { kind: 'app', path: '/logs', label: 'AI Logs', icon: TerminalSquare },
];

export function titleForSlug(slug: string): string {
  const row = SYSTEM_TOOLS_ENTRIES.find((e): e is Extract<SystemToolEntry, { kind: 'legacy' }> => e.kind === 'legacy' && e.slug === slug);
  return row?.label ?? slug;
}
