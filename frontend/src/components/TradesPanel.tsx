import { bjTime, fmt, sideColor } from "../lib/format";
import { sourceLabel, sourcePillClass } from "../lib/source";
import type { TradeItem } from "../types";

const TRADE_ROW_GRID = "grid-cols-[minmax(11ch,1fr)_minmax(9ch,1.1fr)_minmax(8ch,1fr)_minmax(6ch,0.8fr)]";

export function TradesPanel({
  items,
  priceDigits,
  quantityDigits,
  heightClass = "h-[204px]",
}: {
  items: TradeItem[];
  priceDigits: number;
  quantityDigits: number;
  heightClass?: string;
}) {
  return (
    <section className={`panel flex min-h-0 flex-col rounded-2xl p-3 ${heightClass}`}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="font-display text-base">最新成交</h3>
        <span className="text-xs text-slate-500">{items.length} 笔</span>
      </div>
      <div className={`grid ${TRADE_ROW_GRID} gap-2 px-2 pb-2 text-xs uppercase tracking-[0.18em] text-slate-500`}>
        <span>时间</span>
        <span className="text-right">价格</span>
        <span className="text-right">数量</span>
        <span className="text-right">来源</span>
      </div>
      <div className="scrollbar flex-1 space-y-1 overflow-auto pr-1 font-mono tabular-nums">
        {items.map((item) => (
          <div key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className={`grid ${TRADE_ROW_GRID} gap-2 rounded-lg px-2 py-1.5 text-sm`}>
            <span className="num-fixed num-left shrink-0 whitespace-nowrap text-slate-400">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</span>
            <span className={`num-fixed num-price justify-self-end ${sideColor(item.side ?? item.taker_side)}`}>{fmt(item.price, priceDigits)}</span>
            <span className="num-fixed num-qty justify-self-end text-slate-200">{fmt(item.quantity, quantityDigits)}</span>
            <span className="text-right">
              <span className={`inline-flex max-w-full justify-end truncate rounded-md border px-1.5 py-0.5 text-[11px] leading-4 ${sourcePillClass(item.source)}`}>
                {sourceLabel(item.source)}
              </span>
            </span>
          </div>
        ))}
        {items.length === 0 && (
          <div className="rounded-xl bg-slate-950/30 px-3 py-6 text-center text-sm text-slate-500">暂无可见成交</div>
        )}
      </div>
    </section>
  );
}
