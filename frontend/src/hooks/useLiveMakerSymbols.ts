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
        const result = await api.get<{ items: { symbol: string; enabled: boolean }[] }>('/markets/maker-status');
        active = result.items.filter(x => x.enabled).map(x => x.symbol);
      } catch { active = []; }
      if (!disposed) { setSymbols(new Set(active)); timer = window.setTimeout(refresh, 10000); }
    };
    void refresh();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [session?.role, session?.api_key]);
  return symbols;
}
