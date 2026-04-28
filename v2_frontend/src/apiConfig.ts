/** FastAPI origin for axios (same as Login / Logs hardcoded localhost). Override with VITE_API_BASE_URL. */
export const API_BASE =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '').trim().replace(/\/$/, '') ||
  'http://localhost:8000'
