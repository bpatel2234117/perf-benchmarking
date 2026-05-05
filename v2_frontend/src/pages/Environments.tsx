import React from 'react';
import { Loader2, Plus, TerminalSquare } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';

interface EnvironmentsProps {
  token: string;
}

const Environments: React.FC<EnvironmentsProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };

  const { data: environments, isLoading: envLoading } = useQuery({
    queryKey: ['environments'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/environments', authConfig);
      return res.data;
    },
    refetchInterval: 5000
  });

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      <section>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h2 style={{ color: 'var(--primary-color)', fontSize: '1.25rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <TerminalSquare size={20} /> Test Environments Setup
          </h2>
          <button className="btn">
            <Plus size={16} /> Add Environment
          </button>
        </div>
        
        <div className="glass-panel" style={{ overflow: 'hidden' }}>
          {envLoading ? (
            <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" /></div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Environment Name</th>
                  <th>Base URL</th>
                  <th>Status</th>
                  <th>Activity</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {environments?.map((env: any) => (
                  <tr key={env.id}>
                    <td style={{ fontWeight: 500 }}>{env.env_name}</td>
                    <td>{env.base_url}</td>
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
                    <td>
                      <button className="btn" style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem', background: 'transparent', border: '1px solid var(--surface-border)', color: 'var(--text-primary)' }}>
                        Set as System Default
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
};

export default Environments;
