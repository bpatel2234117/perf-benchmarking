/**
 * FastAPI origin for axios.
 * - Unset / empty VITE_API_BASE_URL → same origin (Docker nginx proxies /api → :8000).
 * - Set explicitly for local dev, e.g. http://localhost:8000 or http://10.2.0.93:8000
 */
function resolveApiBase(): string {
  const raw = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
  const configured = raw.trim().replace(/\/$/, '');
  if (configured) return configured;
  if (typeof window !== 'undefined' && window.location?.origin) {
    return window.location.origin;
  }
  return 'http://localhost:8000';
}

export const API_BASE = resolveApiBase();
