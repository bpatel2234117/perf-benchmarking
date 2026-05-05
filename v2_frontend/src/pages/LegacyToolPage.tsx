import React from 'react';
import { Link, useParams } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { titleForSlug } from '../navigation/systemToolsConfig';

/** Placeholder for tools still backed by Streamlit `dashboard.py` until ported to React. */
const LegacyToolPage: React.FC = () => {
  const { slug = '' } = useParams<{ slug: string }>();
  const title = titleForSlug(slug);
  const legacyBase = (import.meta.env.VITE_LEGACY_DASHBOARD_URL as string | undefined)?.replace(/\/$/, '') || '';

  return (
    <div className="animate-in dashboard-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <h2 style={{ color: 'var(--text-primary)', fontSize: '1.35rem', fontWeight: 700, margin: 0 }}>
        {title}
      </h2>
      <div className="glass-panel" style={{ padding: '1.25rem 1.35rem', lineHeight: 1.65, maxWidth: '40rem', color: 'var(--text-secondary)' }}>
        <p style={{ marginBottom: '0.75rem', color: 'var(--text-primary)' }}>
          This workspace is exposed in React for navigation parity with System Tools. Behaviour still lives in the Streamlit dashboard unless a dedicated API exists here.
        </p>
        {legacyBase ? (
          <>
            <a
              href={legacyBase}
              target="_blank"
              rel="noreferrer noopener"
              className="btn"
              style={{ display: 'inline-flex', alignItems: 'center', gap: '0.5rem', textDecoration: 'none', marginTop: '0.25rem', fontWeight: 600 }}
            >
              <ExternalLink size={16} aria-hidden /> Open legacy dashboard (new tab)
            </a>
            <p style={{ fontSize: '0.82rem', marginTop: '0.85rem' }}>
              Tip: set <code style={{ fontSize: '0.85em' }}>VITE_LEGACY_DASHBOARD_URL</code> in <code style={{ fontSize: '0.85em' }}>v2_frontend/.env.local</code> (run <code style={{ fontSize: '0.85em' }}>python3 dashboard.py</code> or deploy URL).
            </p>
          </>
        ) : (
          <p style={{ fontSize: '0.82rem', marginBottom: 0 }}>
            Configure <strong style={{ fontWeight: 600, color: 'var(--text-primary)' }}>VITE_LEGACY_DASHBOARD_URL</strong> (example:{' '}
            <code style={{ fontSize: '0.85em' }}>http://127.0.0.1:8501</code>) to open Streamlit beside this SPA.
          </p>
        )}
      </div>
      <Link to="/" style={{ fontSize: '0.88rem', color: 'var(--primary-color)', fontWeight: 600 }}>
        ← Back to Dashboard
      </Link>
    </div>
  );
};

export default LegacyToolPage;
