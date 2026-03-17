import { useEffect, useRef, useState } from "react";
import type { Level } from "../types";

export function OrderbookPanel({
  symbol,
  bids,
  asks,
  depth,
  mergeTicks,
  tickSize,
  priceDigits,
  qtyDigits,
  onDepthChange,
  onMergeChange,
  onSelectPrice,
}: {
  symbol: string;
  bids: Level[];
  asks: Level[];
  depth: number;
  mergeTicks: number;
  tickSize: number;
  priceDigits: number;
  qtyDigits: number;
  onDepthChange: (value: number) => void;
  onMergeChange: (value: number) => void;
  onSelectPrice: (price: string) => void;
}) {
  const [flashKey, setFlashKey] = useState(0);
  const asksScrollRef = useRef<HTMLDivElement | null>(null);
  const bidsScrollRef = useRef<HTMLDivElement | null>(null);
  const mergedBids = mergeLevels(bids, "buy", tickSize * mergeTicks);
  const mergedAsks = mergeLevels(asks, "sell", tickSize * mergeTicks);
  const sideDepth = Math.max(10, Math.floor(depth / 2));

  useEffect(() => {
    const asksNode = asksScrollRef.current;
    if (asksNode) {
      asksNode.scrollTop = Math.max(asksNode.scrollHeight - asksNode.clientHeight, 0);
    }
    const bidsNode = bidsScrollRef.current;
    if (bidsNode) {
      bidsNode.scrollTop = 0;
    }
  }, [symbol, depth, mergeTicks]);

  const renderRows = (levels: Level[], side: "buy" | "sell") => {
    let cumulative = 0;
    return levels.map(([price, quantity]) => {
      cumulative += Number(quantity);
      return (
        <button
          key={`${side}-${price}-${flashKey}`}
          onClick={() => onSelectPrice(price)}
          className={`grid w-full grid-cols-[minmax(0,1fr)_88px_88px] items-center gap-3 rounded-xl px-3 py-1.5 text-sm font-mono tabular-nums transition hover:bg-white/6 ${
            side === "buy" ? "text-emerald-300" : "text-rose-300"
          }`}
        >
          <span className="text-left">{Number(price).toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits })}</span>
          <span className="text-right text-slate-200">{Number(quantity).toLocaleString("zh-CN", { minimumFractionDigits: qtyDigits, maximumFractionDigits: qtyDigits })}</span>
          <span className="text-right text-slate-400">{Number(cumulative).toLocaleString("zh-CN", { minimumFractionDigits: qtyDigits, maximumFractionDigits: qtyDigits })}</span>
        </button>
      );
    });
  };

  return (
    <section className="panel flex h-[760px] min-h-0 flex-col rounded-3xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="font-display text-lg">{depth} 档订单簿</h3>
          <p className="mt-1 text-xs text-slate-500">显示支持 20 / 50 档，盘口可按 tick 合并显示。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {[20, 50].map((value) => (
            <button key={value} onClick={() => onDepthChange(value)} className={`rounded-full px-3 py-1 text-xs ${depth === value ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300"}`}>
              {value} 档
            </button>
          ))}
          {[1, 5, 10].map((value) => (
            <button key={value} onClick={() => onMergeChange(value)} className={`rounded-full px-3 py-1 text-xs ${mergeTicks === value ? "bg-white/14 text-white" : "bg-white/5 text-slate-300"}`}>
              {value === 1 ? "原始" : `合并 x${value}`}
            </button>
          ))}
          <button className="text-xs text-slate-400 hover:text-white" onClick={() => setFlashKey((value) => value + 1)}>
            重新高亮
          </button>
        </div>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_88px_88px] gap-3 px-3 pb-2 text-xs uppercase tracking-[0.18em] text-slate-500">
        <span className="text-left">价格</span>
        <span className="text-right">数量</span>
        <span className="text-right">累计</span>
      </div>
      <div ref={asksScrollRef} className="scrollbar flex flex-1 min-h-0 flex-col overflow-y-scroll pr-1" style={{ scrollbarGutter: "stable" }}>
        <div className="mt-auto space-y-1">{renderRows([...mergedAsks].slice(0, sideDepth).reverse(), "sell")}</div>
      </div>
      <div className="my-3 rounded-2xl bg-white/5 px-4 py-3 text-center text-sm text-slate-300">
        点击盘口价格可直接带入下单价格
      </div>
      <div ref={bidsScrollRef} className="scrollbar flex-1 min-h-0 overflow-y-scroll pr-1" style={{ scrollbarGutter: "stable" }}>
        <div className="space-y-1">{renderRows(mergedBids.slice(0, sideDepth), "buy")}</div>
      </div>
    </section>
  );
}

function mergeLevels(levels: Level[], side: "buy" | "sell", groupSize: number): Level[] {
  if (groupSize <= 0) return levels;
  const buckets = new Map<number, number>();
  for (const [priceText, quantityText] of levels) {
    const price = Number(priceText);
    const quantity = Number(quantityText);
    const bucket = side === "buy" ? Math.floor(price / groupSize) * groupSize : Math.ceil(price / groupSize) * groupSize;
    buckets.set(bucket, (buckets.get(bucket) ?? 0) + quantity);
  }
  return Array.from(buckets.entries())
    .sort((a, b) => (side === "buy" ? b[0] - a[0] : a[0] - b[0]))
    .map(([price, quantity]) => [String(price), String(quantity)] as Level);
}
