import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ConfirmDialog, EmptyState, ErrorState, PriceStat, SkeletonBlock, StatusPill } from "../components/ui";
import { usePaperAccount } from "../hooks/usePaperAccount";
import { fmt, fmtCompact, fmtPct } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type { PaperMarket, MarketTicker } from "../types";

export function PaperAssetsPage() {
  const navigate = useNavigate();
  const balances = useAppStore((state) => state.balances);
  const perpAccount = useAppStore((state) => state.paperPerpAccount);
  const positions = useAppStore((state) => state.paperPositions);
  const pushToast = useAppStore((state) => state.pushToast);
  const [markets, setMarkets] = useState<PaperMarket[]>([]);
  const [tickers, setTickers] = useState<Record<string, MarketTicker>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [resetOpen, setResetOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  usePaperAccount(3000);

  const refreshTickers = useCallback(async () => {
    const marketResponse = await api.get<{ items: PaperMarket[] }>("/paper/markets");
    setMarkets(marketResponse.items);
    const entries = await Promise.all(
      marketResponse.items.map(async (market) => {
        try {
          const ticker = await api.get<MarketTicker>(`/paper/markets/${encodeURIComponent(market.symbol)}/ticker`);
          return [market.symbol, ticker] as const;
        } catch {
          return undefined;
        }
      }),
    );
    const map: Record<string, MarketTicker> = {};
    entries.forEach((entry) => {
      if (entry) map[entry[0]] = entry[1];
    });
    setTickers(map);
  }, []);

  useEffect(() => {
    void refreshTickers()
      .catch((reason) => setError(reason instanceof Error ? reason.message : "行情读取失败"))
      .finally(() => setLoading(false));
  }, [refreshTickers]);

  const spotTotal = useMemo(() => {
    const priceOf = (asset: string) => {
      const market = markets.find((item) => item.product_type === "SPOT" && item.base_asset === asset && item.quote_asset === "USDT");
      return market ? Number(tickers[market.symbol]?.last_price ?? market.reference_price ?? 0) : 0;
    };
    return balances.reduce((sum, item) => {
      if (item.asset === "USDT") return sum + Number(item.available ?? 0) + Number(item.frozen ?? 0);
      return sum + (Number(item.available ?? 0) + Number(item.frozen ?? 0)) * priceOf(item.asset);
    }, 0);
  }, [balances, markets, tickers]);

  const wallet = Number(perpAccount?.wallet_balance ?? 0);
  const upnl = Number(perpAccount?.unrealized_pnl ?? 0);
  const equity = wallet + upnl;
  const positionMargin = positions.reduce((sum, item) => sum + Number(item.isolated_margin ?? 0), 0);
  const orderMargin = Math.max(0, Number(perpAccount?.used_margin ?? 0) - positionMargin);

  const doReset = async () => {
    setBusy(true);
    setError("");
    try {
      await api.post("/paper/account/reset", { reason: "user_assets_reset" });
      pushToast("success", "模拟账户已复位，已开启新的账户运行批次");
      setResetOpen(false);
      await refreshTickers();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "重置失败";
      setError(message);
      pushToast("error", `重置失败：${message}`);
      setResetOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const spotRows = useMemo(() => {
    const rows = balances
      .filter((item) => Number(item.available) > 0 || Number(item.frozen) > 0)
      .map((item) => {
        const market = markets.find((m) => m.product_type === "SPOT" && m.base_asset === item.asset && m.quote_asset === "USDT");
        const price = market ? Number(tickers[market.symbol]?.last_price ?? market.reference_price ?? 0) : item.asset === "USDT" ? 1 : 0;
        const total = Number(item.available ?? 0) + Number(item.frozen ?? 0);
        return { ...item, market, price, valuation: total * price };
      })
      .sort((a, b) => b.valuation - a.valuation);
    if (rows.length === 0) {
      const usdt = balances.find((item) => item.asset === "USDT");
      if (usdt) return [{ ...usdt, market: undefined, price: 1, valuation: Number(usdt.available ?? 0) + Number(usdt.frozen ?? 0) }];
    }
    return rows;
  }, [balances, markets, tickers]);

  return (
    <>
      <div className="space-y-3">
        <section className="panel rounded-2xl p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="font-display text-xl text-white">模拟资产</h1>
              <p className="mt-1 text-xs text-slate-500">现货与 USDT 永续分别维护独立模拟账户；均不涉及真实资金。</p>
            </div>
            <button
              type="button"
              onClick={() => setResetOpen(true)}
              disabled={busy}
              className="rounded-xl border border-amber-300/20 bg-amber-400/10 px-3 py-2 text-xs text-amber-100 hover:bg-amber-400/20 disabled:opacity-50"
            >
              复位模拟账户
            </button>
          </div>
          {error ? <div className="mt-3 rounded-xl bg-rose-400/10 px-3 py-2 text-xs text-rose-200">{error}</div> : null}
        </section>

        <div className="grid gap-3 xl:grid-cols-[1.25fr_0.75fr]">
          <section className="panel rounded-2xl p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-display text-base text-slate-100">现货账户</h2>
              <PriceStat label="总估值（USDT）" value={loading ? "..." : `${fmtCompact(spotTotal, 2)}`} />
            </div>
            <div className="mt-3 overflow-x-auto scrollbar">
              {loading ? (
                <div className="space-y-2">
                  {Array.from({ length: 4 }).map((_, index) => (
                    <SkeletonBlock key={index} className="h-10" />
                  ))}
                </div>
              ) : spotRows.length === 0 ? (
                <EmptyState compact title="暂无资产数据" />
              ) : (
                <table className="w-full min-w-[640px] text-left text-xs">
                  <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                    <tr>
                      <th className="pb-2">资产</th>
                      <th className="pb-2 text-right">可用</th>
                      <th className="pb-2 text-right">冻结</th>
                      <th className="pb-2 text-right">估值（USDT）</th>
                      <th className="pb-2 text-right">去交易</th>
                    </tr>
                  </thead>
                  <tbody>
                    {spotRows.map((item) => (
                      <tr key={item.asset} className="border-t border-white/5">
                        <td className="py-2 font-medium text-slate-100">{item.asset}</td>
                        <td className="py-2 text-right font-mono text-slate-200">{fmt(item.available, item.asset === "USDT" ? 2 : 4)}</td>
                        <td className="py-2 text-right font-mono text-slate-400">{fmt(item.frozen, item.asset === "USDT" ? 2 : 4)}</td>
                        <td className="py-2 text-right font-mono text-slate-200">{fmt(item.valuation, 2)}</td>
                        <td className="py-2 text-right">
                          {item.market ? (
                            <button
                              type="button"
                              onClick={() => navigate(`/paper/trade/${item.market?.symbol}`)}
                              className="rounded-lg bg-cyan-400/12 px-2.5 py-1 text-[11px] text-cyan-100 hover:bg-cyan-400/20"
                            >
                              {item.market.symbol} →
                            </button>
                          ) : (
                            <span className="text-[11px] text-slate-600">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <p className="mt-3 text-[11px] text-slate-500">估值使用各现货市场最新成交价（或参考价）计算；USDT 按 1:1。</p>
          </section>

          <section className="panel rounded-2xl p-4">
            <h2 className="font-display text-base text-slate-100">永续合约账户</h2>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <AccountMetric label="账户权益" value={`${fmt(equity, 2)}`} highlight />
              <AccountMetric label="钱包余额" value={fmt(wallet, 2)} />
              <AccountMetric label="可用保证金" value={fmt(perpAccount?.available_margin, 2)} />
              <AccountMetric label="已用保证金" value={fmt(perpAccount?.used_margin, 2)} />
              <AccountMetric label="持仓保证金" value={fmt(positionMargin, 2)} />
              <AccountMetric label="订单保证金" value={fmt(orderMargin, 2)} />
              <AccountMetric label="未实现盈亏" value={fmt(upnl, 2)} tone={upnl > 0 ? "buy" : upnl < 0 ? "sell" : "neutral"} />
              <AccountMetric label="已实现盈亏" value={fmt(perpAccount?.realized_pnl, 2)} tone={Number(perpAccount?.realized_pnl ?? 0) > 0 ? "buy" : Number(perpAccount?.realized_pnl ?? 0) < 0 ? "sell" : "neutral"} />
              <AccountMetric label="累计手续费" value={fmt(perpAccount?.total_fees, 2)} />
              <AccountMetric label="当前仓位数" value={String(positions.length)} />
            </div>
            <div className="mt-4 rounded-xl bg-white/5 px-3 py-2.5 text-[11px] leading-5 text-slate-500">
              <div className="mb-1.5 font-medium text-slate-300">说明</div>
              订单保证金 = 已用保证金 − 持仓保证金（由前端按当前仓位估算）。持仓保证金 = 各仓位逐仓保证金之和。
            </div>
            <div className="mt-3 flex items-center gap-2">
              <StatusPill tone="info">USDT 本位</StatusPill>
              <StatusPill tone="neutral">单向持仓 · 逐仓</StatusPill>
            </div>
          </section>
        </div>
      </div>

      <ConfirmDialog
        open={resetOpen}
        title="复位模拟账户"
        danger
        busy={busy}
        confirmText={busy ? "复位中..." : "确认复位"}
        message={
          <>
            当前挂单、仓位和本轮收益将清空，历史记录仍保留在旧运行批次中。
            <div className="mt-2 text-xs text-slate-500">现货 USDT 与独立合约钱包各恢复为 100,000,000 USDT。</div>
          </>
        }
        onConfirm={() => void doReset()}
        onCancel={() => setResetOpen(false)}
      />
    
    </>
  );
}

function AccountMetric({ label, value, tone = "neutral", highlight = false }: { label: string; value: string; tone?: "buy" | "sell" | "neutral"; highlight?: boolean }) {
  const color = tone === "buy" ? "text-emerald-300" : tone === "sell" ? "text-rose-300" : highlight ? "text-cyan-200" : "text-slate-200";
  return (
    <div className="ui-metric">
      <div className="ui-metric-label">{label}</div>
      <div className={`ui-metric-value truncate ${color}`}>{value}</div>
    </div>
  );
}
