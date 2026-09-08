import { isActiveContractPosition } from "../lib/paper";
import { create } from "zustand";
import { clearSession, readSession, writeSession, type AuthSession } from "../lib/session";
import type {
  BalanceItem,
  KlineItem,
  LedgerItem,
  Level,
  MarketTicker,
  OrderItem,
  PaperPerpAccount,
  PaperPosition,
  TradeItem,
} from "../types";

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "fallback" | "offline";

type Toast = { id: number; kind: "success" | "error" | "info"; text: string };

type Store = {
  markets: string[];
  tickerSymbol?: string;
  ticker?: MarketTicker;
  orderbookSymbol?: string;
  orderbook: { bids: Level[]; asks: Level[] };
  orderbookUpdatedAt?: number;
  orderbookSeq?: number;
  orderbookStreamId?: string;
  recentTradesSymbol?: string;
  recentTrades: TradeItem[];
  klinesSymbol?: string;
  klines: KlineItem[];
  balances: BalanceItem[];
  openOrders: OrderItem[];
  orderHistory: OrderItem[];
  accountTrades: TradeItem[];
  ledger: LedgerItem[];
  authSession?: AuthSession;
  selectedInterval: string;
  orderbookDepth: number;
  toasts: Toast[];
  publicStreamStatus: StreamStatus;
  publicStreamLastMessageAt?: number;
  privateStreamStatus: StreamStatus;
  privateStreamLastMessageAt?: number;
  tickerUpdatedAt?: number;
  klinesUpdatedAt?: number;
  paperPerpAccount?: PaperPerpAccount;
  paperPositions: PaperPosition[];
  paperAccountMeta?: {
    user_id: number;
    username: string;
    role: string;
    is_active: boolean;
    account_epoch: number;
    account_run_id?: string | null;
    created_at?: number;
  };
  setMarkets: (items: string[]) => void;
  setTicker: (symbol: string, ticker?: MarketTicker) => void;
  patchTicker: (symbol: string, patch: Partial<MarketTicker>) => void;
  setOrderbook: (symbol: string, book: { bids: Level[]; asks: Level[] }, updatedAt?: number, seq?: number, streamId?: string) => void;
  applyOrderbookDelta: (symbol: string, delta: { bids?: Level[]; asks?: Level[] }, updatedAt?: number, seq?: number, streamId?: string) => void;
  setRecentTrades: (symbol: string, items: TradeItem[]) => void;
  prependRecentTrades: (symbol: string, items: TradeItem[]) => void;
  setKlines: (symbol: string, items: KlineItem[]) => void;
  upsertKline: (symbol: string, item: KlineItem) => void;
  setBalances: (items: BalanceItem[]) => void;
  setOpenOrders: (items: OrderItem[]) => void;
  upsertOrder: (item: OrderItem) => void;
  setOrderHistory: (items: OrderItem[]) => void;
  setAccountTrades: (items: TradeItem[]) => void;
  prependAccountTrades: (items: TradeItem[]) => void;
  setLedger: (items: LedgerItem[]) => void;
  prependLedgerEntries: (items: LedgerItem[]) => void;
  setAuthSession: (session?: AuthSession) => void;
  setSelectedInterval: (value: string) => void;
  setOrderbookDepth: (value: number) => void;
  pushToast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
  setPublicStreamStatus: (status: StreamStatus, lastMessageAt?: number) => void;
  setPrivateStreamStatus: (status: StreamStatus, lastMessageAt?: number) => void;
  setTickerUpdatedAt: (ts?: number) => void;
  setKlinesUpdatedAt: (ts?: number) => void;
  setPaperPerpAccount: (account?: PaperPerpAccount) => void;
  setPaperPositions: (items: PaperPosition[]) => void;
  setPaperAccountMeta: (meta?: Store["paperAccountMeta"]) => void;
};

const applyLevels = (source: Level[], delta: Level[], desc: boolean) => {
  const map = new Map(source.map((item) => [item[0], item[1]]));
  delta.forEach(([price, quantity]) => {
    if (quantity === "0") map.delete(price);
    else map.set(price, quantity);
  });
  return Array.from(map.entries()).sort((a, b) => (desc ? Number(b[0]) - Number(a[0]) : Number(a[0]) - Number(b[0]))) as Level[];
};

const levelListsEqual = (left: Level[], right: Level[]) => (
  left.length === right.length
  && left.every((item, index) => item[0] === right[index]?.[0] && item[1] === right[index]?.[1])
);

const orderbooksEqual = (
  left: { bids: Level[]; asks: Level[] },
  right: { bids: Level[]; asks: Level[] },
) => levelListsEqual(left.bids, right.bids) && levelListsEqual(left.asks, right.asks);

const levelListStartsWith = (items: Level[], prefix: Level[]) => (
  items.length >= prefix.length && prefix.every((item, index) => item[0] === items[index]?.[0] && item[1] === items[index]?.[1])
);

const orderbookExtends = (
  current: { bids: Level[]; asks: Level[] },
  incoming: { bids: Level[]; asks: Level[] },
) => levelListStartsWith(incoming.bids, current.bids) && levelListStartsWith(incoming.asks, current.asks);

const finiteNumber = (value: number | undefined) => (
  typeof value === "number" && Number.isFinite(value) ? value : undefined
);

const normalizeSymbol = (symbol: string) => symbol.toUpperCase();

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

const mergeLedgerEntries = (current: LedgerItem[], incoming: LedgerItem[]) => {
  const map = new Map<string, LedgerItem>();
  [...incoming, ...current].forEach((item) => {
    map.set(item.entry_id, item);
  });
  return Array.from(map.values())
    .sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0))
    .slice(0, 100);
};

let toastId = 0;

export const useAppStore = create<Store>((set) => ({
  markets: [],
  tickerSymbol: undefined,
  ticker: undefined,
  orderbookSymbol: undefined,
  orderbook: { bids: [], asks: [] },
  orderbookUpdatedAt: undefined,
  orderbookSeq: undefined,
  orderbookStreamId: undefined,
  recentTradesSymbol: undefined,
  recentTrades: [],
  klinesSymbol: undefined,
  klines: [],
  balances: [],
  openOrders: [],
  orderHistory: [],
  accountTrades: [],
  ledger: [],
  authSession: readSession(),
  selectedInterval: "1m",
  orderbookDepth: 30,
  toasts: [],
  publicStreamStatus: "idle",
  privateStreamStatus: "idle",
  paperPositions: [],
  setMarkets: (items) => set({ markets: items }),
  setTicker: (symbol, ticker) => set({ tickerSymbol: normalizeSymbol(symbol), ticker }),
  patchTicker: (symbol, patch) =>
    set((state) => {
      const normalizedSymbol = normalizeSymbol(symbol);
      const current = state.tickerSymbol === normalizedSymbol ? state.ticker : undefined;
      return {
        tickerSymbol: normalizedSymbol,
        ticker: current
          ? { ...current, ...patch, symbol: normalizedSymbol }
          : ({ ...patch, symbol: normalizedSymbol } as MarketTicker),
      };
    }),
  setOrderbook: (symbol, orderbook, updatedAt, seq, streamId) =>
    set((state) => {
      const normalizedSymbol = normalizeSymbol(symbol);
      const sameSymbol = state.orderbookSymbol === normalizedSymbol;
      const currentBook = sameSymbol ? state.orderbook : { bids: [], asks: [] };
      const currentSeq = sameSymbol ? state.orderbookSeq : undefined;
      const currentStreamId = sameSymbol ? state.orderbookStreamId : undefined;
      const currentUpdatedAt = sameSymbol ? state.orderbookUpdatedAt : undefined;
      const nextSeq = finiteNumber(seq);
      const nextUpdatedAt = finiteNumber(updatedAt);
      const nextStreamId = typeof streamId === "string" && streamId.length > 0 ? streamId : undefined;
      const streamChanged = sameSymbol && nextStreamId !== undefined && nextStreamId !== currentStreamId;
      const sameContent = orderbooksEqual(currentBook, orderbook);
      if (sameContent) {
        if (!sameSymbol || streamChanged) {
          return {
            orderbookSymbol: normalizedSymbol,
            orderbook,
            orderbookUpdatedAt: nextUpdatedAt ?? Date.now(),
            orderbookSeq: nextSeq,
            orderbookStreamId: nextStreamId,
          };
        }
        const nextFreshAt = nextUpdatedAt ?? currentUpdatedAt;
        const nextHeartbeatSeq = nextSeq ?? currentSeq;
        if (nextFreshAt !== undefined && nextFreshAt !== currentUpdatedAt) {
          return { orderbookUpdatedAt: nextFreshAt, orderbookSeq: nextHeartbeatSeq };
        }
        return state;
      }
      if (!streamChanged && nextSeq !== undefined && currentSeq !== undefined && nextSeq <= currentSeq) {
        if (nextSeq === currentSeq && orderbookExtends(currentBook, orderbook)) {
          return { orderbookSymbol: normalizedSymbol, orderbook, orderbookUpdatedAt: currentUpdatedAt, orderbookSeq: currentSeq };
        }
        return state;
      }
      return {
        orderbookSymbol: normalizedSymbol,
        orderbook,
        orderbookUpdatedAt: nextUpdatedAt ?? Date.now(),
        orderbookSeq: nextSeq,
        orderbookStreamId: nextStreamId ?? currentStreamId,
      };
    }),
  applyOrderbookDelta: (symbol, delta, updatedAt, seq, streamId) =>
    set((state) => {
      const normalizedSymbol = normalizeSymbol(symbol);
      if (state.orderbookSymbol !== normalizedSymbol) {
        return state;
      }
      const orderbook = {
        bids: delta.bids ? applyLevels(state.orderbook.bids, delta.bids, true).slice(0, 100) : state.orderbook.bids,
        asks: delta.asks ? applyLevels(state.orderbook.asks, delta.asks, false).slice(0, 100) : state.orderbook.asks,
      };
      const nextSeq = finiteNumber(seq);
      const nextStreamId = typeof streamId === "string" && streamId.length > 0 ? streamId : undefined;
      const streamChanged = nextStreamId !== undefined && nextStreamId !== state.orderbookStreamId;
      // A delta cannot establish a new backend epoch because it has no complete
      // baseline.  Wait for the first full snapshot from that stream instead.
      if (nextSeq === undefined || nextStreamId === undefined || streamChanged) {
        return state;
      }
      if (!streamChanged && nextSeq !== undefined && state.orderbookSeq !== undefined && nextSeq <= state.orderbookSeq) {
        return state;
      }
      if (orderbooksEqual(state.orderbook, orderbook)) {
        return state;
      }
      return {
        orderbookUpdatedAt: finiteNumber(updatedAt) ?? Date.now(),
        orderbookSeq: nextSeq ?? state.orderbookSeq,
        orderbookStreamId: nextStreamId ?? state.orderbookStreamId,
        orderbook,
      };
    }),
  setRecentTrades: (symbol, items) => set({ recentTradesSymbol: normalizeSymbol(symbol), recentTrades: mergeRecentTrades([], items) }),
  prependRecentTrades: (symbol, items) =>
    set((state) => {
      const normalizedSymbol = normalizeSymbol(symbol);
      const current = state.recentTradesSymbol === normalizedSymbol ? state.recentTrades : [];
      return { recentTradesSymbol: normalizedSymbol, recentTrades: mergeRecentTrades(current, items) };
    }),
  setKlines: (symbol, items) => set({ klinesSymbol: normalizeSymbol(symbol), klines: items }),
  upsertKline: (symbol, item) =>
    set((state) => {
      const normalizedSymbol = normalizeSymbol(symbol);
      const next = state.klinesSymbol === normalizedSymbol ? [...state.klines] : [];
      const index = next.findIndex((entry) => entry.open_time === item.open_time);
      if (index >= 0) next[index] = item;
      else next.push(item);
      next.sort((a, b) => a.open_time - b.open_time);
      return { klinesSymbol: normalizedSymbol, klines: next.slice(-500) };
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
  setAccountTrades: (items) => set({ accountTrades: mergeRecentTrades([], items) }),
  prependAccountTrades: (items) =>
    set((state) => ({ accountTrades: mergeRecentTrades(state.accountTrades, items) })),
  setLedger: (items) => set({ ledger: mergeLedgerEntries([], items) }),
  prependLedgerEntries: (items) =>
    set((state) => ({ ledger: mergeLedgerEntries(state.ledger, items) })),
  setAuthSession: (session) => {
    if (session) writeSession(session);
    else clearSession();
    set({ authSession: session });
  },
  setSelectedInterval: (value) => set({ selectedInterval: value }),
  setOrderbookDepth: (value) => set({ orderbookDepth: value }),
  pushToast: (kind, text) =>
    set((state) => ({ toasts: [...state.toasts, { id: ++toastId, kind, text }] })),
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((item) => item.id !== id) })),
  setPublicStreamStatus: (status, lastMessageAt) =>
    set((state) => ({
      publicStreamStatus: status,
      publicStreamLastMessageAt: typeof lastMessageAt === "number" ? lastMessageAt : state.publicStreamLastMessageAt,
    })),
  setPrivateStreamStatus: (status, lastMessageAt) =>
    set((state) => ({
      privateStreamStatus: status,
      privateStreamLastMessageAt: typeof lastMessageAt === "number" ? lastMessageAt : state.privateStreamLastMessageAt,
    })),
  setTickerUpdatedAt: (ts) => set({ tickerUpdatedAt: ts }),
  setKlinesUpdatedAt: (ts) => set({ klinesUpdatedAt: ts }),
  setPaperPerpAccount: (account) => set({ paperPerpAccount: account }),
  setPaperPositions: (items) => set({ paperPositions: items.filter(isActiveContractPosition) }),
  setPaperAccountMeta: (meta) => set({ paperAccountMeta: meta }),
}));
