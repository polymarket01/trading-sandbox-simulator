import { isActiveContractPosition } from "../lib/paper";
import { preparePositionClose } from "../lib/close-position";
import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ToastViewport } from "../components/ToastViewport";
import { TradeTerminal } from "../components/terminal/TradeTerminal";
import type { TerminalMarketInfo, TerminalOrderPanelState, TerminalPosition } from "../components/terminal/types";
import { useMarketStreams } from "../hooks/useMarketStreams";
import { usePrivateStream } from "../hooks/usePrivateStream";
import { fmt, stepDigits } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type {
  BalanceItem,
  ContractAccount,
  ContractPosition,
  ContractPriceState,
  FeeProfileItem,
  KlineMeta,
  KlineResponse,
  Level,
  MakerInstancePublicStatus,
  MarketDefinition,
  MarketTicker,
  OrderItem,
  TradeItem,
} from "../types";

const EMPTY_ORDERBOOK: { bids: Level[]; asks: Level[] } = { bids: [], asks: [] };

const toTerminalMarket = (market: MarketDefinition): TerminalMarketInfo => ({
  symbol: market.symbol,
  product_type: market.product_type,
  base_asset: market.base_asset,
  quote_asset: market.quote_asset,
  reference_price: market.reference_price,
  is_active: market.is_active,
  max_leverage: market.max_leverage,
});

const toTerminalPosition = (position: ContractPosition): TerminalPosition => ({
  symbol: position.symbol,
  side: position.side,
  quantity: position.quantity,
  entry_price: position.entry_price,
  mark_price: position.mark_price,
  liquidation_price: position.liquidation_price,
  leverage: position.leverage,
  isolated_margin: position.isolated_margin,
  unrealized_pnl: position.unrealized_pnl,
  realized_pnl: position.realized_pnl,
  risk_status: position.risk_status,
  liquidation_distance_pct: position.liquidation_distance_pct,
  margin_buffer: position.margin_buffer,
  updated_at: position.updated_at,
});

export function TradePage() {
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
  const orderbookSeq = useAppStore((state) => state.orderbookSeq);
  const orderbookUpdatedAt = useAppStore((state) => state.orderbookUpdatedAt);
  const recentTradesSymbol = useAppStore((state) => state.recentTradesSymbol);
  const rawRecentTrades = useAppStore((state) => state.recentTrades);
  const recentTrades = recentTradesSymbol === currentSymbol ? rawRecentTrades : [];
  const klinesSymbol = useAppStore((state) => state.klinesSymbol);
  const rawKlines = useAppStore((state) => state.klines);
  const klines = klinesSymbol === currentSymbol ? rawKlines : [];
  const balances = useAppStore((state) => state.balances);
  const openOrders = useAppStore((state) => state.openOrders);
  const orderHistory = useAppStore((state) => state.orderHistory);
  const accountTrades = useAppStore((state) => state.accountTrades);
  const selectedInterval = useAppStore((state) => state.selectedInterval);
  const orderbookDepth = useAppStore((state) => state.orderbookDepth);
  const orderbookUpdatedAtStore = useAppStore((state) => state.orderbookUpdatedAt);
  const tickerUpdatedAt = useAppStore((state) => state.tickerUpdatedAt);
  const klinesUpdatedAt = useAppStore((state) => state.klinesUpdatedAt);
  const publicStreamStatus = useAppStore((state) => state.publicStreamStatus);
  const privateStreamStatus = useAppStore((state) => state.privateStreamStatus);
  const setMarkets = useAppStore((state) => state.setMarkets);
  const setTicker = useAppStore((state) => state.setTicker);
  const setOrderbook = useAppStore((state) => state.setOrderbook);
  const setRecentTrades = useAppStore((state) => state.setRecentTrades);
  const setKlines = useAppStore((state) => state.setKlines);
  const setBalances = useAppStore((state) => state.setBalances);
  const setOpenOrders = useAppStore((state) => state.setOpenOrders);
  const setOrderHistory = useAppStore((state) => state.setOrderHistory);
  const setAccountTrades = useAppStore((state) => state.setAccountTrades);
  const setSelectedInterval = useAppStore((state) => state.setSelectedInterval);
  const setOrderbookDepth = useAppStore((state) => state.setOrderbookDepth);
  const pushToast = useAppStore((state) => state.pushToast);
  const authSession = useAppStore((state) => state.authSession);

  const [marketMap, setMarketMap] = useState<Record<string, MarketDefinition>>({});
  const [accountFees, setAccountFees] = useState<FeeProfileItem[]>([]);
  const [makerInstance, setMakerInstance] = useState<MakerInstancePublicStatus>();
  const [contractAccount, setContractAccount] = useState<ContractAccount>();
  const [contractPositions, setContractPositions] = useState<ContractPosition[]>([]);
  const [contractPriceState, setContractPriceState] = useState<ContractPriceState>();
  const [klineMeta, setKlineMeta] = useState<KlineMeta>();
  const [klineLoading, setKlineLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [cancelingIds, setCancelingIds] = useState<Set<string>>(() => new Set());
  const [orderPanelState, setOrderPanelStateState] = useState<TerminalOrderPanelState>({
    side: "buy",
    orderType: "limit",
    tif: "gtc",
    quantity: "",
    price: "",
    positionAction: "open",
    leverage: "5",
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [submitNotice, setSubmitNotice] = useState("");
  const refreshSeqRef = useRef(0);

  const market = marketMap[currentSymbol];
  const productType = market?.product_type ?? (currentSymbol.endsWith("-PERP") ? "PERP" : "SPOT");
  const isContract = productType === "PERP";
  const isAdmin = authSession?.role === "admin";
  const accountApiKey = authSession?.api_key ?? "";
  const loggedIn = Boolean(authSession);
  const priceDigits = market?.price_precision ?? stepDigits(market?.price_tick) ?? 4;
  const quantityDigits = market?.qty_precision ?? stepDigits(market?.qty_step) ?? 4;
  const tickSize = Math.max(Number(market?.price_tick ?? "0.01"), 0.00000001);
  const marketItems = markets.map((item) => marketMap[item]).filter((item): item is MarketDefinition => Boolean(item));
  const terminalMarkets = useMemo(() => marketItems.map(toTerminalMarket), [marketItems]);
  const terminalPositions = useMemo(() => contractPositions.filter(isActiveContractPosition).map(toTerminalPosition), [contractPositions]);
  const latestPrice = ticker?.last_price ?? market?.reference_price ?? "0";
  const statusText = ticker?.is_active === false ? "PAUSED" : "TRADING";

  useMarketStreams(currentSymbol, selectedInterval, orderbookDepth, false);
  usePrivateStream(currentSymbol);

  const setOrderPanelState = useCallback((patch: Partial<TerminalOrderPanelState>) => {
    setOrderPanelStateState((currentState) => ({ ...currentState, ...patch }));
  }, []);

  const refreshAll = async () => {
    if (!loggedIn) return;
    const requestId = ++refreshSeqRef.current;
    setKlineLoading(true);
    try {
      const marketList = isAdmin
        ? await api.get<{ items: MarketDefinition[] }>("/markets")
        : await api.get<{ items: MarketDefinition[] }>("/paper/markets");
      const nextMarketMap = Object.fromEntries(marketList.items.map((item) => [item.symbol, item]));
      const nextMarket = nextMarketMap[currentSymbol];
      const nextIsContract = nextMarket?.product_type === "PERP";
      if (requestId !== refreshSeqRef.current) return;
      // Market precision must not wait for unrelated account/history requests.
      setMarkets(marketList.items.map((item) => item.symbol));
      setMarketMap(nextMarketMap);
      const [
        tickerResponse,
        tradesResponse,
        klinesResponse,
        accountResponse,
        positionsResponse,
        settingResponse,
        priceStateResponse,
        openOrdersResponse,
        historyResponse,
        accountTradesResponse,
        feesResponse,
        makerInstanceResponse,
      ] = await Promise.all([
        api.get<MarketTicker>(`/markets/${currentSymbol}/ticker`),
        api.get<{ items: TradeItem[] }>(`/markets/${currentSymbol}/trades?limit=100`),
        api.get<KlineResponse>(`/markets/${currentSymbol}/klines?interval=${encodeURIComponent(selectedInterval)}&limit=300`),
        nextIsContract ? api.get<ContractAccount>("/contracts/account", accountApiKey) : api.get<{ items: BalanceItem[] }>("/account/balances", accountApiKey),
        nextIsContract ? api.get<{ items: ContractPosition[] }>(`/contracts/positions?symbol=${currentSymbol}`, accountApiKey).then((response) => {
          if (requestId === refreshSeqRef.current) setContractPositions(response.items);
          return response;
        }) : Promise.resolve({ items: [] as ContractPosition[] }),
        nextIsContract ? api.get<{ setting: { leverage: string } }>(`/contracts/settings/${currentSymbol}`, accountApiKey).catch(() => undefined) : Promise.resolve(undefined),
        nextIsContract ? api.get<ContractPriceState>(`/contracts/prices/${currentSymbol}?refresh_external=true`).catch(() => undefined) : Promise.resolve(undefined),
        nextIsContract ? api.get<{ items: OrderItem[] }>(`/contracts/orders/open?symbol=${currentSymbol}`, accountApiKey) : api.get<{ items: OrderItem[] }>(`/account/orders/open?symbol=${currentSymbol}`, accountApiKey),
        nextIsContract ? api.get<{ items: OrderItem[] }>(`/contracts/orders/history?symbol=${currentSymbol}&limit=500`, accountApiKey) : api.get<{ items: OrderItem[] }>(`/account/orders/history?symbol=${currentSymbol}&limit=500`, accountApiKey),
        nextIsContract ? api.get<{ items: TradeItem[] }>(`/contracts/trades?symbol=${currentSymbol}&limit=500`, accountApiKey) : api.get<{ items: TradeItem[] }>(`/account/trades?symbol=${currentSymbol}&limit=500`, accountApiKey),
        api.get<{ items: FeeProfileItem[] }>("/account/fees", accountApiKey),
        api.get<MakerInstancePublicStatus>(`/markets/${currentSymbol}/maker-instance`),
      ]);
      if (requestId !== refreshSeqRef.current) return;
      startTransition(() => {
        setMarkets(marketList.items.map((item) => item.symbol));
        setMarketMap(nextMarketMap);
        setTicker(currentSymbol, tickerResponse);
        setRecentTrades(currentSymbol, tradesResponse.items);
        setKlines(currentSymbol, klinesResponse.items);
        setKlineMeta(klinesResponse.meta);
        setBalances(nextIsContract ? [] : (accountResponse as { items: BalanceItem[] }).items);
        setContractAccount(nextIsContract ? (accountResponse as ContractAccount) : undefined);
        setContractPositions(nextIsContract ? positionsResponse.items : []);
        setContractPriceState(nextIsContract ? priceStateResponse : undefined);
        setOpenOrders(openOrdersResponse.items);
        setOrderHistory(historyResponse.items);
        setAccountTrades(accountTradesResponse.items);
        setAccountFees(feesResponse.items);
        setMakerInstance(makerInstanceResponse);
        setOrderPanelStateState((state) => ({
          ...state,
          price: state.price || (Number(tickerResponse.best_ask || tickerResponse.last_price) > 0
            ? Number(tickerResponse.best_ask || tickerResponse.last_price).toFixed(nextMarket?.price_precision ?? stepDigits(nextMarket?.price_tick) ?? 4)
            : ""),
          leverage: settingResponse?.setting?.leverage ?? state.leverage,
        }));
      });
      setLoadError("");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      if (requestId === refreshSeqRef.current) setKlineLoading(false);
    }
  };

  useEffect(() => {
    setKlineLoading(true);
    setKlines(currentSymbol, []);
    setKlineMeta(undefined);
  }, [currentSymbol, selectedInterval, setKlines]);

  useEffect(() => {
    if (!loggedIn) return;
    void refreshAll().catch((error) => pushToast("error", error instanceof Error ? error.message : "加载失败"));
    const timer = window.setInterval(() => {
      void refreshAll().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [currentSymbol, selectedInterval, orderbookDepth, loggedIn]);

  useEffect(() => {
    setSelectedPriceCleanup();
    setTicker(currentSymbol, undefined);
    setOrderbook(currentSymbol, { bids: [], asks: [] });
    setRecentTrades(currentSymbol, []);
    setKlines(currentSymbol, []);
    setOpenOrders([]);
    setOrderHistory([]);
    setAccountTrades([]);
    setContractAccount(undefined);
    setContractPositions([]);
    setContractPriceState(undefined);
    setMakerInstance(undefined);
    setSubmitNotice("");
    setSubmitError("");
  }, [currentSymbol, setTicker, setOrderbook, setRecentTrades, setKlines, setOpenOrders, setOrderHistory, setAccountTrades]);

  function setSelectedPriceCleanup() {
    setOrderPanelStateState((state) => ({ ...state, price: "", quantity: "" }));
  }

  const placeOrder = async (state: TerminalOrderPanelState) => {
    if (!loggedIn) {
      pushToast("error", "请先登录");
      return { status: "rejected", message: "请先登录" };
    }
    if (!state.quantity || Number(state.quantity) <= 0) {
      setSubmitError("请输入有效数量");
      return { status: "rejected", message: "请输入有效数量" };
    }
    if (state.orderType === "limit" && (!state.price || Number(state.price) <= 0)) {
      setSubmitError("限价单必须填写价格");
      return { status: "rejected", message: "限价单必须填写价格" };
    }
    setSubmitting(true);
    setSubmitError("");
    setSubmitNotice("");
    try {
      const payload: Record<string, unknown> = {
        symbol: currentSymbol,
        side: state.side,
        type: state.orderType,
        tif: state.orderType === "market" ? "ioc" : state.tif,
        quantity: state.quantity,
        client_order_id: `ui-${Date.now()}`,
      };
      if (state.orderType === "limit") payload.price = state.price;
      if (isContract) {
        payload.position_action = state.positionAction;
        payload.reduce_only = state.positionAction === "close";
        payload.leverage = state.leverage;
      }
      const endpoint = isContract ? "/contracts/orders" : "/orders";
      const response = await api.post<{ order: OrderItem }>(endpoint, payload, accountApiKey);
      const status = response.order.status;
      if (status === "filled") {
        pushToast("success", `${currentSymbol} 订单已全部成交`);
        setOrderPanelState({ quantity: "" });
        setSubmitNotice("订单已全部成交");
      } else if (status === "partially_filled") {
        pushToast("success", `${currentSymbol} 订单部分成交`);
        setSubmitNotice("订单已部分成交，剩余部分继续挂单");
      } else if (status === "rejected") {
        const message = `订单被拒绝：${response.order.reject_reason ?? "未知原因"}`;
        setSubmitError(message);
        pushToast("error", message);
      } else {
        pushToast("success", `订单已提交 · ${response.order.order_id}`);
        setSubmitNotice(`订单已提交 · ${response.order.order_id}`);
      }
      await refreshAll();
      return { status, orderId: response.order.order_id };
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "下单失败";
      setSubmitError(message);
      pushToast("error", `下单失败：${message}`);
      return { status: "rejected", message };
    } finally {
      setSubmitting(false);
    }
  };

  const cancelOrder = async (orderId: string) => {
    if (!loggedIn) return;
    setCancelingIds((current) => new Set(current).add(orderId));
    try {
      await api.delete(isContract ? `/contracts/orders/${orderId}` : `/orders/${orderId}`, accountApiKey);
      pushToast("success", "订单已撤销");
      await refreshAll();
    } catch (reason) {
      pushToast("error", reason instanceof Error ? reason.message : "撤单失败");
    } finally {
      setCancelingIds((current) => {
        const next = new Set(current);
        next.delete(orderId);
        return next;
      });
    }
  };

  const amendOrder = async (order: OrderItem, nextPrice?: string, nextQuantity?: string) => {
    if (!loggedIn) return;
    const payload: Record<string, unknown> = {};
    if (nextPrice && nextPrice !== String(order.price)) payload.price = nextPrice;
    if (nextQuantity && Number(nextQuantity) !== Number(order.quantity)) payload.quantity = nextQuantity;
    if (Object.keys(payload).length === 0) return;
    try {
      await api.patch(isContract ? `/contracts/orders/${order.order_id}` : `/orders/${order.order_id}`, payload, accountApiKey);
      pushToast("success", "订单已修改");
      await refreshAll();
    } catch (reason) {
      pushToast("error", reason instanceof Error ? reason.message : "改单失败");
    }
  };

  const closeBusyRef = useRef(false);
  const closePosition = async (position: TerminalPosition) => {
    if (!loggedIn || closeBusyRef.current) return;
    closeBusyRef.current = true;
    ++refreshSeqRef.current; // Retire refreshes that captured the old position.
    setSubmitting(true);
    setSubmitError("");
    let closeResponseReceived = false;
    try {
      const fresh = await preparePositionClose(api, accountApiKey, position.symbol, position.side);
      setContractPositions(fresh.items);
      const current = fresh.items.find((item) => item.symbol === position.symbol && item.side === position.side && Number(item.quantity) > 0);
      if (!current) {
        pushToast("info", "该方向已无可平持仓");
        return;
      }
      const payload: Record<string, unknown> = {
        symbol: position.symbol,
        side: position.side === "long" ? "sell" : "buy",
        type: "market",
        tif: "ioc",
        quantity: current.quantity,
        position_action: "close",
        reduce_only: true,
        leverage: current.leverage,
        client_order_id: `perp-close-${Date.now()}`,
      };
      const response = await api.post<{ order: OrderItem }>("/contracts/orders", payload, accountApiKey);
      closeResponseReceived = true;
      if (response.order.status === "filled") {
        pushToast("success", `${position.symbol} 仓位已市价平仓`);
      } else if (response.order.status === "rejected") {
        pushToast("error", `平仓被拒绝：${response.order.reject_reason ?? "未知原因"}`);
      } else {
        pushToast("info", `已成交 ${response.order.filled_quantity}，本单剩余 ${response.order.remaining_quantity} 未成交并已撤销；不会自动追单`);
      }
      // Position freshness must not wait on chart, history, or external price requests.
      const after = await api.get<{ items: ContractPosition[] }>(`/contracts/positions?symbol=${position.symbol}`, accountApiKey);
      ++refreshSeqRef.current;
      setContractPositions(after.items);
      void refreshAll();
    } catch (reason) {
      pushToast("error", closeResponseReceived ? "订单结果已返回，但仓位刷新失败，请刷新持仓后查看" : reason instanceof Error ? reason.message : "平仓失败");
    } finally {
      closeBusyRef.current = false;
      setSubmitting(false);
    }
  };

  const makerRunning = makerInstance?.running;
  const makerLabel = makerInstance
    ? makerRunning
      ? makerInstance.status === "stale"
        ? "做市 心跳异常"
        : "做市 运行中"
      : makerInstance.status === "stale"
        ? "做市 PID 残留"
        : "做市 未运行"
    : "做市 检查中";

  return (
    <AppShell>
      <ToastViewport />
      {isAdmin ? (
        <div className="panel hl-admin-bar mb-2 flex flex-wrap items-center gap-2 rounded-xl px-3 py-2 text-xs">
          <span className={`rounded-full px-2 py-0.5 ${makerRunning ? "bg-emerald-400/12 text-emerald-100" : "bg-amber-400/12 text-amber-100"}`}>{makerLabel}</span>
          <span className={`rounded-full px-2 py-0.5 ${ticker?.is_active === false ? "bg-rose-400/12 text-rose-100" : "bg-emerald-400/12 text-emerald-100"}`}>
            市场 {ticker?.is_active === false ? "暂停" : "正常"}
          </span>
          <span className="rounded-full bg-white/6 px-2 py-0.5 text-slate-300">{currentSymbol} · MM 策略铺单观察</span>
          <div className="ml-auto flex gap-1.5">
            <button type="button" onClick={() => navigate(`/ops/bots/${encodeURIComponent(currentSymbol)}?tab=strategy`)} className="rounded bg-cyan-400/12 px-2 py-1 text-cyan-100 hover:bg-cyan-400/20">
              策略配置
            </button>
            <button type="button" onClick={() => navigate(`/ops/bots/${encodeURIComponent(currentSymbol)}?tab=logs`)} className="rounded bg-white/6 px-2 py-1 text-slate-300 hover:bg-white/10">
              机器人日志
            </button>
          </div>
        </div>
      ) : null}

      <TradeTerminal
        symbol={currentSymbol}
        markets={terminalMarkets}
        currentMarket={market ? toTerminalMarket(market) : undefined}
        isPerp={isContract}
        ticker={ticker}
        orderbook={orderbook}
        orderbookUpdatedAt={orderbookUpdatedAt ?? orderbookUpdatedAtStore}
        orderbookSeq={orderbookSeq}
        recentTrades={recentTrades}
        klines={klines}
        klineMeta={klineMeta}
        klineLoading={klineLoading}
        klineError={loadError}
        balances={balances}
        perpAccount={
          contractAccount
            ? {
                user_id: contractAccount.user_id,
                margin_asset: contractAccount.margin_asset,
                wallet_balance: contractAccount.wallet_balance,
                available_margin: contractAccount.available_margin,
                used_margin: contractAccount.used_margin,
                unrealized_pnl: contractAccount.unrealized_pnl,
                realized_pnl: contractAccount.realized_pnl,
                total_fees: contractAccount.total_fees,
                updated_at: contractAccount.updated_at,
              }
            : undefined
        }
        positions={terminalPositions}
        openOrders={openOrders.filter((item) => item.symbol === currentSymbol)}
        historyOrders={orderHistory}
        userFills={accountTrades.filter((item) => item.symbol === currentSymbol)}
        fundingState={isContract ? contractPriceState : undefined}
        priceDigits={priceDigits}
        quantityDigits={quantityDigits}
        tickSize={tickSize}
        marketStatus={statusText}
        publicStatus={publicStreamStatus}
        privateStatus={privateStreamStatus}
        tickerUpdatedAt={tickerUpdatedAt}
        klinesUpdatedAt={klinesUpdatedAt}
        selectedInterval={selectedInterval}
        orderbookDepth={orderbookDepth}
        mergeTicks={1}
        intervalOptions={["1m", "5m", "15m", "1h", "4h", "1d"]}
        onSelectSymbol={(next) => navigate(`/trade/${next}`)}
        onIntervalChange={setSelectedInterval}
        onDepthChange={setOrderbookDepth}
        onMergeChange={() => undefined}
        onRefreshKlines={() => void refreshAll()}
        onPlaceOrder={placeOrder}
        onCancelOrder={cancelOrder}
        onAmendOrder={amendOrder}
        onClosePosition={closePosition}
        submitting={submitting}
        submitError={submitError}
        submitNotice={submitNotice}
        closingIds={cancelingIds}
        orderPanelState={orderPanelState}
        setOrderPanelState={setOrderPanelState}
        extraStatus={
          <span className="text-slate-500">
            maker {accountFees.find((item) => item.symbol === currentSymbol)?.maker_fee_rate ?? "-"} · taker{" "}
            {accountFees.find((item) => item.symbol === currentSymbol)?.taker_fee_rate ?? "-"}
          </span>
        }
      />
    </AppShell>
  );
}
