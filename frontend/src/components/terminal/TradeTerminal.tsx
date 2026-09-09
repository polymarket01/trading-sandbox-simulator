import { useCallback, useMemo, useState } from "react";
import { KlineChart } from "../KlineChart";
import { sanitizeKlines } from "../../lib/kline";
import { fmt } from "../../lib/format";
import { RelativeTime, SkeletonBlock, EmptyState, ErrorState } from "../ui";
import { TerminalHeader } from "./TerminalHeader";
import { TerminalOrderBook } from "./TerminalOrderBook";
import { TerminalTrades } from "./TerminalTrades";
import { TerminalOrderPanel } from "./TerminalOrderPanel";
import { TerminalOrdersTabs } from "./TerminalOrdersTabs";
import type { TerminalProps } from "./types";

export function TradeTerminal(props: TerminalProps) {
  const {
    symbol,
    markets,
    currentMarket,
    isPerp,
    ticker,
    orderbook,
    orderbookUpdatedAt,
    orderbookSeq,
    recentTrades,
    klines,
    klineMeta,
    klineLoading,
    klineError,
    balances,
    perpAccount,
    positions,
    openOrders,
    historyOrders,
    userFills,
    fundingState,
    priceDigits,
    quantityDigits,
    tickSize,
    marketStatus,
    publicStatus,
    privateStatus,
    tickerUpdatedAt,
    klinesUpdatedAt,
    selectedInterval,
    orderbookDepth,
    mergeTicks,
    intervalOptions,
    onSelectSymbol,
    onIntervalChange,
    onDepthChange,
    onMergeChange,
    onRefreshKlines,
    onPlaceOrder,
    onCancelOrder,
    onAmendOrder,
    onClosePosition,
    submitting,
    submitError,
    submitNotice,
    orderPanelState,
    setOrderPanelState,
    extraStatus,
    extraHeaderRight,
    closingIds,
  } = props;

  const [chartTab, setChartTab] = useState<"chart" | "funding">("chart");
  const visibleKlines = useMemo(() => sanitizeKlines(klines, symbol, true), [klines, symbol]);
  const displayTrades = recentTrades.filter((item) => item.source !== "bootstrap_seed");
  const latestPrice = ticker?.last_price ?? ticker?.mid_price ?? ticker?.best_bid ?? ticker?.best_ask ?? currentMarket?.reference_price ?? "0";
  const bestBid = ticker?.best_bid ?? "0";
  const bestAsk = ticker?.best_ask ?? "0";
  const spreadPct = ticker?.spread_pct ?? "0";
  const lastTrade = displayTrades[0];
  const lastSide = lastTrade?.side ?? lastTrade?.taker_side ?? (Number(ticker?.change_24h_pct ?? 0) >= 0 ? "buy" : "sell");
  const userOrderPrices = openOrders.filter((item) => item.type === "limit" && item.price).map((item) => String(item.price));
  const quoteBalance = balances.find((item) => item.asset === currentMarket?.quote_asset)?.available;
  const baseBalance = balances.find((item) => item.asset === currentMarket?.base_asset)?.available;
  const accountBalance = isPerp ? perpAccount?.wallet_balance : quoteBalance;
  const availableBalance = isPerp ? perpAccount?.available_margin : baseBalance;
  const accountPnl = isPerp ? perpAccount?.unrealized_pnl : undefined;

  const selectBookPrice = useCallback((price: string) => {
    const value = Number(price);
    if (Number.isFinite(value) && value > 0) setOrderPanelState({ price: value.toFixed(priceDigits) });
  }, [setOrderPanelState, priceDigits]);
  return (
    <div className="hl-terminal">
      <TerminalHeader
        symbol={symbol}
        markets={markets}
        currentMarket={currentMarket}
        isPerp={isPerp}
        ticker={ticker}
        priceDigits={priceDigits}
        bestBid={bestBid}
        bestAsk={bestAsk}
        spreadPct={spreadPct}
        marketStatus={marketStatus}
        fundingState={fundingState}
        maxLeverage={currentMarket?.max_leverage ?? undefined}
        publicStatus={publicStatus}
        privateStatus={privateStatus}
        tickerUpdatedAt={tickerUpdatedAt}
        onSelectSymbol={onSelectSymbol}
        extraRight={extraHeaderRight}
      />

      <div className="hl-transport-line">
        <span>Market data <RelativeTime ts={tickerUpdatedAt} /></span>
        <span>Chart <RelativeTime ts={klinesUpdatedAt} /></span>
        {orderbookSeq !== undefined ? <span className="font-mono">Book seq {orderbookSeq}</span> : <span>Book -</span>}
        {extraStatus}
      </div>

      <div className="hl-workspace">
        <section className="panel hl-chart-panel flex min-h-0 min-w-0 flex-col rounded-2xl p-0">
          <div className="hl-chart-head flex shrink-0 flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-1">
              <button type="button" onClick={() => setChartTab("chart")} className={`border-b-2 px-2 py-2 text-xs ${chartTab === "chart" ? "border-emerald-300 text-slate-100" : "border-transparent text-slate-500"}`}>Chart</button>
              <button type="button" onClick={() => setChartTab("funding")} className={`border-b-2 px-2 py-2 text-xs ${chartTab === "funding" ? "border-emerald-300 text-slate-100" : "border-transparent text-slate-500"}`}>Funding</button>
              <span className="ml-2 text-[10px] text-slate-500">{symbol} · {selectedInterval}</span>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-1">
              {intervalOptions.map((item) => (
                <button key={item} type="button" onClick={() => onIntervalChange(item)} className={`rounded px-2 py-1 text-[10px] ${selectedInterval === item ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}>
                  {item}
                </button>
              ))}
            </div>
          </div>

          <div className="hl-chart-body min-h-0 flex-1">
            {chartTab === "funding" ? (
              <FundingPanel isPerp={isPerp} fundingState={fundingState} priceDigits={priceDigits} />
            ) : klineError && visibleKlines.length === 0 ? (
              <ErrorState message={`K 线加载失败：${klineError}`} onRetry={onRefreshKlines} />
            ) : klineLoading && visibleKlines.length === 0 ? (
              <div className="flex h-full min-h-[420px] flex-col justify-end space-y-2 p-3">
                <SkeletonBlock className="h-72" />
                <SkeletonBlock className="h-14" />
                <SkeletonBlock className="h-3 w-2/3" />
              </div>
            ) : visibleKlines.length > 0 ? (
              <KlineChart data={visibleKlines} interval={selectedInterval} symbol={symbol} pricePrecision={priceDigits} quantityPrecision={quantityDigits} priceTick={String(tickSize)} compact />
            ) : (
              <EmptyState title="暂无 K 线数据" detail="该周期暂时没有可用成交数据；成交产生后实时聚合。" />
            )}
          </div>
        </section>

        <div className="hl-book-column min-h-0 min-w-0">
          <TerminalOrderBook
            symbol={symbol}
            bids={orderbook.bids}
            asks={orderbook.asks}
            lastPrice={lastTrade?.price}
            lastTradeAt={Number(lastTrade?.ts ?? lastTrade?.executed_at ?? 0) || undefined}
            lastSide={lastSide}
            updatedAt={orderbookUpdatedAt}
            depth={orderbookDepth}
            mergeTicks={mergeTicks}
            tickSize={tickSize}
            priceDigits={priceDigits}
            qtyDigits={quantityDigits}
            myOrderPrices={userOrderPrices}
            onDepthChange={onDepthChange}
            onMergeChange={onMergeChange}
            onSelectPrice={selectBookPrice}
            heightClass="h-full"
          />
          <TerminalTrades items={displayTrades} priceDigits={priceDigits} quantityDigits={quantityDigits} heightClass="h-full" />
        </div>

        <div className="hl-order-column min-h-0 min-w-0">
          <TerminalOrderPanel
            market={currentMarket}
            perpAccount={perpAccount}
            balances={balances}
            state={orderPanelState}
            onChange={setOrderPanelState}
            latestPrice={latestPrice}
            bestBid={bestBid}
            bestAsk={bestAsk}
            priceDigits={priceDigits}
            quantityDigits={quantityDigits}
            marketStatus={marketStatus}
            maxLeverage={currentMarket?.max_leverage ?? undefined}
            submitting={submitting}
            submitError={submitError}
            submitNotice={submitNotice}
            onSubmit={() => void onPlaceOrder(orderPanelState)}
          />
        </div>
      </div>

      <TerminalOrdersTabs
        positions={positions}
        openOrders={openOrders}
        historyOrders={historyOrders}
        fills={userFills}
        priceDigits={priceDigits}
        quantityDigits={quantityDigits}
        closingIds={closingIds}
        onClose={(position) => void onClosePosition(position)}
        onCancel={(order) => void onCancelOrder(order.order_id)}
        onAmend={onAmendOrder}
      />

      <div className="hl-account-strip">
        <div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-slate-500">Account Equity</div>
          <div className="mt-1 font-mono text-slate-100">{accountBalance === undefined ? "-" : `$${fmt(accountBalance, 2)}`}</div>
        </div>
        <AccountMetric label={isPerp ? "Available Margin" : `Available ${currentMarket?.base_asset ?? "Asset"}`} value={availableBalance === undefined ? "-" : fmt(availableBalance, isPerp ? 2 : quantityDigits)} />
        <AccountMetric label={isPerp ? "Unrealized PNL" : "Open Orders"} value={isPerp ? (accountPnl === undefined ? "-" : fmt(accountPnl, 2)) : String(openOrders.length)} tone={isPerp && Number(accountPnl) < 0 ? "sell" : "buy"} />
        <AccountMetric label="Positions" value={String(positions.length)} />
        <AccountMetric label="Connection" value={publicStatus === "open" && privateStatus === "open" ? "Online" : "Degraded"} tone={publicStatus === "open" && privateStatus === "open" ? "buy" : "warn"} />
      </div>
    </div>
  );
}

function AccountMetric({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "buy" | "sell" | "warn" | "neutral" }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.12em] text-slate-500">{label}</div>
      <div className={`mt-1 font-mono tabular-nums ${tone === "buy" ? "text-emerald-300" : tone === "sell" ? "text-rose-300" : tone === "warn" ? "text-amber-200" : "text-slate-200"}`}>{value}</div>
    </div>
  );
}

function FundingPanel({ isPerp, fundingState, priceDigits }: { isPerp: boolean; fundingState?: TerminalProps["fundingState"]; priceDigits: number }) {
  if (!isPerp) return <EmptyState title="Funding unavailable" detail="Spot markets do not expose a funding-rate contract." />;
  return (
    <div className="grid h-full content-start gap-2 p-4 text-sm">
      <div className="border-b border-white/8 pb-3 text-xs text-slate-400">Perpetual funding snapshot</div>
      <FundingRow label="Mark Price" value={fundingState?.mark_price ? fmt(fundingState.mark_price, priceDigits) : "-"} />
      <FundingRow label="Index Price" value={fundingState?.index_price ? fmt(fundingState.index_price, priceDigits) : "-"} />
      <FundingRow label="Funding Rate" value={fundingState?.funding_rate === undefined ? "-" : `${(Number(fundingState.funding_rate) * 100).toFixed(4)}%`} />
      <FundingRow label="Next Funding" value={fundingState?.next_funding_time ? new Date(fundingState.next_funding_time).toLocaleString("zh-CN") : "-"} />
      <div className="mt-3 rounded border border-amber-300/15 bg-amber-300/5 p-3 text-xs leading-5 text-amber-100/80">Funding values are read from the active market contract and are not estimated in the browser.</div>
    </div>
  );
}

function FundingRow({ label, value }: { label: string; value: string }) {
  return <div className="flex items-center justify-between border-b border-white/5 py-2"><span className="text-slate-500">{label}</span><span className="font-mono tabular-nums text-slate-200">{value}</span></div>;
}
