import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { API_BASE } from '../apiConfig';

/**
 * Current user's role name. Reuses the Navbar's ['me'] query cache so this adds no
 * extra network request. Used to hide actions the backend would 403 (RBAC is enforced
 * server-side; this is just so unauthorized buttons aren't shown).
 */
export function useUserRole(token: string): string {
  const { data } = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const res = await axios.get(`${API_BASE}/api/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      return res.data;
    },
    enabled: !!token,
  });
  return String(data?.role ?? '').trim();
}
