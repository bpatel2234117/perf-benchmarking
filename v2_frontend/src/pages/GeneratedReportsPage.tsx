import React from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { ExternalLink, FileText, Loader2, RefreshCw } from 'lucide-react';
import { API_BASE } from '../apiConfig';

interface GeneratedReportsPageProps {
  token: string;
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

const GeneratedReportsPage: React.FC<GeneratedReportsPageProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['generatedReports', token],
    queryFn: async () => {
      const res = await axios.get<{ items: ReportItem[] }>(`${API_BASE}/api/reports`, authConfig);
      return res.data;
    },
  });

  const reports = data?.items ?? [];

  // /api/reports/file/* requires an Admin bearer token, which the browser never sends on a
  // plain <a href> — that request 401s and the tab opens blank. Fetch it with the token and
  // open the resulting blob instead.
  const openReport = async (report: ReportItem) => {
    try {
      const res = await axios.get(`${API_BASE}${report.url}`, { ...authConfig, responseType: 'blob' });
      const blobUrl = URL.createObjectURL(res.data);
      window.open(blobUrl, '_blank', 'noopener');
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
    } catch (err) {
      console.error('Failed to open report:', err);
    }
  };

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
      <section style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.55rem', marginBottom: '0.4rem' }}>
            <FileText size={24} color="var(--primary-color)" />
            <h2 style={{ color: 'var(--text-primary)', margin: 0 }}>Generated Reports</h2>
          </div>
          <p style={{ color: 'var(--text-secondary)', margin: 0 }}>
            Open previously generated HTML benchmark reports from the shared reports folder.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => refetch()} disabled={isFetching}>
          {isFetching ? <Loader2 size={16} className="lucide-spin" /> : <RefreshCw size={16} />}
          Refresh
        </button>
      </section>

      <div className="glass-panel" style={{ overflow: 'hidden' }}>
        {isLoading ? (
          <div style={{ padding: '3rem', textAlign: 'center' }}>
            <Loader2 className="lucide-spin" size={32} />
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Report</th>
                  <th>Build</th>
                  <th>Modified</th>
                  <th>Size</th>
                  <th>Open</th>
                </tr>
              </thead>
              <tbody>
                {reports.map((report) => {
                  return (
                    <tr key={report.name}>
                      <td style={{ fontWeight: 600 }}>{report.name}</td>
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
                          <span style={{ color: 'var(--text-secondary)' }}>—</span>
                        )}
                      </td>
                      <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>{formatModified(report.modified_at)}</td>
                      <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>{formatBytes(report.size_bytes)}</td>
                      <td>
                        <button type="button" className="btn-secondary" onClick={() => openReport(report)} style={{ display: 'inline-flex' }}>
                          <ExternalLink size={16} />
                          Open
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {reports.length === 0 && (
                  <tr>
                    <td colSpan={5} style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>
                      No generated HTML reports found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default GeneratedReportsPage;
