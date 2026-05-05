import React, { useEffect, useRef, useState } from 'react';
import {
  Activity,
  ChevronDown,
  FlaskConical,
  LogOut,
  RefreshCw,
  Wrench,
} from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { SYSTEM_TOOLS_ENTRIES } from '../navigation/systemToolsConfig';

interface NavbarProps {
  onLogout: () => void;
  children: React.ReactNode;
}

function pathMatches(entry: typeof SYSTEM_TOOLS_ENTRIES[number], pathname: string): boolean {
  if (entry.kind === 'app') {
    return entry.path === pathname || (entry.path !== '/' && pathname.startsWith(`${entry.path}/`));
  }
  return pathname === `/tools/${entry.slug}` || pathname.startsWith(`/tools/${entry.slug}/`);
}

const Navbar: React.FC<NavbarProps> = ({ onLogout, children }) => {
  const location = useLocation();
  const [toolsOpen, setToolsOpen] = useState(false);
  const sidebarRef = useRef<HTMLDivElement>(null);

  const systemToolsCurrent = SYSTEM_TOOLS_ENTRIES.some((e) => pathMatches(e, location.pathname));

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
          <Activity size={22} aria-hidden />
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
                {SYSTEM_TOOLS_ENTRIES.map((entry) => {
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
          <button type="button" className="app-sidebar-refresh-btn" onClick={() => window.location.reload()}>
            <RefreshCw size={17} aria-hidden /> Refresh
          </button>
          <button type="button" className="app-sidebar-logout-btn" onClick={onLogout}>
            <LogOut size={17} aria-hidden /> Logout
          </button>
        </nav>
      </aside>

      <div className="app-main-area">
        <main className="main-content app-main-body">{children}</main>
      </div>
    </div>
  );
};

export default Navbar;
