import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { KlineChart } from "../components/KlineChart";
import { OrderbookPanel } from "../components/OrderbookPanel";
import { ToastViewport } from "../components/ToastViewport";
import { TradesPanel } from "../components/TradesPanel";
import { api } from "../api/client";
import { useMarketStreams } from "../hooks/useMarketStreams";
import { fmt, fmtPct, stepDigits } from "../lib/format";
import { sanitizeKlines } from "../lib/kline";
import { summarizeOrderbook } from "../lib/orderbook";
import { useAppStore } from "../store/useAppStore";
import type { ContractPriceState, KlineResponse, Level, MarketDefinition, MarketHealth, MarketTicker, TradeItem } from "../types";

const EMPTY_ORDERBOOK = { bids: [], asks: [] };

export function ContractMarketPage() {
  const { symbol = "BTCUSDT-PERP" } = useParams();
  const currentSymbol = symbol.toUpperCase();
  const navigate = useNavigate();
  const tickerSymbol = useAppStore((state) => state.tickerSymbol);
  const rawTicker = useAppStore((state) => state.ticker);
  const ticker = tickerSymbol === currentSymbol ? rawTicker : undefined;
  const orderbookSymbol = useAppStore((state) => state.orderbookSymbol);
  const rawOrderbook = useAppStore((state) => state.orderbook);
  const orderbook = orderbookSymbol === currentSymbol ? rawOrderbook : EMPTY_ORDERBOOK;
  const orderbookUpdatedAt = useAppStore((state) => state.orderbookUpdatedAt);
  const recentTradesSymbol = useAppStore((state) => state.recentTradesSymbol);
  const rawTrades = useAppStore((state) => state.recentTrades);
  const trades = recentTradesSymbol === currentSymbol ? rawTrades : [];
  const klinesSymbol = useAppStore((state) => state.klinesSymbol);
  const rawKlines = useAppStore((state) => state.klines);
  const klines = klinesSymbol === currentSymbol ? rawKlines : [];
  const setTicker = useAppStore((state) => state.setTicker);
  const setOrderbook = useAppStore((state) => state.setOrderbook);
  const setOrderbookDepth = useAppStore((state) => state.setOrderbookDepth);
  const setRecentTrades = useAppStore((state) => state.setRecentTrades);
  const setKlines = useAppStore((state) => state.setKlines);
  const [markets, setMarkets] = useState<MarketDefinition[]>([]);
  const [priceState, setPriceState] = useState<ContractPriceState>();
  const [health, setHealth] = useState<MarketHealth>();
  const [depth, setDepth] = useState(50);
  const [mergeTicks, setMergeTicks] = useState(1);
  const [selectedPrice, setSelectedPrice] = useState("");
  const [loading, setLoading] = useState(true);
  const market = markets.find((item) => item.symbol === symbol);
  const priceDigits = market?.price_precision ?? stepDigits(market?.price_tick) ?? 2;
  const quantityDigits = market?.qty_precision ?? stepDigits(market?.qty_step) ?? 3;
  const tickSize = Number(market?.price_tick ?? "0.01");
  const bids = orderbook.bids;
  const asks = orderbook.asks;
  const book = summarizeOrderbook(bids, asks);
  const visibleKlines = useMemo(() => sanitizeKlines(klines, symbol, false), [klines, symbol]);

  useMarketStreams(symbol, "1m", depth, false);

  const load = async () => {
    setLoading(true);
    try {
      const [marketList, tickerData, priceData, tradesData, klineData, healthData] = await Promise.all([
        api.get<{ items: MarketDefinition[] }>("/contracts/markets"),
        api.get<MarketTicker>(`/markets/${symbol}/ticker`),
        api.get<ContractPriceState>(`/contracts/prices/${symbol}?refresh_external=true`),
        api.get<{ items: TradeItem[] }>(`/markets/${symbol}/trades?limit=80&include_seed=false`),
        api.get<KlineResponse>(`/markets/${symbol}/klines?interval=1m&limit=220&include_seed=false`),
        api.get<MarketHealth>(`/markets/${symbol}/health`),
      ]);
      setMarkets(marketList.items);
      setTicker(currentSymbol, tickerData);
      setPriceState(priceData);
      setRecentTrades(currentSymbol, tradesData.items);
      setKlines(currentSymbol, klineData.items);
      setHealth(healthData);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setTicker(currentSymbol, undefined);
    setPriceState(undefined);
    setHealth(undefined);
    setOrderbook(symbol, { bids: [], asks: [] });
    setRecentTrades(currentSymbol, []);
    setKlines(currentSymbol, []);
  }, [currentSymbol, symbol, setTicker, setOrderbook, setRecentTrades, setKlines]);

  useEffect(() => {
    setOrderbookDepth(depth);
  }, [depth, setOrderbookDepth]);

  useEffect(() => {
    void load();
  }, [symbol, depth]);

  return (
    <AppShell>
      <ToastViewport />
      <section className="panel mb-3 rounded-2xl p-4">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-display text-2xl text-white">{symbol.replace("USDT", "/USDT")}</h1>
              <span className="rounded-lg bg-violet-400/16 px-2 py-1 text-xs text-violet-100">PERP</span>
              <span className="rounded-lg bg-white/6 px-2 py-1 text-xs text-slate-300">{market?.margin_asset ?? "USDT"} 本位</span>
            </div>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
              指数价采用 Binance USD-M premiumIndex；标记价由本地合约盘口中间价、最近成交和相对指数价的限幅基差计算；资金费率支持 Binance 直连与本地公式两种模式。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select
              value={symbol}
              onChange={(event) => navigate(`/contracts/markets/${event.target.value}`)}
              className="h-10 rounded-xl border border-white/10 bg-slate-950/55 px-3 text-sm text-slate-100 outline-none"
            >
              {markets.map((item) => (
                <option key={item.symbol} value={item.symbol}>{item.symbol}</option>
              ))}
            </select>
            <button type="button" onClick={() => navigate(`/contracts/trade/${symbol}`)} className="rounded-xl bg-cyan-400/16 px-4 text-sm text-cyan-100">
              去交易
            </button>
          </div>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-4 xl:grid-cols-8">
          <MarketMetric label="最新价" value={fmt(ticker?.last_price, priceDigits)} tone={Number(ticker?.change_24h_pct ?? 0) >= 0 ? "up" : "down"} />
          <MarketMetric label="24H" value={fmtPct(ticker?.change_24h_pct, 2)} tone={Number(ticker?.change_24h_pct ?? 0) >= 0 ? "up" : "down"} />
          <MarketMetric label="指数价" value={fmt(priceState?.index_price, priceDigits)} />
          <MarketMetric label="标记价" value={fmt(priceState?.mark_price, priceDigits)} />
          <MarketMetric label="资金费率" value={`${fmt(Number(priceState?.funding_rate ?? 0) * 100, 4)}%`} />
          <MarketMetric label="Premium" value={`${fmt(Number(priceState?.premium_index ?? 0) * 100, 4)}%`} />
          <MarketMetric label="0.5%深度" value={fmt(book.depth_amount_0_5pct, 0)} />
          <MarketMetric label="风控" value={market?.contract_trading_mode ?? "normal"} tone={market?.contract_trading_mode === "paused" ? "down" : market?.contract_trading_mode === "reduce_only" ? "warn" : undefined} />
          <MarketMetric label="健康" value={health?.status ?? (loading ? "loading" : "-")} />
        </div>
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_370px]">
        <section className="panel h-[520px] rounded-2xl p-3">
          {visibleKlines.length > 0 ? (
            <KlineChart
              data={visibleKlines}
              interval="1m"
              symbol={symbol}
              pricePrecision={priceDigits}
              quantityPrecision={quantityDigits}
              priceTick={market?.price_tick ?? "0.01"}
            />
          ) : (
            <div className="flex h-full items-center justify-center rounded-xl border border-dashed border-white/10 bg-slate-950/20 text-sm text-slate-500">
              等待合约真实成交生成 K 线
            </div>
          )}
        </section>
        <div className="grid gap-3">
          <OrderbookPanel
            symbol={symbol}
            bids={bids}
            asks={asks}
            lastPrice={trades[0]?.price ?? ticker?.last_price}
            lastSide={trades[0]?.side ?? trades[0]?.taker_side}
            updatedAt={orderbookUpdatedAt}
            depth={depth}
            mergeTicks={mergeTicks}
            tickSize={tickSize}
            priceDigits={priceDigits}
            qtyDigits={quantityDigits}
            onDepthChange={setDepth}
            onMergeChange={setMergeTicks}
            onSelectPrice={setSelectedPrice}
            heightClass="h-[340px]"
          />
          <TradesPanel items={trades} priceDigits={priceDigits} quantityDigits={quantityDigits} heightClass="h-[170px]" />
        </div>
      </div>
      <section className="panel mt-3 rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-display text-base">合约机制</h2>
          <span className="text-xs text-slate-500">选中价 {selectedPrice || "-"}</span>
        </div>
        <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
          <MarketMetric label="指数源" value={priceState?.index_source ?? market?.index_price_source ?? "-"} />
          <MarketMetric label="标记价模式" value={priceState?.mark_source ?? market?.mark_price_mode ?? "-"} />
          <MarketMetric label="资金模式" value={priceState?.funding_rate_mode ?? market?.funding_rate_mode ?? "-"} />
          <MarketMetric label="影响名义" value={fmt(priceState?.impact_notional ?? market?.funding_impact_notional, 0)} />
          <MarketMetric label="影响买价" value={fmt(priceState?.impact_bid_price, priceDigits)} />
          <MarketMetric label="影响卖价" value={fmt(priceState?.impact_ask_price, priceDigits)} />
        </div>
      </section>
    </AppShell>
  );
}

function MarketMetric({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "neutral" | "up" | "down" | "warn" }) {
  const color = tone === "up" ? "text-emerald-300" : tone === "down" ? "text-rose-300" : tone === "warn" ? "text-amber-200" : "text-slate-100";
  return (
    <div className="min-w-0 rounded-xl bg-white/5 px-3 py-2">
      <div className="truncate text-xs text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-sm tabular-nums ${color}`} title={value}><span className="num-fixed num-money">{value}</span></div>
    </div>
  );
}
