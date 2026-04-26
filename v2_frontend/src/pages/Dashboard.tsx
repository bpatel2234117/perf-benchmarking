import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Play, Square, Loader2, X, BarChart3 } from 'lucide-react';
import { ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

interface DashboardProps {
  token: string;
}

const Dashboard: React.FC<DashboardProps> = ({ token }) => {
  const authConfig = { headers: { Authorization: `Bearer ${token}` } };
  const [showModal, setShowModal] = useState(false);
  const [formData, setFormData] = useState({ scenario_type: '', data_source_type_id: 1, file_set_type_id: 1, scan_type: 'PII' });

  const { data: environments, isLoading: envLoading } = useQuery({
    queryKey: ['environments'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/environments', authConfig);
      return res.data;
    },
    refetchInterval: 5000
  });

  const { data: benchmarks, isLoading: benchLoading, refetch: refetchBenchmarks } = useQuery({
    queryKey: ['benchmarks'],
    queryFn: async () => {
      const res = await axios.get('http://localhost:8000/api/benchmarks', authConfig);
      return res.data;
    },
    refetchInterval: 5000
  });

  const stopWorkflow = async (id: number) => {
    try {
      await axios.post(`http://localhost:8000/api/workflow/stop/${id}`, {}, authConfig);
    } catch (e) {
      console.error(e);
    }
  };

  const submitBenchmark = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await axios.post('http://localhost:8000/api/benchmarks', formData, authConfig);
      setShowModal(false);
      refetchBenchmarks();
    } catch (e) {
      console.error(e);
      alert('Failed to start benchmark workflow');
    }
  };

  // Format data for Scatter Chart (Files vs Throughput)
  const chartData = benchmarks?.filter((b: any) => b.total_files && b.throughput_cluster_files_hr).map((b: any) => ({
    id: b.id,
    files: Number(b.total_files),
    throughput: Number(b.throughput_cluster_files_hr),
    scenario: b.scenario_type
  })) || [];

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
      
      <section style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: '2rem' }}>
        <div>
          <h2 style={{ marginBottom: '1rem', color: 'var(--primary-color)', fontSize: '1.25rem' }}>Test Environments</h2>
          <div className="glass-panel" style={{ overflow: 'hidden' }}>
            {envLoading ? (
              <div style={{ padding: '2rem', textAlign: 'center' }}><Loader2 className="lucide-spin" /></div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Environment</th>
                    <th>Status</th>
                    <th>Activity</th>
                  </tr>
                </thead>
                <tbody>
                  {environments?.map((env: any) => (
                    <tr key={env.id}>
                      <td style={{ fontWeight: 500 }}>{env.env_name}</td>
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
        </div>

        <div>
          <h2 style={{ marginBottom: '1rem', color: 'var(--primary-color)', fontSize: '1.25rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <BarChart3 size={20} /> Cluster Throughput Trend
          </h2>
          <div className="glass-panel" style={{ padding: '1rem', height: '100%' }}>
            {benchLoading ? (
               <div style={{ padding: '4rem', textAlign: 'center' }}><Loader2 className="lucide-spin" size={32} /></div>
            ) : (
              <div className="chart-container">
                <ResponsiveContainer width="100%" height="100%">
                  <ScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--surface-border)" />
                    <XAxis type="number" dataKey="files" name="Total Files" stroke="var(--text-secondary)" />
                    <YAxis type="number" dataKey="throughput" name="Throughput (Files/hr)" stroke="var(--text-secondary)" />
                    <Tooltip cursor={{ strokeDasharray: '3 3' }} />
                    <Scatter name="Benchmarks" data={chartData} fill="var(--primary-color)" />
                  </ScatterChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </div>
      </section>

      <section>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h2 style={{ color: 'var(--primary-color)', fontSize: '1.25rem' }}>Recent Benchmarks</h2>
          <button className="btn" onClick={() => setShowModal(true)}>
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
                  <th>Files Scanned</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {benchmarks?.map((b: any) => (
                  <tr key={b.id}>
                    <td>#{b.id}</td>
                    <td>{b.run_date || '-'}</td>
                    <td style={{ fontWeight: 500 }}>{b.scenario_type || '-'}</td>
                    <td>{b.scan_type || '-'}</td>
                    <td>{b.data_source_type || '-'}</td>
                    <td>{b.file_set_type || '-'}</td>
                    <td>{b.total_files ? Number(b.total_files).toLocaleString() : '-'}</td>
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
                    <td colSpan={8} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-secondary)' }}>No benchmarks found</td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* New Benchmark Modal */}
      {showModal && (
        <div className="modal-overlay">
          <div className="modal-content animate-in">
            <div className="modal-header">
              <h2 style={{ color: 'var(--text-primary)', fontSize: '1.25rem' }}>Start Workflow Run</h2>
              <button onClick={() => setShowModal(false)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)' }}>
                <X size={20} />
              </button>
            </div>
            
            <form onSubmit={submitBenchmark} style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
              <div className="form-grid">
                <div className="input-group">
                  <label>Scenario / DS Name</label>
                  <input type="text" value={formData.scenario_type} onChange={e => setFormData({...formData, scenario_type: e.target.value})} required placeholder="e.g. AWS S3 Mega Scale" />
                </div>
                <div className="input-group">
                  <label>Scan Type</label>
                  <select 
                    style={{ padding: '0.5rem', borderRadius: '4px', border: '1px solid var(--surface-border)' }}
                    value={formData.scan_type} 
                    onChange={e => setFormData({...formData, scan_type: e.target.value})}
                  >
                    <option value="PII">PII (Classification)</option>
                    <option value="Catalog">Catalog (Metadata only)</option>
                  </select>
                </div>
                <div className="input-group">
                  <label>Data Source Type ID</label>
                  <input type="number" min="1" value={formData.data_source_type_id} onChange={e => setFormData({...formData, data_source_type_id: parseInt(e.target.value)})} required />
                </div>
                <div className="input-group">
                  <label>File Set Type ID</label>
                  <input type="number" min="1" value={formData.file_set_type_id} onChange={e => setFormData({...formData, file_set_type_id: parseInt(e.target.value)})} required />
                </div>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '1rem', marginTop: '1rem' }}>
                <button type="button" className="btn" style={{ background: 'transparent', color: 'var(--text-secondary)', border: 'none' }} onClick={() => setShowModal(false)}>Cancel</button>
                <button type="submit" className="btn"><Play size={16} /> Execute Workflow</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dashboard;
