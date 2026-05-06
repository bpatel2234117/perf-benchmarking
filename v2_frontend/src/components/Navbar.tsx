import React, { useEffect, useRef, useState } from 'react';
import {
  ChevronDown,
  FlaskConical,
  LogOut,
  RefreshCw,
  Wrench,
  Loader2,
} from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { SYSTEM_TOOLS_ENTRIES } from '../navigation/systemToolsConfig';
import { API_BASE } from '../apiConfig';

interface NavbarProps {
  onLogout: () => void;
  token: string;
  children: React.ReactNode;
}

function pathMatches(entry: typeof SYSTEM_TOOLS_ENTRIES[number], pathname: string): boolean {
  if (entry.kind === 'app') {
    return entry.path === pathname || (entry.path !== '/' && pathname.startsWith(`${entry.path}/`));
  }
  return pathname === `/tools/${entry.slug}` || pathname.startsWith(`/tools/${entry.slug}/`);
}

const Navbar: React.FC<NavbarProps> = ({ onLogout, token, children }) => {
  const location = useLocation();
  const [toolsOpen, setToolsOpen] = useState(false);
  const sidebarRef = useRef<HTMLDivElement>(null);

  const { data: userProfile } = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/me`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      return res.data;
    }
  });

  const userRole = userProfile?.role || 'Viewer';
  const isAdminOrDev = ['Admin', 'Developer'].includes(userRole);

  const filteredTools = SYSTEM_TOOLS_ENTRIES.filter(entry => {
    if (entry.kind === 'app' && entry.path === '/tools/manage-rbac') return isAdminOrDev;
    return true;
  });

  const systemToolsCurrent = filteredTools.some((e) => pathMatches(e, location.pathname));

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (!sidebarRef.current?.contains(e.target as Node)) setToolsOpen(false);
    }
    if (toolsOpen) document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [toolsOpen]);

  useEffect(() => {
    setToolsOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setToolsOpen(false);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div className="app-shell">
      <aside ref={sidebarRef} className="app-sidebar-purple" aria-label="Main navigation">
        <Link to="/" className="app-sidebar-brand-white">
          <img src="/logo.png" alt="BigID" style={{ width: 24, height: 24, borderRadius: 6, objectFit: 'cover' }} />
          <span className="app-sidebar-title">BigID Benchmarks</span>
        </Link>

        <Link
          to="/workflow"
          className={
            location.pathname === '/workflow' || location.pathname.startsWith('/workflow/')
              ? 'app-sidebar-toplink app-sidebar-toplink--active'
              : 'app-sidebar-toplink'
          }
        >
          <FlaskConical size={18} aria-hidden />
          Workflow Automation
        </Link>

        <div className="app-sidebar-purple-section sidebar-tools-trigger">
          <button
            type="button"
            className={`app-sidebar-sysbtn ${toolsOpen || systemToolsCurrent ? 'app-sidebar-sysbtn--active' : ''}`}
            aria-expanded={toolsOpen}
            aria-controls="system-tools-panel"
            onClick={() => setToolsOpen((o) => !o)}
          >
            <Wrench size={18} aria-hidden /> System Tools
            <ChevronDown size={16} className={`app-sys-chevron ${toolsOpen ? 'app-sys-chevron--open' : ''}`} aria-hidden />
          </button>

          {toolsOpen && (
            <div id="system-tools-panel" className="system-tools-flyout" role="menu">
              <ul className="system-tools-menu">
                {filteredTools.map((entry) => {
                  const Icon = entry.icon;
                  const selected = pathMatches(entry, location.pathname);
                  const to = entry.kind === 'app' ? entry.path : `/tools/${entry.slug}`;
                  return (
                    <li key={entry.kind === 'app' ? entry.path : entry.slug}>
                      <Link
                        role="menuitem"
                        to={to}
                        className={selected ? 'system-tools-link system-tools-link--active' : 'system-tools-link'}
                      >
                        <Icon size={16} aria-hidden strokeWidth={2} />
                        {entry.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>

        <nav className="app-sidebar-purple-footer">
          <div style={{ padding: '0 1rem 1rem', fontSize: '0.75rem', color: 'rgba(255,255,255,0.6)', display: 'flex', alignItems: 'center', gap: 6 }}>
            {userProfile ? (
              <>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981' }} />
                <span>{userRole}</span>
              </>
            ) : (
              <Loader2 size={12} className="lucide-spin" />
            )}
          </div>
          <button type="button" className="app-sidebar-refresh-btn" onClick={() => window.location.reload()}>
            <RefreshCw size={17} aria-hidden /> Refresh
          </button>
          <button type="button" className="app-sidebar-logout-btn" onClick={onLogout}>
            <LogOut size={17} aria-hidden /> Logout
          </button>
        </nav>
      </aside>

      <div className="app-main-area">
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          <main className="main-content app-main-body">{children}</main>
        </div>
      </div>
    </div>
  );
};

export default Navbar;

