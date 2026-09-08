import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';

export function useLiveMakerSymbols() {
  const session = useAppStore(s => s.authSession);
  const [symbols, setSymbols] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    let disposed = false;
    let timer: number;
    const refresh = async () => {
      let active: string[] = [];
      try {
        if (session?.role === 'admin') {
          // Existing catalog works without restarting an already running backend.
          const result = await api.get<{ items: { symbol: string; ladder?: { enabled?: boolean } }[] }>('/admin/liquidity/makers', session.api_key);
          active = result.items.filter(x => x.ladder?.enabled).map(x => x.symbol);
          const legacy = await Promise.all(result.items.filter(x => !x.ladder).map(async x => {
            try {
              const status = await api.get<{ running?: boolean }>(`/markets/${encodeURIComponent(x.symbol)}/maker-instance`);
              return status.running ? x.symbol : null;
            } catch { return null; }
          }));
          active.push(...legacy.filter((x): x is string => x !== null));
        } else {
          const result = await api.get<{ items: { symbol: string; enabled: boolean }[] }>('/markets/maker-status');
          active = result.items.filter(x => x.enabled).map(x => x.symbol);
        }
      } catch { active = []; }
      if (!disposed) { setSymbols(new Set(active)); timer = window.setTimeout(refresh, 10000); }
    };
    void refresh();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [session?.role, session?.api_key]);
  return symbols;
}
