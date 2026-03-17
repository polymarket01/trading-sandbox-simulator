import { bjTime, fmt, sideColor } from "../lib/format";
import type { TradeItem } from "../types";

export function TradesPanel({
  items,
  priceDigits,
  quantityDigits,
}: {
  items: TradeItem[];
  priceDigits: number;
  quantityDigits: number;
}) {
  return (
    <section className="panel flex h-[760px] min-h-0 flex-col rounded-3xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-display text-lg">TAS / 最新成交</h3>
        <span className="text-xs text-slate-500">K 线直接基于这些成交聚合</span>
      </div>
      <div className="grid grid-cols-[0.9fr_1fr_0.9fr] gap-2 px-2 pb-2 text-xs uppercase tracking-[0.18em] text-slate-500">
        <span>时间</span>
        <span className="text-right">价格</span>
        <span className="text-right">数量</span>
      </div>
      <div className="scrollbar flex-1 space-y-1 overflow-auto pr-1 font-mono tabular-nums">
        {items.map((item) => (
          <div key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className="grid grid-cols-[0.9fr_1fr_0.9fr] gap-2 rounded-xl px-2 py-1.5 text-sm">
            <span className="text-slate-400">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</span>
            <span className={`text-right ${sideColor(item.side ?? item.taker_side)}`}>{fmt(item.price, priceDigits)}</span>
            <span className="text-right text-slate-200">{fmt(item.quantity, quantityDigits)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
