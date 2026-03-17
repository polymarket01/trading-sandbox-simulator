import { startTransition, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { AccountCard } from "../components/AccountCard";
import { AppShell } from "../components/AppShell";
import { KlineChart } from "../components/KlineChart";
import { OrderForm } from "../components/OrderForm";
import { OrderbookPanel } from "../components/OrderbookPanel";
import { OrdersTabs } from "../components/OrdersTabs";
import { ToastViewport } from "../components/ToastViewport";
import { TradesPanel } from "../components/TradesPanel";
import { useMarketStreams } from "../hooks/useMarketStreams";
import { usePrivateStream } from "../hooks/usePrivateStream";
import { config } from "../lib/config";
import { fmt, fmtPct, stepDigits } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type { KlineItem, MarketDefinition, MarketTicker, OrderItem, TradeItem } from "../types";

const intervals = ["1s", "5s", "15s", "1m", "5m", "15m", "1d"];

export function TradePage() {
  const navigate = useNavigate();
  const { symbol = "BTCUSDT" } = useParams();
  const markets = useAppStore((state) => state.markets);
  const ticker = useAppStore((state) => state.ticker);
  const orderbook = useAppStore((state) => state.orderbook);
  const recentTrades = useAppStore((state) => state.recentTrades);
  const klines = useAppStore((state) => state.klines);
  const balances = useAppStore((state) => state.balances);
  const openOrders = useAppStore((state) => state.openOrders);
  const orderHistory = useAppStore((state) => state.orderHistory);
  const accountTrades = useAppStore((state) => state.accountTrades);
  const ledger = useAppStore((state) => state.ledger);
  const selectedInterval = useAppStore((state) => state.selectedInterval);
  const orderbookDepth = useAppStore((state) => state.orderbookDepth);
  const setMarkets = useAppStore((state) => state.setMarkets);
  const setTicker = useAppStore((state) => state.setTicker);
  const setOrderbook = useAppStore((state) => state.setOrderbook);
  const setRecentTrades = useAppStore((state) => state.setRecentTrades);
  const setKlines = useAppStore((state) => state.setKlines);
  const setBalances = useAppStore((state) => state.setBalances);
  const setOpenOrders = useAppStore((state) => state.setOpenOrders);
  const setOrderHistory = useAppStore((state) => state.setOrderHistory);
  const setAccountTrades = useAppStore((state) => state.setAccountTrades);
  const setLedger = useAppStore((state) => state.setLedger);
  const setSelectedInterval = useAppStore((state) => state.setSelectedInterval);
  const setOrderbookDepth = useAppStore((state) => state.setOrderbookDepth);
  const pushToast = useAppStore((state) => state.pushToast);
  const [selectedPrice, setSelectedPrice] = useState<string>();
  const [mergeTicks, setMergeTicks] = useState(1);
  const [marketMap, setMarketMap] = useState<Record<string, MarketDefinition>>({});

  const market = marketMap[symbol];
  const priceDigits = market?.price_precision ?? stepDigits(market?.price_tick) ?? 4;
  const quantityDigits = market?.qty_precision ?? stepDigits(market?.qty_step) ?? 4;
  const tickSize = Number(market?.price_tick ?? "0.1");

  useMarketStreams(symbol, selectedInterval, orderbookDepth);
  usePrivateStream(symbol);

  const refreshAll = async () => {
    const [
      marketList,
      tickerResponse,
      orderbookResponse,
      tradesResponse,
      klinesResponse,
      balancesResponse,
      openOrdersResponse,
      historyResponse,
      accountTradesResponse,
      ledgerResponse,
    ] = await Promise.all([
      api.get<{ items: MarketDefinition[] }>("/markets"),
      api.get<MarketTicker>(`/markets/${symbol}/ticker`),
      api.get<{ bids: [string, string][]; asks: [string, string][] }>(`/markets/${symbol}/orderbook?depth=${orderbookDepth}`),
      api.get<{ items: TradeItem[] }>(`/markets/${symbol}/trades?limit=100`),
      api.get<{ items: KlineItem[] }>(`/markets/${symbol}/klines?interval=${selectedInterval}&limit=300`),
      api.get<{ items: unknown[] }>("/account/balances", config.manualApiKey),
      api.get<{ items: OrderItem[] }>(`/account/orders/open?symbol=${symbol}`, config.manualApiKey),
      api.get<{ items: OrderItem[] }>(`/account/orders/history?symbol=${symbol}&limit=500`, config.manualApiKey),
      api.get<{ items: TradeItem[] }>(`/account/trades?symbol=${symbol}&limit=500`, config.manualApiKey),
      api.get<{ items: unknown[] }>("/account/ledger?limit=500", config.manualApiKey),
    ]);
    startTransition(() => {
      setMarkets(marketList.items.map((item) => item.symbol));
      setMarketMap(Object.fromEntries(marketList.items.map((item) => [item.symbol, item])));
      setTicker(tickerResponse);
      setOrderbook({ bids: orderbookResponse.bids, asks: orderbookResponse.asks });
      setRecentTrades(tradesResponse.items);
      setKlines(klinesResponse.items);
      setBalances(balancesResponse.items as never[]);
      setOpenOrders(openOrdersResponse.items);
      setOrderHistory(historyResponse.items);
      setAccountTrades(accountTradesResponse.items);
      setLedger(ledgerResponse.items as never[]);
    });
  };

  useEffect(() => {
    void refreshAll().catch((error) => pushToast("error", error instanceof Error ? error.message : "加载失败"));
  }, [symbol, selectedInterval, orderbookDepth]);

  const cancelOrder = async (orderId: string) => {
    try {
      await api.delete(`/orders/${orderId}`, config.manualApiKey);
      await refreshAll();
      pushToast("success", "订单已撤销");
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "撤单失败");
    }
  };

  return (
    <AppShell>
      <ToastViewport />
      <div className="mb-4">
        <section className="panel rounded-3xl p-4">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex flex-wrap items-center gap-3">
              {markets.map((item) => (
                <button
                  key={item}
                  onClick={() => navigate(`/trade/${item}`)}
                  className={`rounded-full px-4 py-2 text-sm ${symbol === item ? "bg-cyan-400/16 text-cyan-100" : "bg-white/5 text-slate-300"}`}
                >
                  {item.replace("USDT", "/USDT")}
                </button>
              ))}
            </div>
            <div className="grid gap-3 text-sm text-slate-300 md:grid-cols-3 xl:grid-cols-9">
              <Stat label="最新价" value={fmt(ticker?.last_price, priceDigits)} />
              <Stat label="买一 / 卖一" value={`${fmt(ticker?.best_bid, priceDigits)} / ${fmt(ticker?.best_ask, priceDigits)}`} />
              <Stat label="24H 涨跌" value={fmtPct(ticker?.change_24h_pct, 2)} />
              <Stat label="24H 成交量" value={fmt(ticker?.volume_24h, quantityDigits)} />
              <Stat label="24H 成交额" value={`${fmt(ticker?.quote_volume_24h, 0)} USDT`} />
              <Stat label="Spread" value={fmtPct(ticker?.spread_pct, 4)} />
              <Stat label="0.5% 深度金额" value={`${fmt(ticker?.depth_amount_0_5pct, 0)} USDT`} />
              <Stat label="2% 深度金额" value={`${fmt(ticker?.depth_amount_2pct, 0)} USDT`} />
              <Stat label="状态" value={ticker?.is_active ? "active" : "paused"} />
            </div>
          </div>
        </section>
      </div>
      <div className="grid gap-4 xl:grid-cols-[1.58fr_0.9fr_0.58fr]">
        <section className="panel flex h-[760px] min-h-0 flex-col rounded-3xl p-4">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-display text-lg">K 线</h2>
              <p className="mt-1 text-xs text-slate-500">固定按北京时间显示，且完全由 TAS 最新成交聚合生成。</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {intervals.map((item) => (
                <button key={item} onClick={() => setSelectedInterval(item)} className={`rounded-full px-3 py-1.5 text-xs ${selectedInterval === item ? "bg-cyan-400/18 text-cyan-100" : "bg-white/5 text-slate-300"}`}>
                  {item}
                </button>
              ))}
            </div>
          </div>
          <KlineChart
            data={klines}
            interval={selectedInterval}
            symbol={symbol}
            pricePrecision={priceDigits}
            quantityPrecision={quantityDigits}
            priceTick={market?.price_tick ?? "0.1"}
          />
        </section>
        <OrderbookPanel
          symbol={symbol}
          bids={orderbook.bids}
          asks={orderbook.asks}
          depth={orderbookDepth}
          mergeTicks={mergeTicks}
          tickSize={tickSize}
          priceDigits={priceDigits}
          qtyDigits={quantityDigits}
          onDepthChange={setOrderbookDepth}
          onMergeChange={setMergeTicks}
          onSelectPrice={setSelectedPrice}
        />
        <TradesPanel items={recentTrades} priceDigits={priceDigits} quantityDigits={quantityDigits} />
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-[0.72fr_1.48fr]">
        <div className="space-y-4">
          <OrderForm
            symbol={symbol}
            ticker={ticker}
            balances={balances}
            selectedPrice={selectedPrice}
            priceDigits={priceDigits}
            quantityDigits={quantityDigits}
            onSubmitted={refreshAll}
          />
          <AccountCard balances={balances} ticker={ticker} />
        </div>
        <OrdersTabs
          openOrders={openOrders}
          orderHistory={orderHistory}
          accountTrades={accountTrades}
          ledger={ledger}
          priceDigits={priceDigits}
          quantityDigits={quantityDigits}
          onCancel={cancelOrder}
        />
      </div>
    </AppShell>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-white/5 px-3 py-2">
      <div className="text-xs uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className="mt-1 text-sm text-white font-mono tabular-nums">{value}</div>
    </div>
  );
}
