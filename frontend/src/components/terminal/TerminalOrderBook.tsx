import { useCenteredBookScroll } from "../../hooks/useCenteredBookScroll";
import { memo, useRef, useState } from "react";
import { mergeLevels } from "../../lib/orderbook";
import type { Level } from "../../types";
import { RelativeTime } from "../ui";

const ROW_GRID = "grid-cols-[minmax(9ch,1fr)_minmax(7ch,0.8fr)_minmax(8ch,0.9fr)]";

const formatBookNumber = (value: number, digits: number, compact: boolean) => {
  if (!compact || Math.abs(value) < 1000) {
    return value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  const units = [
    { divisor: 1_000_000_000, suffix: "B" },
    { divisor: 1_000_000, suffix: "M" },
    { divisor: 1_000, suffix: "K" },
  ];
  const unit = units.find((item) => Math.abs(value) >= item.divisor) ?? units[units.length - 1];
  return `${(value / unit.divisor).toLocaleString("en-US", { maximumFractionDigits: 2 })}${unit.suffix}`;
};

const estimateScale = (levels: Level[]) => {
  const notionals = levels
    .slice(0, Math.min(levels.length, 8))
    .map(([price, quantity]) => Number(price) * Number(quantity))
    .filter((value) => Number.isFinite(value) && value > 0);
  const avg = notionals.length > 0 ? notionals.reduce((sum, value) => sum + value, 0) / notionals.length : 1;
  const maxRow = Math.max(...levels.map(([price, quantity]) => Number(price) * Number(quantity)).filter((value) => Number.isFinite(value) && value > 0), 0);
  return Math.max(maxRow, avg * Math.max(levels.length, 8), 1);
};

const BookRow = memo(function BookRow({
  side,
  price,
  priceLabel,
  quantityLabel,
  cumulativeLabel,
  depthPct,
  myOrder,
  onSelectPrice,
}: {
  side: "buy" | "sell";
  price: string;
  priceLabel: string;
  quantityLabel: string;
  cumulativeLabel: string;
  depthPct: number;
  myOrder?: boolean;
  onSelectPrice: (price: string) => void;
}) {
  return (
    <button
      type="button"
      data-book-row={side}
      onClick={() => onSelectPrice(price)}
      onDoubleClick={() => void navigator.clipboard?.writeText(`${priceLabel} ${quantityLabel}`)}
      className={`${myOrder ? "hl-book-active-order" : ""} book-row-stable relative grid ${ROW_GRID} w-full items-center gap-1 overflow-hidden rounded px-1.5 py-[2px] font-mono text-[11px] leading-4 tabular-nums ${
        side === "buy" ? "text-emerald-300/90 hover:text-emerald-200" : "text-rose-300/90 hover:text-rose-200"
      }`}
      title={`价格 ${priceLabel} · 数量 ${quantityLabel} · 累计 ${cumulativeLabel}`}
    >
      <span className={`pointer-events-none absolute inset-y-0 right-0 ${side === "buy" ? "bg-emerald-400/8" : "bg-rose-500/8"}`} style={{ width: `${Math.min(100, depthPct)}%` }} />
      <span className="num-fixed num-left relative justify-self-start">{priceLabel}</span>
      <span className="num-fixed relative justify-self-end text-slate-200">{quantityLabel}</span>
      <span className="num-fixed relative justify-self-end text-slate-500">{cumulativeLabel}</span>
    </button>
  );
});

function LevelList({
  levels,
  side,
  cumulative,
  depthScale,
  priceDigits,
  qtyDigits,
  onSelectPrice,
  myOrderPrices,
  compactNumbers,
}: {
  levels: Level[];
  side: "buy" | "sell";
  cumulative: number[];
  depthScale: number;
  priceDigits: number;
  qtyDigits: number;
  onSelectPrice: (price: string) => void;
  myOrderPrices?: string[];
  compactNumbers: boolean;
}) {
  return (
    <div className="space-y-px">
      {levels.map(([price, quantity], index) => {
        const priceNumber = Number(price);
        const rowNotional = priceNumber * Number(quantity);
        const depthPct = Math.min(100, (rowNotional / depthScale) * 100);
        return (
          <BookRow
            key={`${side}-${price}`}
            side={side}
            price={price}
            priceLabel={formatBookNumber(priceNumber, priceDigits, compactNumbers)}
            quantityLabel={formatBookNumber(Number(quantity), qtyDigits, compactNumbers)}
            cumulativeLabel={formatBookNumber(Number(cumulative[index]), qtyDigits, compactNumbers)}
            depthPct={depthPct}
            myOrder={myOrderPrices?.some((item) => Number(item) === priceNumber)}
            onSelectPrice={onSelectPrice}
          />
        );
      })}
    </div>
  );
}

export function TerminalOrderBook({
  symbol,
  bids,
  asks,
  lastPrice,
  lastTradeAt,
  lastSide,
  depth,
  mergeTicks,
  tickSize,
  priceDigits,
  qtyDigits,
  onDepthChange,
  onMergeChange,
  onSelectPrice,
  myOrderPrices = [],
  heightClass = "h-[720px]",
}: {
  symbol: string;
  bids: Level[];
  asks: Level[];
  lastPrice?: string | number | null;
  lastTradeAt?: number;
  lastSide?: string | null;
  updatedAt?: number;
  depth: number;
  mergeTicks: number;
  tickSize: number;
  priceDigits: number;
  qtyDigits: number;
  onDepthChange: (value: number) => void;
  onMergeChange: (value: number) => void;
  onSelectPrice: (price: string) => void;
  myOrderPrices?: string[];
  heightClass?: string;
}) {
  const depthScaleRef = useRef(0);
  const asksScroll = useCenteredBookScroll(false, `${symbol}:${depth}:${mergeTicks}`);
  const bidsScroll = useCenteredBookScroll(false, `${symbol}:${depth}:${mergeTicks}`);
  const [compactNumbers, setCompactNumbers] = useState(false);
  const mergedBids = mergeTicks <= 1 ? bids : mergeLevels(bids, "buy", tickSize, mergeTicks);
  const mergedAsks = mergeTicks <= 1 ? asks : mergeLevels(asks, "sell", tickSize, mergeTicks);
  const sideDepth = Math.max(10, depth);
  const visibleBids = [...mergedBids].sort((a, b) => Number(b[0]) - Number(a[0])).slice(0, sideDepth);
  const visibleAsks = [...mergedAsks].sort((a, b) => Number(a[0]) - Number(b[0])).slice(0, sideDepth);
  // 买卖两列均以最优报价为首，向下展开更深档位。

  // Price labels use the raw BBO, never rounded/merged display buckets.
  const bestBid = Math.max(0, ...bids.map(([price]) => Number(price)).filter(Number.isFinite));
  const rawAsks = asks.map(([price]) => Number(price)).filter((price) => Number.isFinite(price) && price > 0);
  const bestAsk = rawAsks.length ? Math.min(...rawAsks) : 0;
  const mid = bestBid > 0 && bestAsk > 0 ? (bestBid + bestAsk) / 2 : 0;
  const spread = bestBid > 0 && bestAsk > 0 ? bestAsk - bestBid : 0;
  const spreadPct = mid > 0 ? (spread / mid) * 100 : 0;
  const lastPriceNumber = Number(lastPrice ?? 0);
  const lastClass =
    lastSide === "sell" ? "text-rose-300" : lastSide === "buy" ? "text-emerald-300" : "text-slate-100";

  let bidCumulative = 0;
  const bidCum = visibleBids.map(([, qty]) => {
    bidCumulative += Number(qty || 0);
    return bidCumulative;
  });
  let askCumulative = 0;
  const askCum = visibleAsks.map(([, qty]) => {
    askCumulative += Number(qty || 0);
    return askCumulative;
  });
  const bidTotal = bidCumulative;
  const askTotal = askCumulative;
  const bidPct = bidTotal + askTotal > 0 ? (bidTotal / (bidTotal + askTotal)) * 100 : 50;

  const nextScale = estimateScale([...visibleBids, ...visibleAsks]);
  if (depthScaleRef.current <= 0 || nextScale > depthScaleRef.current * 2 || nextScale < depthScaleRef.current * 0.5) {
    depthScaleRef.current = nextScale;
  }
  const depthScale = depthScaleRef.current;


  return (
    <section className={`panel hl-book-panel flex min-h-0 min-w-0 flex-col rounded-2xl p-2 ${heightClass}`}>
      <div className="hl-book-head mb-1.5 flex shrink-0 items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-1.5">
          <span className="border-b-2 border-emerald-300 px-1 pb-1 text-xs text-slate-100">Order Book</span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {[10, 30, 50].map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onDepthChange(value)}
              className={`rounded px-1.5 py-0.5 text-[10px] ${depth === value ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
            >
              {value}
            </button>
          ))}
          <span className="mx-0.5 h-3 w-px bg-white/10" />
          {[1, 5, 10].map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onMergeChange(value)}
              className={`rounded px-1.5 py-0.5 text-[10px] ${mergeTicks === value ? "bg-white/14 text-white" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
            >
              {value === 1 ? "原始" : `x${value}`}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setCompactNumbers((value) => !value)}
            className={`rounded px-1.5 py-0.5 text-[10px] ${compactNumbers ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
            title="切换盘口数字的完整/缩写显示"
          >
            {compactNumbers ? "完整" : "缩写"}
          </button>
        </div>
      </div>

      <div className="my-1 shrink-0 rounded-lg border border-white/8 bg-slate-950/40 px-2 py-1">
        <button type="button" disabled={mid <= 0} onClick={() => onSelectPrice(String(mid))}
          className="flex w-full items-center justify-between gap-2 text-xs text-slate-400" title="当前原始买一与卖一的均值；点击填入委托价格">
          <span>盘口中价</span><span className="num-fixed num-price text-right font-mono text-sm font-semibold text-slate-100">{mid > 0 ? formatBookNumber(mid, priceDigits, false) : "—"}</span>
        </button>
        <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-slate-500">
          <span>最近成交 {lastTradeAt ? <RelativeTime ts={lastTradeAt} /> : ""}</span>
          <button type="button" disabled={lastPriceNumber <= 0} onClick={() => onSelectPrice(String(lastPriceNumber))}
            className={`num-fixed num-price text-right font-mono text-xs ${lastClass}`} title="最近一笔成交价格，可能已较久未成交；点击填入委托价格">
            {lastPriceNumber > 0 ? formatBookNumber(lastPriceNumber, priceDigits, false) : "暂无成交"}
          </button>
        </div>
        <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-slate-500">
          <span>
            买盘 <span className="num-fixed num-short">{bidPct.toFixed(0)}%</span>
          </span>
          <span className="min-w-0 text-center">
            Spread <span className="num-fixed num-price">{spread > 0 ? formatBookNumber(spread, priceDigits, compactNumbers) : "-"}</span> ·{" "}
            <span className="num-fixed num-short">{spreadPct > 0 ? `${spreadPct.toFixed(3)}%` : "-"}</span>
          </span>
          <span>
            卖盘 <span className="num-fixed num-short">{(100 - bidPct).toFixed(0)}%</span>
          </span>
        </div>
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-rose-500/20">
          <div className="h-full rounded-full bg-emerald-400/60" style={{ width: `${bidPct}%` }} />
        </div>
      </div>

      <div className="hl-book-sides grid min-h-0 flex-1 grid-cols-2 divide-x divide-white/10">
        {([
          { side: "buy" as const, label: "买盘", levels: visibleBids, cumulative: bidCum, scroll: bidsScroll },
          { side: "sell" as const, label: "卖盘", levels: visibleAsks, cumulative: askCum, scroll: asksScroll },
        ]).map(({ side, label, levels, cumulative, scroll }) => (
          <div key={side} className="flex min-h-0 min-w-0 flex-col px-1">
            <div className={`py-1 text-xs ${side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{label}<span className="ml-2 text-[10px] text-slate-500">{side === "buy" ? "买一在上" : "卖一在上"}</span></div>
            <div className={`grid ${ROW_GRID} shrink-0 gap-1 px-1.5 pb-1 text-[9px] text-slate-500`}><span>价格</span><span className="text-right">数量</span><span className="text-right">累计</span></div>
            <div {...scroll} aria-label={`${label}滚动区域`} className="scrollbar min-h-0 flex-1 overflow-y-auto">
              {levels.length ? <LevelList levels={levels} side={side} cumulative={cumulative} depthScale={depthScale} priceDigits={priceDigits} qtyDigits={qtyDigits} onSelectPrice={onSelectPrice} myOrderPrices={myOrderPrices} compactNumbers={compactNumbers} /> : <div className="py-8 text-center text-xs text-slate-500">暂无{label}报价</div>}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
