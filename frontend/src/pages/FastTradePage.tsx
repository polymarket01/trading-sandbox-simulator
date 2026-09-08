import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { KlineChart } from "../components/KlineChart";
import { OrderForm } from "../components/OrderForm";
import { OrderbookPanel } from "../components/OrderbookPanel";
import { ToastViewport } from "../components/ToastViewport";
import { TradesPanel } from "../components/TradesPanel";
import { useMarketStreams } from "../hooks/useMarketStreams";
import { usePrivateStream } from "../hooks/usePrivateStream";
import { bjTime, fmt, fmtPct, sideColor, stepDigits } from "../lib/format";
import { KLINE_INTERVALS, sanitizeKlines, summarizeKlineSourceCounts, summarizeKlineSourceVolumes, summarizeKlineSources } from "../lib/kline";
import { summarizeOrderbook } from "../lib/orderbook";
import { useAppStore } from "../store/useAppStore";
import type { BalanceItem, FeeProfileItem, KlineMeta, KlineResponse, MakerInstancePublicStatus, MarketDefinition, MarketTicker, OrderItem, TradeItem } from "../types";

const EMPTY_ORDERBOOK = { bids: [], asks: [] };

export function FastTradePage() {
  const navigate = useNavigate();
  const { symbol = "BTCUSDT" } = useParams();
  const currentSymbol = symbol.toUpperCase();
  const markets = useAppStore((state) => state.markets);
  const tickerSymbol = useAppStore((state) => state.tickerSymbol);
  const rawTicker = useAppStore((state) => state.ticker);
  const ticker = tickerSymbol === currentSymbol ? rawTicker : undefined;
  const orderbookSymbol = useAppStore((state) => state.orderbookSymbol);
  const rawOrderbook = useAppStore((state) => state.orderbook);
  const orderbook = orderbookSymbol === currentSymbol ? rawOrderbook : EMPTY_ORDERBOOK;
  const recentTradesSymbol = useAppStore((state) => state.recentTradesSymbol);
  const rawRecentTrades = useAppStore((state) => state.recentTrades);
  const recentTrades = recentTradesSymbol === currentSymbol ? rawRecentTrades : [];
  const klinesSymbol = useAppStore((state) => state.klinesSymbol);
  const rawKlines = useAppStore((state) => state.klines);
  const klines = klinesSymbol === currentSymbol ? rawKlines : [];
  const balances = useAppStore((state) => state.balances);
  const openOrders = useAppStore((state) => state.openOrders);
  const selectedInterval = useAppStore((state) => state.selectedInterval);
  const orderbookDepth = useAppStore((state) => state.orderbookDepth);
  const orderbookUpdatedAt = useAppStore((state) => state.orderbookUpdatedAt);
  const setMarkets = useAppStore((state) => state.setMarkets);
  const setTicker = useAppStore((state) => state.setTicker);
  const setOrderbook = useAppStore((state) => state.setOrderbook);
  const setRecentTrades = useAppStore((state) => state.setRecentTrades);
  const setKlines = useAppStore((state) => state.setKlines);
  const setBalances = useAppStore((state) => state.setBalances);
  const setOpenOrders = useAppStore((state) => state.setOpenOrders);
  const setSelectedInterval = useAppStore((state) => state.setSelectedInterval);
  const setOrderbookDepth = useAppStore((state) => state.setOrderbookDepth);
  const pushToast = useAppStore((state) => state.pushToast);
  const authSession = useAppStore((state) => state.authSession);
  const [selectedPrice, setSelectedPrice] = useState<string>();
  const [mergeTicks, setMergeTicks] = useState(1);
  const [marketMap, setMarketMap] = useState<Record<string, MarketDefinition>>({});
  const [accountFees, setAccountFees] = useState<FeeProfileItem[]>([]);
  const [makerInstance, setMakerInstance] = useState<MakerInstancePublicStatus>();
  const [klineMeta, setKlineMeta] = useState<KlineMeta>();
  const [showBootstrapKlines, setShowBootstrapKlines] = useState(false);
  const [klineLoading, setKlineLoading] = useState(false);
  const refreshSeqRef = useRef(0);

  const accountApiKey = authSession?.api_key ?? "";
  const market = marketMap[symbol];
  const marketItems = markets.map((item) => marketMap[item]).filter((item): item is MarketDefinition => Boolean(item));
  const spotMarketItems = marketItems.filter((item) => item.product_type !== "PERP");
  const currentFee = accountFees.find((item) => item.symbol === symbol);
  const priceDigits = market?.price_precision ?? stepDigits(market?.price_tick) ?? 4;
  const quantityDigits = market?.qty_precision ?? stepDigits(market?.qty_step) ?? 4;
  const tickSize = Number(market?.price_tick ?? "0.01");
  const liveBook = summarizeOrderbook(orderbook.bids, orderbook.asks);
  const liveTicker: MarketTicker | undefined = ticker
    ? {
        ...ticker,
        best_bid: liveBook.best_bid,
        best_ask: liveBook.best_ask,
        mid_price: liveBook.mid_price,
        spread: liveBook.spread,
        spread_pct: liveBook.spread_pct,
        depth_amount_0_5pct: liveBook.depth_amount_0_5pct,
        depth_amount_2pct: liveBook.depth_amount_2pct,
      }
    : undefined;
  const visibleKlines = useMemo(() => sanitizeKlines(klines, symbol, showBootstrapKlines), [klines, showBootstrapKlines, symbol]);
  const hiddenKlineCount = Math.max(0, klines.length - visibleKlines.length);
  const displayTrades = showBootstrapKlines ? recentTrades : recentTrades.filter((item) => item.source !== "bootstrap_seed");
  const klineSourceSummary = klineMeta?.count
    ? summarizeKlineSourceVolumes(klineMeta.source_quote_volumes) || summarizeKlineSourceCounts(klineMeta.source_counts)
    : summarizeKlineSources(visibleKlines);
  const lowQualityKlines = klineMeta?.data_status === "low_quality" || klineMeta?.quality?.status === "low_quality";
  const emptyKlineMessage =
    hiddenKlineCount > 0 && !showBootstrapKlines
      ? "初始化或无效 K 线已隐藏，等待真实用户/机器人成交。"
      : klineMeta?.message ?? "暂无真实/机器人成交 K 线";
  const makerStatusLabel =
    makerInstance === undefined
      ? "检查中"
      : makerInstance.running
        ? makerInstance.status === "stale"
          ? "心跳异常"
          : "运行中"
        : "无运行实例";

  useMarketStreams(symbol, selectedInterval, orderbookDepth, showBootstrapKlines);
  usePrivateStream(symbol);

  const refreshAll = async () => {
    if (!accountApiKey) return;
    const requestId = ++refreshSeqRef.current;
    setKlineLoading(true);
    try {
      const [
        marketList,
        tickerResponse,
        tradesResponse,
        klinesResponse,
        balancesResponse,
        openOrdersResponse,
        feesResponse,
        makerInstanceResponse,
      ] = await Promise.all([
        api.get<{ items: MarketDefinition[] }>("/markets"),
        api.get<MarketTicker>(`/markets/${symbol}/ticker`),
        api.get<{ items: TradeItem[] }>(`/markets/${symbol}/trades?limit=80&include_seed=${showBootstrapKlines}`),
        api.get<KlineResponse>(`/markets/${symbol}/klines?interval=${selectedInterval}&limit=300&include_seed=${showBootstrapKlines}`),
        api.get<{ items: BalanceItem[] }>("/account/balances", accountApiKey),
        api.get<{ items: OrderItem[] }>(`/account/orders/open?symbol=${symbol}`, accountApiKey),
        api.get<{ items: FeeProfileItem[] }>("/account/fees", accountApiKey),
        api.get<MakerInstancePublicStatus>(`/markets/${symbol}/maker-instance`),
      ]);
      if (requestId !== refreshSeqRef.current) return;
      const nextMarket = marketList.items.find((item) => item.symbol === symbol);
      if (nextMarket?.product_type === "PERP") {
        navigate(`/trade/${symbol}`);
        return;
      }
      startTransition(() => {
        setMarkets(marketList.items.map((item) => item.symbol));
        setMarketMap(Object.fromEntries(marketList.items.map((item) => [item.symbol, item])));
        setTicker(currentSymbol, tickerResponse);
        setRecentTrades(currentSymbol, tradesResponse.items);
        setKlines(currentSymbol, klinesResponse.items);
        setKlineMeta(klinesResponse.meta);
        setBalances(balancesResponse.items);
        setOpenOrders(openOrdersResponse.items);
        setAccountFees(feesResponse.items);
        setMakerInstance(makerInstanceResponse);
      });
    } finally {
      if (requestId === refreshSeqRef.current) setKlineLoading(false);
    }
  };

  useEffect(() => {
    setKlineLoading(true);
    setKlines(currentSymbol, []);
    setKlineMeta(undefined);
  }, [currentSymbol, selectedInterval, showBootstrapKlines, setKlines]);

  useEffect(() => {
    void refreshAll().catch((error) => pushToast("error", error instanceof Error ? error.message : "加载失败"));
  }, [symbol, selectedInterval, orderbookDepth, accountApiKey, showBootstrapKlines]);

  useEffect(() => {
    setSelectedPrice(undefined);
    setMergeTicks(1);
    setTicker(currentSymbol, undefined);
    setOrderbook(symbol, { bids: [], asks: [] });
    setRecentTrades(currentSymbol, []);
    setKlines(currentSymbol, []);
    setKlineMeta(undefined);
    setOpenOrders([]);
    setMakerInstance(undefined);
    setShowBootstrapKlines(false);
    setKlineLoading(true);
  }, [currentSymbol, symbol, setTicker, setOrderbook, setRecentTrades, setKlines, setOpenOrders]);

  const cancelOrder = async (orderId: string) => {
    try {
      await api.delete(`/orders/${orderId}`, accountApiKey);
      await refreshAll();
      pushToast("success", "订单已撤销");
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "撤单失败");
    }
  };

  return (
    <AppShell>
      <ToastViewport />
      <section className="panel mb-3 overflow-hidden rounded-2xl px-4 py-3 lg:h-[130px] xl:h-[118px]">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div className="flex min-w-0 flex-wrap items-start gap-3">
            <div className="h-[72px] min-w-[180px]">
              <div className="flex items-center gap-2">
                <h1 className="font-display text-2xl text-white">{symbol.replace("USDT", "/USDT")}</h1>
                <span className="rounded-lg bg-white/6 px-2 py-1 text-xs text-slate-300">快速下单</span>
              </div>
              <select
                value={symbol}
                onChange={(event) => navigate(`/trade-terminal/${event.target.value}`)}
                className="mt-2 h-9 w-full rounded-lg border border-white/10 bg-slate-950/55 px-3 text-sm text-slate-100 outline-none"
              >
                {spotMarketItems.map((item) => (
                  <option key={item.symbol} value={item.symbol}>
                    {item.symbol.replace("USDT", "/USDT")} · SPOT · {item.market_type}
                  </option>
                ))}
              </select>
            </div>
            <Metric label="最新价" value={fmt(liveTicker?.last_price, priceDigits)} large tone={Number(liveTicker?.change_24h_pct ?? 0) < 0 ? "sell" : "buy"} />
            <Metric label="24H" value={fmtPct(liveTicker?.change_24h_pct, 2)} tone={Number(liveTicker?.change_24h_pct ?? 0) < 0 ? "sell" : "buy"} />
            <Metric label="Spread" value={fmtPct(liveTicker?.spread_pct, 4)} />
            <Metric label="0.5% 深度" value={`${fmt(liveBook.depth_amount_0_5pct, 0)} USDT`} />
            <Metric label="做市实例" value={makerStatusLabel} tone={makerInstance?.running ? "buy" : makerInstance === undefined ? "neutral" : "warn"} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {KLINE_INTERVALS.map((item) => (
              <button key={item} type="button" onClick={() => setSelectedInterval(item)} className={`rounded-lg px-3 py-1.5 text-xs ${selectedInterval === item ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300 hover:bg-white/8"}`}>
                {item}
              </button>
            ))}
            <button
              type="button"
              onClick={() => setShowBootstrapKlines((value) => !value)}
              className={`rounded-lg px-3 py-1.5 text-xs ${showBootstrapKlines ? "bg-amber-400/18 text-amber-100" : "bg-white/5 text-slate-300 hover:bg-white/10"}`}
            >
              初始化 {showBootstrapKlines ? "显示" : "隐藏"}
            </button>
            <button type="button" onClick={() => navigate(`/trade/${symbol}`)} className="rounded-lg bg-white/5 px-3 py-1.5 text-xs text-slate-300 hover:bg-white/10">
              交易台
            </button>
          </div>
        </div>
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(620px,1fr)_340px_360px]">
        <section className="panel flex h-[620px] min-h-0 flex-col rounded-2xl p-3">
          <div className="mb-2 flex h-[48px] items-start justify-between gap-3 overflow-hidden">
            <div>
              <h2 className="font-display text-base">K 线</h2>
              <p className="mt-1 h-4 max-w-2xl overflow-hidden text-ellipsis whitespace-nowrap text-xs text-slate-500">{visibleKlines.length > 0 ? klineSourceSummary : emptyKlineMessage}</p>
            </div>
          </div>
          <div className="relative min-h-0 flex-1">
            {lowQualityKlines ? (
              <InlineState title="K 线质量异常" detail={klineMeta?.message ?? "当前 K 线暂不作为正常做市图表。"} />
            ) : visibleKlines.length > 0 ? (
              <KlineChart
                data={visibleKlines}
                interval={selectedInterval}
                symbol={symbol}
                pricePrecision={priceDigits}
                quantityPrecision={quantityDigits}
                priceTick={market?.price_tick ?? "0.01"}
                compact
              />
            ) : (
              <InlineState title={makerInstance?.running === false ? "无运行中做市实例" : "暂无 K 线"} detail={emptyKlineMessage} />
            )}
            {klineLoading && (
              <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-slate-950/35 backdrop-blur-[1px]">
                <div className="rounded-xl border border-white/10 bg-slate-950/80 px-4 py-2 text-sm text-slate-200">加载 {selectedInterval} K 线...</div>
              </div>
            )}
          </div>
        </section>

        <div className="grid min-h-0 content-start gap-3">
          <OrderbookPanel
            symbol={symbol}
            bids={orderbook.bids}
            asks={orderbook.asks}
            lastPrice={displayTrades[0]?.price ?? liveTicker?.last_price}
            lastSide={displayTrades[0]?.side ?? displayTrades[0]?.taker_side}
            updatedAt={orderbookUpdatedAt}
            depth={orderbookDepth}
            mergeTicks={mergeTicks}
            tickSize={tickSize}
            priceDigits={priceDigits}
            qtyDigits={quantityDigits}
            onDepthChange={setOrderbookDepth}
            onMergeChange={setMergeTicks}
            onSelectPrice={setSelectedPrice}
            onMakerStatusChange={setMakerInstance}
            heightClass="h-[405px]"
          />
          <TradesPanel items={displayTrades} priceDigits={priceDigits} quantityDigits={quantityDigits} heightClass="h-[202px]" />
        </div>

        <aside className="min-h-0 space-y-3">
          <OrderForm
            symbol={symbol}
            ticker={liveTicker}
            balances={balances}
            bids={orderbook.bids}
            asks={orderbook.asks}
            selectedPrice={selectedPrice}
            priceDigits={priceDigits}
            quantityDigits={quantityDigits}
            apiKey={accountApiKey}
            onSubmitted={refreshAll}
            compact
          />
          <QuickBalances balances={balances} quoteAsset={market?.quote_asset ?? "USDT"} baseAsset={market?.base_asset ?? symbol.replace("USDT", "")} fee={currentFee} />
          <QuickOpenOrders orders={openOrders} priceDigits={priceDigits} quantityDigits={quantityDigits} onCancel={cancelOrder} />
        </aside>
      </div>
    </AppShell>
  );
}

function Metric({ label, value, large = false, tone = "neutral" }: { label: string; value: string; large?: boolean; tone?: "buy" | "sell" | "warn" | "neutral" }) {
  const toneClass = tone === "buy" ? "text-emerald-300" : tone === "sell" ? "text-rose-300" : tone === "warn" ? "text-amber-200" : "text-slate-100";
  return (
    <div className={`${large ? "h-[54px] min-w-[150px]" : "h-[48px] min-w-[108px]"} overflow-hidden`}>
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`mt-1 font-mono tabular-nums ${large ? "text-2xl" : "text-sm"} ${toneClass}`} title={value}><span className={large ? "num-fixed num-price" : "num-fixed num-money"}>{value}</span></div>
    </div>
  );
}

function InlineState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="flex h-full min-h-0 items-center justify-center rounded-xl border border-dashed border-white/10 bg-slate-950/20 px-6 text-center">
      <div>
        <div className="text-base font-medium text-slate-200">{title}</div>
        <div className="mt-2 max-w-xl text-sm leading-6 text-slate-500">{detail}</div>
      </div>
    </div>
  );
}

function QuickBalances({ balances, baseAsset, quoteAsset, fee }: { balances: BalanceItem[]; baseAsset: string; quoteAsset: string; fee?: FeeProfileItem }) {
  const visibleAssets = [quoteAsset, baseAsset];
  const balanceMap = Object.fromEntries(balances.map((item) => [item.asset, item]));
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-display text-base">资产</h3>
        <span className="text-xs text-slate-500">maker {fee?.maker_fee_rate ?? "-"} / taker {fee?.taker_fee_rate ?? "-"}</span>
      </div>
      <div className="space-y-2">
        {visibleAssets.map((asset) => {
          const item = balanceMap[asset];
          return (
            <div key={asset} className="grid grid-cols-[80px_1fr_1fr] gap-2 rounded-xl bg-white/5 px-3 py-2 text-sm">
              <span className="text-slate-300">{asset}</span>
              <span className="truncate text-right font-mono tabular-nums text-slate-100">{fmt(item?.available, 8)}</span>
              <span className="truncate text-right font-mono tabular-nums text-slate-500">{fmt(item?.frozen, 8)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function QuickOpenOrders({
  orders,
  priceDigits,
  quantityDigits,
  onCancel,
}: {
  orders: OrderItem[];
  priceDigits: number;
  quantityDigits: number;
  onCancel: (orderId: string) => void;
}) {
  return (
    <section className="panel rounded-2xl p-3">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-display text-base">当前委托</h3>
        <span className="text-xs text-slate-500">{orders.length} 笔</span>
      </div>
      <div className="max-h-[220px] space-y-2 overflow-auto pr-1">
        {orders.slice(0, 8).map((item) => (
          <div key={item.order_id} className="rounded-xl bg-white/5 px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <span className={`text-sm font-medium ${sideColor(item.side)}`}>{item.side === "buy" ? "买入" : "卖出"} · {item.type}</span>
              <button type="button" onClick={() => onCancel(item.order_id)} className="rounded-lg bg-rose-500/12 px-2 py-1 text-xs text-rose-100 hover:bg-rose-500/18">
                撤单
              </button>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-xs tabular-nums">
              <span className="truncate text-slate-300">{fmt(item.price ?? item.avg_price, priceDigits)}</span>
              <span className="truncate text-right text-slate-300">{fmt(item.remaining_quantity, quantityDigits)}</span>
              <span className="truncate text-right text-slate-500">{bjTime(item.created_at)}</span>
            </div>
          </div>
        ))}
        {orders.length === 0 && <div className="rounded-xl bg-slate-950/30 px-3 py-6 text-center text-sm text-slate-500">暂无当前委托</div>}
      </div>
    </section>
  );
}
