import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, Terminal, AlertTriangle } from 'lucide-react';
import { API_BASE } from '../apiConfig';
import PaginationBar from '../components/Pagination';

interface LogsProps {
  token: string;
}

const LOG_PAGE_SIZE = 25;

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
}

const Logs: React.FC<LogsProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const [page, setPage] = useState(0);

  const { data, isLoading } = useQuery({
    queryKey: ['logs', token, page],
    queryFn: async () => {
      const res = await axios.get<{ items: LogErrRow[]; total: number }>(`${API_BASE}/api/logs/errors`, {
        ...authConfig,
        params: { limit: LOG_PAGE_SIZE, offset: page * LOG_PAGE_SIZE },
      });
      return res.data;
    },
    refetchInterval: 10000,
  });

  const logs = data?.items ?? [];
  const total = data?.total ?? 0;

  useEffect(() => {
    setPage(0);
  }, [token]);

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

        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {isLoading ? (
            <div style={{ padding: '3rem', textAlign: 'center' }}>
              <Loader2 className="lucide-spin" size={32} />
            </div>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table>
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Environment</th>
                      <th>Scan ID</th>
                      <th>Source</th>
                      <th>Error</th>
                      <th>AI analysis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((log) => (
                      <tr key={log.id}>
                        <td style={{ whiteSpace: 'nowrap', fontSize: '0.875rem' }}>{log.log_timestamp ?? '—'}</td>
                        <td style={{ fontSize: '0.875rem' }}>{log.environment_name ?? log.environment_id}</td>
                        <td style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace' }}>
                          {log.scan_id ? `${log.scan_id.slice(0, 12)}${log.scan_id.length > 12 ? '…' : ''}` : '—'}
                        </td>
                        <td style={{ fontWeight: 500 }}>{log.log_source ?? '—'}</td>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem' }}>
                            <AlertTriangle size={16} color="var(--danger-color)" style={{ flexShrink: 0, marginTop: '2px' }} />
                            <div>
                              <strong style={{ display: 'block', marginBottom: '0.25rem', fontSize: '0.9rem' }}>
                                {(log.error_context || '').slice(0, 400)}
                                {(log.error_context && log.error_context.length > 400) ? '…' : ''}
                              </strong>
                            </div>
                          </div>
                        </td>
                        <td style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', maxWidth: 320 }}>
                          {(log.ai_analysis || '—').slice(0, 500)}
                        </td>
                      </tr>
                    ))}
                    {logs.length === 0 && (
                      <tr>
                        <td colSpan={6} style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>
                          No recent scanner errors found.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
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
