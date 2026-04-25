import React from 'react';
import { Activity, LogOut } from 'lucide-react';

interface NavbarProps {
  onLogout: () => void;
}

const Navbar: React.FC<NavbarProps> = ({ onLogout }) => {
  return (
    <nav className="navbar">
      <div className="nav-brand">
        <Activity size={24} />
        <span>Scan Benchmarks</span>
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
