import React from 'react';
import { Activity, LogOut, TerminalSquare, LayoutDashboard } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';

interface NavbarProps {
  onLogout: () => void;
}

const Navbar: React.FC<NavbarProps> = ({ onLogout }) => {
  const location = useLocation();

  return (
    <nav className="navbar">
      <div className="nav-brand" style={{ gap: '2rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--primary-color)' }}>
          <Activity size={24} />
          <span>BigID Benchmarks</span>
        </div>
        
        <div style={{ display: 'flex', gap: '1rem', fontSize: '0.875rem' }}>
          <Link to="/" style={{ textDecoration: 'none', color: location.pathname === '/' ? 'var(--primary-color)' : 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <LayoutDashboard size={16} /> Dashboard
          </Link>
          <Link to="/logs" style={{ textDecoration: 'none', color: location.pathname === '/logs' ? 'var(--primary-color)' : 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <TerminalSquare size={16} /> AI Logs
          </Link>
        </div>
      </div>

      <div>
        <button onClick={onLogout} className="btn btn-danger">
          <LogOut size={16} /> Logout
        </button>
      </div>
    </nav>
  );
};

export default Navbar;
