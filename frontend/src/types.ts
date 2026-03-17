export type Level = [string, string];

export interface MarketTicker {
  symbol: string;
  last_price: string;
  best_bid: string;
  best_ask: string;
  spread: string;
  spread_pct: string;
  mid_price: string;
  depth_amount_0_5pct: string;
  depth_amount_2pct: string;
  open_24h: string;
  change_24h: string;
  change_24h_pct: string;
  volume_24h: string;
  quote_volume_24h: string;
  is_active: boolean;
}

export interface MarketDefinition {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  price_tick: string;
  qty_step: string;
  min_qty: string;
  min_notional: string;
  price_precision: number;
  qty_precision: number;
  is_active: boolean;
}

export interface OrderItem {
  order_id: string;
  client_order_id?: string | null;
  symbol: string;
  side: string;
  type: string;
  tif: string;
  status: string;
  price?: string | null;
  quantity: string;
  filled_quantity: string;
  remaining_quantity: string;
  avg_price?: string | null;
  reference_price?: string | null;
  protection_bps?: number | null;
  max_price?: string | null;
  min_price?: string | null;
  reject_reason?: string | null;
  created_at: number;
  updated_at: number;
}

export interface AdminMarketItem {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  price_tick: string;
  qty_step: string;
  min_qty: string;
  min_notional: string;
  price_precision: number;
  qty_precision: number;
  is_active: boolean;
  default_maker_fee_rate: string;
  default_taker_fee_rate: string;
}

export interface TradeItem {
  trade_id: string;
  symbol: string;
  side?: string;
  taker_side?: string;
  price: string;
  quantity: string;
  quote_amount?: string;
  fee?: string;
  fee_asset?: string;
  liquidity_role?: string;
  executed_at?: number;
  ts?: number;
}

export interface BalanceItem {
  asset: string;
  available: string;
  frozen: string;
}

export interface LedgerItem {
  entry_id: string;
  asset: string;
  change_type: string;
  amount: string;
  available_before: string;
  available_after: string;
  frozen_before: string;
  frozen_after: string;
  related_order_id?: string | null;
  related_trade_id?: string | null;
  note?: string | null;
  created_at: number;
}

export interface KlineItem {
  open_time: number;
  close_time: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quote_volume: string;
  trade_count: number;
  is_closed?: boolean;
}
