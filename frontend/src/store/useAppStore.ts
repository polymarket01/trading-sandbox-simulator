import { create } from "zustand";
import type { BalanceItem, KlineItem, LedgerItem, Level, MarketTicker, OrderItem, TradeItem } from "../types";

type Toast = { id: number; kind: "success" | "error"; text: string };

type Store = {
  markets: string[];
  ticker?: MarketTicker;
  orderbook: { bids: Level[]; asks: Level[] };
  recentTrades: TradeItem[];
  klines: KlineItem[];
  balances: BalanceItem[];
  openOrders: OrderItem[];
  orderHistory: OrderItem[];
  accountTrades: TradeItem[];
  ledger: LedgerItem[];
  selectedInterval: string;
  orderbookDepth: number;
  toasts: Toast[];
  setMarkets: (items: string[]) => void;
  setTicker: (ticker: MarketTicker) => void;
  patchTicker: (patch: Partial<MarketTicker>) => void;
  setOrderbook: (book: { bids: Level[]; asks: Level[] }) => void;
  applyOrderbookDelta: (delta: { bids?: Level[]; asks?: Level[] }) => void;
  setRecentTrades: (items: TradeItem[]) => void;
  prependRecentTrades: (items: TradeItem[]) => void;
  setKlines: (items: KlineItem[]) => void;
  upsertKline: (item: KlineItem) => void;
  setBalances: (items: BalanceItem[]) => void;
  setOpenOrders: (items: OrderItem[]) => void;
  upsertOrder: (item: OrderItem) => void;
  setOrderHistory: (items: OrderItem[]) => void;
  setAccountTrades: (items: TradeItem[]) => void;
  setLedger: (items: LedgerItem[]) => void;
  setSelectedInterval: (value: string) => void;
  setOrderbookDepth: (value: number) => void;
  pushToast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
};

const applyLevels = (source: Level[], delta: Level[], desc: boolean) => {
  const map = new Map(source.map((item) => [item[0], item[1]]));
  delta.forEach(([price, quantity]) => {
    if (quantity === "0") map.delete(price);
    else map.set(price, quantity);
  });
  return Array.from(map.entries()).sort((a, b) => (desc ? Number(b[0]) - Number(a[0]) : Number(a[0]) - Number(b[0]))) as Level[];
};

const tradeKey = (item: TradeItem) => `${item.trade_id}-${item.ts ?? item.executed_at ?? 0}`;

const mergeRecentTrades = (current: TradeItem[], incoming: TradeItem[]) => {
  const map = new Map<string, TradeItem>();
  [...incoming, ...current].forEach((item) => {
    map.set(tradeKey(item), item);
  });
  return Array.from(map.values())
    .sort((a, b) => Number(b.ts ?? b.executed_at ?? 0) - Number(a.ts ?? a.executed_at ?? 0))
    .slice(0, 100);
};

let toastId = 0;

export const useAppStore = create<Store>((set) => ({
  markets: [],
  orderbook: { bids: [], asks: [] },
  recentTrades: [],
  klines: [],
  balances: [],
  openOrders: [],
  orderHistory: [],
  accountTrades: [],
  ledger: [],
  selectedInterval: "1m",
  orderbookDepth: 20,
  toasts: [],
  setMarkets: (items) => set({ markets: items }),
  setTicker: (ticker) => set({ ticker }),
  patchTicker: (patch) =>
    set((state) => ({
      ticker: state.ticker ? { ...state.ticker, ...patch } : ({ ...patch } as MarketTicker),
    })),
  setOrderbook: (orderbook) => set({ orderbook }),
  applyOrderbookDelta: (delta) =>
    set((state) => ({
      orderbook: {
        bids: delta.bids ? applyLevels(state.orderbook.bids, delta.bids, true).slice(0, state.orderbookDepth) : state.orderbook.bids,
        asks: delta.asks ? applyLevels(state.orderbook.asks, delta.asks, false).slice(0, state.orderbookDepth) : state.orderbook.asks,
      },
    })),
  setRecentTrades: (items) => set({ recentTrades: mergeRecentTrades([], items) }),
  prependRecentTrades: (items) =>
    set((state) => ({ recentTrades: mergeRecentTrades(state.recentTrades, items) })),
  setKlines: (items) => set({ klines: items }),
  upsertKline: (item) =>
    set((state) => {
      const next = [...state.klines];
      const index = next.findIndex((entry) => entry.open_time === item.open_time);
      if (index >= 0) next[index] = item;
      else next.push(item);
      next.sort((a, b) => a.open_time - b.open_time);
      return { klines: next.slice(-500) };
    }),
  setBalances: (items) => set({ balances: items }),
  setOpenOrders: (items) => set({ openOrders: items }),
  upsertOrder: (item) =>
    set((state) => {
      const history = [item, ...state.orderHistory.filter((entry) => entry.order_id !== item.order_id)].slice(0, 100);
      const open = item.status === "new" || item.status === "partially_filled"
        ? [item, ...state.openOrders.filter((entry) => entry.order_id !== item.order_id)]
        : state.openOrders.filter((entry) => entry.order_id !== item.order_id);
      return { openOrders: open, orderHistory: history };
    }),
  setOrderHistory: (items) => set({ orderHistory: items }),
  setAccountTrades: (items) => set({ accountTrades: items }),
  setLedger: (items) => set({ ledger: items }),
  setSelectedInterval: (value) => set({ selectedInterval: value }),
  setOrderbookDepth: (value) => set({ orderbookDepth: value }),
  pushToast: (kind, text) =>
    set((state) => ({ toasts: [...state.toasts, { id: ++toastId, kind, text }] })),
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((item) => item.id !== id) })),
}));
