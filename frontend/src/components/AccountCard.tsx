import { fmt } from "../lib/format";
import type { BalanceItem, MarketTicker } from "../types";

export function AccountCard({ balances, ticker }: { balances: BalanceItem[]; ticker?: MarketTicker }) {
  const lookup = Object.fromEntries(balances.map((item) => [item.asset, item]));
  const mid = Number(ticker?.mid_price ?? 0);
  const currentBaseAsset = ticker?.symbol.replace("USDT", "");
  const visibleBalances = [...balances].sort((left, right) => {
    const rank = (asset: string) => {
      if (asset === "USDT") return 0;
      if (asset === "BTC") return 1;
      return 2;
    };
    return rank(left.asset) - rank(right.asset) || left.asset.localeCompare(right.asset);
  });
  const estimate =
    Number(lookup.USDT?.available ?? 0) +
    Number(lookup.USDT?.frozen ?? 0) +
    (currentBaseAsset
      ? (Number(lookup[currentBaseAsset]?.available ?? 0) + Number(lookup[currentBaseAsset]?.frozen ?? 0)) * mid
      : 0);

  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-display text-lg">账户概览</h3>
        <span className="text-xs text-slate-500">spot_manual_user</span>
      </div>
      <div className="space-y-3">
        {visibleBalances.map((item) => {
          const asset = item.asset;
          return (
            <div key={asset} className="rounded-2xl bg-white/5 px-3 py-3">
              <div className="mb-1 flex items-center justify-between text-sm text-slate-400">
                <span>{asset}</span>
                <span>{fmt(Number(item?.available ?? 0) + Number(item?.frozen ?? 0), 4)}</span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-500">可用 {fmt(item?.available, 4)}</span>
                <span className="text-slate-500">冻结 {fmt(item?.frozen, 4)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-4 rounded-2xl bg-cyan-400/10 px-3 py-3">
        <div className="text-xs uppercase tracking-[0.18em] text-cyan-200/70">当前页可估资产</div>
        <div className="mt-2 font-display text-2xl text-white">{fmt(estimate, 2)} USDT</div>
      </div>
    </section>
  );
}
