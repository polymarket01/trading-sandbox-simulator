import { fmt, signedNumber } from "../../lib/format";
import { marketStatusLabel } from "../../lib/paper";
import type { TerminalMarketInfo } from "./types";

export function MarketSidebar({
  markets,
  currentSymbol,
  onSelect,
}: {
  markets: TerminalMarketInfo[];
  currentSymbol: string;
  onSelect: (symbol: string) => void;
}) {
  const spot = markets.filter((item) => item.product_type === "SPOT");
  const perp = markets.filter((item) => item.product_type === "PERP");
  const renderGroup = (title: string, items: TerminalMarketInfo[]) =>
    items.length === 0 ? null : (
      <div>
        <div className="px-2 pb-1 pt-2 text-[10px] uppercase tracking-[0.18em] text-slate-500">{title}</div>
        <div className="space-y-px">
          {items.map((item) => {
            const change = Number(item.change_24h_pct ?? 0);
            const active = item.symbol === currentSymbol;
            const blocked = Boolean(item.paper_status && item.paper_status !== "TRADING");
            return (
              <button
                key={item.symbol}
                type="button"
                onClick={() => onSelect(item.symbol)}
                className={`flex w-full items-center justify-between gap-1.5 rounded-lg px-2 py-1.5 text-left text-xs transition ${
                  active ? "bg-cyan-400/12 text-cyan-100" : "text-slate-300 hover:bg-white/6 hover:text-white"
                }`}
              >
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${blocked ? "bg-amber-300" : "bg-emerald-400/80"}`} title={blocked ? marketStatusLabel(item.paper_status) : "正常交易"} />
                  <span className="truncate">{item.symbol.replace("USDT", "/USDT")}</span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5 font-mono tabular-nums">
                  <span className="text-slate-400">{fmt(item.last_price ?? item.reference_price, 2)}</span>
                  <span className={`w-[52px] text-right ${change > 0 ? "text-emerald-300" : change < 0 ? "text-rose-300" : "text-slate-500"}`}>
                    {signedNumber(change, 2)}%
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
    );

  return (
    <div className="panel flex max-h-[860px] min-h-0 flex-col overflow-y-auto rounded-2xl p-1.5 scrollbar">
      {renderGroup("现货", spot)}
      {renderGroup("永续", perp)}
      {markets.length === 0 ? <div className="px-2 py-6 text-center text-xs text-slate-500">暂无市场</div> : null}
    </div>
  );
}
