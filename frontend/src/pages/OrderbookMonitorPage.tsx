import { useLiveMakerSymbols } from "../hooks/useLiveMakerSymbols";
import { memo, startTransition, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ToastViewport } from "../components/ToastViewport";
import { useMarketStreams } from "../hooks/useMarketStreams";
import { bjTime, fmt, fmtQuantity, stepDigits } from "../lib/format";
import { sourceLabel, sourcePillClass } from "../lib/source";
import { useAppStore } from "../store/useAppStore";
import type { Level, MarketDefinition, MarketHealth, MarketTicker, TradeItem } from "../types";

const DEPTH_BANDS = [
  { key: "1bp", label: "0.01%", bps: 1 },
  { key: "5bp", label: "0.05%", bps: 5 },
  { key: "10bp", label: "0.10%", bps: 10 },
  { key: "30bp", label: "0.30%", bps: 30 },
  { key: "50bp", label: "0.50%", bps: 50 },
  { key: "100bp", label: "1.00%", bps: 100 },
  { key: "2pct", label: "2.00%", bps: 200 },
] as const;

const SWEEP_TARGETS = [1_000, 10_000, 100_000, 500_000, 1_000_000];
const EMPTY_ORDERBOOK = { bids: [], asks: [] };

type MarketTopOwner = {
  type?: string;
  label?: string;
  usernames?: string[];
};

type MarketSurveillanceItem = {
  symbol: string;
  status: "ok" | "warn" | "critical" | string;
  top: {
    bid?: MarketTopOwner;
    ask?: MarketTopOwner;
  };
  metrics: {
    best_bid?: string | null;
    best_ask?: string | null;
    spread_quote?: string | null;
    spread_pct: string;
    depth_amount_0_5pct: string;
    depth_amount_2pct: string;
    book_imbalance: string;
    bid_level_count: number;
    ask_level_count: number;
    open_order_count: number;
    open_order_notional: string;
    trade_count_24h: number;
    quote_volume_24h: string;
  };
  open_orders_by_role: {
    side: string;
    role: string;
    open_order_count: number;
    remaining_quantity: string;
    notional: string;
  }[];
  top_open_order_users: {
    username: string;
    role: string;
    open_order_count: number;
    remaining_quantity: string;
    notional: string;
  }[];
  checks: {
    code: string;
    severity: "ok" | "warn" | "critical" | string;
    label: string;
    detail: string;
  }[];
};

type BookRow = {
  index: number;
  priceKey: string;
  price: number;
  quantity: number;
  notional: number;
  cumulative: number;
  distancePct: number;
};

type SweepSide = {
  vwap: number | null;
  quote: number;
  base: number;
  ok: boolean;
  unfilled: number;
  deltaPct: number | null;
};

type DepthBandMetric = (typeof DEPTH_BANDS)[number] & {
  bid: number;
  ask: number;
  total: number;
  imbalance: number;
};

const num = (value?: string | number | null) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

const pctText = (value?: number | null, digits = 4) => {
  if (value === undefined || value === null || !Number.isFinite(value)) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
};

const compactNotional = (value: number) => {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1_000_000) return `${fmt(value / 1_000_000, 2)}M`;
  if (Math.abs(value) >= 1_000) return `${fmt(value / 1_000, 1)}K`;
  return fmt(value, 0);
};

const fullNotional = (value: number) => {
  if (!Number.isFinite(value)) return "-";
  const digits = Math.abs(value) >= 100 ? 0 : 2;
  return value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

const targetLabel = (value: number) => {
  if (value >= 1_000_000) return "100 万";
  if (value >= 500_000) return "50 万";
  if (value >= 100_000) return "10 万";
  if (value >= 10_000) return "1 万";
  return "1 千";
};

function computeRows(levels: Level[], side: "bid" | "ask", touch: number): BookRow[] {
  let cumulative = 0;
  return levels.map(([priceText, quantityText], index) => {
    const price = num(priceText);
    const quantity = num(quantityText);
    const notional = price * quantity;
    cumulative += notional;
    const distancePct =
      touch > 0 && price > 0
        ? side === "bid"
          ? ((touch - price) / touch) * 100
          : ((price - touch) / touch) * 100
        : 0;
    return { index: index + 1, priceKey: priceText, price, quantity, notional, cumulative, distancePct };
  });
}

function estimateMonitorRowDepthScale(rows: BookRow[], expectedDepth: number) {
  const headRows = rows.slice(0, Math.min(rows.length, 5)).filter((row) => row.notional > 0);
  const averageHeadNotional = headRows.length > 0
    ? headRows.reduce((sum, row) => sum + row.notional, 0) / headRows.length
    : 1;
  const maxRowNotional = Math.max(...rows.map((row) => row.notional), 0);
  return Math.max(maxRowNotional, averageHeadNotional * Math.max(expectedDepth, rows.length, 1), 1);
}

function coverageRatio(rows: BookRow[], touch: number, side: "bid" | "ask") {
  const last = rows[rows.length - 1];
  if (!last || touch <= 0 || last.price <= 0) return null;
  const value = side === "bid" ? (touch - last.price) / touch : (last.price - touch) / touch;
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function depthWithinTouch(levels: Level[], touch: number, bps: number, side: "bid" | "ask") {
  if (touch <= 0) return 0;
  const limit = side === "bid" ? touch * (1 - bps / 10_000) : touch * (1 + bps / 10_000);
  return levels.reduce((sum, [priceText, quantityText]) => {
    const price = num(priceText);
    const quantity = num(quantityText);
    if (price <= 0 || quantity <= 0) return sum;
    if (side === "bid" && price < limit) return sum;
    if (side === "ask" && price > limit) return sum;
    return sum + price * quantity;
  }, 0);
}

function sweepQuote(levels: Level[], targetQuote: number): Omit<SweepSide, "deltaPct"> {
  if (targetQuote <= 0 || levels.length === 0) {
    return { vwap: null, quote: 0, base: 0, ok: false, unfilled: targetQuote };
  }
  let remaining = targetQuote;
  let totalBase = 0;
  let totalQuote = 0;
  for (const [priceText, quantityText] of levels) {
    const price = num(priceText);
    const quantity = num(quantityText);
    const levelQuote = price * quantity;
    if (price <= 0 || quantity <= 0 || levelQuote <= 0) continue;
    if (remaining <= 0) break;
    if (levelQuote <= remaining) {
      totalBase += quantity;
      totalQuote += levelQuote;
      remaining -= levelQuote;
    } else {
      const partialBase = remaining / price;
      totalBase += partialBase;
      totalQuote += remaining;
      remaining = 0;
      break;
    }
  }
  return {
    vwap: totalBase > 0 ? totalQuote / totalBase : null,
    quote: totalQuote,
    base: totalBase,
    ok: remaining <= 0.000001,
    unfilled: Math.max(0, remaining),
  };
}

function sweepWithDelta(levels: Level[], targetQuote: number, mid: number, side: "buy" | "sell"): SweepSide {
  const result = sweepQuote(levels, targetQuote);
  const deltaPct =
    result.vwap !== null && mid > 0
      ? side === "buy"
        ? ((result.vwap - mid) / mid) * 100
        : ((mid - result.vwap) / mid) * 100
      : null;
  return { ...result, deltaPct };
}

export function OrderbookMonitorPage() {
  const liveMakers = useLiveMakerSymbols();
  const navigate = useNavigate();
  const { symbol: routeSymbol } = useParams();
  const requestedSymbol = (routeSymbol || "BTCUSDT").toUpperCase();
  const markets = useAppStore((state) => state.markets);
  const tickerSymbol = useAppStore((state) => state.tickerSymbol);
  const rawTicker = useAppStore((state) => state.ticker);
  const ticker = tickerSymbol === requestedSymbol ? rawTicker : undefined;
  const orderbookSymbol = useAppStore((state) => state.orderbookSymbol);
  const rawOrderbook = useAppStore((state) => state.orderbook);
  const orderbook = orderbookSymbol === requestedSymbol ? rawOrderbook : EMPTY_ORDERBOOK;
  const orderbookUpdatedAt = useAppStore((state) => state.orderbookUpdatedAt);
  const recentTradesSymbol = useAppStore((state) => state.recentTradesSymbol);
  const rawRecentTrades = useAppStore((state) => state.recentTrades);
  const recentTrades = recentTradesSymbol === requestedSymbol ? rawRecentTrades : [];
  const setMarkets = useAppStore((state) => state.setMarkets);
  const setTicker = useAppStore((state) => state.setTicker);
  const setRecentTrades = useAppStore((state) => state.setRecentTrades);
  const setOrderbookDepth = useAppStore((state) => state.setOrderbookDepth);
  const authSession = useAppStore((state) => state.authSession);
  const pushToast = useAppStore((state) => state.pushToast);
  const adminApiKey = authSession?.role === "admin" ? authSession.api_key : "";
  const [marketMap, setMarketMap] = useState<Record<string, MarketDefinition>>({});
  const [health, setHealth] = useState<MarketHealth>();
  const [surveillance, setSurveillance] = useState<MarketSurveillanceItem>();
  const [loading, setLoading] = useState(false);
  const [depthLimit, setDepthLimit] = useState(100);
  const refreshSeqRef = useRef(0);

  const market = marketMap[requestedSymbol];
  const priceDigits = market?.price_precision ?? stepDigits(market?.price_tick) ?? 4;
  const qtyDigits = market?.qty_precision ?? stepDigits(market?.qty_step) ?? 4;
  const bestBid = num(orderbook.bids[0]?.[0]);
  const bestAsk = num(orderbook.asks[0]?.[0]);
  const mid = bestBid > 0 && bestAsk > 0 ? (bestBid + bestAsk) / 2 : num(ticker?.mid_price);
  const spread = bestBid > 0 && bestAsk > 0 ? bestAsk - bestBid : 0;
  const spreadBps = mid > 0 ? (spread / mid) * 10_000 : 0;
  const visibleTrades = recentTrades.filter((item) => item.source !== "bootstrap_seed");
  const latestTrade = visibleTrades[0];
  const latestTradePrice = num(latestTrade?.price);
  const latestTradeTs = Number(latestTrade?.ts ?? latestTrade?.executed_at ?? 0);
  const referenceNow = Math.max(Date.now(), latestTradeTs);
  const trades60s = visibleTrades.filter((item) => {
    const ts = Number(item.ts ?? item.executed_at ?? 0);
    return ts > 0 && referenceNow - ts <= 60_000;
  });
  const quote60s = trades60s.reduce((sum, item) => sum + (num(item.quote_amount) || num(item.price) * num(item.quantity)), 0);
  const tradeTouchDeviation =
    latestTradePrice > 0 && mid > 0
      ? latestTradePrice < bestBid
        ? ((bestBid - latestTradePrice) / mid) * 100
        : latestTradePrice > bestAsk
          ? ((latestTradePrice - bestAsk) / mid) * 100
          : 0
      : null;
  const bidRows = useMemo(() => computeRows(orderbook.bids.slice(0, depthLimit), "bid", bestBid), [bestBid, depthLimit, orderbook.bids]);
  const askRows = useMemo(() => computeRows(orderbook.asks.slice(0, depthLimit), "ask", bestAsk), [bestAsk, depthLimit, orderbook.asks]);
  const bidNotional = bidRows[bidRows.length - 1]?.cumulative ?? 0;
  const askNotional = askRows[askRows.length - 1]?.cumulative ?? 0;
  const bidCoverage = coverageRatio(bidRows, bestBid, "bid");
  const askCoverage = coverageRatio(askRows, bestAsk, "ask");
  const depthBands = DEPTH_BANDS.map((band) => {
    const bid = depthWithinTouch(orderbook.bids, bestBid, band.bps, "bid");
    const ask = depthWithinTouch(orderbook.asks, bestAsk, band.bps, "ask");
    const total = bid + ask;
    const imbalance = total > 0 ? ((bid - ask) / total) * 100 : 0;
    return { ...band, bid, ask, total, imbalance };
  });
  const sweepRows = SWEEP_TARGETS.map((target) => ({
    target,
    buy: sweepWithDelta(orderbook.asks, target, mid, "buy"),
    sell: sweepWithDelta(orderbook.bids, target, mid, "sell"),
  }));
  const status = surveillance?.status ?? health?.status ?? "loading";
  const statusLabel = status === "ok" ? "正常" : status === "critical" ? "异常" : status === "warn" ? "关注" : "加载中";

  useMarketStreams(requestedSymbol, "1m", depthLimit, false);

  const refresh = async () => {
    if (!adminApiKey) return;
    const requestId = ++refreshSeqRef.current;
    setLoading(true);
    try {
      const [marketList, tickerResponse, tradesResponse, healthResponse, surveillanceResponse] = await Promise.all([
        api.get<{ items: MarketDefinition[] }>("/markets"),
        api.get<MarketTicker>(`/markets/${requestedSymbol}/ticker`),
        api.get<{ items: TradeItem[] }>(`/markets/${requestedSymbol}/trades?limit=120&include_seed=false`),
        api.get<MarketHealth>(`/markets/${requestedSymbol}/health`),
        api.get<{ items: MarketSurveillanceItem[] }>("/admin/market-surveillance", adminApiKey),
      ]);
      if (requestId !== refreshSeqRef.current) return;
      startTransition(() => {
        setMarkets(marketList.items.map((item) => item.symbol));
        setMarketMap(Object.fromEntries(marketList.items.map((item) => [item.symbol, item])));
        setTicker(requestedSymbol, tickerResponse);
        setRecentTrades(requestedSymbol, tradesResponse.items);
        setHealth(healthResponse);
        setSurveillance(surveillanceResponse.items.find((item) => item.symbol === requestedSymbol));
      });
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "盘口监控加载失败");
    } finally {
      if (requestId === refreshSeqRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    setOrderbookDepth(depthLimit);
  }, [depthLimit, setOrderbookDepth]);

  useEffect(() => {
    void refresh();
  }, [requestedSymbol, depthLimit, adminApiKey]);

  useEffect(() => {
    if (markets.length > 0 && !markets.includes(requestedSymbol)) {
      navigate(`/ops/orderbook/${markets[0]}`, { replace: true });
    }
  }, [markets, navigate, requestedSymbol]);

  return (
    <AppShell>
      <ToastViewport />
      <section className="panel mb-3 rounded-2xl px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="font-display text-xl text-white">盘口监控</h1>
              <StatusBadge status={status} label={statusLabel} />
              {loading && <span className="rounded-lg bg-white/8 px-2.5 py-1 text-xs text-slate-300">刷新中</span>}
            </div>
            <p className="mt-1 text-xs text-slate-500">报价或数量变化才会改变盘口；策略检查周期不等于每轮必须撤换订单。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={requestedSymbol}
              onChange={(event) => navigate(`/ops/orderbook/${event.target.value}`)}
              className="h-9 rounded-xl border border-white/10 bg-slate-950/55 px-3 text-sm text-slate-100 outline-none"
            >
              {markets.map((item) => (
                <option key={item} value={item}>{item}{liveMakers.has(item) ? " · LIVE" : ""}</option>
              ))}
            </select>
            {[50, 100].map((value) => (
              <button
                key={value}
                onClick={() => setDepthLimit(value)}
                className={`h-9 rounded-xl px-3 text-sm ${depthLimit === value ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300 hover:bg-white/10"}`}
              >
                {value} 档
              </button>
            ))}
            <button onClick={() => void refresh()} className="h-9 rounded-xl bg-white/8 px-3 text-sm text-slate-100 hover:bg-white/12">
              刷新
            </button>
          </div>
        </div>
      </section>

      <section className="mb-3 grid gap-3 md:grid-cols-4 xl:grid-cols-12">
        <MetricCard className="xl:col-span-2" label="买一 / 卖一" value={`${fmt(bestBid, priceDigits)} / ${fmt(bestAsk, priceDigits)}`} />
        <MetricCard className="xl:col-span-2" label="Spread" value={`${fmt(spread, priceDigits)} · ${fmt(spreadBps, 2)} bps`} />
        <MetricCard label="中间价" value={fmt(mid, priceDigits)} />
        <MetricCard className="xl:col-span-2" label="最新成交" value={latestTrade ? `${fmt(latestTrade.price, priceDigits)} · ${bjTime(latestTradeTs)}` : "-"} />
        <MetricCard label="贴边偏离" value={tradeTouchDeviation === null ? "-" : pctText(tradeTouchDeviation, 4)} tone={tradeTouchDeviation && tradeTouchDeviation > 2 ? "danger" : "neutral"} />
        <MetricCard label="近 60s 成交" value={`${trades60s.length} 笔 · ${compactNotional(quote60s)}`} />
        <MetricCard label="24H 成交额" value={compactNotional(num(ticker?.quote_volume_24h ?? surveillance?.metrics.quote_volume_24h))} />
        <MetricCard className="xl:col-span-2" label="盘口更新时间" value={orderbookUpdatedAt ? bjTime(orderbookUpdatedAt) : "-"} />
      </section>

      <div className="mb-3 grid gap-3 xl:grid-cols-[minmax(0,1.2fr)_minmax(360px,0.8fr)]">
        <DepthBandPanel bands={depthBands} quoteAsset={market?.quote_asset ?? "USDT"} />
        <SweepPanel rows={sweepRows} priceDigits={priceDigits} quoteAsset={market?.quote_asset ?? "USDT"} />
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.75fr)]">
        <section className="panel rounded-2xl p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-display text-base">订单簿形态</h2>
              <p className="mt-1 text-xs text-slate-500">
                买侧覆盖 {bidCoverage === null ? "-" : pctText(bidCoverage * 100, 4)} · 卖侧覆盖 {askCoverage === null ? "-" : pctText(askCoverage * 100, 4)}
              </p>
            </div>
            <div className="flex flex-wrap gap-2 text-xs text-slate-400">
              <span className="rounded-lg bg-emerald-400/10 px-2.5 py-1 text-emerald-100">买侧名义 {compactNotional(bidNotional)}</span>
              <span className="rounded-lg bg-rose-500/10 px-2.5 py-1 text-rose-100">卖侧名义 {compactNotional(askNotional)}</span>
            </div>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            <OrderbookSideTable title="买盘" side="bid" rows={bidRows} priceDigits={priceDigits} qtyDigits={qtyDigits} expectedDepth={depthLimit} scaleKey={`${requestedSymbol}:${depthLimit}`} />
            <OrderbookSideTable title="卖盘" side="ask" rows={askRows} priceDigits={priceDigits} qtyDigits={qtyDigits} expectedDepth={depthLimit} scaleKey={`${requestedSymbol}:${depthLimit}`} />
          </div>
        </section>

        <div className="space-y-3">
          <LatestTradesPanel items={visibleTrades.slice(0, 36)} priceDigits={priceDigits} qtyDigits={qtyDigits} />
          <SurveillancePanel item={surveillance} health={health} />
        </div>
      </div>
    </AppShell>
  );
}

function StatusBadge({ status, label }: { status: string; label: string }) {
  const className =
    status === "ok"
      ? "bg-emerald-400/15 text-emerald-100"
      : status === "critical"
        ? "bg-rose-500/16 text-rose-100"
        : status === "warn"
          ? "bg-amber-400/16 text-amber-100"
          : "bg-white/8 text-slate-300";
  return <span className={`rounded-lg px-2.5 py-1 text-xs ${className}`}>{label}</span>;
}

function statusLabelFor(value: string) {
  return value === "ok" ? "正常" : value === "critical" ? "异常" : value === "warn" ? "关注" : "加载中";
}

function MetricCard({
  label,
  value,
  tone = "neutral",
  className = "",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "danger";
  className?: string;
}) {
  return (
    <div className={`ui-metric min-h-[76px] ${tone === "danger" ? "border border-rose-500/25" : ""} ${className}`}>
      <div className="ui-metric-label">{label}</div>
      <div className={`ui-metric-value mt-2 ${tone === "danger" ? "text-rose-200" : ""}`} title={value}>
        <span className="num-fixed num-money">{value}</span>
      </div>
    </div>
  );
}

function DepthBandPanel({ bands, quoteAsset }: { bands: DepthBandMetric[]; quoteAsset: string }) {
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-base">深度分布</h2>
          <p className="mt-1 text-xs text-slate-500">相对买一/卖一向外扩展，统计价×量名义金额。</p>
        </div>
        <span className="text-xs text-slate-500">{quoteAsset}</span>
      </div>
      <div className="overflow-hidden rounded-xl border border-white/8">
        <div className="grid grid-cols-[72px_1fr_1fr_1fr_88px] bg-white/5 px-3 py-2 text-xs uppercase tracking-[0.14em] text-slate-500">
          <span>范围</span>
          <span className="text-right">买侧</span>
          <span className="text-right">卖侧</span>
          <span className="text-right">合计</span>
          <span className="text-right">失衡</span>
        </div>
        {bands.map((band) => (
          <DepthBandRow
            key={band.key}
            label={band.label}
            bid={band.bid}
            ask={band.ask}
            total={band.total}
            imbalance={band.imbalance}
          />
        ))}
      </div>
    </section>
  );
}

const DepthBandRow = memo(function DepthBandRow({
  label,
  bid,
  ask,
  total,
  imbalance,
}: {
  label: string;
  bid: number;
  ask: number;
  total: number;
  imbalance: number;
}) {
  return (
    <div className="grid grid-cols-[72px_1fr_1fr_1fr_88px] border-t border-white/6 px-3 py-2 font-mono text-sm tabular-nums">
      <span className="text-slate-400">{label}</span>
      <span className="num-fixed num-money justify-self-end text-emerald-300">{compactNotional(bid)}</span>
      <span className="num-fixed num-money justify-self-end text-rose-300">{compactNotional(ask)}</span>
      <span className="num-fixed num-money justify-self-end text-slate-100">{compactNotional(total)}</span>
      <span className={`text-right ${Math.abs(imbalance) > 70 ? "text-amber-200" : "text-slate-400"}`}>{pctText(imbalance, 1)}</span>
    </div>
  );
});

function SweepPanel({
  rows,
  priceDigits,
  quoteAsset,
}: {
  rows: { target: number; buy: SweepSide; sell: SweepSide }[];
  priceDigits: number;
  quoteAsset: string;
}) {
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-base">滑点探针</h2>
          <p className="mt-1 text-xs text-slate-500">模拟主动吃单，输出 VWAP 和相对中间价偏移。</p>
        </div>
        <span className="text-xs text-slate-500">{quoteAsset}</span>
      </div>
      <div className="overflow-hidden rounded-xl border border-white/8">
        <div className="grid grid-cols-[72px_1fr_1fr] bg-white/5 px-3 py-2 text-xs uppercase tracking-[0.14em] text-slate-500">
          <span>金额</span>
          <span className="text-right">主动买</span>
          <span className="text-right">主动卖</span>
        </div>
        {rows.map((row) => (
          <div key={row.target} className="grid grid-cols-[72px_1fr_1fr] border-t border-white/6 px-3 py-2 text-xs">
            <span className="font-mono text-slate-400">{targetLabel(row.target)}</span>
            <SweepCell side={row.buy} priceDigits={priceDigits} tone="buy" />
            <SweepCell side={row.sell} priceDigits={priceDigits} tone="sell" />
          </div>
        ))}
      </div>
    </section>
  );
}

function SweepCell({ side, priceDigits, tone }: { side: SweepSide; priceDigits: number; tone: "buy" | "sell" }) {
  const color = tone === "buy" ? "text-rose-200" : "text-emerald-200";
  if (side.vwap === null) {
    return <span className="text-right text-xs text-amber-200">深度不足</span>;
  }
  return (
    <span className={`flex min-w-0 flex-col items-end gap-0.5 text-right font-mono tabular-nums ${color}`} title={`成交 ${compactNotional(side.quote)}，未成交 ${compactNotional(side.unfilled)}`}>
      <span className="num-fixed num-price">{fmt(side.vwap, priceDigits)}</span>
      <span className="num-fixed num-short">
        {pctText(side.deltaPct, 4)}
        {!side.ok ? <span className="ml-1 text-amber-200">不足</span> : null}
      </span>
    </span>
  );
}

function OrderbookSideTable({
  title,
  side,
  rows,
  priceDigits,
  qtyDigits,
  expectedDepth,
  scaleKey,
}: {
  title: string;
  side: "bid" | "ask";
  rows: BookRow[];
  priceDigits: number;
  qtyDigits: number;
  expectedDepth: number;
  scaleKey: string;
}) {
  const depthScaleRef = useRef(0);
  useEffect(() => {
    depthScaleRef.current = 0;
  }, [scaleKey, side]);
  if (rows.length > 0) {
    depthScaleRef.current = Math.max(depthScaleRef.current, estimateMonitorRowDepthScale(rows, expectedDepth));
  }
  const depthScale = depthScaleRef.current || 1;
  const sideClass = side === "bid" ? "text-emerald-300" : "text-rose-300";
  const heatClass = side === "bid" ? "bg-emerald-400/10" : "bg-rose-500/10";
  return (
    <div className="min-w-0 rounded-xl border border-white/8 bg-slate-950/20">
      <div className="flex items-center justify-between border-b border-white/8 px-3 py-2">
        <h3 className="font-display text-sm text-slate-100">{title}</h3>
        <span className="text-xs text-slate-500">{rows.length} 档</span>
      </div>
      <div className="scrollbar overflow-x-auto">
        <div className="grid min-w-[680px] grid-cols-[44px_minmax(108px,1fr)_minmax(96px,0.8fr)_minmax(116px,0.9fr)_78px_minmax(140px,1fr)] gap-2 px-3 py-2 text-xs uppercase tracking-[0.14em] text-slate-500">
          <span>#</span>
          <span className="text-right">价格</span>
          <span className="text-right">数量</span>
          <span className="text-right">名义</span>
          <span className="text-right">距离</span>
          <span className="text-right">累计</span>
        </div>
        <div className="max-h-[540px] overflow-y-auto">
          {rows.map((row) => {
            const width = Math.min(100, (row.notional / depthScale) * 100);
            const normalizedPrice = Number.isFinite(row.price) ? row.price.toFixed(priceDigits) : String(row.price);
            const rowKey = row.priceKey || normalizedPrice;
            return (
              <OrderbookTableRow
                key={`${side}-${rowKey}`}
                side={side}
                price={rowKey}
                index={row.index}
                quantity={row.quantity}
                notional={row.notional}
                distancePct={row.distancePct}
                cumulative={row.cumulative}
                width={width}
                priceDigits={priceDigits}
                qtyDigits={qtyDigits}
                heatClass={heatClass}
                sideClass={sideClass}
              />
            );
          })}
          {rows.length === 0 && <div className="px-3 py-10 text-center text-sm text-slate-500">暂无盘口档位</div>}
        </div>
      </div>
    </div>
  );
}

const OrderbookTableRow = memo(function OrderbookTableRow({
  side,
  price,
  index,
  quantity,
  notional,
  distancePct,
  cumulative,
  width,
  priceDigits,
  qtyDigits,
  heatClass,
  sideClass,
}: {
  side: "bid" | "ask";
  price: string;
  index: number;
  quantity: number;
  notional: number;
  distancePct: number;
  cumulative: number;
  width: number;
  priceDigits: number;
  qtyDigits: number;
  heatClass: string;
  sideClass: string;
}) {
  return (
    <div
      data-book-row={side}
      data-price={price}
      data-row-key={price}
      data-depth-pct={width.toFixed(4)}
      className="book-row-stable relative grid min-w-[680px] grid-cols-[44px_minmax(108px,1fr)_minmax(96px,0.8fr)_minmax(116px,0.9fr)_78px_minmax(140px,1fr)] gap-2 border-t border-white/5 px-3 py-1.5 font-mono text-sm tabular-nums"
      title={`数量 ${fmtQuantity(quantity, qtyDigits)}，名义 ${fullNotional(notional)}，累计 ${fullNotional(cumulative)}`}
    >
      <span className={`pointer-events-none absolute inset-y-1 ${side === "bid" ? "right-0" : "left-0"} ${heatClass}`} style={{ width: `${width}%` }} />
      <span className="relative text-slate-500">{index}</span>
      <span className={`relative num-fixed num-price justify-self-end ${sideClass}`}>{fmt(Number(price), priceDigits)}</span>
      <span className="relative num-fixed num-qty justify-self-end text-slate-200">{fmtQuantity(quantity, qtyDigits)}</span>
      <span className="relative num-fixed num-money justify-self-end text-slate-400">{fullNotional(notional)}</span>
      <span className="relative num-fixed num-short justify-self-end text-slate-500">{pctText(distancePct, 3)}</span>
      <span className="relative num-fixed num-money justify-self-end text-slate-200">{fullNotional(cumulative)}</span>
    </div>
  );
});

function LatestTradesPanel({ items, priceDigits, qtyDigits }: { items: TradeItem[]; priceDigits: number; qtyDigits: number }) {
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="font-display text-base">最新成交</h2>
        <span className="text-xs text-slate-500">{items.length} 笔</span>
      </div>
      <div className="grid grid-cols-[108px_1fr_0.8fr_0.7fr] gap-2 px-2 pb-2 text-xs uppercase tracking-[0.14em] text-slate-500">
        <span>时间</span>
        <span className="text-right">价格</span>
        <span className="text-right">数量</span>
        <span className="text-right">来源</span>
      </div>
      <div className="scrollbar max-h-[314px] overflow-auto font-mono tabular-nums">
        {items.map((item) => (
          <div key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className="grid grid-cols-[108px_1fr_0.8fr_0.7fr] gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-white/5">
            <span className="num-fixed num-left shrink-0 whitespace-nowrap text-slate-500">{bjTime(item.ts ?? item.executed_at)}</span>
            <span className={`num-fixed num-price justify-self-end ${item.side === "buy" || item.taker_side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{fmt(item.price, priceDigits)}</span>
            <span className="num-fixed num-qty justify-self-end text-slate-200">{fmt(item.quantity, qtyDigits)}</span>
            <span className="text-right">
              <span className={`inline-flex max-w-full justify-end truncate rounded-md border px-1.5 py-0.5 text-[11px] leading-4 ${sourcePillClass(item.source)}`}>
                {sourceLabel(item.source)}
              </span>
            </span>
          </div>
        ))}
        {items.length === 0 && <div className="py-10 text-center text-sm text-slate-500">暂无成交</div>}
      </div>
    </section>
  );
}

function SurveillancePanel({ item, health }: { item?: MarketSurveillanceItem; health?: MarketHealth }) {
  const checks = item?.checks ?? health?.checks ?? [];
  const surveillanceStatus = item?.status ?? health?.status ?? "loading";
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="font-display text-base">做市结构</h2>
        <StatusBadge status={surveillanceStatus} label={statusLabelFor(surveillanceStatus)} />
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <SmallStat label="买一归属" value={item?.top.bid?.label ?? "-"} />
        <SmallStat label="卖一归属" value={item?.top.ask?.label ?? "-"} />
        <SmallStat label="活跃挂单" value={item ? `${item.metrics.open_order_count} · ${compactNotional(num(item.metrics.open_order_notional))}` : "-"} />
        <SmallStat label="买盘占比" value={item ? pctText(num(item.metrics.book_imbalance) * 100, 1) : health ? pctText(num(health.metrics.book_imbalance) * 100, 1) : "-"} />
      </div>
      {item && (
        <div className="mt-3 grid gap-3">
          <div>
            <div className="mb-2 text-xs uppercase tracking-[0.16em] text-slate-500">角色挂单</div>
            <div className="space-y-1.5">
              {item.open_orders_by_role.slice(0, 6).map((row) => (
                <div key={`${row.side}-${row.role}`} className="flex items-center justify-between gap-3 rounded-xl bg-white/5 px-3 py-2 text-xs">
                  <span className={row.side === "buy" ? "text-emerald-300" : "text-rose-300"}>{row.side} · {row.role}</span>
                  <span className="font-mono text-slate-100">{row.open_order_count} · {compactNotional(num(row.notional))}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="mb-2 text-xs uppercase tracking-[0.16em] text-slate-500">大额挂单账户</div>
            <div className="space-y-1.5">
              {item.top_open_order_users.slice(0, 5).map((row) => (
                <div key={row.username} className="flex items-center justify-between gap-3 rounded-xl bg-white/5 px-3 py-2 text-xs">
                  <span className="truncate text-slate-200">{row.username} · {row.role}</span>
                  <span className="shrink-0 font-mono text-slate-100">{compactNotional(num(row.notional))}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      <div className="mt-3 space-y-2">
        {checks.slice(0, 5).map((check) => (
          <div key={check.code} className="rounded-xl bg-slate-950/35 px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-slate-100">{check.label}</span>
              <StatusBadge status={check.severity} label={check.severity} />
            </div>
            <div className="mt-1 text-xs leading-5 text-slate-400">{check.detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function SmallStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="ui-metric">
      <div className="ui-metric-label">{label}</div>
      <div className="ui-metric-value truncate" title={value}>{value}</div>
    </div>
  );
}
