import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, Download, Eye, RefreshCw, Mail, AlertCircle, FileText, Settings, ChevronDown, ChevronRight, CheckCircle2, XCircle } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import PageHeader from '../components/PageHeader';

interface GenerateBenchmarkReportPageProps {
    token: string;
}

interface BuildOption {
    version: string;
    count: number;
}

interface ReportItem {
    name: string;
    url: string;
    build?: string;
    size_bytes: number;
    modified_at: number;
}

function formatBytes(bytes: number): string {
    if (!Number.isFinite(bytes)) return '—';
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
}

function formatModified(epochSeconds: number): string {
    if (!Number.isFinite(epochSeconds)) return '—';
    return new Date(epochSeconds * 1000).toLocaleString();
}

// Styled toggle checkbox row
const ToggleOption: React.FC<{
    checked: boolean;
    onChange: (v: boolean) => void;
    label: string;
    desc?: string;
    flag?: string;
}> = ({ checked, onChange, label, desc, flag }) => (
    <label
        style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.75rem',
            padding: '0.75rem 1rem',
            borderRadius: '8px',
            background: checked ? 'rgba(139, 92, 246, 0.08)' : 'var(--bg-secondary)',
            border: `1.5px solid ${checked ? '#8B5CF6' : 'var(--border-color)'}`,
            cursor: 'pointer',
            transition: 'all 0.15s ease',
        }}
    >
        <div
            style={{
                width: 38,
                height: 22,
                borderRadius: 11,
                background: checked ? '#8B5CF6' : 'var(--border-color)',
                position: 'relative',
                flexShrink: 0,
                transition: 'background 0.15s ease',
            }}
        >
            <div
                style={{
                    position: 'absolute',
                    top: 3,
                    left: checked ? 19 : 3,
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    background: 'white',
                    transition: 'left 0.15s ease',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                }}
            />
        </div>
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} style={{ display: 'none' }} />
        <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 500, fontSize: '0.9rem', color: 'var(--text-primary)' }}>{label}</div>
            {desc && <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginTop: '0.1rem' }}>{desc}</div>}
        </div>
        {flag && (
            <code style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', background: 'var(--bg-secondary)', padding: '2px 6px', borderRadius: 4, border: '1px solid var(--border-color)', flexShrink: 0 }}>
                {flag}
            </code>
        )}
    </label>
);

const UNSTRUCTURED_DS_OPTIONS = ['S3', 'SMB'] as const;
const STRUCTURED_DS_OPTIONS = ['MSSQL', 'Postgres'] as const;

const DsCategoryPicker: React.FC<{
    enabled: boolean;
    onEnabledChange: (v: boolean) => void;
    label: string;
    desc: string;
    options: readonly string[];
    selected: Set<string>;
    onToggleDs: (ds: string) => void;
    onSelectAll: () => void;
}> = ({ enabled, onEnabledChange, label, desc, options, selected, onToggleDs, onSelectAll }) => (
    <div
        style={{
            padding: '0.85rem 1rem',
            borderRadius: 8,
            border: `1.5px solid ${enabled ? '#8B5CF6' : 'var(--border-color)'}`,
            background: enabled ? 'rgba(139, 92, 246, 0.06)' : 'var(--bg-secondary)',
        }}
    >
        <ToggleOption
            checked={enabled}
            onChange={(v) => {
                onEnabledChange(v);
                if (v && selected.size === 0) onSelectAll();
            }}
            label={label}
            desc={desc}
        />
        {enabled && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', marginTop: '0.75rem', paddingLeft: '0.25rem' }}>
                {options.map((ds) => {
                    const on = selected.has(ds);
                    return (
                        <label
                            key={ds}
                            style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '0.35rem',
                                padding: '0.35rem 0.65rem',
                                borderRadius: 6,
                                border: `1px solid ${on ? '#8B5CF6' : 'var(--border-color)'}`,
                                background: on ? 'rgba(139, 92, 246, 0.12)' : 'var(--surface-ground, #fff)',
                                cursor: 'pointer',
                                fontSize: '0.82rem',
                                fontWeight: on ? 600 : 400,
                            }}
                        >
                            <input
                                type="checkbox"
                                checked={on}
                                onChange={() => onToggleDs(ds)}
                                style={{ accentColor: '#8B5CF6' }}
                            />
                            {ds}
                        </label>
                    );
                })}
                <button
                    type="button"
                    onClick={onSelectAll}
                    style={{
                        fontSize: '0.75rem',
                        padding: '0.35rem 0.55rem',
                        borderRadius: 6,
                        border: '1px solid var(--border-color)',
                        background: 'transparent',
                        cursor: 'pointer',
                        color: 'var(--text-secondary)',
                    }}
                >
                    Select all
                </button>
            </div>
        )}
    </div>
);

const GenerateBenchmarkReportPage: React.FC<GenerateBenchmarkReportPageProps> = ({ token }) => {
    const authConfig = { headers: { Authorization: `Bearer ${token}` } };
    const queryClient = useQueryClient();

    const [activeTab, setActiveTab] = useState<'generate' | 'view'>('generate');
    const [previewReport, setPreviewReport] = useState<{ name: string; url: string; blobUrl?: string } | null>(null);
    const [reportActionError, setReportActionError] = useState<string | null>(null);
    const [currentBuild, setCurrentBuild] = useState<string>('');
    const [previousBuild, setPreviousBuild] = useState<string>('');
    const [environmentId, setEnvironmentId] = useState<number>(0);
    const [includeUnstructured, setIncludeUnstructured] = useState(true);
    const [includeStructured, setIncludeStructured] = useState(true);
    const [unstructuredSelected, setUnstructuredSelected] = useState<Set<string>>(
        () => new Set(UNSTRUCTURED_DS_OPTIONS)
    );
    const [structuredSelected, setStructuredSelected] = useState<Set<string>>(
        () => new Set(STRUCTURED_DS_OPTIONS)
    );
    const [includeScannerPerf, setIncludeScannerPerf] = useState(false);
    const [scannerScope, setScannerScope] = useState<'last' | 'all'>('last');
    const [includeErrors, setIncludeErrors] = useState(false);
    const [includeTestEnv] = useState(true);
    const [testEnvExpanded, setTestEnvExpanded] = useState(true);
    const [testEnvDesc, setTestEnvDesc] = useState('Two large-scale environments');
    const [testEnvScanners, setTestEnvScanners] = useState(5);
    const [testEnvThreadsPerScanner, setTestEnvThreadsPerScanner] = useState(10);
    const [testEnvScanThreads, setTestEnvScanThreads] = useState(50);
    const [testEnvDsLite, setTestEnvDsLite] = useState('True');
    const [testEnvScannerJobs, setTestEnvScannerJobs] = useState('false');
    const [testEnvFileCount, setTestEnvFileCount] = useState('1 M');
    const [testEnvFileType, setTestEnvFileType] = useState('txt');

    const { data: userProfile, isLoading: userLoading } = useQuery({
        queryKey: ['me'],
        queryFn: async () => {
            const res = await axios.get(`${API_BASE}/api/me`, { headers: { Authorization: `Bearer ${token}` } });
            return res.data;
        }
    });

    const { data: buildsData, isLoading: buildsLoading } = useQuery({
        queryKey: ['availableBuilds', token],
        queryFn: async () => {
            const res = await axios.get<{ builds: BuildOption[] }>(`${API_BASE}/api/benchmark/builds`, authConfig);
            return res.data;
        },
    });

    const { data: reportsData, isLoading: reportsLoading, refetch: refetchReports } = useQuery({
        queryKey: ['generatedReports', token],
        queryFn: async () => {
            const res = await axios.get<{ items: ReportItem[] }>(`${API_BASE}/api/reports`, authConfig);
            return res.data;
        },
    });

    const generateReportMutation = useMutation({
        mutationFn: async () => {
            const params = new URLSearchParams();
            if (currentBuild) params.append('current_build', currentBuild);
            if (previousBuild) params.append('previous_build', previousBuild);
            if (environmentId > 0) params.append('environment_id', environmentId.toString());
            const unList = includeUnstructured
                ? UNSTRUCTURED_DS_OPTIONS.filter((d) => unstructuredSelected.has(d))
                : [];
            const stList = includeStructured
                ? STRUCTURED_DS_OPTIONS.filter((d) => structuredSelected.has(d))
                : [];
            params.append('unstructured_ds', unList.join(','));
            params.append('structured_ds', stList.join(','));
            if (includeScannerPerf) {
                params.append('scanner_performance', 'true');
                params.append('scanner_scope', scannerScope);
            }
            if (includeErrors) params.append('errors', 'true');
            if (includeTestEnv) {
                params.append('test_env', 'true');
                params.append('test_env_environments', testEnvDesc);
                params.append('test_env_scanners', testEnvScanners.toString());
                params.append('test_env_threads_per_scanner', testEnvThreadsPerScanner.toString());
                params.append('test_env_scan_setting_threads', testEnvScanThreads.toString());
                params.append('test_env_ds_lite_mode', testEnvDsLite);
                params.append('test_env_scanner_jobs_thresholds', testEnvScannerJobs);
                params.append('test_env_file_count', testEnvFileCount);
                params.append('test_env_file_type', testEnvFileType);
            }
            const res = await axios.post(`${API_BASE}/api/benchmark/generate-report`, params, authConfig);
            return res.data;
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['generatedReports'] });
            setActiveTab('view');
            refetchReports();
        },
    });

    const isAdmin = userProfile?.role === 'Admin';

    if (userLoading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 400 }}>
                <Loader2 className="lucide-spin" size={32} color="var(--primary-color)" />
            </div>
        );
    }

    if (!isAdmin) {
        return (
            <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                <PageHeader icon={Mail} title="Generate Benchmark Report" subtitle="Create email-friendly HTML reports" />
                <div className="glass-panel" style={{ padding: '2.5rem', textAlign: 'center' }}>
                    <AlertCircle size={40} color="#dc2626" style={{ marginBottom: '1rem' }} />
                    <div style={{ color: '#dc2626', fontSize: '1.1rem', fontWeight: 600, marginBottom: '0.5rem' }}>Access Denied</div>
                    <p style={{ color: 'var(--text-secondary)', margin: 0 }}>Only Admin users can generate benchmark reports.</p>
                </div>
            </div>
        );
    }

    const builds = buildsData?.builds ?? [];
    const reports = reportsData?.items ?? [];
    const build_versions = builds.map(b => b.version);

    // /api/reports/file/* is Admin-authenticated, so a plain <a href download> gets a 401
    // (the browser sends no Authorization header) and silently does nothing. Fetch the file
    // with the token and hand the browser a blob instead — same approach as Preview.
    const fetchReportBlob = async (report: ReportItem): Promise<string> => {
        const res = await axios.get(`${API_BASE}${report.url}`, { ...authConfig, responseType: 'blob' });
        return URL.createObjectURL(res.data);
    };

    const downloadReport = async (report: ReportItem) => {
        setReportActionError(null);
        try {
            const blobUrl = await fetchReportBlob(report);
            const link = document.createElement('a');
            link.href = blobUrl;
            link.download = report.name;
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
        } catch (err) {
            console.error('Failed to download report:', err);
            setReportActionError(`Could not download ${report.name}. Please try again.`);
        }
    };

    const toggleUnstructuredDs = (ds: string) => {
        setUnstructuredSelected((prev) => {
            const next = new Set(prev);
            if (next.has(ds)) next.delete(ds);
            else next.add(ds);
            return next;
        });
    };
    const toggleStructuredDs = (ds: string) => {
        setStructuredSelected((prev) => {
            const next = new Set(prev);
            if (next.has(ds)) next.delete(ds);
            else next.add(ds);
            return next;
        });
    };

    const selectedDsCount =
        (includeUnstructured ? UNSTRUCTURED_DS_OPTIONS.filter((d) => unstructuredSelected.has(d)).length : 0) +
        (includeStructured ? STRUCTURED_DS_OPTIONS.filter((d) => structuredSelected.has(d)).length : 0);
    const canGenerate = selectedDsCount > 0;

    const inputStyle: React.CSSProperties = {
        width: '100%',
        padding: '0.55rem 0.75rem',
        border: '1.5px solid var(--border-color)',
        borderRadius: '8px',
        background: 'var(--bg-secondary)',
        color: 'var(--text-primary)',
        fontSize: '0.9rem',
        boxSizing: 'border-box',
    };

    const labelStyle: React.CSSProperties = {
        display: 'block',
        marginBottom: '0.3rem',
        fontWeight: 500,
        color: 'var(--text-secondary)',
        fontSize: '0.8rem',
        letterSpacing: '0.02em',
        textTransform: 'uppercase',
    };

    const sectionHeadStyle: React.CSSProperties = {
        fontSize: '0.8rem',
        fontWeight: 600,
        color: 'var(--text-secondary)',
        letterSpacing: '0.05em',
        textTransform: 'uppercase',
        marginBottom: '0.75rem',
        display: 'flex',
        alignItems: 'center',
        gap: '0.5rem',
    };

    return (
        <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
            <PageHeader icon={Mail} title="Generate Benchmark Report" subtitle="Create email-friendly HTML reports with customizable sections" />

            {/* Tabs */}
            <div style={{ display: 'flex', gap: '0.25rem', borderBottom: '2px solid var(--border-color)', paddingBottom: '0' }}>
                {(['generate', 'view'] as const).map((tab) => {
                    const isActive = activeTab === tab;
                    return (
                        <button
                            key={tab}
                            type="button"
                            onClick={() => setActiveTab(tab)}
                            style={{
                                padding: '0.6rem 1.2rem',
                                border: 'none',
                                background: 'transparent',
                                color: isActive ? 'var(--primary-color)' : 'var(--text-secondary)',
                                cursor: 'pointer',
                                fontWeight: isActive ? 700 : 400,
                                fontSize: '0.92rem',
                                borderBottom: isActive ? '2px solid var(--primary-color)' : '2px solid transparent',
                                marginBottom: '-2px',
                                transition: 'all 0.15s',
                                display: 'flex',
                                alignItems: 'center',
                                gap: '0.4rem',
                            }}
                        >
                            {tab === 'generate' ? <FileText size={15} /> : <Eye size={15} />}
                            {tab === 'generate' ? 'Generate' : `View Reports${reports.length ? ` (${reports.length})` : ''}`}
                        </button>
                    );
                })}
            </div>

            {/* ── Generate Tab ── */}
            {activeTab === 'generate' && (
                <form
                    onSubmit={(e) => { e.preventDefault(); generateReportMutation.mutate(); }}
                    style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}
                >
                    {buildsLoading ? (
                        <div className="glass-panel" style={{ padding: '3rem', textAlign: 'center' }}>
                            <Loader2 className="lucide-spin" size={28} color="var(--primary-color)" />
                        </div>
                    ) : (
                        <>
                            {/* Build & Scope */}
                            <div className="glass-panel" style={{ padding: '1.5rem' }}>
                                <div style={sectionHeadStyle}>
                                    <Settings size={14} />
                                    Build &amp; Scope
                                </div>
                                <div style={{ display: 'grid', gridTemplateColumns: '240px 240px 120px', gap: '1rem' }}>
                                    <div>
                                        <label style={labelStyle}>Current Build <span style={{ color: 'var(--text-secondary)', fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>(optional — defaults to latest)</span></label>
                                        <select value={currentBuild} onChange={(e) => setCurrentBuild(e.target.value)} style={inputStyle}>
                                            <option value="">Latest available</option>
                                            {build_versions.map((b) => (
                                                <option key={b} value={b}>{b}</option>
                                            ))}
                                        </select>
                                    </div>
                                    <div>
                                        <label style={labelStyle}>Previous Build <span style={{ color: 'var(--text-secondary)', fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>(optional)</span></label>
                                        <select value={previousBuild} onChange={(e) => setPreviousBuild(e.target.value)} style={inputStyle}>
                                            <option value="">None</option>
                                            {build_versions.map((b) => (
                                                <option key={b} value={b}>{b}</option>
                                            ))}
                                        </select>
                                    </div>
                                    <div>
                                        <label style={labelStyle}>Environment ID</label>
                                        <input
                                            type="number"
                                            min="0"
                                            value={environmentId}
                                            onChange={(e) => setEnvironmentId(parseInt(e.target.value, 10) || 0)}
                                            style={inputStyle}
                                            placeholder="0 = all"
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* Report Options */}
                            <div className="glass-panel" style={{ padding: '1.5rem' }}>
                                <div style={sectionHeadStyle}>
                                    <FileText size={14} />
                                    Report Sections
                                </div>
                                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.6rem' }}>
                                    <ToggleOption
                                        checked={includeScannerPerf}
                                        onChange={setIncludeScannerPerf}
                                        label="Scanner Performance"
                                        desc="CPU, memory, and scan speed metrics"
                                        flag="--scanner-performance"
                                    />
                                    <ToggleOption
                                        checked={includeErrors}
                                        onChange={setIncludeErrors}
                                        label="Errors &amp; RCA"
                                        desc="Observed issues and root-cause notes"
                                        flag="--errors"
                                    />
                                </div>
                                {includeScannerPerf && (
                                    <div style={{ marginTop: '0.75rem', display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
                                        <span style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                                            Scanner charts window
                                        </span>
                                        {([
                                            { v: 'last' as const, label: 'Last workflow run', hint: 'Most recent workflow run for this build (all its envs)' },
                                            { v: 'all' as const, label: 'All build scans', hint: 'Every benchmark scan of this build' },
                                        ]).map((opt) => (
                                            <button
                                                key={opt.v}
                                                type="button"
                                                title={opt.hint}
                                                onClick={() => setScannerScope(opt.v)}
                                                style={{
                                                    padding: '0.35rem 0.75rem',
                                                    borderRadius: '999px',
                                                    border: `1px solid ${scannerScope === opt.v ? 'var(--accent, #2563eb)' : 'var(--border-color, #d1d5db)'}`,
                                                    background: scannerScope === opt.v ? 'var(--accent, #2563eb)' : 'transparent',
                                                    color: scannerScope === opt.v ? '#fff' : 'var(--text-secondary)',
                                                    fontSize: '0.75rem',
                                                    fontWeight: 600,
                                                    cursor: 'pointer',
                                                }}
                                            >
                                                {opt.label}
                                            </button>
                                        ))}
                                    </div>
                                )}
                                <div style={{ marginTop: '1rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                                    <div style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                        Data sources in report
                                    </div>
                                    <DsCategoryPicker
                                        enabled={includeUnstructured}
                                        onEnabledChange={setIncludeUnstructured}
                                        label="Unstructured DS"
                                        desc="Object stores and file shares"
                                        options={UNSTRUCTURED_DS_OPTIONS}
                                        selected={unstructuredSelected}
                                        onToggleDs={toggleUnstructuredDs}
                                        onSelectAll={() => setUnstructuredSelected(new Set(UNSTRUCTURED_DS_OPTIONS))}
                                    />
                                    <DsCategoryPicker
                                        enabled={includeStructured}
                                        onEnabledChange={setIncludeStructured}
                                        label="Structured DS"
                                        desc="Relational databases"
                                        options={STRUCTURED_DS_OPTIONS}
                                        selected={structuredSelected}
                                        onToggleDs={toggleStructuredDs}
                                        onSelectAll={() => setStructuredSelected(new Set(STRUCTURED_DS_OPTIONS))}
                                    />
                                    {!canGenerate && (
                                        <p style={{ margin: 0, fontSize: '0.82rem', color: 'var(--danger-color)' }}>
                                            Select at least one data source to generate a report.
                                        </p>
                                    )}
                                </div>
                            </div>

                            {/* Test Environment Configuration */}
                            <div className="glass-panel" style={{ padding: '1.5rem' }}>
                                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
                                    <div style={sectionHeadStyle}>
                                        <Settings size={14} />
                                        Test Environment Configuration
                                        <code style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', background: 'var(--bg-secondary)', padding: '2px 6px', borderRadius: 4, border: '1px solid var(--border-color)', fontWeight: 400 }}>--test-env</code>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => setTestEnvExpanded(x => !x)}
                                        style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.82rem', flexShrink: 0 }}
                                    >
                                        {testEnvExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                                        {testEnvExpanded ? 'Collapse' : 'Edit config'}
                                    </button>
                                </div>

                                {testEnvExpanded && (
                                    <div style={{ marginTop: '1rem', padding: '1rem', background: 'var(--bg-secondary)', borderRadius: '8px', border: '1px solid var(--border-color)' }}>
                                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '0.75rem', marginBottom: '0.75rem' }}>
                                            <div style={{ gridColumn: '1 / -1' }}>
                                                <label style={labelStyle}>Environment Description</label>
                                                <input type="text" value={testEnvDesc} onChange={(e) => setTestEnvDesc(e.target.value)} style={inputStyle} placeholder="e.g. Two large-scale environments" />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>Scanners</label>
                                                <input type="number" min="1" value={testEnvScanners} onChange={(e) => setTestEnvScanners(parseInt(e.target.value) || 1)} style={inputStyle} />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>Threads / Scanner</label>
                                                <input type="number" min="1" value={testEnvThreadsPerScanner} onChange={(e) => setTestEnvThreadsPerScanner(parseInt(e.target.value) || 1)} style={inputStyle} />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>Scan Threads</label>
                                                <input type="number" min="1" value={testEnvScanThreads} onChange={(e) => setTestEnvScanThreads(parseInt(e.target.value) || 1)} style={inputStyle} />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>File Count</label>
                                                <input type="text" value={testEnvFileCount} onChange={(e) => setTestEnvFileCount(e.target.value)} style={inputStyle} placeholder="1 M" />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>File Type</label>
                                                <input type="text" value={testEnvFileType} onChange={(e) => setTestEnvFileType(e.target.value)} style={inputStyle} placeholder="txt" />
                                            </div>
                                            <div>
                                                <label style={labelStyle}>DS Lite Mode</label>
                                                <select value={testEnvDsLite} onChange={(e) => setTestEnvDsLite(e.target.value)} style={inputStyle}>
                                                    <option value="True">True</option>
                                                    <option value="False">False</option>
                                                </select>
                                            </div>
                                            <div>
                                                <label style={labelStyle}>Scanner Jobs Thresholds</label>
                                                <select value={testEnvScannerJobs} onChange={(e) => setTestEnvScannerJobs(e.target.value)} style={inputStyle}>
                                                    <option value="false">false</option>
                                                    <option value="true">true</option>
                                                </select>
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Submit */}
                            <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
                                <button
                                    type="submit"
                                    disabled={generateReportMutation.isPending || !canGenerate}
                                    style={{
                                        padding: '0.7rem 1.75rem',
                                        background: generateReportMutation.isPending ? 'var(--text-secondary)' : 'linear-gradient(135deg, #8B5CF6 0%, #6366F1 100%)',
                                        color: 'white',
                                        border: 'none',
                                        borderRadius: '8px',
                                        cursor: generateReportMutation.isPending ? 'not-allowed' : 'pointer',
                                        fontWeight: 600,
                                        fontSize: '0.95rem',
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: '0.5rem',
                                        boxShadow: generateReportMutation.isPending ? 'none' : '0 2px 8px rgba(139, 92, 246, 0.35)',
                                        transition: 'all 0.15s',
                                    }}
                                >
                                    {generateReportMutation.isPending ? (
                                        <><Loader2 size={16} className="lucide-spin" /> Generating…</>
                                    ) : (
                                        <><Mail size={16} /> Generate Report</>
                                    )}
                                </button>

                                {generateReportMutation.isPending && (
                                    <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                                        Running report script, this may take a moment…
                                    </span>
                                )}
                            </div>

                            {generateReportMutation.isError && (
                                <div style={{ padding: '1rem 1.25rem', background: '#fee2e2', color: '#991b1b', borderRadius: '8px', fontSize: '0.9rem', display: 'flex', alignItems: 'flex-start', gap: '0.75rem' }}>
                                    <XCircle size={18} style={{ flexShrink: 0, marginTop: 1 }} />
                                    <div>
                                        <strong>Generation failed</strong><br />
                                        {(generateReportMutation.error as any)?.response?.data?.detail || (generateReportMutation.error as any)?.message || 'Unknown error'}
                                    </div>
                                </div>
                            )}

                            {generateReportMutation.isSuccess && (
                                <div style={{ padding: '1rem 1.25rem', background: '#dcfce7', color: '#166534', borderRadius: '8px', fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <CheckCircle2 size={18} style={{ flexShrink: 0 }} />
                                    Report generated successfully — check the <strong>View Reports</strong> tab.
                                </div>
                            )}
                        </>
                    )}
                </form>
            )}

            {/* ── View Reports Tab ── */}
            {activeTab === 'view' && (
                <div className="glass-panel" style={{ overflow: 'hidden' }}>
                    <div style={{ padding: '1rem 1.25rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--border-color)' }}>
                        <span style={{ fontWeight: 600, color: 'var(--text-primary)', fontSize: '0.95rem' }}>
                            Generated Reports {reports.length > 0 && <span style={{ color: 'var(--text-secondary)', fontWeight: 400 }}>({reports.length})</span>}
                        </span>
                        <button
                            type="button"
                            className="btn-secondary"
                            onClick={() => refetchReports()}
                            disabled={reportsLoading}
                            style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem' }}
                        >
                            {reportsLoading ? <Loader2 size={14} className="lucide-spin" /> : <RefreshCw size={14} />}
                            Refresh
                        </button>
                    </div>

                    {reportActionError && (
                        <div style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.5rem',
                            padding: '0.75rem 1.25rem',
                            color: '#dc2626',
                            fontSize: '0.85rem',
                            borderBottom: '1px solid var(--border-color)',
                        }}>
                            <AlertCircle size={15} /> {reportActionError}
                        </div>
                    )}

                    {reportsLoading ? (
                        <div style={{ padding: '3rem', textAlign: 'center' }}>
                            <Loader2 className="lucide-spin" size={28} color="var(--primary-color)" />
                        </div>
                    ) : reports.length === 0 ? (
                        <div style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
                            <FileText size={36} style={{ marginBottom: '0.75rem', opacity: 0.4 }} />
                            <div>No generated reports found.</div>
                            <div style={{ fontSize: '0.85rem', marginTop: '0.25rem' }}>Use the Generate tab to create one.</div>
                        </div>
                    ) : (
                        <div style={{ overflowX: 'auto' }}>
                            <table>
                                <thead>
                                    <tr>
                                        <th>Report Name</th>
                                        <th>Build</th>
                                        <th>Modified</th>
                                        <th>Size</th>
                                        <th>Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {reports.map((report) => {
                                        return (
                                            <tr key={report.name}>
                                                <td style={{ fontWeight: 600, fontSize: '0.9rem' }}>{report.name}</td>
                                                <td style={{ whiteSpace: 'nowrap' }}>
                                                    {report.build ? (
                                                        <span style={{
                                                            fontSize: '0.78rem',
                                                            fontWeight: 600,
                                                            color: 'var(--primary-color)',
                                                            background: 'var(--bg-secondary)',
                                                            border: '1px solid var(--border-color)',
                                                            padding: '2px 8px',
                                                            borderRadius: 6,
                                                        }}>
                                                            {report.build}
                                                        </span>
                                                    ) : (
                                                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>—</span>
                                                    )}
                                                </td>
                                                <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap', fontSize: '0.85rem' }}>{formatModified(report.modified_at)}</td>
                                                <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap', fontSize: '0.85rem' }}>{formatBytes(report.size_bytes)}</td>
                                                <td>
                                                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                                                        <button
                                                            type="button"
                                                            className="btn-secondary"
                                                            onClick={async () => {
                                                                setReportActionError(null);
                                                                try {
                                                                    const blobUrl = await fetchReportBlob(report);
                                                                    setPreviewReport({ name: report.name, url: report.url, blobUrl });
                                                                } catch (err) {
                                                                    console.error('Failed to load preview:', err);
                                                                    setReportActionError(`Could not open ${report.name}. Please try again.`);
                                                                }
                                                            }}
                                                            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.82rem', padding: '0.4rem 0.7rem' }}
                                                        >
                                                            <Eye size={13} /> Preview
                                                        </button>
                                                        <button
                                                            type="button"
                                                            className="btn-secondary"
                                                            onClick={() => downloadReport(report)}
                                                            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', fontSize: '0.82rem', padding: '0.4rem 0.7rem' }}
                                                        >
                                                            <Download size={13} /> Download
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            )}

            {/* ── Preview Modal ── */}
            {previewReport && (
                <div
                    style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
                    onClick={() => { if (previewReport.blobUrl) URL.revokeObjectURL(previewReport.blobUrl); setPreviewReport(null); }}
                >
                    <div
                        style={{ background: 'white', borderRadius: '10px', width: '92%', height: '92%', display: 'flex', flexDirection: 'column', overflow: 'hidden', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div style={{ padding: '0.85rem 1.25rem', borderBottom: '1px solid #e5e7eb', display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: '#f9fafb' }}>
                            <span style={{ fontWeight: 600, fontSize: '0.9rem', color: '#374151' }}>{previewReport.name}</span>
                            <button
                                type="button"
                                onClick={() => { if (previewReport.blobUrl) URL.revokeObjectURL(previewReport.blobUrl); setPreviewReport(null); }}
                                style={{ background: 'none', border: 'none', fontSize: '1.3rem', cursor: 'pointer', color: '#6b7280', lineHeight: 1, padding: '0.25rem 0.5rem' }}
                            >
                                ✕
                            </button>
                        </div>
                        <iframe
                            src={previewReport.blobUrl || `${API_BASE}${previewReport.url}`}
                            style={{ flex: 1, border: 'none', width: '100%' }}
                            title="Report Preview"
                        />
                    </div>
                </div>
            )}
        </div>
    );
};

export default GenerateBenchmarkReportPage;
