import { useCallback, useEffect } from "react";
import { api } from "../api/client";
import { useAppStore } from "../store/useAppStore";
import type { PaperAccountResponse, PaperPerpAccountResponse } from "../types";

export function usePaperAccount(intervalMs = 3000) {
  const setBalances = useAppStore((state) => state.setBalances);
  const setPaperPerpAccount = useAppStore((state) => state.setPaperPerpAccount);
  const setPaperPositions = useAppStore((state) => state.setPaperPositions);
  const setPaperAccountMeta = useAppStore((state) => state.setPaperAccountMeta);

  const refresh = useCallback(async () => {
    const [accountResponse, perpResponse] = await Promise.all([
      api.get<PaperAccountResponse>("/paper/account"),
      api.get<PaperPerpAccountResponse>("/paper/account/perp").catch(() => undefined),
    ]);
    setBalances(accountResponse.spot.balances);
    setPaperAccountMeta({
      user_id: accountResponse.user.user_id,
      username: accountResponse.user.username,
      role: accountResponse.user.role,
      is_active: accountResponse.user.is_active,
      account_epoch: accountResponse.user.account_epoch,
      account_run_id: accountResponse.user.account_run_id ?? null,
      created_at: accountResponse.user.created_at,
    });
    setPaperPerpAccount(accountResponse.perp.account);
    if (perpResponse) {
      setPaperPerpAccount(perpResponse.account);
      setPaperPositions(perpResponse.positions ?? []);
    }
  }, [setBalances, setPaperAccountMeta, setPaperPerpAccount, setPaperPositions]);

  useEffect(() => {
    void refresh().catch(() => undefined);
    const timer = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refresh]);
}
