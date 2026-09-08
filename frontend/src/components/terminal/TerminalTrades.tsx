import { bjTime, fmt } from "../../lib/format";
import { sourceLabel, sourcePillClass } from "../../lib/source";
import type { TradeItem } from "../../types";

const ROW_GRID = "grid-cols-[minmax(9ch,1fr)_minmax(8ch,0.9fr)_minmax(7ch,0.8fr)_minmax(9ch,0.9fr)]";

export function TerminalTrades({ items, priceDigits, quantityDigits, heightClass = "h-[280px]" }: { items: TradeItem[]; priceDigits: number; quantityDigits: number; heightClass?: string }) {
  return (
    <section className={`panel hl-trades-panel flex min-h-0 min-w-0 flex-col rounded-2xl p-2 ${heightClass}`}>
      <div className="mb-1.5 flex shrink-0 items-center justify-between px-1">
        <h3 className="font-display text-xs text-slate-200">最新成交</h3>
        <span className="text-[10px] text-slate-500">{items.length} 笔</span>
      </div>
      <div className={`grid ${ROW_GRID} shrink-0 gap-1 px-1.5 pb-1 text-[9px] uppercase tracking-[0.14em] text-slate-600`}>
        <span>时间</span>
        <span className="text-right">价格</span>
        <span className="text-right">数量</span>
        <span className="text-right">来源</span>
      </div>
      <div className="scrollbar min-h-0 flex-1 space-y-px overflow-y-auto pr-0.5">
        {items.map((item) => {
          const side = item.side ?? item.taker_side;
          return (
            <div key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className={`grid ${ROW_GRID} items-center gap-1 rounded px-1.5 py-[3px] font-mono text-[11px] tabular-nums`}>
              <span className="num-fixed num-left shrink-0 whitespace-nowrap text-slate-500">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</span>
              <span className={`num-fixed num-price justify-self-end ${side === "buy" ? "text-emerald-300" : side === "sell" ? "text-rose-300" : "text-slate-200"}`}>
                {fmt(item.price, priceDigits)}
              </span>
              <span className="num-fixed num-qty justify-self-end text-slate-300">{fmt(item.quantity, quantityDigits)}</span>
              <span className="min-w-0 justify-self-end">
                <span className={`inline-flex min-w-0 max-w-full justify-end truncate whitespace-nowrap rounded border px-1 py-px text-[9px] leading-3 ${sourcePillClass(item.source)}`}>{sourceLabel(item.source)}</span>
              </span>
            </div>
          );
        })}
        {items.length === 0 ? <div className="px-3 py-8 text-center text-xs text-slate-500">暂无可见成交</div> : null}
      </div>
    </section>
  );
}
