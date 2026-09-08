import type {
  BalanceItem,
  ContractPriceState,
  KlineItem,
  KlineMeta,
  Level,
  MarketTicker,
  OrderItem,
  PaperPerpAccount,
  TradeItem,
} from "../../types";
import type { StreamStatus } from "../../store/useAppStore";

export type TerminalMarketInfo = {
  symbol: string;
  product_type: "SPOT" | "PERP";
  base_asset: string;
  quote_asset: string;
  reference_price?: string | null;
  last_price?: string | null;
  change_24h_pct?: number | null;
  paper_status?: string | null;
  is_active?: boolean;
  max_leverage?: string | null;
  maker_fee_rate?: string | null;
  taker_fee_rate?: string | null;
};

export type TerminalPosition = {
  symbol: string;
  side: string;
  quantity: string;
  entry_price: string;
  mark_price: string;
  liquidation_price: string;
  leverage: string;
  isolated_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  risk_status?: string;
  liquidation_distance_pct?: string;
  margin_buffer?: string;
  updated_at?: number;
};

export type TerminalOrderPanelState = {
  side: "buy" | "sell";
  orderType: "limit" | "market";
  tif: "gtc" | "ioc" | "post_only";
  quantity: string;
  price: string;
  positionAction: "open" | "close";
  leverage: string;
};

export type TerminalProps = {
  symbol: string;
  markets: TerminalMarketInfo[];
  currentMarket?: TerminalMarketInfo;
  isPerp: boolean;
  ticker?: MarketTicker;
  orderbook: { bids: Level[]; asks: Level[] };
  orderbookUpdatedAt?: number;
  orderbookSeq?: number;
  recentTrades: TradeItem[];
  klines: KlineItem[];
  klineMeta?: KlineMeta;
  klineLoading?: boolean;
  klineError?: string;
  balances: BalanceItem[];
  perpAccount?: PaperPerpAccount;
  positions: TerminalPosition[];
  openOrders: OrderItem[];
  historyOrders: OrderItem[];
  userFills: TradeItem[];
  fundingState?: ContractPriceState;
  priceDigits: number;
  quantityDigits: number;
  tickSize: number;
  marketStatus: string;
  publicStatus: StreamStatus;
  privateStatus: StreamStatus;
  tickerUpdatedAt?: number;
  klinesUpdatedAt?: number;
  selectedInterval: string;
  orderbookDepth: number;
  mergeTicks: number;
  intervalOptions: string[];
  onSelectSymbol: (symbol: string) => void;
  onIntervalChange: (interval: string) => void;
  onDepthChange: (depth: number) => void;
  onMergeChange: (ticks: number) => void;
  onRefreshKlines: () => void;
  onPlaceOrder: (state: TerminalOrderPanelState) => Promise<{ status: string; orderId?: string; message?: string }>;
  onCancelOrder: (orderId: string) => Promise<void>;
  onAmendOrder?: (order: OrderItem, price?: string, quantity?: string) => Promise<void>;
  onClosePosition: (position: TerminalPosition) => Promise<void>;
  submitting: boolean;
  submitError?: string;
  submitNotice?: string;
  closingIds?: Set<string>;
  setOrderPanelState: (state: Partial<TerminalOrderPanelState>) => void;
  orderPanelState: TerminalOrderPanelState;
  extraStatus?: React.ReactNode;
  extraHeaderRight?: React.ReactNode;
};

export type { StreamStatus };
