export type Level = [string, string];

export interface PersistenceContract {
  persistence_mode: string;
  run_id: string;
  runtime_state_durable: boolean;
  history_sampled: boolean;
  display_only_history?: boolean;
}

export interface SandboxHealth extends PersistenceContract {
  ok: boolean;
  status: string;
  storage_degraded?: boolean;
}

export interface MarketTicker {
  symbol: string;
  product_type?: "SPOT" | "PERP";
  last_price: string;
  best_bid: string;
  best_ask: string;
  spread: string;
  spread_pct: string;
  mid_price: string;
  depth_amount_0_5pct: string;
  depth_amount_2pct: string;
  open_24h: string;
  high_24h?: string;
  low_24h?: string;
  change_24h: string;
  change_24h_pct: string;
  volume_24h: string;
  quote_volume_24h: string;
  is_active: boolean;
}

export interface PaperMarket {
  id: number;
  symbol: string;
  product_type: "SPOT" | "PERP";
  market_type: string;
  base_asset: string;
  quote_asset: string;
  margin_asset?: string | null;
  status?: string;
  paper_status?: string;
  price_source?: string;
  price_source_symbol?: string | null;
  reference_price?: string | null;
  price_tick: string;
  qty_step: string;
  min_qty: string;
  min_notional: string;
  max_leverage: string;
  default_leverage: string;
  maintenance_margin_rate: string;
  funding_rate: string;
  funding_interval_hours: number;
  price_protection_pct?: string;
  maker_fee_rate?: string;
  taker_fee_rate?: string;
  price_precision: number;
  qty_precision: number;
  is_active: boolean;
}

export interface PaperPerpAccount {
  user_id: number;
  margin_asset: string;
  wallet_balance: string;
  available_margin: string;
  used_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  total_fees: string;
  updated_at: number;
}

export interface PaperPosition {
  symbol: string;
  product_type: "PERP" | string;
  side: "long" | "short" | "flat" | string;
  quantity: string;
  entry_price: string;
  mark_price: string;
  liquidation_price: string;
  leverage: string;
  margin_mode: string;
  isolated_margin: string;
  maintenance_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  risk_status?: string;
  liquidation_distance_pct?: string;
  margin_buffer?: string;
  updated_at: number;
}

export interface PaperAccountUser {
  user_id: number;
  username: string;
  role: string;
  is_active: boolean;
  account_epoch: number;
  account_run_id?: string | null;
  created_at?: number;
}

export interface PaperAccountResponse {
  user: PaperAccountUser;
  spot: { balances: BalanceItem[] };
  perp: { account: PaperPerpAccount };
}

export interface PaperPerpAccountResponse {
  account: PaperPerpAccount;
  positions: PaperPosition[];
}

export interface PaperFundingItem {
  event_id: string;
  market_id: number;
  amount: string;
  rate: string;
  created_at: number;
}

export interface PaperLiquidationItem {
  event_id: string;
  market_id: number;
  quantity: string;
  realized_pnl: string;
  reason: string;
  created_at: number;
}

export interface MarketDefinition {
  symbol: string;
  product_type: "SPOT" | "PERP";
  market_type: "mainstream" | "listed";
  base_asset: string;
  quote_asset: string;
  margin_asset?: string | null;
  price_tick: string;
  qty_step: string;
  min_qty: string;
  min_notional: string;
  max_leverage?: string;
  default_leverage?: string;
  maintenance_margin_rate?: string;
  funding_rate?: string;
  funding_interval_hours?: number;
  index_price_source?: string;
  mark_price_mode?: string;
  funding_rate_mode?: "binance" | "formula" | string;
  funding_interest_rate?: string;
  funding_clamp_rate?: string;
  funding_cap_rate?: string;
  funding_impact_notional?: string;
  contract_trading_mode?: "normal" | "reduce_only" | "paused" | string;
  price_state?: ContractPriceState | null;
  reference_price?: string | null;
  price_precision: number;
  qty_precision: number;
  is_active: boolean;
}

export interface OrderItem {
  order_id: string;
  client_order_id?: string | null;
  symbol: string;
  product_type?: "SPOT" | "PERP" | string;
  side: string;
  position_action?: "open" | "close" | string | null;
  reduce_only?: boolean;
  leverage?: string | null;
  type: string;
  tif: string;
  status: string;
  price?: string | null;
  quantity: string;
  filled_quantity: string;
  remaining_quantity: string;
  avg_price?: string | null;
  notional?: string | null;
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
  product_type: "SPOT" | "PERP";
  market_type: "mainstream" | "listed";
  base_asset: string;
  quote_asset: string;
  margin_asset?: string | null;
  price_tick: string;
  qty_step: string;
  min_qty: string;
  min_notional: string;
  max_leverage: string;
  default_leverage: string;
  maintenance_margin_rate: string;
  funding_rate: string;
  funding_interval_hours: number;
  index_price_source: string;
  mark_price_mode: string;
  funding_rate_mode: "binance" | "formula" | string;
  funding_interest_rate: string;
  funding_clamp_rate: string;
  funding_cap_rate: string;
  funding_impact_notional: string;
  contract_trading_mode: "normal" | "reduce_only" | "paused" | string;
  reference_price?: string | null;
  price_precision: number;
  qty_precision: number;
  is_active: boolean;
  default_maker_fee_rate: string;
  default_taker_fee_rate: string;
}

export interface FeeProfileItem {
  symbol: string;
  product_type?: "SPOT" | "PERP" | string;
  market_type: "mainstream" | "listed";
  maker_fee_rate: string;
  taker_fee_rate: string;
  source?: "user" | "market_default" | string;
}

export interface MarketHealthCheck {
  code: string;
  label: string;
  severity: "ok" | "warn" | "critical" | string;
  detail: string;
}

export interface MarketHealth {
  symbol: string;
  status: "ok" | "warn" | "critical" | string;
  market_type: "mainstream" | "listed";
  is_active: boolean;
  ts: number;
  metrics: {
    best_bid: string;
    best_ask: string;
    mid_price: string;
    spread: string;
    spread_pct: string;
    depth_amount_0_5pct: string;
    depth_amount_2pct: string;
    book_imbalance: string;
    bid_level_count: number;
    ask_level_count: number;
    quote_volume_24h: string;
  };
  checks: MarketHealthCheck[];
}

export interface TradeItem {
  trade_id: string;
  symbol: string;
  product_type?: "SPOT" | "PERP" | string;
  side?: string;
  taker_side?: string;
  position_action?: "open" | "close" | string | null;
  taker_position_action?: "open" | "close" | string | null;
  maker_position_action?: "open" | "close" | string | null;
  realized_pnl?: string;
  taker_realized_pnl?: string;
  maker_realized_pnl?: string;
  price: string;
  quantity: string;
  quote_amount?: string;
  fee?: string;
  fee_asset?: string;
  taker_fee?: string;
  maker_fee?: string;
  taker_fee_asset?: string;
  maker_fee_asset?: string;
  liquidity_role?: string;
  source?: "user" | "bot" | "bootstrap_seed" | "mixed" | "unknown" | string;
  executed_at?: number;
  ts?: number;
}

export interface ContractAccount {
  user_id: number;
  margin_asset: string;
  wallet_balance: string;
  available_margin: string;
  used_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  total_fees: string;
  updated_at: number;
}

export interface ContractPosition {
  symbol: string;
  product_type: "PERP" | string;
  side: "long" | "short" | "flat" | string;
  quantity: string;
  entry_price: string;
  mark_price: string;
  liquidation_price: string;
  leverage: string;
  margin_mode: string;
  isolated_margin: string;
  maintenance_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  risk_status?: "flat" | "ok" | "watch" | "warning" | "liquidation_due" | string;
  liquidation_distance_pct?: string;
  margin_buffer?: string;
  risk_tier?: number;
  risk_notional_floor?: string;
  risk_notional_cap?: string | null;
  risk_max_leverage?: string;
  maintenance_margin_rate?: string;
  maintenance_amount?: string;
  updated_at: number;
}

export interface ContractSetting {
  symbol: string;
  leverage: string;
  margin_mode: string;
  position_mode?: string;
  max_leverage: string;
  default_leverage: string;
}

export interface ContractPriceState {
  symbol: string;
  product_type: "PERP" | string;
  index_symbol: string;
  index_source: string;
  index_price: string;
  external_mark_price: string;
  mark_price: string;
  mark_source: string;
  local_mid_price: string;
  local_last_price: string;
  impact_bid_price: string;
  impact_ask_price: string;
  premium_index: string;
  funding_rate: string;
  funding_rate_mode: "binance" | "formula" | string;
  interest_rate: string;
  funding_clamp_rate: string;
  funding_cap_rate: string;
  impact_notional: string;
  next_funding_time?: number | null;
  source_status: string;
  source_message?: string | null;
  external_updated_at?: number | null;
  updated_at: number;
}

export interface ContractRiskAlert {
  symbol: string;
  user_id: number;
  username: string;
  side: string;
  quantity: string;
  mark_price: string;
  liquidation_price: string;
  liquidation_distance_pct: string;
  margin_buffer: string;
  risk_status: string;
}

export interface ContractMaintenanceStatus {
  metrics?: {
    updated_at?: number;
    markets?: number;
    price_refreshes?: number;
    auto_settle_enabled?: boolean;
    auto_liquidation_enabled?: boolean;
    auto_adl_enabled?: boolean;
    fetch_external?: boolean;
    funding_settlements?: { symbol: string; funding_time: number; funding_rate: string; settled_count: number }[];
    funding_jobs?: ContractFundingJob[];
    funding_job_failures?: number;
    liquidations?: {
      symbol: string;
      event_id: string;
      user_id: number;
      position_side: string;
      quantity: string;
      mark_price: string;
      realized_pnl: string;
      liquidated_at: number;
    }[];
    liquidation_count?: number;
    adl_events?: {
      symbol: string;
      liquidation_event_id: string;
      event_count: number;
      covered_amount: string;
      residual_after: string;
      status: string;
    }[];
    adl_event_count?: number;
    risk_alert_count?: number;
    errors?: { symbol: string; error: string }[];
  };
  risk_alerts?: Record<string, ContractRiskAlert[]>;
  liquidity?: {
    updated_at?: number;
    markets?: number;
    reseeded_markets?: number;
    skipped_markets?: number;
    placed_orders?: number;
    canceled_orders?: number;
    rejected_orders?: number;
    errors?: { symbol: string; error: string }[];
    items?: {
      symbol: string;
      status?: string;
      mid_price: string | null;
      placed_orders: number;
      canceled_orders: number;
      rejected_orders: number;
    }[];
  };
}

export interface ContractFundingEvent {
  event_id: string;
  symbol: string;
  user_id: number;
  position_side: string;
  quantity: string;
  index_price: string;
  mark_price: string;
  funding_rate: string;
  amount: string;
  funding_rate_mode: string;
  funding_time: number;
  created_at: number;
}

export interface ContractLedgerItem {
  entry_id: string;
  user_id: number;
  symbol?: string | null;
  margin_asset: string;
  change_type: string;
  amount: string;
  wallet_before: string;
  wallet_after: string;
  available_before: string;
  available_after: string;
  used_margin_before: string;
  used_margin_after: string;
  unrealized_pnl_before: string;
  unrealized_pnl_after: string;
  realized_pnl_before: string;
  realized_pnl_after: string;
  total_fees_before: string;
  total_fees_after: string;
  related_order_id?: string | null;
  related_trade_id?: string | null;
  related_event_id?: string | null;
  note?: string | null;
  created_at: number;
}

export interface ContractFundingSettlement {
  settlement_id: string;
  symbol: string;
  funding_time: number;
  funding_rate: string;
  funding_rate_mode: string;
  index_price: string;
  mark_price: string;
  settled_count: number;
  total_amount: string;
  status: string;
  error_message?: string | null;
  started_at: number;
  completed_at?: number | null;
  created_at: number;
  updated_at: number;
}

export interface ContractFundingJob {
  job_id: string;
  symbol: string;
  funding_time: number;
  status: string;
  attempt_count: number;
  max_attempts: number;
  locked_by?: string | null;
  locked_at?: number | null;
  heartbeat_at?: number | null;
  next_retry_at?: number | null;
  settlement_id?: string | null;
  last_error?: string | null;
  started_at?: number | null;
  completed_at?: number | null;
  created_at: number;
  updated_at: number;
  action?: string | null;
}

export interface ContractFundingJobRetryResult {
  job: ContractFundingJob;
  result?: {
    symbol: string;
    funding_time: number;
    funding_rate: string;
    settled_count: number;
    already_settled?: boolean;
    settlement_id?: string | null;
    status?: string;
  } | null;
  error?: string;
}

export interface ContractFundingJobBatchRetryResult {
  requested_count: number;
  succeeded_count: number;
  failed_count: number;
  limit: number;
  symbol?: string | null;
  updated_at: number;
  items: Array<ContractFundingJobRetryResult | { job_id: string; error: string }>;
}

export interface ContractLiquidationEvent {
  event_id: string;
  symbol: string;
  user_id: number;
  position_side: string;
  quantity: string;
  entry_price: string;
  mark_price: string;
  liquidation_price: string;
  bankruptcy_price: string;
  realized_pnl: string;
  released_margin: string;
  insurance_covered: string;
  residual_bad_debt: string;
  adl_covered: string;
  adl_residual: string;
  adl_status: string;
  maintenance_margin: string;
  margin_buffer: string;
  risk_status: string;
  reason: string;
  liquidated_at: number;
  created_at: number;
}

export interface ContractInsuranceFund {
  margin_asset: string;
  balance: string;
  updated_at: number;
}

export interface ContractInsuranceEvent {
  event_id: string;
  margin_asset: string;
  symbol?: string | null;
  event_type: string;
  amount: string;
  balance_before: string;
  balance_after: string;
  residual_bad_debt: string;
  user_id?: number | null;
  market_id?: number | null;
  related_liquidation_event_id?: string | null;
  note?: string | null;
  created_at: number;
}

export interface ContractAdlEvent {
  event_id: string;
  liquidation_event_id: string;
  user_id: number;
  username?: string | null;
  market_id: number;
  symbol?: string | null;
  position_side: string;
  quantity: string;
  entry_price: string;
  mark_price: string;
  execution_price: string;
  realized_pnl: string;
  released_margin: string;
  bad_debt_before: string;
  covered_amount: string;
  residual_after: string;
  pnl_pct: string;
  effective_leverage: string;
  rank_score: string;
  status: string;
  reason: string;
  created_at: number;
}

export interface ContractRiskTier {
  symbol: string;
  tier: number;
  notional_floor: string;
  notional_cap?: string | null;
  max_leverage: string;
  maintenance_margin_rate: string;
  maintenance_amount: string;
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
  source?: "user" | "bot" | "bootstrap_seed" | "mixed" | "unknown" | string;
  source_counts?: Record<string, number>;
  source_volumes?: Record<string, string>;
  source_quote_volumes?: Record<string, string>;
  is_closed?: boolean;
}

export interface KlineMeta {
  symbol: string;
  interval: string;
  limit: number;
  include_seed: boolean;
  count: number;
  data_status: "ok" | "seed_hidden" | "empty" | "low_quality" | "waiting_for_instance" | string;
  message: string;
  source_counts: Record<string, number>;
  source_volumes?: Record<string, string>;
  source_quote_volumes?: Record<string, string>;
  hidden_seed_count: number;
	  maker_instance?: {
	    status?: string | null;
	    running?: boolean;
	    heartbeat_status?: string | null;
	    strategy_version?: string | null;
	    configured_strategy_version?: string | null;
	    runtime_strategy_mismatch?: boolean;
	  } | null;
  quality?: {
    status: "ok" | "empty" | "low_quality" | string;
    sample_count: number;
    flat_ohlc_count: number;
    zero_body_count: number;
    unique_close_count: number;
    flat_ohlc_ratio: number;
    zero_body_ratio: number;
    price_range: string;
    reason: string;
  };
}

export interface KlineResponse {
  items: KlineItem[];
  meta?: KlineMeta;
}

export interface MakerInstancePublicStatus {
  symbol: string;
  status: "starting" | "running" | "stale" | "stopping" | "stopped" | "error" | string;
  persisted_status?: string;
  heartbeat_status?: "ok" | "stale" | "missing" | string;
	  running: boolean;
	  strategy_version?: string | null;
	  configured_strategy_version?: string | null;
	  runtime_strategy_mismatch?: boolean;
  metrics_present: boolean;
  last_metrics_ts?: number | null;
  metrics_age_ms?: number | null;
  data_plane_status?: "fresh" | "stale" | "missing" | "empty" | string;
  engine_open_order_count?: number | null;
  last_heartbeat_at?: number | null;
  started_at?: number | null;
  stopped_at?: number | null;
  last_error?: string | null;
  runner_parent_watch?: {
    enabled: boolean;
    status?: "watching" | "parent_lost" | string | null;
    expected_pid?: number | null;
    actual_parent_pid?: number | null;
    detected_at?: string | null;
  };
  reference_price?: {
    venue: "Binance" | string;
    market: "spot" | "perpetual" | string;
    bid?: string | null;
    ask?: string | null;
    mid: string;
    source: string;
    age_ms: number;
    updated_at: number;
    stale_after_ms: number;
    stale: boolean;
  } | null;
}
