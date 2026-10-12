import React, { useState } from 'react';
import { LogIn } from 'lucide-react';
import axios from 'axios';
import { API_BASE } from '../apiConfig';

interface LoginProps {
  setToken: (token: string) => void;
}

const Login: React.FC<LoginProps> = ({ setToken }) => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const response = await axios.post(
        `${API_BASE}/api/token`,
        { username: email.trim(), password },
        { headers: { 'Content-Type': 'application/json' } },
      );
      setToken(response.data.access_token);
    } catch (err: any) {
      let errorMessage = 'Login failed. Ensure you use a @bigid.com email.';
      if (!err.response) {
        errorMessage = `Cannot reach API at ${API_BASE}. Check spb_api is running on port 8000.`;
      } else if (err.response?.data?.detail) {
        errorMessage = typeof err.response.data.detail === 'string'
          ? err.response.data.detail
          : 'Login failed. Please check your credentials.';
      }
      setError(errorMessage);
    }
  };

  return (
    <div className="login-container">
      <div className="glass-panel login-card animate-in">
        <div style={{ textAlign: 'center' }}>
          <h2>Welcome Back</h2>
          <p style={{ color: 'var(--text-secondary)' }}>Sign in to manage benchmark workflows</p>
        </div>

        {error && <div style={{ color: 'var(--danger-color)', fontSize: '0.875rem', textAlign: 'center' }}>{error}</div>}

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          <div className="input-group">
            <label>Email (@bigid.com)</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="e.g. bpatel@bigid.com"
              required
            />
          </div>
          <div className="input-group">
            <label>Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter your password"
              required
            />
          </div>
          <button type="submit" className="btn" style={{ justifyContent: 'center' }}>
            <LogIn size={18} /> Sign In
          </button>
        </form>
      </div>
    </div>
  );
};

export default Login;
