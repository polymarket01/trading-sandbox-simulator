import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { fmtQuantity } from "../lib/format";
import { mergeLevels } from "../lib/orderbook";
import type { Level, MakerInstancePublicStatus } from "../types";

const ROW_GRID_CLASS = "grid-cols-[minmax(11ch,1fr)_minmax(10ch,max-content)_minmax(12ch,max-content)]";
const SPLIT_ROW_GRID_CLASS = "grid-cols-[minmax(8.5ch,1fr)_minmax(5ch,0.65fr)_minmax(6ch,0.75fr)]";

const estimateRowDepthScale = (levels: Level[], sideDepth: number) => {
  const notionals = levels
    .slice(0, Math.min(levels.length, 5))
    .map(([price, quantity]) => Number(price) * Number(quantity))
    .filter((value) => Number.isFinite(value) && value > 0);
  const averageHeadNotional = notionals.length > 0
    ? notionals.reduce((sum, value) => sum + value, 0) / notionals.length
    : 1;
  const maxRowNotional = Math.max(
    ...levels.map(([price, quantity]) => Number(price) * Number(quantity)).filter((value) => Number.isFinite(value) && value > 0),
    0,
  );
  return Math.max(maxRowNotional, averageHeadNotional * Math.max(sideDepth, levels.length, 1), 1);
};

const OrderbookFreshnessBadge = memo(function OrderbookFreshnessBadge({ updatedAt }: { updatedAt?: number }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const updateAgeMs = updatedAt ? Math.max(0, nowMs - updatedAt) : null;
  const updateAgeLabel = updateAgeMs === null ? "-" : updateAgeMs < 1000 ? `${(updateAgeMs / 1000).toFixed(1)}s` : `${Math.round(updateAgeMs / 1000)}s`;
  const updateFresh = updateAgeMs !== null && updateAgeMs < 1000;
  return (
    <span className={`rounded-lg px-2.5 py-1 text-xs ${updateFresh ? "bg-emerald-400/14 text-emerald-100" : "bg-amber-400/12 text-amber-100"}`}>
      盘口 {updateAgeLabel}
    </span>
  );
});

const OrderbookRow = memo(function OrderbookRow({
  side,
  price,
  rowKey,
  priceLabel,
  quantityLabel,
  cumulativeLabel,
  depthPct,
  compact,
  onSelectPrice,
}: {
  side: "buy" | "sell";
  price: string;
  rowKey: string;
  priceLabel: string;
  quantityLabel: string;
  cumulativeLabel: string;
  depthPct: number;
  compact: boolean;
  onSelectPrice: (price: string) => void;
}) {
  return (
    <button
      data-book-row={side}
      data-price={price}
      data-row-key={rowKey}
      data-depth-pct={depthPct.toFixed(4)}
      onClick={() => onSelectPrice(price)}
      className={`book-row-stable relative grid w-full ${compact ? `${SPLIT_ROW_GRID_CLASS} gap-1 px-1.5 py-1 text-xs` : `${ROW_GRID_CLASS} gap-3 px-3 py-1.5 text-sm`} items-center overflow-hidden rounded-lg font-mono tabular-nums ${
        side === "buy" ? "text-emerald-300" : "text-rose-300"
      }`}
      title={`累计数量 ${cumulativeLabel}`}
    >
      <span
        className={`pointer-events-none absolute inset-y-1 right-0 ${side === "buy" ? "bg-emerald-400/8" : "bg-rose-500/8"}`}
        style={{ width: `${depthPct}%` }}
      />
      <span className={`relative num-fixed num-left ${compact ? "" : "num-price"} justify-self-start`}>{priceLabel}</span>
      <span className={`relative num-fixed ${compact ? "" : "num-qty"} justify-self-end text-slate-200`}>{quantityLabel}</span>
      <span className={`relative num-fixed ${compact ? "" : "num-qty"} justify-self-end text-slate-400`}>{cumulativeLabel}</span>
    </button>
  );
});

function OrderbookPanelComponent({
  symbol,
  bids,
  asks,
  lastPrice,
  lastSide,
  updatedAt,
  depth,
  mergeTicks,
  tickSize,
  priceDigits,
  qtyDigits,
  onDepthChange,
  onMergeChange,
  onSelectPrice,
  onMakerStatusChange,
  apiPrefix = "",
  heightClass = "h-[640px]",
  layout = "stacked",
}: {
  symbol: string;
  bids: Level[];
  asks: Level[];
  lastPrice?: string | number | null;
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
  onMakerStatusChange?: (status: MakerInstancePublicStatus) => void;
  apiPrefix?: string;
  heightClass?: string;
  layout?: "stacked" | "split";
}) {
  const asksScrollRef = useRef<HTMLDivElement | null>(null);
  const bidsScrollRef = useRef<HTMLDivElement | null>(null);
  const asksPinnedToMiddleRef = useRef(true);
  const depthScaleRef = useRef<Record<string, number>>({});
  const [makerInstance, setMakerInstance] = useState<MakerInstancePublicStatus>();
  const mergedBids = mergeTicks <= 1 ? bids : mergeLevels(bids, "buy", tickSize, mergeTicks);
  const mergedAsks = mergeTicks <= 1 ? asks : mergeLevels(asks, "sell", tickSize, mergeTicks);
  const isSplit = layout === "split";
  const sideDepth = isSplit ? Math.max(10, depth) : Math.max(10, Math.floor(depth / 2));
  const visibleAsks = isSplit ? mergedAsks.slice(0, sideDepth) : [...mergedAsks].slice(0, sideDepth).reverse();
  const visibleBids = mergedBids.slice(0, sideDepth);
  const bestBid = Number(mergedBids[0]?.[0] ?? 0);
  const bestAsk = Number(mergedAsks[0]?.[0] ?? 0);
  const lastTradePrice = Number(lastPrice ?? 0);
  const spread = bestBid > 0 && bestAsk > 0 ? bestAsk - bestBid : 0;
  const spreadPct = spread > 0 && bestBid > 0 ? (spread / bestBid) * 100 : 0;
  const bidTotal = visibleBids.reduce((sum, [, quantity]) => sum + Number(quantity || 0), 0);
  const askTotal = visibleAsks.reduce((sum, [, quantity]) => sum + Number(quantity || 0), 0);
  const totalDepth = bidTotal + askTotal;
  const bidPct = totalDepth > 0 ? (bidTotal / totalDepth) * 100 : 50;
  const lastPriceClass =
    lastSide === "sell"
      ? "text-rose-200"
      : lastSide === "buy"
        ? "text-emerald-200"
        : "text-slate-100";
  const reference = makerInstance?.reference_price;
  const referenceMid = Number(reference?.mid ?? 0);
  const localMid = bestBid > 0 && bestAsk > 0 ? (bestBid + bestAsk) / 2 : 0;
  const referenceDeviationBps = referenceMid > 0 && localMid > 0
    ? ((localMid - referenceMid) / referenceMid) * 10_000
    : null;

  const scrollAsksToBest = () => {
    const asksNode = asksScrollRef.current;
    if (!asksNode) return;
    const target = isSplit ? 0 : Math.max(asksNode.scrollHeight - asksNode.clientHeight, 0);
    if (Math.abs(asksNode.scrollTop - target) <= 1) return;
    if (isSplit) {
      asksNode.scrollTop = target;
      return;
    }
    asksNode.scrollTop = target;
  };

  useEffect(() => {
    depthScaleRef.current = {};
    asksPinnedToMiddleRef.current = true;
    window.requestAnimationFrame(scrollAsksToBest);
    const bidsNode = bidsScrollRef.current;
    if (bidsNode) {
      bidsNode.scrollTop = 0;
    }
  }, [symbol, depth, mergeTicks, layout]);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    const refreshMaker = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const normalizedApiPrefix = apiPrefix ? `/${apiPrefix.replace(/^\/+|\/+$/g, "")}` : "";
        const next = await api.get<MakerInstancePublicStatus>(`${normalizedApiPrefix}/markets/${symbol}/maker-instance`);
        if (active) {
          setMakerInstance(next);
          onMakerStatusChange?.(next);
        }
      } catch {
        // Keep the last authoritative sample visible; its age continues to show staleness.
      } finally {
        inFlight = false;
      }
    };
    void refreshMaker();
    const timer = window.setInterval(() => void refreshMaker(), 1000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [apiPrefix, onMakerStatusChange, symbol]);

  const runnerParentLost = makerInstance?.runner_parent_watch?.status === "parent_lost";
  const makerDataPlaneStale = makerInstance?.data_plane_status === "stale"
    || makerInstance?.data_plane_status === "missing"
    || makerInstance?.data_plane_status === "empty";
  const makerStatusLabel = runnerParentLost
    ? "Runner已丢失"
    : makerInstance?.status === "degraded" || makerDataPlaneStale
      ? makerInstance?.data_plane_status === "empty" ? "MM盘口为空" : "MM数据停更"
    : makerInstance?.running && makerInstance.heartbeat_status === "ok"
      ? "MM运行"
      : "MM已停止";
  const referenceRow = (
    <div
      className={`mt-2 flex min-w-0 items-center justify-between gap-2 rounded-lg border px-2.5 py-1.5 text-[11px] ${
        !makerInstance?.running || makerInstance?.status === "degraded" || makerDataPlaneStale || reference?.stale
          ? "border-amber-400/20 bg-amber-400/8 text-amber-100"
          : "border-cyan-400/15 bg-cyan-400/[0.06] text-cyan-100"
      }`}
    >
        <span className="min-w-0 truncate">
        {reference?.venue ?? "Binance"} {reference?.market === "perpetual" ? "永续" : "现货"}
        <span className="ml-2 font-mono text-sm tabular-nums">
          {referenceMid > 0 ? referenceMid.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}
        </span>
      </span>
      <span className="shrink-0 font-mono tabular-nums text-slate-300">
        本地 {referenceDeviationBps === null ? "-" : `${referenceDeviationBps >= 0 ? "+" : ""}${referenceDeviationBps.toFixed(2)} bps`}
        {reference ? ` · ${(reference.age_ms / 1000).toFixed(1)}s` : ""}
        {makerInstance ? ` · ${makerStatusLabel}` : ""}
      </span>
    </div>
  );

  const bestAskKey = visibleAsks[0]?.[0] ?? "";
  useEffect(() => {
    if (!asksPinnedToMiddleRef.current) return;
    window.requestAnimationFrame(scrollAsksToBest);
  }, [bestAskKey, isSplit]);

  const onAsksScroll = () => {
    const asksNode = asksScrollRef.current;
    if (!asksNode) return;
    const distanceToBest = asksNode.scrollHeight - asksNode.clientHeight - asksNode.scrollTop;
    asksPinnedToMiddleRef.current = distanceToBest <= 3;
  };

    const renderRows = (levels: Level[], side: "buy" | "sell", compact = false) => {
      const cumulativeByIndex: number[] = new Array(levels.length).fill(0);
      // 累计统一从最优价向下累加：深层变化不会让买一/卖一的累计列跟着闪。
      let cumulative = 0;
      for (let index = 0; index < levels.length; index += 1) {
        const quantity = Number(levels[index][1]);
        cumulative += quantity;
        cumulativeByIndex[index] = cumulative;
      }
    const scaleKey = `${layout}:${compact ? "compact" : "stacked"}:${side}:${sideDepth}`;
    const estimatedScale = estimateRowDepthScale(levels, sideDepth);
    const previousScale = depthScaleRef.current[scaleKey] ?? 0;
    // 粘滞缩放：只有新基准明显变大（>100%）或明显变小（<50%）才更新，
    // 避免远档微变导致整盘深度条闪烁。
    let depthScale = previousScale;
    if (previousScale <= 0 || estimatedScale > previousScale * 2.0 || estimatedScale < previousScale * 0.5) {
      depthScale = estimatedScale;
    }
    depthScaleRef.current[scaleKey] = depthScale;
    return levels.map(([price, quantity], index) => {
      const priceNumber = Number(price);
      const normalizedPrice = Number.isFinite(priceNumber) ? priceNumber.toFixed(priceDigits) : price;
      const rawPriceKey = String(price);
      const rowNotional = priceNumber * Number(quantity);
      const depthPct = Math.min(100, Math.round((rowNotional / depthScale) * 1000) / 10);
      const priceLabel = priceNumber.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits });
      const quantityLabel = fmtQuantity(quantity, qtyDigits);
      const cumulativeLabel = fmtQuantity(cumulativeByIndex[index], qtyDigits);
      return (
        <OrderbookRow
          key={`${side}-${rawPriceKey}`}
          side={side}
          price={normalizedPrice}
          rowKey={rawPriceKey}
          priceLabel={priceLabel}
          quantityLabel={quantityLabel}
          cumulativeLabel={cumulativeLabel}
          depthPct={depthPct}
          compact={compact}
          onSelectPrice={onSelectPrice}
        />
      );
    });
  };

  const renderSplitRows = (levels: Level[], side: "buy" | "sell") => {
    if (levels.length === 0) {
      return <div className="rounded-xl bg-slate-950/30 px-3 py-6 text-center text-xs text-slate-500">暂无{side === "buy" ? "买盘" : "卖盘"}</div>;
    }
    return renderRows(levels, side, true);
  };

  const splitHeader = (side: "buy" | "sell") => (
    <div className={`mb-2 grid ${SPLIT_ROW_GRID_CLASS} gap-2 px-2 text-[11px] uppercase tracking-[0.14em] text-slate-500`}>
      <span className={side === "buy" ? "text-emerald-300/70" : "text-rose-300/70"}>{side === "buy" ? "买价" : "卖价"}</span>
      <span className="text-right">数量</span>
      <span className="text-right">累计</span>
    </div>
  );

  if (isSplit) {
    return (
      <section className={`panel flex min-h-0 flex-col rounded-2xl p-3 ${heightClass}`}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-base">订单簿</h3>
            <p className="mt-1 text-xs text-slate-500">左右买卖盘 · 每侧 {depth} 档</p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <OrderbookFreshnessBadge updatedAt={updatedAt} />
            {[20, 50, 100].map((value) => (
              <button key={value} onClick={() => onDepthChange(value)} className={`rounded-lg px-2.5 py-1 text-xs ${depth === value ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300"}`}>
                {value}
              </button>
            ))}
            {[1, 5, 10].map((value) => (
              <button key={value} onClick={() => onMergeChange(value)} className={`rounded-lg px-2.5 py-1 text-xs ${mergeTicks === value ? "bg-white/14 text-white" : "bg-white/5 text-slate-300"}`}>
                {value === 1 ? "原始" : `x${value}`}
              </button>
            ))}
          </div>
        </div>
        <div className="mb-3 rounded-xl border border-white/8 bg-slate-950/35 px-3 py-2">
          <div className="grid grid-cols-3 items-center gap-2 font-mono tabular-nums">
            <button type="button" onClick={() => bestBid > 0 && onSelectPrice(String(bestBid))} className="min-w-0 text-left text-xs text-emerald-300 hover:text-emerald-100">
              <span className="block text-[11px] text-slate-500">买一</span>
              <span className="num-fixed num-left num-price block">{bestBid ? bestBid.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span>
            </button>
            <button type="button" onClick={() => lastTradePrice > 0 && onSelectPrice(String(lastTradePrice))} className={`min-w-0 text-center text-sm font-semibold ${lastPriceClass}`}>
              <span className="block text-[11px] font-normal text-slate-500">最新</span>
              <span className="num-fixed num-price block">{lastTradePrice ? lastTradePrice.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span>
            </button>
            <button type="button" onClick={() => bestAsk > 0 && onSelectPrice(String(bestAsk))} className="min-w-0 text-right text-xs text-rose-300 hover:text-rose-100">
              <span className="block text-[11px] text-slate-500">卖一</span>
              <span className="num-fixed num-price block">{bestAsk ? bestAsk.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span>
            </button>
          </div>
          <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-slate-500">
            <span>买盘 <span className="num-fixed num-short">{bidPct.toFixed(0)}%</span></span>
            <span className="min-w-0 text-center">Spread <span className="num-fixed num-price">{spread ? spread.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span> · <span className="num-fixed num-short">{spreadPct ? `${spreadPct.toFixed(4)}%` : "-"}</span></span>
            <span>卖盘 <span className="num-fixed num-short">{(100 - bidPct).toFixed(0)}%</span></span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-rose-500/18">
            <div className="h-full rounded-full bg-emerald-400/55" style={{ width: `${bidPct}%` }} />
          </div>
          {referenceRow}
        </div>
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-3">
          <div className="min-h-0 overflow-hidden rounded-xl border border-emerald-400/10 bg-emerald-400/[0.025] p-2">
            {splitHeader("buy")}
            <div
              ref={bidsScrollRef}
              className="scrollbar h-[calc(100%-24px)] overflow-y-auto pr-1"
              style={{ scrollbarGutter: "stable" }}
            >
              <div className="space-y-1">{renderSplitRows(visibleBids, "buy")}</div>
            </div>
          </div>
          <div className="min-h-0 overflow-hidden rounded-xl border border-rose-400/10 bg-rose-500/[0.025] p-2">
            {splitHeader("sell")}
            <div
              ref={asksScrollRef}
              className="scrollbar h-[calc(100%-24px)] overflow-y-auto pr-1"
              style={{ scrollbarGutter: "stable" }}
            >
              <div className="space-y-1">{renderSplitRows(visibleAsks, "sell")}</div>
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className={`panel flex min-h-0 flex-col rounded-2xl p-3 ${heightClass}`}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="font-display text-base">订单簿</h3>
          <p className="mt-1 text-xs text-slate-500">{depth} 档 · 点击价格填入下单</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <OrderbookFreshnessBadge updatedAt={updatedAt} />
          {[20, 50].map((value) => (
            <button key={value} onClick={() => onDepthChange(value)} className={`rounded-lg px-2.5 py-1 text-xs ${depth === value ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300"}`}>
              {value}
            </button>
          ))}
          {[1, 5, 10].map((value) => (
            <button key={value} onClick={() => onMergeChange(value)} className={`rounded-lg px-2.5 py-1 text-xs ${mergeTicks === value ? "bg-white/14 text-white" : "bg-white/5 text-slate-300"}`}>
              {value === 1 ? "原始" : `x${value}`}
            </button>
          ))}
        </div>
      </div>
      <div className={`grid ${ROW_GRID_CLASS} gap-3 px-3 pb-2 text-xs uppercase tracking-[0.18em] text-slate-500`}>
        <span className="text-left">价格</span>
        <span className="text-right">数量</span>
        <span className="text-right">累计</span>
      </div>
      <div
        ref={asksScrollRef}
        onScroll={onAsksScroll}
        className="scrollbar flex flex-1 min-h-0 flex-col overflow-y-scroll pr-1"
        style={{ scrollbarGutter: "stable" }}
      >
        <div className="mt-auto space-y-1">{renderRows(visibleAsks, "sell")}</div>
      </div>
      <div className="my-3 min-h-[118px] rounded-xl border border-white/8 bg-slate-950/35 px-3 py-2">
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(116px,auto)_minmax(0,1fr)] items-start gap-3">
          <div className="min-w-0 font-mono tabular-nums">
            <div className="text-[11px] text-slate-500">买一</div>
            <button type="button" onClick={() => bestBid > 0 && onSelectPrice(String(bestBid))} className="mt-1 block max-w-full truncate text-left text-xs text-emerald-300 hover:text-emerald-100">
              <span className="num-fixed num-left num-price">{bestBid ? bestBid.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span>
            </button>
          </div>
          <button
            type="button"
            onClick={() => lastTradePrice > 0 && onSelectPrice(String(lastTradePrice))}
            className={`min-w-0 text-center font-mono text-base font-semibold tabular-nums ${lastPriceClass}`}
          >
            <span className="block text-[11px] font-normal text-slate-500">最新成交价</span>
            <span className="num-fixed num-price block">
              {lastTradePrice ? lastTradePrice.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}
            </span>
          </button>
          <div className="min-w-0 text-right font-mono tabular-nums">
            <div className="text-[11px] text-slate-500">卖一</div>
            <button type="button" onClick={() => bestAsk > 0 && onSelectPrice(String(bestAsk))} className="mt-1 block max-w-full truncate text-right text-xs text-rose-300 hover:text-rose-100">
              <span className="num-fixed num-price">{bestAsk ? bestAsk.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span>
            </button>
          </div>
        </div>
        <div className="mt-1 text-center text-[11px] text-slate-500">
          Spread <span className="num-fixed num-price">{spread ? spread.toLocaleString("zh-CN", { minimumFractionDigits: priceDigits, maximumFractionDigits: priceDigits }) : "-"}</span> · <span className="num-fixed num-short">{spreadPct ? `${spreadPct.toFixed(4)}%` : "-"}</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-rose-500/18">
          <div className="h-full rounded-full bg-emerald-400/55" style={{ width: `${bidPct}%` }} />
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px] text-slate-500">
          <span>买盘 <span className="num-fixed num-short">{bidPct.toFixed(0)}%</span></span>
          <span>卖盘 <span className="num-fixed num-short">{(100 - bidPct).toFixed(0)}%</span></span>
        </div>
        {referenceRow}
      </div>
      <div
        ref={bidsScrollRef}
        className="scrollbar flex-1 min-h-0 overflow-y-scroll pr-1"
        style={{ scrollbarGutter: "stable" }}
      >
        <div className="space-y-1">{renderRows(visibleBids, "buy")}</div>
      </div>
    </section>
  );
}

export const OrderbookPanel = memo(OrderbookPanelComponent);
