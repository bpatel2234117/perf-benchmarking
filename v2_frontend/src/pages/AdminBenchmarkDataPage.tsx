import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { Database, Loader2, ChevronRight, ChevronDown, Layers, Download, Ban, RotateCcw } from 'lucide-react';
import { API_BASE } from '../apiConfig';

interface AdminBenchmarkDataPageProps { token: string; }

interface Run {
  id: number;
  source: string;
  build_version: string;
  major_build: string;
  data_source_type: string;
  file_set_type: string;
  scan_type: string;
  is_acl: boolean;
  is_feature_flags: boolean;
  scan_name: string | null;
  is_active: boolean;
  total_scan_hours: number | null;
  total_files: number | null;
  total_size_gb: number | null;
  throughput_cluster_gb_hr: number | null;
  throughput_cluster_files_hr: number | null;
  throughput_scanner_gb_hr: number | null;
  throughput_scanner_files_hr: number | null;
  throughput_thread_gb_hr: number | null;
  throughput_thread_files_hr: number | null;
}

function modeLabel(r: Run): string {
  const ols = /ols/i.test(r.scan_type || '');
  if (r.is_acl && ols) return 'ACL+OLS';
  if (r.is_acl) return 'ACL';
  if (ols) return 'OLS';
  return 'FULL';
}
function sizeLabel(fileSet: string): string {
  const u = (fileSet || '').toUpperCase();
  if (u.includes('CSV')) return 'CSV';
  if (u.includes('SMALL')) return 'Small';
  if (u.includes('LARGE')) return 'Large';
  if (u.includes('TABLE')) return 'Tables';
  return fileSet || '—';
}
function fmtDuration(hours: number | null): string {
  if (hours == null || !Number.isFinite(hours) || hours <= 0) return '—';
  const total = Math.round(hours * 3600);
  return `${Math.floor(total / 3600)}:${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}
function num(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}
function mean(rows: Run[], key: keyof Run): number | null {
  const vals = rows.map(r => r[key] as number | null).filter((v): v is number => v != null && Number.isFinite(v));
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}
function meanCsv(rows: Run[], key: keyof Run, digits: number): string {
  const m = mean(rows, key);
  return m == null ? '' : m.toFixed(digits);
}
function csvCell(v: string | number): string {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const MODE_COLOR: Record<string, string> = {
  FULL: '#2563eb', ACL: '#7c3aed', OLS: '#0891b2', 'ACL+OLS': '#db2777',
};

const MultiSelect: React.FC<{ label: string; options: string[]; selected: string[]; onChange: (v: string[]) => void }> = ({ label, options, selected, onChange }) => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLLabelElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);
  const toggle = (opt: string) => {
    onChange(selected.includes(opt) ? selected.filter(o => o !== opt) : [...selected, opt]);
  };
  const summary = selected.length === 0 ? 'All' : selected.length === 1 ? selected[0] : `${selected.length} selected`;
  return (
    <label ref={ref} style={{ fontSize: '0.8rem', fontWeight: 600, position: 'relative' }}>{label}
      <button type="button" onClick={() => setOpen(o => !o)}
        style={{ display: 'block', marginTop: 4, padding: '0.4rem 0.6rem', minWidth: 130, textAlign: 'left', borderRadius: 6, border: '1px solid var(--surface-border)', background: '#fff', cursor: 'pointer', fontWeight: 500 }}>
        {summary} <span style={{ float: 'right', color: '#94a3b8' }}>▾</span>
      </button>
      {open && (
        <div style={{ position: 'absolute', top: '100%', left: 0, marginTop: 2, zIndex: 11, background: '#fff', border: '1px solid var(--surface-border)', borderRadius: 8, boxShadow: '0 4px 14px rgba(0,0,0,0.12)', maxHeight: 260, overflowY: 'auto', minWidth: 170, padding: '4px 0' }}>
          {selected.length > 0 && (
            <div onClick={() => onChange([])} style={{ padding: '5px 12px', fontSize: '0.74rem', color: '#2563eb', cursor: 'pointer', fontWeight: 600 }}>Clear all</div>
          )}
          {options.map(opt => (
            <label key={opt} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 12px', fontSize: '0.82rem', fontWeight: 500, cursor: 'pointer', whiteSpace: 'nowrap' }}>
              <input type="checkbox" checked={selected.includes(opt)} onChange={() => toggle(opt)} />
              {opt}
            </label>
          ))}
        </div>
      )}
    </label>
  );
};

interface Scenario { key: string; ds: string; size: string; mode: string; runs: Run[]; }
interface BuildGroup { major: string; scenarios: Scenario[]; runCount: number; }

const AdminBenchmarkDataPage: React.FC<AdminBenchmarkDataPageProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const [major, setMajor] = useState('');
  const [ds, setDs] = useState<string[]>([]);
  const [mode, setMode] = useState<string[]>([]);
  const [size, setSize] = useState<string[]>([]);
  const [openBuilds, setOpenBuilds] = useState<Set<string>>(new Set());
  const [openScenarios, setOpenScenarios] = useState<Set<string>>(new Set());
  const [pageSize, setPageSize] = useState<number>(25);  // builds per page; 0 = All
  const [page, setPage] = useState(0);

  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ['adminBenchmarkRuns', token],
    queryFn: async () => {
      const res = await axios.get<{ rows: Run[] }>(`${API_BASE}/api/admin/benchmark-runs`, { ...authConfig, params: { limit: 5000, include_inactive: true } });
      return res.data.rows;
    },
  });

  // Toggle a run's is_active; the backend recomputes that major build's averages.
  const toggleActive = useMutation({
    mutationFn: async ({ id, active }: { id: number; active: boolean }) => {
      const res = await axios.post<{ ok: boolean; id: number; is_active: boolean }>(
        `${API_BASE}/api/admin/benchmark-runs/${id}/active`, { active }, authConfig);
      return res.data;
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['adminBenchmarkRuns', token] }); },
  });

  const rows = useMemo(() => data ?? [], [data]);
  const majors = useMemo(() => [...new Set(rows.map(r => r.major_build).filter(Boolean))].sort((a, b) => Number(b) - Number(a)), [rows]);
  const dsTypes = useMemo(() => [...new Set(rows.map(r => r.data_source_type).filter(Boolean))].sort(), [rows]);
  const modes = useMemo(() => [...new Set(rows.map(r => modeLabel(r)))].sort(), [rows]);
  const sizes = useMemo(() => [...new Set(rows.map(r => sizeLabel(r.file_set_type)))].sort(), [rows]);

  const builds: BuildGroup[] = useMemo(() => {
    const filtered = rows.filter(r =>
      (!major || r.major_build === major) &&
      (!ds.length || ds.includes(r.data_source_type)) &&
      (!mode.length || mode.includes(modeLabel(r))) &&
      (!size.length || size.includes(sizeLabel(r.file_set_type))));
    const byBuild = new Map<string, Run[]>();
    for (const r of filtered) {
      const arr = byBuild.get(r.major_build);
      if (arr) arr.push(r); else byBuild.set(r.major_build, [r]);
    }
    return [...byBuild.entries()].map(([m, rs]) => {
      const scen = new Map<string, Scenario>();
      for (const r of rs) {
        const size = sizeLabel(r.file_set_type), mode = modeLabel(r);
        const key = `${m}||${r.data_source_type}||${size}||${mode}`;
        const g = scen.get(key);
        if (g) g.runs.push(r); else scen.set(key, { key, ds: r.data_source_type, size, mode, runs: [r] });
      }
      const scenarios = [...scen.values()].sort((a, b) => a.ds.localeCompare(b.ds) || a.size.localeCompare(b.size) || a.mode.localeCompare(b.mode));
      return { major: m, scenarios, runCount: rs.filter(r => r.is_active).length };
    }).sort((a, b) => (Number(b.major) || 0) - (Number(a.major) || 0));
  }, [rows, major, ds, mode, size]);

  const exportCsv = () => {
    const headers = [
      'Build Number', 'DS Type', 'File Set', 'Scan Type',
      'Cluster GB/hr', 'Cluster Files/hr', 'Scanner GB/hr', 'Scanner Files/hr', 'Thread GB/hr', 'Thread Files/hr',
    ];
    const lines = [headers.join(',')];
    for (const b of builds) {
      for (const s of b.scenarios) {
        const active = s.runs.filter(r => r.is_active);
        lines.push([
          b.major, s.ds, s.size, s.mode,
          meanCsv(active, 'throughput_cluster_gb_hr', 2),
          meanCsv(active, 'throughput_cluster_files_hr', 0),
          meanCsv(active, 'throughput_scanner_gb_hr', 2),
          meanCsv(active, 'throughput_scanner_files_hr', 0),
          meanCsv(active, 'throughput_thread_gb_hr', 2),
          meanCsv(active, 'throughput_thread_files_hr', 0),
        ].map(csvCell).join(','));
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `benchmark-data-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };
  const scenarioCount = builds.reduce((n, b) => n + b.scenarios.length, 0);

  // Auto-open the newest build the first time data arrives / filters change.
  useEffect(() => {
    if (builds.length) setOpenBuilds(new Set([builds[0].major]));
    else setOpenBuilds(new Set());
    setOpenScenarios(new Set());
    setPage(0);
  }, [builds]);

  const effectiveSize = pageSize > 0 ? pageSize : builds.length || 1;
  const totalPages = Math.max(1, Math.ceil(builds.length / effectiveSize));
  const curPage = Math.min(page, totalPages - 1);
  const pagedBuilds = builds.slice(curPage * effectiveSize, curPage * effectiveSize + effectiveSize);

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, k: string) => {
    const n = new Set(set); n.has(k) ? n.delete(k) : n.add(k); setter(n);
  };

  const card: React.CSSProperties = { border: '1px solid var(--surface-border)', borderRadius: 12, overflow: 'hidden', marginBottom: 14, background: '#fff', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' };
  const buildHeader: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px', cursor: 'pointer', background: 'linear-gradient(90deg,#eef2ff,#fafaff)', borderBottom: '1px solid var(--surface-border)' };
  const chip = (bg: string, color: string): React.CSSProperties => ({ background: bg, color, fontSize: '0.72rem', fontWeight: 700, padding: '2px 9px', borderRadius: 999, whiteSpace: 'nowrap' });
  const statTile: React.CSSProperties = { flex: '1 1 0', minWidth: 92, padding: '8px 10px', borderRadius: 8, background: '#f8fafc', textAlign: 'center' };
  const statVal: React.CSSProperties = { fontSize: '1.05rem', fontWeight: 800, color: 'var(--primary-color)' };
  const statLbl: React.CSSProperties = { fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.04em', color: '#64748b', marginTop: 2 };

  return (
    <div style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem', maxWidth: 1280, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Database size={22} />
        <h2 style={{ margin: 0 }}>Benchmark Data</h2>
      </div>
      <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
        Collapsible by major build. Each scenario (data source · size · mode) shows the <strong>averaged</strong> throughput
        across its runs — the same averages used by the trend charts and reports. Expand a scenario to see the individual runs.
      </p>

      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label style={{ fontSize: '0.8rem', fontWeight: 600 }}>Major build
          <select value={major} onChange={e => setMajor(e.target.value)} style={{ display: 'block', marginTop: 4, padding: '0.4rem 0.6rem', borderRadius: 6, border: '1px solid var(--surface-border)' }}>
            <option value="">All</option>{majors.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <MultiSelect label="Data source" options={dsTypes} selected={ds} onChange={setDs} />
        <MultiSelect label="Scan type" options={modes} selected={mode} onChange={setMode} />
        <MultiSelect label="File set" options={sizes} selected={size} onChange={setSize} />
        <label style={{ fontSize: '0.8rem', fontWeight: 600 }}>Builds / page
          <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(0); }} style={{ display: 'block', marginTop: 4, padding: '0.4rem 0.6rem', borderRadius: 6, border: '1px solid var(--surface-border)' }}>
            {[10, 25, 50, 100].map(n => <option key={n} value={n}>{n}</option>)}
            <option value={0}>All</option>
          </select>
        </label>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => setOpenBuilds(new Set(builds.map(b => b.major)))} style={{ fontSize: '0.78rem', padding: '0.4rem 0.8rem' }}>Expand all</button>
          <button className="btn" onClick={() => setOpenBuilds(new Set())} style={{ fontSize: '0.78rem', padding: '0.4rem 0.8rem' }}>Collapse all</button>
          <button className="btn" onClick={exportCsv} disabled={!scenarioCount} title="Download the averaged scenario rows (respects active filters)" style={{ fontSize: '0.78rem', padding: '0.4rem 0.8rem', display: 'flex', alignItems: 'center', gap: 6 }}>
            <Download size={14} /> Download CSV{scenarioCount ? ` (${scenarioCount})` : ''}
          </button>
        </div>
      </div>

      {builds.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
          <span>Builds {curPage * effectiveSize + 1}–{Math.min((curPage + 1) * effectiveSize, builds.length)} of {builds.length}</span>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
            <button className="btn" disabled={curPage <= 0} onClick={() => setPage(curPage - 1)} style={{ fontSize: '0.78rem', padding: '0.35rem 0.7rem' }}>‹ Prev</button>
            <span style={{ alignSelf: 'center' }}>Page {curPage + 1} / {totalPages}</span>
            <button className="btn" disabled={curPage >= totalPages - 1} onClick={() => setPage(curPage + 1)} style={{ fontSize: '0.78rem', padding: '0.35rem 0.7rem' }}>Next ›</button>
          </div>
        </div>
      )}

      {isLoading && <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}><Loader2 className="lucide-spin" size={18} /> Loading…</div>}
      {error && <div style={{ color: '#ef4444' }}>Failed to load benchmark data.</div>}
      {!isLoading && !builds.length && <div style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-secondary)' }}>No benchmark runs match the filters.</div>}

      {pagedBuilds.map(b => {
        const open = openBuilds.has(b.major);
        return (
          <div key={b.major} style={card}>
            <div style={buildHeader} onClick={() => toggle(openBuilds, setOpenBuilds, b.major)}>
              {open ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
              <Layers size={18} style={{ color: 'var(--primary-color)' }} />
              <span style={{ fontSize: '1.15rem', fontWeight: 800 }}>Build {b.major}</span>
              <span style={chip('#e0e7ff', '#3730a3')}>{b.scenarios.length} scenarios</span>
              <span style={chip('#f1f5f9', '#475569')}>{b.runCount} runs</span>
            </div>
            {open && (
              <div style={{ padding: '10px 14px 16px' }}>
                {b.scenarios.map(s => {
                  const so = openScenarios.has(s.key);
                  const activeRuns = s.runs.filter(r => r.is_active);
                  const inactiveCount = s.runs.length - activeRuns.length;
                  return (
                    <div key={s.key} style={{ border: '1px solid #eef1f6', borderRadius: 10, marginTop: 10 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', cursor: 'pointer', flexWrap: 'wrap' }}
                           onClick={() => toggle(openScenarios, setOpenScenarios, s.key)}>
                        {so ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                        <span style={chip('#dbeafe', '#1e40af')}>{s.ds}</span>
                        <span style={chip('#dcfce7', '#166534')}>{s.size}</span>
                        <span style={chip((MODE_COLOR[s.mode] || '#475569') + '22', MODE_COLOR[s.mode] || '#475569')}>{s.mode}</span>
                        <span style={{ fontSize: '0.74rem', color: '#64748b' }}>
                          avg of {activeRuns.length} run{activeRuns.length === 1 ? '' : 's'}
                          {inactiveCount > 0 && (
                            <span style={{ color: '#b45309', fontWeight: 600 }}> · {inactiveCount} excluded</span>
                          )}
                        </span>
                        <div style={{ flex: '1 1 100%', display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_cluster_gb_hr'))}</div><div style={statLbl}>Cluster GB/hr</div></div>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_cluster_files_hr'), 0)}</div><div style={statLbl}>Cluster Files/hr</div></div>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_scanner_gb_hr'))}</div><div style={statLbl}>Scanner GB/hr</div></div>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_scanner_files_hr'), 0)}</div><div style={statLbl}>Scanner Files/hr</div></div>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_thread_gb_hr'))}</div><div style={statLbl}>Thread GB/hr</div></div>
                          <div style={statTile}><div style={statVal}>{num(mean(activeRuns, 'throughput_thread_files_hr'), 0)}</div><div style={statLbl}>Thread Files/hr</div></div>
                        </div>
                      </div>
                      {so && (
                        <div style={{ overflowX: 'auto', overflowY: 'auto', maxHeight: 360, borderTop: '1px solid #eef1f6' }}>
                          <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: '0.8rem', minWidth: 900 }}>
                            <thead><tr style={{ background: '#fafbfc' }}>
                              {['Build', 'Scan Name', 'Duration', 'Files', 'GB', 'Cluster GB/hr', 'Cluster Files/hr', 'Scanner GB/hr', 'Thread GB/hr', 'Active'].map((h, i) => (
                                <th key={h} style={{ padding: '6px 10px', textAlign: i < 2 ? 'left' : (h === 'Active' ? 'center' : 'right'), color: '#64748b', fontSize: '0.68rem', textTransform: 'uppercase', borderBottom: '1px solid #eef1f6', whiteSpace: 'nowrap', position: 'sticky', top: 0, background: '#fafbfc', zIndex: 1 }}>{h}</th>
                              ))}
                            </tr></thead>
                            <tbody>
                              {s.runs.map(r => {
                                const td: React.CSSProperties = { padding: '5px 10px', borderBottom: '1px solid #f4f6f9', opacity: r.is_active ? 1 : 0.5 };
                                const busy = toggleActive.isPending && toggleActive.variables?.id === r.id;
                                return (
                                <tr key={r.id} style={{ background: r.is_active ? undefined : '#fdf6ec' }}>
                                  <td style={td}>{r.build_version}</td>
                                  <td style={{ ...td, textDecoration: r.is_active ? undefined : 'line-through' }} title={r.scan_name || ''}>{r.scan_name || '—'}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{fmtDuration(r.total_scan_hours)}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{r.total_files != null ? r.total_files.toLocaleString() : '—'}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{num(r.total_size_gb, 1)}</td>
                                  <td style={{ ...td, textAlign: 'right', fontWeight: 700, color: 'var(--primary-color)' }}>{num(r.throughput_cluster_gb_hr)}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{num(r.throughput_cluster_files_hr, 0)}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{num(r.throughput_scanner_gb_hr)}</td>
                                  <td style={{ ...td, textAlign: 'right' }}>{num(r.throughput_thread_gb_hr)}</td>
                                  <td style={{ ...td, opacity: 1, textAlign: 'center' }}>
                                    <button
                                      type="button"
                                      title={r.is_active ? 'Exclude this run from throughput averages' : 'Include this run in throughput averages'}
                                      disabled={busy}
                                      onClick={() => toggleActive.mutate({ id: r.id, active: !r.is_active })}
                                      style={{
                                        display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 9px', borderRadius: 6,
                                        border: `1px solid ${r.is_active ? '#fca5a5' : '#86efac'}`,
                                        background: r.is_active ? '#fef2f2' : '#f0fdf4',
                                        color: r.is_active ? '#b91c1c' : '#15803d',
                                        fontSize: '0.72rem', fontWeight: 700, cursor: busy ? 'wait' : 'pointer', whiteSpace: 'nowrap',
                                      }}
                                    >
                                      {busy ? <Loader2 size={12} className="lucide-spin" /> : r.is_active ? <Ban size={12} /> : <RotateCcw size={12} />}
                                      {r.is_active ? 'Deactivate' : 'Reactivate'}
                                    </button>
                                  </td>
                                </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

export default AdminBenchmarkDataPage;
