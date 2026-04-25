import React from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Play, Square, Loader2 } from 'lucide-react';

interface DashboardProps {
  token: string;
}

const Dashboard: React.FC<DashboardProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const { data: environments, isLoading: envLoading } = useQuery({
    queryKey: ['environments'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/environments', authConfig);
      return res.data;
    },
    refetchInterval: 5000 // Polling
  });

  const { data: benchmarks, isLoading: benchLoading } = useQuery({
    queryKey: ['benchmarks'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/benchmarks', authConfig);
      return res.data;
    },
    refetchInterval: 5000 // Polling
  });

  const stopWorkflow = async (id: number) => {
    try {
      await axios.post(`http://localhost:8000/api/workflow/stop/${id}`, {}, authConfig);
      // Let React Query auto-refetch
    } catch (e) {
      console.error(e);
    }
  };

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      
      <section>
        <h2 style={{ marginBottom: '1rem', color: 'var(--primary-color)' }}>Test Environments</h2>
        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {envLoading ? (
            <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" /></div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Environment</th>
                  <th>URL</th>
                  <th>Status</th>
                  <th>Activity</th>
                </tr>
              </thead>
              <tbody>
                {environments?.map((env: any) => (
                  <tr key={env.id}>
                    <td style={{ fontWeight: 500 }}>{env.env_name}</td>
                    <td style={{ color: 'var(--text-secondary)' }}>{env.base_url}</td>
                    <td>
                      <span className={`badge ${env.is_active ? 'active' : 'inactive'}`}>
                        {env.is_active ? 'Online' : 'Offline'}
                      </span>
                    </td>
                    <td>
                      {env.is_busy ? (
                        <span style={{ color: 'var(--danger-color)', fontSize: '0.875rem', fontWeight: 600 }}>Running Scan</span>
                      ) : (
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>Idle</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h2 style={{ color: 'var(--primary-color)' }}>Recent Benchmarks</h2>
          <button className="btn">
            <Play size={16} /> New Benchmark Run
          </button>
        </div>
        
        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {benchLoading ? (
             <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" /></div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Date</th>
                  <th>Scenario</th>
                  <th>Scan Type</th>
                  <th>Data Source</th>
                  <th>File Set</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {benchmarks?.map((b: any) => (
                  <tr key={b.id}>
                    <td>#{b.id}</td>
                    <td>{b.run_date || '-'}</td>
                    <td>{b.scenario_type || '-'}</td>
                    <td>{b.scan_type || '-'}</td>
                    <td>{b.data_source_type || '-'}</td>
                    <td>{b.file_set_type || '-'}</td>
                    <td>
                      <button 
                        className="btn-danger" 
                        style={{ padding: '0.25rem 0.5rem', borderRadius: '4px', border: '1px solid', background: 'transparent', cursor: 'pointer' }}
                        onClick={() => stopWorkflow(b.id)}
                      >
                        <Square size={14} /> Stop
                      </button>
                    </td>
                  </tr>
                ))}
                {benchmarks?.length === 0 && (
                  <tr>
                    <td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-secondary)' }}>No benchmarks found</td>
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

export default Dashboard;
