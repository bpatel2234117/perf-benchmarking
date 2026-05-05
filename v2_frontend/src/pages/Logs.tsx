import React from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Loader2, Terminal, AlertTriangle } from 'lucide-react';

interface LogsProps {
  token: string;
}

const Logs: React.FC<LogsProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const { data: logs, isLoading } = useQuery({
    queryKey: ['logs'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/logs/errors', authConfig);
      return res.data;
    },
    refetchInterval: 10000 // Poll every 10s
  });

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
            <div style={{ padding: '3rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={32} /></div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Scan ID</th>
                  <th>Data Source</th>
                  <th>Source File</th>
                  <th>Error Context</th>
                </tr>
              </thead>
              <tbody>
                {logs?.map((log: any) => (
                  <tr key={log.id}>
                    <td style={{ whiteSpace: 'nowrap', fontSize: '0.875rem' }}>{log.captured_at}</td>
                    <td style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>{log.scan_id?.slice(0, 8)}...</td>
                    <td style={{ fontWeight: 500 }}>{log.ds_name || 'Unknown'}</td>
                    <td style={{ fontSize: '0.875rem', fontFamily: 'monospace' }}>{log.source_file || '-'}</td>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.5rem' }}>
                        <AlertTriangle size={16} color="var(--danger-color)" style={{ flexShrink: 0, marginTop: '2px' }} />
                        <div>
                          <strong style={{ display: 'block', marginBottom: '0.25rem' }}>{log.error_message}</strong>
                          <span style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>{log.ai_summary}</span>
                        </div>
                      </div>
                    </td>
                  </tr>
                ))}
                {(!logs || logs.length === 0) && (
                  <tr>
                    <td colSpan={5} style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-secondary)' }}>
                      No recent scanner errors found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
};

export default Logs;
