import { appBasePath } from "../lib/config";
import { InstalledMakerSelect } from "../components/InstalledMakerSelect";
import { StrategyCatalogPage } from "./StrategyCatalogPage";
import { LiquidityControlPage } from "./LiquidityControlPage";
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ToastViewport } from "../components/ToastViewport";
import { bjDateTime, bjTime, fmt, stepDigits } from "../lib/format";
import { normalizedStrategyKey, strategyDisplayName, strategyOptionsForProduct } from "../lib/strategy";
import { useAppStore } from "../store/useAppStore";
import type {
  AdminMarketItem,
  ContractAccount,
  ContractAdlEvent,
  ContractFundingEvent,
  ContractFundingJob,
  ContractFundingJobBatchRetryResult,
  ContractFundingJobRetryResult,
  ContractFundingSettlement,
  ContractInsuranceEvent,
  ContractInsuranceFund,
  ContractLedgerItem,
  ContractLiquidationEvent,
  ContractMaintenanceStatus,
  ContractPosition,
  ContractPriceState,
  ContractRiskAlert,
  ContractRiskTier,
  LedgerItem,
  OrderItem,
  TradeItem,
} from "../types";

type AdminUser = {
  id: number;
  username: string;
  role: string;
  api_key: string;
  api_secret: string;
  api_secret_masked?: string;
  api_secret_present?: boolean;
  is_active: boolean;
  has_password?: boolean;
  balances: { asset: string; available: string; frozen: string }[];
  fee_profiles?: { symbol: string; maker_fee_rate: string; taker_fee_rate: string }[];
};

type LiquidityRuntimeConfig = Record<string, unknown>;
type StrategySchemaField = {
  path: string;
  label?: string;
  kind?: "boolean" | "integer" | "number" | "string" | string;
  help?: string;
};
type StrategySchemaSection = {
  key: string;
  label?: string;
  fields?: StrategySchemaField[];
};
type MarketStrategyConfigState = {
  strategy_key: string;
  display_name?: string;
  config?: LiquidityRuntimeConfig;
  effective_config?: LiquidityRuntimeConfig;
  schema?: { sections?: StrategySchemaSection[] };
  is_enabled?: boolean;
};
type MarketStrategyState = {
  symbol: string;
  selected: MarketStrategyConfigState;
  items: MarketStrategyConfigState[];
  templates: {
    strategy_key: string;
    display_name?: string;
    config_schema?: { sections?: StrategySchemaSection[] };
    default_config?: LiquidityRuntimeConfig;
  }[];
};
type StrategyApplyStatus = {
  state: string;
  label: string;
  detail: string;
  running: boolean;
  previous_strategy_key?: string;
  strategy_key?: string;
  instance_status?: string;
};
type UpdateMarketStrategyResponse = {
  ok: boolean;
  selected: MarketStrategyConfigState;
  instance?: MakerInstanceStatus;
  apply_status?: StrategyApplyStatus;
};
type AdminUserActivity = {
  user: Pick<AdminUser, "id" | "username" | "role" | "is_active">;
  summary: {
    open_order_count: number;
    recent_order_count: number;
    recent_trade_count: number;
    recent_ledger_count: number;
  };
  open_orders: OrderItem[];
  recent_orders: OrderItem[];
  recent_trades: TradeItem[];
  recent_ledger: LedgerItem[];
};
type DeploymentChecklist = {
  status: "ok" | "warn" | "critical" | string;
  summary: {
    critical: number;
    warn: number;
    ok: number;
  };
  checks: {
    code: string;
    severity: "ok" | "warn" | "critical" | string;
    label: string;
    detail: string;
    action: string;
  }[];
};
type HistoryRetentionConfigSnapshot = {
  order_keep_per_market?: number;
  trade_keep_per_market?: number;
  kline_keep_per_market_interval?: number;
  ledger_keep_per_user?: number;
  contract_ledger_keep_per_user?: number;
  batch_size?: number;
  customer_records_preserved?: boolean;
};

type HistoryRetentionRunResult = {
  dry_run?: boolean;
  config?: HistoryRetentionConfigSnapshot;
  deleted?: {
    orders?: number;
    trades?: number;
    klines?: number;
    ledger_entries?: number;
    contract_ledger_entries?: number;
  };
  partitions?: Record<string, number | undefined>;
};

type RobotTradeSourceSummary = {
  symbol?: string;
  product_type?: "SPOT" | "PERP" | string;
  source: string;
  trade_count: number;
  quote_amount?: string;
  maker_fee?: string;
  taker_fee?: string;
};

type DataRetentionDomainSummary = {
  orders: {
    total: number;
    live: number;
    retention_eligible: number;
    customer_retention_eligible: number;
    robot_retention_eligible: number;
    system_retention_eligible: number;
    prune_candidate?: number;
  };
  trades: {
    total: number;
    customer_involved: number;
    robot_only: number;
    system_or_unknown: number;
    prune_candidate?: number;
  };
  markets?: Array<{
    symbol: string;
    product_type?: "SPOT" | "PERP" | string;
    orders: {
      retention_eligible: number;
      customer_retention_eligible: number;
      robot_retention_eligible: number;
      system_retention_eligible: number;
      prune_candidate?: number;
    };
    trades: {
      total: number;
      customer_involved: number;
      robot_only: number;
      system_or_unknown: number;
      prune_candidate?: number;
    };
    robot_trade_sources?: RobotTradeSourceSummary[];
    pressure_score: number;
  }>;
  robot_trade_sources?: RobotTradeSourceSummary[];
  policy?: {
    customer_records?: string;
    robot_records?: string;
    system_records?: string;
    source_of_truth_unchanged?: boolean;
  };
};

type DataRetentionMarketSummary = NonNullable<DataRetentionDomainSummary["markets"]>[number];

type SystemStatus = {
  status: "ok" | "warn" | "critical" | string;
  ts?: number;
  uptime_seconds: number;
  process: {
    pid: number;
    python: string;
    platform: string;
    rss_mb: number;
  };
  database: {
    mode: string;
    path?: string | null;
    size_mb?: number | null;
  };
  counts: {
    users: number;
    active_users: number;
    markets: number;
    active_markets: number;
    orders: number;
    open_orders: number;
    trades: number;
    ledger_entries: number;
    balances: number;
    book_markets: number;
  };
  history_retention?: {
    enabled: boolean;
    auto_enabled: boolean;
    sqlite_only: boolean;
    interval_seconds: number;
    config?: HistoryRetentionConfigSnapshot;
    last_result?: HistoryRetentionRunResult | null;
    last_error?: string | null;
  };
  data_retention_domains?: DataRetentionDomainSummary;
  websocket: {
    public_connections: number;
    private_connections: number;
    public_subscription_keys: number;
    private_subscription_keys: number;
    metrics?: {
      broadcast_calls?: number;
      messages_attempted?: number;
      send_timeouts?: number;
      send_failures?: number;
      dropped_sockets?: number;
    };
  };
  rate_limits: {
    enabled: boolean;
    order_submit_rate_per_second: number;
    order_submit_burst: number;
    order_cancel_rate_per_second: number;
    order_cancel_burst: number;
  };
  risk_limits: {
    max_open_orders_per_user_market: number;
    max_open_orders_per_user_total: number;
  };
  orderbook_invariants?: {
    symbol: string;
    product_type?: "SPOT" | "PERP" | string;
    engine_open_order_count?: number;
    db_live_open_order_count?: number;
    engine_only_count?: number;
    db_only_count?: number;
    orderbook?: {
      same_seq_diff_count?: number;
      seq_rollback_count?: number;
    };
    reconcile?: Record<string, unknown> | null;
    flow?: {
      action_queue_size?: number | null;
      active_action_size?: number | null;
      action_load_size?: number | null;
      in_flight?: number | null;
      skipped_queue?: number | null;
      last_status?: string | null;
    };
  }[];
  books?: {
    symbol: string;
    open_orders: number;
    bid_levels: number;
    ask_levels: number;
  }[];
  warnings: string[];
};
type MarketTopOwner = {
  type?: string;
  label?: string;
  usernames?: string[];
};
type MarketSurveillanceItem = {
  symbol: string;
  market_type: "mainstream" | "listed";
  is_active: boolean;
  status: "ok" | "warn" | "critical" | string;
  top: {
    bid?: MarketTopOwner;
    ask?: MarketTopOwner;
  };
  metrics: {
    best_bid?: string | null;
    best_ask?: string | null;
    spread_quote?: string | null;
    spread_pct: string;
    depth_amount_0_5pct: string;
    depth_amount_2pct: string;
    book_imbalance: string;
    bid_level_count: number;
    ask_level_count: number;
    open_order_count: number;
    open_order_notional: string;
    trade_count_24h: number;
    quote_volume_24h: string;
  };
  open_orders_by_role: {
    side: string;
    role: string;
    open_order_count: number;
    remaining_quantity: string;
    notional: string;
  }[];
  top_open_order_users: {
    username: string;
    role: string;
    open_order_count: number;
    remaining_quantity: string;
    notional: string;
  }[];
  checks: {
    code: string;
    severity: "ok" | "warn" | "critical" | string;
    label: string;
    detail: string;
  }[];
};
type MarketTemplate = {
  template_id: string;
  label: string;
  description: string;
  symbol: string;
  product_type?: "SPOT" | "PERP";
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
  funding_rate_mode?: string;
  funding_interest_rate?: string;
  funding_clamp_rate?: string;
  funding_cap_rate?: string;
  funding_impact_notional?: string;
  reference_price?: string | null;
  default_maker_fee_rate: string;
  default_taker_fee_rate: string;
  initial_base_balance: string;
  initial_quote_balance: string;
};
type MarketBotAccount = {
  id: number;
  symbol: string;
  uid: number;
  username: string;
  api_key: string;
  api_secret: string;
  api_secret_masked?: string;
  api_secret_present?: boolean;
  bot_label: string;
  role: "maker" | "flow" | "hedge" | string;
  strategy_role?: string | null;
  initial_quote_amount: string;
  initial_base_amount: string;
  initial_base_notional: string;
  reference_price: string;
  is_enabled: boolean;
  base_balance: { asset: string; available: string; frozen: string };
  quote_balance: { asset: string; available: string; frozen: string };
  contract_account?: {
    margin_asset: string;
    wallet_balance: string;
    available_margin: string;
    used_margin: string;
    unrealized_pnl: string;
    realized_pnl: string;
    total_fees: string;
  } | null;
  created_at?: number | null;
};
type StartReadinessItem = {
  code: string;
  label: string;
  detail: string;
};
type StartReadiness = {
  ok: boolean;
  blockers: StartReadinessItem[];
  warnings: StartReadinessItem[];
  summary?: Record<string, unknown>;
};
type MakerInstanceStatus = {
  symbol: string;
  status: "starting" | "running" | "stale" | "stopping" | "stopped" | "error" | string;
  persisted_status?: string;
  heartbeat_status?: string;
  pid?: number | null;
  running: boolean;
  strategy_version?: string | null;
  configured_strategy_version?: string | null;
  runtime_strategy_mismatch?: boolean;
  metrics_present: boolean;
  last_metrics_ts?: number | null;
  last_heartbeat_at?: number | null;
  started_at?: number | null;
  stopped_at?: number | null;
  last_error?: string | null;
  pid_file: string;
  lock_file: string;
  log_path: string;
  log_exists: boolean;
  last_log_lines: string[];
  log_scope?: "current_run" | "full_file" | string;
  run_id?: string | null;
  log_start_offset?: number | null;
  log_size?: number | null;
  log_started_at?: number | null;
  start_readiness?: StartReadiness | null;
};
type MarketBotFormState = {
  uid: string;
  username: string;
  password: string;
  apiKey: string;
  apiSecret: string;
  botLabel: string;
  role: "maker" | "flow" | "hedge";
  strategyRole: string;
  referencePrice: string;
  initialQuoteAmount: string;
  initialBaseNotional: string;
  initialBaseAmount: string;
  isEnabled: boolean;
};
type MarketBotEditState = Omit<MarketBotFormState, "uid">;
type MarketFeeEdit = {
  makerFeeRate: string;
  takerFeeRate: string;
};
type UserEditState = {
  role: string;
  isActive: boolean;
  password: string;
  makerFeeRate: string;
  takerFeeRate: string;
  marketFees: Record<string, MarketFeeEdit>;
};
type BalanceAdjustmentState = {
  asset: string;
  amount: string;
  reason: string;
};
type AccountActionPending = {
  key: string;
  label: string;
};
type SeedBookState = {
  midPrice: string;
  levels: string;
  gapTicks: string;
  quantity: string;
  cancelExisting: boolean;
};
type SweepPreviewState = {
  side: "buy" | "sell";
  mode: "quote_amount" | "quantity";
  quoteAmount: string;
  quantity: string;
  depth: string;
};
type SweepPreviewResult = {
  symbol: string;
  side: "buy" | "sell";
  input_mode: string;
  requested_quantity?: string | null;
  requested_quote_amount?: string | null;
  filled_quantity: string;
  quote_amount: string;
  remaining_quantity: string;
  remaining_quote_amount: string;
  avg_price: string;
  worst_price: string;
  reference_price: string;
  slippage_bps: string;
  move_pct: string;
  levels_used: number;
  status: string;
  severity: "ok" | "warn" | "critical" | string;
  summary: string;
  depth: number;
};
type NewMarketFormState = {
  defaultMakerStrategy?: string;
  priceSourceSymbol?: string;
  symbol: string;
  productType: string;
  marketType: string;
  baseAsset: string;
  quoteAsset: string;
  marginAsset: string;
  priceTick: string;
  qtyStep: string;
  minQty: string;
  minNotional: string;
  maxLeverage: string;
  defaultLeverage: string;
  maintenanceMarginRate: string;
  fundingRate: string;
  fundingIntervalHours: string;
  indexPriceSource: string;
  markPriceMode: string;
  fundingRateMode: string;
  fundingInterestRate: string;
  fundingClampRate: string;
  fundingCapRate: string;
  fundingImpactNotional: string;
  defaultMakerFeeRate: string;
  defaultTakerFeeRate: string;
  referencePrice: string;
  createDefaultBots: boolean;
  defaultBotCount: string;
  defaultBotInitialQuoteAmount: string;
  defaultBotInitialBaseNotional: string;
  isActive: boolean;
};

type ContractAdminUser = {
  id: number;
  username: string;
  role: string;
};
type ContractAccountAdminItem = {
  user: ContractAdminUser;
  account: ContractAccount;
};
type ContractPositionAdminItem = {
  user: ContractAdminUser;
  position: ContractPosition;
};
type ContractOrderAdminItem = {
  user: ContractAdminUser;
  order: OrderItem;
};
type AdminOrderAuditItem = {
  user: ContractAdminUser;
  order: OrderItem;
};
type AdminTradeAuditItem = {
  taker_user: ContractAdminUser;
  maker_user: ContractAdminUser;
  trade: TradeItem;
};
type AdminAuditQueryMeta = {
  query_scope: string;
  count_scope: string;
  product_type: string;
  symbol: string;
  user_id: number | null;
  status?: string | null;
  data_domain: OrderAuditDataScope;
  limit: number;
  result_count: number;
  has_more: boolean;
};
type AdminOperationAuditItem = {
  id: number;
  operation_id: string;
  created_at: number;
  actor_user_id?: number | null;
  actor_username?: string | null;
  domain: string;
  operation_type: string;
  target_type: string;
  target_id?: string | null;
  target_symbol?: string | null;
  status: string;
  summary: string;
  result?: unknown;
};
type AdminOperationResult = {
  title: string;
  status: "success" | "error";
  message: string;
  result?: unknown;
  context?: Record<string, unknown>;
  recovery?: AdminOperationRecoveryHint;
  ts: number;
};
type AdminActiveOperation = {
  title: string;
  context?: Record<string, unknown>;
  startedAt: number;
};
type AdminOperationRecoveryHint = {
  title: string;
  steps: string[];
  targetSection?: AdminSection;
  targetSymbol?: string | null;
  targetMarketDetailTab?: MarketDetailTab;
  targetContractTab?: ContractAdminTab;
  targetSystemTab?: SystemAdminTab;
  targetLabel?: string;
};
type AdminOperationAuditFilters = {
  domain: "all" | "account" | "market" | "bot" | "contract" | "system";
  status: "all" | "success" | "failed" | "error";
  operationType: string;
  targetSymbol: string;
  limit: "50" | "100" | "200";
};
type AdminOperationFailureRule = {
  match: string;
  domain: Exclude<AdminOperationAuditFilters["domain"], "all">;
  operationType: string;
  targetType: string;
};
type AdminOperationEvidence = {
  level: string;
  verificationPath: string;
  gap: string;
  tone: "proof" | "needs_check" | "limited";
};
type AdminOperationApprovalProfile = {
  level: "approval_required" | "change_window_required" | "runtime_confirmed" | "sandbox_confirmed";
  label: string;
  evidencePackage: string;
  gap: string;
  tone: "danger" | "warn" | "limited";
};
type AdminOperationArchiveProfile = {
  source: string;
  status: string;
  proofBoundary: string;
  gap: string;
  followUp: string;
};
type AccountDirectoryReview = {
  primaryLedger: string;
  verificationPath: string;
  boundary: string;
  tone: "customer" | "admin" | "spot_robot" | "contract_robot" | "system";
};
type SpotBalanceAdjustmentRunbook = {
  key: "credit" | "debit" | "single_reset" | "batch_reset";
  operation: string;
  preCheck: string;
  postCheck: string;
  recoveryPath: string;
  boundary: string;
  tone: "positive" | "negative" | "reset" | "danger";
};
type SpotBalanceAdjustmentPolicy = {
  policy: string;
  preCheck: string;
  postCheck: string;
  recoveryPath: string;
  boundary: string;
};
type AccountTimelineItem = {
  key: string;
  ts: number;
  domain: "现货" | "合约";
  event: string;
  market?: string | null;
  detail: string;
  amount?: string;
  tone?: "positive" | "negative" | "neutral";
};
type ContractDetailFilters = {
  symbol: string;
  query: string;
  fundingStatus: "all" | "active" | "failed" | "settled";
};
type ContractFundingEventAdminItem = {
  user: ContractAdminUser;
  event: ContractFundingEvent;
};
type ContractLedgerAdminItem = {
  user: ContractAdminUser;
  entry: ContractLedgerItem;
};
type ContractInsuranceEventAdminItem = ContractInsuranceEvent;
type ContractAdlEventAdminItem = ContractAdlEvent;
type ContractLiquidationEventAdminItem = {
  user: ContractAdminUser;
  event: ContractLiquidationEvent;
};
type ContractRiskTierEdit = {
  tier: number;
  notional_floor: string;
  notional_cap: string;
  max_leverage: string;
  maintenance_margin_rate: string;
  maintenance_amount: string;
};

type AdminSection = "maker_config" | "flow_config" | "overview" | "markets" | "orders" | "strategies" | "bots" | "accounts" | "accounting" | "contracts" | "risk" | "system";
type MarketDetailTab = "overview" | "base" | "rules" | "product" | "bots" | "strategy" | "instance" | "logs" | "maintenance";
type ContractAdminTab = "overview" | "accounts" | "positions" | "orders" | "funding" | "liquidation" | "insurance" | "risk";
type SystemAdminTab = "overview" | "deployment" | "consistency" | "runtime" | "operations" | "audit";
type OperationBoundaryView = "governance" | "roles" | "approval" | "catalog";
type SystemRuntimeView = "summary" | "retention" | "robot" | "books";
type AdminBusinessTarget = {
  section: AdminSection;
  targetSymbol?: string | null;
  targetUserId?: number | null;
  accountScope?: AccountUserScope;
  contractAccountScope?: ContractAccountScope;
  marketDetailTab?: MarketDetailTab;
  contractTab?: ContractAdminTab;
  systemTab?: SystemAdminTab;
};
type AccountUserKind = "customer" | "admin" | "spot_robot" | "contract_robot" | "system";
type AccountUserScope = "all" | "internal" | "robot" | AccountUserKind;
type AccountUserScopeOption = { key: AccountUserScope; label: string; hint: string; count: number };
type AccountAdminView = "overview" | "onboarding" | "detail" | "maintenance";
type ContractAccountKind = "customer" | "admin" | "spot_robot" | "contract_robot" | "system";
type ContractAccountScope = "all" | "internal" | "robot" | ContractAccountKind;
type AuditProductScope = "all" | "SPOT" | "PERP";
type AuditStatusScope = "live" | "all" | "new" | "partially_filled" | "filled" | "canceled" | "rejected";
type OrderAuditDataScope = "all" | "customer" | "robot" | "system";
type OrderAuditTarget = {
  product?: AuditProductScope;
  symbol?: string | null;
  userId?: number | string | null;
  status?: AuditStatusScope;
  dataScope?: OrderAuditDataScope;
};
type ContractDetailLoadState = {
  loaded: boolean;
  loading: boolean;
  error: string | null;
  updatedAt: number | null;
};
type ReconciliationCheckResult = {
  code: string;
  domain: string;
  severity: "blocking" | "warning";
  status: "passed" | "warning" | "failed";
  difference_count: number;
  max_difference: string | null;
  samples: Array<Record<string, string | number | null>>;
  recommendation: string;
};
type ReconciliationSummary = {
  run_id: string;
  scope: string;
  status: "passed" | "warning" | "failed";
  started_at: string;
  completed_at: string;
  elapsed_ms: number;
  watermark: Record<string, number>;
  difference_count: number;
  max_difference: string;
  blocking_count: number;
  allow_trading: boolean;
  automatic_repair: false;
  checks: ReconciliationCheckResult[];
};
type ShadowAccountingSummary = {
  mode: "shadow";
  source_of_truth: false;
  transactions: number;
  entries: number;
  reversals: number;
  immutable_committed_records: boolean;
  automatic_repair: false;
};
type FinancialOutboxSummary = {
  mode: "transactional_database_outbox";
  delivery_semantics: "at_least_once";
  consumer_deduplication_key: "event_id";
  checkpoint: null | { name: string; watermark: Record<string, number>; created_at: string };
  counts: { pending: number; processing: number; delivered: number; dead: number; total: number; receipts: number; retry_events: number; replay_requests: number };
  oldest_pending_at: string | null;
  automatic_balance_repair: false;
  runtime?: { metrics?: Record<string, unknown>; last_result?: Record<string, number> | null; last_error?: string | null };
};
type AccountingProofItem = {
  checkpoint_id: string;
  sequence_no: number;
  previous_checkpoint_id: string | null;
  reconciliation_run_id: string;
  status: "passed" | "failed";
  difference_count: number;
  blocking_count: number;
  watermark: Record<string, number>;
  counts: { spot_rows: number; contract_rows: number; insurance_rows: number; total_rows: number };
  business_snapshot_hash: string;
  accounting_snapshot_hash: string;
  proof_hash: string;
  completed_at: string;
};
type AccountingProofPage = {
  items: AccountingProofItem[];
  total: number;
  limit: number;
  offset: number;
  verification: {
    mode: "immutable_hash_chain";
    checkpoint_count: number;
    invalid_count: number;
    issues: Array<Record<string, unknown>>;
    latest: AccountingProofItem | null;
    hash_algorithm: "sha256";
    canonical_decimal_scale: number;
    signed_or_worm: false;
  };
};
type RobotFinancialCheckpointItem = {
  checkpoint_id: string;
  sequence_no: number;
  reconciliation_run_id: string;
  accounting_proof_checkpoint_id: string;
  status: "passed" | "failed";
  watermarks: { spot_start_exclusive: number; spot_end_id: number; contract_start_exclusive: number; contract_end_id: number };
  counts: {
    item_count: number;
    spot_source_count: number;
    contract_source_count: number;
    source_count: number;
    invalid_item_count: number;
    customer_source_count: number;
    raw_records_deleted: number;
    pruning_authorized: false;
  };
  checkpoint_hash: string;
  created_at: string;
};
type RobotFinancialCheckpointPage = {
  items: RobotFinancialCheckpointItem[];
  total: number;
  limit: number;
  offset: number;
  verification: {
    mode: "robot_financial_checkpoint_v1";
    checkpoint_count: number;
    invalid_count: number;
    issues: Array<Record<string, unknown>>;
    latest: RobotFinancialCheckpointItem | null;
    financial_pruning_authorized: false;
    raw_records_deleted: number;
  };
};
type AccountingGateReadiness = {
  gate_e: {
    status: "ready" | "collecting_evidence";
    ready: boolean;
    criteria: Array<{ code: string; passed: boolean; actual: unknown; required: unknown; evidence: string }>;
    observation: {
      first_completed_at: string | null;
      latest_completed_at: string | null;
      observation_hours: number;
      distinct_utc_dates: string[];
      accounting_proof_count: number;
      robot_checkpoint_count: number;
      nonempty_robot_checkpoint_count: number;
      robot_source_count: number;
    };
  };
  evidence_collector: {
    enabled: boolean;
    interval_seconds: number;
    automatic_repair: false;
    latest: null | {
      run_key: string;
      observation_date: string;
      status: "running" | "passed" | "failed";
      attempt_count: number;
      reconciliation_run_id: string | null;
      accounting_proof_checkpoint_id: string | null;
      robot_checkpoint_id: string | null;
      last_error_code: string | null;
      completed_at: string | null;
    };
  };
  gate_f: { status: "closed"; source_of_truth_switch_allowed: false; reason: string };
  automatic_repair: false;
};

const adminNavGroups: { title: string; items: { key: AdminSection; label: string; hint: string }[] }[] = [
  { title: "开始与日常操作", items: [
    { key: "overview", label: "工作台", hint: "了解状态，选择下一步" },
    { key: "markets", label: "交易市场", hint: "币对、交易规则、历史 K 线" },
    { key: "maker_config", label: "铺单策略", hint: "配置盘口深度与报价" },
    { key: "flow_config", label: "刷量策略", hint: "成交生成与图表量能" },
  ] },
  { title: "账户与交易", items: [
    { key: "accounts", label: "账户与资金", hint: "用户、现货钱包、保证金" },
    { key: "orders", label: "订单与成交", hint: "查看委托及成交记录" },
    { key: "bots", label: "机器人账户", hint: "身份与市场绑定" },
    { key: "strategies", label: "策略模板", hint: "新币对的默认参数" },
  ] },
  { title: "诊断与高级管理", items: [
    { key: "risk", label: "风险监控", hint: "异常定位与处理入口" },
    { key: "contracts", label: "合约清算", hint: "资金费、强平与保险基金" },
    { key: "accounting", label: "账务对账", hint: "一致性检查与差异" },
    { key: "system", label: "系统与审计", hint: "运行资源与操作记录" },
  ] },
];
const adminSectionKeys: AdminSection[] = ["maker_config", "flow_config", "overview", "markets", "orders", "strategies", "bots", "accounts", "accounting", "contracts", "risk", "system"];
const marketDetailTabKeys: MarketDetailTab[] = ["overview", "base", "rules", "product", "bots", "strategy", "instance", "logs", "maintenance"];
const contractAdminTabKeys: ContractAdminTab[] = ["overview", "accounts", "positions", "orders", "funding", "liquidation", "insurance", "risk"];
const systemAdminTabKeys: SystemAdminTab[] = ["overview", "deployment", "consistency", "runtime", "operations", "audit"];
const accountUserKindKeys: AccountUserKind[] = ["customer", "admin", "spot_robot", "contract_robot", "system"];
const accountUserScopeKeys: AccountUserScope[] = ["all", "customer", "internal", "robot", "admin", "spot_robot", "contract_robot", "system"];
const contractAccountScopeKeys: ContractAccountScope[] = ["all", "customer", "internal", "robot", "admin", "spot_robot", "contract_robot", "system"];

const spotBalanceAdjustmentRunbooks: SpotBalanceAdjustmentRunbook[] = [
  {
    key: "credit",
    operation: "现货入账",
    preCheck: "确认目标 UID、主体分类、资产、入账金额和原因；金额必须为正数，并确认不是合约保证金补充。",
    postCheck: "核对现货可用余额、冻结余额、现货资金流水和系统操作记录。",
    recoveryPath: "如果失败，先筛选 adjust_spot_balance 操作记录和该 UID 现货流水，再决定是否补录反向调整。",
    boundary: "只影响现货钱包；不改变合约保证金、仓位、资金费或机器人实例状态。",
    tone: "positive",
  },
  {
    key: "debit",
    operation: "现货扣款",
    preCheck: "确认目标 UID、主体分类、资产、扣减金额和原因；检查可用余额、冻结余额和未完成现货订单。",
    postCheck: "核对扣减后可用余额、冻结余额、现货资金流水和订单冻结资金是否仍一致。",
    recoveryPath: "不要连续重复扣款；先确认是否已有部分账变，再用反向入账恢复差额。",
    boundary: "不能用来处理合约亏损、强平坏账或资金费；这些回到合约清算域。",
    tone: "negative",
  },
  {
    key: "single_reset",
    operation: "单 UID 现货资金重置",
    preCheck: "确认目标 UID 是测试主体并确认演练窗口；记录重置前现货余额、合约保证金索引和机器人绑定。",
    postCheck: "核对重置后现货余额、目标 UID 活动、操作记录；若有合约保证金账户，再回合约清算复核保证金未被混改。",
    recoveryPath: "重置异常时不要手工改库，先用账户 CSV 和操作记录定位差异 UID。",
    boundary: "这是测试沙盒恢复动作，不是生产调账流程；不应替代正式审计审批。",
    tone: "reset",
  },
  {
    key: "batch_reset",
    operation: "批量测试主体资金重置",
    preCheck: "确认全部测试任务已暂停、无重要演练数据需要保留，并明确影响测试主体范围。",
    postCheck: "导出当前主体 CSV 抽查客户、机器人和系统身份；再回市场、机器人和合约清算查看关键状态。",
    recoveryPath: "如范围错误，立即保留操作记录和导出快照，按 UID 逐个恢复，不重复批量执行。",
    boundary: "高风险批量动作；只用于本地测试主体恢复，不用于真实客户或生产资金。",
    tone: "danger",
  },
];

const cloneValue = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isPrimitive = (value: unknown): value is string | number | boolean | null =>
  value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean";

const shouldLoadContractDetailsForSection = (section: AdminSection) =>
  section === "contracts" || section === "risk" || section === "accounts";

const defaultAdminOperationFilters = (): AdminOperationAuditFilters => ({
  domain: "all",
  status: "all",
  operationType: "all",
  targetSymbol: "",
  limit: "100",
});

const adminOperationAuditDomain = (domain: string): AdminOperationAuditFilters["domain"] =>
  domain === "account" || domain === "market" || domain === "bot" || domain === "contract" || domain === "system" ? domain : "all";

const adminOperationAuditStatus = (status: string): AdminOperationAuditFilters["status"] =>
  status === "failed" || status === "error" || status === "success" ? status : "all";

const adminOperationAuditLimit = (limit: string): AdminOperationAuditFilters["limit"] =>
  limit === "50" || limit === "100" || limit === "200" ? limit : "100";

const adminOperationTypeInput = (value: string) => value.trim().toLowerCase().replace(/\s+/g, "_").slice(0, 64);

const adminOperationFiltersFromParams = (params: URLSearchParams): AdminOperationAuditFilters => ({
  domain: adminOperationAuditDomain((params.get("opDomain") || "").trim()),
  status: adminOperationAuditStatus((params.get("opStatus") || "").trim()),
  operationType: adminOperationTypeInput(params.get("opType") || "") || "all",
  targetSymbol: normalizeMarketSymbolInput(params.get("opSymbol") || ""),
  limit: adminOperationAuditLimit((params.get("opLimit") || "").trim()),
});

const hasAdminOperationFilterParams = (params: URLSearchParams) =>
  params.has("opDomain") || params.has("opStatus") || params.has("opType") || params.has("opSymbol") || params.has("opLimit");

const adminOperationFiltersEqual = (left: AdminOperationAuditFilters, right: AdminOperationAuditFilters) =>
  left.domain === right.domain
  && left.status === right.status
  && left.operationType === right.operationType
  && left.targetSymbol === right.targetSymbol
  && left.limit === right.limit;

const hasNonDefaultAdminOperationFilters = (filters: AdminOperationAuditFilters) =>
  !adminOperationFiltersEqual(filters, defaultAdminOperationFilters());

const contractTabForOperationType = (operationType: string): ContractAdminTab => {
  const type = adminOperationTypeInput(operationType);
  if (type === "adjust_contract_account") return "accounts";
  if (["settle_funding", "retry_funding_job", "retry_failed_funding_jobs"].includes(type)) return "funding";
  if (type === "execute_adl") return "liquidation";
  if (type === "adjust_insurance_fund") return "insurance";
  if (["save_risk_tiers", "update_risk_tiers"].includes(type)) return "risk";
  if (["seed_contract_book", "seed_contract_orderbook", "reset_contract_maker_state"].includes(type)) return "orders";
  return "overview";
};

const marketTabForOperationType = (operationType: string): MarketDetailTab => {
  const type = adminOperationTypeInput(operationType);
  if (type === "update_contract_trading_mode") return "product";
  if (type.includes("seed") || type.includes("reset") || type.includes("wipe")) return "logs";
  return "overview";
};

const adminOperationQuery = (filters: AdminOperationAuditFilters) => {
  const params = new URLSearchParams();
  params.set("limit", filters.limit);
  if (filters.domain !== "all") params.set("domain", filters.domain);
  if (filters.status !== "all") params.set("status", filters.status);
  const operationType = adminOperationTypeInput(filters.operationType);
  if (operationType && operationType !== "all") params.set("operation_type", operationType);
  const symbol = normalizeMarketSymbolInput(filters.targetSymbol);
  if (symbol) params.set("target_symbol", symbol);
  return `/admin/operations?${params.toString()}`;
};

const adminOperationFiltersForItem = (item: AdminOperationAuditItem): AdminOperationAuditFilters => ({
  domain: adminOperationAuditDomain(item.domain),
  status: adminOperationAuditStatus(item.status),
  operationType: adminOperationTypeInput(item.operation_type) || "all",
  targetSymbol: item.target_symbol ?? "",
  limit: "100",
});

const operationRecoveryHint = (title: string, context?: Record<string, unknown>): AdminOperationRecoveryHint => {
  const contextSymbol = typeof context?.symbol === "string" ? normalizeMarketSymbolInput(context.symbol) : undefined;
  if (
    title.includes("创建交易币对")
    || title.includes("保存市场参数")
    || title.includes("保存市场默认费率")
    || title.includes("暂停市场")
    || title.includes("恢复市场")
  ) {
    return {
      title: "先回市场详情核对配置和运行边界",
      targetSection: "markets",
      targetSymbol: contextSymbol,
      targetMarketDetailTab: title.includes("费率") ? "rules" : title.includes("参数") ? "base" : "overview",
      targetLabel: "打开市场详情",
      steps: [
        "刷新市场运营，确认目标市场的启用状态、产品类型、规则和默认费率是否已经变化。",
        "如果是 PERP 市场，再到合约清算核对交易模式、价格源、风险参数和最近拒单。",
        "创建或保存失败后不要手工改库，先查看系统与审计的操作记录和后端错误详情。",
      ],
    };
  }
  if (title.includes("机器人账号")) {
    return {
      title: "先核对机器人账号绑定和资金归属",
      targetSection: "bots",
      targetLabel: "打开机器人账号",
      steps: [
        "到机器人账号资源池确认目标市场、UID、API Key、角色和启用状态。",
        "SPOT 机器人继续到账户与资金核对现货余额和流水；PERP 机器人继续到合约清算核对保证金账户和持仓风险。",
        "账号创建或保存失败后不要连续重复提交，先确认是否已经生成登录主体、API 凭据或资金模板。",
      ],
    };
  }
  if (title.includes("机器人策略")) {
    return {
      title: "先核对策略参数和实例运行状态",
      targetSection: "maker_config",
      targetSymbol: contextSymbol,
      targetLabel: "打开铺单策略",
      steps: [
        "到机器人运营确认目标市场、已保存策略、运行策略、实例状态和最近 apply status。",
        "SPOT 运行中保存可能热生效并清理旧策略挂单；PERP 策略切换通常需要重启实例才完全切到新策略。",
        "保存失败后不要直接重启实例，先确认策略配置是否已写入、运行策略是否仍是旧版本。",
      ],
    };
  }
  if (title.includes("做市实例")) {
    return {
      title: "先核对实例状态，再决定是否重试",
      targetSection: "maker_config",
      targetSymbol: contextSymbol,
      targetLabel: "打开铺单策略",
      steps: [
        "到机器人运营查看当前实例状态、heartbeat、pid identity 和最近日志。",
        "如果涉及重启或停止并撤单，先确认旧挂单清理结果，避免连续重复操作。",
        "若失败原因指向凭据、余额或 FLOW guard，先处理对应账户和策略状态后再重试。",
      ],
    };
  }
  if (title.includes("合约保证金调账")) {
    return {
      title: "先核对合约保证金账户和流水",
      targetSection: "contracts",
      targetContractTab: "accounts",
      targetLabel: "打开保证金账户",
      steps: [
        "到合约清算的保证金账户 tab 核对目标 UID、保证金钱包、可用保证金和最近合约流水。",
        "调账失败后不要连续提交大额变更，先确认是否已经产生局部账本变化。",
        "再到系统与审计操作记录筛选 adjust_contract_account，核对成功或失败记录。",
      ],
    };
  }
  if (title.includes("合约盘口初始化")) {
    return {
      title: "先核对合约订单和系统流动性账户",
      targetSection: "contracts",
      targetSymbol: contextSymbol,
      targetContractTab: "orders",
      targetLabel: "打开合约订单与成交",
      steps: [
        "到合约清算的订单与成交 tab 核对系统流动性账户是否产生委托。",
        "再回看合约保证金账户，确认铺盘账户的保证金和可用余额没有异常。",
        "不要反复铺盘；先确认当前盘口和订单簿一致性后再决定是否重试。",
      ],
    };
  }
  if (title.includes("切换合约交易模式")) {
    return {
      title: "先回市场产品页核对交易模式",
      targetSection: "markets",
      targetSymbol: contextSymbol,
      targetMarketDetailTab: "product",
      targetLabel: "打开市场产品配置",
      steps: [
        "到市场运营的后台参数 / 产品配置区核对 PERP 交易模式。",
        "确认交易页和合约下单入口是否按 normal / reduce_only / paused 展示正确状态。",
        "模式切换失败时先保留错误详情，不要跳过确认提示直接重复提交。",
      ],
    };
  }
  if (title.includes("资金费")) {
    return {
      title: "先核对资金费任务和结算水位",
      targetSection: "contracts",
      targetSymbol: contextSymbol,
      targetContractTab: "funding",
      targetLabel: "打开资金费",
      steps: [
        "到合约清算的资金费 tab 查看任务状态、失败原因和结算水位。",
        "已有 settlement 的周期不要重复手工扣款，避免重复影响合约保证金钱包。",
        "如果连续失败，保留错误详情并检查后端资金费 worker 日志。",
      ],
    };
  }
  if (title.includes("保险基金") || title.includes("ADL") || title.includes("风险阶梯")) {
    const targetContractTab: ContractAdminTab = title.includes("保险基金") ? "insurance" : title.includes("ADL") ? "liquidation" : "risk";
    return {
      title: "先回到清算域核对事件和参数",
      targetSection: "contracts",
      targetSymbol: contextSymbol,
      targetContractTab,
      targetLabel: targetContractTab === "insurance" ? "打开保险基金" : targetContractTab === "liquidation" ? "打开强平 / ADL" : "打开风险参数",
      steps: [
        "到合约清算对应 tab 核对强平、ADL、保险基金或风险参数的当前状态。",
        "不要连续提交同一清算动作；先确认事件状态、残余坏账和风险监控告警。",
        "参数类失败应保留当前输入，核对阶梯边界后再保存。",
      ],
    };
  }
  if (title.includes("调账") || title.includes("重置") || title.includes("API Key") || title.includes("主体身份") || title.includes("账户身份") || title.includes("创建登录主体") || title.includes("创建账户")) {
    return {
      title: "先核对目标 UID、主体分类和流水",
      targetSection: "accounts",
      targetLabel: "打开账户与资金",
      steps: [
        "到账户与资金确认目标 UID、主体分类、余额、API Key 状态和最近流水。",
        "调账或重置失败后不要重复提交大额变更，必要时先用只读视图核对差异。",
        "API Key 轮换失败时，不要把旧 secret 外发，先确认机器人或脚本凭据来源。",
      ],
    };
  }
  if (title.includes("盘口") || title.includes("撤销市场挂单") || title.includes("清理行情历史") || title.includes("清除 K 线")) {
    return {
      title: "先核对市场状态和订单簿一致性",
      targetSection: "markets",
      targetLabel: "打开市场运营",
      steps: [
        "到市场运营确认当前市场、挂单、K 线和历史保留状态。",
        "清历史或撤挂单失败后不要手工改库，先查看系统与审计的一致性和运行资源。",
        "如涉及合约盘口初始化，还要回合约清算确认保证金账户和当前委托。",
      ],
    };
  }
  return {
    title: "先刷新业务域并保留错误详情",
    targetSection: "system",
    targetLabel: "打开系统与审计",
    steps: [
      "刷新当前业务页，确认这次失败是否已经产生局部状态变化。",
      "如果状态不一致，先查看系统与审计的一致性、运行资源和最近操作记录。",
      "保留错误详情用于查后端日志，不要直接连续重复危险操作。",
    ],
  };
};

const adminOperationFailureRules: AdminOperationFailureRule[] = [
  { match: "创建登录主体", domain: "account", operationType: "create_user", targetType: "user" },
  { match: "创建账户", domain: "account", operationType: "create_user", targetType: "user" },
  { match: "保存主体身份", domain: "account", operationType: "update_user_identity", targetType: "user" },
  { match: "保存账户身份", domain: "account", operationType: "update_user_identity", targetType: "user" },
  { match: "保存全市场费率", domain: "account", operationType: "update_user_fees_all", targetType: "user" },
  { match: "保存单市场费率", domain: "account", operationType: "update_user_fees", targetType: "user" },
  { match: "创建交易币对", domain: "market", operationType: "create_market", targetType: "market" },
  { match: "保存市场参数", domain: "market", operationType: "update_market_config", targetType: "market" },
  { match: "保存市场默认费率", domain: "market", operationType: "update_market_fees", targetType: "market" },
  { match: "暂停市场", domain: "market", operationType: "update_market_status", targetType: "market" },
  { match: "恢复市场", domain: "market", operationType: "update_market_status", targetType: "market" },
  { match: "重置单 UID 现货资金", domain: "account", operationType: "reset_user_balances", targetType: "user" },
  { match: "重置单账户资金", domain: "account", operationType: "reset_user_balances", targetType: "user" },
  { match: "批量重置测试主体资金", domain: "account", operationType: "reset_test_users", targetType: "test_users" },
  { match: "批量重置测试账户", domain: "account", operationType: "reset_test_users", targetType: "test_users" },
  { match: "轮换 API Key", domain: "account", operationType: "rotate_api_key", targetType: "user" },
  { match: "现货余额调账", domain: "account", operationType: "adjust_spot_balance", targetType: "user" },
  { match: "合约保证金调账", domain: "contract", operationType: "adjust_contract_account", targetType: "user" },
  { match: "合约盘口初始化", domain: "contract", operationType: "seed_contract_orderbook", targetType: "market" },
  { match: "现货盘口初始化", domain: "market", operationType: "seed_spot_orderbook", targetType: "market" },
  { match: "切换合约交易模式", domain: "market", operationType: "update_contract_trading_mode", targetType: "market" },
  { match: "资金费结算", domain: "contract", operationType: "settle_funding", targetType: "market" },
  { match: "资金费任务重试", domain: "contract", operationType: "retry_funding_job", targetType: "funding_job" },
  { match: "资金费批量重试", domain: "contract", operationType: "retry_failed_funding_jobs", targetType: "funding_jobs" },
  { match: "保险基金调账", domain: "contract", operationType: "adjust_insurance_fund", targetType: "insurance_fund" },
  { match: "执行 ADL", domain: "contract", operationType: "execute_adl", targetType: "liquidation" },
  { match: "保存风险阶梯", domain: "contract", operationType: "update_risk_tiers", targetType: "market" },
  { match: "撤销市场挂单", domain: "market", operationType: "reset_spot_market", targetType: "market" },
  { match: "清理行情历史", domain: "market", operationType: "wipe_spot_market_data", targetType: "market" },
  { match: "清除历史 K 线", domain: "market", operationType: "wipe_market_klines", targetType: "market" },
  { match: "创建机器人账号", domain: "bot", operationType: "create_market_bot", targetType: "market" },
  { match: "补默认机器人账号", domain: "bot", operationType: "create_default_market_bots", targetType: "market" },
  { match: "补 FLOW 机器人账号", domain: "bot", operationType: "create_default_flow_bot", targetType: "market" },
  { match: "保存机器人账号", domain: "bot", operationType: "update_market_bot", targetType: "market" },
  { match: "保存机器人策略", domain: "bot", operationType: "update_market_strategy", targetType: "market" },
  { match: "启动做市实例", domain: "bot", operationType: "maker_instance_start", targetType: "market" },
  { match: "停止做市实例", domain: "bot", operationType: "maker_instance_stop", targetType: "market" },
  { match: "停止并撤单", domain: "bot", operationType: "maker_instance_stop_and_cancel", targetType: "market" },
  { match: "重启做市实例", domain: "bot", operationType: "maker_instance_restart", targetType: "market" },
];

const adminFailureAuditPayload = (
  title: string,
  failureMessage: string,
  errorDetail: string,
  context?: Record<string, unknown>,
) => {
  const rule = adminOperationFailureRules.find((item) => title.includes(item.match)) ?? {
    domain: "system" as const,
    operationType: "admin_operation_failure",
    targetType: "operation",
  };
  const targetId = context?.user_id ?? context?.job_id ?? context?.event_id ?? context?.target_id ?? undefined;
  const targetSymbol = typeof context?.symbol === "string" ? context.symbol : undefined;
  return {
    domain: rule.domain,
    operation_type: rule.operationType,
    target_type: rule.targetType,
    target_id: targetId,
    target_symbol: targetSymbol,
    summary: failureMessage,
    error: errorDetail,
    context: { title, ...(context ?? {}) },
  };
};

const humanizeKey = (key: string) =>
  key
    .replace(/_/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, (match) => match.toUpperCase());

const maskSecret = (value?: string | null) => {
  if (!value) return "-";
  if (value.length <= 10) return "****";
  return `${value.slice(0, 6)}...${value.slice(-4)}`;
};

const normalizeMarketSymbolInput = (value: string) => value.replace(/\s+/g, "").toUpperCase();

const coerceBotRole = (role?: string): "maker" | "flow" | "hedge" =>
  role === "flow" || role === "hedge" ? role : "maker";

const defaultMarketBotEdit = (bot: MarketBotAccount): MarketBotEditState => ({
  username: bot.username,
  password: "",
  apiKey: bot.api_key,
  apiSecret: bot.api_secret_masked ?? bot.api_secret ?? "",
  botLabel: bot.bot_label,
  role: coerceBotRole(bot.role),
  strategyRole: bot.strategy_role ?? bot.role,
  referencePrice: bot.reference_price,
  initialQuoteAmount: bot.initial_quote_amount,
  initialBaseNotional: bot.initial_base_notional,
  initialBaseAmount: bot.initial_base_amount,
  isEnabled: bot.is_enabled,
});

const marketBotEditPayload = (form: MarketBotEditState) => ({
  username: form.username || undefined,
  password: form.password || undefined,
  api_key: form.apiKey || undefined,
  api_secret: form.apiSecret || undefined,
  bot_label: form.botLabel || undefined,
  role: form.role,
  strategy_role: form.strategyRole || form.role,
  reference_price: form.referencePrice || undefined,
  initial_quote_amount: form.initialQuoteAmount,
  initial_base_notional: form.initialBaseNotional,
  initial_base_amount: form.initialBaseAmount || undefined,
  is_enabled: form.isEnabled,
});

const marketBotSecretChanged = (bot: MarketBotAccount, form: MarketBotEditState) => {
  const nextSecret = form.apiSecret.trim();
  if (!nextSecret) return false;
  const currentSecret = bot.api_secret_masked ?? bot.api_secret ?? "";
  return nextSecret !== currentSecret && nextSecret !== bot.api_secret;
};

const marketBotEditChangeGroups = (bot: MarketBotAccount, form: MarketBotEditState, isPerp: boolean) => {
  const groups: string[] = [];
  const trimmedStrategyRole = form.strategyRole.trim() || form.role;
  if (form.username.trim() !== bot.username || form.botLabel.trim() !== bot.bot_label) {
    groups.push("身份标签");
  }
  if (form.apiKey.trim() !== bot.api_key || marketBotSecretChanged(bot, form) || Boolean(form.password.trim())) {
    groups.push("API / 登录凭据");
  }
  if (form.role !== coerceBotRole(bot.role) || trimmedStrategyRole !== (bot.strategy_role ?? bot.role)) {
    groups.push("策略绑定");
  }
  if (
    form.referencePrice !== bot.reference_price
    || form.initialQuoteAmount !== bot.initial_quote_amount
    || form.initialBaseNotional !== bot.initial_base_notional
    || form.initialBaseAmount !== bot.initial_base_amount
  ) {
    groups.push(isPerp ? "合约保证金模板" : "现货资金模板");
  }
  if (form.isEnabled !== bot.is_enabled) {
    groups.push("启停状态");
  }
  return groups;
};

const marketBotTemplateBoundary = (market?: AdminMarketItem) =>
  market?.product_type === "PERP"
    ? "PERP 会创建或同步合约保证金账户；保证金流水、仓位、资金费和清算风险仍回合约清算核对。"
    : "SPOT 会写入现货 base/quote 初始模板；现货流水和余额维护仍回账户与资金核对。";

const marketBotOperationBoundary = (market?: AdminMarketItem) =>
  market?.product_type === "PERP"
    ? "不会启动策略实例、不会调账或清算、不会使用现货钱包抵扣合约保证金。"
    : "不会启动策略实例、不会调账或清算、不会创建合约仓位或合约保证金。";

const stableConfigFingerprint = (value: unknown) => {
  try {
    return JSON.stringify(value ?? {});
  } catch {
    return "";
  }
};

const strategySaveBoundary = (market?: { product_type?: string }) =>
  market?.product_type === "PERP"
    ? "PERP 参数会被运行实例轮询读取；策略切换通常需要保存并重启才完全切换运行策略。"
    : "SPOT 参数保存后运行实例约 1 秒内轮询生效；切换策略可能触发旧策略挂单清理。";

const setValueAtPath = (source: unknown, path: Array<string | number>, nextValue: unknown): unknown => {
  if (path.length === 0) return nextValue;
  const [head, ...tail] = path;
  if (Array.isArray(source)) {
    const next = [...source];
    next[Number(head)] = setValueAtPath(next[Number(head)], tail, nextValue);
    return next;
  }
  const current = isPlainObject(source) ? source : {};
  return {
    ...current,
    [String(head)]: tail.length > 0 ? setValueAtPath(current[String(head)], tail, nextValue) : nextValue,
  };
};

const getValueAtPath = (source: unknown, path: string): unknown => {
  let cursor = source;
  for (const segment of path.split(".").filter(Boolean)) {
    if (!isPlainObject(cursor) && !Array.isArray(cursor)) return undefined;
    cursor = (cursor as Record<string, unknown>)[segment];
  }
  return cursor;
};

const strategySchemaFields = (strategy?: MarketStrategyConfigState): StrategySchemaField[] =>
  (strategy?.schema?.sections ?? []).flatMap((section) => section.fields ?? []);

const deriveMarketPrecisions = (market?: AdminMarketItem) => {
  const pricePrecision = stepDigits(market?.price_tick) ?? 0;
  const qtyPrecision = Math.max(stepDigits(market?.qty_step) ?? 0, stepDigits(market?.min_qty) ?? 0);
  return { pricePrecision, qtyPrecision };
};

type MarketEditScope = "base" | "rules" | "fees" | "product";

type MarketEditField = {
  field: keyof AdminMarketItem;
  label: string;
};

const comparableMarketValue = (market: AdminMarketItem | undefined, field: keyof AdminMarketItem) => {
  const value = market?.[field];
  if (field === "contract_trading_mode") return normalizeContractTradingMode(String(value ?? "normal"));
  return value === null || value === undefined ? "" : String(value);
};

const marketEditFieldsForScope = (scope: MarketEditScope, market: AdminMarketItem): MarketEditField[] => {
  if (scope === "base") {
    return [
      { field: "product_type", label: "产品类型" },
      { field: "market_type", label: "市场类型" },
      ...(market.product_type === "PERP" ? [{ field: "margin_asset" as const, label: "保证金币种" }] : []),
    ];
  }
  if (scope === "rules") {
    return [
      { field: "price_tick", label: "价格步长" },
      { field: "qty_step", label: "数量步长" },
      { field: "min_qty", label: "最小数量" },
      { field: "min_notional", label: "最小成交额" },
    ];
  }
  if (scope === "fees") {
    return [
      { field: "default_maker_fee_rate", label: "默认 maker 费率" },
      { field: "default_taker_fee_rate", label: "默认 taker 费率" },
    ];
  }
  if (market.product_type === "PERP") {
    return [
      { field: "max_leverage", label: "最大杠杆" },
      { field: "default_leverage", label: "默认杠杆" },
      { field: "maintenance_margin_rate", label: "维持保证金率" },
      { field: "funding_rate", label: "资金费率" },
      { field: "funding_interval_hours", label: "资金费间隔" },
      { field: "index_price_source", label: "指数价来源" },
      { field: "mark_price_mode", label: "标记价模式" },
      { field: "funding_rate_mode", label: "资金费模式" },
      { field: "contract_trading_mode", label: "合约交易模式" },
    ];
  }
  return [{ field: "reference_price", label: "参考价" }];
};

const changedMarketEditFields = (market: AdminMarketItem, persistedMarket: AdminMarketItem | undefined, scope: MarketEditScope) => {
  if (!persistedMarket) return [];
  return marketEditFieldsForScope(scope, market).filter((item) => (
    comparableMarketValue(market, item.field) !== comparableMarketValue(persistedMarket, item.field)
  ));
};

const changedMarketEditFieldCount = (
  market: AdminMarketItem | undefined,
  persistedMarket: AdminMarketItem | undefined,
  scopes: MarketEditScope[],
) => (
  market && persistedMarket
    ? scopes.reduce((total, scope) => total + changedMarketEditFields(market, persistedMarket, scope).length, 0)
    : 0
);

type ContractTradingMode = "normal" | "reduce_only" | "paused";

const normalizeContractTradingMode = (mode?: string | null): ContractTradingMode =>
  mode === "reduce_only" || mode === "paused" ? mode : "normal";

const contractTradingModeLabel = (mode?: string | null) => {
  const normalized = normalizeContractTradingMode(mode);
  if (normalized === "reduce_only") return "只减仓";
  if (normalized === "paused") return "暂停新单";
  return "正常交易";
};

const contractTradingModeDescription = (mode?: string | null) => {
  const normalized = normalizeContractTradingMode(mode);
  if (normalized === "reduce_only") return "拒绝新的开仓订单，保留减仓和平仓路径。";
  if (normalized === "paused") return "拒绝该合约市场的新订单，适合临时风控或故障隔离。";
  return "允许正常开仓、减仓和平仓。";
};

const contractTradingModeClass = (mode?: string | null) => {
  const normalized = normalizeContractTradingMode(mode);
  if (normalized === "paused") return "bg-rose-500/16 text-rose-100";
  if (normalized === "reduce_only") return "bg-amber-400/16 text-amber-100";
  return "bg-emerald-400/15 text-emerald-100";
};

const contractTradingModeConfirmMessage = (symbol: string, previousMode: ContractTradingMode, nextMode: ContractTradingMode) =>
  [
    `${symbol} 合约交易模式将从「${contractTradingModeLabel(previousMode)}」切换为「${contractTradingModeLabel(nextMode)}」。`,
    contractTradingModeDescription(nextMode),
    "该操作会影响该合约市场的新下单行为，请确认已经通知相关运营/机器人侧流程。",
  ].join("\n\n");

const contractTradingModeRunbookRows: ContractTradingMode[] = ["normal", "reduce_only", "paused"];

function contractTradingModeRunbook(mode?: string | null) {
  const normalized = normalizeContractTradingMode(mode);
  if (normalized === "reduce_only") {
    return {
      scenario: "风险观察、价格源异常、保证金压力或需要保留平仓通道时使用。",
      preCheck: "核对当前委托、活跃仓位、机器人实例、保证金占用、价格源和操作记录，确认不再允许新开仓。",
      postCheck: "检查交易页模式提示、新开仓拒单原因、减仓/平仓路径、机器人是否停止新增 open 单。",
      recovery: "异常解除后先回合约清算核对仓位风险和保证金，再切回 normal 并复查订单与成交、机器人运行和失败操作记录。",
      boundary: "只减仓不是清算动作，不自动撤单、不自动释放保证金、不修复历史拒单或机器人策略参数。",
    };
  }
  if (normalized === "paused") {
    return {
      scenario: "市场故障、价格源不可用、清算链路异常或需要临时冻结合约新单时使用。",
      preCheck: "确认盘口、价格源、风险队列、资金费任务、强平/ADL 状态和机器人控制面，通知相关运营流程。",
      postCheck: "检查所有新单拒绝、交易页提示、机器人实例不继续下单、风险监控和系统与审计无新增关键失败。",
      recovery: "恢复前先修复故障源并核对价格/账本/仓位，再按 reduce_only 或 normal 分阶段恢复，保留操作记录和后验核对。",
      boundary: "暂停新单不等于停机撤单、市场清理、账本冻结或强平处理；已有风险仍回合约清算和风险监控处置。",
    };
  }
  return {
    scenario: "正常交易窗口，允许开仓、减仓和平仓。",
    preCheck: "从 reduce_only/paused 恢复前核对价格源、风险队列、保证金账户、当前委托、机器人参数和最近失败操作。",
    postCheck: "检查交易页可下单、订单与成交有正常生命周期、机器人可按策略挂单、风险监控没有阻断项。",
    recovery: "如恢复后出现拒单、价格源异常或保证金异常，立即回 reduce_only 或 paused，并回操作记录、合约清算和机器人运营核对。",
    boundary: "normal 只恢复下单准入，不自动补单、重算费用、重建盘口、修复资金或清算历史。",
  };
}

function contractTradingModeRunbookBoundaryNote(mode?: string | null) {
  const runbook = contractTradingModeRunbook(mode);
  return `${runbook.boundary} 当前演练口径只做运营核对，不替代审批单、发布单或不可变审计账本。`;
}

const defaultBalanceAdjustment = (user: AdminUser): BalanceAdjustmentState => ({
  asset: user.balances.find((item) => item.asset === "USDT")?.asset ?? user.balances[0]?.asset ?? "USDT",
  amount: "1000",
  reason: "admin manual adjustment",
});

const sumDecimalStrings = (items: string[]) => items.reduce((sum, value) => sum + Number(value || 0), 0);

const isLiveOrderStatus = (status?: string | null) => status === "new" || status === "partially_filled";

type OrderStateBucket = "live" | "filled" | "canceled" | "rejected" | "expired" | "other";

const orderStateBuckets: OrderStateBucket[] = ["live", "filled", "canceled", "rejected", "expired", "other"];

function orderStateBucket(status?: string | null): OrderStateBucket {
  const normalized = String(status ?? "").toLowerCase();
  if (normalized === "new" || normalized === "partially_filled") return "live";
  if (normalized === "filled") return "filled";
  if (normalized === "canceled" || normalized === "cancelled") return "canceled";
  if (normalized === "rejected") return "rejected";
  if (normalized === "expired") return "expired";
  return "other";
}

function orderStateBucketLabel(bucket: OrderStateBucket) {
  if (bucket === "live") return "当前委托";
  if (bucket === "filled") return "已成交";
  if (bucket === "canceled") return "已撤销";
  if (bucket === "rejected") return "拒单";
  if (bucket === "expired") return "已过期";
  return "其他状态";
}

function orderStateReview(bucket: OrderStateBucket) {
  if (bucket === "live") {
    return {
      scope: "new / partially_filled，仍可能影响盘口、冻结资金、合约保证金或机器人挂单。",
      verificationPath: "订单与成交核对订单状态；现货冻结回账户与资金，合约保证金占用回合约清算保证金账户。",
      boundary: "本页只做查单和导出；撤单、停机撤单或盘口清理仍回交易页、机器人运营或市场运营并保留确认。",
    };
  }
  if (bucket === "filled") {
    return {
      scope: "filled，已产生成交、手续费和可能的合约 realized PnL。",
      verificationPath: "成交审计核对 trade id、taker/maker、手续费和来源；合约 PnL、资金费和强平关联回合约清算。",
      boundary: "已成交订单不在审计页做回滚或账务修正；资金和账本 source-of-truth 仍在对应账本域。",
    };
  }
  if (bucket === "canceled") {
    return {
      scope: "canceled / cancelled，可能来自用户撤单、机器人停机撤单、市场撤挂单或系统维护。",
      verificationPath: "先核对订单更新时间和操作记录；若来自机器人或市场维护，再回机器人运营、市场运营或系统与审计。",
      boundary: "撤销事实只说明订单不再 live，不证明冻结资金或保证金异常已完全恢复；资金核对回账户/合约账本。",
    };
  }
  if (bucket === "rejected") {
    return {
      scope: "rejected，通常来自余额不足、价格/数量规则、合约交易模式、reduce-only 或风控拒绝。",
      verificationPath: "看 reject_reason、市场规则和产品参数；PERP 拒单回合约清算风险参数和市场运营产品配置核对。",
      boundary: "拒单不生成成交；不要在订单审计页补单或改状态，先确认规则、余额、保证金和机器人参数。",
    };
  }
  if (bucket === "expired") {
    return {
      scope: "expired，常见于 IOC/FOK 或时效规则未完全成交。",
      verificationPath: "核对 TIF、剩余数量、成交审计和市场流动性；机器人相关过期回机器人运营日志排查。",
      boundary: "过期订单不代表撮合异常；只有和盘口、成交或系统一致性异常同时出现时才升级处理。",
    };
  }
  return {
    scope: "后端返回的非标准或新增订单状态。",
    verificationPath: "先保留订单号、状态和原始筛选条件，再核对系统与审计、后端日志和对应产品域。",
    boundary: "未知状态只做观察和升级线索，不在前端审计页直接修改订单或账本。",
  };
}

function orderStatusVerificationPath(order: OrderItem) {
  const review = orderStateReview(orderStateBucket(order.status));
  if (order.product_type === "PERP") return `${review.verificationPath} 合约订单深层核对回合约清算 -> 订单与成交。`;
  if (order.product_type === "SPOT") return `${review.verificationPath} 现货订单深层核对回账户与资金和市场运营。`;
  return review.verificationPath;
}

function orderStatusBoundaryNote(order: OrderItem) {
  const review = orderStateReview(orderStateBucket(order.status));
  return `${review.boundary} 当前 CSV 是已加载审计结果，不是全量订单生命周期报表。`;
}

type TradeSourceBucket = "customer" | "robot" | "flow" | "system" | "mixed" | "unknown";

const tradeSourceBuckets: TradeSourceBucket[] = ["customer", "robot", "flow", "system", "mixed", "unknown"];

const orderAuditDataScopes: OrderAuditDataScope[] = ["all", "customer", "robot", "system"];

function auditProductScopeFromParam(value: string): AuditProductScope {
  const normalized = value.trim().toUpperCase();
  if (normalized === "SPOT" || normalized === "PERP") return normalized;
  return "all";
}

function auditStatusScopeFromParam(value: string): AuditStatusScope {
  const normalized = value.trim().toLowerCase();
  if (normalized === "open") return "live";
  if (normalized === "all" || normalized === "live" || normalized === "new" || normalized === "partially_filled" || normalized === "filled" || normalized === "canceled" || normalized === "rejected") return normalized;
  return "live";
}

function orderAuditDataScopeFromParam(value: string): OrderAuditDataScope {
  const normalized = value.trim().toLowerCase();
  if (normalized === "customer" || normalized === "robot" || normalized === "system") return normalized;
  return "all";
}

function hasOrderAuditParams(params: URLSearchParams) {
  return params.has("auditProduct")
    || params.has("auditSymbol")
    || params.has("auditUserId")
    || params.has("auditStatus")
    || params.has("auditDomain")
    || params.has("auditDataScope")
    || params.has("dataDomain");
}

function adminSectionFromSearch(search: string): AdminSection {
  const params = new URLSearchParams(search);
  const section = (params.get("section") || "").trim() as AdminSection;
  if (adminSectionKeys.includes(section)) return section;
  const contractTab = (params.get("contractTab") || "").trim() as ContractAdminTab;
  if (contractAdminTabKeys.includes(contractTab)) return "contracts";
  if (params.has("contractAccountScope") || params.has("contractScope")) return "contracts";
  const systemTab = (params.get("systemTab") || "").trim() as SystemAdminTab;
  if (systemAdminTabKeys.includes(systemTab) || hasAdminOperationFilterParams(params)) return "system";
  if (hasOrderAuditParams(params)) return "orders";
  if ((params.get("userId") || "").trim()) return "accounts";
  if ((params.get("market") || params.get("symbol") || params.get("marketTab") || params.get("tab") || "").trim()) return "markets";
  return "overview";
}

function marketSymbolFromSearch(search: string) {
  const params = new URLSearchParams(search);
  return normalizeMarketSymbolInput(params.get("market") || params.get("symbol") || "");
}

function marketDetailTabFromSearch(search: string): MarketDetailTab {
  const params = new URLSearchParams(search);
  const tab = (params.get("marketTab") || params.get("tab") || "").trim() as MarketDetailTab;
  return marketDetailTabKeys.includes(tab) ? tab : "overview";
}

function contractAdminTabFromSearch(search: string): ContractAdminTab {
  const params = new URLSearchParams(search);
  const tab = (params.get("contractTab") || "").trim() as ContractAdminTab;
  return contractAdminTabKeys.includes(tab) ? tab : "overview";
}

function systemAdminTabFromSearch(search: string): SystemAdminTab {
  const params = new URLSearchParams(search);
  const tab = (params.get("systemTab") || "").trim() as SystemAdminTab;
  return systemAdminTabKeys.includes(tab) ? tab : "overview";
}

function userIdFromSearch(search: string) {
  const raw = (new URLSearchParams(search).get("userId") || "").trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function orderAuditDataScopeLabel(scope: OrderAuditDataScope) {
  if (scope === "customer") return "客户记录";
  if (scope === "robot") return "机器人对账";
  if (scope === "system") return "系统控制 / 未知";
  return "全部数据域";
}

function auditProductFromProductType(productType?: string | null): AuditProductScope {
  if (productType === "SPOT" || productType === "PERP") return productType;
  return "all";
}

function orderAuditDataScopeHint(scope: OrderAuditDataScope) {
  if (scope === "customer") return "普通外部客户业务事实";
  if (scope === "robot") return "MM / FLOW / 机器人 UID";
  if (scope === "system") return "管理主体、系统主体、seed 或未知来源";
  return "当前已加载结果";
}

function orderAuditDataScopeForUser(user: { username: string; role: string }): Exclude<OrderAuditDataScope, "all"> {
  const kind = accountUserKindForUser(user);
  if (kind === "customer") return "customer";
  if (kind === "spot_robot" || kind === "contract_robot") return "robot";
  return "system";
}

function orderAuditDataScopeForAccountScope(scope: AccountUserScope): OrderAuditDataScope {
  if (scope === "customer") return "customer";
  if (scope === "robot" || scope === "spot_robot" || scope === "contract_robot") return "robot";
  if (scope === "admin" || scope === "system") return "system";
  return "all";
}

function tradeSourceBucket(trade: TradeItem): TradeSourceBucket {
  const source = String(trade.source ?? "").toLowerCase();
  if (source.includes("flow")) return "flow";
  if (source === "bot" || source.includes("mm") || source.includes("robot")) return "robot";
  if (source === "bootstrap_seed" || source.includes("seed") || source.includes("fallback")) return "system";
  if (source === "mixed") return "mixed";
  if (source === "user" || source === "match") return "customer";
  return "unknown";
}

function orderAuditDataScopeForTrade(item: AdminTradeAuditItem): Exclude<OrderAuditDataScope, "all"> {
  const bucket = tradeSourceBucket(item.trade);
  if (bucket === "customer") return "customer";
  if (bucket === "robot" || bucket === "flow") return "robot";
  const takerScope = orderAuditDataScopeForUser(item.taker_user);
  const makerScope = orderAuditDataScopeForUser(item.maker_user);
  if (takerScope === "robot" || makerScope === "robot") return "robot";
  if (takerScope === "customer" && makerScope === "customer") return "customer";
  return "system";
}

function orderAuditItemMatchesDataScope(item: AdminOrderAuditItem, scope: OrderAuditDataScope) {
  return scope === "all" || orderAuditDataScopeForUser(item.user) === scope;
}

function tradeAuditItemMatchesDataScope(item: AdminTradeAuditItem, scope: OrderAuditDataScope) {
  return scope === "all" || orderAuditDataScopeForTrade(item) === scope;
}

function tradeSourceBucketLabel(bucket: TradeSourceBucket) {
  if (bucket === "customer") return "客户 / 普通撮合";
  if (bucket === "robot") return "做市机器人";
  if (bucket === "flow") return "FLOW 主动流";
  if (bucket === "system") return "系统流动性";
  if (bucket === "mixed") return "混合来源";
  return "未知来源";
}

function tradeSourceReview(bucket: TradeSourceBucket) {
  if (bucket === "customer") {
    return {
      scope: "`user / match`，通常是普通客户与订单簿自然撮合。",
      verificationPath: "订单与成交核对 taker/maker、订单号、价格数量和手续费；账户资金回账户与资金或合约清算核对。",
      boundary: "成交事实不能单独证明余额、保证金或 PnL 已完全符合预期，资金侧仍以账本为准。",
    };
  }
  if (bucket === "robot") {
    return {
      scope: "`bot`，通常由 MM / PERP_MM 挂单或机器人 UID / 账号参与形成。",
      verificationPath: "机器人运营核对策略、实例、日志和账号绑定；账户与资金或合约清算核对资金占用和流水。",
      boundary: "成交来源只说明机器人参与，不说明策略参数正确，也不替代实例日志和机器人账号核对。",
    };
  }
  if (bucket === "flow") {
    return {
      scope: "`flow`，来自本地沙盒 FLOW 主动小额 IOC，进入撮合、账本和 K 线。",
      verificationPath: "机器人运营核对 FLOW 队列、guard、source policy 和日志；成交审计核对 trade id，账本域核对费用和资金变化。",
      boundary: "FLOW 真实 IOC 与虚拟成交口径不同；虚拟成交不应进入普通成交账本。",
    };
  }
  if (bucket === "system") {
    return {
      scope: "`bootstrap_seed / seed / fallback`，系统流动性、初始化或兜底盘口相关成交。",
      verificationPath: "市场运营和系统与审计核对铺盘/兜底操作记录；合约系统主体回合约清算核对保证金和订单。",
      boundary: "系统流动性成交不应被误读为真实客户活跃度，交接时要标明来源。",
    };
  }
  if (bucket === "mixed") {
    return {
      scope: "`mixed`，同一聚合窗口内存在多来源成交。",
      verificationPath: "拆回成交明细和 K 线 source_counts/source_volumes，再分别核对客户、机器人、FLOW 或系统流动性。",
      boundary: "混合来源不能直接用于判断单一策略表现或客户成交质量。",
    };
  }
  return {
    scope: "未知、空值或后续新增来源。",
    verificationPath: "保留 trade id、订单号和筛选条件，回订单与成交、系统与审计和后端日志确认来源写入。",
    boundary: "未知来源只做排查线索，不在审计页修改成交来源或账本。",
  };
}

function tradeSourceVerificationPath(trade: TradeItem) {
  const review = tradeSourceReview(tradeSourceBucket(trade));
  if (trade.product_type === "PERP") return `${review.verificationPath} PERP 成交费用、realized PnL 和保证金变化回合约清算核对。`;
  if (trade.product_type === "SPOT") return `${review.verificationPath} SPOT 成交费用和余额变化回账户与资金核对。`;
  return review.verificationPath;
}

function tradeFeePnlVerificationPath(trade: TradeItem) {
  if (trade.product_type === "PERP") {
    return "合约成交费用、taker/maker realized PnL、持仓和合约保证金流水回合约清算核对。";
  }
  if (trade.product_type === "SPOT") {
    return "现货手续费、余额增减、冻结释放和现货流水回账户与资金核对。";
  }
  return "按产品类型回账户与资金或合约清算核对费用、PnL 和账本流水。";
}

function tradeBoundaryNote(trade: TradeItem) {
  const review = tradeSourceReview(tradeSourceBucket(trade));
  return `${review.boundary} 当前 CSV 是已加载成交审计结果，不是全量成交报表或日终对账包。`;
}

type ContractClearingOrderTradeBucket = "live_margin" | "filled_settlement" | "close_reduce" | "rejected_canceled" | "robot_system" | "unknown";

const contractClearingOrderTradeBuckets: ContractClearingOrderTradeBucket[] = [
  "live_margin",
  "filled_settlement",
  "close_reduce",
  "rejected_canceled",
  "robot_system",
  "unknown",
];

function contractClearingOrderTradeBucketLabel(bucket: ContractClearingOrderTradeBucket) {
  if (bucket === "live_margin") return "当前委托 / 保证金";
  if (bucket === "filled_settlement") return "成交费用 / PnL";
  if (bucket === "close_reduce") return "含减仓 / 只减仓";
  if (bucket === "rejected_canceled") return "拒单 / 撤单";
  if (bucket === "robot_system") return "机器人 / 系统单据";
  return "未知单据";
}

function contractOrderIsCloseReduce(order: OrderItem) {
  return order.reduce_only || String(order.position_action ?? "").toLowerCase() === "close";
}

function contractTradeIsCloseReduce(trade: TradeItem) {
  return [trade.position_action, trade.taker_position_action, trade.maker_position_action]
    .some((action) => ["close", "reverse"].includes(String(action ?? "").toLowerCase()));
}

function contractOrderUserIsRobotOrSystem(user: ContractAdminUser) {
  return contractAccountKindForUser(user) !== "customer";
}

function contractTradeIsRobotOrSystem(trade: TradeItem) {
  const bucket = tradeSourceBucket(trade);
  return bucket === "robot" || bucket === "flow" || bucket === "system" || bucket === "mixed";
}

function contractOrderClearingBucket(order: OrderItem, user: ContractAdminUser): ContractClearingOrderTradeBucket {
  const stateBucket = orderStateBucket(order.status);
  if (stateBucket === "live") return "live_margin";
  if (contractOrderIsCloseReduce(order)) return "close_reduce";
  if (stateBucket === "filled") return "filled_settlement";
  if (stateBucket === "canceled" || stateBucket === "rejected" || stateBucket === "expired") return "rejected_canceled";
  if (contractOrderUserIsRobotOrSystem(user)) return "robot_system";
  return "unknown";
}

function contractTradeClearingBucket(trade: TradeItem): ContractClearingOrderTradeBucket {
  if (contractTradeIsCloseReduce(trade)) return "close_reduce";
  if (contractTradeIsRobotOrSystem(trade)) return "robot_system";
  if (tradeSourceBucket(trade) === "unknown") return "unknown";
  return "filled_settlement";
}

function contractClearingOrderTradeReview(bucket: ContractClearingOrderTradeBucket) {
  if (bucket === "live_margin") {
    return {
      scope: "new / partially_filled 合约委托，仍可能占用保证金、影响盘口、仓位开口或机器人库存。",
      verificationPath: "核对合约委托剩余量、保证金流水 margin_reserve / margin_release、仓位风险、交易模式和机器人挂单。",
      boundary: "这里只做只读清算核对；撤单、释放保证金、停机撤单或市场清理仍回交易页、机器人运营或市场运营并保留确认。",
    };
  }
  if (bucket === "filled_settlement") {
    return {
      scope: "合约成交、手续费、taker/maker realized PnL 和可能的仓位结算。",
      verificationPath: "核对 trade id、订单号、成交价量、trade_fee / position_close 保证金流水、仓位已实现 PnL 和账户 total_fees。",
      boundary: "成交记录不在前端回滚或重算费用；手续费、PnL、保证金和仓位仍以撮合及合约账本落账为准。",
    };
  }
  if (bucket === "close_reduce") {
    return {
      scope: "包含平仓、只减仓及减仓后反手成交；reverse 同时含新增风险，不能视为只减仓。",
      verificationPath: "核对 reduce_only / position_action、成交侧 taker/maker close/reverse、仓位变化、保证金释放和风险参数中的只减仓模式。",
      boundary: "该分组不是强制平仓或 ADL 入口；异常平仓、强平和自动减仓仍回强平 / ADL 页处理。",
    };
  }
  if (bucket === "rejected_canceled") {
    return {
      scope: "rejected / canceled / expired 合约委托，常见于余额、保证金、交易模式、TIF、价格数量规则或机器人撤单。",
      verificationPath: "先看 reject_reason、订单更新时间和操作记录，再回市场运营产品配置、风险参数、机器人运营或系统与审计核对。",
      boundary: "拒单和撤单不在这里补单、改状态或手工修账；先确认规则、保证金、机器人动作和清算链路。",
    };
  }
  if (bucket === "robot_system") {
    return {
      scope: "SPOT 机器人 UID、PERP 机器人 UID、FLOW 或系统流动性参与的合约单据。",
      verificationPath: "回机器人运营、机器人账号、FLOW 队列、系统与审计和合约系统主体核对来源、策略和资金占用。",
      boundary: "机器人或系统控制来源不能被当作普通客户活跃度；合约保证金账户归属只做运营识别，不合并现货钱包和合约保证金。",
    };
  }
  return {
    scope: "后端新增状态、未知来源或当前页面无法归类的合约单据。",
    verificationPath: "保留 order_id / trade_id、状态、来源和筛选条件，回系统与审计、后端日志和对应清算链路确认语义。",
    boundary: "未知单据只做升级线索，不在前端页面修改订单、成交、账本、仓位、保证金或来源字段。",
  };
}

function contractClearingOrderTradeVerificationPath(bucket: ContractClearingOrderTradeBucket) {
  return contractClearingOrderTradeReview(bucket).verificationPath;
}

function contractClearingOrderTradeBoundaryNote(bucket: ContractClearingOrderTradeBucket) {
  const review = contractClearingOrderTradeReview(bucket);
  return `${review.boundary} 当前 CSV 是已加载合约清算局部订单/成交结果，不是全量订单生命周期或日终对账包。`;
}

type PositionRiskBucket = "liquidation" | "warning" | "watch" | "normal" | "flat" | "unknown";

const positionRiskBuckets: PositionRiskBucket[] = ["liquidation", "warning", "watch", "normal", "flat", "unknown"];

function positionRiskBucket(position: ContractPosition): PositionRiskBucket {
  const status = String(position.risk_status ?? "").toLowerCase();
  const quantity = Number(position.quantity || 0);
  const distance = Number(position.liquidation_distance_pct);
  if (status.includes("liquidation") || (Number.isFinite(distance) && distance <= 0.02)) return "liquidation";
  if (status.includes("warning") || (Number.isFinite(distance) && distance <= 0.05)) return "warning";
  if (status.includes("watch") || (Number.isFinite(distance) && distance <= 0.15)) return "watch";
  if (status === "flat" || position.side === "flat" || quantity <= 0) return "flat";
  if (status === "ok") return "normal";
  return "unknown";
}

function positionRiskBucketLabel(bucket: PositionRiskBucket) {
  if (bucket === "liquidation") return "临近强平";
  if (bucket === "warning") return "高风险";
  if (bucket === "watch") return "观察";
  if (bucket === "normal") return "正常";
  if (bucket === "flat") return "空仓";
  return "未知";
}

function positionRiskReview(bucket: PositionRiskBucket) {
  if (bucket === "liquidation") {
    return {
      scope: "`liquidation_due` 或距强平约 <= 2%，优先核对标记价、强平价、保证金缓冲和维护告警。",
      verificationPath: "先看仓位卡片，再核对合约保证金流水、强平 / ADL、保险基金和风险参数。",
      boundary: "该分组只做运营优先级提示；是否强平、ADL 或坏账处理仍以后端清算逻辑为准。",
    };
  }
  if (bucket === "warning") {
    return {
      scope: "`warning` 或距强平约 <= 5%，通常需要确认价格源、风险阶梯和账户保证金是否合理。",
      verificationPath: "核对标记价、风险阶梯、保证金账户、最近委托和资金费结算，必要时回风险监控排队处理。",
      boundary: "不要在仓位风险页直接补保证金、改杠杆或改状态；资金动作必须回明确业务入口并保留确认边界。",
    };
  }
  if (bucket === "watch") {
    return {
      scope: "`watch` 或距强平约 <= 15%，属于观察和交接范围。",
      verificationPath: "关注保证金缓冲、未实现 PnL、资金费和未成交委托；机器人仓位还要回机器人运营看策略状态。",
      boundary: "观察分组不等于立即处置，只有和价格源、保证金、订单或机器人异常同时出现时升级。",
    };
  }
  if (bucket === "normal") {
    return {
      scope: "`ok` 且仍有活跃持仓，当前未触发前端运营关注阈值。",
      verificationPath: "常规核对持仓、保证金、成交和资金费；日终或专题对账仍回账本域和后端报表。",
      boundary: "正常只代表当前已加载快照无明显风险，不代表历史账本、全量仓位或后续价格变化无风险。",
    };
  }
  if (bucket === "flat") {
    return {
      scope: "`flat` 或数量为 0，当前没有需要核对的活跃合约敞口。",
      verificationPath: "如需复盘，回合约成交、保证金流水和历史强平 / ADL 事件核对。",
      boundary: "空仓不代表历史 PnL、手续费或资金费已完全结清，资金结论仍以账本为准。",
    };
  }
  return {
    scope: "后端新增或空缺风险状态，前端暂时无法归入标准分级。",
    verificationPath: "保留用户、市场、方向、risk_status 和距离强平字段，回系统与审计和后端日志确认序列化口径。",
    boundary: "未知状态只做升级线索，不在前端页面修改仓位、保证金或清算结果。",
  };
}

function positionRiskVerificationPath(position: ContractPosition) {
  const review = positionRiskReview(positionRiskBucket(position));
  if (position.product_type === "PERP") return `${review.verificationPath} 当前市场 ${position.symbol} 属于 PERP 清算域。`;
  return review.verificationPath;
}

function positionRiskBoundaryNote(position: ContractPosition) {
  const review = positionRiskReview(positionRiskBucket(position));
  return `${review.boundary} 当前 CSV 是已加载仓位风险快照，不是全量历史仓位报表。`;
}

type FundingOpsBucket = "failed" | "running" | "pending" | "settled" | "account_record" | "unknown";

const fundingOpsBuckets: FundingOpsBucket[] = ["failed", "running", "pending", "settled", "account_record", "unknown"];

function fundingOpsBucket(status?: string | null, recordKind: "job" | "settlement" | "event" = "job"): FundingOpsBucket {
  if (recordKind === "event") return "account_record";
  const normalized = String(status ?? "").toLowerCase();
  if (normalized === "failed" || normalized === "error") return "failed";
  if (normalized === "running" || normalized === "processing" || normalized === "locked") return "running";
  if (normalized === "pending" || normalized === "queued" || normalized === "retrying") return "pending";
  if (normalized === "settled" || normalized === "completed" || normalized === "succeeded" || normalized === "success") return "settled";
  return "unknown";
}

function fundingOpsBucketLabel(bucket: FundingOpsBucket) {
  if (bucket === "failed") return "失败任务";
  if (bucket === "running") return "运行中任务";
  if (bucket === "pending") return "待结算任务";
  if (bucket === "settled") return "已结算周期";
  if (bucket === "account_record") return "逐账户记录";
  return "未知状态";
}

function fundingOpsReview(bucket: FundingOpsBucket) {
  if (bucket === "failed") {
    return {
      scope: "`failed / error` 资金费任务或结算，通常需要先看错误、是否重复结算和价格源状态。",
      verificationPath: "先核对任务 last_error、结算水位、资金费事件和合约保证金流水；必要时回系统与审计看资金费失败操作记录。",
      boundary: "重试和手动结算都属于危险写入，必须保留前端确认和后端 confirm_execute；不要只凭失败状态反复重放。",
    };
  }
  if (bucket === "running") {
    return {
      scope: "`running / processing / locked`，表示已有 worker 或维护流程正在处理资金费。",
      verificationPath: "核对 locked_by、heartbeat、更新时间和 worker 状态；若心跳停滞，再回系统与审计和后端日志判断是否需要恢复。",
      boundary: "运行中不等于失败；不要和批量重试混用，避免同一 funding time 被并发处理。",
    };
  }
  if (bucket === "pending") {
    return {
      scope: "`pending / queued / retrying`，表示待处理或等待重试窗口的资金费任务。",
      verificationPath: "核对 funding_time、next_retry_at、标记价/指数价和资金费模式，确认是否只是未到处理窗口。",
      boundary: "待结算不是异常；只有超过窗口、价格源异常或重试耗尽时才升级到风险监控或系统排查。",
    };
  }
  if (bucket === "settled") {
    return {
      scope: "`settled / completed / succeeded` 资金费周期，表示当前周期已形成结算水位。",
      verificationPath: "核对 settlement_id、settled_count、total_amount、逐账户资金费记录和合约流水。",
      boundary: "已结算只证明该周期已有结算记录，不等于全量账户对账或日终账本核对完成。",
    };
  }
  if (bucket === "account_record") {
    return {
      scope: "逐账户资金费事件，记录用户方向、数量、费率、指数价/标记价和资金费金额。",
      verificationPath: "按 user_id、event_id 回合约保证金流水核对钱包、可用保证金、占用保证金和 realized/unrealized 影响。",
      boundary: "逐账户记录是资金费结果，不是手动冲正入口；异常修复必须回明确账本/清算专题处理。",
    };
  }
  return {
    scope: "后端新增、空缺或非标准资金费状态。",
    verificationPath: "保留 job_id、settlement_id、funding_time、status 和筛选条件，回系统与审计和后端日志确认状态语义。",
    boundary: "未知状态只做升级线索，不在前端页面修改资金费任务、结算水位或用户账本。",
  };
}

function fundingOpsVerificationPath(bucket: FundingOpsBucket) {
  return fundingOpsReview(bucket).verificationPath;
}

function fundingOpsBoundaryNote(bucket: FundingOpsBucket) {
  const review = fundingOpsReview(bucket);
  return `${review.boundary} 当前 CSV 是已加载资金费当前结果，不是全量资金费对账包。`;
}

type ClearingEventBucket = "adl_due" | "residual_bad_debt" | "insurance_covered" | "adl_executed" | "settled_no_debt" | "unknown";

const clearingEventBuckets: ClearingEventBucket[] = ["adl_due", "residual_bad_debt", "insurance_covered", "adl_executed", "settled_no_debt", "unknown"];

function clearingEventBucketFromLiquidation(event: ContractLiquidationEvent): ClearingEventBucket {
  const adlResidual = Number(event.adl_residual || 0);
  const residualBadDebt = Number(event.residual_bad_debt || 0);
  const insuranceCovered = Number(event.insurance_covered || 0);
  const adlCovered = Number(event.adl_covered || 0);
  const status = String(event.adl_status ?? "").toLowerCase();
  if (adlResidual > 0 || status === "pending" || status === "partial") return "adl_due";
  if (residualBadDebt > 0 && adlCovered <= 0) return "residual_bad_debt";
  if (adlCovered > 0 || status === "executed" || status === "completed" || status === "settled") return "adl_executed";
  if (insuranceCovered > 0) return "insurance_covered";
  if (Number.isFinite(residualBadDebt) && residualBadDebt <= 0 && Number.isFinite(adlResidual) && adlResidual <= 0) return "settled_no_debt";
  return "unknown";
}

function clearingEventBucketFromAdl(event: ContractAdlEvent): ClearingEventBucket {
  const residualAfter = Number(event.residual_after || 0);
  const covered = Number(event.covered_amount || 0);
  const status = String(event.status ?? "").toLowerCase();
  if (residualAfter > 0) return "residual_bad_debt";
  if (covered > 0 || status === "executed" || status === "completed" || status === "settled") return "adl_executed";
  return status ? "unknown" : "adl_executed";
}

function clearingEventBucketLabel(bucket: ClearingEventBucket) {
  if (bucket === "adl_due") return "待 ADL 处理";
  if (bucket === "residual_bad_debt") return "残余坏账";
  if (bucket === "insurance_covered") return "保险覆盖";
  if (bucket === "adl_executed") return "ADL 已执行";
  if (bucket === "settled_no_debt") return "无坏账事件";
  return "未知状态";
}

function clearingEventReview(bucket: ClearingEventBucket) {
  if (bucket === "adl_due") {
    return {
      scope: "强平后仍有 `adl_residual > 0`，或 ADL 状态为 `pending / partial`。",
      verificationPath: "先核对强平事件、残余坏账、保险覆盖、ADL 候选排序和保险基金余额，再看操作记录。",
      boundary: "执行 ADL 是危险写入，必须逐事件确认并通过后端 confirm_execute；不要在摘要或导出里快捷处理。",
    };
  }
  if (bucket === "residual_bad_debt") {
    return {
      scope: "仍存在未覆盖坏账或 ADL 执行后仍有 residual_after。",
      verificationPath: "核对强平事件、ADL 历史、保险基金流水和合约保证金流水，确认剩余坏账归属。",
      boundary: "残余坏账不是普通亏损展示，不能在前端表格里改数；人工接管或冲正必须另开清算/账本专题。",
    };
  }
  if (bucket === "insurance_covered") {
    return {
      scope: "强平亏损已由保险基金覆盖，当前无 ADL 剩余。",
      verificationPath: "核对保险覆盖金额、保险基金流水、关联强平事件和用户合约流水。",
      boundary: "保险覆盖只说明本次清算有基金介入，不代表保险基金余额、坏账归档或日终对账完成。",
    };
  }
  if (bucket === "adl_executed") {
    return {
      scope: "ADL 自动减仓记录或强平事件已记录 ADL 覆盖。",
      verificationPath: "核对 ADL 事件、关联强平、被减仓账户、执行价、覆盖坏账和剩余坏账。",
      boundary: "ADL 结果不能单独证明所有仓位和账本已对齐，仍需回合约仓位、保证金流水和保险基金流水核对。",
    };
  }
  if (bucket === "settled_no_debt") {
    return {
      scope: "强平事件没有残余坏账，也没有 ADL 剩余。",
      verificationPath: "常规核对强平价、破产价、释放保证金、已实现 PnL 和用户合约流水。",
      boundary: "无坏账只代表当前强平事件闭合，不代表历史对账、保险基金或全量仓位没有风险。",
    };
  }
  return {
    scope: "后端新增、空缺或非标准清算 / ADL 状态。",
    verificationPath: "保留 event_id、liquidation_event_id、adl_status、status 和筛选条件，回系统与审计和后端日志确认状态语义。",
    boundary: "未知状态只做升级线索，不在前端页面修改强平、ADL、保险基金或用户账本。",
  };
}

function clearingEventVerificationPath(bucket: ClearingEventBucket) {
  return clearingEventReview(bucket).verificationPath;
}

function clearingEventBoundaryNote(bucket: ClearingEventBucket) {
  const review = clearingEventReview(bucket);
  return `${review.boundary} 当前 CSV 是已加载清算事件当前结果，不是全量清算对账包。`;
}

type InsuranceEventBucket = "residual_bad_debt" | "fund_covered" | "manual_increase" | "manual_decrease" | "balance_neutral" | "unknown";

const insuranceEventBuckets: InsuranceEventBucket[] = ["residual_bad_debt", "fund_covered", "manual_increase", "manual_decrease", "balance_neutral", "unknown"];

function insuranceEventBucket(event: ContractInsuranceEvent): InsuranceEventBucket {
  const eventType = String(event.event_type ?? "").toLowerCase();
  const amount = Number(event.amount || 0);
  const residual = Number(event.residual_bad_debt || 0);
  if (residual > 0 || eventType === "bad_debt_uncovered") return "residual_bad_debt";
  if (eventType === "bad_debt_cover" || amount < 0) return "fund_covered";
  if (eventType === "admin_adjustment" && amount > 0) return "manual_increase";
  if (eventType === "admin_adjustment" && amount < 0) return "manual_decrease";
  if (Number.isFinite(amount) && amount === 0) return "balance_neutral";
  return "unknown";
}

function insuranceEventBucketLabel(bucket: InsuranceEventBucket) {
  if (bucket === "residual_bad_debt") return "待处理坏账";
  if (bucket === "fund_covered") return "保险覆盖";
  if (bucket === "manual_increase") return "人工注入";
  if (bucket === "manual_decrease") return "人工扣减";
  if (bucket === "balance_neutral") return "零额记录";
  return "未知类型";
}

function insuranceEventReview(bucket: InsuranceEventBucket) {
  if (bucket === "residual_bad_debt") {
    return {
      scope: "`residual_bad_debt > 0` 或 `bad_debt_uncovered`，表示保险基金未完全覆盖强平亏穿。",
      verificationPath: "先核对关联强平事件、基金余额、保险基金流水、ADL 历史和合约保证金流水，确认剩余坏账归属。",
      boundary: "残余坏账不是普通基金流水，不在前端表格里改数；ADL、人工接管或冲正必须另走清算/账本专题。",
    };
  }
  if (bucket === "fund_covered") {
    return {
      scope: "`bad_debt_cover` 或基金负向扣减，表示保险基金已用于覆盖强平亏穿。",
      verificationPath: "核对基金扣减金额、余额前后值、关联强平事件、用户合约流水和本次清算结果。",
      boundary: "保险覆盖记录只说明基金介入本次事件，不代表日终对账、坏账归档或全部仓位风险已闭合。",
    };
  }
  if (bucket === "manual_increase") {
    return {
      scope: "`admin_adjustment` 且金额为正，表示管理员手动增加保险基金余额。",
      verificationPath: "回系统与审计操作记录核对执行人、原因、金额、余额前后值和备注，再核对基金余额。",
      boundary: "人工注入是危险写入，必须保留前端确认、后端 confirm_execute 和操作记录；不能替代资金来源凭证。",
    };
  }
  if (bucket === "manual_decrease") {
    return {
      scope: "`admin_adjustment` 且金额为负，表示管理员手动扣减保险基金余额。",
      verificationPath: "回系统与审计操作记录核对执行人、原因、金额、余额前后值、备注和关联清算背景。",
      boundary: "人工扣减会改写清算基金余额，不能作为坏账自动处理；缺少审批/日终对账时只能视为本机维护记录。",
    };
  }
  if (bucket === "balance_neutral") {
    return {
      scope: "金额为 0 且无残余坏账，通常是占位、兼容或无余额变化记录。",
      verificationPath: "保留 event_id、event_type、note 和筛选条件，回后端日志或系统与审计确认该类型语义。",
      boundary: "零额记录不能证明基金无风险，也不能证明关联强平或 ADL 已完成对账。",
    };
  }
  return {
    scope: "后端新增、空缺或非标准保险基金事件类型。",
    verificationPath: "保留 event_id、event_type、amount、residual_bad_debt 和 note，回系统与审计和后端日志确认状态语义。",
    boundary: "未知类型只做升级线索，不在前端页面修改保险基金、坏账、强平、ADL 或用户账本。",
  };
}

function insuranceEventVerificationPath(bucket: InsuranceEventBucket) {
  return insuranceEventReview(bucket).verificationPath;
}

function insuranceEventBoundaryNote(bucket: InsuranceEventBucket) {
  const review = insuranceEventReview(bucket);
  return `${review.boundary} 当前 CSV 是已加载保险基金流水当前结果，不是全量基金对账包。`;
}

type MarginLedgerBucket = "initialization" | "admin_maintenance" | "margin_lifecycle" | "trade_settlement" | "trade_fee" | "funding_fee" | "clearing" | "unknown";

const marginLedgerBuckets: MarginLedgerBucket[] = ["initialization", "admin_maintenance", "margin_lifecycle", "trade_settlement", "trade_fee", "funding_fee", "clearing", "unknown"];

function marginLedgerBucket(entry: ContractLedgerItem): MarginLedgerBucket {
  const changeType = String(entry.change_type ?? "").toLowerCase();
  if (["account_init", "deposit_reset"].includes(changeType)) return "initialization";
  if (["admin_adjust", "admin_demo_reset"].includes(changeType)) return "admin_maintenance";
  if (["margin_reserve", "margin_release", "margin_settle"].includes(changeType)) return "margin_lifecycle";
  if (["position_close"].includes(changeType)) return "trade_settlement";
  if (["trade_fee"].includes(changeType)) return "trade_fee";
  if (["funding_fee"].includes(changeType)) return "funding_fee";
  if (["liquidation", "adl_deleverage"].includes(changeType)) return "clearing";
  return "unknown";
}

function marginLedgerBucketLabel(bucket: MarginLedgerBucket) {
  if (bucket === "initialization") return "初始化 / 重置";
  if (bucket === "admin_maintenance") return "人工维护";
  if (bucket === "margin_lifecycle") return "保证金锁定/释放";
  if (bucket === "trade_settlement") return "开平仓结算";
  if (bucket === "trade_fee") return "交易手续费";
  if (bucket === "funding_fee") return "资金费";
  if (bucket === "clearing") return "强平 / ADL";
  return "未知类型";
}

function marginLedgerReview(bucket: MarginLedgerBucket) {
  if (bucket === "initialization") {
    return {
      scope: "账户创建、演示资金初始化或兼容重置写入的合约保证金账户首笔记录。",
      verificationPath: "核对用户创建、机器人 seed、默认合约保证金钱包、UID 摘要和同一用户现货账本边界。",
      boundary: "初始化记录不代表统一账户入金凭证；现货钱包和合约保证金仍是两套账本，不在这里合并。",
    };
  }
  if (bucket === "admin_maintenance") {
    return {
      scope: "管理员调整、演示重置或机器人维护产生的合约保证金账本变化。",
      verificationPath: "回系统与审计、演示重置入口、机器人配置和操作备注核对执行背景、金额和余额前后值。",
      boundary: "人工维护是危险写入，必须保留独立入口、确认提示和后端确认参数；不能和普通查看混在一起。",
    };
  }
  if (bucket === "margin_lifecycle") {
    return {
      scope: "委托开仓时初始保证金锁定、成交后占用结算、撤单或剩余数量释放。",
      verificationPath: "核对关联 order_id、订单状态、成交数量、杠杆、合约仓位和 used_margin 前后值。",
      boundary: "该类记录主要解释保证金占用变化，金额可能为 0；不能单独证明钱包现金流变化。",
    };
  }
  if (bucket === "trade_settlement") {
    return {
      scope: "平仓成交带来的已实现 PnL 和保证金释放。",
      verificationPath: "核对关联 trade_id、order_id、成交价、仓位方向、realized_pnl、wallet 和 used_margin 前后值。",
      boundary: "平仓结算只覆盖本笔成交，不替代全量仓位、手续费、资金费和日终对账。",
    };
  }
  if (bucket === "trade_fee") {
    return {
      scope: "合约成交手续费扣减及 total_fees 累计。",
      verificationPath: "核对关联成交、订单、费率配置、wallet 扣减、total_fees 增量和交易明细。",
      boundary: "手续费流水只说明费用扣减，不等同于成交盈亏或账户净值变化全貌。",
    };
  }
  if (bucket === "funding_fee") {
    return {
      scope: "资金费结算任务对持仓用户产生的收付。",
      verificationPath: "核对资金费任务、结算批次、funding event、费率模式、持仓方向和相关 event_id。",
      boundary: "资金费记录依赖结算任务状态；失败、重试和补偿仍在资金费页处理，不在账本表直接改数。",
    };
  }
  if (bucket === "clearing") {
    return {
      scope: "强平、保险覆盖后账本落账，或 ADL 自动减仓带来的已实现变化。",
      verificationPath: "核对 liquidation/adl event、保险基金流水、残余坏账、仓位关闭结果和用户保证金流水。",
      boundary: "清算类流水是结果记录，不是执行按钮；ADL、坏账接管和保险基金调整保持在对应清算页隔离处理。",
    };
  }
  return {
    scope: "后端新增、空缺或非标准合约保证金变更类型。",
    verificationPath: "保留 entry_id、change_type、related_*、note 和筛选条件，回后端日志、系统与审计确认语义。",
    boundary: "未知类型只做升级线索，不在前端页面修改账户、仓位、保证金或清算数据。",
  };
}

function marginLedgerVerificationPath(entry: ContractLedgerItem) {
  return marginLedgerReview(marginLedgerBucket(entry)).verificationPath;
}

function marginLedgerBoundaryNote(entry: ContractLedgerItem) {
  const review = marginLedgerReview(marginLedgerBucket(entry));
  return `${review.boundary} 当前 CSV 是已加载合约保证金流水当前结果，不是全量账务对账包。`;
}

type RiskParameterBucket = "trading_mode" | "leverage_bounds" | "maintenance_margin" | "funding_mode" | "risk_tier" | "coverage_gap";

const riskParameterBuckets: RiskParameterBucket[] = ["trading_mode", "leverage_bounds", "maintenance_margin", "funding_mode", "risk_tier", "coverage_gap"];

function riskParameterBucketLabel(bucket: RiskParameterBucket) {
  if (bucket === "trading_mode") return "交易模式";
  if (bucket === "leverage_bounds") return "杠杆边界";
  if (bucket === "maintenance_margin") return "维持保证金";
  if (bucket === "funding_mode") return "资金费模式";
  if (bucket === "risk_tier") return "风险阶梯";
  return "配置缺口";
}

function riskParameterReview(bucket: RiskParameterBucket) {
  if (bucket === "trading_mode") {
    return {
      scope: "`normal / reduce_only / paused` 决定 PERP 市场是否允许开仓、仅减仓或暂停交易。",
      verificationPath: "先核对市场运营产品配置、当前委托拒单、机器人运行状态和操作记录中的交易模式变更。",
      boundary: "风险参数页只读展示交易模式；编辑仍回市场运营产品配置并保留二次确认，不复制第二套保存入口。",
    };
  }
  if (bucket === "leverage_bounds") {
    return {
      scope: "市场最大杠杆、默认杠杆和各风险阶梯最大杠杆共同影响新订单可用杠杆。",
      verificationPath: "核对市场配置、风险阶梯、下单拒单原因、仓位杠杆和保证金占用变化。",
      boundary: "杠杆边界不是调仓工具；修改后也不自动重算既有仓位或补偿历史订单。",
    };
  }
  if (bucket === "maintenance_margin") {
    return {
      scope: "市场默认维持保证金率、阶梯维持率和维持扣减共同影响强平价、风险告警和清算触发。",
      verificationPath: "核对仓位风险、强平价、维护保证金、风险阶梯档位和最近强平/ADL 事件。",
      boundary: "维持保证金参数是清算核心配置；页面展示或导出不能替代风控审批、回测或日终风险核对。",
    };
  }
  if (bucket === "funding_mode") {
    return {
      scope: "资金费模式决定费率来源和结算解释，例如 Binance 源或本地公式。",
      verificationPath: "核对市场级资金费模式、价格状态、资金费任务、结算水位和逐账户资金费记录。",
      boundary: "资金费模式展示不执行结算或重试；失败任务仍回资金费 tab 处理。",
    };
  }
  if (bucket === "risk_tier") {
    return {
      scope: "按名义价值分层的最大杠杆、维持保证金率和维持扣减配置。",
      verificationPath: "核对阶梯连续性、最后一档上限、当前持仓名义价值、下单拒单和强平价变化。",
      boundary: "保存风险阶梯是危险写入，必须保留前端二次确认、后端 confirm_execute 和操作记录。",
    };
  }
  return {
    scope: "当前页面已加载市场、价格状态或风险阶梯之间存在缺口，需要人工补查。",
    verificationPath: "核对 `/admin/contracts/risk-tiers`、市场配置、价格状态和后端日志，确认是 fallback、缺表还是市场状态未加载。",
    boundary: "配置缺口只做升级线索，不在前端表格里自动补阶梯、改市场配置或触发清算。",
  };
}

function riskParameterVerificationPath(bucket: RiskParameterBucket) {
  return riskParameterReview(bucket).verificationPath;
}

function riskParameterBoundaryNote(bucket: RiskParameterBucket) {
  const review = riskParameterReview(bucket);
  return `${review.boundary} 当前 CSV 是当前页面风险参数快照，不是审批单、回测报告或完整风控变更包。`;
}

const defaultSeedBook = (market?: AdminMarketItem, surveillance?: MarketSurveillanceItem): SeedBookState => {
  const bid = Number(surveillance?.metrics.best_bid ?? 0);
  const ask = Number(surveillance?.metrics.best_ask ?? 0);
  const reference = Number(market?.reference_price ?? 0);
  const mid = bid > 0 && ask > 0 ? String((bid + ask) / 2) : reference > 0 ? String(reference) : market?.market_type === "mainstream" ? "100" : "1";
  return {
    midPrice: mid,
    levels: "12",
    gapTicks: "1",
    quantity: "",
    cancelExisting: true,
  };
};

const defaultSweepPreview = (): SweepPreviewState => ({
  side: "buy",
  mode: "quote_amount",
  quoteAmount: "1000",
  quantity: "",
  depth: "50",
});

const riskTierEditsFromItems = (items: ContractRiskTier[]): Record<string, ContractRiskTierEdit[]> => {
  const grouped: Record<string, ContractRiskTierEdit[]> = {};
  items.forEach((item) => {
    const symbol = item.symbol;
    grouped[symbol] = grouped[symbol] ?? [];
    grouped[symbol].push({
      tier: item.tier,
      notional_floor: item.notional_floor,
      notional_cap: item.notional_cap ?? "",
      max_leverage: item.max_leverage,
      maintenance_margin_rate: item.maintenance_margin_rate,
      maintenance_amount: item.maintenance_amount ?? "0",
    });
  });
  Object.values(grouped).forEach((rows) => rows.sort((a, b) => a.tier - b.tier));
  return grouped;
};

export function AdminPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const appliedDeepLinkSearch = useRef("");
  const skipUrlSyncOnce = useRef(false);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [deploymentChecklist, setDeploymentChecklist] = useState<DeploymentChecklist>();
  const [systemStatus, setSystemStatus] = useState<SystemStatus>();
  const [reconciliationSummary, setReconciliationSummary] = useState<ReconciliationSummary | null>(null);
  const [shadowAccountingSummary, setShadowAccountingSummary] = useState<ShadowAccountingSummary | null>(null);
  const [financialOutboxSummary, setFinancialOutboxSummary] = useState<FinancialOutboxSummary | null>(null);
  const [accountingProofs, setAccountingProofs] = useState<AccountingProofPage | null>(null);
  const [robotFinancialCheckpoints, setRobotFinancialCheckpoints] = useState<RobotFinancialCheckpointPage | null>(null);
  const [accountingGateReadiness, setAccountingGateReadiness] = useState<AccountingGateReadiness | null>(null);
  const accountingEvidenceLoadedRef = useRef(false);
  const [reconciliationRunning, setReconciliationRunning] = useState(false);
  const [accountingProofRunning, setAccountingProofRunning] = useState(false);
  const [robotCheckpointRunning, setRobotCheckpointRunning] = useState(false);
  const [markets, setMarkets] = useState<Record<string, AdminMarketItem>>({});
  const [persistedMarkets, setPersistedMarkets] = useState<Record<string, AdminMarketItem>>({});
  const [persistedMarketModes, setPersistedMarketModes] = useState<Record<string, ContractTradingMode>>({});
  const [marketSymbols, setMarketSymbols] = useState<string[]>([]);
  const [marketTemplates, setMarketTemplates] = useState<MarketTemplate[]>([]);
  const [marketSurveillance, setMarketSurveillance] = useState<Record<string, MarketSurveillanceItem>>({});
  const [marketBots, setMarketBots] = useState<Record<string, MarketBotAccount[]>>({});
  const [marketBotForms, setMarketBotForms] = useState<Record<string, MarketBotFormState>>({});
  const [makerInstances, setMakerInstances] = useState<Record<string, MakerInstanceStatus>>({});
  const [strategySelections, setStrategySelections] = useState<Record<string, string>>({});
  const [marketStrategies, setMarketStrategies] = useState<Record<string, MarketStrategyState>>({});
  const [marketStrategyConfigEdits, setMarketStrategyConfigEdits] = useState<Record<string, LiquidityRuntimeConfig>>({});
  const [strategyApplyStatuses, setStrategyApplyStatuses] = useState<Record<string, StrategyApplyStatus>>({});
  const [contractAccounts, setContractAccounts] = useState<ContractAccountAdminItem[]>([]);
  const [contractPositions, setContractPositions] = useState<ContractPositionAdminItem[]>([]);
  const [contractOrders, setContractOrders] = useState<ContractOrderAdminItem[]>([]);
  const [contractTrades, setContractTrades] = useState<TradeItem[]>([]);
  const [contractMarketStates, setContractMarketStates] = useState<ContractPriceState[]>([]);
  const [contractFundingEvents, setContractFundingEvents] = useState<ContractFundingEventAdminItem[]>([]);
  const [contractLedgerEntries, setContractLedgerEntries] = useState<ContractLedgerAdminItem[]>([]);
  const [contractInsuranceFunds, setContractInsuranceFunds] = useState<ContractInsuranceFund[]>([]);
  const [contractInsuranceEvents, setContractInsuranceEvents] = useState<ContractInsuranceEventAdminItem[]>([]);
  const [contractAdlEvents, setContractAdlEvents] = useState<ContractAdlEventAdminItem[]>([]);
  const [contractFundingJobs, setContractFundingJobs] = useState<ContractFundingJob[]>([]);
  const [contractFundingSettlements, setContractFundingSettlements] = useState<ContractFundingSettlement[]>([]);
  const [contractLiquidationEvents, setContractLiquidationEvents] = useState<ContractLiquidationEventAdminItem[]>([]);
  const [contractDetailsLoaded, setContractDetailsLoaded] = useState(false);
  const [contractDetailsLoading, setContractDetailsLoading] = useState(false);
  const [contractDetailsError, setContractDetailsError] = useState<string | null>(null);
  const [contractDetailsLoadedAt, setContractDetailsLoadedAt] = useState<number | null>(null);
  const [contractRiskTierEdits, setContractRiskTierEdits] = useState<Record<string, ContractRiskTierEdit[]>>({});
  const [contractMaintenance, setContractMaintenance] = useState<ContractMaintenanceStatus>({});
  const [insuranceAdjustAmount, setInsuranceAdjustAmount] = useState("0");
  const [insuranceAdjusting, setInsuranceAdjusting] = useState(false);
  const [adlExecutingEventId, setAdlExecutingEventId] = useState<string | null>(null);
  const [batchRetryingFundingJobs, setBatchRetryingFundingJobs] = useState(false);
  const [userEdits, setUserEdits] = useState<Record<number, UserEditState>>({});
  const [balanceAdjustments, setBalanceAdjustments] = useState<Record<number, BalanceAdjustmentState>>({});
  const [accountActionPending, setAccountActionPending] = useState<AccountActionPending | null>(null);
  const [seedBookForms, setSeedBookForms] = useState<Record<string, SeedBookState>>({});
  const [sweepPreviewForms, setSweepPreviewForms] = useState<Record<string, SweepPreviewState>>({});
  const [sweepPreviews, setSweepPreviews] = useState<Record<string, SweepPreviewResult>>({});
  const [activeActivityUserId, setActiveActivityUserId] = useState<number | null>(null);
  const [loadingActivityUserId, setLoadingActivityUserId] = useState<number | null>(null);
  const [userActivities, setUserActivities] = useState<Record<number, AdminUserActivity>>({});
  const [auditOrders, setAuditOrders] = useState<AdminOrderAuditItem[]>([]);
  const [auditTrades, setAuditTrades] = useState<AdminTradeAuditItem[]>([]);
  const [auditOrderQueryMeta, setAuditOrderQueryMeta] = useState<AdminAuditQueryMeta | null>(null);
  const [auditTradeQueryMeta, setAuditTradeQueryMeta] = useState<AdminAuditQueryMeta | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditProduct, setAuditProduct] = useState<AuditProductScope>(() => auditProductScopeFromParam(new URLSearchParams(location.search).get("auditProduct") || ""));
  const [auditSymbol, setAuditSymbol] = useState(() => normalizeMarketSymbolInput(new URLSearchParams(location.search).get("auditSymbol") || "") || "all");
  const [auditUserId, setAuditUserId] = useState(() => {
    const value = (new URLSearchParams(location.search).get("auditUserId") || "").trim();
    return value && value !== "all" && Number.isFinite(Number(value)) ? value : "all";
  });
  const [auditStatus, setAuditStatus] = useState<AuditStatusScope>(() => auditStatusScopeFromParam(new URLSearchParams(location.search).get("auditStatus") || ""));
  const [auditDataScope, setAuditDataScope] = useState<OrderAuditDataScope>(() => {
    const params = new URLSearchParams(location.search);
    return orderAuditDataScopeFromParam(params.get("auditDomain") || params.get("auditDataScope") || params.get("dataDomain") || "");
  });
  const [adminOperations, setAdminOperations] = useState<AdminOperationAuditItem[]>([]);
  const [operationAuditFilters, setOperationAuditFilters] = useState<AdminOperationAuditFilters>(() => defaultAdminOperationFilters());
  const [operationAuditLoading, setOperationAuditLoading] = useState(false);
  const [operationAuditError, setOperationAuditError] = useState<string | null>(null);
  const [lastOperationResult, setLastOperationResult] = useState<AdminOperationResult | null>(null);
  const [activeAdminOperation, setActiveAdminOperation] = useState<AdminActiveOperation | null>(null);
  const [newUser, setNewUser] = useState({
    username: "",
    password: "",
    role: "manual_user",
    usdt: "100000000",
    baseAmount: "100000000",
    makerFeeRate: "0",
    takerFeeRate: "0",
  });
  const [newMarket, setNewMarket] = useState<NewMarketFormState>({
    symbol: "ABCUSDT",
    productType: "SPOT",
    marketType: "listed",
    baseAsset: "ABC",
    quoteAsset: "USDT",
    marginAsset: "USDT",
    priceTick: "0.001",
    qtyStep: "0.01",
    minQty: "0.01",
    minNotional: "5",
    maxLeverage: "20",
    defaultLeverage: "5",
    maintenanceMarginRate: "0.005",
    fundingRate: "0",
    fundingIntervalHours: "8",
    indexPriceSource: "binance",
    markPriceMode: "orderbook",
    fundingRateMode: "binance",
    fundingInterestRate: "0.0001",
    fundingClampRate: "0.0005",
    fundingCapRate: "0.02",
    fundingImpactNotional: "25000",
    defaultMakerFeeRate: "0",
    defaultTakerFeeRate: "0",
    referencePrice: "1",
    createDefaultBots: false,
    defaultBotCount: "2",
    defaultBotInitialQuoteAmount: "1000000000",
    defaultBotInitialBaseNotional: "1000000000",
    isActive: true,
  });
  const authSession = useAppStore((state) => state.authSession);
  const setAuthSession = useAppStore((state) => state.setAuthSession);
  const pushToast = useAppStore((state) => state.pushToast);
  const adminApiKey = authSession?.role === "admin" ? authSession.api_key : "";
  const [adminSection, setAdminSection] = useState<AdminSection>(() => adminSectionFromSearch(location.search));
  const lightweightLiquiditySection = adminSection === "maker_config" || adminSection === "flow_config";
  const [selectedMarketSymbol, setSelectedMarketSymbol] = useState(() => marketSymbolFromSearch(location.search));
  const [marketDetailTab, setMarketDetailTab] = useState<MarketDetailTab>(() => marketDetailTabFromSearch(location.search));
  const [contractAdminTab, setContractAdminTab] = useState<ContractAdminTab>(() => contractAdminTabFromSearch(location.search));
  const [contractAccountScope, setContractAccountScope] = useState<ContractAccountScope>(() => {
    const params = new URLSearchParams(location.search);
    return contractAccountScopeFromParam(params.get("contractAccountScope") || params.get("contractScope") || "");
  });
  const [systemAdminTab, setSystemAdminTab] = useState<SystemAdminTab>(() => systemAdminTabFromSearch(location.search));
  const [selectedUserId, setSelectedUserId] = useState<number | null>(() => userIdFromSearch(location.search));
  const [accountScope, setAccountScope] = useState<AccountUserScope>("all");
  const [accountAdminView, setAccountAdminView] = useState<AccountAdminView>("overview");
  const [accountUserQuery, setAccountUserQuery] = useState("");
  const selectedMarketSymbols =
    selectedMarketSymbol && marketSymbols.includes(selectedMarketSymbol)
      ? [selectedMarketSymbol]
      : marketSymbols.slice(0, 1);
  const accountKindCounts = users.reduce<Record<AccountUserKind, number>>((current, user) => {
    const kind = accountUserKindForUser(user);
    current[kind] += 1;
    return current;
  }, { customer: 0, admin: 0, spot_robot: 0, contract_robot: 0, system: 0 });
  const accountUsersInScope = users.filter((user) => accountScopeIncludesUser(accountScope, user));
  const visibleUsers = selectedUserId ? accountUsersInScope.filter((user) => user.id === selectedUserId) : accountUsersInScope.slice(0, 1);
  const internalAccountCount = accountKindCounts.admin + accountKindCounts.spot_robot + accountKindCounts.contract_robot + accountKindCounts.system;
  const robotAccountCount = accountKindCounts.spot_robot + accountKindCounts.contract_robot;
	  const accountScopeOptions: AccountUserScopeOption[] = [
	    { key: "all", label: "全部主体", hint: "客户 + 机器人 + 内部主体", count: users.length },
	    { key: "customer", label: "客户 UID", hint: "普通外部客户完整记录", count: accountKindCounts.customer },
	    { key: "internal", label: "内部主体", hint: "管理 / 机器人 / 系统控制", count: internalAccountCount },
	    { key: "robot", label: "全部机器人", hint: "特殊 UID / 运行对账", count: robotAccountCount },
	    { key: "admin", label: "管理主体", hint: "后台登录 / 测试对象", count: accountKindCounts.admin },
	    { key: "spot_robot", label: "现货机器人", hint: "SPOT MM / FLOW 对账", count: accountKindCounts.spot_robot },
	    { key: "contract_robot", label: "合约机器人", hint: "PERP MM / FLOW 对账", count: accountKindCounts.contract_robot },
	    { key: "system", label: "系统主体", hint: "内部流动性 / 清算", count: accountKindCounts.system },
	  ];
  const selectedAccountUser = selectedUserId === null ? null : accountUsersInScope.find((user) => user.id === selectedUserId) ?? null;
  const accountUserQueryText = accountUserQuery.trim().toLowerCase();
  const accountSelectableUsers = accountUserQueryText
    ? accountUsersInScope.filter((user) => [
        user.id,
        user.username,
        user.role,
        accountUserKindLabel(accountUserKindForUser(user)),
      ].some((value) => String(value).toLowerCase().includes(accountUserQueryText)))
    : accountUsersInScope;
  const accountSelectOptions = selectedAccountUser && !accountSelectableUsers.some((user) => user.id === selectedAccountUser.id)
    ? [selectedAccountUser, ...accountSelectableUsers]
    : accountSelectableUsers;
  const accountDirectoryDefaultUser = accountUsersInScope[0];
  const selectedAccountContractAccounts = selectedAccountUser
    ? contractAccounts.filter((item) => item.user.id === selectedAccountUser.id || item.account.user_id === selectedAccountUser.id)
    : [];
  const selectedAccountContractPositions = selectedAccountUser
    ? contractPositions.filter((item) => item.user.id === selectedAccountUser.id)
    : [];
  const selectedAccountContractOrders = selectedAccountUser
    ? contractOrders.filter((item) => item.user.id === selectedAccountUser.id)
    : [];
  const selectedAccountContractLedgerEntries = selectedAccountUser
    ? contractLedgerEntries.filter((item) => item.user.id === selectedAccountUser.id || item.entry.user_id === selectedAccountUser.id).slice(0, 20)
    : [];
  const accountAdminViewOptions: Array<{ key: AccountAdminView; label: string; hint: string; badge: string; tone?: "warn" }> = [
    { key: "overview", label: "主体总览", hint: "分类 / 账本边界", badge: String(accountUsersInScope.length) },
    { key: "onboarding", label: "账户开立", hint: "身份 / 初始模板 / 费率", badge: "创建" },
    { key: "detail", label: "单 UID 核查", hint: "数据域 / 时间线", badge: selectedAccountUser ? `#${selectedAccountUser.id}` : "选择" },
    { key: "maintenance", label: "账户维护", hint: "UID 手续费 / 身份 / 现货", badge: selectedAccountUser ? accountUserKindLabel(accountUserKindForUser(selectedAccountUser)) : "选择", tone: "warn" },
  ];
  const contractDetailLoadState: ContractDetailLoadState = {
    loaded: contractDetailsLoaded,
    loading: contractDetailsLoading,
    error: contractDetailsError,
    updatedAt: contractDetailsLoadedAt,
  };

  const roleFeeDefaults = (_role: string) => ({
    maker: "0",
    taker: "0",
  });

  const defaultUserEdit = (
    user: AdminUser,
    symbols: string[] = marketSymbols,
    marketLookup: Record<string, AdminMarketItem> = markets,
  ): UserEditState => {
    const primaryFee = user.fee_profiles?.[0];
    const roleDefaults = roleFeeDefaults(user.role);
    const profiles = Object.fromEntries((user.fee_profiles ?? []).map((profile) => [profile.symbol, profile]));
    return {
      role: user.role,
      isActive: user.is_active,
      password: "",
      makerFeeRate: primaryFee?.maker_fee_rate ?? roleDefaults.maker,
      takerFeeRate: primaryFee?.taker_fee_rate ?? roleDefaults.taker,
      marketFees: Object.fromEntries(
        symbols.map((symbol) => {
          const profile = profiles[symbol];
          const market = marketLookup[symbol];
          return [
            symbol,
            {
              makerFeeRate: profile?.maker_fee_rate ?? market?.default_maker_fee_rate ?? roleDefaults.maker,
              takerFeeRate: profile?.taker_fee_rate ?? market?.default_taker_fee_rate ?? roleDefaults.taker,
            },
          ];
        }),
      ),
    };
  };

  const accountActionKey = (scope: number | "all" | "new", action: string, symbol?: string) =>
    `account:${scope}:${action}${symbol ? `:${symbol}` : ""}`;

  const accountActionBusy = (key: string) => accountActionPending?.key === key;
  const accountActionDisabled = Boolean(accountActionPending);

  const runAccountAction = async (
    key: string,
    label: string,
    action: () => Promise<unknown>,
  ) => {
    if (accountActionPending) {
      pushToast("error", `${accountActionPending.label} 正在执行，完成后再操作账户维护。`);
      return false;
    }
    setAccountActionPending({ key, label });
    try {
      await action();
      return true;
    } finally {
      setAccountActionPending((current) => (current?.key === key ? null : current));
    }
  };

  const userEditChangedFields = (user: AdminUser, edit: UserEditState) => {
    const baseline = defaultUserEdit(user);
    const changedFields: string[] = [];
    if (edit.role !== baseline.role) changedFields.push("角色");
    if (edit.isActive !== baseline.isActive) changedFields.push("启停");
    if (edit.password.trim()) changedFields.push("新密码");
    if (edit.makerFeeRate !== baseline.makerFeeRate || edit.takerFeeRate !== baseline.takerFeeRate) {
      changedFields.push("全市场费率");
    }
    const changedMarketFeeCount = marketSymbols.filter((symbol) => {
      const currentFee = edit.marketFees[symbol];
      const baselineFee = baseline.marketFees[symbol];
      return Boolean(
        currentFee
        && baselineFee
        && (currentFee.makerFeeRate !== baselineFee.makerFeeRate || currentFee.takerFeeRate !== baselineFee.takerFeeRate),
      );
    }).length;
    if (changedMarketFeeCount > 0) changedFields.push(`单市场费率 ${changedMarketFeeCount}`);
    return changedFields;
  };

  const defaultMarketBotForm = (market?: AdminMarketItem): MarketBotFormState => ({
    uid: "",
    username: "",
    password: "",
    apiKey: "",
    apiSecret: "",
    botLabel: "",
    role: "maker",
    strategyRole: "maker",
    referencePrice: market?.reference_price ?? "",
    initialQuoteAmount: "1000000000",
    initialBaseNotional: "1000000000",
    initialBaseAmount: "",
    isEnabled: true,
  });

  const fetchContractDetailTables = useCallback(async (filters?: ContractDetailFilters) => {
    const [
      contractOrdersData,
      contractTradesData,
      contractFundingEventsData,
      contractLedgerData,
      contractInsuranceEventsData,
      contractAdlEventsData,
      contractFundingJobsData,
      contractFundingSettlementsData,
      contractLiquidationEventsData,
    ] = await Promise.all([
      api.get<{ items: ContractOrderAdminItem[] }>(contractDetailTableQuery("/admin/contracts/orders", filters, users, { user: true }), adminApiKey),
      api.get<{ items: TradeItem[] }>(contractDetailTableQuery("/admin/contracts/trades", filters, users, { user: true }), adminApiKey),
      api.get<{ items: ContractFundingEventAdminItem[] }>(contractDetailTableQuery("/admin/contracts/funding/events", filters, users, { user: true }), adminApiKey),
      api.get<{ items: ContractLedgerAdminItem[] }>(contractDetailTableQuery("/admin/contracts/ledger", filters, users, { user: true }), adminApiKey),
      api.get<{ items: ContractInsuranceEventAdminItem[] }>(contractDetailTableQuery("/admin/contracts/insurance-events", filters, users, { user: true }), adminApiKey),
      api.get<{ items: ContractAdlEventAdminItem[] }>(contractDetailTableQuery("/admin/contracts/adl-events", filters, users, { user: true }), adminApiKey),
      api.get<{ items: ContractFundingJob[] }>(contractDetailTableQuery("/admin/contracts/funding/jobs", filters, users, { fundingStatus: true }), adminApiKey),
      api.get<{ items: ContractFundingSettlement[] }>(contractDetailTableQuery("/admin/contracts/funding/settlements", filters, users, { fundingStatus: true }), adminApiKey),
      api.get<{ items: ContractLiquidationEventAdminItem[] }>(contractDetailTableQuery("/admin/contracts/liquidations", filters, users, { user: true }), adminApiKey),
    ]);
    return {
      contractOrdersData,
      contractTradesData,
      contractFundingEventsData,
      contractLedgerData,
      contractInsuranceEventsData,
      contractAdlEventsData,
      contractFundingJobsData,
      contractFundingSettlementsData,
      contractLiquidationEventsData,
    };
  }, [adminApiKey, users]);

  const applyContractDetailTables = (data: Awaited<ReturnType<typeof fetchContractDetailTables>>) => {
    setContractOrders(data.contractOrdersData.items);
    setContractTrades(data.contractTradesData.items);
    setContractFundingEvents(data.contractFundingEventsData.items);
    setContractLedgerEntries(data.contractLedgerData.items);
    setContractInsuranceEvents(data.contractInsuranceEventsData.items);
    setContractAdlEvents(data.contractAdlEventsData.items);
    setContractFundingJobs(data.contractFundingJobsData.items);
    setContractFundingSettlements(data.contractFundingSettlementsData.items);
    setContractLiquidationEvents(data.contractLiquidationEventsData.items);
    setContractDetailsLoaded(true);
    setContractDetailsLoadedAt(Date.now());
    setContractDetailsError(null);
  };

  const refreshContractDetails = useCallback(async (options: { silent?: boolean; filters?: ContractDetailFilters } = {}) => {
    if (!adminApiKey) return;
    if (!options.silent) setContractDetailsLoading(true);
    setContractDetailsError(null);
    try {
      const data = await fetchContractDetailTables(options.filters);
      applyContractDetailTables(data);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载合约明细失败";
      setContractDetailsError(message);
      if (!options.silent) pushToast("error", message);
    } finally {
      if (!options.silent) setContractDetailsLoading(false);
    }
  }, [adminApiKey, fetchContractDetailTables, pushToast]);

  const load = async () => {
    if (!adminApiKey) return;
    const safeGet = <T,>(path: string, fallback: T) => api.get<T>(path, adminApiKey).catch(() => fallback);
    const [
      data,
      marketData,
      checklistData,
      systemData,
      templateData,
      contractAccountsData,
      contractPositionsData,
      contractMarketStatesData,
      contractInsuranceFundsData,
      contractRiskTiersData,
      contractMaintenanceData,
      adminOperationsData,
      reconciliationData,
      shadowAccountingData,
      outboxData,
      accountingProofData,
      robotCheckpointData,
      gateReadinessData,
    ] = await Promise.all([
      api.get<{ items: AdminUser[] }>("/admin/users", adminApiKey),
      api.get<{ items: AdminMarketItem[] }>("/admin/markets", adminApiKey),
      api.get<DeploymentChecklist>("/admin/deployment-checklist", adminApiKey),
      api.get<SystemStatus>("/admin/system-status", adminApiKey),
      api.get<{ items: MarketTemplate[] }>("/admin/market-templates", adminApiKey),
      safeGet<{ items: ContractAccountAdminItem[] }>("/admin/contracts/accounts", { items: contractAccounts }),
      safeGet<{ items: ContractPositionAdminItem[] }>("/admin/contracts/positions", { items: contractPositions }),
      safeGet<{ items: ContractPriceState[] }>("/admin/contracts/market-states", { items: contractMarketStates }),
      safeGet<{ items: ContractInsuranceFund[] }>("/admin/contracts/insurance-funds", { items: contractInsuranceFunds }),
      safeGet<{ items: ContractRiskTier[] }>("/admin/contracts/risk-tiers", { items: [] }),
      safeGet<ContractMaintenanceStatus>("/admin/contracts/maintenance", contractMaintenance),
      safeGet<{ items: AdminOperationAuditItem[] }>("/admin/operations?limit=100", { items: adminOperations }),
      safeGet<{ item: ReconciliationSummary | null }>("/admin/accounting/reconciliation/latest", { item: reconciliationSummary }),
      safeGet<ShadowAccountingSummary>("/admin/accounting/shadow-summary", shadowAccountingSummary ?? { mode: "shadow", source_of_truth: false, transactions: 0, entries: 0, reversals: 0, immutable_committed_records: true, automatic_repair: false }),
      safeGet<FinancialOutboxSummary>("/admin/accounting/outbox-summary", financialOutboxSummary ?? { mode: "transactional_database_outbox", delivery_semantics: "at_least_once", consumer_deduplication_key: "event_id", checkpoint: null, counts: { pending: 0, processing: 0, delivered: 0, dead: 0, total: 0, receipts: 0, retry_events: 0, replay_requests: 0 }, oldest_pending_at: null, automatic_balance_repair: false }),
      safeGet<AccountingProofPage>("/admin/accounting/proof-checkpoints?limit=20", accountingProofs ?? { items: [], total: 0, limit: 20, offset: 0, verification: { mode: "immutable_hash_chain", checkpoint_count: 0, invalid_count: 0, issues: [], latest: null, hash_algorithm: "sha256", canonical_decimal_scale: 18, signed_or_worm: false } }),
      Promise.resolve<RobotFinancialCheckpointPage>(robotFinancialCheckpoints ?? { items: [], total: 0, limit: 20, offset: 0, verification: { mode: "robot_financial_checkpoint_v1", checkpoint_count: 0, invalid_count: 0, issues: [], latest: null, financial_pruning_authorized: false, raw_records_deleted: 0 } }),
      Promise.resolve(accountingGateReadiness),
    ]);
    const symbols = marketData.items.map((item) => item.symbol);
    const marketLookup = Object.fromEntries(marketData.items.map((item) => [item.symbol, item]));
    const [marketStrategyEntries, botEntries, surveillanceData, instanceData] = await Promise.all([
      Promise.all(
        symbols.map(async (symbol) => {
          const response = await api.get<MarketStrategyState>(`/admin/markets/${symbol}/strategy`, adminApiKey);
          return [symbol, response] as const;
        }),
      ),
      Promise.all(
        symbols.map(async (symbol) => {
          const response = await api.get<{ items: MarketBotAccount[] }>(`/admin/markets/${symbol}/bots`, adminApiKey);
          return [symbol, response.items] as const;
        }),
      ),
      api.get<{ items: MarketSurveillanceItem[] }>("/admin/market-surveillance", adminApiKey),
      api.get<{ items: MakerInstanceStatus[] }>("/admin/maker-instances", adminApiKey),
    ]);
    setUsers(data.items);
    setDeploymentChecklist(checklistData);
    setSystemStatus(systemData);
    setMarketTemplates(templateData.items);
    setContractAccounts(contractAccountsData.items);
    setContractPositions(contractPositionsData.items);
    setContractMarketStates(contractMarketStatesData.items);
    setContractInsuranceFunds(contractInsuranceFundsData.items);
    setContractRiskTierEdits(riskTierEditsFromItems(contractRiskTiersData.items));
    setContractMaintenance(contractMaintenanceData);
    setAdminOperations(adminOperationsData.items);
    setReconciliationSummary(reconciliationData.item);
    setShadowAccountingSummary(shadowAccountingData);
    setFinancialOutboxSummary(outboxData);
    setAccountingProofs(accountingProofData);
    setRobotFinancialCheckpoints(robotCheckpointData);
    setAccountingGateReadiness(gateReadinessData);
    setUserEdits(Object.fromEntries(data.items.map((user) => [user.id, defaultUserEdit(user, symbols, marketLookup)])));
    setUserActivities({});
    setActiveActivityUserId(null);
    setMarketSymbols(symbols);
    setMarkets(marketLookup);
    setPersistedMarkets(marketLookup);
    setPersistedMarketModes(Object.fromEntries(marketData.items.map((item) => [item.symbol, normalizeContractTradingMode(item.contract_trading_mode)])));
    setMarketBots(Object.fromEntries(botEntries));
    setMakerInstances(Object.fromEntries(instanceData.items.map((item) => [item.symbol, item])));
    setMarketBotForms((current) =>
      Object.fromEntries(symbols.map((symbol) => [symbol, current[symbol] ?? defaultMarketBotForm(marketLookup[symbol])]))
    );
    setMarketSurveillance(Object.fromEntries(surveillanceData.items.map((item) => [item.symbol, item])));
    setMarketStrategies(Object.fromEntries(marketStrategyEntries));
    setStrategySelections(
      Object.fromEntries(marketStrategyEntries.map(([symbol, item]) => [symbol, String(item.selected?.strategy_key ?? "LITE").toUpperCase()]))
    );
    setMarketStrategyConfigEdits(
      Object.fromEntries(
        marketStrategyEntries.map(([symbol, item]) => [
          symbol,
          cloneValue(item.selected?.effective_config ?? item.selected?.config ?? {}),
        ])
      )
    );
    if (shouldLoadContractDetailsForSection(adminSection) || contractDetailsLoaded) {
      setContractDetailsLoading(true);
      setContractDetailsError(null);
      try {
        const details = await fetchContractDetailTables();
        applyContractDetailTables(details);
      } catch (error) {
        setContractDetailsError(error instanceof Error ? error.message : "加载合约明细失败");
      } finally {
        setContractDetailsLoading(false);
      }
    }
  };

  const runReconciliation = async () => {
    if (!adminApiKey || reconciliationRunning) return;
    setReconciliationRunning(true);
    try {
      const result = await api.post<ReconciliationSummary>("/admin/accounting/reconciliation/run", { scope: "full" }, adminApiKey);
      setReconciliationSummary(result);
      const [shadow, gates] = await Promise.all([
        api.get<ShadowAccountingSummary>("/admin/accounting/shadow-summary", adminApiKey),
        api.get<AccountingGateReadiness>("/admin/accounting/gate-readiness", adminApiKey),
      ]);
      setShadowAccountingSummary(shadow);
      setAccountingGateReadiness(gates);
      pushToast(result.blocking_count ? "error" : "success", `对账完成：${result.status}，差异 ${result.difference_count}`);
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "对账执行失败");
    } finally {
      setReconciliationRunning(false);
    }
  };

  const createAccountingProof = async () => {
    if (!adminApiKey || accountingProofRunning || reconciliationRunning) return;
    setAccountingProofRunning(true);
    try {
      const idempotencyKey = `admin-proof-${crypto.randomUUID()}`;
      const result = await api.post<{ created: boolean; item: AccountingProofItem }>(
        "/admin/accounting/proof-checkpoints",
        { idempotency_key: idempotencyKey },
        adminApiKey,
      );
      const [proofs, latest, gates] = await Promise.all([
        api.get<AccountingProofPage>("/admin/accounting/proof-checkpoints?limit=20", adminApiKey),
        api.get<{ item: ReconciliationSummary | null }>("/admin/accounting/reconciliation/latest", adminApiKey),
        api.get<AccountingGateReadiness>("/admin/accounting/gate-readiness", adminApiKey),
      ]);
      setAccountingProofs(proofs);
      setReconciliationSummary(latest.item);
      setAccountingGateReadiness(gates);
      pushToast(
        result.item.status === "passed" ? "success" : "error",
        `证明检查点 #${result.item.sequence_no}：${result.item.status}`,
      );
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "生成账务证明失败");
    } finally {
      setAccountingProofRunning(false);
    }
  };

  const createRobotFinancialCheckpoint = async () => {
    if (!adminApiKey || robotCheckpointRunning || accountingProofRunning || reconciliationRunning) return;
    setRobotCheckpointRunning(true);
    try {
      const result = await api.post<{ created: boolean; item: RobotFinancialCheckpointItem }>(
        "/admin/accounting/robot-checkpoints",
        { idempotency_key: `admin-robot-checkpoint-${crypto.randomUUID()}` },
        adminApiKey,
      );
      const [checkpoints, proofs, latest] = await Promise.all([
        api.get<RobotFinancialCheckpointPage>("/admin/accounting/robot-checkpoints?limit=20", adminApiKey),
        api.get<AccountingProofPage>("/admin/accounting/proof-checkpoints?limit=20", adminApiKey),
        api.get<{ item: ReconciliationSummary | null }>("/admin/accounting/reconciliation/latest", adminApiKey),
      ]);
      setRobotFinancialCheckpoints(checkpoints);
      setAccountingProofs(proofs);
      setReconciliationSummary(latest.item);
      setAccountingGateReadiness(await api.get<AccountingGateReadiness>("/admin/accounting/gate-readiness", adminApiKey));
      pushToast(
        result.item.status === "passed" ? "success" : "error",
        `机器人金融 checkpoint #${result.item.sequence_no}：${result.item.status}，原始记录未删除`,
      );
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "生成机器人金融 checkpoint 失败");
    } finally {
      setRobotCheckpointRunning(false);
    }
  };

  const refreshContractLiveData = useCallback(async () => {
    if (!adminApiKey) return;
    try {
      const includeDetails = adminSection === "contracts" || adminSection === "risk" || (adminSection === "accounts" && contractDetailsLoaded);
      const [
        contractAccountsData,
        contractPositionsData,
        contractMarketStatesData,
        contractInsuranceFundsData,
        contractMaintenanceData,
        instanceData,
        contractDetailsData,
      ] = await Promise.all([
        api.get<{ items: ContractAccountAdminItem[] }>("/admin/contracts/accounts", adminApiKey),
        api.get<{ items: ContractPositionAdminItem[] }>("/admin/contracts/positions", adminApiKey),
        api.get<{ items: ContractPriceState[] }>("/admin/contracts/market-states", adminApiKey),
        api.get<{ items: ContractInsuranceFund[] }>("/admin/contracts/insurance-funds", adminApiKey),
        api.get<ContractMaintenanceStatus>("/admin/contracts/maintenance", adminApiKey),
        api.get<{ items: MakerInstanceStatus[] }>("/admin/maker-instances", adminApiKey),
        includeDetails ? fetchContractDetailTables() : Promise.resolve(null),
      ]);
      setContractAccounts(contractAccountsData.items);
      setContractPositions(contractPositionsData.items);
      setContractMarketStates(contractMarketStatesData.items);
      setContractInsuranceFunds(contractInsuranceFundsData.items);
      setContractMaintenance(contractMaintenanceData);
      setMakerInstances(Object.fromEntries(instanceData.items.map((item) => [item.symbol, item])));
      if (contractDetailsData) applyContractDetailTables(contractDetailsData);
    } catch {
      // 自动刷新失败时保持当前屏幕数据，避免轮询期间反复打扰运维操作。
    }
  }, [adminApiKey, adminSection, contractDetailsLoaded, fetchContractDetailTables]);

  const refreshAdminOperations = useCallback(async (filters: AdminOperationAuditFilters = operationAuditFilters) => {
    if (!adminApiKey) return;
    setOperationAuditLoading(true);
    setOperationAuditError(null);
    try {
      const data = await api.get<{ items: AdminOperationAuditItem[] }>(adminOperationQuery(filters), adminApiKey);
      setAdminOperations(data.items);
    } catch (error) {
      setOperationAuditError(error instanceof Error ? error.message : String(error || "操作记录加载失败"));
      // 操作记录加载失败不影响当前业务操作结果展示。
    } finally {
      setOperationAuditLoading(false);
    }
  }, [adminApiKey, operationAuditFilters]);

  const resetAdminOperationFilters = () => {
    const next = defaultAdminOperationFilters();
    setOperationAuditFilters(next);
    void refreshAdminOperations(next);
  };

  const showOperationResult = (
    title: string,
    message: string,
    result?: unknown,
    status: "success" | "error" = "success",
    recovery?: AdminOperationRecoveryHint,
    context?: Record<string, unknown>,
  ) => {
    setLastOperationResult({ title, status, message, result, context, recovery, ts: Date.now() });
    pushToast(status, message);
  };

  const operationErrorMessage = (error: unknown) => error instanceof Error ? error.message : String(error || "操作失败");

  const showOperationError = (title: string, message: string, error: unknown, context?: Record<string, unknown>) => {
    const detail = operationErrorMessage(error);
    showOperationResult(title, `${message}：${detail}`, { ok: false, error: detail, ...(context ?? {}) }, "error", operationRecoveryHint(title, context), context);
    return detail;
  };

  const recordAdminOperationFailure = async (
    title: string,
    failureMessage: string,
    errorDetail: string,
    context?: Record<string, unknown>,
  ) => {
    if (!adminApiKey) return;
    try {
      const response = await api.post<{ item: AdminOperationAuditItem }>(
        "/admin/operations/failures",
        adminFailureAuditPayload(title, failureMessage, errorDetail, context),
        adminApiKey,
      );
      setAdminOperations((current) => [response.item, ...current.filter((item) => item.operation_id !== response.item.operation_id)].slice(0, 100));
    } catch {
      // Failure audit should never mask the original operation error shown above.
    }
  };

  const runAdminOperation = async (
    title: string,
    failureMessage: string,
    action: () => Promise<void>,
    context?: Record<string, unknown>,
  ) => {
    const startedAt = Date.now() + Math.random();
    setActiveAdminOperation({ title, context, startedAt });
    try {
      await action();
      return true;
    } catch (error) {
      const detail = showOperationError(title, failureMessage, error, context);
      void recordAdminOperationFailure(title, failureMessage, detail, context);
      return false;
    } finally {
      setActiveAdminOperation((current) => current?.startedAt === startedAt ? null : current);
    }
  };

  const syncAccountScopeForUserId = (userId: number) => {
    const targetUser = users.find((user) => user.id === userId);
    if (targetUser) {
      setAccountScope(accountUserKindForUser(targetUser));
    }
  };

  const openAdminBusinessTarget = (target: AdminBusinessTarget) => {
    const targetSymbol = target.targetSymbol && marketSymbols.includes(target.targetSymbol) ? target.targetSymbol : "";
    const targetUserId = target.targetUserId != null && users.some((user) => user.id === target.targetUserId) ? target.targetUserId : null;
    const targetAccountScope = target.accountScope && accountUserScopeKeys.includes(target.accountScope) ? target.accountScope : null;
    const targetContractAccountScope = target.contractAccountScope && contractAccountScopeKeys.includes(target.contractAccountScope) ? target.contractAccountScope : null;
    if (target.targetSymbol && marketSymbols.includes(target.targetSymbol)) {
      setSelectedMarketSymbol(target.targetSymbol);
    }
    if (target.section === "accounts" && targetAccountScope) {
      setAccountScope(targetAccountScope);
    }
    if (targetUserId != null) {
      setSelectedUserId(targetUserId);
      if (target.section === "accounts") {
        const targetUser = users.find((user) => user.id === targetUserId);
        const targetKind = targetUser ? accountUserKindForUser(targetUser) : null;
        if (!targetAccountScope || (targetKind && !accountScopeIncludesKind(targetAccountScope, targetKind))) {
          syncAccountScopeForUserId(targetUserId);
        }
      }
    } else if (target.section === "accounts") {
      setSelectedUserId(null);
    }
    if (target.marketDetailTab) {
      setMarketDetailTab(target.marketDetailTab);
    }
    if (target.contractTab) {
      setContractAdminTab(target.contractTab);
    }
    if (target.section === "contracts" && targetContractAccountScope) {
      setContractAccountScope(targetContractAccountScope);
    }
    if (target.systemTab) {
      setSystemAdminTab(target.systemTab);
    }
    setAdminSection(target.section);
    const params = new URLSearchParams();
    if (target.section !== "overview") params.set("section", target.section);
    if ((target.section === "markets" || target.section === "strategies") && targetSymbol) {
      params.set("market", targetSymbol);
    }
    if (target.section === "markets" && target.marketDetailTab && target.marketDetailTab !== "overview") {
      params.set("marketTab", target.marketDetailTab);
    }
    if (target.section === "contracts" && target.contractTab && target.contractTab !== "overview") {
      params.set("contractTab", target.contractTab);
    }
    if (target.section === "contracts" && target.contractTab === "accounts" && targetContractAccountScope && targetContractAccountScope !== "all") {
      params.set("contractAccountScope", targetContractAccountScope);
    }
    if (target.section === "system" && target.systemTab && target.systemTab !== "overview") {
      params.set("systemTab", target.systemTab);
    }
    if (target.section === "accounts" && targetUserId != null) {
      params.set("userId", String(targetUserId));
    }
    if (target.section === "accounts" && targetAccountScope && targetAccountScope !== "all") {
      params.set("accountScope", targetAccountScope);
    }
    const nextSearch = params.toString() ? `?${params.toString()}` : "";
    appliedDeepLinkSearch.current = nextSearch;
    skipUrlSyncOnce.current = false;
    navigate({ pathname: location.pathname, search: nextSearch }, { replace: true });
  };

  const openMarketOperationAudit = (symbol: string) => {
    const nextFilters: AdminOperationAuditFilters = {
      ...defaultAdminOperationFilters(),
      targetSymbol: normalizeMarketSymbolInput(symbol),
      limit: "100",
    };
    setOperationAuditFilters(nextFilters);
    setSystemAdminTab("audit");
    setAdminSection("system");
    void refreshAdminOperations(nextFilters);
  };

  const openOrderAuditTarget = useCallback((target: OrderAuditTarget) => {
    const requestedSymbol = normalizeMarketSymbolInput(String(target.symbol ?? ""));
    const symbolIsKnown = requestedSymbol !== "" && marketSymbols.includes(requestedSymbol);
    const symbolProduct = symbolIsKnown ? markets[requestedSymbol]?.product_type : undefined;
    const nextProduct = target.product ?? auditProductFromProductType(symbolProduct);
    const symbolMatchesProduct = !requestedSymbol || nextProduct === "all" || symbolProduct === nextProduct;
    const requestedUserId = String(target.userId ?? "").trim();
    const numericUserId = Number(requestedUserId);
    const userIsKnown =
      requestedUserId !== ""
      && requestedUserId !== "all"
      && Number.isFinite(numericUserId)
      && users.some((user) => user.id === numericUserId);
    const nextSymbol = symbolIsKnown && symbolMatchesProduct ? requestedSymbol : "all";
    const nextUserId = userIsKnown ? String(numericUserId) : "all";
    const nextStatus = target.status ?? "all";
    const nextDataScope = target.dataScope ?? "all";
    setAuditProduct(nextProduct);
    setAuditSymbol(nextSymbol);
    setAuditUserId(nextUserId);
    setAuditStatus(nextStatus);
    setAuditDataScope(nextDataScope);
    setAdminSection("orders");
    const params = new URLSearchParams();
    params.set("section", "orders");
    if (nextProduct !== "all") params.set("auditProduct", nextProduct);
    if (nextSymbol !== "all") params.set("auditSymbol", nextSymbol);
    if (nextUserId !== "all") params.set("auditUserId", nextUserId);
    if (nextStatus !== "live") params.set("auditStatus", nextStatus);
    if (nextDataScope !== "all") params.set("auditDomain", nextDataScope);
    const nextSearch = `?${params.toString()}`;
    appliedDeepLinkSearch.current = nextSearch;
    skipUrlSyncOnce.current = false;
    navigate({ pathname: location.pathname, search: nextSearch }, { replace: true });
  }, [location.pathname, marketSymbols, markets, navigate, users]);

  const openAdminOperationTarget = (item: AdminOperationAuditItem) => {
    const symbol = item.target_symbol ?? "";
    if (item.domain === "account") {
      const targetUserId = Number(item.target_id);
      openAdminBusinessTarget({
        section: "accounts",
        targetUserId: Number.isFinite(targetUserId) ? targetUserId : null,
      });
      return;
    }
    if (item.domain === "market") {
      openAdminBusinessTarget({
        section: "markets",
        targetSymbol: symbol,
        marketDetailTab: marketTabForOperationType(item.operation_type),
      });
      return;
    }
    if (item.domain === "bot") {
      openAdminBusinessTarget({
        section: "markets",
        targetSymbol: symbol,
        marketDetailTab: "instance",
      });
      return;
    }
    if (item.domain === "contract") {
      openAdminBusinessTarget({
        section: "contracts",
        targetSymbol: symbol,
        contractTab: contractTabForOperationType(item.operation_type),
      });
      return;
    }
    if (item.domain === "system") {
      const operationType = adminOperationTypeInput(item.operation_type);
      openAdminBusinessTarget({
        section: "system",
        systemTab: operationType === "orderbook_rebuild"
          ? "consistency"
          : operationType === "history_retention_run"
            ? "runtime"
            : "overview",
      });
      return;
    }
    openAdminBusinessTarget({ section: "system" });
  };

  const openSystemAdminTab = (tab: SystemAdminTab) => {
    openAdminBusinessTarget({ section: "system", systemTab: tab });
  };

  const openMarketDetailTab = (tab: MarketDetailTab) => {
    openAdminBusinessTarget({ section: "markets", targetSymbol: selectedMarketSymbol, marketDetailTab: tab });
  };

  const openRiskQueueTarget = (item: RiskQueueItem) => {
    if (item.targetSystemTab === "audit" && item.targetOperationStatus && item.targetOperationStatus !== "all") {
      const nextFilters: AdminOperationAuditFilters = {
        ...defaultAdminOperationFilters(),
        domain: item.targetOperationDomain ?? "all",
        status: item.targetOperationStatus,
        operationType: item.targetOperationType ?? "all",
        targetSymbol: item.targetOperationSymbol ?? "",
      };
      setOperationAuditFilters(nextFilters);
      void refreshAdminOperations(nextFilters);
    }
    openAdminBusinessTarget({
      section: item.targetSection,
      targetSymbol: item.targetSymbol,
      marketDetailTab: item.targetMarketDetailTab,
      contractTab: item.targetContractTab,
      systemTab: item.targetSystemTab,
    });
  };

  const loadOrderAudit = useCallback(async () => {
    if (!adminApiKey) return;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const orderParams = new URLSearchParams({ limit: "200" });
      const tradeParams = new URLSearchParams({ limit: "200" });
      if (auditProduct !== "all") {
        orderParams.set("product_type", auditProduct);
        tradeParams.set("product_type", auditProduct);
      }
      if (auditSymbol !== "all") {
        orderParams.set("symbol", auditSymbol);
        tradeParams.set("symbol", auditSymbol);
      }
      if (auditUserId !== "all") {
        orderParams.set("user_id", auditUserId);
        tradeParams.set("user_id", auditUserId);
      }
      if (auditDataScope !== "all") {
        orderParams.set("data_domain", auditDataScope);
        tradeParams.set("data_domain", auditDataScope);
      }
      orderParams.set("status", auditStatus);
      const [ordersData, tradesData] = await Promise.all([
        api.get<{ items: AdminOrderAuditItem[]; query?: AdminAuditQueryMeta }>(`/admin/orders?${orderParams.toString()}`, adminApiKey),
        api.get<{ items: AdminTradeAuditItem[]; query?: AdminAuditQueryMeta }>(`/admin/trades?${tradeParams.toString()}`, adminApiKey),
      ]);
      setAuditOrders(ordersData.items);
      setAuditTrades(tradesData.items);
      setAuditOrderQueryMeta(ordersData.query ?? null);
      setAuditTradeQueryMeta(tradesData.query ?? null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载订单与成交审计失败";
      setAuditOrderQueryMeta(null);
      setAuditTradeQueryMeta(null);
      setAuditError(message);
      pushToast("error", message);
    } finally {
      setAuditLoading(false);
    }
  }, [adminApiKey, auditDataScope, auditProduct, auditStatus, auditSymbol, auditUserId, pushToast]);

  useEffect(() => {
    if (lightweightLiquiditySection) return;
    const timer = window.setTimeout(() => void load(), 100);
    return () => window.clearTimeout(timer);
  }, [adminApiKey, lightweightLiquiditySection]);

  useEffect(() => {
    if (!adminApiKey || adminSection !== "accounting" || accountingEvidenceLoadedRef.current) return;
    accountingEvidenceLoadedRef.current = true;
    void Promise.all([
      api.get<RobotFinancialCheckpointPage>("/admin/accounting/robot-checkpoints?limit=20", adminApiKey),
      api.get<AccountingGateReadiness>("/admin/accounting/gate-readiness", adminApiKey),
    ]).then(([checkpoints, readiness]) => {
      setRobotFinancialCheckpoints(checkpoints);
      setAccountingGateReadiness(readiness);
    }).catch((error) => {
      accountingEvidenceLoadedRef.current = false;
      pushToast("error", error instanceof Error ? error.message : "账务证明加载失败");
    });
  }, [adminApiKey, adminSection, pushToast]);

  useEffect(() => {
    if (adminSection !== "orders") return;
    void loadOrderAudit();
  }, [adminSection, loadOrderAudit]);

  useEffect(() => {
    if (!adminApiKey || !shouldLoadContractDetailsForSection(adminSection)) return;
    if (contractDetailsLoaded || contractDetailsLoading) return;
    void refreshContractDetails();
  }, [adminApiKey, adminSection, contractDetailsLoaded, contractDetailsLoading, refreshContractDetails]);

  useEffect(() => {
    if (!adminApiKey || lightweightLiquiditySection) return;
    const selectedMarket = selectedMarketSymbol ? markets[selectedMarketSymbol] : undefined;
    const shouldPollContracts = adminSection === "contracts" || adminSection === "risk" || selectedMarket?.product_type === "PERP";
    if (!shouldPollContracts) return;
    const intervalId = window.setInterval(() => {
      void refreshContractLiveData();
    }, 10000);
    return () => window.clearInterval(intervalId);
  }, [adminApiKey, adminSection, selectedMarketSymbol, markets, refreshContractLiveData]);

  useEffect(() => {
    if (marketSymbols.length === 0) {
      return;
    }
    if (!selectedMarketSymbol || !marketSymbols.includes(selectedMarketSymbol)) {
      setSelectedMarketSymbol(marketSymbols[0]);
    }
  }, [marketSymbols, selectedMarketSymbol]);

  useEffect(() => {
    if (users.length === 0) {
      if (selectedUserId !== null) setSelectedUserId(null);
      return;
    }
    const scopedUsers = users.filter((user) => accountScopeIncludesUser(accountScope, user));
    if (scopedUsers.length === 0) {
      if (selectedUserId !== null) setSelectedUserId(null);
      return;
    }
    const selectedUserInScope = selectedUserId !== null && scopedUsers.some((user) => user.id === selectedUserId);
    if ((accountAdminView === "detail" || accountAdminView === "maintenance") && !selectedUserInScope) {
      setSelectedUserId(scopedUsers[0].id);
      return;
    }
    if (selectedUserId !== null && !selectedUserInScope) {
      setSelectedUserId(null);
    }
  }, [users, accountScope, accountAdminView, selectedUserId]);

  useEffect(() => {
    setAccountUserQuery("");
  }, [accountScope, accountAdminView]);

  const resetUser = async (userId: number) => {
    const user = users.find((item) => item.id === userId);
    const confirmed = window.confirm(`重置 ${user?.username ?? userId} 的测试现货资金？该操作会改写该 UID 现货钱包并写入账变。`);
    if (!confirmed) return;
    await runAccountAction(accountActionKey(userId, "reset"), "重置单 UID 现货资金", async () => {
      await runAdminOperation("重置单 UID 现货资金", `${user?.username ?? userId} 测试现货资金重置失败`, async () => {
        const result = await api.post(`/admin/users/${userId}/reset-balances`, { confirm_execute: true }, adminApiKey);
        await load();
        showOperationResult("重置单 UID 现货资金", `${user?.username ?? userId} 测试现货资金已重置`, result);
      }, { user_id: userId, username: user?.username });
    });
  };

  const resetAllTestUsers = async () => {
    const confirmed = window.confirm("批量重置测试主体资金？该操作会批量改写 trader/admin 等测试主体现货余额并写入账变。");
    if (!confirmed) return;
    await runAccountAction(accountActionKey("all", "reset-test-users"), "批量重置测试主体资金", async () => {
      await runAdminOperation("批量重置测试主体资金", "批量重置测试主体资金失败", async () => {
        const result = await api.post<{ count: number }>("/admin/reset-test-users", { confirm_execute: true }, adminApiKey);
        await load();
        showOperationResult("批量重置测试主体资金", `已重置 ${result.count} 个测试主体`, result);
      });
    });
  };

  const exportAccountDirectory = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const scope = accountScope.replace(/[^a-zA-Z0-9_-]+/g, "-");
    const botRows = Object.entries(marketBots).flatMap(([symbol, bots]) =>
      bots.map((bot) => ({
        symbol,
        uid: bot.uid,
        label: `${symbol}:${bot.role}:${bot.is_enabled ? "enabled" : "paused"}`,
      })),
    );
    downloadCsv(
      `admin_accounts_${scope}_${stamp}.csv`,
      [
        "user_id",
        "username",
        "account_kind",
        "role",
        "status",
        "password_status",
        "api_key_status",
        "api_secret_status",
        "spot_asset_count",
        "spot_usdt_available",
        "spot_usdt_frozen",
        "spot_balances",
        "contract_account_count",
        "contract_wallet_balance",
        "contract_available_margin",
        "contract_used_margin",
        "contract_unrealized_pnl",
        "active_contract_positions",
        "live_contract_orders",
	        "bot_binding_count",
        "bot_bindings",
        "primary_ledger_domain",
        "verification_path",
        "boundary_note",
        "spot_adjustment_policy",
        "spot_adjustment_pre_check",
        "spot_adjustment_post_check",
        "spot_adjustment_recovery_path",
        "spot_adjustment_boundary_note",
	      ],
	      accountUsersInScope.map((user) => {
	        const kind = accountUserKindForUser(user);
	        const review = accountDirectoryReview(kind);
	        const adjustmentPolicy = spotBalanceAdjustmentPolicy(kind);
	        const usdt = user.balances.find((item) => item.asset === "USDT");
	        const userContractAccounts = contractAccounts.filter((item) => item.user.id === user.id || item.account.user_id === user.id);
        const userContractPositions = contractPositions.filter((item) => item.user.id === user.id);
        const userContractOrders = contractOrders.filter((item) => item.user.id === user.id);
        const activeContractPositions = userContractPositions.filter((item) => item.position.side !== "flat" && Number(item.position.quantity || 0) > 0);
        const liveContractOrders = userContractOrders.filter((item) => isLiveOrderStatus(item.order.status));
        const userBotLabels = botRows.filter((item) => item.uid === user.id).map((item) => item.label);
        return [
          user.id,
          user.username,
          accountUserKindLabel(kind),
          user.role,
          user.is_active ? "active" : "paused",
          user.has_password ? "present" : "missing",
          user.api_key ? "present" : "missing",
          user.api_secret_present ?? Boolean(user.api_secret) ? "present" : "missing",
          user.balances.length,
          usdt?.available ?? "",
          usdt?.frozen ?? "",
          user.balances.map((item) => `${item.asset}:available=${item.available};frozen=${item.frozen}`).join(" | "),
          userContractAccounts.length,
          sumDecimalStrings(userContractAccounts.map((item) => item.account.wallet_balance)),
          sumDecimalStrings(userContractAccounts.map((item) => item.account.available_margin)),
          sumDecimalStrings(userContractAccounts.map((item) => item.account.used_margin)),
          sumDecimalStrings(userContractAccounts.map((item) => item.account.unrealized_pnl)),
          activeContractPositions.length,
	          liveContractOrders.length,
	          userBotLabels.length,
	          userBotLabels.join(" | "),
	          review.primaryLedger,
	          review.verificationPath,
	          review.boundary,
	          adjustmentPolicy.policy,
	          adjustmentPolicy.preCheck,
	          adjustmentPolicy.postCheck,
	          adjustmentPolicy.recoveryPath,
	          adjustmentPolicy.boundary,
	        ];
	      }),
	    );
  };

  const createUser = async () => {
    const username = newUser.username.trim();
    if (!username) {
      pushToast("error", "请输入 username");
      return;
    }
    if (!newUser.password) {
      pushToast("error", "请输入 password");
      return;
    }
    const initialBalances: Record<string, string> = {};
    if (newUser.usdt) initialBalances.USDT = newUser.usdt;
    marketSymbols.forEach((symbol) => {
      const market = markets[symbol];
      if (market?.product_type !== "SPOT") return;
      const base = market.base_asset;
      if (base && base !== "USDT" && newUser.baseAmount) initialBalances[base] = newUser.baseAmount;
    });
    const balanceSummary = Object.entries(initialBalances)
      .map(([asset, amount]) => `${asset}: ${amount}`)
      .join("\n") || "无初始现货余额";
    const onboardingBoundary = accountOnboardingRoleBoundaryCopy(newUser.role);
    const confirmed = window.confirm(
      `确认创建登录主体 ${username}？\n\n` +
      `账户类型：${onboardingBoundary.title}\n` +
      `维护口径：${onboardingBoundary.confirmBoundary}\n\n` +
      `角色：${newUser.role}\n初始现货模板：\n${balanceSummary}\n\n` +
      `全市场费率：maker ${newUser.makerFeeRate || "-"} / taker ${newUser.takerFeeRate || "-"}\n\n` +
      "该操作会创建登录主体、生成 API Key/Secret，并写入现货初始余额模板。",
    );
    if (!confirmed) return;
    await runAccountAction(accountActionKey("new", "create"), "创建登录主体", async () => {
      await runAdminOperation("创建登录主体", `${username} 创建登录主体失败`, async () => {
        await api.post(
          "/admin/users",
          {
            username,
            password: newUser.password,
            role: newUser.role,
            initial_balances: initialBalances,
            maker_fee_rate: newUser.makerFeeRate,
            taker_fee_rate: newUser.takerFeeRate,
          },
          adminApiKey,
        );
        setNewUser((current) => ({ ...current, username: "", password: "" }));
        await load();
        showOperationResult("创建登录主体", `${username} 登录主体已创建`, { ok: true, username, role: newUser.role });
      }, { username, role: newUser.role });
    });
  };

  const updateUserEdit = (user: AdminUser, patch: Partial<UserEditState>) => {
    setUserEdits((current) => ({
      ...current,
      [user.id]: {
        ...(current[user.id] ?? defaultUserEdit(user)),
        ...patch,
      },
    }));
  };

  const updateUserMarketFeeEdit = (user: AdminUser, symbol: string, patch: Partial<MarketFeeEdit>) => {
    setUserEdits((current) => {
      const base = current[user.id] ?? defaultUserEdit(user);
      const currentFee = base.marketFees[symbol] ?? {
        makerFeeRate: markets[symbol]?.default_maker_fee_rate ?? "",
        takerFeeRate: markets[symbol]?.default_taker_fee_rate ?? "",
      };
      return {
        ...current,
        [user.id]: {
          ...base,
          marketFees: {
            ...base.marketFees,
            [symbol]: {
              ...currentFee,
              ...patch,
            },
          },
        },
      };
    });
  };

  const updateBalanceAdjustment = (user: AdminUser, patch: Partial<BalanceAdjustmentState>) => {
    setBalanceAdjustments((current) => ({
      ...current,
      [user.id]: {
        ...(current[user.id] ?? defaultBalanceAdjustment(user)),
        ...patch,
      },
    }));
  };

  const saveUser = async (user: AdminUser) => {
    const edit = userEdits[user.id] ?? defaultUserEdit(user);
    const changes: string[] = [];
    if (edit.role !== user.role) changes.push(`角色：${user.role} -> ${edit.role}`);
    if (edit.isActive !== user.is_active) changes.push(`登录/API：${user.is_active ? "active" : "paused"} -> ${edit.isActive ? "active" : "paused"}`);
    if (edit.password) changes.push("登录密码：将重置为新密码");
    if (changes.length > 0) {
      const confirmed = window.confirm(`确认保存 ${user.username} 的身份与凭据维护？\n\n${changes.join("\n")}\n\n该操作会影响登录、API 调用或账户角色。`);
      if (!confirmed) return;
    }
    const payload: Record<string, string | boolean> = {
      role: edit.role,
      is_active: edit.isActive,
    };
    if (edit.password) payload.password = edit.password;
    await runAccountAction(accountActionKey(user.id, "identity"), "保存主体身份", async () => {
      await runAdminOperation("保存主体身份", `${user.username} 主体身份维护失败`, async () => {
        await api.put(`/admin/users/${user.id}`, payload, adminApiKey);
        await load();
        showOperationResult("保存主体身份", `${user.username} 主体身份维护已保存`, { ok: true, user_id: user.id, username: user.username });
      }, { user_id: user.id, username: user.username });
    });
  };

  const saveUserFees = async (user: AdminUser) => {
    const edit = userEdits[user.id] ?? defaultUserEdit(user);
    const kind = accountUserKindForUser(user);
    const feePolicy = accountFeePolicyCopy(kind);
    const confirmed = window.confirm(
      `确认保存 ${user.username} 的全市场 UID 手续费？\n\n` +
      `账户分类：${accountUserKindLabel(kind)}\n` +
      `维护口径：${feePolicy.confirmBoundary}\n` +
      `maker：${edit.makerFeeRate || "-"}\n` +
      `taker：${edit.takerFeeRate || "-"}\n` +
      `影响范围：当前 ${marketSymbols.length} 个市场的 UID 覆盖费率\n\n` +
      "该操作只影响后续成交手续费，不回算历史成交、现货流水、合约 total_fees 或 PnL。",
    );
    if (!confirmed) return;
    await runAccountAction(accountActionKey(user.id, "fees-all"), "保存全市场费率", async () => {
      await runAdminOperation("保存全市场费率", `${user.username} 全市场 UID 手续费保存失败`, async () => {
        const result = await api.put(
          `/admin/users/${user.id}/fees-all`,
          { maker_fee_rate: edit.makerFeeRate, taker_fee_rate: edit.takerFeeRate },
          adminApiKey,
        );
        await load();
        showOperationResult("保存全市场费率", `${user.username} 全市场 UID 手续费已更新`, result);
      }, {
        user_id: user.id,
        username: user.username,
        account_kind: kind,
        market_count: marketSymbols.length,
        maker_fee_rate: edit.makerFeeRate,
        taker_fee_rate: edit.takerFeeRate,
      });
    });
  };

  const saveUserMarketFee = async (user: AdminUser, symbol: string) => {
    const edit = userEdits[user.id] ?? defaultUserEdit(user);
    const fee = edit.marketFees[symbol];
    if (!fee) return;
    const kind = accountUserKindForUser(user);
    const feePolicy = accountFeePolicyCopy(kind);
    const confirmed = window.confirm(
      `确认保存 ${user.username} 在 ${symbol} 的 UID 手续费？\n\n` +
      `账户分类：${accountUserKindLabel(kind)}\n` +
      `维护口径：${feePolicy.confirmBoundary}\n` +
      `maker：${fee.makerFeeRate || "-"}\n` +
      `taker：${fee.takerFeeRate || "-"}\n\n` +
      "该操作只覆盖这个 UID + 市场的后续成交费率，不修改市场默认费率、不回算历史费用。",
    );
    if (!confirmed) return;
    await runAccountAction(accountActionKey(user.id, "fee", symbol), `保存 ${symbol} 单市场费率`, async () => {
      await runAdminOperation("保存单市场费率", `${user.username} ${symbol} UID 手续费保存失败`, async () => {
        const result = await api.put(
          `/admin/users/${user.id}/fees?symbol=${encodeURIComponent(symbol)}`,
          { maker_fee_rate: fee.makerFeeRate, taker_fee_rate: fee.takerFeeRate },
          adminApiKey,
        );
        await load();
        showOperationResult("保存单市场费率", `${user.username} ${symbol} UID 手续费已更新`, result);
      }, {
        user_id: user.id,
        username: user.username,
        account_kind: kind,
        symbol,
        maker_fee_rate: fee.makerFeeRate,
        taker_fee_rate: fee.takerFeeRate,
      });
    });
  };

  const toggleUserActivity = async (user: AdminUser) => {
    if (activeActivityUserId === user.id) {
      setActiveActivityUserId(null);
      return;
    }
    setActiveActivityUserId(user.id);
    if (userActivities[user.id]) return;
    try {
      setLoadingActivityUserId(user.id);
      const data = await api.get<AdminUserActivity>(`/admin/users/${user.id}/activity?limit=20`, adminApiKey);
      setUserActivities((current) => ({ ...current, [user.id]: data }));
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "加载 UID 活动失败");
    } finally {
      setLoadingActivityUserId(null);
    }
  };

  const rotateUserApiKey = async (user: AdminUser) => {
    const confirmed = window.confirm(`轮换 ${user.username} 的 API Key？旧 Key 会失效，正在使用该 Key 的机器人或脚本需要更新。`);
    if (!confirmed) return;
    await runAccountAction(accountActionKey(user.id, "rotate-api-key"), "轮换 API Key", async () => {
      await runAdminOperation("轮换 API Key", `${user.username} API key 轮换失败`, async () => {
        const response = await api.post<{ user: AdminUser }>(`/admin/users/${user.id}/rotate-api-key`, undefined, adminApiKey);
        if (authSession?.user_id === user.id) {
          setAuthSession({
            ...authSession,
            api_key: response.user.api_key,
            api_secret: response.user.api_secret,
          });
        } else {
          await load();
        }
        await refreshAdminOperations();
        showOperationResult("轮换 API Key", `${user.username} API key 已轮换`, { ok: true, user_id: user.id, username: user.username });
      }, { user_id: user.id, username: user.username });
    });
  };

  const adjustUser = async (user: AdminUser) => {
    const adjustment = balanceAdjustments[user.id] ?? defaultBalanceAdjustment(user);
    if (!adjustment.asset || !adjustment.amount || !adjustment.reason.trim()) {
      pushToast("error", "调账需要资产、金额和原因");
      return;
    }
    const confirmed = window.confirm(
      `确认给 ${user.username} 调整 ${adjustment.asset} ${adjustment.amount}？该操作会立即写入余额和资金流水。`,
    );
    if (!confirmed) return;
    await runAccountAction(accountActionKey(user.id, "adjust-balance"), "现货余额调账", async () => {
      await runAdminOperation("现货余额调账", `${user.username} ${adjustment.asset} 调账失败`, async () => {
        const result = await api.post(
          `/admin/users/${user.id}/adjust-balance`,
          { asset: adjustment.asset, amount: adjustment.amount, reason: adjustment.reason.trim() },
          adminApiKey,
        );
        await load();
        showOperationResult("现货余额调账", `${user.username} ${adjustment.asset} 已调账 ${adjustment.amount}`, result);
      }, { user_id: user.id, username: user.username, asset: adjustment.asset, amount: adjustment.amount });
    });
  };

  const createMarket = async () => {
    const marketSymbol = normalizeMarketSymbolInput(newMarket.symbol);
    if (!marketSymbol) {
      pushToast("error", "请先输入币对");
      return;
    }
    if (marketSymbols.includes(marketSymbol)) {
      pushToast("error", `${marketSymbol} 市场已存在`);
      return;
    }
    await runAdminOperation("创建交易币对", `${marketSymbol} 市场创建失败`, async () => {
      const result = await api.post(
        "/admin/markets",
        {
          symbol: marketSymbol,
          product_type: newMarket.productType,
          default_maker_strategy: newMarket.defaultMakerStrategy || undefined,
          price_source_symbol: newMarket.priceSourceSymbol || marketSymbol.replace(/-PERP$/, ""),
          market_type: newMarket.marketType,
          base_asset: newMarket.baseAsset,
          quote_asset: newMarket.quoteAsset,
          margin_asset: newMarket.productType === "PERP" ? newMarket.marginAsset : undefined,
          price_tick: newMarket.priceTick,
          qty_step: newMarket.qtyStep,
          min_qty: newMarket.minQty,
          min_notional: newMarket.minNotional,
          max_leverage: newMarket.productType === "PERP" ? newMarket.maxLeverage : "1",
          default_leverage: newMarket.productType === "PERP" ? newMarket.defaultLeverage : "1",
          maintenance_margin_rate: newMarket.productType === "PERP" ? newMarket.maintenanceMarginRate : "0",
          funding_rate: newMarket.productType === "PERP" ? newMarket.fundingRate : "0",
          funding_interval_hours: Number(newMarket.fundingIntervalHours),
          index_price_source: newMarket.productType === "PERP" ? newMarket.indexPriceSource : "manual",
          mark_price_mode: newMarket.productType === "PERP" ? newMarket.markPriceMode : "orderbook",
          funding_rate_mode: newMarket.productType === "PERP" ? newMarket.fundingRateMode : "formula",
          funding_interest_rate: newMarket.productType === "PERP" ? newMarket.fundingInterestRate : "0.0001",
          funding_clamp_rate: newMarket.productType === "PERP" ? newMarket.fundingClampRate : "0.0005",
          funding_cap_rate: newMarket.productType === "PERP" ? newMarket.fundingCapRate : "0.02",
          funding_impact_notional: newMarket.productType === "PERP" ? newMarket.fundingImpactNotional : "25000",
          default_maker_fee_rate: newMarket.defaultMakerFeeRate,
          default_taker_fee_rate: newMarket.defaultTakerFeeRate,
          reference_price: newMarket.referencePrice || undefined,
          create_default_bots: false,
          default_bot_count: Number(newMarket.defaultBotCount),
          default_bot_initial_quote_amount: newMarket.defaultBotInitialQuoteAmount,
          default_bot_initial_base_notional: newMarket.defaultBotInitialBaseNotional,
          is_active: newMarket.isActive,
        },
        adminApiKey,
      );
      await load();
      showOperationResult("创建交易币对", `${marketSymbol} 市场已创建`, result, "success", undefined, {
        symbol: marketSymbol,
        product_type: newMarket.productType,
      });
    }, { symbol: marketSymbol, product_type: newMarket.productType });
  };

  const applyMarketTemplate = (template: MarketTemplate) => {
    setNewMarket((current) => ({
      ...current,
      symbol: template.symbol,
      productType: template.product_type ?? "SPOT",
      marketType: template.market_type,
      baseAsset: template.base_asset,
      quoteAsset: template.quote_asset,
      marginAsset: template.margin_asset ?? template.quote_asset,
      priceTick: template.price_tick,
      qtyStep: template.qty_step,
      minQty: template.min_qty,
      minNotional: template.min_notional,
      maxLeverage: template.max_leverage ?? current.maxLeverage,
      defaultLeverage: template.default_leverage ?? current.defaultLeverage,
      maintenanceMarginRate: template.maintenance_margin_rate ?? current.maintenanceMarginRate,
      fundingRate: template.funding_rate ?? current.fundingRate,
      fundingIntervalHours: String(template.funding_interval_hours ?? current.fundingIntervalHours),
      indexPriceSource: template.index_price_source ?? current.indexPriceSource,
      markPriceMode: template.mark_price_mode ?? current.markPriceMode,
      fundingRateMode: template.funding_rate_mode ?? current.fundingRateMode,
      fundingInterestRate: template.funding_interest_rate ?? current.fundingInterestRate,
      fundingClampRate: template.funding_clamp_rate ?? current.fundingClampRate,
      fundingCapRate: template.funding_cap_rate ?? current.fundingCapRate,
      fundingImpactNotional: template.funding_impact_notional ?? current.fundingImpactNotional,
      defaultMakerFeeRate: template.default_maker_fee_rate,
      defaultTakerFeeRate: template.default_taker_fee_rate,
      referencePrice: template.reference_price ?? current.referencePrice,
      createDefaultBots: (template.product_type ?? "SPOT") === "SPOT",
    }));
  };

  const updateMarketBotForm = (symbol: string, patch: Partial<MarketBotFormState>) => {
    setMarketBotForms((current) => ({
      ...current,
      [symbol]: {
        ...(current[symbol] ?? defaultMarketBotForm(markets[symbol])),
        ...patch,
      },
    }));
  };

  const createMarketBot = async (symbol: string) => {
    const market = markets[symbol];
    const form = marketBotForms[symbol] ?? defaultMarketBotForm(markets[symbol]);
    if (!form.referencePrice && !form.initialBaseAmount) {
      pushToast("error", "需要参考价格或直接输入 base 数量");
      return;
    }
    const confirmed = window.confirm(
      `确认创建 ${symbol} 机器人账号？\n\n`
      + `身份：${form.uid ? `绑定已有 UID ${form.uid}` : (form.username || "自动生成用户名")} / ${form.botLabel || "自动生成标签"}\n`
      + `角色：${form.role} / ${form.strategyRole || form.role}\n`
      + `初始模板：quote ${form.initialQuoteAmount || "-"}，base notional ${form.initialBaseNotional || "-"}，参考价 ${form.referencePrice || "-"}\n\n`
      + `${marketBotTemplateBoundary(market)}\n\n`
      + `${marketBotOperationBoundary(market)}\n\n`
      + "该操作会创建或绑定 mm_bot 登录主体，并可能生成或更新 API Key / Secret。",
    );
    if (!confirmed) return;
    await runAdminOperation("创建机器人账号", `${symbol} 机器人账号创建失败`, async () => {
      const response = await api.post(
        `/admin/markets/${symbol}/bots`,
        {
          uid: form.uid ? Number(form.uid) : undefined,
          username: form.username || undefined,
          password: form.password || undefined,
          api_key: form.apiKey || undefined,
          api_secret: form.apiSecret || undefined,
          bot_label: form.botLabel || undefined,
          role: form.role,
          strategy_role: form.strategyRole || form.role,
          reference_price: form.referencePrice || undefined,
          initial_quote_amount: form.initialQuoteAmount,
          initial_base_notional: form.initialBaseNotional,
          initial_base_amount: form.initialBaseAmount || undefined,
          is_enabled: form.isEnabled,
        },
        adminApiKey,
      );
      await load();
      setMarketBotForms((current) => ({ ...current, [symbol]: defaultMarketBotForm(markets[symbol]) }));
      showOperationResult("创建机器人账号", `${symbol} 机器人账号已创建`, response);
    }, { symbol, role: form.role, product_type: market?.product_type });
  };

  const createDefaultMarketBots = async (symbol: string) => {
    const market = markets[symbol];
    const form = marketBotForms[symbol] ?? defaultMarketBotForm(market);
    const confirmed = window.confirm(
      `确认为 ${symbol} 补齐默认 maker 机器人账号？\n\n`
      + `目标数量：2\n初始模板：quote ${form.initialQuoteAmount || "-"}，base notional ${form.initialBaseNotional || "-"}，参考价 ${form.referencePrice || market?.reference_price || "-"}\n\n`
      + `${marketBotTemplateBoundary(market)}\n\n`
      + `${marketBotOperationBoundary(market)}\n\n`
      + "该操作会为缺失的默认机器人创建 mm_bot、API 凭据和市场绑定。",
    );
    if (!confirmed) return;
    await runAdminOperation("补默认机器人账号", `${symbol} 默认机器人账号补齐失败`, async () => {
      const response = await api.post(
        `/admin/markets/${symbol}/bots/defaults`,
        {
          bot_count: 2,
          reference_price: form.referencePrice || market?.reference_price || undefined,
          initial_quote_amount: form.initialQuoteAmount,
          initial_base_notional: form.initialBaseNotional,
        },
        adminApiKey,
      );
      await load();
      showOperationResult("补默认机器人账号", `${symbol} 默认机器人账号已补齐`, response);
    }, { symbol, role: "maker", product_type: market?.product_type });
  };

  const createDefaultFlowBot = async (symbol: string) => {
    const market = markets[symbol];
    const form = marketBotForms[symbol] ?? defaultMarketBotForm(market);
    const confirmed = window.confirm(
      `确认为 ${symbol} 补 FLOW 机器人账号？\n\n`
      + `初始模板：quote ${form.initialQuoteAmount || "-"}，base notional ${form.initialBaseNotional || "-"}，参考价 ${form.referencePrice || market?.reference_price || "-"}\n\n`
      + "FLOW 只服务现货本地沙盒成交流，不使用合约保证金、资金费、强平或保险基金。\n\n"
      + "不会启动策略实例、不会调账或清算、不会改变普通客户完整记录口径。",
    );
    if (!confirmed) return;
    await runAdminOperation("补 FLOW 机器人账号", `${symbol} FLOW 机器人账号补齐失败`, async () => {
      const response = await api.post<{ created: boolean }>(
        `/admin/markets/${symbol}/bots/default-flow`,
        {
          reference_price: form.referencePrice || market?.reference_price || undefined,
          initial_quote_amount: form.initialQuoteAmount,
          initial_base_notional: form.initialBaseNotional,
          is_enabled: true,
        },
        adminApiKey,
      );
      await load();
      showOperationResult("补 FLOW 机器人账号", response.created ? `${symbol} FLOW 机器人账号已创建` : `${symbol} 已有 FLOW 机器人账号`, response);
    }, { symbol, role: "flow", product_type: market?.product_type });
  };

  const updateMarketBot = async (symbol: string, botId: number, form: MarketBotEditState) => {
    const market = markets[symbol];
    const bot = (marketBots[symbol] ?? []).find((item) => item.id === botId);
    if (!form.referencePrice && !form.initialBaseAmount) {
      pushToast("error", "需要参考价格或直接输入 base 数量");
      return;
    }
    const isPerp = market?.product_type === "PERP";
    const changeGroups = bot ? marketBotEditChangeGroups(bot, form, isPerp) : ["配置字段"];
    if (changeGroups.length === 0) {
      pushToast("success", `${symbol} 机器人账号没有需要保存的变更`);
      return;
    }
    const confirmed = window.confirm(
      `确认保存 ${symbol} 机器人账号配置？\n\n`
      + `账号：${bot?.username ?? `bot_id ${botId}`} / UID ${bot?.uid ?? "-"}\n`
      + `变更分区：${changeGroups.join("、")}\n`
      + `角色：${form.role} / ${form.strategyRole || form.role}；状态：${form.isEnabled ? "enabled" : "paused"}\n\n`
      + `${marketBotTemplateBoundary(market)}\n\n`
      + `${marketBotOperationBoundary(market)}\n\n`
      + "该操作可能修改登录主体、API 凭据、策略绑定、资金模板或机器人启停状态。",
    );
    if (!confirmed) return;
    await runAdminOperation("保存机器人账号", `${symbol} 机器人账号保存失败`, async () => {
      const response = await api.put(`/admin/markets/${symbol}/bots/${botId}`, marketBotEditPayload(form), adminApiKey);
      await load();
      showOperationResult("保存机器人账号", `${symbol} 机器人账号已保存`, response);
    }, { symbol, bot_id: botId, uid: bot?.uid, role: form.role, changed: changeGroups });
  };

  const updateSeedBook = (symbol: string, patch: Partial<SeedBookState>) => {
    setSeedBookForms((current) => ({
      ...current,
      [symbol]: {
        ...(current[symbol] ?? defaultSeedBook(markets[symbol], marketSurveillance[symbol])),
        ...patch,
      },
    }));
  };

  const seedMarketBook = async (symbol: string) => {
    const seed = seedBookForms[symbol] ?? defaultSeedBook(markets[symbol], marketSurveillance[symbol]);
    const isContract = markets[symbol]?.product_type === "PERP";
    if (!seed.midPrice) {
      pushToast("error", `${symbol} 需要中间价`);
      return;
    }
    const confirmed = window.confirm(
      `${symbol} 将真实${seed.cancelExisting ? "撤销当前挂单并" : ""}铺设${isContract ? "合约" : "测试"}盘口，可能改变订单簿和${isContract ? "合约保证金" : "账户冻结资金"}。确认继续？`,
    );
    if (!confirmed) {
      return;
    }
    await runAdminOperation(`${isContract ? "合约" : "现货"}盘口初始化`, `${symbol} ${isContract ? "合约盘口" : "测试盘口"}初始化失败`, async () => {
      const result = await api.post(
        isContract ? `/admin/contracts/markets/${encodeURIComponent(symbol)}/seed-book` : `/admin/markets/${encodeURIComponent(symbol)}/seed-book`,
        {
          mid_price: seed.midPrice,
          levels: Number(seed.levels),
          gap_ticks: Number(seed.gapTicks),
          quantity: seed.quantity || undefined,
          cancel_existing: seed.cancelExisting,
          confirm_execute: true,
        },
        adminApiKey,
      );
      await load();
      showOperationResult(`${isContract ? "合约" : "现货"}盘口初始化`, `${symbol} ${isContract ? "合约盘口" : "测试盘口"}已初始化`, result);
    }, { symbol, product_type: isContract ? "PERP" : "SPOT" });
  };

  const updateSweepPreview = (symbol: string, patch: Partial<SweepPreviewState>) => {
    setSweepPreviewForms((current) => ({
      ...current,
      [symbol]: {
        ...(current[symbol] ?? defaultSweepPreview()),
        ...patch,
      },
    }));
  };

  const previewSweep = async (symbol: string) => {
    const preview = sweepPreviewForms[symbol] ?? defaultSweepPreview();
    const body =
      preview.mode === "quote_amount"
        ? { side: preview.side, quote_amount: preview.quoteAmount, depth: Number(preview.depth) }
        : { side: preview.side, quantity: preview.quantity, depth: Number(preview.depth) };
    const result = await api.post<SweepPreviewResult>(`/admin/markets/${symbol}/sweep-preview`, body, adminApiKey);
    setSweepPreviews((current) => ({ ...current, [symbol]: result }));
  };

  const toggleMarket = async (symbol: string) => {
    const market = markets[symbol];
    if (!market) return;
    const active = !market.is_active;
    const title = active ? "恢复市场" : "暂停市场";
    const confirmed = window.confirm(
      active
        ? `恢复 ${symbol} 市场？\n\n恢复后新的下单请求会重新进入撮合校验；该操作不会自动启动机器人实例、铺盘、撤单、调账或清算。`
        : `暂停 ${symbol} 市场？\n\n暂停后新的下单请求会被拒绝；该操作不会撤销当前挂单、停止机器人实例、调账或清算。`,
    );
    if (!confirmed) return;
    await runAdminOperation(title, `${symbol} 市场状态切换失败`, async () => {
      const result = await api.put(`/admin/markets/${symbol}`, { is_active: active }, adminApiKey);
      await load();
      showOperationResult(title, `${symbol} 已${active ? "恢复" : "暂停"}`, result, "success", undefined, {
        symbol,
        is_active: active,
      });
    }, { symbol, is_active: active });
  };

  const updateMarketField = (symbol: string, field: keyof AdminMarketItem, value: string | number | boolean) => {
    setMarkets((current) => ({
      ...current,
      [symbol]: (() => {
        const nextMarket = {
          ...current[symbol],
          [field]: value,
        } as AdminMarketItem;
        if (field === "price_tick" || field === "qty_step" || field === "min_qty") {
          const derived = deriveMarketPrecisions(nextMarket);
          nextMarket.price_precision = derived.pricePrecision;
          nextMarket.qty_precision = derived.qtyPrecision;
        }
        return nextMarket;
      })(),
    }));
  };

  const revertMarketFields = (symbol: string, fields: MarketEditField[]) => {
    const persisted = persistedMarkets[symbol];
    if (!persisted || fields.length === 0) return;
    setMarkets((current) => {
      const existing = current[symbol];
      if (!existing) return current;
      const patch = Object.fromEntries(fields.map((item) => [item.field, persisted[item.field]])) as Partial<AdminMarketItem>;
      const nextMarket = { ...existing, ...patch };
      const derived = deriveMarketPrecisions(nextMarket);
      return {
        ...current,
        [symbol]: {
          ...nextMarket,
          price_precision: derived.pricePrecision,
          qty_precision: derived.qtyPrecision,
        },
      };
    });
  };

  const saveMarketConfig = async (symbol: string) => {
    const market = markets[symbol];
    if (!market) return;
    const derived = deriveMarketPrecisions(market);
    const nextContractMode = market.product_type === "PERP" ? normalizeContractTradingMode(market.contract_trading_mode) : "normal";
    const previousContractMode = persistedMarketModes[symbol] ?? nextContractMode;
    const contractModeChanged = market.product_type === "PERP" && previousContractMode !== nextContractMode;
    if (contractModeChanged) {
      const confirmed = window.confirm(contractTradingModeConfirmMessage(symbol, previousContractMode, nextContractMode));
      if (!confirmed) return;
    }
    const payload = {
      product_type: market.product_type,
      market_type: market.market_type,
      margin_asset: market.margin_asset || undefined,
      price_tick: market.price_tick,
      qty_step: market.qty_step,
      min_qty: market.min_qty,
      min_notional: market.min_notional,
      max_leverage: market.max_leverage,
      default_leverage: market.default_leverage,
      maintenance_margin_rate: market.maintenance_margin_rate,
      funding_rate: market.funding_rate,
      funding_interval_hours: market.funding_interval_hours,
      index_price_source: market.index_price_source,
      mark_price_mode: market.mark_price_mode,
      funding_rate_mode: market.funding_rate_mode,
      funding_interest_rate: market.funding_interest_rate,
      funding_clamp_rate: market.funding_clamp_rate,
      funding_cap_rate: market.funding_cap_rate,
      funding_impact_notional: market.funding_impact_notional,
      contract_trading_mode: nextContractMode,
      reference_price: market.reference_price || undefined,
      price_precision: derived.pricePrecision,
      qty_precision: derived.qtyPrecision,
    };
    if (contractModeChanged) {
      await runAdminOperation("切换合约交易模式", `${symbol} 合约交易模式切换失败`, async () => {
        const result = await api.put(`/admin/markets/${symbol}`, payload, adminApiKey);
        await load();
        showOperationResult(
          "切换合约交易模式",
          `${symbol} 已切换为${contractTradingModeLabel(nextContractMode)}`,
          { ok: true, symbol, previous_mode: previousContractMode, next_mode: nextContractMode, result },
        );
      }, { symbol, previous_mode: previousContractMode, next_mode: nextContractMode });
      return;
    }
    await runAdminOperation("保存市场参数", `${symbol} 市场参数保存失败`, async () => {
      const result = await api.put(`/admin/markets/${symbol}`, payload, adminApiKey);
      await load();
      showOperationResult("保存市场参数", `${symbol} 市场参数已更新`, { ok: true, symbol, result }, "success", undefined, {
        symbol,
        product_type: market.product_type,
      });
    }, { symbol, product_type: market.product_type });
  };

  const refreshContractMarketState = async (symbol: string) => {
    await api.post<{ ok: boolean; state: ContractPriceState }>(
      `/admin/contracts/market-states/${encodeURIComponent(symbol)}/refresh`,
      undefined,
      adminApiKey,
    );
    await load();
    pushToast("success", `${symbol} 指数价/标记价已刷新`);
  };

  const settleContractFunding = async (symbol: string) => {
    const confirmed = window.confirm(`按当前资金费率结算 ${symbol} 的所有合约持仓？该操作会改写合约保证金钱包余额并写入资金费记录。`);
    if (!confirmed) return;
    await runAdminOperation("资金费结算", `${symbol} 资金费结算失败`, async () => {
      const result = await api.post<{ settled_count: number; funding_rate: string; already_settled?: boolean }>(
        `/admin/contracts/funding/${encodeURIComponent(symbol)}/settle`,
        { confirm_execute: true },
        adminApiKey,
      );
      await load();
      showOperationResult(
        "资金费结算",
        result.already_settled
          ? `${symbol} 当前资金费周期已结算，未重复扣款。`
          : `${symbol} 资金费率已结算 ${result.settled_count} 笔，费率 ${result.funding_rate}`,
        result,
      );
    }, { symbol });
  };

  const retryContractFundingJob = async (job: ContractFundingJob) => {
    const confirmed = window.confirm(`立即重试 ${job.symbol} ${bjDateTime(job.funding_time)} 的资金费任务？已有 settlement 的周期不会重复扣款。`);
    if (!confirmed) return;
    await runAdminOperation("资金费任务重试", `${job.symbol} 资金费任务重试失败`, async () => {
      const result = await api.post<ContractFundingJobRetryResult>(
        `/admin/contracts/funding/jobs/${encodeURIComponent(job.job_id)}/retry`,
        { confirm_execute: true },
        adminApiKey,
      );
      await load();
      if (result.error) {
        showOperationResult("资金费任务重试", `${job.symbol} 资金费任务重试失败：${result.error}`, result, "error");
        return;
      }
      showOperationResult(
        "资金费任务重试",
        result.result?.already_settled
          ? `${job.symbol} 资金费周期已有 settlement，任务已对齐为已结算。`
          : `${job.symbol} 资金费任务已重试，状态 ${result.job.status}，尝试 ${result.job.attempt_count}/${result.job.max_attempts}`,
        result,
      );
    }, { symbol: job.symbol, job_id: job.job_id });
  };

  const retryFailedContractFundingJobs = async () => {
    const failedCount = contractFundingJobs.filter((job) => job.status === "failed").length;
    const confirmed = window.confirm(`立即批量重试当前资金费任务队列中的 ${failedCount} 条 failed 任务？已有 settlement 的周期不会重复扣款。`);
    if (!confirmed) return;
    setBatchRetryingFundingJobs(true);
    try {
      await runAdminOperation("资金费批量重试", "资金费批量重试失败", async () => {
        const result = await api.post<ContractFundingJobBatchRetryResult>(
          "/admin/contracts/funding/jobs/retry-failed?limit=50",
          { confirm_execute: true },
          adminApiKey,
        );
        await load();
        if (result.failed_count > 0) {
          showOperationResult("资金费批量重试", `资金费批量重试完成：成功 ${result.succeeded_count}，失败 ${result.failed_count}`, result, "error");
          return;
        }
        showOperationResult("资金费批量重试", `资金费批量重试完成：成功 ${result.succeeded_count}/${result.requested_count}`, result);
      }, { failed_count: failedCount });
    } finally {
      setBatchRetryingFundingJobs(false);
    }
  };

  const adjustContractInsuranceFund = async () => {
    const amount = insuranceAdjustAmount.trim();
    if (!amount || Number(amount) === 0 || Number.isNaN(Number(amount))) {
      pushToast("error", "保险基金调账金额必须是非零数字");
      return;
    }
    const confirmed = window.confirm(`调整 USDT 保险基金 ${amount}？该操作会改写保险基金余额并写入保险基金流水。`);
    if (!confirmed) return;
    setInsuranceAdjusting(true);
    try {
      await runAdminOperation("保险基金调账", `USDT 保险基金调整 ${amount} 失败`, async () => {
        const result = await api.post<{ fund: ContractInsuranceFund; event: ContractInsuranceEvent }>(
          "/admin/contracts/insurance-funds/adjust",
          {
            margin_asset: "USDT",
            amount,
            reason: "admin insurance fund adjustment",
            confirm_execute: true,
          },
          adminApiKey,
        );
        setInsuranceAdjustAmount("0");
        await load();
        showOperationResult("保险基金调账", `USDT 保险基金已调整 ${amount}，余额 ${result.fund.balance}`, result);
      }, { margin_asset: "USDT", amount });
    } finally {
      setInsuranceAdjusting(false);
    }
  };

  const executeContractAdl = async (event: ContractLiquidationEvent) => {
    const residual = Number(event.adl_residual || event.residual_bad_debt || 0);
    if (residual <= 0) {
      pushToast("error", "该强平事件没有待处理 ADL 剩余坏账");
      return;
    }
    const confirmed = window.confirm(`对 ${event.symbol} 强平事件 ${event.event_id} 执行 ADL？将按盈利率和有效杠杆排序减掉盈利对手方仓位。`);
    if (!confirmed) return;
    setAdlExecutingEventId(event.event_id);
    try {
      await runAdminOperation("执行 ADL", `${event.symbol} ADL 执行失败`, async () => {
        const result = await api.post<{ status: string; covered_amount: string; residual_after: string }>(
          `/admin/contracts/liquidations/${encodeURIComponent(event.event_id)}/adl-execute`,
          { max_candidates: 20, confirm_execute: true },
          adminApiKey,
        );
        await load();
        showOperationResult("执行 ADL", `ADL 已执行，覆盖 ${result.covered_amount}，剩余 ${result.residual_after}，状态 ${result.status}`, result);
      }, { symbol: event.symbol, event_id: event.event_id });
    } finally {
      setAdlExecutingEventId(null);
    }
  };

  const updateContractRiskTier = (symbol: string, index: number, field: keyof ContractRiskTierEdit, value: string | number) => {
    setContractRiskTierEdits((current) => {
      const rows = [...(current[symbol] ?? [])];
      const row = rows[index];
      if (!row) return current;
      rows[index] = { ...row, [field]: field === "tier" ? Number(value) : String(value) };
      return { ...current, [symbol]: rows };
    });
  };

  const addContractRiskTier = (symbol: string) => {
    setContractRiskTierEdits((current) => {
      const rows = [...(current[symbol] ?? [])].sort((a, b) => a.tier - b.tier);
      const previous = rows[rows.length - 1];
      const previousFloor = Number(previous?.notional_floor ?? 0);
      const generatedCap = String((Number.isFinite(previousFloor) ? previousFloor : 0) + 50000);
      const nextFloor = previous?.notional_cap || generatedCap;
      const next: ContractRiskTierEdit = {
        tier: rows.length + 1,
        notional_floor: nextFloor,
        notional_cap: "",
        max_leverage: previous?.max_leverage ?? markets[symbol]?.max_leverage ?? "5",
        maintenance_margin_rate: previous?.maintenance_margin_rate ?? markets[symbol]?.maintenance_margin_rate ?? "0.005",
        maintenance_amount: previous?.maintenance_amount ?? "0",
      };
      if (previous && !previous.notional_cap) {
        rows[rows.length - 1] = { ...previous, notional_cap: nextFloor };
        next.notional_floor = rows[rows.length - 1].notional_cap;
      }
      return { ...current, [symbol]: [...rows, next] };
    });
  };

  const removeContractRiskTier = (symbol: string, index: number) => {
    setContractRiskTierEdits((current) => {
      const rows = (current[symbol] ?? []).filter((_, rowIndex) => rowIndex !== index);
      const normalized = rows.map((row, rowIndex) => ({ ...row, tier: rowIndex + 1 }));
      if (normalized.length > 0) {
        normalized[normalized.length - 1] = { ...normalized[normalized.length - 1], notional_cap: "" };
      }
      return { ...current, [symbol]: normalized };
    });
  };

  const saveContractRiskTiers = async (symbol: string) => {
    const rows = (contractRiskTierEdits[symbol] ?? []).map((row, index) => ({
      tier: index + 1,
      notional_floor: row.notional_floor,
      notional_cap: row.notional_cap.trim() ? row.notional_cap : undefined,
      max_leverage: row.max_leverage,
      maintenance_margin_rate: row.maintenance_margin_rate,
      maintenance_amount: row.maintenance_amount,
    }));
    const confirmed = window.confirm(`保存 ${symbol} 风险限额阶梯？该操作会影响杠杆上限、维持保证金和强平价。`);
    if (!confirmed) return;
    await runAdminOperation("保存风险阶梯", `${symbol} 风险限额阶梯保存失败`, async () => {
      const result = await api.put(`/admin/contracts/risk-tiers/${encodeURIComponent(symbol)}`, { tiers: rows, confirm_execute: true }, adminApiKey);
      await load();
      showOperationResult("保存风险阶梯", `${symbol} 风险限额阶梯已保存`, result);
    }, { symbol, tier_count: rows.length });
  };

  const saveMarketFees = async (symbol: string) => {
    const market = markets[symbol];
    if (!market) return;
    const confirmed = window.confirm(
      `确认保存 ${symbol} 的市场默认手续费？\n\n` +
      `maker：${market.default_maker_fee_rate || "-"}\n` +
      `taker：${market.default_taker_fee_rate || "-"}\n\n` +
      "市场默认费率只作为没有 UID 覆盖时的兜底，只影响后续成交，不批量改写已有 UID fee_profiles，也不回算历史费用。",
    );
    if (!confirmed) return;
    await runAdminOperation("保存市场默认费率", `${symbol} 市场默认手续费保存失败`, async () => {
      const result = await api.put(
        `/admin/markets/${symbol}/fees`,
        {
          maker_fee_rate: market.default_maker_fee_rate,
          taker_fee_rate: market.default_taker_fee_rate,
        },
        adminApiKey,
      );
      await load();
      showOperationResult("保存市场默认费率", `${symbol} 市场默认手续费已更新`, result, "success", undefined, {
        symbol,
        maker_fee_rate: market.default_maker_fee_rate,
        taker_fee_rate: market.default_taker_fee_rate,
      });
    }, {
      symbol,
      maker_fee_rate: market.default_maker_fee_rate,
      taker_fee_rate: market.default_taker_fee_rate,
    });
  };

  const resetMarket = async (symbol: string) => {
    const confirmed = window.confirm(`撤销 ${symbol} 全部当前挂单？该操作会改变订单状态和账户冻结资金。`);
    if (!confirmed) return;
    await runAdminOperation("撤销市场挂单", `${symbol} 当前挂单撤销失败`, async () => {
      const result = await api.post(`/admin/markets/${symbol}/reset`, { confirm_execute: true }, adminApiKey);
      await load();
      showOperationResult("撤销市场挂单", `${symbol} 当前挂单已撤销`, result);
    }, { symbol });
  };

  const wipeMarketData = async (symbol: string) => {
    if (!window.confirm(`清理 ${symbol} 的 K 线和最近成交展示？保留订单、成交账务、资金及持仓。新成交会继续显示。`)) return;
    await runAdminOperation("清理行情历史", `${symbol} 行情历史清理失败`, async () => {
      const result = await api.post(`/admin/markets/${symbol}/wipe-display-history`, { confirm_execute: true }, adminApiKey);
      await load();
      showOperationResult("清理行情历史", `${symbol} 行情历史已清理，账务与持仓保留`, result);
    }, { symbol });
  };

  const wipeMarketKlines = async (symbol: string) => {
    const confirmed = window.confirm(`只清除 ${symbol} 历史 K 线？清除后不能撤销，新成交仍会生成 K 线。不影响挂单、机器人和成交历史。`);
    if (!confirmed) return;
    await runAdminOperation("清除历史 K 线", `${symbol} 历史 K 线清除失败`, async () => {
      const result = await api.post(`/admin/markets/${symbol}/wipe-klines`, { confirm_execute: true }, adminApiKey);
      await load();
      showOperationResult("清除历史 K 线", `${symbol} 历史 K 线已清除，不影响挂单、机器人和成交历史`, result);
    }, { symbol });
  };

  const selectMarketStrategy = (symbol: string, strategy: string) => {
    const next = strategy.toUpperCase();
    const strategyState = marketStrategies[symbol];
    const matched = strategyState?.items?.find((item) => item.strategy_key === next)
      ?? strategyState?.templates?.find((item) => item.strategy_key === next);
    setStrategySelections((current) => ({ ...current, [symbol]: next }));
    setMarketStrategyConfigEdits((current) => ({
      ...current,
      [symbol]: cloneValue(
        (matched as MarketStrategyConfigState | undefined)?.effective_config
          ?? (matched as MarketStrategyConfigState | undefined)?.config
          ?? (matched as { default_config?: LiquidityRuntimeConfig } | undefined)?.default_config
          ?? {},
      ),
    }));
  };

  useEffect(() => {
    if (!location.search) return;
    if (appliedDeepLinkSearch.current === location.search) return;
    const params = new URLSearchParams(location.search);
    const requestedSection = (params.get("section") || "").trim() as AdminSection;
    const requestedSymbol = (params.get("market") || params.get("symbol") || "").trim().toUpperCase();
    const requestedTab = (params.get("marketTab") || params.get("tab") || "").trim() as MarketDetailTab;
    const requestedContractTab = (params.get("contractTab") || "").trim() as ContractAdminTab;
    const requestedContractAccountScopeInput = (params.get("contractAccountScope") || params.get("contractScope") || "").trim();
    const hasContractAccountScopeParam = contractAccountScopeKeys.includes(requestedContractAccountScopeInput as ContractAccountScope);
    const requestedContractAccountScope = contractAccountScopeFromParam(requestedContractAccountScopeInput);
    const requestedSystemTab = (params.get("systemTab") || "").trim() as SystemAdminTab;
    const requestedUserId = Number((params.get("userId") || "").trim());
    const requestedAccountScopeInput = (params.get("accountScope") || params.get("accountKind") || "").trim();
    const hasAccountScopeParam = accountUserScopeKeys.includes(requestedAccountScopeInput as AccountUserScope);
    const requestedAccountScope = accountScopeFromParam(requestedAccountScopeInput);
    const requestedStrategy = (params.get("strategy") || "").trim().toUpperCase();
    const requestedOperationFilters = adminOperationFiltersFromParams(params);
    const hasOperationFilters = hasAdminOperationFilterParams(params);
    const hasAuditFilters = hasOrderAuditParams(params);
    const requestedAuditProduct = auditProductScopeFromParam(params.get("auditProduct") || "");
    const requestedAuditSymbol = normalizeMarketSymbolInput(params.get("auditSymbol") || "");
    const requestedAuditUserId = (params.get("auditUserId") || "").trim();
    const requestedAuditStatus = auditStatusScopeFromParam(params.get("auditStatus") || "");
    const requestedAuditDomain = orderAuditDataScopeFromParam(params.get("auditDomain") || params.get("auditDataScope") || params.get("dataDomain") || "");
    const needsMarketData = Boolean(requestedSymbol || requestedAuditSymbol);
    const needsUserData =
      Boolean(params.get("userId") && Number.isFinite(requestedUserId))
      || Boolean(requestedAuditUserId && requestedAuditUserId !== "all");
    if ((needsMarketData && marketSymbols.length === 0) || (needsUserData && users.length === 0)) return;
    const validSection = adminSectionKeys.includes(requestedSection);
    const validTab = marketDetailTabKeys.includes(requestedTab);
    const validContractTab = contractAdminTabKeys.includes(requestedContractTab);
    const validSystemTab = systemAdminTabKeys.includes(requestedSystemTab);
    if (requestedSymbol && !marketSymbols.includes(requestedSymbol)) {
      pushToast("error", `后台没有找到市场 ${requestedSymbol}`);
      appliedDeepLinkSearch.current = location.search;
      return;
    }
    if (requestedAuditSymbol && !marketSymbols.includes(requestedAuditSymbol)) {
      pushToast("error", `订单审计没有找到市场 ${requestedAuditSymbol}`);
      appliedDeepLinkSearch.current = location.search;
      return;
    }
    if (requestedSymbol && requestedStrategy && !marketStrategies[requestedSymbol]) return;
    if (validSection) {
      setAdminSection(requestedSection);
    }
    if (requestedSymbol) {
      if (!validSection) setAdminSection("markets");
      setSelectedMarketSymbol(requestedSymbol);
    }
    if (validTab) {
      if (!validSection) setAdminSection("markets");
      setMarketDetailTab(requestedTab);
    } else if (requestedSymbol) {
      setMarketDetailTab("overview");
    }
    if (validContractTab) {
      setAdminSection("contracts");
      setContractAdminTab(requestedContractTab);
    }
    if (hasContractAccountScopeParam) {
      setContractAccountScope(requestedContractAccountScope);
      if (!validSection) setAdminSection("contracts");
      if (!validContractTab) setContractAdminTab("accounts");
    }
    if (validSystemTab) {
      setAdminSection("system");
      setSystemAdminTab(requestedSystemTab);
    }
    if (hasAccountScopeParam) {
      setAccountScope(requestedAccountScope);
      if (!validSection) setAdminSection("accounts");
    }
    if (Number.isFinite(requestedUserId) && users.some((user) => user.id === requestedUserId)) {
      const requestedUser = users.find((user) => user.id === requestedUserId);
      const requestedUserKind = requestedUser ? accountUserKindForUser(requestedUser) : null;
      setSelectedUserId(requestedUserId);
      if (!hasAccountScopeParam || (requestedUserKind && !accountScopeIncludesKind(requestedAccountScope, requestedUserKind))) {
        syncAccountScopeForUserId(requestedUserId);
      }
      if (!validSection) setAdminSection("accounts");
    }
    if (requestedSymbol && requestedStrategy) {
      selectMarketStrategy(requestedSymbol, requestedStrategy);
    }
    if (hasOperationFilters) {
      setAdminSection("system");
      setSystemAdminTab("audit");
      setOperationAuditFilters(requestedOperationFilters);
      void refreshAdminOperations(requestedOperationFilters);
    }
    if (hasAuditFilters) {
      setAdminSection("orders");
      const auditSymbolMatchesProduct =
        !requestedAuditSymbol
        || requestedAuditProduct === "all"
        || markets[requestedAuditSymbol]?.product_type === requestedAuditProduct;
      setAuditProduct(requestedAuditProduct);
      setAuditSymbol(requestedAuditSymbol && auditSymbolMatchesProduct ? requestedAuditSymbol : "all");
      setAuditStatus(requestedAuditStatus);
      setAuditDataScope(requestedAuditDomain);
      if (requestedAuditUserId && requestedAuditUserId !== "all") {
        const numericAuditUserId = Number(requestedAuditUserId);
        setAuditUserId(Number.isFinite(numericAuditUserId) && users.some((user) => user.id === numericAuditUserId) ? String(numericAuditUserId) : "all");
      } else {
        setAuditUserId("all");
      }
    }
    skipUrlSyncOnce.current = true;
    appliedDeepLinkSearch.current = location.search;
  }, [location.search, marketSymbols, marketStrategies, markets, pushToast, refreshAdminOperations, users]);

  useEffect(() => {
    if (!adminApiKey || (marketSymbols.length === 0 && !lightweightLiquiditySection)) return;
    if (skipUrlSyncOnce.current) {
      skipUrlSyncOnce.current = false;
      return;
    }
    const params = new URLSearchParams();
    if (adminSection !== "overview") params.set("section", adminSection);
    if ((adminSection === "markets" || adminSection === "strategies") && selectedMarketSymbol) {
      params.set("market", selectedMarketSymbol);
    }
    if (adminSection === "markets" && marketDetailTab !== "overview") {
      params.set("marketTab", marketDetailTab);
    }
    if (adminSection === "contracts" && contractAdminTab !== "overview") {
      params.set("contractTab", contractAdminTab);
    }
    if (adminSection === "contracts" && contractAdminTab === "accounts" && contractAccountScope !== "all") {
      params.set("contractAccountScope", contractAccountScope);
    }
    if (adminSection === "system" && systemAdminTab !== "overview") {
      params.set("systemTab", systemAdminTab);
    }
    if (adminSection === "system" && systemAdminTab === "audit" && hasNonDefaultAdminOperationFilters(operationAuditFilters)) {
      if (operationAuditFilters.domain !== "all") params.set("opDomain", operationAuditFilters.domain);
      if (operationAuditFilters.status !== "all") params.set("opStatus", operationAuditFilters.status);
      if (operationAuditFilters.operationType !== "all") params.set("opType", adminOperationTypeInput(operationAuditFilters.operationType));
      if (operationAuditFilters.targetSymbol) params.set("opSymbol", normalizeMarketSymbolInput(operationAuditFilters.targetSymbol));
      if (operationAuditFilters.limit !== "100") params.set("opLimit", operationAuditFilters.limit);
    }
    if (adminSection === "orders") {
      if (auditProduct !== "all") params.set("auditProduct", auditProduct);
      if (auditSymbol !== "all") params.set("auditSymbol", auditSymbol);
      if (auditUserId !== "all") params.set("auditUserId", auditUserId);
      if (auditStatus !== "live") params.set("auditStatus", auditStatus);
      if (auditDataScope !== "all") params.set("auditDomain", auditDataScope);
    }
    if (adminSection === "accounts" && selectedUserId) {
      params.set("userId", String(selectedUserId));
    }
    if (adminSection === "accounts" && accountScope !== "all") {
      params.set("accountScope", accountScope);
    }
    const nextSearch = params.toString() ? `?${params.toString()}` : "";
    if (location.search === nextSearch) return;
    appliedDeepLinkSearch.current = nextSearch;
    navigate({ pathname: location.pathname, search: nextSearch }, { replace: true });
  }, [
    adminApiKey,
    auditDataScope,
    auditProduct,
    auditStatus,
    auditSymbol,
    auditUserId,
    adminSection,
    accountScope,
    contractAdminTab,
    contractAccountScope,
    location.pathname,
    location.search,
    marketDetailTab,
    marketSymbols.length,
    navigate,
    operationAuditFilters,
    selectedMarketSymbol,
    selectedUserId,
    systemAdminTab,
  ]);

  const updateMarketStrategyConfigValue = (symbol: string, path: Array<string | number>, nextValue: unknown) => {
    setMarketStrategyConfigEdits((current) => ({
      ...current,
      [symbol]: setValueAtPath(cloneValue(current[symbol] ?? {}), path, nextValue) as LiquidityRuntimeConfig,
    }));
  };

  const saveStrategySelection = async (symbol: string, options?: { skipConfirm?: boolean }) => {
    const market = markets[symbol];
    const fallbackStrategy = market?.product_type === "PERP" ? "PERP_MM" : "LITE";
    const strategyVersion = String(strategySelections[symbol] ?? marketStrategies[symbol]?.selected?.strategy_key ?? fallbackStrategy).toUpperCase();
    const strategyLabel = strategyDisplayName(strategyVersion);
    const savedStrategy = String(marketStrategies[symbol]?.selected?.strategy_key ?? fallbackStrategy).toUpperCase();
    const draftConfig = marketStrategyConfigEdits[symbol] ?? marketStrategies[symbol]?.selected?.effective_config ?? {};
    const savedConfig = marketStrategies[symbol]?.selected?.effective_config ?? {};
    const strategyChanged = strategyVersion !== savedStrategy;
    const configChanged = stableConfigFingerprint(draftConfig) !== stableConfigFingerprint(savedConfig);
    if (!strategyChanged && !configChanged) {
      pushToast("success", `${symbol} 策略参数没有需要保存的变更`);
      return false;
    }
    const instance = makerInstances[symbol];
    const changeGroups = [
      strategyChanged ? `策略选择：${strategyDisplayName(savedStrategy)} -> ${strategyLabel}` : "",
      configChanged ? "参数配置变更" : "",
    ].filter(Boolean);
    if (!options?.skipConfirm) {
      const confirmed = window.confirm(
        `确认保存 ${symbol} 机器人策略？\n\n`
        + `变更：${changeGroups.join("；")}\n`
        + `实例状态：${instance?.running ? `运行中 / ${adminStatusText(instance.status)}` : "未运行"}\n\n`
        + `${strategySaveBoundary(market)}\n\n`
        + "该操作会写入市场策略配置，并可能影响正在运行的做市报价行为。",
      );
      if (!confirmed) return false;
    }
    return await runAdminOperation("保存机器人策略", `${symbol} 机器人策略保存失败`, async () => {
      const response = await api.put<UpdateMarketStrategyResponse>(`/admin/markets/${symbol}/strategy`,
        {
          strategy_key: strategyVersion,
          config: draftConfig,
        },
        adminApiKey,
      );
      if (response.apply_status) {
        setStrategyApplyStatuses((current) => ({ ...current, [symbol]: response.apply_status as StrategyApplyStatus }));
      }
      await load();
      showOperationResult(
        "保存机器人策略",
        response.apply_status?.label ? `${symbol} ${response.apply_status.label}` : `${symbol} 策略 ${strategyLabel} 已保存`,
        response,
      );
    }, { symbol, strategy_key: strategyVersion, product_type: market?.product_type, changed: changeGroups });
  };

  const saveStrategyAndRestart = async (symbol: string) => {
    const market = markets[symbol];
    const fallbackStrategy = market?.product_type === "PERP" ? "PERP_MM" : "LITE";
    const strategyVersion = String(strategySelections[symbol] ?? marketStrategies[symbol]?.selected?.strategy_key ?? fallbackStrategy).toUpperCase();
    const confirmed = window.confirm(`保存 ${symbol} 策略 ${strategyDisplayName(strategyVersion)} 并重启做市实例？该操作会短暂中断该币对做市进程，并先撤销机器人旧挂单。`);
    if (!confirmed) return;
    const saved = await saveStrategySelection(symbol, { skipConfirm: true });
    if (!saved) return;
    await restartMakerInstance(symbol, true);
  };

  const applyMakerInstanceResponse = (response: { instance: MakerInstanceStatus }) => {
    setMakerInstances((current) => ({ ...current, [response.instance.symbol]: response.instance }));
  };

  const startMakerInstance = async (symbol: string) => {
    const confirmed = window.confirm(`启动 ${symbol} 做市实例？该操作会让机器人开始真实挂单并可能产生成交。`);
    if (!confirmed) return;
    await runAdminOperation("启动做市实例", `${symbol} 做市实例启动失败`, async () => {
      const response = await api.post<{ instance: MakerInstanceStatus }>(
        `/admin/markets/${symbol}/maker-instance/start`,
        { confirm_execute: true },
        adminApiKey,
      );
      applyMakerInstanceResponse(response);
      await refreshAdminOperations();
      showOperationResult("启动做市实例", `${symbol} 做市实例已启动`, response);
    }, { symbol });
  };

  const stopMakerInstance = async (symbol: string) => {
    const confirmed = window.confirm(`停止 ${symbol} 做市实例？普通停止不会主动撤销当前机器人挂单。`);
    if (!confirmed) return;
    await runAdminOperation("停止做市实例", `${symbol} 做市实例停止失败`, async () => {
      const response = await api.post<{ instance: MakerInstanceStatus }>(
        `/admin/markets/${symbol}/maker-instance/stop`,
        { confirm_execute: true },
        adminApiKey,
      );
      applyMakerInstanceResponse(response);
      await refreshAdminOperations();
      showOperationResult("停止做市实例", `${symbol} 做市实例已停止`, response);
    }, { symbol });
  };

  const stopAndCancelMakerInstance = async (symbol: string) => {
    const confirmed = window.confirm(`停止 ${symbol} 做市实例，并撤销该币对所有机器人当前挂单？`);
    if (!confirmed) return;
    await runAdminOperation("停止并撤单", `${symbol} 停止并撤单失败`, async () => {
      const response = await api.post<{ instance: MakerInstanceStatus; cleanup?: { canceled_count?: number } }>(
        `/admin/markets/${symbol}/maker-instance/stop-and-cancel`,
        { confirm_execute: true },
        adminApiKey,
      );
      applyMakerInstanceResponse(response);
      await refreshAdminOperations();
      showOperationResult("停止并撤单", `${symbol} 已停止，撤销机器人挂单 ${response.cleanup?.canceled_count ?? 0} 笔`, response);
    }, { symbol });
  };

  const restartMakerInstance = async (symbol: string, skipConfirm = false) => {
    if (!skipConfirm) {
      const confirmed = window.confirm(`重启 ${symbol} 做市实例？该操作会先停止当前实例、撤销机器人旧挂单，再重新启动挂单。`);
      if (!confirmed) return;
    }
    await runAdminOperation("重启做市实例", `${symbol} 做市实例重启失败`, async () => {
      const response = await api.post<{ instance: MakerInstanceStatus; cleanup?: { canceled_count?: number } }>(
        `/admin/markets/${symbol}/maker-instance/restart`,
        { confirm_execute: true },
        adminApiKey,
      );
      applyMakerInstanceResponse(response);
      await refreshAdminOperations();
      showOperationResult("重启做市实例", `${symbol} 做市实例已重启，撤旧挂单 ${response.cleanup?.canceled_count ?? 0} 笔`, response);
    }, { symbol });
  };

  const renderPrimitiveEditor = (
    symbol: string,
    label: string,
    value: string | number | boolean | null,
    path: Array<string | number>,
    onChangeValue: (symbol: string, path: Array<string | number>, nextValue: unknown) => void,
  ) => {
    if (typeof value === "boolean") {
      return (
        <label key={path.join(".")} className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-3 py-3 text-sm">
          <span className="text-slate-300">{label}</span>
          <input
            type="checkbox"
            checked={value}
            onChange={(event) => onChangeValue(symbol, path, event.target.checked)}
            className="h-4 w-4 accent-cyan-400"
          />
        </label>
      );
    }
    return (
      <label key={path.join(".")} className="block">
        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">{label}</span>
        <input
          value={value === null ? "" : String(value)}
          onChange={(event) => onChangeValue(symbol, path, event.target.value)}
          className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
        />
      </label>
    );
  };

  const renderConfigNode = (
    symbol: string,
    node: unknown,
    onChangeValue: (symbol: string, path: Array<string | number>, nextValue: unknown) => void,
    path: Array<string | number> = [],
  ): React.ReactNode => {
    if (isPrimitive(node)) {
      const label = humanizeKey(String(path[path.length - 1] ?? "value"));
      return renderPrimitiveEditor(symbol, label, node, path, onChangeValue);
    }

    if (Array.isArray(node)) {
      const sectionName = humanizeKey(String(path[path.length - 1] ?? "item"));
      return (
        <div className="grid gap-3">
          {node.map((item, index) => (
              <div key={`${path.join(".")}.${index}`} className="rounded-2xl border border-white/8 bg-slate-950/30 p-3">
                <div className="mb-3 text-xs uppercase tracking-[0.18em] text-cyan-100/70">{sectionName} #{index + 1}</div>
                {renderConfigNode(symbol, item, onChangeValue, [...path, index])}
              </div>
            ))}
          </div>
      );
    }

    if (isPlainObject(node)) {
      return (
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(node).map(([key, value]) => {
            const nextPath = [...path, key];
            if (isPrimitive(value)) {
              return renderPrimitiveEditor(symbol, humanizeKey(key), value, nextPath, onChangeValue);
            }
            return (
              <div key={nextPath.join(".")} className="rounded-2xl border border-white/8 bg-slate-950/30 p-3 md:col-span-2">
                <div className="mb-3 text-xs uppercase tracking-[0.18em] text-cyan-100/70">{humanizeKey(key)}</div>
                {renderConfigNode(symbol, value, onChangeValue, nextPath)}
              </div>
            );
          })}
        </div>
      );
    }

    return null;
  };

  const selectSidebarUser = (userId: number) => {
    setSelectedUserId(userId);
    syncAccountScopeForUserId(userId);
  };

  return (
    <AppShell>
      <ToastViewport />
      <label className="mb-3 block text-sm text-slate-400 xl:hidden">后台功能 <select aria-label="后台功能导航" value={adminSection} onChange={(event) => setAdminSection(event.target.value as AdminSection)} className="ml-2 rounded-lg border border-white/10 bg-slate-900 p-2 text-slate-100">{adminNavGroups.map((group) => <optgroup key={group.title} label={group.title}>{group.items.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</optgroup>)}</select></label>
      <div className="grid gap-3 xl:grid-cols-[240px_minmax(0,1fr)]">
        <AdminSidebar
          section={adminSection}
          onSectionChange={setAdminSection}
          userCount={users.length}
          marketCount={marketSymbols.length}
          status={deploymentChecklist?.status ?? systemStatus?.status}
          selectedMarketSymbol={selectedMarketSymbol}
          marketSymbols={marketSymbols}
          onMarketChange={setSelectedMarketSymbol}
          selectedUserId={selectedUserId}
          users={users}
          onUserChange={selectSidebarUser}
        />
        <div className="min-w-0 space-y-3 admin-workspace">
          <section className="panel rounded-2xl p-4">
            <p className="text-xs text-slate-500">管理后台 / {adminNavGroups.find((group) => group.items.some((item) => item.key === adminSection))?.title}</p>
            <h1 className="mt-1 text-xl font-semibold">{adminNavGroups.flatMap((group) => group.items).find((item) => item.key === adminSection)?.label}</h1>
            <p className="mt-2 text-sm text-slate-400">{adminSection === "overview" ? "先选择交易市场，再配置做市报价和模拟成交；到交易终端观察结果。账户、清算和诊断工具按需使用。" : adminNavGroups.flatMap((group) => group.items).find((item) => item.key === adminSection)?.hint}</p>
            {adminSection === "overview" && <div className="mt-4 grid gap-2 sm:grid-cols-3">{([
              ["markets", "01 选择市场", "设置现货或永续的交易规则"],
              ["maker_config", "02 配置做市", "让买卖盘口出现报价和深度"],
              ["flow_config", "03 模拟成交", "生成测试成交与 K 线量能"],
            ] as const).map(([key, title, description]) => <button key={key} onClick={() => setAdminSection(key)} className="rounded-xl border border-white/10 bg-white/[0.025] p-3 text-left hover:border-cyan-400/40"><span className="text-sm text-cyan-200">{title}</span><span className="mt-1 block text-xs text-slate-400">{description}</span></button>)}</div>}
          </section>
          {adminSection === "maker_config" && <LiquidityControlPage key="maker" kind="maker" />}
          {adminSection === "flow_config" && <LiquidityControlPage key="flow" kind="flow" />}
          {adminSection === "overview" && (
            <OverviewDashboard
              users={users}
              marketSymbols={marketSymbols}
              markets={markets}
              marketSurveillance={marketSurveillance}
              marketBots={marketBots}
              makerInstances={makerInstances}
              positions={contractPositions}
              marketStates={contractMarketStates}
              fundingJobs={contractFundingJobs}
              liquidationEvents={contractLiquidationEvents}
              adlEvents={contractAdlEvents}
              insuranceEvents={contractInsuranceEvents}
              maintenance={contractMaintenance}
              adminOperations={adminOperations}
              deploymentChecklist={deploymentChecklist}
              systemStatus={systemStatus}
              onOpenSection={setAdminSection}
              onOpenTarget={openAdminBusinessTarget}
              onOpenOrderAudit={openOrderAuditTarget}
              onOpenRiskItem={openRiskQueueTarget}
              onOpenMarket={(symbol) => {
                setSelectedMarketSymbol(symbol);
                setAdminSection("markets");
                setMarketDetailTab("overview");
              }}
            />
          )}
          {adminSection === "system" && (
            <SystemAuditPanel
              checklist={deploymentChecklist}
              status={systemStatus}
              operations={adminOperations}
              operationFilters={operationAuditFilters}
              operationLoading={operationAuditLoading}
              operationError={operationAuditError}
              lastOperationResult={lastOperationResult}
              activeTab={systemAdminTab}
              defaultPerpSymbol={Object.values(markets).find((market) => market.product_type === "PERP")?.symbol}
              onOpenSection={setAdminSection}
              onOpenTarget={openAdminBusinessTarget}
              onOpenOrderAudit={openOrderAuditTarget}
              onOpenOperationTarget={openAdminOperationTarget}
              onTabChange={openSystemAdminTab}
              onOperationFiltersChange={setOperationAuditFilters}
              onRefreshOperations={refreshAdminOperations}
              onResetOperationFilters={resetAdminOperationFilters}
              onRefresh={load}
            />
          )}
          {adminSection === "contracts" && (
            <ContractAdminPanel
              accounts={contractAccounts}
              positions={contractPositions}
              orders={contractOrders}
              trades={contractTrades}
              contractMarkets={Object.values(markets).filter((market) => market.product_type === "PERP")}
              marketStates={contractMarketStates}
              fundingEvents={contractFundingEvents}
              ledgerEntries={contractLedgerEntries}
              insuranceFunds={contractInsuranceFunds}
              insuranceEvents={contractInsuranceEvents}
              adlEvents={contractAdlEvents}
              insuranceAdjustAmount={insuranceAdjustAmount}
              insuranceAdjusting={insuranceAdjusting}
              adlExecutingEventId={adlExecutingEventId}
              fundingJobs={contractFundingJobs}
              fundingSettlements={contractFundingSettlements}
              liquidationEvents={contractLiquidationEvents}
              riskTierEdits={contractRiskTierEdits}
              maintenance={contractMaintenance}
              detailLoadState={contractDetailLoadState}
              activeTab={contractAdminTab}
              accountScope={contractAccountScope}
              onTabChange={setContractAdminTab}
              onAccountScopeChange={setContractAccountScope}
              onRefreshMarketState={refreshContractMarketState}
              onSettleFunding={settleContractFunding}
              onRetryFundingJob={retryContractFundingJob}
              onRetryFailedFundingJobs={retryFailedContractFundingJobs}
              onInsuranceAdjustAmountChange={setInsuranceAdjustAmount}
              onAdjustInsuranceFund={adjustContractInsuranceFund}
              onExecuteAdl={executeContractAdl}
              batchRetryingFundingJobs={batchRetryingFundingJobs}
              onRiskTierChange={updateContractRiskTier}
              onAddRiskTier={addContractRiskTier}
              onRemoveRiskTier={removeContractRiskTier}
              onSaveRiskTiers={saveContractRiskTiers}
              onRefresh={load}
              onRefreshDetails={(filters) => void refreshContractDetails({ filters })}
              onOpenAccountDirectory={() => {
                setAccountAdminView("overview");
                setSelectedUserId(null);
                openAdminBusinessTarget({ section: "accounts", accountScope: "all" });
              }}
              onOpenBotAccounts={() => openAdminBusinessTarget({ section: "bots" })}
              onOpenContractAccounts={() => openAdminBusinessTarget({ section: "contracts", contractTab: "accounts" })}
              onOpenContractPositions={() => openAdminBusinessTarget({ section: "contracts", contractTab: "positions" })}
              onOpenContractOrders={() => openAdminBusinessTarget({ section: "contracts", contractTab: "orders" })}
              onOpenContractTrade={(symbol) => navigate(`/contracts/trade/${encodeURIComponent(symbol)}`)}
              onOpenMarketProductConfig={(symbol) => {
                setSelectedMarketSymbol(symbol);
                setMarketDetailTab("product");
                setAdminSection("markets");
              }}
            />
          )}
          {adminSection === "risk" && (
            <RiskMonitoringPanel
              deploymentChecklist={deploymentChecklist}
              systemStatus={systemStatus}
              marketSymbols={marketSymbols}
              marketSurveillance={marketSurveillance}
              makerInstances={makerInstances}
              positions={contractPositions}
              marketStates={contractMarketStates}
              fundingJobs={contractFundingJobs}
              liquidationEvents={contractLiquidationEvents}
              adlEvents={contractAdlEvents}
              insuranceEvents={contractInsuranceEvents}
              maintenance={contractMaintenance}
              adminOperations={adminOperations}
              detailLoadState={contractDetailLoadState}
              onOpenRiskItem={openRiskQueueTarget}
              onRefresh={load}
            />
          )}
          {adminSection === "markets" && (
            <MarketWorkspacePanel
              users={users}
              marketSymbols={marketSymbols}
              markets={markets}
              persistedMarkets={persistedMarkets}
              marketTemplates={marketTemplates}
              selectedMarketSymbol={selectedMarketSymbol}
              onMarketChange={(symbol) => openAdminBusinessTarget({ section: "markets", targetSymbol: symbol, marketDetailTab: "overview" })}
              detailTab={marketDetailTab}
              onDetailTabChange={openMarketDetailTab}
              marketSurveillance={marketSurveillance}
              marketBots={marketBots}
              makerInstances={makerInstances}
              marketStrategies={marketStrategies}
              strategySelections={strategySelections}
              marketStrategyConfigEdits={marketStrategyConfigEdits}
              strategyApplyStatuses={strategyApplyStatuses}
              lastOperationResult={lastOperationResult}
              activeAdminOperation={activeAdminOperation}
              newMarket={newMarket}
              onNewMarketChange={setNewMarket}
              onApplyMarketTemplate={applyMarketTemplate}
              onCreateMarket={createMarket}
              onUpdateMarketField={updateMarketField}
              onRevertMarketFields={revertMarketFields}
              onSaveMarketConfig={saveMarketConfig}
              onSaveMarketFees={saveMarketFees}
              onToggleMarket={toggleMarket}
              onResetMarket={resetMarket}
              onWipeMarketKlines={wipeMarketKlines}
              onWipeMarketData={wipeMarketData}
              seedBookForms={seedBookForms}
              onUpdateSeedBook={updateSeedBook}
              onSeedMarketBook={seedMarketBook}
              sweepPreviewForms={sweepPreviewForms}
              sweepPreviews={sweepPreviews}
              onUpdateSweepPreview={updateSweepPreview}
              onPreviewSweep={previewSweep}
              marketBotForms={marketBotForms}
              defaultMarketBotForm={defaultMarketBotForm}
              onMarketBotFormChange={updateMarketBotForm}
              onCreateMarketBot={createMarketBot}
              onCreateDefaultBots={createDefaultMarketBots}
              onCreateFlowBot={createDefaultFlowBot}
              onUpdateMarketBot={updateMarketBot}
              onStrategyChange={selectMarketStrategy}
              onStrategyConfigChange={updateMarketStrategyConfigValue}
              onSaveStrategy={saveStrategySelection}
              onSaveStrategyAndRestart={saveStrategyAndRestart}
              onStartInstance={startMakerInstance}
              onStopInstance={stopMakerInstance}
              onStopAndCancelInstance={stopAndCancelMakerInstance}
              onRestartInstance={restartMakerInstance}
              renderConfigNode={renderConfigNode}
              onOpenBotsStrategy={(symbol) => navigate(`/admin?section=maker_config&market=${encodeURIComponent(symbol)}`)}
              onOpenOperationAudit={openMarketOperationAudit}
              onReload={load}
            />
          )}
          {adminSection === "orders" && (
            <OrderAuditPanel
              orders={auditOrders}
              trades={auditTrades}
              orderQueryMeta={auditOrderQueryMeta}
              tradeQueryMeta={auditTradeQueryMeta}
              loading={auditLoading}
              error={auditError}
              product={auditProduct}
              symbol={auditSymbol}
              userId={auditUserId}
              status={auditStatus}
              dataScope={auditDataScope}
              markets={markets}
              marketSymbols={marketSymbols}
              users={users}
              dataRetentionDomains={systemStatus?.data_retention_domains}
              onProductChange={setAuditProduct}
              onSymbolChange={setAuditSymbol}
              onUserChange={setAuditUserId}
              onStatusChange={setAuditStatus}
              onDataScopeChange={setAuditDataScope}
              onRefresh={loadOrderAudit}
            />
          )}
          {adminSection === "bots" && (
            <GlobalBotAccountsPanel
              markets={markets}
              marketSymbols={marketSymbols}
              marketBots={marketBots}
              users={users}
              dataRetentionDomains={systemStatus?.data_retention_domains}
              onOpenOrderAudit={openOrderAuditTarget}
              onOpenAccounts={() => {
                setAccountAdminView("overview");
                setSelectedUserId(null);
                openAdminBusinessTarget({ section: "accounts", accountScope: "robot" });
              }}
              onOpenContractAccounts={() => openAdminBusinessTarget({ section: "contracts", contractTab: "accounts", contractAccountScope: "robot" })}
              onOpenRobotOperations={() => openAdminBusinessTarget({ section: "maker_config" })}
              onOpenAccount={(userId, accountScope) => openAdminBusinessTarget({ section: "accounts", targetUserId: userId, accountScope })}
              onOpenMarket={(symbol) => {
                setSelectedMarketSymbol(symbol);
                setMarketDetailTab("bots");
                setAdminSection("markets");
              }}
            />
          )}
          {adminSection === "accounts" && (
        <section className="panel rounded-3xl p-4">
          <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="font-display text-xl">账户与资金</h2>
              <p className="mt-1 text-sm text-slate-400">本页作为账户目录：管理登录主体、API Key、费率和现货钱包，同时只读索引合约保证金与机器人绑定；两套资金账本不合并。</p>
            </div>
		            <div className="flex flex-wrap gap-2">
		              <button
		                type="button"
		                onClick={exportAccountDirectory}
	                disabled={accountUsersInScope.length === 0}
	                className="rounded-2xl bg-white/8 px-4 py-3 text-sm font-medium text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
		              >
		                导出当前主体 CSV
		              </button>
		            </div>
          </div>
          <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-7">
            <SurveillanceMetric label="全部主体" value={String(users.length)} />
            <SurveillanceMetric label="客户 UID" value={String(accountKindCounts.customer)} />
            <SurveillanceMetric label="管理主体" value={String(accountKindCounts.admin)} />
            <SurveillanceMetric label="现货机器人" value={String(accountKindCounts.spot_robot)} />
            <SurveillanceMetric label="合约机器人" value={String(accountKindCounts.contract_robot)} />
            <SurveillanceMetric label="系统主体" value={String(accountKindCounts.system)} />
            <SurveillanceMetric label="启用主体" value={String(users.filter((user) => user.is_active).length)} />
          </div>
          <div className="mb-4 flex gap-2 overflow-x-auto pb-1 scrollbar">
            {accountScopeOptions.map((option) => {
              const active = accountScope === option.key;
              return (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setAccountScope(option.key)}
                  className={`shrink-0 rounded-xl px-3 py-2 text-left transition ${active ? "bg-violet-400/16 text-violet-100" : "bg-white/6 text-slate-300 hover:bg-white/10"}`}
                >
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <span>{option.label}</span>
                    <span className="rounded-full bg-white/8 px-2 py-0.5 text-[11px]">{option.count}</span>
                  </div>
                  <div className="mt-0.5 text-xs text-slate-500">{option.hint}</div>
                </button>
              );
            })}
		          </div>
	          <div className="mb-4">
	            <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">账户分区</div>
	            <div className="grid gap-2 md:grid-cols-4">
	              {accountAdminViewOptions.map((option) => {
	                const active = accountAdminView === option.key;
	                const badgeClass = option.tone === "warn" ? "bg-amber-400/12 text-amber-100" : "bg-white/8 text-slate-300";
	                return (
	                  <button
	                    key={option.key}
	                    type="button"
	                    onClick={() => setAccountAdminView(option.key)}
	                    className={`min-h-[72px] rounded-2xl border px-4 py-3 text-left transition ${active ? "border-violet-300/40 bg-violet-400/12 text-violet-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"}`}
	                  >
	                    <div className="flex items-center justify-between gap-2">
	                      <span className="font-medium">{option.label}</span>
	                      <span className={`rounded-full px-2 py-0.5 text-[11px] ${badgeClass}`}>{option.badge}</span>
	                    </div>
	                    <div className="mt-1 text-xs text-slate-500">{option.hint}</div>
	                  </button>
	                );
	              })}
	            </div>
	          </div>
	          {accountAdminView === "overview" && (
	            <>
		              <AccountQuickPaths
		                selectedUser={selectedAccountUser ?? undefined}
		                defaultUser={accountDirectoryDefaultUser}
                    accountScope={accountScope}
                    scopedAccountCount={accountUsersInScope.length}
		                onOpenView={setAccountAdminView}
		                onOpenOrderAudit={openOrderAuditTarget}
	                onOpenContractAccounts={() => openAdminBusinessTarget({ section: "contracts", contractTab: "accounts", contractAccountScope: contractAccountScopeFromAccountScope(accountScope) })}
	                onOpenBots={() => openAdminBusinessTarget({ section: "bots" })}
	              />
	              <AccountLedgerDirectory onOpenSection={setAdminSection} />
	              <AccountDirectoryReviewStrip options={accountScopeOptions} activeScope={accountScope} />
	              <div className="mb-4 rounded-2xl border border-violet-400/12 bg-violet-400/6 px-4 py-3 text-sm leading-6 text-violet-50/90">
	                主体总览只做目录、账本边界和资源归属说明；开户、单 UID 核查、身份费率维护和现货资金维护已拆到独立分段。
	              </div>
	            </>
	          )}
	          {accountAdminView === "onboarding" && (
	            <div className="mb-4 rounded-3xl border border-cyan-400/12 bg-cyan-400/6 p-4">
	              <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
	                <div>
	                  <h3 className="font-display text-base">账户开立</h3>
	                  <p className="mt-1 text-xs leading-5 text-slate-400">新建主体默认获得现货 USDT 1 亿与独立合约钱包 1 亿；可在下方覆盖现货初始模板，机器人绑定和风险参数仍回对应运营域。</p>
	                </div>
	                <button
	                  type="button"
	                  onClick={createUser}
	                  disabled={accountActionDisabled}
	                  className={`self-start whitespace-nowrap rounded-2xl bg-cyan-400/18 px-4 py-2 text-sm text-cyan-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
	                >
		                  {accountActionBusy(accountActionKey("new", "create")) ? "创建中..." : "创建登录主体"}
	                </button>
	              </div>
	              <AccountOnboardingRoleBoundary role={newUser.role} />
	              <div className="grid gap-3 xl:grid-cols-3">
	                <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
		                  <h4 className="font-display text-sm">主体身份</h4>
	                  <p className="mt-1 text-xs leading-5 text-slate-500">创建登录主体并生成 API Key / Secret；创建后如需绑定机器人，回机器人账号或机器人运营。</p>
	                  <div className="mt-3 grid gap-3">
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">username</span>
	                      <input value={newUser.username} onChange={(event) => setNewUser((current) => ({ ...current, username: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">password</span>
	                      <input type="password" value={newUser.password} onChange={(event) => setNewUser((current) => ({ ...current, password: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">role</span>
	                      <select value={newUser.role} onChange={(event) => setNewUser((current) => ({ ...current, role: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
	                        <option value="manual_user">manual_user</option>
	                        <option value="mm_bot">mm_bot</option>
	                        <option value="admin">admin</option>
	                      </select>
	                    </label>
	                  </div>
	                </div>
	                <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	                  <h4 className="font-display text-sm">初始现货模板</h4>
	                  <p className="mt-1 text-xs leading-5 text-slate-500">现货模板可按测试需要覆盖；合约保证金账户由统一开户契约自动开立为 1 亿 USDT，现货与合约仍是两套独立账本。</p>
	                  <div className="mt-3 grid gap-3">
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">initial USDT</span>
	                      <input value={newUser.usdt} onChange={(event) => setNewUser((current) => ({ ...current, usdt: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">initial base assets</span>
	                      <input value={newUser.baseAmount} onChange={(event) => setNewUser((current) => ({ ...current, baseAmount: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                  </div>
	                </div>
	                <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	                  <h4 className="font-display text-sm">费率模板</h4>
	                  <p className="mt-1 text-xs leading-5 text-slate-500">创建时写入所有当前市场的默认 maker / taker 费率；后续可在单账户维护分段里维护。</p>
	                  <div className="mt-3 grid grid-cols-2 gap-2">
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">maker fee</span>
	                      <input value={newUser.makerFeeRate} onChange={(event) => setNewUser((current) => ({ ...current, makerFeeRate: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                    <label className="block">
	                      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">taker fee</span>
	                      <input value={newUser.takerFeeRate} onChange={(event) => setNewUser((current) => ({ ...current, takerFeeRate: event.target.value }))} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                    </label>
	                  </div>
	                </div>
	              </div>
	            </div>
	          )}
	          {(accountAdminView === "detail" || accountAdminView === "maintenance") && (
	            <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	              <div className="mb-3 grid gap-2 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
	                <label className="block min-w-0">
	                  <span className="mb-2 block text-xs uppercase tracking-[0.16em] text-slate-500">筛选当前分类账户</span>
	                  <input
	                    value={accountUserQuery}
	                    onChange={(event) => setAccountUserQuery(event.target.value)}
	                    placeholder="输入 UID、用户名、角色或账户分类"
	                    disabled={accountUsersInScope.length === 0}
	                    className="w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
	                  />
	                </label>
	                <div className="flex flex-wrap items-center gap-2">
	                  <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	                    匹配 {accountSelectableUsers.length} / {accountUsersInScope.length}
	                  </span>
	                  {accountUserQuery && (
	                    <button
	                      type="button"
	                      onClick={() => setAccountUserQuery("")}
	                      className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-100 hover:bg-white/12"
	                    >
	                      清空
	                    </button>
	                  )}
	                </div>
	              </div>
	              <label className="block">
	                <span className="mb-2 block text-xs uppercase tracking-[0.16em] text-slate-500">当前分类账户</span>
	                <select
	                  value={selectedUserId ?? ""}
	                  onChange={(event) => setSelectedUserId(Number(event.target.value))}
	                  disabled={accountUsersInScope.length === 0}
	                  className="w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-slate-100 outline-none"
	                >
	                  {accountSelectOptions.map((user) => (
	                    <option key={user.id} value={user.id}>{user.username} · {accountUserKindLabel(accountUserKindForUser(user))} · {user.role}</option>
	                  ))}
	                  {accountUsersInScope.length === 0 && <option value="">当前分类暂无账户</option>}
	                </select>
	                {accountUserQuery && accountSelectableUsers.length === 0 && accountUsersInScope.length > 0 && (
	                  <span className="mt-2 block text-xs text-amber-100">无匹配账户，当前选择仍保留；清空筛选后可查看全部候选。</span>
	                )}
	              </label>
	            </div>
	          )}
	          {accountAdminView === "detail" && selectedAccountUser && (
	            <AccountDetailPanel
	              user={selectedAccountUser}
	              kind={accountUserKindForUser(selectedAccountUser)}
	              contractAccounts={selectedAccountContractAccounts}
	              contractPositions={selectedAccountContractPositions}
	              contractOrders={selectedAccountContractOrders}
	              contractLedgerEntries={selectedAccountContractLedgerEntries}
	              contractDetailLoadState={contractDetailLoadState}
	              activity={userActivities[selectedAccountUser.id]}
	              activityVisible={activeActivityUserId === selectedAccountUser.id}
	              activityLoading={loadingActivityUserId === selectedAccountUser.id}
	              onToggleActivity={() => void toggleUserActivity(selectedAccountUser)}
	              onOpenSection={setAdminSection}
	              onOpenContractAccounts={() => openAdminBusinessTarget({
	                section: "contracts",
	                contractTab: "accounts",
	                contractAccountScope: contractAccountScopeFromParam(accountUserKindForUser(selectedAccountUser)),
	              })}
	              onOpenOrderAudit={openOrderAuditTarget}
	            />
	          )}
	          {accountAdminView === "detail" && !selectedAccountUser && (
	            <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 px-4 py-6 text-center text-sm text-slate-500">当前分类暂无可核查主体。</div>
          )}
          {accountAdminView === "maintenance" && (
            <>
              <AccountFeeGovernanceStrip users={users} markets={markets} />
              <SpotBalanceAdjustmentRunbookStrip />
              <div className="mb-4 rounded-2xl border border-amber-400/14 bg-amber-400/6 p-4">
	                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
	                  <div>
	                    <h3 className="font-display text-base text-amber-50">批量测试资金维护</h3>
	                    <p className="mt-1 text-xs leading-5 text-amber-50/75">只用于演示测试账户恢复；执行前确认没有进行中的重要测试，执行后抽查账户目录和现货流水。</p>
	                  </div>
	                  <button
	                    type="button"
	                    onClick={resetAllTestUsers}
	                    disabled={accountActionDisabled}
	                    className={`self-start whitespace-nowrap rounded-2xl bg-amber-400/16 px-4 py-2 text-sm font-medium text-amber-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
	                  >
	                    {accountActionBusy(accountActionKey("all", "reset-test-users")) ? "重置中..." : "批量重置测试主体资金"}
	                  </button>
	                </div>
	              </div>
	              <div className="space-y-3">
            {visibleUsers.map((user) => {
              const kind = accountUserKindForUser(user);
              const feePolicy = accountFeePolicyCopy(kind);
              const edit = userEdits[user.id] ?? defaultUserEdit(user);
              const adjustment = balanceAdjustments[user.id] ?? defaultBalanceAdjustment(user);
              const identityActionKey = accountActionKey(user.id, "identity");
              const rotateApiKeyActionKey = accountActionKey(user.id, "rotate-api-key");
              const feesAllActionKey = accountActionKey(user.id, "fees-all");
              const resetActionKey = accountActionKey(user.id, "reset");
              const adjustActionKey = accountActionKey(user.id, "adjust-balance");
              const pendingLabel = accountActionPending?.key.startsWith(`account:${user.id}:`) ? accountActionPending.label : null;
              const changedFields = userEditChangedFields(user, edit);
              return (
                <div key={user.id} className="rounded-3xl border border-white/8 bg-white/5 p-4">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="font-display text-lg">{user.username}</div>
                        <span className={`rounded-full px-2 py-1 text-xs ${accountUserKindClass(kind)}`}>{accountUserKindLabel(kind)}</span>
                        <span className={user.is_active ? "rounded-full bg-emerald-400/15 px-2 py-1 text-xs text-emerald-100" : "rounded-full bg-rose-500/15 px-2 py-1 text-xs text-rose-100"}>
                          {user.is_active ? "active" : "paused"}
                        </span>
                      </div>
                      <div className="mt-1 text-sm text-slate-400">
                        {user.role} · key {user.api_key} · secret {maskSecret(user.api_secret)}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button onClick={() => void toggleUserActivity(user)} className="rounded-full bg-white/8 px-4 py-2 text-sm text-slate-100">
                        {activeActivityUserId === user.id ? "收起活动" : "查看活动"}
                      </button>
                    </div>
                  </div>
                  <AccountMaintenanceStatusStrip changedFields={changedFields} pendingLabel={pendingLabel} />
                  <AccountMaintenanceBoundaryStrip
                    user={user}
                    kind={kind}
                    feeProfileCount={user.fee_profiles?.length ?? 0}
                    marketCount={marketSymbols.length}
                  />
                  <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
                    <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                      <div>
                        <h4 className="font-display text-sm">身份与凭据维护</h4>
                        <p className="mt-1 text-xs leading-5 text-slate-500">角色、登录/API 启停、密码和 API Key 会影响访问边界；执行前确认目标 UID、主体分类、外部脚本和机器人凭据来源。</p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => saveUser(user)}
                          disabled={accountActionDisabled}
                          className={`whitespace-nowrap rounded-2xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                        >
                          {accountActionBusy(identityActionKey) ? "保存中..." : "保存主体身份"}
                        </button>
                        <button
                          type="button"
                          onClick={() => rotateUserApiKey(user)}
                          disabled={accountActionDisabled}
                          className={`whitespace-nowrap rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                        >
                          {accountActionBusy(rotateApiKeyActionKey) ? "轮换中..." : "轮换 API"}
                        </button>
                      </div>
                    </div>
                    <div className="grid gap-3 md:grid-cols-3">
                      <label className="block">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">role</span>
                        <select value={edit.role} onChange={(event) => updateUserEdit(user, { role: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
                          <option value="manual_user">manual_user</option>
                          <option value="mm_bot">mm_bot</option>
                          <option value="admin">admin</option>
                        </select>
                      </label>
                      <label className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-3 py-3 text-sm">
                        <span className="text-slate-300">API / 登录启用</span>
                        <input type="checkbox" checked={edit.isActive} onChange={(event) => updateUserEdit(user, { isActive: event.target.checked })} className="h-4 w-4 accent-cyan-400" />
                      </label>
                      <label className="block">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">new password</span>
                        <input type="password" value={edit.password} onChange={(event) => updateUserEdit(user, { password: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
                      </label>
                    </div>
                  </div>
                  <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
                    <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                      <div>
                        <h4 className="font-display text-sm">{feePolicy.title} · 全市场</h4>
                        <p className="mt-1 text-xs leading-5 text-slate-500">{feePolicy.fullMarketDetail}</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => saveUserFees(user)}
                        disabled={accountActionDisabled}
                        className={`self-start whitespace-nowrap rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                      >
                        {accountActionBusy(feesAllActionKey) ? "保存中..." : "保存费率"}
                      </button>
                    </div>
                    <div className="mb-3 flex flex-wrap gap-2 text-xs">
                      <span className={`rounded-full px-2 py-0.5 ${accountUserKindClass(kind)}`}>{accountUserKindLabel(kind)}</span>
                      <span className={`rounded-full px-2 py-0.5 ${feePolicy.chipClass}`}>{feePolicy.chip}</span>
                      <span className="rounded-full bg-white/8 px-2 py-0.5 text-slate-300">已有覆盖 {user.fee_profiles?.length ?? 0}/{marketSymbols.length}</span>
                      <span className="rounded-full bg-amber-400/12 px-2 py-0.5 text-amber-100">仅后续成交生效</span>
                      <span className="rounded-full bg-white/8 px-2 py-0.5 text-slate-400">不回算历史费用</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2 md:max-w-xl">
                      <label className="block">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">maker fee</span>
                        <input value={edit.makerFeeRate} onChange={(event) => updateUserEdit(user, { makerFeeRate: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
                      </label>
                      <label className="block">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">taker fee</span>
                        <input value={edit.takerFeeRate} onChange={(event) => updateUserEdit(user, { takerFeeRate: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
                      </label>
                    </div>
                  </div>
                  <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
                    <div className="mb-3 flex items-center justify-between gap-3">
                      <div>
                        <h4 className="font-display text-sm">{feePolicy.title} · 单市场</h4>
                        <p className="mt-1 text-xs leading-5 text-slate-500">{feePolicy.singleMarketDetail}</p>
                      </div>
                      <span className="text-xs text-slate-500">{marketSymbols.length} markets</span>
                    </div>
                    <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                      {marketSymbols.map((symbol) => {
                        const fee = edit.marketFees[symbol] ?? {
                          makerFeeRate: markets[symbol]?.default_maker_fee_rate ?? "",
                          takerFeeRate: markets[symbol]?.default_taker_fee_rate ?? "",
                        };
                        return (
                          <div key={`${user.id}-${symbol}-fee`} className="grid gap-2 rounded-2xl bg-white/5 px-3 py-3 sm:grid-cols-[0.9fr_1fr_1fr_auto] sm:items-end">
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium text-slate-100">{symbol}</div>
                              <div className="mt-1 truncate text-xs text-slate-500">{markets[symbol]?.market_type ?? "-"}</div>
                            </div>
                            <label className="block min-w-0">
                              <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">maker</span>
                              <input
                                value={fee.makerFeeRate}
                                onChange={(event) => updateUserMarketFeeEdit(user, symbol, { makerFeeRate: event.target.value })}
                                className="w-full rounded-2xl border border-white/10 bg-slate-950/35 px-3 py-2 text-sm outline-none"
                              />
                            </label>
                            <label className="block min-w-0">
                              <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">taker</span>
                              <input
                                value={fee.takerFeeRate}
                                onChange={(event) => updateUserMarketFeeEdit(user, symbol, { takerFeeRate: event.target.value })}
                                className="w-full rounded-2xl border border-white/10 bg-slate-950/35 px-3 py-2 text-sm outline-none"
                              />
                            </label>
                            <button
                              type="button"
                              onClick={() => saveUserMarketFee(user, symbol)}
                              disabled={accountActionDisabled}
                              className={`h-9 rounded-2xl bg-emerald-400/16 px-4 text-sm text-emerald-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                            >
                              {accountActionBusy(accountActionKey(user.id, "fee", symbol)) ? "保存中..." : "保存"}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  {activeActivityUserId === user.id && selectedAccountUser?.id !== user.id && (
                    <UserActivityPanel
                      activity={userActivities[user.id]}
                      loading={loadingActivityUserId === user.id}
                    />
                  )}
                  <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
                    <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                      <div>
                        <h4 className="font-display text-sm">现货资金维护</h4>
                        <p className="mt-1 text-xs leading-5 text-slate-500">正数入账，负数扣减；会写入资金流水。重置现货资金属于测试主体恢复动作，执行前先按上方演练口径核对。</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => resetUser(user.id)}
                        disabled={accountActionDisabled}
                        className={`self-start whitespace-nowrap rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                      >
                        {accountActionBusy(resetActionKey) ? "重置中..." : "重置现货资金"}
                      </button>
                    </div>
                    <div className="grid gap-2 md:grid-cols-[0.7fr_1fr_1.4fr_auto] md:items-end">
                      <label className="block min-w-0">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">asset</span>
                        <select
                          value={adjustment.asset}
                          onChange={(event) => updateBalanceAdjustment(user, { asset: event.target.value })}
                          className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none"
                        >
                          {user.balances.map((item) => (
                            <option key={`${user.id}-${item.asset}-adjust`} value={item.asset}>{item.asset}</option>
                          ))}
                        </select>
                      </label>
                      <label className="block min-w-0">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">amount</span>
                        <input
                          value={adjustment.amount}
                          onChange={(event) => updateBalanceAdjustment(user, { amount: event.target.value })}
                          className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                        />
                      </label>
                      <label className="block min-w-0">
                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">reason</span>
                        <input
                          value={adjustment.reason}
                          onChange={(event) => updateBalanceAdjustment(user, { reason: event.target.value })}
                          className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                        />
                      </label>
                      <button
                        type="button"
                        onClick={() => adjustUser(user)}
                        disabled={accountActionDisabled}
                        className={`h-9 rounded-2xl bg-amber-400/16 px-4 text-sm text-amber-100 ${accountActionDisabled ? "cursor-not-allowed opacity-60" : ""}`}
                      >
                        {accountActionBusy(adjustActionKey) ? "执行中..." : "确认执行"}
                      </button>
                    </div>
                  </div>
                  <div className="mt-3 grid gap-2 md:grid-cols-3">
                    {user.balances.map((item) => (
                      <div key={item.asset} className="rounded-2xl bg-slate-950/35 px-3 py-3 text-sm text-slate-300">
                        <div className="text-slate-500">{item.asset}</div>
                        <div className="mt-1">可用 {item.available}</div>
                        <div className="text-slate-500">冻结 {item.frozen}</div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
              </div>
            </>
          )}
        </section>
          )}
          {adminSection === "accounting" && (
            <AccountingReconciliationPanel
              summary={reconciliationSummary}
              shadow={shadowAccountingSummary}
              outbox={financialOutboxSummary}
              proofs={accountingProofs}
              robotCheckpoints={robotFinancialCheckpoints}
              gateReadiness={accountingGateReadiness}
              running={reconciliationRunning}
              proofRunning={accountingProofRunning}
              robotCheckpointRunning={robotCheckpointRunning}
              onRun={runReconciliation}
              onCreateProof={createAccountingProof}
              onCreateRobotCheckpoint={createRobotFinancialCheckpoint}
            />
          )}
          {adminSection === "strategies" && <StrategyCatalogPage />}

        </div>
      </div>
    </AppShell>
  );
}

const marketDetailTabs: { key: MarketDetailTab; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "base", label: "基础属性" },
  { key: "rules", label: "交易规则" },
  { key: "product", label: "产品专属配置" },
  { key: "bots", label: "策略与账户" },
  { key: "logs", label: "日志与风险" },
  { key: "maintenance", label: "维护与清理" },
];

type MarketProductReview = {
  productType: string;
  primaryOwner: string;
  tradingLedger: string;
  riskAndClearing: string;
  verificationPath: string;
  boundary: string;
  tone: "spot" | "perp" | "unknown";
};

function marketProductReview(market: AdminMarketItem): MarketProductReview {
  if (market.product_type === "PERP") {
    return {
      productType: "PERP 永续合约",
      primaryOwner: "市场运营 + 合约清算",
      tradingLedger: "合约订单 / 合约保证金 / 逐仓仓位",
      riskAndClearing: "资金费、强平、ADL、保险基金和风险阶梯回合约清算",
      verificationPath: "产品配置核对交易模式、杠杆、资金费和价格源；合约清算核对保证金、仓位、流水和清算事件。",
      boundary: "不使用现货钱包抵扣保证金；合约风险参数和清算回看不在现货市场页处理。",
      tone: "perp",
    };
  }
  if (market.product_type === "SPOT") {
    return {
      productType: "SPOT 现货",
      primaryOwner: "市场运营 + 账户与资金",
      tradingLedger: "现货订单 / 现货余额 / 现货流水",
      riskAndClearing: "盘口、K 线、订单审计和机器人运行；无资金费、强平或保险基金",
      verificationPath: "基础属性/交易规则核对 symbol、tick、step 和费率；账户与资金核对现货余额与冻结；订单与成交核对现货订单。",
      boundary: "不展示合约保证金、资金费、强平、ADL 或保险基金。",
      tone: "spot",
    };
  }
  return {
    productType: market.product_type || "UNKNOWN",
    primaryOwner: "市场运营",
    tradingLedger: "需先确认产品类型",
    riskAndClearing: "未识别产品类型，不应执行产品专属风险动作",
    verificationPath: "先在市场基础属性确认 product_type，再进入对应后台域核对。",
    boundary: "未知产品不推断资金账本、清算或机器人风险归属。",
    tone: "unknown",
  };
}

function marketProductReviewClass(tone: MarketProductReview["tone"]) {
  if (tone === "perp") return "bg-violet-400/14 text-violet-100";
  if (tone === "spot") return "bg-cyan-400/14 text-cyan-100";
  return "bg-amber-400/14 text-amber-100";
}

function MarketOperationsQuickPaths({
  market,
  bots,
  instance,
  surveillance,
  strategyVersion,
  onTabChange,
}: {
  market: AdminMarketItem;
  bots: MarketBotAccount[];
  instance?: MakerInstanceStatus;
  surveillance?: MarketSurveillanceItem;
  strategyVersion: string;
  onTabChange: (tab: MarketDetailTab) => void;
}) {
  const enabledBots = bots.filter((bot) => bot.is_enabled).length;
  const blockerCount = instance?.start_readiness?.blockers?.length ?? 0;
  const warningCount = instance?.start_readiness?.warnings?.length ?? 0;
  const spreadText = surveillance ? `${fmt(surveillance.metrics.spread_pct, 4)}%` : "-";
  const isPerpGuarded = market.product_type === "PERP" && market.contract_trading_mode && market.contract_trading_mode !== "normal";
  const needsAction = !market.is_active || blockerCount > 0;
  const needsAttention = warningCount > 0 || enabledBots === 0 || !instance?.running || Boolean(isPerpGuarded) || !surveillance;
  const judgment = needsAction ? "需处理" : needsAttention ? "需关注" : "正常";
  const judgmentTone: "neutral" | "warn" | "danger" = needsAction ? "danger" : needsAttention ? "warn" : "neutral";
  const mainDomain =
    !market.is_active
      ? "市场状态"
      : blockerCount > 0
        ? "运行预检"
        : enabledBots === 0
          ? "机器人资源"
          : isPerpGuarded
            ? "产品参数 / 交易模式"
            : !instance?.running
              ? "实例控制"
              : warningCount > 0
                ? "预检警告"
                : !surveillance
                  ? "市场概览"
                  : "常规观察";
  const nextText =
    needsAction
      ? "先处理市场状态或实例预检阻断，再启动或继续测试。"
      : needsAttention
        ? "先核对机器人、实例、产品参数或预检警告。"
        : "当前可继续观察盘口、成交和策略运行状态。";
  const quickPaths: Array<{
    label: string;
    status: string;
    helper: string;
    tab: MarketDetailTab;
    tone: "cyan" | "emerald" | "amber" | "rose" | "slate";
  }> = [
    {
      label: "看市场概览",
      status: `${market.product_type} · ${market.is_active ? "启用" : "暂停"}`,
      helper: "先确认市场身份、成交概况和盘口摘要。",
      tab: "overview",
      tone: market.is_active ? "cyan" : "rose",
    },
    {
      label: "调规则/费率",
      status: `maker ${market.default_maker_fee_rate} · taker ${market.default_taker_fee_rate}`,
      helper: "价格步长、数量步长、最小成交和市场默认手续费。",
      tab: "rules",
      tone: "slate",
    },
    {
      label: "看产品参数",
      status:
        market.product_type === "PERP"
          ? `${contractTradingModeLabel(market.contract_trading_mode)} · ${fmt(market.max_leverage, 0)}x`
          : `${market.base_asset}/${market.quote_asset}`,
      helper: market.product_type === "PERP" ? "合约模式、杠杆、资金费和标记价来源。" : "现货资产、参考价和产品基础信息。",
      tab: "product",
      tone: isPerpGuarded ? "amber" : market.product_type === "PERP" ? "amber" : "slate",
    },
    {
      label: "配机器人资源",
      status: `${enabledBots}/${bots.length} 启用`,
      helper: "机器人 UID、API Key、角色和初始资金模板。",
      tab: "bots",
      tone: enabledBots > 0 ? "slate" : "amber",
    },
    {
      label: "调策略参数",
      status: strategyDisplayName(strategyVersion),
      helper: "当前市场策略参数；需要重启的策略仍走实例控制。",
      tab: "strategy",
      tone: "slate",
    },
    {
      label: instance?.running ? "管运行实例" : "启动实例",
      status: `${adminStatusText(instance?.status)} · 阻断 ${blockerCount} · 警告 ${warningCount}`,
      helper: `预检、启动、停止、重启和日志入口；当前 spread ${spreadText}。`,
      tab: "instance",
      tone: blockerCount > 0 ? "rose" : instance?.running ? "emerald" : "amber",
    },
  ];

  const toneClass = {
    cyan: "bg-cyan-400/14 text-cyan-100 hover:bg-cyan-400/20",
    emerald: "bg-emerald-400/14 text-emerald-100 hover:bg-emerald-400/20",
    amber: "bg-amber-400/14 text-amber-100 hover:bg-amber-400/20",
    rose: "bg-rose-500/14 text-rose-100 hover:bg-rose-500/20",
    slate: "bg-white/7 text-slate-200 hover:bg-white/12",
  } as const;

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h3 className="font-display text-base text-slate-100">市场操作路径</h3>
          <p className="mt-1 text-sm text-slate-500">按运营人员的常用顺序放入口；按钮只切换分区，不直接执行危险动作。</p>
        </div>
        <span className={`w-fit rounded-full px-3 py-1 text-xs ${judgmentTone === "danger" ? "bg-rose-500/16 text-rose-100" : judgmentTone === "warn" ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
          {judgment}
        </span>
      </div>
      <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
          <div className="mt-1 text-sm text-slate-200">{judgment} · 阻断 {blockerCount} · 警告 {warningCount}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
          <div className="mt-1 text-sm text-slate-200">{mainDomain}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
          <div className={`mt-1 text-sm ${judgmentTone === "danger" ? "text-rose-100" : judgmentTone === "warn" ? "text-amber-100" : "text-slate-400"}`}>{nextText}</div>
        </div>
      </div>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {quickPaths.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={() => onTabChange(item.tab)}
            className={`min-h-[104px] rounded-2xl border border-white/8 p-3 text-left transition ${toneClass[item.tone]}`}
          >
            <div className="flex items-start justify-between gap-3">
              <span className="text-sm font-medium">{item.label}</span>
              <span className="shrink-0 rounded-full bg-slate-950/35 px-2 py-0.5 font-mono text-[11px] text-current">{item.status}</span>
            </div>
            <p className="mt-3 text-xs leading-5 text-slate-400">{item.helper}</p>
          </button>
        ))}
      </div>
    </section>
  );
}

function instanceReadinessFixPath(item: StartReadinessItem, severity: "blocker" | "warning", market: AdminMarketItem): {
  owner: string;
  next: string;
  verification: string;
  boundary: string;
  tab: MarketDetailTab;
  tone: "danger" | "warn" | "neutral";
} {
  const code = item.code || "";
  const lower = `${item.code} ${item.label} ${item.detail}`.toLowerCase();
  if (code === "market_inactive") {
    return {
      owner: "市场运营",
      next: "先确认是否应恢复市场，再回基础属性核对市场状态。",
      verification: "看市场状态、交易模式、操作记录和当前委托影响。",
      boundary: "恢复市场仍走原暂停/恢复入口和确认边界，不在预检建议里直接执行。",
      tab: "base",
      tone: "danger",
    };
  }
  if (
    code === "no_enabled_maker"
    || code === "missing_api_credentials"
    || code === "missing_initial_quote"
    || code === "missing_initial_base"
    || code === "missing_reference_price"
    || code === "legacy_bot_uid"
    || code === "perp_low_maker_count"
    || code === "v2_low_maker_count"
    || code === "lite_low_maker_count"
    || code === "no_flow_bot"
    || lower.includes("maker")
    || lower.includes("api")
    || lower.includes("flow")
  ) {
    return {
      owner: "机器人资源",
      next: "回机器人资源核对 UID、API Key、角色、启用状态和初始资金模板。",
      verification: market.product_type === "PERP"
        ? "PERP maker/hedge 还要回合约清算核对保证金账户和持仓风险。"
        : "SPOT maker/flow 还要回账户与资金核对现货余额和冻结。",
      boundary: "补机器人、补凭据或保存账号仍使用原按钮和二次确认；这里不创建或修改账号。",
      tab: "bots",
      tone: severity === "blocker" ? "danger" : "warn",
    };
  }
  if (
    code === "missing_contract_wallet"
    || code === "empty_contract_margin"
    || lower.includes("contract")
    || lower.includes("margin")
    || lower.includes("保证金")
  ) {
    return {
      owner: "合约清算 / 机器人资源",
      next: "先看机器人资源里的合约保证金钱包摘要，再去合约清算的保证金账户复核。",
      verification: "核对保证金钱包、可用保证金、占用保证金、合约流水和最近拒单。",
      boundary: "合约保证金调账、资金费、强平和 ADL 不在市场预检区执行。",
      tab: "bots",
      tone: severity === "blocker" ? "danger" : "warn",
    };
  }
  if (code === "empty_quote_balance" || code === "empty_base_balance" || lower.includes("balance") || lower.includes("余额")) {
    return {
      owner: "账户与资金 / 机器人资源",
      next: "先看机器人资源里的资金快照，再到账户与资金核对现货钱包。",
      verification: "核对可用、冻结、现货流水、调账记录和机器人绑定市场。",
      boundary: "现货入账/扣款仍在账户维护区执行并保留确认，不在预检建议里执行。",
      tab: "bots",
      tone: severity === "blocker" ? "danger" : "warn",
    };
  }
  if (code === "runtime_bundle_warning" || lower.includes("strategy") || lower.includes("runtime")) {
    return {
      owner: "机器人运营",
      next: "回策略参数核对当前保存策略、运行策略和参数是否一致。",
      verification: "保存参数后需要重启的策略，继续到实例控制核对 PID、heartbeat 和运行策略。",
      boundary: "策略保存和保存并重启仍保留原确认；这里不自动保存或重启。",
      tab: "strategy",
      tone: severity === "blocker" ? "danger" : "warn",
    };
  }
  return {
    owner: "机器人运营 / 日志",
    next: "先看日志与风险，再按错误详情回到对应业务域。",
    verification: "核对 last_error、最新日志、盘口风险、系统状态和操作记录。",
    boundary: "未知预检信号只作为排查线索，不自动推断调账、清算或重启动作。",
    tab: "logs",
    tone: severity === "blocker" ? "danger" : "neutral",
  };
}

function InstanceReadinessRecoveryGuide({
  market,
  checks,
  onTabChange,
}: {
  market: AdminMarketItem;
  checks?: StartReadiness | null;
  onTabChange: (tab: MarketDetailTab) => void;
}) {
  const items = [
    ...(checks?.blockers ?? []).map((item) => ({ item, severity: "blocker" as const })),
    ...(checks?.warnings ?? []).map((item) => ({ item, severity: "warning" as const })),
  ];
  const rows = items.length
    ? items.map(({ item, severity }) => ({
        item,
        severity,
        fix: instanceReadinessFixPath(item, severity, market),
      }))
    : [{
        item: { code: "ready", label: "预检通过", detail: "当前没有阻断项或警告。" },
        severity: "warning" as const,
        fix: {
          owner: "机器人运营",
          next: "可以回实例控制启动；启动后继续核对 PID、heartbeat、盘口双边和运行策略。",
          verification: "启动后看实例状态、最近日志、订单与成交、账户或保证金变化。",
          boundary: "预检通过不代表跳过启动确认、停机边界或后续风险观察。",
          tab: "instance" as MarketDetailTab,
          tone: "neutral" as const,
        },
      }];
  const toneClass = {
    danger: "bg-rose-500/10 text-rose-100",
    warn: "bg-amber-400/10 text-amber-100",
    neutral: "bg-white/7 text-slate-200",
  } as const;

  return (
    <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
      <div className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h4 className="font-display text-base text-slate-100">预检处置建议</h4>
          <p className="mt-1 text-xs text-slate-500">把阻断和警告翻译成下一步排查路径；这里不自动修复、不启动实例。</p>
        </div>
        <span className="text-xs text-slate-500">本地导航</span>
      </div>
      <div className="grid gap-2">
        {rows.map(({ item, severity, fix }) => (
          <div key={`${severity}-${item.code}-${item.label}`} className={`rounded-xl border border-white/8 p-3 ${toneClass[fix.tone]}`}>
            <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{severity === "blocker" ? "阻断" : item.code === "ready" ? "就绪" : "警告"}：{item.label}</span>
                  <span className="rounded-full bg-slate-950/35 px-2 py-0.5 font-mono text-[11px] text-current">{item.code}</span>
                </div>
                <p className="mt-2 text-xs leading-5 text-slate-400">{item.detail}</p>
              </div>
              <button type="button" onClick={() => onTabChange(fix.tab)} className="w-fit shrink-0 rounded-xl bg-slate-950/35 px-3 py-1.5 text-xs text-current transition hover:bg-slate-900/80">
                去{marketDetailTabs.find((tab) => tab.key === fix.tab)?.label ?? "对应分区"}
              </button>
            </div>
            <div className="mt-3 grid gap-2 text-xs leading-5 md:grid-cols-3">
              <div>
                <span className="block text-slate-500">归属</span>
                <span className="text-slate-200">{fix.owner}</span>
              </div>
              <div>
                <span className="block text-slate-500">下一步</span>
                <span className="text-slate-300">{fix.next}</span>
              </div>
              <div>
                <span className="block text-slate-500">边界</span>
                <span className="text-slate-400">{fix.boundary}</span>
              </div>
            </div>
            <p className="mt-2 text-xs leading-5 text-slate-500">{fix.verification}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function MarketOperationsBoundaryStrip({
  market,
  bots,
  instance,
  surveillance,
  strategyVersion,
  onTabChange,
}: {
  market: AdminMarketItem;
  bots: MarketBotAccount[];
  instance?: MakerInstanceStatus;
  surveillance?: MarketSurveillanceItem;
  strategyVersion: string;
  onTabChange: (tab: MarketDetailTab) => void;
}) {
  const enabledMakers = bots.filter((bot) => bot.is_enabled && bot.role === "maker").length;
  const enabledFlows = bots.filter((bot) => bot.is_enabled && bot.role === "flow").length;
  const isPerp = market.product_type === "PERP";
  const blockerCount = instance?.start_readiness?.blockers?.length ?? 0;
  const warningCount = instance?.start_readiness?.warnings?.length ?? 0;
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    boundary: string;
    tab: MarketDetailTab;
  }> = [
    {
      domain: "市场身份",
      owner: "市场运营",
      status: `${market.product_type} · ${market.market_type} · ${market.is_active ? "enabled" : "paused"}`,
      boundary: "管理 symbol、产品类型、市场类型和启停状态；启停影响新单接入，不代表清算或机器人运行状态。",
      tab: "base",
    },
    {
      domain: "交易规则 / 费率",
      owner: "市场运营",
      status: `tick ${market.price_tick} · step ${market.qty_step}`,
      boundary: "维护价格精度、数量步长、最小成交和默认费率；用户级费率覆盖仍回到账户与资金。",
      tab: "rules",
    },
    {
      domain: "产品参数",
      owner: isPerp ? "市场运营 / 合约清算索引" : "市场运营",
      status: isPerp
        ? `${contractTradingModeLabel(market.contract_trading_mode)} · ${fmt(market.max_leverage, 0)}x · ${market.funding_rate_mode ?? "-"}`
        : `${market.base_asset}/${market.quote_asset} · reference ${fmt(market.reference_price, 4)}`,
      boundary: isPerp
        ? "合约交易模式、杠杆、资金费和标记价来源在市场运营保存；风险阶梯和清算回看在合约清算。"
        : "SPOT 只维护现货资产、参考价和交易规则，不展示合约保证金字段。",
      tab: "product",
    },
    {
      domain: "机器人资源",
      owner: "机器人账号",
      status: `${bots.length} 绑定 · maker ${enabledMakers} · flow ${enabledFlows}`,
      boundary: "绑定机器人 UID、API Key、角色和资金快照；实例启停和策略运行回机器人运营。",
      tab: "bots",
    },
    {
      domain: "策略参数",
      owner: "机器人运营",
      status: strategyDisplayName(strategyVersion),
      boundary: "维护当前市场策略参数；SPOT 多数热生效，PERP 策略切换以实例重启后的运行策略为准。",
      tab: "strategy",
    },
    {
      domain: "实例控制",
      owner: "机器人运营",
      status: `${adminStatusText(instance?.status)} · 阻断 ${blockerCount} · 警告 ${warningCount}`,
      boundary: "启动、停止、停止并撤单、重启只作用当前市场实例；不处理账户调账或清算动作。",
      tab: "instance",
    },
    {
      domain: "日志 / 盘口风险",
      owner: "市场运营 / 风险监控",
      status: `${surveillance?.status ?? "-"} · open ${surveillance?.metrics.open_order_count ?? "-"}`,
      boundary: "查看盘口健康、扫盘预估、铺盘和日志；跨域风险进入风险监控，系统一致性回系统与审计。",
      tab: "logs",
    },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">市场运营边界</h3>
        <p className="mt-1 text-sm text-slate-500">市场详情按币对组织操作：市场配置、产品参数、机器人资源、策略实例和风险观察各归其位；这里只导航，不改变任何 source-of-truth。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">市场域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <button type="button" onClick={() => onTabChange(row.tab)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
                    打开
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MarketProductReviewStrip({ market }: { market: AdminMarketItem }) {
  const review = marketProductReview(market);
  const exportReview = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const safeSymbol = market.symbol.replace(/[^A-Za-z0-9_-]/g, "_");
    downloadCsv(
      `admin_market_product_review_${safeSymbol}_${stamp}.csv`,
      [
        "symbol",
        "product_type",
        "contract_trading_mode",
        "contract_trading_mode_label",
        "trading_mode_scenario",
        "trading_mode_pre_check",
        "trading_mode_post_check",
        "trading_mode_recovery_path",
        "trading_mode_boundary_note",
        "primary_owner",
        "trading_ledger",
        "risk_and_clearing",
        "verification_path",
        "boundary_note",
      ],
      [
        [
          market.symbol,
          market.product_type,
          market.product_type === "PERP" ? normalizeContractTradingMode(market.contract_trading_mode) : "",
          market.product_type === "PERP" ? contractTradingModeLabel(market.contract_trading_mode) : "",
          market.product_type === "PERP" ? contractTradingModeRunbook(market.contract_trading_mode).scenario : "",
          market.product_type === "PERP" ? contractTradingModeRunbook(market.contract_trading_mode).preCheck : "",
          market.product_type === "PERP" ? contractTradingModeRunbook(market.contract_trading_mode).postCheck : "",
          market.product_type === "PERP" ? contractTradingModeRunbook(market.contract_trading_mode).recovery : "",
          market.product_type === "PERP" ? contractTradingModeRunbookBoundaryNote(market.contract_trading_mode) : "",
          review.primaryOwner,
          review.tradingLedger,
          review.riskAndClearing,
          review.verificationPath,
          review.boundary,
        ],
      ],
    );
  };

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h3 className="font-display text-base text-slate-100">产品类型核对口径</h3>
          <p className="mt-1 text-sm text-slate-500">先确认该市场是现货还是合约，再进入对应后台核对账本、风险和清算；这里是只读核对，不执行保存或控制动作。</p>
        </div>
        <button type="button" onClick={exportReview} className="w-fit rounded-xl bg-white/8 px-3 py-2 text-sm text-slate-100 transition hover:bg-white/12">
          导出当前产品口径 CSV
        </button>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">产品类型</th>
              <th className="px-3 py-2 font-normal">主后台</th>
              <th className="px-3 py-2 font-normal">交易 / 账本</th>
              <th className="px-3 py-2 font-normal">风险 / 清算</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            <tr className="border-t border-white/8 align-top">
              <td className="px-3 py-2">
                <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] ${marketProductReviewClass(review.tone)}`}>
                  {review.productType}
                </span>
              </td>
              <td className="px-3 py-2 text-slate-100">{review.primaryOwner}</td>
              <td className="px-3 py-2 leading-5">{review.tradingLedger}</td>
              <td className="px-3 py-2 leading-5">{review.riskAndClearing}</td>
              <td className="px-3 py-2 leading-5 text-slate-500">{review.verificationPath}</td>
              <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MarketFeeGovernanceStrip({
  market,
  users,
}: {
  market: AdminMarketItem;
  users: AdminUser[];
}) {
  const usersWithMarketOverride = users.filter((user) => (
    user.fee_profiles?.some((profile) => profile.symbol === market.symbol)
  ));
  const customerOverrideCount = usersWithMarketOverride.filter((user) => accountUserKindForUser(user) === "customer").length;
  const robotOverrideCount = usersWithMarketOverride.filter((user) => {
    const kind = accountUserKindForUser(user);
    return kind === "spot_robot" || kind === "contract_robot";
  }).length;
  const isZeroDefault = feeRateIsZero(market.default_maker_fee_rate) && feeRateIsZero(market.default_taker_fee_rate);
  const rows = [
    {
      domain: "市场默认",
      owner: "市场运营",
      status: `maker ${market.default_maker_fee_rate} · taker ${market.default_taker_fee_rate}`,
      policy: "仅作为没有 UID 覆盖时的兜底费率；新建市场和模板默认 maker/taker 为 0。",
      boundary: "保存市场默认费率只影响后续成交，不回算历史成交、现货流水、合约 total_fees 或 PnL。",
    },
    {
      domain: "UID 覆盖",
      owner: "账户与资金 / 账户维护",
      status: `${usersWithMarketOverride.length} 个 UID 已单独设置 · 客户 ${customerOverrideCount}`,
      policy: "撮合计费先查 UID+市场费率，再回退本市场默认；普通客户单独优惠或测试费率应放在账户维护。",
      boundary: "本页不批量改写用户 fee_profiles，避免把市场兜底误当客户专属设置。",
    },
    {
      domain: "机器人 UID",
      owner: "机器人账号 / 账户维护",
      status: `${robotOverrideCount} 个机器人 UID 在本市场有覆盖`,
      policy: "机器人本质是特殊 UID，费率参与对账即可；新增机器人默认 maker/taker 为 0。",
      boundary: "机器人策略参数、库存和高频成交留存策略不在市场默认费率里处理。",
    },
    {
      domain: "保存边界",
      owner: "市场运营",
      status: isZeroDefault ? "当前市场默认 0" : "当前市场默认非 0",
      policy: "“保存默认费率”只保存市场兜底 maker/taker；“保存交易规则”保存 tick、step、最小数量和最小成交额。",
      boundary: "不会创建审计日志之外的新账务动作，不触发调账，不修改机器人落库策略。",
    },
  ];

  return (
    <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h4 className="font-display text-base text-slate-100">市场费率治理口径</h4>
          <p className="mt-1 text-sm text-slate-500">这里说明市场默认费率、单 UID 覆盖和机器人 UID 的优先级；实际保存入口仍保持分开。</p>
        </div>
        <span className={`w-fit rounded-full px-3 py-1 text-xs ${isZeroDefault ? "bg-emerald-400/14 text-emerald-100" : "bg-amber-400/14 text-amber-100"}`}>
          {isZeroDefault ? "默认 0" : "默认非 0"}
        </span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">费率域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">计费原则</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.policy}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ContractTradingModeRunbookStrip({ mode }: { mode?: string | null }) {
  const currentMode = normalizeContractTradingMode(mode);
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">交易模式切换演练口径</h3>
          <p className="mt-1 text-sm text-slate-500">用于切换 `normal / reduce_only / paused` 前后的人工核对；这里只给恢复路径，不新增审批、自动回滚或执行入口。</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs ${contractTradingModeClass(currentMode)}`}>
          当前：{contractTradingModeLabel(currentMode)}
        </span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1180px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">目标模式</th>
              <th className="px-3 py-2 font-normal">适用场景</th>
              <th className="px-3 py-2 font-normal">切换前核对</th>
              <th className="px-3 py-2 font-normal">切换后核对</th>
              <th className="px-3 py-2 font-normal">恢复 / 回退路径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {contractTradingModeRunbookRows.map((rowMode) => {
              const runbook = contractTradingModeRunbook(rowMode);
              const isCurrent = rowMode === currentMode;
              return (
                <tr key={rowMode} className={`border-t border-white/8 align-top ${isCurrent ? "bg-cyan-400/[0.045]" : ""}`}>
                  <td className="px-3 py-2">
                    <div className="flex flex-col gap-1">
                      <span className={`w-fit rounded-full px-2.5 py-1 text-[11px] ${contractTradingModeClass(rowMode)}`}>
                        {contractTradingModeLabel(rowMode)}
                      </span>
                      <span className="font-mono text-[11px] text-slate-500">{rowMode}</span>
                    </div>
                  </td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{runbook.scenario}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{runbook.preCheck}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{runbook.postCheck}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{runbook.recovery}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{runbook.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-slate-600">切换保存仍使用现有“保存产品配置”按钮和二次确认；后端写入边界不变。</p>
    </section>
  );
}

function MarketUnsavedChangesStrip({
  changedFields,
  pending,
  scopeLabel,
  onRevert,
}: {
  changedFields: MarketEditField[];
  pending: boolean;
  scopeLabel: string;
  onRevert?: () => void;
}) {
  const changedLabels = changedFields.map((item) => item.label);
  const changedText =
    changedLabels.length > 5
      ? `${changedLabels.slice(0, 5).join("、")} 等 ${changedLabels.length} 项`
      : changedLabels.join("、");
  const toneClass = pending
    ? "border-cyan-300 text-cyan-100"
    : changedFields.length > 0
      ? "border-amber-300 text-amber-100"
      : "border-emerald-300 text-emerald-100";
  const title = pending ? "保存中" : changedFields.length > 0 ? "有未保存修改" : "当前无未保存修改";
  const detail = pending
    ? `${scopeLabel} 正在提交，完成后会刷新为后台最新配置。`
    : changedFields.length > 0
      ? `${scopeLabel} 已修改：${changedText}。保存前请确认这些是本次要改的配置。`
      : `${scopeLabel} 与最近一次后台加载的配置一致；修改字段后再保存。`;

  return (
    <div className={`mb-4 flex flex-col gap-2 border-l-2 pl-3 text-xs leading-5 sm:flex-row sm:items-start sm:justify-between ${toneClass}`}>
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        <div className="text-slate-400">{detail}</div>
      </div>
      {changedFields.length > 0 && onRevert && (
        <button
          type="button"
          onClick={onRevert}
          disabled={pending}
          className={`shrink-0 rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 ${pending ? "cursor-not-allowed opacity-60" : ""}`}
        >
          恢复本页修改
        </button>
      )}
    </div>
  );
}

function FeeBpsField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const format = (rate: string) => rate === "" ? "" : Number.isFinite(Number(rate)) ? (Number(rate) * 10000).toFixed(6).replace(/\.?0+$/, "") || "0" : rate;
  const [draft, setDraft] = useState(() => format(value));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setDraft(format(value)); }, [value, editing]);
  return <label className="block min-w-0"><span className="mb-1 block text-xs text-slate-500">{label}（bps）</span>
    <input type="text" inputMode="decimal" value={draft} onFocus={() => setEditing(true)} onBlur={() => setEditing(false)}
      onChange={event => { const next = event.target.value; if (!/^-?\d*(\.\d{0,6})?$/.test(next)) return; setDraft(next); onChange(next === "" || next === "-" || next === "." || next === "-." ? "" : (Number(next) / 10000).toFixed(10)); }}
      className="w-full rounded-xl border border-white/10 bg-slate-950/45 px-3 py-2.5 text-sm text-slate-100 outline-none" />
  </label>;
}

function AdminField({
  label,
  value,
  onChange,
  type = "text",
  readOnly = false,
}: {
  label: string;
  value: string | number | boolean | null | undefined;
  onChange?: (value: string) => void;
  type?: string;
  readOnly?: boolean;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-xs text-slate-500">{label}</span>
      <input
        type={type}
        value={value === null || value === undefined ? "" : String(value)}
        readOnly={readOnly}
        onInput={(event) => onChange?.(event.currentTarget.value)}
        onChange={(event) => onChange?.(event.target.value)}
        className={`w-full rounded-xl border border-white/10 px-3 py-2.5 text-sm outline-none ${
          readOnly ? "bg-white/5 text-slate-400" : "bg-slate-950/45 text-slate-100"
        }`}
      />
    </label>
  );
}

function MarketSymbolInput({
  label = "币对",
  value,
  onChange,
  className = "w-full rounded-xl border border-white/10 bg-slate-950/45 px-3 py-2.5 text-sm text-slate-100 outline-none",
}: {
  label?: string;
  value: string | null | undefined;
  onChange: (value: string) => void;
  className?: string;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-xs text-slate-500">{label}</span>
      <input
        value={value ?? ""}
        onChange={(event) => onChange(normalizeMarketSymbolInput(event.target.value))}
        autoCapitalize="characters"
        spellCheck={false}
        placeholder="例如 BTCUSDT"
        className={className}
      />
    </label>
  );
}

function AdminSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-xs text-slate-500">{label}</span>
      <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} className="w-full rounded-xl border border-white/10 bg-slate-950/55 px-3 py-2.5 text-sm outline-none">
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  );
}

const operationSearchText = (value: unknown) => {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const operationContextMatchesSymbol = (context: Record<string, unknown> | undefined, symbol: string) => {
  const normalizedSymbol = normalizeMarketSymbolInput(symbol);
  if (!normalizedSymbol) return false;
  const directSymbol = typeof context?.symbol === "string" ? normalizeMarketSymbolInput(context.symbol) : "";
  if (directSymbol && directSymbol === normalizedSymbol) return true;
  return operationSearchText(context).toUpperCase().includes(normalizedSymbol.toUpperCase());
};

const operationResultMatchesSymbol = (item: AdminOperationResult, symbol: string) => {
  const normalizedSymbol = normalizeMarketSymbolInput(symbol);
  if (!normalizedSymbol) return false;
  if (operationContextMatchesSymbol(item.context, normalizedSymbol)) return true;
  return [item.title, item.message, item.result]
    .map((value) => operationSearchText(value).toUpperCase())
    .some((value) => value.includes(normalizedSymbol.toUpperCase()));
};

function MarketOperationStatusPanel({
  symbol,
  activeOperation,
  lastOperationResult,
  onOpenOperationAudit,
}: {
  symbol: string;
  activeOperation: AdminActiveOperation | null;
  lastOperationResult: AdminOperationResult | null;
  onOpenOperationAudit: (symbol: string) => void;
}) {
  const active = activeOperation && operationContextMatchesSymbol(activeOperation.context, symbol) ? activeOperation : null;
  const recent = lastOperationResult && operationResultMatchesSymbol(lastOperationResult, symbol) ? lastOperationResult : null;
  if (!active && !recent) return null;

  const recentTone =
    recent?.status === "error"
      ? "border-rose-400/14 bg-rose-500/8"
      : active
        ? "border-cyan-400/14 bg-cyan-400/8"
        : "border-emerald-400/14 bg-emerald-400/8";

  return (
    <section className={`panel rounded-2xl p-4 ${recentTone}`}>
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-display text-base text-slate-100">市场操作状态</h3>
            {active && <span className="rounded-full bg-cyan-400/16 px-2 py-0.5 text-xs text-cyan-100">执行中</span>}
            {recent && (
              <span className={`rounded-full px-2 py-0.5 text-xs ${operationStatusClass(recent.status)}`}>
                {recent.status === "success" ? "最近成功" : "最近异常"}
              </span>
            )}
          </div>
          {active && (
            <p className="mt-1 text-sm text-cyan-100">
              正在执行：{active.title}。当前只显示前端观测状态，真实结果仍以业务数据和操作记录为准。
            </p>
          )}
          {recent && (
            <div className="mt-2 text-sm text-slate-300">
              <div className="font-medium text-slate-100">{recent.title}</div>
              <div className="mt-1 leading-5">{recent.message}</div>
              <div className="mt-1 text-xs text-slate-500">{bjDateTime(recent.ts)}</div>
            </div>
          )}
          {recent?.status === "error" && recent.recovery && (
            <div className="mt-3 border-t border-white/10 pt-3 text-xs leading-5 text-slate-300">
              <div className="font-medium text-rose-100">{recent.recovery.title}</div>
              <ul className="mt-1 space-y-1">
                {recent.recovery.steps.slice(0, 3).map((step) => <li key={step}>- {step}</li>)}
              </ul>
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => onOpenOperationAudit(symbol)}
          className="shrink-0 rounded-xl bg-white/10 px-3 py-2 text-sm text-slate-100 hover:bg-white/15"
        >
          看该市场操作记录
        </button>
      </div>
      {recent?.result != null && (
        <details className="mt-3 border-t border-white/10 pt-3">
          <summary className="cursor-pointer list-none text-xs text-slate-400">查看返回摘要</summary>
          <pre className="mt-2 max-h-[180px] overflow-auto rounded-xl bg-slate-950/35 p-3 text-xs leading-5 text-slate-300">
            {jsonPreview(recent.result)}
          </pre>
        </details>
      )}
    </section>
  );
}

function MarketWorkspacePanel({
  users,
  marketSymbols,
  markets,
  persistedMarkets,
  marketTemplates,
  selectedMarketSymbol,
  onMarketChange,
  detailTab,
  onDetailTabChange,
  marketSurveillance,
  marketBots,
  makerInstances,
  marketStrategies,
  strategySelections,
  marketStrategyConfigEdits,
  strategyApplyStatuses,
  lastOperationResult,
  activeAdminOperation,
  newMarket,
  onNewMarketChange,
  onApplyMarketTemplate,
  onCreateMarket,
  onUpdateMarketField,
  onRevertMarketFields,
  onSaveMarketConfig,
  onSaveMarketFees,
  onToggleMarket,
  onResetMarket,
  onWipeMarketKlines,
  onWipeMarketData,
  seedBookForms,
  onUpdateSeedBook,
  onSeedMarketBook,
  sweepPreviewForms,
  sweepPreviews,
  onUpdateSweepPreview,
  onPreviewSweep,
  marketBotForms,
  defaultMarketBotForm,
  onMarketBotFormChange,
  onCreateMarketBot,
  onCreateDefaultBots,
  onCreateFlowBot,
  onUpdateMarketBot,
  onStrategyChange,
  onStrategyConfigChange,
  onSaveStrategy,
  onSaveStrategyAndRestart,
  onStartInstance,
  onStopInstance,
  onStopAndCancelInstance,
  onRestartInstance,
  renderConfigNode,
  onOpenBotsStrategy,
  onOpenOperationAudit,
  onReload,
}: {
  users: AdminUser[];
  marketSymbols: string[];
  markets: Record<string, AdminMarketItem>;
  persistedMarkets: Record<string, AdminMarketItem>;
  marketTemplates: MarketTemplate[];
  selectedMarketSymbol: string;
  onMarketChange: (symbol: string) => void;
  detailTab: MarketDetailTab;
  onDetailTabChange: (tab: MarketDetailTab) => void;
  marketSurveillance: Record<string, MarketSurveillanceItem>;
  marketBots: Record<string, MarketBotAccount[]>;
  makerInstances: Record<string, MakerInstanceStatus>;
  marketStrategies: Record<string, MarketStrategyState>;
  strategySelections: Record<string, string>;
  marketStrategyConfigEdits: Record<string, LiquidityRuntimeConfig>;
  strategyApplyStatuses: Record<string, StrategyApplyStatus>;
  lastOperationResult: AdminOperationResult | null;
  activeAdminOperation: AdminActiveOperation | null;
  newMarket: NewMarketFormState;
  onNewMarketChange: Dispatch<SetStateAction<NewMarketFormState>>;
  onApplyMarketTemplate: (template: MarketTemplate) => void;
  onCreateMarket: () => void;
  onUpdateMarketField: (symbol: string, field: keyof AdminMarketItem, value: string | number | boolean) => void;
  onRevertMarketFields: (symbol: string, fields: MarketEditField[]) => void;
  onSaveMarketConfig: (symbol: string) => void;
  onSaveMarketFees: (symbol: string) => void;
  onToggleMarket: (symbol: string) => void;
  onResetMarket: (symbol: string) => void;
  onWipeMarketKlines: (symbol: string) => void;
  onWipeMarketData: (symbol: string) => void;
  seedBookForms: Record<string, SeedBookState>;
  onUpdateSeedBook: (symbol: string, patch: Partial<SeedBookState>) => void;
  onSeedMarketBook: (symbol: string) => void;
  sweepPreviewForms: Record<string, SweepPreviewState>;
  sweepPreviews: Record<string, SweepPreviewResult>;
  onUpdateSweepPreview: (symbol: string, patch: Partial<SweepPreviewState>) => void;
  onPreviewSweep: (symbol: string) => void;
  marketBotForms: Record<string, MarketBotFormState>;
  defaultMarketBotForm: (market?: AdminMarketItem) => MarketBotFormState;
  onMarketBotFormChange: (symbol: string, patch: Partial<MarketBotFormState>) => void;
  onCreateMarketBot: (symbol: string) => void;
  onCreateDefaultBots: (symbol: string) => void;
  onCreateFlowBot: (symbol: string) => void;
  onUpdateMarketBot: (symbol: string, botId: number, edit: MarketBotEditState) => void;
  onStrategyChange: (symbol: string, strategy: string) => void;
  onStrategyConfigChange: (symbol: string, path: Array<string | number>, nextValue: unknown) => void;
  onSaveStrategy: (symbol: string) => void;
  onSaveStrategyAndRestart: (symbol: string) => void;
  onStartInstance: (symbol: string) => void;
  onStopInstance: (symbol: string) => void;
  onStopAndCancelInstance: (symbol: string) => void;
  onRestartInstance: (symbol: string) => void;
  renderConfigNode: (
    symbol: string,
    node: unknown,
    onChangeValue: (symbol: string, path: Array<string | number>, nextValue: unknown) => void,
    path?: Array<string | number>,
  ) => React.ReactNode;
  onOpenBotsStrategy: (symbol: string, strategyKey: string) => void;
  onOpenOperationAudit: (symbol: string) => void;
  onReload: () => void;
}) {
  const [workspaceTab, setWorkspaceTab] = useState<"manage" | "create">("manage");
  const symbol = selectedMarketSymbol || marketSymbols[0] || "";
  const market = symbol ? markets[symbol] : undefined;
  const persistedMarket = symbol ? persistedMarkets[symbol] : undefined;
  const bots = symbol ? marketBots[symbol] ?? [] : [];
  const surveillance = symbol ? marketSurveillance[symbol] : undefined;
  const instance = symbol ? makerInstances[symbol] : undefined;
  const creatingMarket = activeAdminOperation?.title === "创建交易币对";
  const newMarketSymbol = normalizeMarketSymbolInput(newMarket.symbol);
  const newMarketExists = !!newMarketSymbol && marketSymbols.includes(newMarketSymbol);
  const newMarketCreateDisabled = creatingMarket || !newMarketSymbol || newMarketExists;
  const activeMarketOperation = market && activeAdminOperation && operationContextMatchesSymbol(activeAdminOperation.context, market.symbol)
    ? activeAdminOperation
    : null;
  const marketStatusPending = activeMarketOperation?.title === "暂停市场" || activeMarketOperation?.title === "恢复市场";
  const marketConfigPending = activeMarketOperation?.title === "保存市场参数" || activeMarketOperation?.title === "切换合约交易模式";
  const marketFeesPending = activeMarketOperation?.title === "保存市场默认费率";
  const marketMaintenancePending =
    activeMarketOperation?.title === "撤销市场挂单"
    || activeMarketOperation?.title === "清除历史 K 线"
    || activeMarketOperation?.title === "清理行情历史";
  const marketDirtyCounts = Object.fromEntries(
    marketSymbols.map((item) => [
      item,
      changedMarketEditFieldCount(markets[item], persistedMarkets[item], ["base", "rules", "fees", "product"]),
    ]),
  ) as Record<string, number>;
  const dirtyMarketSymbols = marketSymbols.filter((item) => (marketDirtyCounts[item] ?? 0) > 0);
  const totalMarketDirtyCount = dirtyMarketSymbols.reduce((total, item) => total + (marketDirtyCounts[item] ?? 0), 0);
  const handleReloadMarkets = () => {
    if (totalMarketDirtyCount > 0) {
      const visibleSymbols = dirtyMarketSymbols.slice(0, 5).join("、");
      const suffix = dirtyMarketSymbols.length > 5 ? ` 等 ${dirtyMarketSymbols.length} 个市场` : "";
      const confirmed = window.confirm(
        `重新加载会丢弃当前页面里的本地未保存市场修改。\n\n`
        + `涉及：${visibleSymbols}${suffix}\n`
        + `未保存字段：${totalMarketDirtyCount} 项\n\n`
        + "确认重新加载后台最新配置？",
      );
      if (!confirmed) return;
    }
    onReload();
  };
  const marketTabDirtyCounts: Partial<Record<MarketDetailTab, number>> = market && persistedMarket
    ? {
        base: changedMarketEditFieldCount(market, persistedMarket, ["base"]),
        rules: changedMarketEditFieldCount(market, persistedMarket, ["rules", "fees"]),
        product: changedMarketEditFieldCount(market, persistedMarket, ["product"]),
      }
    : {};

  return (
    <section className="space-y-4">
      <nav className="flex gap-2">{(["manage", "create"] as const).map(value => <button key={value} onClick={() => setWorkspaceTab(value)} className={`rounded-xl px-4 py-2 ${workspaceTab === value ? "bg-cyan-400/16 text-cyan-100" : "bg-white/5"}`}>{value === "manage" ? "交易币对" : "创建交易币对"}</button>)}</nav>
      {workspaceTab === "create" ? (        <NewMarketCard
          form={newMarket}
          templates={marketTemplates}
          onChange={onNewMarketChange}
          onApplyTemplate={onApplyMarketTemplate}
          onCreate={onCreateMarket}
          creating={creatingMarket}
          existingSymbols={marketSymbols}
          lastOperationResult={lastOperationResult}
        />) : <div className="grid gap-4 xl:grid-cols-[250px_minmax(0,1fr)]">
      <div className="space-y-4">
        <section className="panel rounded-2xl p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <div>
              <h2 className="font-display text-xl">市场运营</h2>
              <p className="mt-1 text-sm text-slate-400">选择币对后，切换右侧功能页签。</p>
            </div>
            <button
              type="button"
              onClick={() => setWorkspaceTab("create")}
              className={`rounded-xl bg-emerald-400/16 px-3 py-2 text-sm text-emerald-100 `}
            >
              {creatingMarket ? "创建中..." : "创建"}
            </button>
          </div>
          <div className="max-h-[420px] space-y-2 overflow-y-auto pr-1 scrollbar">
            {marketSymbols.map((item) => {
              const itemMarket = markets[item];
              const itemDirtyCount = marketDirtyCounts[item] ?? 0;
              const active = item === symbol;
              return (
                <button
                  key={item}
                  type="button"
                  onClick={() => onMarketChange(item)}
                  className={`w-full rounded-xl border px-3 py-3 text-left transition ${
                    active ? "border-cyan-400/22 bg-cyan-400/12" : "border-white/8 bg-white/5 hover:bg-white/8"
                  }`}
                  title={itemDirtyCount ? `${item} 有 ${itemDirtyCount} 项未保存修改` : item}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm text-white">{item}</span>
                    <div className="flex shrink-0 items-center gap-1">
                      {itemDirtyCount > 0 && (
                        <span className="rounded-full bg-amber-300/18 px-2 py-0.5 text-[10px] leading-4 text-amber-100">未保存</span>
                      )}
                      <span className={`rounded-full px-2 py-0.5 text-[11px] ${itemMarket?.product_type === "PERP" ? "bg-violet-400/14 text-violet-100" : "bg-cyan-400/14 text-cyan-100"}`}>
                        {itemMarket?.product_type ?? "-"}
                      </span>
                    </div>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">{itemMarket?.market_type ?? "-"} · {itemMarket?.is_active ? "enabled" : "paused"}</div>
                </button>
              );
            })}
          </div>
        </section>

      </div>
      {market ? (
        <section className="min-w-0 space-y-4">
          <div className="panel rounded-2xl p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-display text-xl">{market.symbol} 市场详情</h2>
                  <span className={`rounded-full px-2.5 py-1 text-xs ${market.product_type === "PERP" ? "bg-violet-400/14 text-violet-100" : "bg-cyan-400/14 text-cyan-100"}`}>{market.product_type}</span>
                  <span className={market.is_active ? "rounded-full bg-emerald-400/14 px-2.5 py-1 text-xs text-emerald-100" : "rounded-full bg-amber-400/14 px-2.5 py-1 text-xs text-amber-100"}>
                    {market.is_active ? "已启用" : "已暂停"}
                  </span>
                </div>
                <p className="mt-1 text-sm text-slate-400">按功能切换页签；铺单与刷量在各自页面按币对配置和启停。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => onOpenBotsStrategy(market.symbol, strategySelections[market.symbol] ?? marketStrategies[market.symbol]?.selected?.strategy_key ?? (market.product_type === "PERP" ? "PERP_MM" : "LITE"))}
                  className="rounded-xl bg-cyan-400/16 px-3 py-2 text-sm text-cyan-100"
                >
                  铺单配置
                </button>
                <button type="button" onClick={() => onDetailTabChange("logs")} className="rounded-xl bg-white/8 px-3 py-2 text-sm text-slate-100">
                  看日志
                </button>
                <button
                  type="button"
                  onClick={handleReloadMarkets}
                  title={totalMarketDirtyCount > 0 ? `重新加载前会确认丢弃 ${totalMarketDirtyCount} 项本地未保存修改` : "重新加载后台最新配置"}
                  className={`rounded-xl px-3 py-2 text-sm text-slate-100 ${totalMarketDirtyCount > 0 ? "bg-amber-400/14 text-amber-100" : "bg-white/8"}`}
                >
                  重新加载
                </button>
              </div>
            </div>
          </div>
          <div className="panel rounded-2xl p-4">
            <div className="flex flex-wrap gap-2">
              {marketDetailTabs.map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => onDetailTabChange(tab.key)}
                  className={`flex shrink-0 items-center gap-2 rounded-xl px-3 py-2 text-sm transition ${detailTab === tab.key ? "bg-cyan-400/16 text-cyan-100" : "bg-white/6 text-slate-300 hover:bg-white/10"}`}
                  title={marketTabDirtyCounts[tab.key] ? `${tab.label} 有 ${marketTabDirtyCounts[tab.key]} 项未保存修改` : tab.label}
                >
                  <span>{tab.label}</span>
                  {Boolean(marketTabDirtyCounts[tab.key]) && (
                    <span className="rounded-full bg-amber-300/18 px-2 py-0.5 text-[10px] leading-4 text-amber-100">未保存</span>
                  )}
                </button>
              ))}
            </div>
          </div>
          {detailTab === "overview" && <>
          <div className="border-y border-white/8 bg-white/[0.025] px-4 py-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-slate-100">低频市场状态动作</div>
                <div className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
                  {market.is_active
                    ? "暂停市场会拒绝新的下单请求；不会撤销当前挂单、停止机器人实例、调账或清算。"
                    : "恢复市场只重新允许新的下单请求进入撮合校验；不会自动启动机器人实例、铺盘、撤单、调账或清算。"}
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
                  <span className="rounded-full bg-white/8 px-2 py-0.5">执行前会二次确认</span>
                  <span className="rounded-full bg-white/8 px-2 py-0.5">不撤单</span>
                  <span className="rounded-full bg-white/8 px-2 py-0.5">不调账</span>
                  <span className="rounded-full bg-white/8 px-2 py-0.5">不清算</span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => onToggleMarket(market.symbol)}
                disabled={marketStatusPending}
                className={`shrink-0 rounded-xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 ${marketStatusPending ? "cursor-not-allowed opacity-60" : ""}`}
              >
                {marketStatusPending ? "处理中..." : market.is_active ? "暂停市场" : "恢复市场"}
              </button>
            </div>
          </div>
          </>}
          {detailTab === "maintenance" && <>
          <div className="border-b border-white/8 bg-rose-500/[0.035] px-4 py-3">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <div className="text-sm font-medium text-rose-100">低频危险动作：市场维护与历史清理</div>
                <div className="mt-1 max-w-3xl text-xs leading-5 text-rose-100/75">
                  这些动作只用于本地测试市场恢复或数据清理。撤销全部挂单会改变订单状态和账户冻结资金；清除历史 K 线不影响挂单、机器人和成交历史；清理行情历史仅清理 K 线与最近成交展示，保留订单、账务和持仓。提交仍会二次确认，并由后端 confirm_execute 拦截未确认请求。
                </div>
              </div>
              <div className="flex w-full flex-col gap-2 sm:flex-row lg:w-auto">
                <button
                  type="button"
                  onClick={() => onResetMarket(market.symbol)}
                  disabled={marketMaintenancePending}
                  className={`rounded-xl bg-rose-500/16 px-4 py-2 text-sm text-rose-100 ${marketMaintenancePending ? "cursor-not-allowed opacity-60" : ""}`}
                >
                  撤销全部挂单
                </button>
                <button
                  type="button"
                  onClick={() => onWipeMarketKlines(market.symbol)}
                  disabled={marketMaintenancePending}
                  className={`rounded-xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 ${marketMaintenancePending ? "cursor-not-allowed opacity-60" : ""}`}
                >
                  清除历史 K 线
                </button>
                <button
                  type="button"
                  onClick={() => onWipeMarketData(market.symbol)}
                  disabled={marketMaintenancePending}
                  className={`rounded-xl bg-white/8 px-4 py-2 text-sm text-slate-100 ${marketMaintenancePending ? "cursor-not-allowed opacity-60" : ""}`}
                >
                  清理行情历史（保留账务）
                </button>
              </div>
            </div>
          </div>
          </>}
          {detailTab === "overview" && <>
          <MarketOperationStatusPanel
            symbol={market.symbol}
            activeOperation={activeAdminOperation}
            lastOperationResult={lastOperationResult}
            onOpenOperationAudit={onOpenOperationAudit}
          />
          </>}
          <MarketDetailTabPanel
            tab={detailTab}
            market={market}
            persistedMarket={persistedMarket}
            users={users}
            bots={bots}
            surveillance={surveillance}
            instance={instance}
            strategyState={marketStrategies[market.symbol]}
            strategyVersion={strategySelections[market.symbol] ?? marketStrategies[market.symbol]?.selected?.strategy_key ?? (market.product_type === "PERP" ? "PERP_MM" : "LITE")}
            strategyConfig={marketStrategyConfigEdits[market.symbol] ?? marketStrategies[market.symbol]?.selected?.effective_config ?? {}}
            applyStatus={strategyApplyStatuses[market.symbol]}
            seedBook={seedBookForms[market.symbol] ?? defaultSeedBook(market, surveillance)}
            sweepForm={sweepPreviewForms[market.symbol] ?? defaultSweepPreview()}
            sweepPreview={sweepPreviews[market.symbol]}
            botForm={marketBotForms[market.symbol] ?? defaultMarketBotForm(market)}
            onUpdateMarketField={(field, value) => onUpdateMarketField(market.symbol, field, value)}
            onRevertMarketFields={(fields) => onRevertMarketFields(market.symbol, fields)}
            onSaveMarketConfig={() => onSaveMarketConfig(market.symbol)}
            onSaveMarketFees={() => onSaveMarketFees(market.symbol)}
            marketConfigPending={marketConfigPending}
            marketFeesPending={marketFeesPending}
            onUpdateSeedBook={(patch) => onUpdateSeedBook(market.symbol, patch)}
            onSeedMarketBook={() => onSeedMarketBook(market.symbol)}
            onUpdateSweepPreview={(patch) => onUpdateSweepPreview(market.symbol, patch)}
            onPreviewSweep={() => onPreviewSweep(market.symbol)}
            onMarketBotFormChange={(patch) => onMarketBotFormChange(market.symbol, patch)}
            onCreateMarketBot={() => onCreateMarketBot(market.symbol)}
            onCreateDefaultBots={() => onCreateDefaultBots(market.symbol)}
            onCreateFlowBot={() => onCreateFlowBot(market.symbol)}
            onUpdateMarketBot={(botId, edit) => onUpdateMarketBot(market.symbol, botId, edit)}
            onStrategyChange={(next) => onStrategyChange(market.symbol, next)}
            onStrategyConfigChange={onStrategyConfigChange}
            onSaveStrategy={() => onSaveStrategy(market.symbol)}
            onSaveStrategyAndRestart={() => onSaveStrategyAndRestart(market.symbol)}
            onStartInstance={() => onStartInstance(market.symbol)}
            onStopInstance={() => onStopInstance(market.symbol)}
            onStopAndCancelInstance={() => onStopAndCancelInstance(market.symbol)}
            onRestartInstance={() => onRestartInstance(market.symbol)}
            renderConfigNode={renderConfigNode}
            onTabChange={onDetailTabChange}
          />
        </section>
      ) : (
        <section className="panel rounded-2xl p-8 text-center text-sm text-slate-500">暂无市场，请先创建。</section>
      )}
      </div>}
    </section>
  );
}

function NewMarketCard({
  form,
  templates,
  onChange,
  onApplyTemplate,
  onCreate,
  creating,
  existingSymbols,
  lastOperationResult,
}: {
  form: NewMarketFormState;
  templates: MarketTemplate[];
  onChange: Dispatch<SetStateAction<NewMarketFormState>>;
  onApplyTemplate: (template: MarketTemplate) => void;
  onCreate: () => void;
  creating: boolean;
  existingSymbols: string[];
  lastOperationResult: AdminOperationResult | null;
}) {
  const update = (patch: Partial<NewMarketFormState>) => onChange((current) => ({ ...current, ...patch }));
  const isPerp = form.productType === "PERP";
  const normalizedSymbol = normalizeMarketSymbolInput(form.symbol);
  const symbolExists = !!normalizedSymbol && existingSymbols.includes(normalizedSymbol);
  const createDisabled = creating || !normalizedSymbol || symbolExists;
  const createResult =
    lastOperationResult?.title === "创建交易币对"
    && normalizedSymbol
    && operationResultMatchesSymbol(lastOperationResult, normalizedSymbol)
      ? lastOperationResult
      : null;
  const createNotice = !normalizedSymbol
    ? { tone: "warn" as const, title: "请先输入币对", detail: "例如 BTCUSDT 或 BTCUSDT-PERP；这里只做本机测试市场配置。" }
    : creating
        ? { tone: "busy" as const, title: `正在创建 ${normalizedSymbol}`, detail: "创建完成后会刷新市场列表；不要重复提交同一个市场。" }
        : createResult
          ? {
              tone: createResult.status === "success" ? "success" as const : "error" as const,
              title: createResult.title,
              detail: createResult.message,
            }
          : symbolExists
            ? { tone: "warn" as const, title: `${normalizedSymbol} 已存在`, detail: "直接在右侧市场详情维护规则、产品参数、机器人资源或实例。" }
          : null;
  const noticeClass =
    createNotice?.tone === "success"
      ? "border-emerald-400/14 bg-emerald-400/8 text-emerald-100"
      : createNotice?.tone === "error"
        ? "border-rose-400/14 bg-rose-500/8 text-rose-100"
        : createNotice?.tone === "busy"
          ? "border-cyan-400/14 bg-cyan-400/8 text-cyan-100"
          : "border-amber-400/14 bg-amber-400/8 text-amber-100";
  return (
    <section className="panel rounded-2xl p-4">
      <details open>
        <summary className="cursor-pointer list-none">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="font-display text-base">创建交易币对</h3>
              <p className="mt-1 text-xs text-slate-500">创建后自动分配策略执行身份和所需凭据，策略默认停止。</p>
            </div>
            <button
              type="button"
              disabled={createDisabled}
              onClick={(event) => { event.preventDefault(); onCreate(); }}
              className={`rounded-xl bg-emerald-400/16 px-3 py-2 text-sm text-emerald-100 ${createDisabled ? "cursor-not-allowed opacity-60" : ""}`}
            >
              {creating ? "创建中..." : "保存"}
            </button>
          </div>
        </summary>
        <div className="mt-3 space-y-4">
          {createNotice && (
            <div className={`rounded-xl border px-3 py-2 text-xs leading-5 ${noticeClass}`}>
              <div className="font-medium">{createNotice.title}</div>
              <div className="mt-0.5 opacity-80">{createNotice.detail}</div>
            </div>
          )}
          {templates.length > 0 && (
            <div className="flex gap-2 overflow-x-auto pb-1 scrollbar">
              {templates.slice(0, 8).map((template) => (
                <button key={template.template_id} type="button" onClick={() => onApplyTemplate(template)} className="shrink-0 rounded-xl border border-white/8 bg-white/5 px-3 py-2 text-left text-xs text-slate-300 hover:bg-white/8">
                  <span className="block font-medium text-slate-100">{template.symbol}</span>
                  <span>{template.product_type ?? "SPOT"} · {template.market_type}</span>
                </button>
              ))}
            </div>
          )}
          <div className="grid gap-3 md:grid-cols-2">
            <InstalledMakerSelect product={isPerp ? "PERP" : "SPOT"} value={form.defaultMakerStrategy || ""} onChange={value => update({ defaultMakerStrategy: value })} />
            <AdminField label="上游交易币对" value={form.priceSourceSymbol || form.symbol.replace(/-PERP$/, "")} onChange={value => update({ priceSourceSymbol: value })} />
            <p className="text-xs text-slate-400 md:col-span-2">合约 SIMPLE_BBO / CONTRACT_LADDER 使用内部免钱包身份；现货 SIMPLE_BBO、LITE / PERP_MM 使用普通账户，自动初始化模拟资产并校验余额。</p>
            <FeeBpsField label="默认 Maker 费率" value={form.defaultMakerFeeRate} onChange={value => update({ defaultMakerFeeRate: value })} />
            <FeeBpsField label="默认 Taker 费率" value={form.defaultTakerFeeRate} onChange={value => update({ defaultTakerFeeRate: value })} />
            <MarketSymbolInput label="币对" value={form.symbol} onChange={(value) => update({ symbol: value })} />
            <AdminSelect label="产品类型" value={form.productType} options={["SPOT", "PERP"]} onChange={(value) => update({ productType: value, createDefaultBots: value === "SPOT" && form.createDefaultBots })} />
            <AdminField label="初始参考价格" value={form.referencePrice} onChange={value => update({ referencePrice: value })} />
            <AdminSelect label="市场分类" value={form.marketType} options={["listed", "mainstream"]} onChange={(value) => update({ marketType: value })} />
            <div className="grid grid-cols-2 gap-2">
              <AdminField label="基础资产" value={form.baseAsset} onChange={(value) => update({ baseAsset: value.toUpperCase() })} />
              <AdminField label={isPerp ? "保证金资产" : "计价资产"} value={isPerp ? form.marginAsset : form.quoteAsset} onChange={(value) => isPerp ? update({ marginAsset: value.toUpperCase() }) : update({ quoteAsset: value.toUpperCase() })} />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <AdminField label="价格步长" value={form.priceTick} onChange={(value) => update({ priceTick: value })} />
              <AdminField label="数量步长" value={form.qtyStep} onChange={(value) => update({ qtyStep: value })} />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <AdminField label="最小数量" value={form.minQty} onChange={(value) => update({ minQty: value })} />
              <AdminField label={`最小交易金额（${form.quoteAsset || "USDT"}）`} value={form.minNotional} onChange={(value) => update({ minNotional: value })} />
            </div>
            {isPerp ? (
              <div className="rounded-xl border border-violet-400/12 bg-violet-400/6 p-3">
                <div className="mb-2 text-xs font-medium text-violet-100">PERP 专属默认值</div>
                <div className="grid gap-2">
                  <AdminField label="最大杠杆" value={form.maxLeverage} onChange={(value) => update({ maxLeverage: value })} />
                  <AdminField label="默认杠杆" value={form.defaultLeverage} onChange={(value) => update({ defaultLeverage: value })} />
                  <AdminField label="初始资金费率" value={form.fundingRate} onChange={(value) => update({ fundingRate: value })} />
                </div>
              </div>
            ) : (
              <p className="text-sm text-slate-400">自动创建 2 个普通做市账户及模拟资产；创建后在铺单策略页面启动。</p>
            )}
          </div>
        </div>
      </details>
    </section>
  );
}

function MarketDetailTabPanel({
  tab,
  market,
  persistedMarket,
  users,
  bots,
  surveillance,
  instance,
  strategyState,
  strategyVersion,
  strategyConfig,
  applyStatus,
  seedBook,
  sweepForm,
  sweepPreview,
  botForm,
  onUpdateMarketField,
  onRevertMarketFields,
  onSaveMarketConfig,
  onSaveMarketFees,
  marketConfigPending,
  marketFeesPending,
  onUpdateSeedBook,
  onSeedMarketBook,
  onUpdateSweepPreview,
  onPreviewSweep,
  onMarketBotFormChange,
  onCreateMarketBot,
  onCreateDefaultBots,
  onCreateFlowBot,
  onUpdateMarketBot,
  onStrategyChange,
  onStrategyConfigChange,
  onSaveStrategy,
  onSaveStrategyAndRestart,
  onStartInstance,
  onStopInstance,
  onStopAndCancelInstance,
  onRestartInstance,
  renderConfigNode,
  onTabChange,
}: {
  tab: MarketDetailTab;
  market: AdminMarketItem;
  persistedMarket?: AdminMarketItem;
  users: AdminUser[];
  bots: MarketBotAccount[];
  surveillance?: MarketSurveillanceItem;
  instance?: MakerInstanceStatus;
  strategyState?: MarketStrategyState;
  strategyVersion: string;
  strategyConfig: LiquidityRuntimeConfig;
  applyStatus?: StrategyApplyStatus;
  seedBook: SeedBookState;
  sweepForm: SweepPreviewState;
  sweepPreview?: SweepPreviewResult;
  botForm: MarketBotFormState;
  onUpdateMarketField: (field: keyof AdminMarketItem, value: string | number | boolean) => void;
  onRevertMarketFields: (fields: MarketEditField[]) => void;
  onSaveMarketConfig: () => void;
  onSaveMarketFees: () => void;
  marketConfigPending: boolean;
  marketFeesPending: boolean;
  onUpdateSeedBook: (patch: Partial<SeedBookState>) => void;
  onSeedMarketBook: () => void;
  onUpdateSweepPreview: (patch: Partial<SweepPreviewState>) => void;
  onPreviewSweep: () => void;
  onMarketBotFormChange: (patch: Partial<MarketBotFormState>) => void;
  onCreateMarketBot: () => void;
  onCreateDefaultBots: () => void;
  onCreateFlowBot: () => void;
  onUpdateMarketBot: (botId: number, edit: MarketBotEditState) => void;
  onStrategyChange: (strategy: string) => void;
  onStrategyConfigChange: (symbol: string, path: Array<string | number>, nextValue: unknown) => void;
  onSaveStrategy: () => void;
  onSaveStrategyAndRestart: () => void;
  onStartInstance: () => void;
  onStopInstance: () => void;
  onStopAndCancelInstance: () => void;
  onRestartInstance: () => void;
  renderConfigNode: (
    symbol: string,
    node: unknown,
    onChangeValue: (symbol: string, path: Array<string | number>, nextValue: unknown) => void,
    path?: Array<string | number>,
  ) => React.ReactNode;
  onTabChange: (tab: MarketDetailTab) => void;
}) {
  const isPerp = market.product_type === "PERP";
  const enabledMakers = bots.filter((bot) => bot.is_enabled && bot.role === "maker").length;
  const enabledFlows = bots.filter((bot) => bot.is_enabled && bot.role === "flow").length;
  const baseChangedFields = changedMarketEditFields(market, persistedMarket, "base");
  const rulesChangedFields = changedMarketEditFields(market, persistedMarket, "rules");
  const feeChangedFields = changedMarketEditFields(market, persistedMarket, "fees");
  const productChangedFields = changedMarketEditFields(market, persistedMarket, "product");
  const rulesAndFeeChangedFields = [...rulesChangedFields, ...feeChangedFields];

  if (tab === "maintenance") return null;
  if (["bots", "strategy", "instance"].includes(tab)) return <section className="panel rounded-2xl p-4 space-y-4">
    <h3 className="text-lg">{market.symbol} 执行账户与策略</h3>
    <p className="text-sm text-slate-400">执行账户随币对策略自动配置。内部铺单没有钱包；普通做市账户遵守余额、保证金和下单校验。API 凭据由系统自动生成。</p>
    <div className="flex gap-3"><a className="rounded bg-cyan-400/15 px-4 py-2" href={`/admin?section=maker_config&market=${encodeURIComponent(market.symbol)}`}>配置 / 启停铺单</a><a className="rounded bg-white/10 px-4 py-2" href={`/admin?section=flow_config&market=${encodeURIComponent(market.symbol)}`}>配置 / 启停刷量</a></div>
    <div className="space-y-2">{bots.map(bot => <div key={bot.id} className="font-mono text-sm">UID {bot.uid} · {bot.role} · {bot.strategy_role} · {bot.is_enabled ? "已绑定" : "未启用"}</div>)}</div>
  </section>;
  if (tab === "overview") {
    return (
      <section className="space-y-4">
        <div className="panel rounded-2xl p-4">
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
            <SurveillanceMetric label="产品类型" value={market.product_type} />
            <SurveillanceMetric label="市场状态" value={market.is_active ? "启用" : "暂停"} />
            <SurveillanceMetric label="实例状态" value={adminStatusText(instance?.status)} />
            <SurveillanceMetric label="机器人" value={`${bots.length} 个 / maker ${enabledMakers}`} />
            <SurveillanceMetric label="策略" value={strategyDisplayName(strategyVersion)} />
            <SurveillanceMetric label="盘口健康" value={surveillance?.status ?? "-"} />
          </div>
        </div>
        <MarketSurveillancePanel item={surveillance} />
      </section>
    );
  }

  if (tab === "base") {
    return (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="font-display text-lg">基础属性</h3>
          <button
            type="button"
            onClick={onSaveMarketConfig}
            disabled={marketConfigPending}
            className={`rounded-xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100 ${marketConfigPending ? "cursor-not-allowed opacity-60" : ""}`}
          >
            {marketConfigPending ? "保存中..." : "保存基础属性"}
          </button>
        </div>
        <MarketUnsavedChangesStrip
          changedFields={baseChangedFields}
          pending={marketConfigPending}
          scopeLabel="基础属性"
          onRevert={() => onRevertMarketFields(baseChangedFields)}
        />
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <AdminField label="symbol" value={market.symbol} readOnly />
          <AdminSelect label="product_type" value={market.product_type} options={["SPOT", "PERP"]} onChange={(value) => onUpdateMarketField("product_type", value)} />
          <AdminSelect label="market_type" value={market.market_type} options={["listed", "mainstream"]} onChange={(value) => onUpdateMarketField("market_type", value)} />
          <AdminField label="base_asset" value={market.base_asset} readOnly />
          <AdminField label="quote_asset" value={market.quote_asset} readOnly />
          {isPerp && <AdminField label="margin_asset" value={market.margin_asset ?? ""} onChange={(value) => onUpdateMarketField("margin_asset", value.toUpperCase())} />}
        </div>
      </section>
    );
  }

  if (tab === "rules") {
    return (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h3 className="font-display text-lg">交易规则与默认费率</h3>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onSaveMarketConfig}
              disabled={marketConfigPending}
              className={`rounded-xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100 ${marketConfigPending ? "cursor-not-allowed opacity-60" : ""}`}
            >
              {marketConfigPending ? "保存中..." : "保存交易规则"}
            </button>
            <button
              type="button"
              onClick={onSaveMarketFees}
              disabled={marketFeesPending}
              className={`rounded-xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100 ${marketFeesPending ? "cursor-not-allowed opacity-60" : ""}`}
            >
              {marketFeesPending ? "保存中..." : "保存默认费率"}
            </button>
          </div>
        </div>
        <MarketUnsavedChangesStrip
          changedFields={rulesAndFeeChangedFields}
          pending={marketConfigPending || marketFeesPending}
          scopeLabel="交易规则与默认费率"
          onRevert={() => onRevertMarketFields(rulesAndFeeChangedFields)}
        />
        <MarketFeeGovernanceStrip market={market} users={users} />
        <p className="mb-3 text-xs text-slate-400">1 bps = 0.01%；填 5 表示 0.05%。最小交易金额是价格 × 数量，合约按名义金额计算，不是保证金。UID 专属费率优先于币对默认费率。</p>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <AdminField label="价格精度（最小变动单位）" value={market.price_tick} onChange={(value) => onUpdateMarketField("price_tick", value)} />
          <AdminField label="数量精度（最小变动单位）" value={market.qty_step} onChange={(value) => onUpdateMarketField("qty_step", value)} />
          <AdminField label="最小下单数量" value={market.min_qty} onChange={(value) => onUpdateMarketField("min_qty", value)} />
          <AdminField label={`最小交易金额（${market.quote_asset || "USDT"}）`} value={market.min_notional} onChange={(value) => onUpdateMarketField("min_notional", value)} />
          <p className="text-xs text-slate-400">精度填写步长，如 0.01；整数数量填 1。小数位数自动推导。若最少交易 1 个，数量精度和最小下单数量都填 1。</p>
          <FeeBpsField label="默认 Maker 费率" value={market.default_maker_fee_rate} onChange={(value) => onUpdateMarketField("default_maker_fee_rate", value)} />
          <FeeBpsField label="默认 Taker 费率" value={market.default_taker_fee_rate} onChange={(value) => onUpdateMarketField("default_taker_fee_rate", value)} />
        </div>
      </section>
    );
  }

  if (tab === "product") {
    return (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-lg">产品专属配置</h3>
            <p className="mt-1 text-sm text-slate-400">{isPerp ? "PERP 只显示合约保证金、资金费和标记价字段。" : "SPOT 只显示现货资产和库存初始化语义。"}</p>
          </div>
          <button
            type="button"
            onClick={onSaveMarketConfig}
            disabled={marketConfigPending}
            className={`rounded-xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100 ${marketConfigPending ? "cursor-not-allowed opacity-60" : ""}`}
          >
            {marketConfigPending ? "保存中..." : "保存产品配置"}
          </button>
        </div>
        <MarketUnsavedChangesStrip
          changedFields={productChangedFields}
          pending={marketConfigPending}
          scopeLabel="产品专属配置"
          onRevert={() => onRevertMarketFields(productChangedFields)}
        />
        {isPerp ? (
          <>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              <AdminField label="max_leverage" value={market.max_leverage} onChange={(value) => onUpdateMarketField("max_leverage", value)} />
              <AdminField label="default_leverage" value={market.default_leverage} onChange={(value) => onUpdateMarketField("default_leverage", value)} />
              <AdminField label="maintenance_margin_rate" value={market.maintenance_margin_rate} onChange={(value) => onUpdateMarketField("maintenance_margin_rate", value)} />
              <AdminField label="funding_rate" value={market.funding_rate} onChange={(value) => onUpdateMarketField("funding_rate", value)} />
              <AdminField label="funding_interval_hours" value={market.funding_interval_hours} type="number" onChange={(value) => onUpdateMarketField("funding_interval_hours", Number(value))} />
              <AdminSelect label="index_price_source" value={market.index_price_source ?? "binance"} options={["binance", "manual"]} onChange={(value) => onUpdateMarketField("index_price_source", value)} />
              <AdminSelect label="mark_price_mode" value={market.mark_price_mode ?? "orderbook"} options={["orderbook"]} onChange={(value) => onUpdateMarketField("mark_price_mode", value)} />
              <AdminSelect label="funding_rate_mode" value={market.funding_rate_mode ?? "binance"} options={["binance", "formula"]} onChange={(value) => onUpdateMarketField("funding_rate_mode", value)} />
              <AdminSelect label="contract_trading_mode" value={market.contract_trading_mode ?? "normal"} options={["normal", "reduce_only", "paused"]} onChange={(value) => onUpdateMarketField("contract_trading_mode", value)} />
              <div className="rounded-xl border border-amber-300/20 bg-amber-300/8 px-3 py-2 text-sm leading-6 text-amber-100 md:col-span-2 xl:col-span-3">
                <span className="font-medium">合约交易模式是高风险开关。</span>
                <span className="ml-2 text-amber-100/80">
                  {contractTradingModeLabel(market.contract_trading_mode)}：{contractTradingModeDescription(market.contract_trading_mode)}
                  保存时如发生模式变化会二次确认。
                </span>
              </div>
            </div>
            <div className="mt-4">
              <ContractTradingModeRunbookStrip mode={market.contract_trading_mode} />
            </div>
          </>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <SurveillanceMetric label="base 资产" value={market.base_asset} />
            <SurveillanceMetric label="quote 资产" value={market.quote_asset} />
            <AdminField label="reference_price" value={market.reference_price ?? ""} onChange={(value) => onUpdateMarketField("reference_price", value)} />
            <SurveillanceMetric label="库存初始化" value="在机器人资源中设置 quote/base" />
          </div>
        )}
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <MarketSurveillancePanel item={surveillance} />
      <section className="panel rounded-2xl p-4">
        <h3 className="font-display text-lg">日志与风险</h3>
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SurveillanceMetric label="last_error" value={instance?.last_error ?? "-"} />
          <SurveillanceMetric label="心跳" value={bjDateTime(instance?.last_heartbeat_at ?? instance?.last_metrics_ts)} />
          <SurveillanceMetric label="PID" value={String(instance?.pid ?? "-")} />
          <SurveillanceMetric label="metrics" value={instance?.metrics_present ? "present" : "missing"} />
        </div>
        <pre className="mt-4 max-h-[360px] overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950/45 p-3 text-xs leading-5 text-slate-400 scrollbar">
          {(instance?.last_log_lines ?? ["暂无本次运行日志"]).join("\n")}
        </pre>
      </section>
		      <section className="panel rounded-2xl p-4">
        <h3 className="font-display text-lg">扫盘风险预估</h3>
        <div className="mt-3 grid gap-2 md:grid-cols-[0.7fr_0.8fr_1fr_0.7fr_auto] md:items-end">
          <AdminSelect label="side" value={sweepForm.side} options={["buy", "sell"]} onChange={(value) => onUpdateSweepPreview({ side: value as "buy" | "sell" })} />
          <AdminSelect label="mode" value={sweepForm.mode} options={["quote_amount", "quantity"]} onChange={(value) => onUpdateSweepPreview({ mode: value as "quote_amount" | "quantity" })} />
          <AdminField label={sweepForm.mode === "quote_amount" ? "quote amount" : "quantity"} value={sweepForm.mode === "quote_amount" ? sweepForm.quoteAmount : sweepForm.quantity} onChange={(value) => sweepForm.mode === "quote_amount" ? onUpdateSweepPreview({ quoteAmount: value }) : onUpdateSweepPreview({ quantity: value })} />
          <AdminField label="depth" value={sweepForm.depth} onChange={(value) => onUpdateSweepPreview({ depth: value })} />
          <button type="button" onClick={onPreviewSweep} className="h-9 rounded-xl bg-amber-400/16 px-4 text-sm text-amber-100">预估</button>
        </div>
        {sweepPreview && <div className="mt-3 rounded-xl bg-slate-950/35 px-3 py-2 text-sm text-slate-300">{sweepPreview.summary}</div>}
      </section>
      <section className="panel rounded-2xl border-rose-300/14 bg-rose-500/[0.035] p-4">
        <div className="mb-3 flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <h3 className="font-display text-lg text-rose-50">低频危险动作：{isPerp ? "初始化合约盘口" : "初始化测试盘口"}</h3>
            <p className="mt-1 max-w-4xl text-xs leading-5 text-rose-100/75">
              只用于本地测试市场恢复或演练铺盘。执行会按当前参数放置测试挂单；勾选撤销时会先撤销该市场当前挂单，可能改变订单簿和{isPerp ? "合约保证金占用" : "账户冻结资金"}。提交仍会二次确认，并由后端 confirm_execute 拦截未确认请求。
            </p>
          </div>
          <span className="shrink-0 rounded-full bg-rose-400/14 px-2 py-0.5 text-[11px] text-rose-100">
            不要连续重复铺盘
          </span>
        </div>
        <div className="grid gap-2 md:grid-cols-[1fr_0.7fr_0.8fr_1fr_auto] md:items-end">
          <AdminField label="mid price" value={seedBook.midPrice} onChange={(value) => onUpdateSeedBook({ midPrice: value })} />
          <AdminField label="levels" value={seedBook.levels} onChange={(value) => onUpdateSeedBook({ levels: value })} />
          <AdminField label="gap ticks" value={seedBook.gapTicks} onChange={(value) => onUpdateSeedBook({ gapTicks: value })} />
          <AdminField label="base qty" value={seedBook.quantity} onChange={(value) => onUpdateSeedBook({ quantity: value })} />
          <button type="button" onClick={onSeedMarketBook} className="h-9 rounded-xl bg-rose-500/16 px-4 text-sm text-rose-100">确认铺盘</button>
        </div>
        <label className="mt-3 flex items-center gap-2 text-xs text-rose-100/75">
          <input type="checkbox" checked={seedBook.cancelExisting} onChange={(event) => onUpdateSeedBook({ cancelExisting: event.target.checked })} className="h-4 w-4 accent-cyan-400" />
          执行前撤销该市场当前挂单
        </label>
      </section>
    </section>
  );
}

function OrderAuditPanel({
  orders,
  trades,
  orderQueryMeta,
  tradeQueryMeta,
  loading,
  error,
  product,
  symbol,
  userId,
  status,
  dataScope,
  markets,
  marketSymbols,
  users,
  dataRetentionDomains,
  onProductChange,
  onSymbolChange,
  onUserChange,
  onStatusChange,
  onDataScopeChange,
  onRefresh,
}: {
  orders: AdminOrderAuditItem[];
  trades: AdminTradeAuditItem[];
  orderQueryMeta: AdminAuditQueryMeta | null;
  tradeQueryMeta: AdminAuditQueryMeta | null;
  loading: boolean;
  error: string | null;
  product: AuditProductScope;
  symbol: string;
  userId: string;
  status: AuditStatusScope;
  dataScope: OrderAuditDataScope;
  markets: Record<string, AdminMarketItem>;
  marketSymbols: string[];
  users: AdminUser[];
  dataRetentionDomains?: DataRetentionDomainSummary;
  onProductChange: Dispatch<SetStateAction<AuditProductScope>>;
  onSymbolChange: Dispatch<SetStateAction<string>>;
  onUserChange: Dispatch<SetStateAction<string>>;
  onStatusChange: Dispatch<SetStateAction<AuditStatusScope>>;
  onDataScopeChange: Dispatch<SetStateAction<OrderAuditDataScope>>;
  onRefresh: () => void;
}) {
  const auditDataScope = dataScope;
  const scopedOrders = orders.filter((item) => orderAuditItemMatchesDataScope(item, auditDataScope));
  const scopedTrades = trades.filter((item) => tradeAuditItemMatchesDataScope(item, auditDataScope));
  const dataScopeCounts = Object.fromEntries(
    orderAuditDataScopes.map((scope) => [
      scope,
      orders.filter((item) => orderAuditItemMatchesDataScope(item, scope)).length
        + trades.filter((item) => tradeAuditItemMatchesDataScope(item, scope)).length,
    ]),
  ) as Record<OrderAuditDataScope, number>;
  const liveOrderCount = scopedOrders.filter((item) => isLiveOrderStatus(item.order.status)).length;
  const spotOrderCount = scopedOrders.filter((item) => item.order.product_type === "SPOT").length;
  const perpOrderCount = scopedOrders.filter((item) => item.order.product_type === "PERP").length;
  const spotTradeCount = scopedTrades.filter((item) => item.trade.product_type === "SPOT").length;
  const perpTradeCount = scopedTrades.filter((item) => item.trade.product_type === "PERP").length;
  const orderStateRows = orderStateBuckets.map((bucket) => ({
    bucket,
    count: scopedOrders.filter((item) => orderStateBucket(item.order.status) === bucket).length,
  }));
  const tradeSourceRows = tradeSourceBuckets.map((bucket) => ({
    bucket,
    count: scopedTrades.filter((item) => tradeSourceBucket(item.trade) === bucket).length,
  }));
  const filteredSymbols = product === "all"
    ? marketSymbols
    : marketSymbols.filter((item) => markets[item]?.product_type === product);
  const selectedUser = userId === "all" ? undefined : users.find((item) => String(item.id) === userId);
  const productLabel = product === "all" ? "全部产品" : product;
  const symbolLabel = symbol === "all" ? "全部市场" : symbol;
  const userLabel = selectedUser ? `${selectedUser.username} #${selectedUser.id}` : "全部主体";
  const statusLabel = status === "live" ? "当前委托" : status === "all" ? "全部状态" : status;
  const exportScope = [
    product,
    auditDataScope,
    symbol,
    selectedUser?.username ?? userId,
    status,
  ].map((item) => String(item || "all").replace(/[^a-zA-Z0-9_-]+/g, "-")).join("_");
  const exportOrders = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_orders_${exportScope}_${stamp}.csv`,
      [
        "updated_at",
        "created_at",
        "account",
        "user_id",
        "product_type",
        "symbol",
        "side",
        "position_action",
        "reduce_only",
        "type",
        "tif",
        "status",
        "order_state_bucket",
        "order_state_label",
        "status_verification_path",
        "status_boundary_note",
        "price",
        "quantity",
        "filled_quantity",
        "remaining_quantity",
        "avg_price",
        "reject_reason",
        "order_id",
        "client_order_id",
      ],
      scopedOrders.map(({ user, order }) => {
        const bucket = orderStateBucket(order.status);
        return [
          bjDateTime(order.updated_at ?? order.created_at),
          bjDateTime(order.created_at),
          user.username,
          user.id,
          order.product_type ?? "",
          order.symbol,
          order.side,
          order.position_action ?? "",
          order.reduce_only ? "true" : "false",
          order.type,
          order.tif,
          order.status,
          bucket,
          orderStateBucketLabel(bucket),
          orderStatusVerificationPath(order),
          orderStatusBoundaryNote(order),
          order.price ?? "",
          order.quantity,
          order.filled_quantity,
          order.remaining_quantity,
          order.avg_price ?? "",
          order.reject_reason ?? "",
          order.order_id,
          order.client_order_id ?? "",
        ];
      }),
    );
  };
  const exportTrades = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_trades_${exportScope}_${stamp}.csv`,
      [
        "executed_at",
        "product_type",
        "symbol",
        "taker",
        "taker_user_id",
        "maker",
        "maker_user_id",
        "side",
        "position_action",
        "price",
        "quantity",
        "quote_amount",
        "fee",
        "fee_asset",
        "liquidity_role",
        "source",
        "trade_source_bucket",
        "trade_source_label",
        "source_verification_path",
        "fee_pnl_verification_path",
        "trade_boundary_note",
        "realized_pnl",
        "trade_id",
      ],
      scopedTrades.map(({ taker_user, maker_user, trade }) => {
        const bucket = tradeSourceBucket(trade);
        return [
          bjDateTime(trade.executed_at ?? trade.ts),
          trade.product_type ?? "",
          trade.symbol,
          taker_user.username,
          taker_user.id,
          maker_user.username,
          maker_user.id,
          trade.side ?? trade.taker_side ?? "",
          trade.position_action ?? "",
          trade.price,
          trade.quantity,
          trade.quote_amount ?? "",
          trade.fee ?? "",
          trade.fee_asset ?? "",
          trade.liquidity_role ?? "",
          trade.source ?? "",
          bucket,
          tradeSourceBucketLabel(bucket),
          tradeSourceVerificationPath(trade),
          tradeFeePnlVerificationPath(trade),
          tradeBoundaryNote(trade),
          trade.realized_pnl ?? "",
          trade.trade_id,
        ];
      }),
    );
  };
  type OrderAuditView = "overview" | "orders" | "trades";
  const [auditView, setAuditView] = useState<OrderAuditView>("overview");
  const auditViewOptions: Array<{
    key: OrderAuditView;
    label: string;
    hint: string;
    badge: string;
    tone?: "neutral" | "warn";
  }> = [
    { key: "overview", label: "审计总览", hint: "范围 / 边界", badge: `${scopedOrders.length + scopedTrades.length}` },
    { key: "orders", label: "订单审计", hint: "状态 / 当前委托", badge: String(scopedOrders.length), tone: liveOrderCount > 0 ? "warn" : "neutral" },
    { key: "trades", label: "成交审计", hint: "来源 / 费用", badge: String(scopedTrades.length) },
  ];
  const applyQuickAuditPath = ({
    nextProduct,
    nextStatus,
    nextDataScope,
    nextView,
  }: {
    nextProduct: AuditProductScope;
    nextStatus: AuditStatusScope;
    nextDataScope: OrderAuditDataScope;
    nextView: OrderAuditView;
  }) => {
    onProductChange(nextProduct);
    onSymbolChange("all");
    onUserChange("all");
    onStatusChange(nextStatus);
    onDataScopeChange(nextDataScope);
    setAuditView(nextView);
  };
  const openRobotPressureMarket = (market: DataRetentionMarketSummary) => {
    onProductChange(auditProductFromProductType(market.product_type));
    onSymbolChange(market.symbol);
    onUserChange("all");
    onStatusChange("all");
    onDataScopeChange("robot");
    setAuditView("overview");
  };
  const openRobotPressureSource = (source: RobotTradeSourceSummary) => {
    onProductChange(auditProductFromProductType(source.product_type));
    onSymbolChange(source.symbol ?? "all");
    onUserChange("all");
    onStatusChange("all");
    onDataScopeChange("robot");
    setAuditView("overview");
  };
  const totalScopedRecords = scopedOrders.length + scopedTrades.length;
  const queryHasMore = Boolean(orderQueryMeta?.has_more || tradeQueryMeta?.has_more);
  const expectedDataScope = selectedUser ? orderAuditDataScopeForUser(selectedUser) : undefined;
  const dataScopeMismatch = Boolean(selectedUser && auditDataScope !== "all" && expectedDataScope && auditDataScope !== expectedDataScope);
  const auditNeedsAction = Boolean(error);
  const auditNeedsAttention = dataScopeMismatch || queryHasMore || (!loading && totalScopedRecords === 0);
  const auditJudgment = auditNeedsAction ? "需处理" : auditNeedsAttention ? "需关注" : "正常";
  const auditJudgmentTone: "neutral" | "warn" | "danger" = auditNeedsAction ? "danger" : auditNeedsAttention ? "warn" : "neutral";
  const auditMainDomain =
    error
      ? "接口状态"
      : dataScopeMismatch && expectedDataScope
        ? `数据域 / ${orderAuditDataScopeLabel(expectedDataScope)}`
        : !loading && totalScopedRecords === 0
          ? "筛选条件"
          : queryHasMore
            ? "后端查询切片"
            : auditDataScope === "customer"
              ? "客户业务记录"
              : auditDataScope === "robot"
                ? "机器人对账"
                : auditDataScope === "system"
                  ? "系统控制 / 未知来源"
                  : status === "live" || liveOrderCount > 0
                    ? "当前委托"
                    : product === "PERP"
                      ? "合约订单成交"
                      : scopedTrades.length >= scopedOrders.length
                        ? "成交费用"
                        : "订单审计";
  const auditNextText =
    error
      ? "先刷新审计；若仍失败，再去系统与审计看接口或操作记录。"
      : dataScopeMismatch && expectedDataScope
        ? `当前 UID 更适合按${orderAuditDataScopeLabel(expectedDataScope)}核对，可切换数据域。`
        : !loading && totalScopedRecords === 0
          ? "当前筛选为空，先放宽产品、市场、UID、状态或数据域。"
          : queryHasMore
            ? "当前只是最近切片，建议收窄市场、UID 或数据域后再导出核对。"
            : liveOrderCount > 0
              ? "先核对当前委托，再按客户、机器人或合约域继续追明细。"
              : "当前可继续按数据域、产品和 UID 做只读审计。";
  const dataScopeCountIsLoaded = (scope: OrderAuditDataScope) => auditDataScope === "all" || auditDataScope === scope;
  const dataScopeCountLabel = (scope: OrderAuditDataScope) => dataScopeCountIsLoaded(scope) ? `${dataScopeCounts[scope]} 条` : "切换查看";
  const dataScopeBadgeLabel = (scope: OrderAuditDataScope) => dataScopeCountIsLoaded(scope) ? String(dataScopeCounts[scope]) : "切换";
  const auditQuickPathRows: Array<{
    title: string;
    cue: string;
    status: string;
    tone: "customer" | "robot" | "system" | "neutral" | "contract" | "warn";
    onClick: () => void;
  }> = [
    {
      title: "看客户业务记录",
      cue: "筛选普通外部客户订单、成交和费用事实，优先用于完整留痕核查。",
      status: dataScopeCountLabel("customer"),
      tone: "customer",
      onClick: () => applyQuickAuditPath({ nextProduct: "all", nextStatus: "all", nextDataScope: "customer", nextView: "overview" }),
    },
    {
      title: "看机器人对账",
      cue: "筛选 MM / FLOW / PERP_MM 特殊 UID，用于库存、费用、策略和盘口对账。",
      status: dataScopeCountLabel("robot"),
      tone: "robot",
      onClick: () => applyQuickAuditPath({ nextProduct: "all", nextStatus: "all", nextDataScope: "robot", nextView: "overview" }),
    },
    {
      title: "看系统控制 / 未知来源",
      cue: "筛选管理主体、系统主体、seed、fallback 或未知来源，避免误读成客户业务或机器人对账。",
      status: dataScopeCountLabel("system"),
      tone: "system",
      onClick: () => applyQuickAuditPath({ nextProduct: "all", nextStatus: "all", nextDataScope: "system", nextView: "overview" }),
    },
    {
      title: "看当前委托",
      cue: "只看当前 live/open 语义订单，并保留当前数据域，用于排查盘口、冻结资金和机器人挂单。",
      status: `${liveOrderCount} 单`,
      tone: liveOrderCount > 0 ? "warn" : "neutral",
      onClick: () => applyQuickAuditPath({ nextProduct: "all", nextStatus: "live", nextDataScope: auditDataScope, nextView: "orders" }),
    },
    {
      title: "看合约订单成交",
      cue: "筛选 PERP 订单与成交，并保留当前数据域；费用、PnL 和保证金深层核对继续回合约清算。",
      status: `${perpOrderCount + perpTradeCount} 条`,
      tone: "contract",
      onClick: () => applyQuickAuditPath({ nextProduct: "PERP", nextStatus: "all", nextDataScope: auditDataScope, nextView: "overview" }),
    },
    {
      title: "看成交费用",
      cue: "直接进入当前数据域的成交审计，核对 taker/maker、手续费、来源和合约 realized PnL。",
      status: `${scopedTrades.length} 笔`,
      tone: "neutral",
      onClick: () => applyQuickAuditPath({ nextProduct: "all", nextStatus: "all", nextDataScope: auditDataScope, nextView: "trades" }),
    },
  ];
  const auditQuickToneClass = (tone: "customer" | "robot" | "system" | "neutral" | "contract" | "warn") => {
    if (tone === "customer") return "border-emerald-400/16 bg-emerald-400/6 hover:bg-emerald-400/10";
    if (tone === "robot") return "border-cyan-400/16 bg-cyan-400/6 hover:bg-cyan-400/10";
    if (tone === "system") return "border-amber-400/16 bg-amber-400/6 hover:bg-amber-400/10";
    if (tone === "contract") return "border-violet-400/16 bg-violet-400/6 hover:bg-violet-400/10";
    if (tone === "warn") return "border-amber-400/20 bg-amber-400/8 hover:bg-amber-400/12";
    return "border-white/8 bg-slate-950/25 hover:bg-white/8";
  };

  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="font-display text-xl">订单与成交</h2>
          <p className="mt-1 text-sm text-slate-400">
            统一审计现货和合约订单、成交；默认只查最近 200 条，订单默认看当前委托，避免把历史长表压到后台首屏。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={onRefresh} className="rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
            {loading ? "刷新中..." : "刷新审计"}
          </button>
          <button
            type="button"
            onClick={exportOrders}
            disabled={loading || scopedOrders.length === 0}
            className="rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
          >
            导出当前订单 CSV
          </button>
          <button
            type="button"
            onClick={exportTrades}
            disabled={loading || scopedTrades.length === 0}
            className="rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
          >
            导出当前成交 CSV
          </button>
        </div>
      </div>
      <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-7">
        <SurveillanceMetric label="订单结果" value={String(scopedOrders.length)} />
        <SurveillanceMetric label="当前委托" value={String(liveOrderCount)} />
        <SurveillanceMetric label="SPOT订单" value={String(spotOrderCount)} />
        <SurveillanceMetric label="PERP订单" value={String(perpOrderCount)} />
        <SurveillanceMetric label="成交结果" value={String(scopedTrades.length)} />
        <SurveillanceMetric label="SPOT成交" value={String(spotTradeCount)} />
        <SurveillanceMetric label="PERP成交" value={String(perpTradeCount)} />
      </div>
      <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 p-4">
        <div className="mb-3">
          <h3 className="font-display text-base text-slate-100">订单审计快速路径</h3>
          <p className="mt-1 text-sm text-slate-500">按常见审计场景套用现有筛选；这里只切换视图和查询条件，不执行撤单、补单、费用重算、清算或历史裁剪。</p>
        </div>
        <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span className={`rounded-full px-2 py-0.5 text-xs ${auditJudgmentTone === "danger" ? "bg-rose-500/16 text-rose-100" : auditJudgmentTone === "warn" ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
                {auditJudgment}
              </span>
              <span className="text-sm text-slate-200">订单 {scopedOrders.length} · 成交 {scopedTrades.length}</span>
            </div>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在查什么</div>
            <div className="mt-1 text-sm text-slate-200">{auditMainDomain}</div>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
            <div className={`mt-1 text-sm ${auditJudgmentTone === "danger" ? "text-rose-100" : auditJudgmentTone === "warn" ? "text-amber-100" : "text-slate-400"}`}>{auditNextText}</div>
          </div>
        </div>
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {auditQuickPathRows.map((row) => (
            <button
              key={row.title}
              type="button"
              onClick={row.onClick}
              className={`min-h-[116px] rounded-xl border p-4 text-left transition ${auditQuickToneClass(row.tone)}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-slate-100">{row.title}</span>
                <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-300">{row.status}</span>
              </div>
              <div className="mt-2 text-xs leading-5 text-slate-500">{row.cue}</div>
            </button>
          ))}
        </div>
      </div>
      <div className="mb-4">
        <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">订单成交分区</div>
        <div className="grid gap-2 md:grid-cols-3">
          {auditViewOptions.map((item) => {
            const active = auditView === item.key;
            const toneClass = item.tone === "warn" ? "bg-amber-400/12 text-amber-100" : "bg-white/8 text-slate-300";
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => setAuditView(item.key)}
                className={`min-h-[68px] rounded-2xl border px-4 py-3 text-left transition ${
                  active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.label}</span>
                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${toneClass}`}>{item.badge}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{item.hint}</div>
              </button>
            );
          })}
        </div>
      </div>
      <div className="mb-4">
        <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">数据域筛选</div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {orderAuditDataScopes.map((scope) => {
            const active = auditDataScope === scope;
            const toneClass =
              scope === "customer"
                ? "bg-emerald-400/12 text-emerald-100"
                : scope === "robot"
                  ? "bg-cyan-400/12 text-cyan-100"
                  : scope === "system"
                    ? "bg-amber-400/12 text-amber-100"
                    : "bg-white/8 text-slate-300";
            return (
              <button
                key={scope}
                type="button"
                onClick={() => onDataScopeChange(scope)}
                className={`min-h-[66px] rounded-2xl border px-4 py-3 text-left transition ${
                  active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{orderAuditDataScopeLabel(scope)}</span>
                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${toneClass}`}>{dataScopeBadgeLabel(scope)}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{orderAuditDataScopeHint(scope)}</div>
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-slate-600">数据域筛选会随刷新审计下沉到后端查询；当前计数和 CSV 仍只代表已加载结果，不代表生产全量归档。当前已限定某个数据域时，其他数据域先显示“切换”，避免把当前切片里的 0 误读成全局没有记录。</p>
      </div>
      <OrderAuditQuerySliceStrip orderQueryMeta={orderQueryMeta} tradeQueryMeta={tradeQueryMeta} />
      <OrderAuditTargetStrip
        user={selectedUser}
        dataScope={auditDataScope}
        symbol={symbol}
        markets={markets}
        productLabel={productLabel}
        symbolLabel={symbolLabel}
        statusLabel={statusLabel}
      />
      <div className="mb-4 grid gap-2 md:grid-cols-2 xl:grid-cols-5">
        <label className="block">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">product</span>
          <select
            value={product}
            onChange={(event) => {
              onProductChange(event.target.value as AuditProductScope);
              onSymbolChange("all");
            }}
            className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none"
          >
            <option value="all">全部产品</option>
            <option value="SPOT">SPOT 现货</option>
            <option value="PERP">PERP 合约</option>
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">market</span>
          <select value={symbol} onChange={(event) => onSymbolChange(event.target.value)} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
            <option value="all">全部市场</option>
            {filteredSymbols.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">account</span>
          <select value={userId} onChange={(event) => onUserChange(event.target.value)} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
            <option value="all">全部主体</option>
            {users.map((user) => (
              <option key={user.id} value={user.id}>{user.username} · {accountUserKindLabel(accountUserKindForUser(user))}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">order status</span>
          <select value={status} onChange={(event) => onStatusChange(event.target.value as AuditStatusScope)} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
            <option value="live">当前委托</option>
            <option value="all">全部状态</option>
            <option value="new">new</option>
            <option value="partially_filled">partially_filled</option>
            <option value="filled">filled</option>
            <option value="canceled">canceled</option>
            <option value="rejected">rejected</option>
          </select>
        </label>
        <div className="rounded-2xl border border-white/8 bg-slate-950/25 px-3 py-2 text-xs leading-5 text-slate-400">
          成交表不使用订单状态筛选；按产品、市场和账户过滤最近成交。
        </div>
      </div>
      {error && <div className="mb-4 rounded-2xl border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">{error}</div>}
      {auditView === "overview" && (
        <>
          <OrderAuditBoundaryStrip
            orderCount={scopedOrders.length}
            tradeCount={scopedTrades.length}
            liveOrderCount={liveOrderCount}
            productLabel={productLabel}
            symbolLabel={symbolLabel}
            userLabel={userLabel}
            statusLabel={statusLabel}
            dataScopeLabel={orderAuditDataScopeLabel(auditDataScope)}
            loading={loading}
          />
          <OrderTradeRetentionPolicyStrip orders={scopedOrders} trades={scopedTrades} summary={dataRetentionDomains} />
          <OrderRobotRetentionPressureStrip summary={dataRetentionDomains} onOpenMarketAudit={openRobotPressureMarket} onOpenSourceAudit={openRobotPressureSource} />
        </>
      )}
      {auditView === "orders" && (
        <>
          <OrderStatusReviewStrip rows={orderStateRows} />
          <ActivitySection title="订单审计" empty={!loading && scopedOrders.length === 0}>
            {loading && scopedOrders.length === 0 ? <div className="py-4 text-sm text-slate-500">加载订单...</div> : <AuditOrderTable items={scopedOrders} />}
          </ActivitySection>
        </>
      )}
      {auditView === "trades" && (
        <>
          <TradeSourceReviewStrip rows={tradeSourceRows} />
          <ActivitySection title="成交审计" empty={!loading && scopedTrades.length === 0}>
            {loading && scopedTrades.length === 0 ? <div className="py-4 text-sm text-slate-500">加载成交...</div> : <AuditTradeTable items={scopedTrades} />}
          </ActivitySection>
        </>
      )}
    </section>
  );
}

function OrderAuditTargetStrip({
  user,
  dataScope,
  symbol,
  markets,
  productLabel,
  symbolLabel,
  statusLabel,
}: {
  user: AdminUser | undefined;
  dataScope: OrderAuditDataScope;
  symbol: string;
  markets: Record<string, AdminMarketItem>;
  productLabel: string;
  symbolLabel: string;
  statusLabel: string;
}) {
  if (!user) return null;
  const kind = accountUserKindForUser(user);
  const expectedScope = orderAuditDataScopeForUser(user);
  const isMismatched = dataScope !== "all" && dataScope !== expectedScope;
  const feePolicy = orderAuditFeePolicyForUser(user, symbol, markets);
  const boundaryNote =
    expectedScope === "customer"
      ? "普通外部客户订单、成交、费用和资金事实优先完整留痕；UID 手续费覆盖在账户与资金维护，订单审计不回算历史成交。"
      : expectedScope === "robot"
        ? "机器人 UID 是特殊运营账户，明细主要服务库存、费用、策略和盘口对账；后续聚合、抽样或短保留不能影响客户业务事实。"
        : "管理主体、系统主体、seed 或 fallback 来源属于系统控制与审计数据，只做后台控制、初始化或流动性排查，不代表客户活跃度。";

  return (
    <section className={`mb-4 rounded-2xl border p-3 ${isMismatched ? "border-amber-300/30 bg-amber-400/8" : "border-white/8 bg-slate-950/25"}`}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-slate-600">当前审计对象</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm text-slate-100">{user.username}</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5 text-xs text-slate-300">UID {user.id}</span>
            <span className={`rounded-full px-2 py-0.5 text-xs ${accountUserKindClass(kind)}`}>{accountUserKindLabel(kind)}</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5 text-xs text-slate-300">role {user.role}</span>
          </div>
        </div>
        <div className="grid gap-2 text-xs text-slate-400 sm:grid-cols-2 lg:min-w-[360px]">
          <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
            <div className="text-slate-500">当前数据域</div>
            <div className="mt-1 text-sm text-slate-100">{orderAuditDataScopeLabel(dataScope)}</div>
          </div>
          <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
            <div className="text-slate-500">该 UID 建议数据域</div>
            <div className="mt-1 text-sm text-slate-100">{orderAuditDataScopeLabel(expectedScope)}</div>
          </div>
        </div>
      </div>
      <div className="mt-3 grid gap-2 text-xs text-slate-400 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
          <div className="text-slate-500">产品 / 市场</div>
          <div className="mt-1 text-slate-200">{productLabel} · {symbolLabel}</div>
        </div>
        <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
          <div className="text-slate-500">订单状态</div>
          <div className="mt-1 text-slate-200">{statusLabel}</div>
        </div>
        <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
          <div className="text-slate-500">管理面</div>
          <div className="mt-1 text-slate-200">{expectedScope === "customer" ? "客户业务数据" : expectedScope === "robot" ? "机器人运行对账" : "系统控制与审计"}</div>
        </div>
        <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
          <div className="text-slate-500">UID 手续费</div>
          <div className="mt-1 text-slate-200">{feePolicy.value}</div>
          <div className="mt-1 leading-5 text-slate-500">{feePolicy.detail}</div>
        </div>
      </div>
      {isMismatched && (
        <div className="mt-3 rounded-xl border border-amber-300/20 bg-amber-400/10 px-3 py-2 text-xs leading-5 text-amber-100">
          当前数据域与该 UID 账户类型不一致，结果可能为空，或仅适合做交叉排查。
        </div>
      )}
      <p className="mt-3 text-xs leading-5 text-slate-500">{boundaryNote}</p>
    </section>
  );
}

function orderAuditFeePolicyForUser(
  user: AdminUser,
  symbol: string,
  markets: Record<string, AdminMarketItem>,
) {
  const profiles = user.fee_profiles ?? [];
  if (symbol !== "all") {
    const profile = profiles.find((item) => item.symbol === symbol);
    if (profile) {
      const allZero = feeRateIsZero(profile.maker_fee_rate) && feeRateIsZero(profile.taker_fee_rate);
      return {
        value: `UID 覆盖 maker ${profile.maker_fee_rate} · taker ${profile.taker_fee_rate}`,
        detail: `${allZero ? "当前覆盖为 0" : "当前覆盖非 0"}；只影响后续成交，历史成交和流水不回算。`,
      };
    }
    const market = markets[symbol];
    if (market) {
      return {
        value: `回退市场默认 maker ${market.default_maker_fee_rate} · taker ${market.default_taker_fee_rate}`,
        detail: "该 UID 在当前市场没有单独覆盖；如需普通客户单独费率，回账户与资金维护。",
      };
    }
    return {
      value: "市场未识别",
      detail: "当前筛选市场不在已加载市场配置中；先刷新市场或回市场运营核对。",
    };
  }
  const marketCount = Object.keys(markets).length;
  if (profiles.length === 0) {
    return {
      value: "无 UID 覆盖",
      detail: "全部市场视图下会回退各市场默认费率；选择具体市场可核对 maker/taker。",
    };
  }
  return {
    value: `覆盖 ${profiles.length}/${marketCount} 个市场`,
    detail: "UID 覆盖优先于市场默认，只影响后续成交；保存入口在账户与资金，订单审计只读核对。",
  };
}

function OrderAuditQuerySliceStrip({
  orderQueryMeta,
  tradeQueryMeta,
}: {
  orderQueryMeta: AdminAuditQueryMeta | null;
  tradeQueryMeta: AdminAuditQueryMeta | null;
}) {
  const rows = [
    { label: "订单查询", meta: orderQueryMeta, note: "订单状态筛选只作用于订单表。" },
    { label: "成交查询", meta: tradeQueryMeta, note: "成交表不使用订单状态筛选。" },
  ];
  return (
    <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
      <div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-xs uppercase tracking-[0.18em] text-slate-600">后端查询切片</div>
        <div className="text-xs text-slate-500">recent_slice / loaded_result_only</div>
      </div>
      <div className="grid gap-2 lg:grid-cols-2">
        {rows.map(({ label, meta, note }) => {
          if (!meta) {
            return (
              <div key={label} className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
                <div className="text-sm text-slate-200">{label}</div>
                <div className="mt-1 text-xs text-slate-500">尚未加载或本次查询失败。</div>
              </div>
            );
          }
          const filterText = [
            meta.product_type === "all" ? "全部产品" : meta.product_type,
            meta.symbol === "all" ? "全部市场" : meta.symbol,
            meta.user_id === null ? "全部主体" : `UID ${meta.user_id}`,
            orderAuditDataScopeLabel(meta.data_domain),
            meta.status ? `状态 ${meta.status}` : null,
          ].filter(Boolean).join(" · ");
          return (
            <div key={label} className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-slate-200">{label}</span>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${meta.has_more ? "bg-amber-400/12 text-amber-100" : "bg-emerald-400/10 text-emerald-100"}`}>
                  {meta.result_count}/{meta.limit}{meta.has_more ? " · 可能更多" : ""}
                </span>
              </div>
              <div className="mt-1 break-words text-xs text-slate-500">{filterText}</div>
              <div className="mt-1 text-xs text-slate-600">{note}</div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-slate-600">这里不做昂贵全量 count；`可能更多` 只表示本次结果达到 limit，需要更窄筛选或后续分页专题。</p>
    </div>
  );
}

function OrderAuditBoundaryStrip({
  orderCount,
  tradeCount,
  liveOrderCount,
  productLabel,
  symbolLabel,
  userLabel,
  statusLabel,
  dataScopeLabel,
  loading,
}: {
  orderCount: number;
  tradeCount: number;
  liveOrderCount: number;
  productLabel: string;
  symbolLabel: string;
  userLabel: string;
  statusLabel: string;
  dataScopeLabel: string;
  loading: boolean;
}) {
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    boundary: string;
  }> = [
    {
      domain: "审计范围",
      owner: "订单与成交",
      status: `${productLabel} · 订单 ${orderCount} · 成交 ${tradeCount}${loading ? " · 刷新中" : ""}`,
      boundary: "统一回看 SPOT/PERP 订单与成交；这里只做审计、筛选和导出，不执行撤单、清算、调账或实例控制。",
    },
    {
      domain: "数据域筛选",
      owner: "订单与成交",
      status: dataScopeLabel,
      boundary: "只筛当前已加载结果，帮助区分客户业务、机器人对账和系统控制/未知来源；不扩大查询、不清历史、不改保留策略。",
    },
    {
      domain: "产品与市场筛选",
      owner: "订单与成交 / 市场运营",
      status: `${productLabel} · ${symbolLabel}`,
      boundary: "本页只选择审计视图；市场身份、交易规则、费率、合约交易模式和产品参数仍回市场运营维护。",
    },
    {
      domain: "账户筛选",
      owner: "账户与资金",
      status: userLabel,
      boundary: "按登录主体回看订单和成交；用户身份、API Key、现货余额、合约保证金和机器人绑定仍分别回账户与资金或机器人入口。",
    },
    {
      domain: "当前委托 / 历史订单",
      owner: "订单审计",
      status: `${statusLabel} · 当前委托 ${liveOrderCount}`,
      boundary: "默认看 live/open 语义，必要时切换历史状态；撤单、重建订单或清理历史不放在审计表里执行。",
    },
    {
      domain: "成交回看",
      owner: "成交审计",
      status: `成交 ${tradeCount}`,
      boundary: "统一回看成交、手续费、流动性角色和来源；合约 realized PnL、资金费、强平坏账的深层核对回合约清算。",
    },
    {
      domain: "导出交接",
      owner: "运营交接",
      status: "CSV 当前已加载结果",
      boundary: "导出只覆盖当前前端已加载订单或成交结果，不扩大查询范围，不触发后端写入，也不替代不可变审计账本。",
    },
  ];

  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">订单与成交边界</h3>
        <p className="mt-1 text-sm text-slate-500">订单审计是跨产品回看入口，不是交易执行台；高风险动作仍留在各自业务域并保留确认边界。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1040px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">审计域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OrderTradeRetentionPolicyStrip({
  orders,
  trades,
  summary,
}: {
  orders: AdminOrderAuditItem[];
  trades: AdminTradeAuditItem[];
  summary?: DataRetentionDomainSummary;
}) {
  const customerOrderCount = orders.filter(({ user }) => accountUserKindForUser(user) === "customer").length;
  const robotOrderCount = orders.filter(({ user }) => {
    const kind = accountUserKindForUser(user);
    return kind === "spot_robot" || kind === "contract_robot";
  }).length;
  const nonCustomerOrderCount = Math.max(orders.length - customerOrderCount, 0);
  const customerTradeCount = trades.filter(({ trade }) => tradeSourceBucket(trade) === "customer").length;
  const robotTradeCount = trades.filter(({ trade }) => {
    const bucket = tradeSourceBucket(trade);
    return bucket === "robot" || bucket === "flow";
  }).length;
  const systemTradeCount = Math.max(trades.length - customerTradeCount - robotTradeCount, 0);
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    globalSignal: string;
    retention: string;
    boundary: string;
  }> = [
    {
      domain: "客户业务记录",
      owner: "订单与成交 / 账户与资金 / 合约清算",
      status: `${customerOrderCount} 订单 · ${customerTradeCount} 成交`,
      globalSignal: `客户旧单 ${retentionValue(summary?.orders.customer_retention_eligible)} · 客户成交 ${retentionValue(summary?.trades.customer_involved)}`,
      retention: "普通外部客户订单、成交、费用、流水、仓位、资金费和强平应作为业务事实完整核对；当前页面只加载最近结果，不等于生产全量归档。",
      boundary: "机器人留存优化不能覆盖客户业务事实；费用和资金变化仍以现货流水或合约保证金流水为准。",
    },
    {
      domain: "机器人对账记录",
      owner: "机器人运营 / 机器人账号",
      status: `${robotOrderCount} 订单 · ${robotTradeCount} 成交`,
      globalSignal: `机器人旧单 ${retentionValue(summary?.orders.robot_retention_eligible)} · robot-only 成交 ${retentionValue(summary?.trades.robot_only)}`,
      retention: "MM / PERP_MM / FLOW 明细优先服务库存、费用、策略和盘口对账；后续可按来源做聚合、抽样、短保留或汇总表。",
      boundary: "当前真实机器人/FLOW 成交仍按既有撮合、账本和 K 线落库；本页只标明后续留存方向，不改成交 source-of-truth。",
    },
    {
      domain: "系统控制 / 未知记录",
      owner: "市场运营 / 系统与审计",
      status: `${nonCustomerOrderCount} 非客户订单 · ${systemTradeCount} 系统控制/未知成交`,
      globalSignal: `系统旧单 ${retentionValue(summary?.orders.system_retention_eligible)} · 系统控制/未知成交 ${retentionValue(summary?.trades.system_or_unknown)}`,
      retention: "初始化、seed、fallback、混合或未知来源需要保留可排查线索，但不应被误读为普通客户活跃度。",
      boundary: "来源修正、铺盘、重建和清历史不在订单审计页执行；高风险动作仍回系统与审计或市场运营并保留确认。",
    },
    {
      domain: "历史保留与归档",
      owner: "系统与审计 -> 运行资源",
      status: "SQLite 沙盒体量控制",
      globalSignal: `真实可裁 订单 ${retentionValue(summary?.orders.prune_candidate)} · 成交 ${retentionValue(summary?.trades.prune_candidate)}`,
      retention: "当前历史保留服务是本地沙盒数据库体量控制；标准交易所后台应把客户业务、机器人运行、系统控制和市场公共数据分域归档。",
      boundary: "本页不执行历史裁剪、不创建归档包、不新增不可变审计账本；真实裁剪仍回系统与审计并要求 confirm_execute。",
    },
  ];

  return (
    <section className="mb-4 rounded-2xl border border-cyan-400/12 bg-cyan-400/6 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-cyan-50">订单成交留存边界</h3>
        <p className="mt-1 text-sm text-slate-400">把客户业务事实、机器人运行对账和系统控制/未知来源分开看；当前结果来自本页筛选，全库留存信号来自系统状态，只读展示，不触发清理、归档或重算费用。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1320px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">数据域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前结果</th>
              <th className="px-3 py-2 font-normal">全库留存信号</th>
              <th className="px-3 py-2 font-normal">保留原则</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 font-mono text-cyan-100">{row.globalSignal}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.retention}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OrderRobotRetentionPressureStrip({
  summary,
  onOpenMarketAudit,
  onOpenSourceAudit,
}: {
  summary?: DataRetentionDomainSummary;
  onOpenMarketAudit: (market: DataRetentionMarketSummary) => void;
  onOpenSourceAudit: (source: RobotTradeSourceSummary) => void;
}) {
  const summaryLoaded = Boolean(summary);
  const topMarkets = (summary?.markets ?? []).filter((market) => market.pressure_score > 0).slice(0, 5);
  const topSources = (
    summary?.robot_trade_sources?.length
      ? summary.robot_trade_sources
      : (summary?.markets ?? []).flatMap((market) => market.robot_trade_sources ?? [])
  ).slice(0, 6);
  const robotOrderCandidate = summary?.orders.robot_retention_eligible;
  const robotTradeCandidate = summary?.trades.robot_only;
  const robotPruneCandidate = typeof robotOrderCandidate === "number" && typeof robotTradeCandidate === "number"
    ? robotOrderCandidate + robotTradeCandidate
    : undefined;
  const leadingMarket = topMarkets[0];
  const sourceSummary = topSources.length > 0
    ? topSources.map((source) => `${source.source || "unknown"} ${retentionValue(source.trade_count)}`).join(" · ")
    : "-";

  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">机器人留存压力线索</h3>
        <p className="mt-1 text-sm text-slate-500">从系统状态聚合里提取市场和 source 线索，用于判断机器人高频明细后续是否需要汇总、抽样或短保留；本页只筛选审计，不裁剪、不归档、不改落库。</p>
      </div>
      <div className="mb-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <SurveillanceMetric label="机器人旧单" value={retentionValue(robotOrderCandidate)} />
        <SurveillanceMetric label="robot-only 成交" value={retentionValue(robotTradeCandidate)} />
        <SurveillanceMetric label="机器人候选合计" value={retentionValue(robotPruneCandidate)} />
        <SurveillanceMetric label="最高压力市场" value={leadingMarket ? `${leadingMarket.symbol} ${retentionValue(leadingMarket.pressure_score)}` : "-"} />
      </div>
      <div className="mb-3 rounded-xl border border-white/8 bg-white/[0.025] px-3 py-2 text-xs leading-5 text-slate-400">
        全局主要 source：<span className="font-mono text-cyan-100">{sourceSummary}</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1280px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">市场</th>
              <th className="px-3 py-2 font-normal">产品</th>
              <th className="px-3 py-2 font-normal">压力分</th>
              <th className="px-3 py-2 font-normal">机器人候选</th>
              <th className="px-3 py-2 font-normal">真实可裁</th>
              <th className="px-3 py-2 font-normal">主要 source</th>
              <th className="px-3 py-2 font-normal">客户保护</th>
              <th className="px-3 py-2 font-normal">边界</th>
              <th className="px-3 py-2 text-right font-normal">审计</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {topMarkets.map((market) => (
              <tr key={market.symbol} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{market.symbol}</td>
                <td className="px-3 py-2 font-mono text-slate-300">{market.product_type ?? "-"}</td>
                <td className="px-3 py-2 font-mono text-cyan-100">{retentionValue(market.pressure_score)}</td>
                <td className="px-3 py-2 font-mono text-slate-200">旧单 {retentionValue(market.orders.robot_retention_eligible)} · 成交 {retentionValue(market.trades.robot_only)}</td>
                <td className="px-3 py-2 font-mono text-amber-100">订单 {retentionValue(market.orders.prune_candidate)} · 成交 {retentionValue(market.trades.prune_candidate)}</td>
                <td className="px-3 py-2 font-mono text-slate-300">{robotTradeSourceSummary(market.robot_trade_sources?.[0])}</td>
                <td className="px-3 py-2 font-mono text-emerald-100">订单 {retentionValue(market.orders.customer_retention_eligible)} · 成交 {retentionValue(market.trades.customer_involved)}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">只定位候选压力；汇总表、短保留和裁剪仍是后续专题。</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => onOpenMarketAudit(market)}
                    className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                  >
                    看机器人审计
                  </button>
                </td>
              </tr>
            ))}
            {topMarkets.length === 0 && (
              <tr>
                <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={9}>
                  {summaryLoaded ? "暂无机器人留存压力市场。" : "系统状态聚合加载中，压力市场稍后显示。"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="mt-3">
        <div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
          <h4 className="text-sm font-medium text-slate-200">按 source 分布</h4>
          <p className="text-xs text-slate-500">用于判断是 FLOW、MM、seed 还是未知来源制造高频成交；这里只有市场级审计入口，不新增 source 条件查询。</p>
        </div>
        <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
          <table className="min-w-[1120px] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="px-3 py-2 font-normal">source</th>
                <th className="px-3 py-2 font-normal">市场</th>
                <th className="px-3 py-2 font-normal">成交笔数</th>
                <th className="px-3 py-2 font-normal">quote 成交额</th>
                <th className="px-3 py-2 font-normal">手续费</th>
                <th className="px-3 py-2 font-normal">边界</th>
                <th className="px-3 py-2 text-right font-normal">审计</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {topSources.map((source) => (
                <tr key={`${source.symbol ?? "all"}-${source.source}`} className="border-t border-white/8 align-top">
                  <td className="px-3 py-2 font-mono text-cyan-100">{source.source || "unknown"}</td>
                  <td className="px-3 py-2">
                    <div className="font-mono text-slate-100">{source.symbol ?? "全部市场"}</div>
                    <div className="text-[11px] text-slate-500">{source.product_type ?? "-"}</div>
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-200">{retentionValue(source.trade_count)}</td>
                  <td className="px-3 py-2 font-mono text-slate-200">{fmt(source.quote_amount ?? "0", 2)}</td>
                  <td className="px-3 py-2 font-mono text-slate-300">maker {fmt(source.maker_fee ?? "0", 6)} · taker {fmt(source.taker_fee ?? "0", 6)}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">按 source 看压力来源；成交明细仍回订单成交和账本核对，不在这里汇总入账。</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => onOpenSourceAudit(source)}
                      className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                    >
                      看 source 审计
                    </button>
                  </td>
                </tr>
              ))}
              {topSources.length === 0 && (
                <tr>
                  <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={7}>
                    {summaryLoaded ? "暂无 robot-only source 分布。" : "系统状态聚合加载中，source 分布稍后显示。"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

type RiskSeverity = "critical" | "warn" | "info";
type RiskMonitoringView = "overview" | "queue" | "boundary";
type RiskQueueItem = {
  key: string;
  severity: RiskSeverity;
  domain: string;
  title: string;
  detail: string;
  action: string;
  targetSection: AdminSection;
  targetSymbol?: string;
  targetMarketDetailTab?: MarketDetailTab;
  targetContractTab?: ContractAdminTab;
  targetSystemTab?: SystemAdminTab;
  targetOperationDomain?: AdminOperationAuditFilters["domain"];
  targetOperationStatus?: AdminOperationAuditFilters["status"];
  targetOperationType?: string;
  targetOperationSymbol?: string;
};
type RiskQueueSources = {
  deploymentChecklist?: DeploymentChecklist;
  systemStatus?: SystemStatus;
  marketSymbols: string[];
  marketSurveillance: Record<string, MarketSurveillanceItem>;
  makerInstances: Record<string, MakerInstanceStatus>;
  positions: ContractPositionAdminItem[];
  marketStates: ContractPriceState[];
  fundingJobs: ContractFundingJob[];
  liquidationEvents: ContractLiquidationEventAdminItem[];
  adlEvents: ContractAdlEventAdminItem[];
  insuranceEvents: ContractInsuranceEventAdminItem[];
  maintenance: ContractMaintenanceStatus;
  adminOperations: AdminOperationAuditItem[];
};
type RiskQueueSnapshot = {
  sortedQueue: RiskQueueItem[];
  criticalCount: number;
  warnCount: number;
  activePositions: ContractPositionAdminItem[];
  stressedPositions: ContractPositionAdminItem[];
  closestLiquidationDistance: number | null;
  riskAlerts: ContractRiskAlert[];
  failedFundingJobs: ContractFundingJob[];
  activeFundingJobs: ContractFundingJob[];
  sourceIssues: ContractPriceState[];
  invariantDiffCount: number;
  versionAlertCount: number;
  wsIssueCount: number;
  marketIssues: MarketSurveillanceItem[];
  instanceIssues: { symbol: string; instance?: MakerInstanceStatus }[];
  residualBadDebt: number;
  adlResidual: number;
  activeAdlResidual: number;
};

const riskSeverityRank: Record<RiskSeverity, number> = { critical: 0, warn: 1, info: 2 };

const failedOperationCriticalTypes = new Set([
  "adjust_contract_account",
  "adjust_insurance_fund",
  "execute_adl",
  "history_retention_run",
  "orderbook_rebuild",
  "reset_contract_maker_state",
  "reset_spot_market",
  "reset_test_users",
  "reset_user_balances",
  "retry_failed_funding_jobs",
  "rotate_api_key",
  "settle_funding",
  "update_contract_trading_mode",
  "update_risk_tiers",
  "wipe_market_klines",
  "wipe_spot_market_data",
]);

function riskSeverityClass(severity: RiskSeverity) {
  if (severity === "critical") return "bg-rose-500/16 text-rose-100";
  if (severity === "warn") return "bg-amber-400/16 text-amber-100";
  return "bg-cyan-400/14 text-cyan-100";
}

function riskSeverityLabel(severity: RiskSeverity) {
  if (severity === "critical") return "需处理";
  if (severity === "warn") return "需关注";
  return "观察";
}

type RiskSeverityReview = {
  level: string;
  scope: string;
  verificationPath: string;
  boundary: string;
};

function riskSeverityReview(severity: RiskSeverity): RiskSeverityReview {
  if (severity === "critical") {
    return {
      level: "当班处理",
      scope: "清算残余风险、合约保证金/仓位高风险、资金费失败、系统一致性差异、关键危险操作失败。",
      verificationPath: "先进入队列指向的业务页核对事实，再按业务页的确认边界执行必要动作。",
      boundary: "风险监控页不直接执行清算、重试、调账、停机、重建或清历史。",
    };
  }
  if (severity === "warn") {
    return {
      level: "当班复核",
      scope: "价格源退化、市场/机器人异常、版本异常、非关键操作失败和需要人工确认的运行告警。",
      verificationPath: "按业务域核对状态、日志、订单/账本和最近操作记录，确认是否升级为需处理。",
      boundary: "复核结果仍回原业务页处理，不在风险页做快捷修复。",
    };
  }
  return {
    level: "观察留痕",
    scope: "趋势观察、低影响提示或暂不需要执行动作的背景信号。",
    verificationPath: "保留队列和导出记录，必要时回业务页做抽查。",
    boundary: "观察项不是执行指令，也不代表已完成对账或审计。",
  };
}

function riskQueueVerificationPath(item: RiskQueueItem) {
  if (["部署", "系统", "一致性", "WS"].includes(item.domain)) {
    return "系统与审计核对部署检查、orderbook invariants、WebSocket 指标、运行资源和最近失败操作。";
  }
  if (["市场", "价格源"].includes(item.domain)) {
    return "市场运营核对市场状态、产品参数、交易模式、外部价格源和盘口风险。";
  }
  if (item.domain === "机器人") {
    return "机器人运营核对实例状态、heartbeat、PID identity、guard、策略版本和最近日志。";
  }
  if (item.domain === "仓位") {
    return "合约清算的仓位风险核对逐仓保证金、强平距离、风险阶梯、维持保证金和相关流水。";
  }
  if (item.domain === "资金费") {
    return "合约清算的资金费页核对任务状态、结算水位、资金费记录、失败原因和合约流水。";
  }
  if (item.domain === "保险基金") {
    return "合约清算的保险基金页核对基金余额、基金流水、坏账覆盖和关联强平事件。";
  }
  if (item.domain === "ADL") {
    return "合约清算的强平 / ADL 页核对强平事件、ADL 历史、残余坏账和仓位变化。";
  }
  if (item.domain === "操作失败") {
    return "系统与审计的操作记录页核对失败操作、执行人、目标、错误上下文和业务页最终状态。";
  }
  return `${riskQueueTargetText(item)} 核对目标业务页的当前状态和相关流水/日志。`;
}

function riskQueueBoundaryNote(item: RiskQueueItem) {
  if (item.domain === "操作失败") return "失败记录只证明页面观察到接口失败，不自动重试、不补偿，也不证明底层业务已回滚。";
  if (["资金费", "保险基金", "ADL", "仓位"].includes(item.domain)) return "清算、资金费重试、ADL、保险基金调账和风险参数维护只在合约清算业务页按确认边界执行。";
  if (item.domain === "机器人") return "实例启停、停止并撤单和重启只在机器人运营页执行，并保留既有确认和操作记录。";
  if (["部署", "系统", "一致性", "WS"].includes(item.domain)) return "系统类修复、重建盘口、清历史和凭据变更回系统与审计或对应业务页，不在风险页直接执行。";
  return "风险监控只做分流、排序和导出；具体处理回目标业务页，不在本页执行写入动作。";
}

function isFailedAdminOperation(item: AdminOperationAuditItem) {
  return item.status === "failed" || item.status === "error";
}

function failedAdminOperationSeverity(item: AdminOperationAuditItem): RiskSeverity {
  if (failedOperationCriticalTypes.has(item.operation_type)) return "critical";
  if (item.domain === "contract" && ["settle_funding", "execute_adl", "adjust_insurance_fund", "adjust_contract_account"].includes(item.operation_type)) return "critical";
  return "warn";
}

function riskQueueTargetText(item: RiskQueueItem) {
  return [
    item.targetSection,
    item.targetSymbol ?? "",
    item.targetMarketDetailTab ? `marketTab=${item.targetMarketDetailTab}` : "",
    item.targetContractTab ? `contractTab=${item.targetContractTab}` : "",
    item.targetSystemTab ? `systemTab=${item.targetSystemTab}` : "",
    item.targetOperationDomain && item.targetOperationDomain !== "all" ? `opDomain=${item.targetOperationDomain}` : "",
    item.targetOperationStatus && item.targetOperationStatus !== "all" ? `opStatus=${item.targetOperationStatus}` : "",
    item.targetOperationType ? `opType=${item.targetOperationType}` : "",
    item.targetOperationSymbol ? `opSymbol=${item.targetOperationSymbol}` : "",
  ].filter(Boolean).join(" / ");
}

function adminOperationTargetText(item: AdminOperationAuditItem) {
  return item.target_symbol ?? item.target_id ?? item.target_type ?? "-";
}

function adminOperationActorText(item: AdminOperationAuditItem) {
  return item.actor_username ?? (item.actor_user_id != null ? `#${item.actor_user_id}` : "system");
}

function adminOperationEvidence(item: AdminOperationAuditItem): AdminOperationEvidence {
  const operationType = adminOperationTypeInput(item.operation_type);
  if (isFailedAdminOperation(item)) {
    return {
      level: "失败事实",
      verificationPath: `${operationTargetLabel(item)}，再核对原业务状态和错误上下文。`,
      gap: "不证明底层业务已回滚，也不自动重试或补偿。",
      tone: "needs_check",
    };
  }
  if (item.status !== "success") {
    return {
      level: "状态待核",
      verificationPath: `${operationTargetLabel(item)}，确认业务侧最终状态。`,
      gap: "不是最终成功凭证，也不是不可变审计。",
      tone: "limited",
    };
  }
  if (item.domain === "system" && operationType === "orderbook_rebuild") {
    return {
      level: "盘口重建维护凭证",
      verificationPath: "看一致性，核对目标市场 Engine / DB、差异样本、盘口版本异常和操作记录结果。",
      gap: "只证明本机内存订单簿从数据库 live orders 重建过，不替代撮合一致性报告、发布单或恢复演练记录。",
      tone: "proof",
    };
  }
  if (item.domain === "system" && operationType === "history_retention_run") {
    return {
      level: "历史保留维护凭证",
      verificationPath: "看运行资源，核对最近历史保留结果、数据规模、客户保护口径和操作记录结果。",
      gap: "不替代 WORM 归档、长期留存策略、裁剪前快照或恢复演练。",
      tone: "proof",
    };
  }
  if (item.domain === "contract") {
    if (operationType === "adjust_contract_account") {
      return {
        level: "保证金维护凭证",
        verificationPath: "看保证金账户，核对合约流水、保证金钱包、可用保证金和仓位风险。",
        gap: "不是全量账本对账包，不能替代日终清算核对。",
        tone: "proof",
      };
    }
    if (["settle_funding", "retry_funding_job", "retry_failed_funding_jobs"].includes(operationType)) {
      return {
        level: "资金费维护凭证",
        verificationPath: "看资金费，核对任务状态、结算水位、资金费事件和合约流水。",
        gap: "不替代资金费批次审计、重复结算检查或日终报告。",
        tone: "proof",
      };
    }
    if (operationType === "execute_adl") {
      return {
        level: "ADL 维护凭证",
        verificationPath: "看强平 / ADL，核对 ADL 历史、坏账余额、仓位变化和基金流水。",
        gap: "不替代完整清算报告或人工复核审批单。",
        tone: "proof",
      };
    }
    if (operationType === "adjust_insurance_fund") {
      return {
        level: "保险基金维护凭证",
        verificationPath: "看保险基金，核对基金余额、基金流水和关联强平事件。",
        gap: "不替代基金日终报表或外部财务审计。",
        tone: "proof",
      };
    }
    if (["save_risk_tiers", "update_risk_tiers"].includes(operationType)) {
      return {
        level: "风控参数维护凭证",
        verificationPath: "看风险参数，核对阶梯、杠杆上限、仓位风险和下单拒单。",
        gap: "不替代参数审批、灰度发布或回滚记录。",
        tone: "proof",
      };
    }
    return {
      level: "合约维护凭证",
      verificationPath: `${operationTargetLabel(item)}，核对合约订单、成交、流水或风险状态。`,
      gap: "不是合约账本 source-of-truth。",
      tone: "proof",
    };
  }
  if (item.domain === "account") {
    return {
      level: "账户维护凭证",
      verificationPath: "看账户，核对现货余额、API 状态、合约保证金索引和最近流水。",
      gap: "不表示现货钱包与合约保证金已合并，也不是完整对账包。",
      tone: "proof",
    };
  }
  if (item.domain === "market") {
    return {
      level: "市场维护凭证",
      verificationPath: "看市场日志或产品配置，核对盘口、订单审计、K 线和合约交易模式。",
      gap: "不替代撮合一致性报告或市场级发布审批。",
      tone: "proof",
    };
  }
  if (item.domain === "bot") {
    return {
      level: "实例操作凭证",
      verificationPath: "看实例，核对 heartbeat、PID identity、运行策略、日志和当前挂单。",
      gap: "不替代机器人运行审计流或策略变更审批。",
      tone: "proof",
    };
  }
  return {
    level: "系统维护凭证",
    verificationPath: "看系统，核对部署检查、一致性、运行资源和最近失败操作。",
    gap: "不替代不可变审计、保留策略或日终归档。",
    tone: "proof",
  };
}

function adminOperationArchiveProfile(item: AdminOperationAuditItem): AdminOperationArchiveProfile {
  const evidence = adminOperationEvidence(item);
  const approval = adminOperationApprovalProfile({ domain: item.domain, operationType: item.operation_type });
  const operationType = adminOperationTypeInput(item.operation_type);
  if (isFailedAdminOperation(item)) {
    return {
      source: "前端观察失败",
      status: "失败事实",
      proofBoundary: "证明后台页面观察到该接口失败，并保留目标、执行人、错误摘要和恢复定位。",
      gap: "不能证明底层业务没有部分写入、已经回滚或已经补偿。",
      followUp: evidence.verificationPath,
    };
  }
  if (operationType === "history_retention_run") {
    return {
      source: "历史保留维护",
      status: "本机保留口径",
      proofBoundary: "证明本机沙盒历史保留动作的执行摘要和裁剪范围。",
      gap: "不能替代 WORM 归档、长期留存策略、裁剪前快照或恢复演练。",
      followUp: "看运行资源页的历史保留结果，并抽样核对订单、成交、K 线和流水规模。",
    };
  }
  if (operationType === "orderbook_rebuild") {
    return {
      source: "盘口一致性维护",
      status: "本机恢复口径",
      proofBoundary: "证明目标市场内存订单簿按数据库 live GTC 限价单执行过一次重建，并保留重建前后差异摘要。",
      gap: "不能替代发布单、演练单、撮合一致性报告、恢复窗口审批或外部归档。",
      followUp: "看一致性页的 Engine / DB 差异、盘口版本异常和该条操作结果 JSON。",
    };
  }
  if (approval.level === "approval_required") {
    return {
      source: "资金 / 清算维护记录",
      status: approval.label,
      proofBoundary: "证明该维护动作有操作记录和业务核对路径。",
      gap: `${approval.gap}；同时缺签名链、不可变归档和日终对账包。`,
      followUp: evidence.verificationPath,
    };
  }
  if (approval.level === "change_window_required") {
    return {
      source: "市场 / 历史维护记录",
      status: approval.label,
      proofBoundary: "证明该市场或历史维护动作已被后台记录，可回目标业务页核对影响范围。",
      gap: `${approval.gap}；不是发布单、演练单或可追溯归档。`,
      followUp: evidence.verificationPath,
    };
  }
  if (approval.level === "runtime_confirmed") {
    return {
      source: "机器人 / FLOW 运行记录",
      status: approval.label,
      proofBoundary: "证明该运行维护动作已被后台记录，可回实例、队列、盘口和成交来源核对。",
      gap: `${approval.gap}；不是策略审批、运行审计流或自动回滚记录。`,
      followUp: evidence.verificationPath,
    };
  }
  return {
    source: "管理员操作记录",
    status: approval.label,
    proofBoundary: "证明已接入维护动作的执行时间、执行人、目标、状态和精简结果。",
    gap: `${evidence.gap}；生产归档仍缺签名链、审批单关联和不可变存储。`,
    followUp: evidence.verificationPath,
  };
}

function buildRiskQueueSnapshot({
  deploymentChecklist,
  systemStatus,
  marketSymbols,
  marketSurveillance,
  makerInstances,
  positions,
  marketStates,
  fundingJobs,
  liquidationEvents,
  adlEvents,
  insuranceEvents,
  maintenance,
  adminOperations,
}: RiskQueueSources): RiskQueueSnapshot {
  const activePositions = positions.filter((item) => Number(item.position.quantity) > 0 && item.position.side !== "flat");
  const stressedPositions = activePositions.filter((item) => !["flat", "ok"].includes(item.position.risk_status ?? "ok"));
  const closestLiquidationDistance = activePositions.reduce<number | null>((best, item) => {
    const value = Number(item.position.liquidation_distance_pct);
    if (!Number.isFinite(value) || value <= 0) return best;
    return best === null || value < best ? value : best;
  }, null);
  const riskAlerts = Object.values(maintenance.risk_alerts ?? {}).flat();
  const failedFundingJobs = fundingJobs.filter((item) => item.status === "failed");
  const activeFundingJobs = fundingJobs.filter((item) => !["completed", "succeeded", "settled"].includes(item.status));
  const sourceIssues = marketStates.filter((state) => !["ok", "external_ok"].includes(state.source_status));
  const maintenanceErrors = maintenance.metrics?.errors ?? [];
  const liquidityErrors = maintenance.liquidity?.errors ?? [];
  const invariantItems = systemStatus?.orderbook_invariants ?? [];
  const invariantDiffCount = invariantItems.reduce((sum, item) => sum + (item.engine_only_count ?? 0) + (item.db_only_count ?? 0), 0);
  const versionAlertCount = invariantItems.reduce(
    (sum, item) => sum + (item.orderbook?.same_seq_diff_count ?? 0) + (item.orderbook?.seq_rollback_count ?? 0),
    0,
  );
  const wsMetrics = systemStatus?.websocket.metrics;
  const wsIssueCount = (wsMetrics?.send_timeouts ?? 0) + (wsMetrics?.send_failures ?? 0) + (wsMetrics?.dropped_sockets ?? 0);
  const marketIssues = marketSymbols
    .map((symbol) => marketSurveillance[symbol])
    .filter((item): item is MarketSurveillanceItem => Boolean(item) && item.status !== "ok");
  const instanceIssues = marketSymbols
    .map((symbol) => ({ symbol, instance: makerInstances[symbol] }))
    .filter(({ instance }) => {
      if (!instance) return false;
      return instance.status === "error" || instance.status === "stale" || instance.runtime_strategy_mismatch || instance.heartbeat_status === "stale";
    });
  const residualBadDebt = insuranceEvents.reduce((sum, item) => sum + Number(item.residual_bad_debt || 0), 0);
  const adlResidual = liquidationEvents.reduce((sum, item) => sum + Number(item.event.adl_residual || 0), 0);
  const activeAdlResidual = adlEvents.reduce((sum, item) => sum + Number(item.residual_after || 0), 0);
  const checklistIssues = (deploymentChecklist?.checks ?? []).filter((item) => item.severity !== "ok");
  const failedAdminOperations = adminOperations.filter(isFailedAdminOperation);

  const queue: RiskQueueItem[] = [];
  checklistIssues.forEach((item) => {
    queue.push({
      key: `check-${item.code}`,
      severity: item.severity === "critical" ? "critical" : "warn",
      domain: "部署",
      title: item.label,
      detail: item.detail,
      action: item.action || "查看系统与审计",
      targetSection: "system",
    });
  });
  (systemStatus?.warnings ?? []).forEach((warning, index) => {
    queue.push({
      key: `system-warning-${index}`,
      severity: systemStatus?.status === "ok" ? "warn" : "critical",
      domain: "系统",
      title: "系统告警",
      detail: warning,
      action: "查看系统状态",
      targetSection: "system",
    });
  });
  invariantItems.forEach((item) => {
    const diffCount = (item.engine_only_count ?? 0) + (item.db_only_count ?? 0);
    const versionCount = (item.orderbook?.same_seq_diff_count ?? 0) + (item.orderbook?.seq_rollback_count ?? 0);
    if (diffCount > 0 || versionCount > 0) {
      queue.push({
        key: `invariant-${item.symbol}`,
        severity: diffCount > 0 ? "critical" : "warn",
        domain: "一致性",
        title: `${item.symbol} 簿/DB`,
        detail: `engine_only ${item.engine_only_count ?? 0} / db_only ${item.db_only_count ?? 0} / version ${versionCount}`,
        action: "查看系统与审计",
        targetSection: "system",
      });
    }
  });
  if (wsIssueCount > 0) {
    queue.push({
      key: "ws-issues",
      severity: (wsMetrics?.send_failures ?? 0) > 0 || (wsMetrics?.dropped_sockets ?? 0) > 0 ? "critical" : "warn",
      domain: "WS",
      title: "连接投递异常",
      detail: `timeout ${wsMetrics?.send_timeouts ?? 0} / failure ${wsMetrics?.send_failures ?? 0} / dropped ${wsMetrics?.dropped_sockets ?? 0}`,
      action: "查看系统与审计",
      targetSection: "system",
    });
  }
  marketIssues.forEach((item) => {
    queue.push({
      key: `market-${item.symbol}`,
      severity: item.status === "critical" ? "critical" : "warn",
      domain: "市场",
      title: `${item.symbol} ${item.status}`,
      detail: item.checks?.find((check) => check.severity !== "ok")?.detail ?? `spread ${fmt(item.metrics.spread_pct, 4)}%`,
      action: "查看市场运营",
      targetSection: "markets",
      targetSymbol: item.symbol,
      targetMarketDetailTab: "overview",
    });
  });
  instanceIssues.forEach(({ symbol, instance }) => {
    queue.push({
      key: `instance-${symbol}`,
      severity: instance?.status === "error" ? "critical" : "warn",
      domain: "机器人",
      title: `${symbol} ${adminStatusText(instance?.status)}`,
      detail: instance?.runtime_strategy_mismatch ? "运行策略与保存策略不一致" : instance?.last_error ?? instance?.heartbeat_status ?? "-",
      action: "查看机器人运营",
      targetSection: "maker_config",
      targetSymbol: symbol,
    });
  });
  sourceIssues.forEach((state) => {
    queue.push({
      key: `source-${state.symbol}`,
      severity: "warn",
      domain: "价格源",
      title: `${state.symbol} ${state.source_status}`,
      detail: state.source_message ?? `mark ${fmt(state.mark_price, 4)} / external ${fmt(state.external_mark_price, 4)}`,
      action: "查看合约清算",
      targetSection: "contracts",
      targetSymbol: state.symbol,
      targetContractTab: "overview",
    });
  });
  [...maintenanceErrors, ...liquidityErrors].forEach((item, index) => {
    queue.push({
      key: `maintenance-error-${index}-${item.symbol}`,
      severity: "critical",
      domain: "合约维护",
      title: `${item.symbol} 维护错误`,
      detail: item.error,
      action: "查看合约清算",
      targetSection: "contracts",
      targetSymbol: item.symbol,
      targetContractTab: "overview",
    });
  });
  riskAlerts.forEach((item, index) => {
    const distance = Number(item.liquidation_distance_pct);
    queue.push({
      key: `risk-alert-${item.symbol}-${item.user_id}-${item.side}-${index}`,
      severity: item.risk_status?.includes("liquidation") || (Number.isFinite(distance) && distance <= 0.02) ? "critical" : "warn",
      domain: "仓位",
      title: `${item.symbol} ${item.username} ${item.side}`,
      detail: `${item.risk_status} · 距强平 ${fmt(distance * 100, 2)}% · buffer ${fmt(item.margin_buffer, 4)}`,
      action: "查看仓位风险",
      targetSection: "contracts",
      targetSymbol: item.symbol,
      targetContractTab: "positions",
    });
  });
  stressedPositions.forEach((item, index) => {
    const distance = Number(item.position.liquidation_distance_pct);
    queue.push({
      key: `position-${item.user.id}-${item.position.symbol}-${item.position.side}-${index}`,
      severity: item.position.risk_status?.includes("liquidation") || (Number.isFinite(distance) && distance <= 0.02) ? "critical" : "warn",
      domain: "仓位",
      title: `${item.position.symbol} ${item.user.username} ${item.position.side}`,
      detail: `${item.position.risk_status ?? "-"} · 距强平 ${fmt(distance * 100, 2)}% · 未实现 ${fmt(item.position.unrealized_pnl, 4)}`,
      action: "查看合约清算",
      targetSection: "contracts",
      targetSymbol: item.position.symbol,
      targetContractTab: "positions",
    });
  });
  failedFundingJobs.slice(0, 8).forEach((job) => {
    queue.push({
      key: `funding-${job.job_id}`,
      severity: "critical",
      domain: "资金费",
      title: `${job.symbol} 资金费任务失败`,
      detail: `${bjDateTime(job.funding_time)} · ${job.last_error ?? "failed"} · #${job.attempt_count}/${job.max_attempts}`,
      action: "查看资金费",
      targetSection: "contracts",
      targetSymbol: job.symbol,
      targetContractTab: "funding",
    });
  });
  if (residualBadDebt > 0) {
    queue.push({
      key: "insurance-bad-debt",
      severity: "critical",
      domain: "保险基金",
      title: "存在未覆盖坏账",
      detail: `residual_bad_debt ${fmt(residualBadDebt, 4)}`,
      action: "查看保险基金",
      targetSection: "contracts",
      targetContractTab: "insurance",
    });
  }
  if (adlResidual > 0 || activeAdlResidual > 0) {
    queue.push({
      key: "adl-residual",
      severity: "critical",
      domain: "ADL",
      title: "存在 ADL 剩余风险",
      detail: `liquidation residual ${fmt(adlResidual, 4)} / adl residual ${fmt(activeAdlResidual, 4)}`,
      action: "查看强平 / ADL",
      targetSection: "contracts",
      targetContractTab: "liquidation",
    });
  }
  failedAdminOperations.slice(0, 8).forEach((item) => {
    const operationFilters = adminOperationFiltersForItem(item);
    queue.push({
      key: `admin-operation-${item.operation_id}`,
      severity: failedAdminOperationSeverity(item),
      domain: "操作失败",
      title: operationTypeLabel(item.operation_type),
      detail: `${operationDomainLabel(item.domain)} · ${adminOperationTargetText(item)} · ${adminOperationActorText(item)} · ${item.summary}`,
      action: "查看操作记录",
      targetSection: "system",
      targetSystemTab: "audit",
      targetOperationDomain: operationFilters.domain,
      targetOperationStatus: operationFilters.status,
      targetOperationType: operationFilters.operationType,
      targetOperationSymbol: operationFilters.targetSymbol,
    });
  });
  const sortedQueue = queue.sort((left, right) => riskSeverityRank[left.severity] - riskSeverityRank[right.severity] || left.domain.localeCompare(right.domain));
  return {
    sortedQueue,
    criticalCount: sortedQueue.filter((item) => item.severity === "critical").length,
    warnCount: sortedQueue.filter((item) => item.severity === "warn").length,
    activePositions,
    stressedPositions,
    closestLiquidationDistance,
    riskAlerts,
    failedFundingJobs,
    activeFundingJobs,
    sourceIssues,
    invariantDiffCount,
    versionAlertCount,
    wsIssueCount,
    marketIssues,
    instanceIssues,
    residualBadDebt,
    adlResidual,
    activeAdlResidual,
  };
}

function RiskMonitoringBoundaryStrip({
  sortedQueueCount,
  criticalCount,
  warnCount,
  activePositionsCount,
  stressedPositionsCount,
  failedFundingJobCount,
  residualBadDebt,
  sourceIssueCount,
  marketIssueCount,
  instanceIssueCount,
  invariantDiffCount,
  wsIssueCount,
  failedOperationCount,
}: {
  sortedQueueCount: number;
  criticalCount: number;
  warnCount: number;
  activePositionsCount: number;
  stressedPositionsCount: number;
  failedFundingJobCount: number;
  residualBadDebt: number;
  sourceIssueCount: number;
  marketIssueCount: number;
  instanceIssueCount: number;
  invariantDiffCount: number;
  wsIssueCount: number;
  failedOperationCount: number;
}) {
  const rows = [
    {
      domain: "总队列",
      signal: `${sortedQueueCount} 待处理 · 高优先级 ${criticalCount} · 需关注 ${warnCount}`,
      owner: "风险监控",
      boundary: "只做跨域聚合、排序、导出和分流；不在这里直接执行清算、重试、调账、停机或清理。",
      target: "按队列按钮进入业务页",
    },
    {
      domain: "仓位 / 保证金",
      signal: `风险仓位 ${stressedPositionsCount} / 活跃 ${activePositionsCount}`,
      owner: "合约清算",
      boundary: "查看强平距离、维持保证金和逐仓风险；具体仓位、强平、ADL 处理回合约清算。",
      target: "合约清算 -> 仓位风险",
    },
    {
      domain: "资金费 / 清算",
      signal: `资金费失败 ${failedFundingJobCount} · 坏账剩余 ${fmt(residualBadDebt, 4)}`,
      owner: "合约清算",
      boundary: "资金费重试、结算、保险基金调账和 ADL 都保留原业务页确认，不在风险监控页快捷执行。",
      target: "合约清算 -> 资金费 / 强平 / 保险基金",
    },
    {
      domain: "市场 / 价格源",
      signal: `价格源异常 ${sourceIssueCount} · 市场告警 ${marketIssueCount}`,
      owner: "市场运营",
      boundary: "市场状态、产品参数、交易模式和外部价格源核对回市场运营；风险页只暴露异常入口。",
      target: "市场运营",
    },
    {
      domain: "机器人运行",
      signal: `实例异常 ${instanceIssueCount}`,
      owner: "机器人运营",
      boundary: "实例启停、重启、日志、heartbeat、guard 和运行参数回机器人运营；风险页不直接停机或撤单。",
      target: "机器人运营",
    },
    {
      domain: "系统一致性",
      signal: `簿/DB差异 ${invariantDiffCount} · WS异常 ${wsIssueCount}`,
      owner: "系统与审计",
      boundary: "部署检查、orderbook invariants、WS 和运行资源回系统与审计；清理和重建类动作保留操作边界。",
      target: "系统与审计",
    },
    {
      domain: "操作失败",
      signal: `失败记录 ${failedOperationCount}`,
      owner: "系统与审计",
      boundary: "客户端观察到的高风险/低频操作失败只进入记录和待处理，不自动重试、不自动补偿。",
      target: "系统与审计 -> 操作记录",
    },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">风险监控边界</h3>
        <p className="mt-1 text-sm text-slate-500">风险监控是值班分流台：先识别风险来自哪个域，再回对应业务页处理；这里不承接危险执行动作。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1060px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">风险域</th>
              <th className="px-3 py-2 font-normal">当前信号</th>
              <th className="px-3 py-2 font-normal">处理主入口</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 font-normal">落点</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.signal}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-slate-400">{row.target}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RiskSeverityReviewStrip({
  criticalCount,
  warnCount,
  infoCount,
}: {
  criticalCount: number;
  warnCount: number;
  infoCount: number;
}) {
  const rows: { severity: RiskSeverity; count: number }[] = [
    { severity: "critical", count: criticalCount },
    { severity: "warn", count: warnCount },
    { severity: "info", count: infoCount },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">风险分级处置口径</h3>
        <p className="mt-1 text-sm text-slate-500">把待处理队列先分成当班处理、当班复核和观察留痕；分级只决定排查优先级，不替代业务页确认、清算或审计。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">级别</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = riskSeverityReview(row.severity);
              return (
                <tr key={row.severity} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2">
                    <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] ${riskSeverityClass(row.severity)}`}>
                      {riskSeverityLabel(row.severity)} · {review.level}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RiskMonitoringPanel({
  deploymentChecklist,
  systemStatus,
  marketSymbols,
  marketSurveillance,
  makerInstances,
  positions,
  marketStates,
  fundingJobs,
  liquidationEvents,
  adlEvents,
  insuranceEvents,
  maintenance,
  adminOperations,
  detailLoadState,
  onOpenRiskItem,
  onRefresh,
}: {
  deploymentChecklist?: DeploymentChecklist;
  systemStatus?: SystemStatus;
  marketSymbols: string[];
  marketSurveillance: Record<string, MarketSurveillanceItem>;
  makerInstances: Record<string, MakerInstanceStatus>;
  positions: ContractPositionAdminItem[];
  marketStates: ContractPriceState[];
  fundingJobs: ContractFundingJob[];
  liquidationEvents: ContractLiquidationEventAdminItem[];
  adlEvents: ContractAdlEventAdminItem[];
  insuranceEvents: ContractInsuranceEventAdminItem[];
  maintenance: ContractMaintenanceStatus;
  adminOperations: AdminOperationAuditItem[];
  detailLoadState: ContractDetailLoadState;
  onOpenRiskItem: (item: RiskQueueItem) => void;
  onRefresh: () => void;
}) {
  const {
    sortedQueue,
    criticalCount,
    warnCount,
    activePositions,
    stressedPositions,
    closestLiquidationDistance,
    riskAlerts,
    failedFundingJobs,
    activeFundingJobs,
    sourceIssues,
    invariantDiffCount,
    versionAlertCount,
    wsIssueCount,
    marketIssues,
    instanceIssues,
    residualBadDebt,
  } = buildRiskQueueSnapshot({
    deploymentChecklist,
    systemStatus,
    marketSymbols,
    marketSurveillance,
    makerInstances,
    positions,
    marketStates,
    fundingJobs,
    liquidationEvents,
    adlEvents,
    insuranceEvents,
    maintenance,
    adminOperations,
  });
  const failedOperationCount = adminOperations.filter((item) => item.status === "failed" || item.status === "error").length;
  const infoCount = sortedQueue.filter((item) => item.severity === "info").length;
  const [riskView, setRiskView] = useState<RiskMonitoringView>("overview");
  const domainSignalCount =
    stressedPositions.length +
    failedFundingJobs.length +
    sourceIssues.length +
    marketIssues.length +
    instanceIssues.length +
    invariantDiffCount +
    versionAlertCount +
    wsIssueCount +
    failedOperationCount;
  const riskViewOptions: Array<{
    key: RiskMonitoringView;
    label: string;
    hint: string;
    badge?: string;
    tone?: "neutral" | "warn" | "danger";
  }> = [
    { key: "overview", label: "风险总览", hint: "仓位 / 清算 / 系统", badge: String(domainSignalCount), tone: domainSignalCount > 0 ? "warn" : "neutral" },
    {
      key: "queue",
      label: "处置队列",
      hint: "待处理 / 导出",
      badge: String(sortedQueue.length),
      tone: criticalCount > 0 ? "danger" : warnCount > 0 ? "warn" : "neutral",
    },
    { key: "boundary", label: "分级边界", hint: "职责 / 口径", badge: "只读", tone: "neutral" },
  ];
  const exportRiskQueue = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_risk_queue_${stamp}.csv`,
      [
        "severity",
        "severity_label",
        "domain",
        "title",
        "detail",
        "action",
        "target",
        "target_section",
        "target_symbol",
        "target_market_tab",
        "target_contract_tab",
        "target_system_tab",
        "target_operation_domain",
        "target_operation_status",
        "target_operation_type",
        "target_operation_symbol",
        "triage_level",
        "verification_path",
        "boundary_note",
        "queue_key",
      ],
      sortedQueue.map((item) => {
        const review = riskSeverityReview(item.severity);
        return [
          item.severity,
          riskSeverityLabel(item.severity),
          item.domain,
          item.title,
          item.detail,
          item.action,
          riskQueueTargetText(item),
          item.targetSection,
          item.targetSymbol ?? "",
          item.targetMarketDetailTab ?? "",
          item.targetContractTab ?? "",
          item.targetSystemTab ?? "",
          item.targetOperationDomain ?? "",
          item.targetOperationStatus ?? "",
          item.targetOperationType ?? "",
          item.targetOperationSymbol ?? "",
          review.level,
          riskQueueVerificationPath(item),
          riskQueueBoundaryNote(item),
          item.key,
        ];
      }),
    );
  };

  return (
    <section className="space-y-4">
      <section className="panel rounded-2xl p-4">
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-display text-xl">风险监控</h2>
              <ContractDetailLoadBadge state={detailLoadState} />
            </div>
            <p className="mt-1 text-sm text-slate-400">合并展示仓位、资金费、清算、机器人、盘口一致性、系统告警和后台操作失败；执行类动作仍回到对应业务页完成。</p>
          </div>
          <button type="button" onClick={onRefresh} className="w-fit rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
            刷新风险状态
          </button>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-7">
          <SurveillanceMetric label="待处理" value={String(sortedQueue.length)} />
          <SurveillanceMetric label="高优先级" value={String(criticalCount)} />
          <SurveillanceMetric label="需关注" value={String(warnCount)} />
          <SurveillanceMetric label="风险仓位" value={`${stressedPositions.length} / ${activePositions.length}`} />
          <SurveillanceMetric label="价格源异常" value={String(sourceIssues.length)} />
          <SurveillanceMetric label="资金费失败" value={String(failedFundingJobs.length)} />
          <SurveillanceMetric label="簿/DB差异" value={`${invariantDiffCount} / ${versionAlertCount}`} />
        </div>
        <RiskMonitoringQuickPaths
          sortedQueueCount={sortedQueue.length}
          criticalCount={criticalCount}
          warnCount={warnCount}
          activePositionsCount={activePositions.length}
          stressedPositionsCount={stressedPositions.length}
          failedFundingJobCount={failedFundingJobs.length}
          liquidationCount={liquidationEvents.length}
          residualBadDebt={residualBadDebt}
          sourceIssueCount={sourceIssues.length}
          marketIssueCount={marketIssues.length}
          instanceIssueCount={instanceIssues.length}
          systemIssueCount={invariantDiffCount + versionAlertCount + wsIssueCount}
          failedOperationCount={failedOperationCount}
          onViewChange={setRiskView}
          onOpenRiskItem={onOpenRiskItem}
        />
        <div className="mt-4">
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">风险监控分区</div>
          <div className="grid gap-2 md:grid-cols-3">
            {riskViewOptions.map((item) => {
              const active = riskView === item.key;
              const toneClass =
                item.tone === "danger"
                  ? "bg-rose-500/14 text-rose-100"
                  : item.tone === "warn"
                    ? "bg-amber-400/12 text-amber-100"
                    : "bg-white/8 text-slate-300";
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setRiskView(item.key)}
                  className={`min-h-[68px] rounded-2xl border px-4 py-3 text-left transition ${
                    active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{item.label}</span>
                    {item.badge && <span className={`rounded-full px-2 py-0.5 text-[11px] ${toneClass}`}>{item.badge}</span>}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">{item.hint}</div>
                </button>
              );
            })}
          </div>
        </div>
      </section>

      {riskView === "boundary" && (
        <>
          <RiskMonitoringBoundaryStrip
            sortedQueueCount={sortedQueue.length}
            criticalCount={criticalCount}
            warnCount={warnCount}
            activePositionsCount={activePositions.length}
            stressedPositionsCount={stressedPositions.length}
            failedFundingJobCount={failedFundingJobs.length}
            residualBadDebt={residualBadDebt}
            sourceIssueCount={sourceIssues.length}
            marketIssueCount={marketIssues.length}
            instanceIssueCount={instanceIssues.length}
            invariantDiffCount={invariantDiffCount}
            wsIssueCount={wsIssueCount}
            failedOperationCount={failedOperationCount}
          />
          <RiskSeverityReviewStrip criticalCount={criticalCount} warnCount={warnCount} infoCount={infoCount} />
        </>
      )}

      {riskView === "queue" && (
        <section className="panel rounded-2xl p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-display text-lg">待处理队列</h3>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={exportRiskQueue}
                disabled={sortedQueue.length === 0}
                className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
              >
                导出当前待处理 CSV
              </button>
              <span className={`rounded-full px-3 py-1 text-xs ${criticalCount > 0 ? "bg-rose-500/16 text-rose-100" : warnCount > 0 ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
                {criticalCount > 0 ? "需处理" : warnCount > 0 ? "需关注" : "正常"}
              </span>
            </div>
          </div>
          {sortedQueue.length === 0 ? (
            <div className="rounded-2xl border border-emerald-400/12 bg-emerald-400/6 px-4 py-6 text-sm text-emerald-50/90">当前没有需要立即处理的风险项。</div>
          ) : (
            <RiskQueueTable items={sortedQueue} onOpenItem={onOpenRiskItem} />
          )}
        </section>
      )}

      {riskView === "overview" && (
        <section className="grid gap-3 xl:grid-cols-3">
          <ActivitySection title="仓位风险" empty={false}>
            <div className="grid gap-2">
              <SurveillanceMetric label="活跃仓位" value={String(activePositions.length)} />
              <SurveillanceMetric label="风险告警" value={String(riskAlerts.length)} />
              <SurveillanceMetric label="最近强平距离" value={closestLiquidationDistance === null ? "-" : `${fmt(closestLiquidationDistance * 100, 2)}%`} />
            </div>
            {stressedPositions.length > 0 && <ContractPositionMiniTable items={stressedPositions.slice(0, 8)} />}
          </ActivitySection>
          <ActivitySection title="资金费与清算" empty={false}>
            <div className="grid gap-2">
              <SurveillanceMetric label="资金费任务" value={`${activeFundingJobs.length} active / ${failedFundingJobs.length} failed`} />
              <SurveillanceMetric label="强平事件" value={String(liquidationEvents.length)} />
              <SurveillanceMetric label="ADL事件" value={String(adlEvents.length)} />
              <SurveillanceMetric label="坏账剩余" value={fmt(residualBadDebt, 4)} />
            </div>
          </ActivitySection>
          <ActivitySection title="系统一致性" empty={false}>
            <div className="grid gap-2">
              <SurveillanceMetric label="系统状态" value={systemStatus?.status ?? "-"} />
              <SurveillanceMetric label="WS异常" value={String(wsIssueCount)} />
              <SurveillanceMetric label="簿/DB差异" value={String(invariantDiffCount)} />
              <SurveillanceMetric label="版本异常" value={String(versionAlertCount)} />
              <SurveillanceMetric label="实例异常" value={String(instanceIssues.length)} />
              <SurveillanceMetric label="市场告警" value={String(marketIssues.length)} />
            </div>
          </ActivitySection>
        </section>
      )}
    </section>
  );
}

function RiskMonitoringQuickPaths({
  sortedQueueCount,
  criticalCount,
  warnCount,
  activePositionsCount,
  stressedPositionsCount,
  failedFundingJobCount,
  liquidationCount,
  residualBadDebt,
  sourceIssueCount,
  marketIssueCount,
  instanceIssueCount,
  systemIssueCount,
  failedOperationCount,
  onViewChange,
  onOpenRiskItem,
}: {
  sortedQueueCount: number;
  criticalCount: number;
  warnCount: number;
  activePositionsCount: number;
  stressedPositionsCount: number;
  failedFundingJobCount: number;
  liquidationCount: number;
  residualBadDebt: number;
  sourceIssueCount: number;
  marketIssueCount: number;
  instanceIssueCount: number;
  systemIssueCount: number;
  failedOperationCount: number;
  onViewChange: (view: RiskMonitoringView) => void;
  onOpenRiskItem: (item: RiskQueueItem) => void;
}) {
  const operationalIssueCount = sourceIssueCount + marketIssueCount + instanceIssueCount;
  const systemAndAuditIssueCount = systemIssueCount + failedOperationCount;
  const judgment = criticalCount > 0 ? "需处理" : warnCount > 0 ? "需关注" : "正常";
  const judgmentTone: "neutral" | "warn" | "danger" = criticalCount > 0 ? "danger" : warnCount > 0 ? "warn" : "neutral";
  const mainDomain =
    failedOperationCount > 0
      ? "系统与审计"
      : failedFundingJobCount > 0 || residualBadDebt > 0
        ? "合约清算"
        : stressedPositionsCount > 0
          ? "仓位风险"
          : operationalIssueCount > 0
            ? "市场 / 机器人"
            : systemIssueCount > 0
              ? "系统一致性"
              : "常规观察";
  const nextText =
    criticalCount > 0
      ? "先处理高优先级队列，执行动作仍回业务页确认。"
      : warnCount > 0
        ? "先定位风险来源，再按快速路径进入对应后台域。"
        : "当前只需抽样观察，不在风险页执行写操作。";
  const rows: Array<{
    title: string;
    cue: string;
    status: string;
    tone: "neutral" | "warn" | "danger";
    actionLabel: string;
    view?: RiskMonitoringView;
    target?: RiskQueueItem;
  }> = [
    {
      title: "看仓位风险",
      cue: "进入合约仓位风险，核对强平距离、保证金缓冲和风险阶梯。",
      status: `风险 ${stressedPositionsCount} / 活跃 ${activePositionsCount}`,
      tone: stressedPositionsCount > 0 ? "warn" : "neutral",
      actionLabel: "去仓位风险",
      target: {
        key: "quick-position-risk",
        severity: stressedPositionsCount > 0 ? "warn" : "info",
        domain: "合约清算",
        title: "查看仓位风险",
        detail: "从风险监控快速进入合约仓位风险核对。",
        action: "打开仓位风险",
        targetSection: "contracts",
        targetContractTab: "positions",
      },
    },
    {
      title: "查资金费任务",
      cue: "进入资金费页，核对失败任务、结算水位和逐账户记录。",
      status: failedFundingJobCount > 0 ? `失败 ${failedFundingJobCount}` : "无失败",
      tone: failedFundingJobCount > 0 ? "danger" : "neutral",
      actionLabel: "去资金费",
      target: {
        key: "quick-funding-risk",
        severity: failedFundingJobCount > 0 ? "critical" : "info",
        domain: "合约清算",
        title: "查看资金费任务",
        detail: "从风险监控快速进入资金费任务和结算水位核对。",
        action: "打开资金费",
        targetSection: "contracts",
        targetContractTab: "funding",
      },
    },
    {
      title: "复核强平 / ADL",
      cue: "进入清算事件页，核对强平、残余坏账和 ADL 历史。",
      status: `坏账 ${fmt(residualBadDebt, 2)} / 强平 ${liquidationCount}`,
      tone: residualBadDebt > 0 ? "danger" : liquidationCount > 0 ? "warn" : "neutral",
      actionLabel: "去强平 / ADL",
      target: {
        key: "quick-liquidation-risk",
        severity: residualBadDebt > 0 ? "critical" : liquidationCount > 0 ? "warn" : "info",
        domain: "合约清算",
        title: "复核强平 / ADL",
        detail: "从风险监控快速进入强平、坏账和 ADL 核对。",
        action: "打开强平 / ADL",
        targetSection: "contracts",
        targetContractTab: "liquidation",
      },
    },
    {
      title: "核对保险基金",
      cue: "进入保险基金页，核对基金余额、坏账覆盖和人工调整流水。",
      status: `坏账 ${fmt(residualBadDebt, 2)}`,
      tone: residualBadDebt > 0 ? "warn" : "neutral",
      actionLabel: "去保险基金",
      target: {
        key: "quick-insurance-risk",
        severity: residualBadDebt > 0 ? "warn" : "info",
        domain: "合约清算",
        title: "核对保险基金",
        detail: "从风险监控快速进入保险基金余额和流水核对。",
        action: "打开保险基金",
        targetSection: "contracts",
        targetContractTab: "insurance",
      },
    },
    {
      title: "看机器人 / 市场异常",
      cue: "切到处置队列，按市场、实例、价格源和盘口风险进入业务页。",
      status: `市场/实例 ${operationalIssueCount}`,
      tone: operationalIssueCount > 0 ? "warn" : "neutral",
      actionLabel: "看处置队列",
      view: "queue",
    },
    {
      title: "查系统 / 失败操作",
      cue: "有失败操作时进入操作记录；否则进入系统一致性和连接检查。",
      status: `系统 ${systemIssueCount} · 失败 ${failedOperationCount}`,
      tone: failedOperationCount > 0 ? "danger" : systemAndAuditIssueCount > 0 ? "warn" : "neutral",
      actionLabel: failedOperationCount > 0 ? "看失败记录" : "看系统一致性",
      target: {
        key: "quick-system-risk",
        severity: failedOperationCount > 0 ? "critical" : systemAndAuditIssueCount > 0 ? "warn" : "info",
        domain: "系统与审计",
        title: "查看系统与审计风险",
        detail: "从风险监控快速进入系统一致性、连接检查或失败操作记录。",
        action: "打开系统与审计",
        targetSection: "system",
        targetSystemTab: failedOperationCount > 0 ? "audit" : "consistency",
        targetOperationStatus: failedOperationCount > 0 ? "failed" : undefined,
        targetOperationDomain: failedOperationCount > 0 ? "all" : undefined,
      },
    },
  ];

  const toneClass = (tone: "neutral" | "warn" | "danger") => {
    if (tone === "danger") return "border-rose-400/20 bg-rose-500/8 text-rose-100";
    if (tone === "warn") return "border-amber-400/20 bg-amber-400/8 text-amber-100";
    return "border-white/8 bg-slate-950/25 text-slate-100";
  };

  return (
    <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h3 className="font-display text-base text-slate-100">风险快速分流路径</h3>
          <p className="mt-1 text-sm text-slate-500">按值班常见动作进入现有后台域；这里只做分流和查看，不执行资金费重试、ADL、调账、停机或清理。</p>
        </div>
        <span className={`w-fit rounded-full px-3 py-1 text-xs ${judgmentTone === "danger" ? "bg-rose-500/16 text-rose-100" : judgmentTone === "warn" ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
          {judgment}
        </span>
      </div>
      <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
          <div className="mt-1 text-sm text-slate-200">{judgment} · 待处理 {sortedQueueCount} · 高优先级 {criticalCount}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">风险来自哪里</div>
          <div className="mt-1 text-sm text-slate-200">{mainDomain}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
          <div className="mt-1 text-sm text-slate-400">{nextText}</div>
        </div>
      </div>
	      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
	        {rows.map((row) => (
	          <button
	            key={row.title}
            type="button"
            onClick={() => {
              if (row.view) {
                onViewChange(row.view);
                return;
              }
              if (row.target) onOpenRiskItem(row.target);
            }}
            className={`min-h-[112px] rounded-xl border p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-400/8 ${toneClass(row.tone)}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{row.title}</span>
              <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-200">{row.status}</span>
            </div>
            <div className="mt-2 min-h-[40px] text-xs leading-5 text-slate-500">{row.cue}</div>
            <div className="mt-3 text-xs text-cyan-100">{row.actionLabel}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function OrderStatusReviewStrip({ rows }: { rows: { bucket: OrderStateBucket; count: number }[] }) {
  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">订单状态核对口径</h3>
        <p className="mt-1 text-sm text-slate-500">按订单生命周期先判断核对路径；状态表只做查单解释和交接，不新增撤单、补单、清算或调账入口。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">状态分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用状态</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = orderStateReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{orderStateBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TradeSourceReviewStrip({ rows }: { rows: { bucket: TradeSourceBucket; count: number }[] }) {
  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">成交来源与费用口径</h3>
        <p className="mt-1 text-sm text-slate-500">按成交来源区分客户、机器人、FLOW、系统流动性和未知来源；费用、PnL 和资金变化仍回对应账本域核对。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">来源分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用来源</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = tradeSourceReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{tradeSourceBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ContractClearingOrderTradeReviewStrip({ rows }: { rows: { bucket: ContractClearingOrderTradeBucket; count: number }[] }) {
  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">合约订单成交核对口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果区分保证金占用、成交费用/PnL、平仓减仓、拒单撤单、机器人/系统单据和未知单据；这里只说明清算核对路径。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读清算核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">清算信号</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用单据</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = contractClearingOrderTradeReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{contractClearingOrderTradeBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-slate-600">当前数量是命中信号的单据数，同一单据可能同时影响来源、费用或减仓语义；该表用于排查排序，不作为结算汇总。</p>
    </section>
  );
}

function PositionRiskReviewStrip({ rows }: { rows: { bucket: PositionRiskBucket; count: number }[] }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">仓位风险分级核对口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果给运营人员排序风险和核对路径；分级只做只读排查，不执行强平、ADL、补保证金或调账。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读清算核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">风险分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">清算边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = positionRiskReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{positionRiskBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function FundingOpsReviewStrip({ rows }: { rows: { bucket: FundingOpsBucket; count: number }[] }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">资金费任务处置口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果区分失败、运行中、待结算、已结算和逐账户记录；这里只说明核对路径，不执行结算或重试。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读资金费核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">处置分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = fundingOpsReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{fundingOpsBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ClearingEventReviewStrip({ rows }: { rows: { bucket: ClearingEventBucket; count: number }[] }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">清算事件处置口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果区分待 ADL、残余坏账、保险覆盖、ADL 已执行和无坏账事件；这里只说明核对路径，不执行 ADL 或调账。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读清算核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">处置分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = clearingEventReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{clearingEventBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function InsuranceEventReviewStrip({ rows }: { rows: { bucket: InsuranceEventBucket; count: number }[] }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">保险基金处置口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果区分待处理坏账、保险覆盖、人工注入、人工扣减、零额记录和未知类型；这里只说明核对路径，不执行调账或坏账接管。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读基金核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">处置分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = insuranceEventReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{insuranceEventBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MarginLedgerReviewStrip({ rows }: { rows: { bucket: MarginLedgerBucket; count: number }[] }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">保证金流水核对口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前筛选结果区分初始化、人工维护、保证金锁定释放、开平仓结算、费用、资金费和清算记录；这里只说明核对路径，不执行调账。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读账本核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">流水分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = marginLedgerReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{marginLedgerBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RiskParameterReviewStrip({ rows }: { rows: { bucket: RiskParameterBucket; count: number }[] }) {
  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">风险参数核对口径</h3>
          <p className="mt-1 text-sm text-slate-500">按当前页面数据区分交易模式、杠杆边界、维持保证金、资金费模式、风险阶梯和配置缺口；这里只说明核对路径，不保存参数。</p>
        </div>
        <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读风控核对</span>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">参数分组</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">适用信号</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">操作边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => {
              const review = riskParameterReview(row.bucket);
              return (
                <tr key={row.bucket} className={`border-t border-white/8 align-top ${row.count > 0 ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2 font-medium text-slate-100">{riskParameterBucketLabel(row.bucket)}</td>
                  <td className="px-3 py-2 font-mono text-slate-100">{row.count}</td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.scope}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RiskQueueTable({ items, onOpenItem }: { items: RiskQueueItem[]; onOpenItem: (item: RiskQueueItem) => void }) {
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(items.length / AUDIT_TABLE_PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visibleItems = items.slice((safePage - 1) * AUDIT_TABLE_PAGE_SIZE, safePage * AUDIT_TABLE_PAGE_SIZE);

  useEffect(() => {
    setPage(1);
  }, [items.length]);

  return (
    <div>
      {items.length > AUDIT_TABLE_PAGE_SIZE && (
        <AuditTablePager page={safePage} total={items.length} pageSize={AUDIT_TABLE_PAGE_SIZE} label="待处理队列分页" onPageChange={setPage} />
      )}
      <div className="max-h-[560px] overflow-auto">
        <table className="min-w-[860px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-2 pr-3 font-normal">级别</th>
              <th className="py-2 pr-3 font-normal">域</th>
              <th className="py-2 pr-3 font-normal">事项</th>
              <th className="py-2 pr-3 font-normal">详情</th>
              <th className="py-2 pr-3 text-right font-normal">入口</th>
            </tr>
          </thead>
          <tbody className="text-slate-200">
            {visibleItems.map((item) => (
              <tr key={item.key} className="border-t border-white/8">
                <td className="py-2 pr-3">
                  <span className={`rounded-full px-2 py-1 ${riskSeverityClass(item.severity)}`}>{riskSeverityLabel(item.severity)}</span>
                </td>
                <td className="py-2 pr-3 text-slate-400">{item.domain}</td>
                <td className="py-2 pr-3 font-medium text-slate-100">{item.title}</td>
                <td className="py-2 pr-3 text-slate-400">{item.detail}</td>
                <td className="py-2 pr-3 text-right">
                  <button type="button" title={riskQueueTargetText(item)} onClick={() => onOpenItem(item)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12">
                    {item.action}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function buildAccountTimeline({
  activity,
  contractOrders,
  contractLedgerEntries,
}: {
  activity?: AdminUserActivity;
  contractOrders: ContractOrderAdminItem[];
  contractLedgerEntries: ContractLedgerAdminItem[];
}) {
  const items: AccountTimelineItem[] = [];
  activity?.recent_ledger.forEach((item) => {
    const amount = Number(item.amount || 0);
    items.push({
      key: `spot-ledger-${item.entry_id}`,
      ts: Number(item.created_at || 0),
      domain: "现货",
      event: "资金流水",
      market: item.asset,
      detail: item.change_type,
      amount: `${fmt(item.amount, 8)} ${item.asset}`,
      tone: amount > 0 ? "positive" : amount < 0 ? "negative" : "neutral",
    });
  });
  activity?.recent_orders.forEach((item) => {
    items.push({
      key: `spot-order-${item.order_id}`,
      ts: Number(item.updated_at ?? item.created_at ?? 0),
      domain: "现货",
      event: "订单",
      market: item.symbol,
      detail: `${item.side} · ${item.status}`,
      amount: `${fmt(item.remaining_quantity, 6)} / ${fmt(item.quantity, 6)}`,
      tone: item.side === "buy" ? "positive" : item.side === "sell" ? "negative" : "neutral",
    });
  });
  activity?.recent_trades.forEach((item) => {
    items.push({
      key: `spot-trade-${item.trade_id}`,
      ts: Number(item.executed_at ?? item.ts ?? 0),
      domain: "现货",
      event: "成交",
      market: item.symbol,
      detail: `${item.side ?? item.taker_side ?? "-"} · ${item.liquidity_role ?? "-"}`,
      amount: `${fmt(item.quantity, 6)} @ ${fmt(item.price, 6)}`,
      tone: (item.side ?? item.taker_side) === "buy" ? "positive" : (item.side ?? item.taker_side) === "sell" ? "negative" : "neutral",
    });
  });
  contractOrders.forEach(({ order }) => {
    items.push({
      key: `contract-order-${order.order_id}`,
      ts: Number(order.updated_at ?? order.created_at ?? 0),
      domain: "合约",
      event: "合约委托",
      market: order.symbol,
      detail: `${order.side} · ${order.position_action ?? "-"} · ${order.status}`,
      amount: `${fmt(order.remaining_quantity, 6)} / ${fmt(order.quantity, 6)}`,
      tone: order.side === "buy" ? "positive" : order.side === "sell" ? "negative" : "neutral",
    });
  });
  contractLedgerEntries.forEach(({ entry }) => {
    const amount = Number(entry.amount || 0);
    const usedDelta = Number(entry.used_margin_after || 0) - Number(entry.used_margin_before || 0);
    const displayAmount = amount !== 0 ? `${fmt(entry.amount, 8)} ${entry.margin_asset}` : `used ${fmt(usedDelta, 8)} ${entry.margin_asset}`;
    items.push({
      key: `contract-ledger-${entry.entry_id}`,
      ts: Number(entry.created_at || 0),
      domain: "合约",
      event: "保证金流水",
      market: entry.symbol,
      detail: entry.change_type,
      amount: displayAmount,
      tone: amount > 0 || usedDelta < 0 ? "positive" : amount < 0 || usedDelta > 0 ? "negative" : "neutral",
    });
  });
  return items.sort((left, right) => right.ts - left.ts).slice(0, 18);
}

function accountOnboardingRoleBoundaryCopy(role: string) {
  if (role === "mm_bot") {
    return {
      title: "机器人特殊 UID",
      chip: "运行对账对象",
      toneClass: "border-cyan-400/16 bg-cyan-400/6 text-cyan-50",
      writes: "创建 mm_bot 登录主体、API Key / Secret、现货初始模板和全市场 UID 费率模板。",
      route: "市场绑定、策略角色、实例启动和机器人资金快照仍回机器人账号或机器人运营。",
      boundary: "这不是普通客户开户；不会自动绑定市场、启动机器人、开合约保证金或改变机器人成交留存策略。",
      confirmBoundary: "机器人特殊 UID，默认费率应为 0；创建后仍需回机器人账号绑定市场和核对资金。",
    };
  }
  if (role === "admin") {
    return {
      title: "管理测试账户",
      chip: "系统控制对象",
      toneClass: "border-fuchsia-400/16 bg-fuchsia-400/6 text-fuchsia-50",
      writes: "创建后台管理登录主体、API Key / Secret、现货测试模板和全市场 UID 费率模板。",
      route: "后续身份启停、API 轮换和操作记录回账户维护与系统审计核对。",
      boundary: "管理主体不属于普通外部客户，也不是机器人策略 UID；不要把它的记录计入客户业务事实。",
      confirmBoundary: "管理测试账户，属于系统控制对象；不应用作普通客户优惠或机器人策略账号。",
    };
  }
  return {
    title: "普通客户测试账户",
    chip: "客户完整记录",
    toneClass: "border-emerald-400/16 bg-emerald-400/6 text-emerald-50",
    writes: "创建客户登录主体、API Key / Secret、现货初始模板和全市场 UID 费率模板。",
    route: "后续客户订单、成交、费用和资金事实回账户与资金、订单与成交和合约清算核对。",
    boundary: "这里只创建登录主体和现货模板；不会自动开合约保证金、绑定机器人或做统一账户抵扣。",
    confirmBoundary: "普通客户 UID，客户业务记录应完整核对；合约保证金和现货钱包仍是两套账本。",
  };
}

function AccountOnboardingRoleBoundary({ role }: { role: string }) {
  const copy = accountOnboardingRoleBoundaryCopy(role);
  return (
    <div className={`mb-3 rounded-2xl border px-3 py-3 ${copy.toneClass}`}>
      <div className="grid gap-3 lg:grid-cols-[0.75fr_1.15fr_1.1fr]">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前创建对象</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-white/8 px-2 py-0.5 text-xs">{role}</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5 text-xs">{copy.chip}</span>
          </div>
          <div className="mt-1 text-sm font-medium">{copy.title}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">会写入什么</div>
          <div className="mt-1 text-xs leading-5 text-slate-300">{copy.writes}</div>
          <div className="mt-2 text-xs leading-5 text-slate-400">{copy.route}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">不要误解成</div>
          <div className="mt-1 text-xs leading-5 text-slate-300">{copy.boundary}</div>
          <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
            <span className="rounded-full bg-white/8 px-2 py-0.5">默认费率 0</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5">不合并账本</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5">不自动绑定资源</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function AccountQuickPaths({
  selectedUser,
  defaultUser,
  accountScope,
  scopedAccountCount,
  onOpenView,
  onOpenOrderAudit,
  onOpenContractAccounts,
  onOpenBots,
}: {
  selectedUser?: AdminUser;
  defaultUser?: AdminUser;
  accountScope: AccountUserScope;
  scopedAccountCount: number;
  onOpenView: (view: AccountAdminView) => void;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
  onOpenContractAccounts: () => void;
  onOpenBots: () => void;
}) {
  const selectedKind = selectedUser ? accountUserKindForUser(selectedUser) : null;
  const defaultKind = defaultUser ? accountUserKindForUser(defaultUser) : null;
  const currentKind = selectedKind ?? defaultKind;
  const directoryAuditScope = selectedUser ? orderAuditDataScopeForUser(selectedUser) : orderAuditDataScopeForAccountScope(accountScope);
  const selectedLabel = selectedUser ? `${selectedUser.username} · UID ${selectedUser.id}` : "未选择 UID";
  const scopeLabel = accountUserScopeLabel(accountScope);
  const canOpenSingleUserView = Boolean(selectedUser ?? defaultUser);
  const accountJudgment = scopedAccountCount === 0 ? "空分类" : selectedUser ? "单 UID" : "目录模式";
  const accountJudgmentTone: "neutral" | "warn" | "danger" = scopedAccountCount === 0 ? "danger" : selectedUser ? "neutral" : "warn";
  const accountMainDomain =
    scopedAccountCount === 0
      ? "账户开立 / 分类"
      : selectedUser
        ? `${accountUserKindLabel(selectedKind!)} · ${selectedLabel}`
        : `${scopeLabel} · ${scopedAccountCount} 个主体`;
  const accountNextText =
    scopedAccountCount === 0
      ? "当前分类没有主体，可切换分类或开测试账户。"
      : selectedUser
        ? currentKind === "spot_robot" || currentKind === "contract_robot"
          ? "机器人 UID 先查订单对账、资金快照或市场绑定，不按客户资金账本理解。"
          : currentKind === "system" || currentKind === "admin"
            ? "系统或管理主体先核对身份和审计记录，避免误读成客户业务账户。"
            : "可进入单 UID 核查、维护 UID 费率或查看订单成交。"
        : "当前是目录模式；先选单 UID 核查，或按分类继续查看账本边界。";
  const feePathTitle =
    currentKind === "customer"
      ? "维护客户 UID 手续费"
      : currentKind === "spot_robot" || currentKind === "contract_robot"
        ? "维护机器人 UID 费率"
        : currentKind === "admin" || currentKind === "system"
          ? "维护内部 UID 费率"
          : "维护单 UID 手续费";
  const feePathCue =
    currentKind === "customer"
        ? "普通外部客户的单独手续费放这里；只影响后续成交，不回算历史费用。"
      : currentKind === "spot_robot" || currentKind === "contract_robot"
        ? "机器人默认费率应为 0；只在压测或对账需要时覆盖，不改变成交留存策略。"
        : currentKind === "admin" || currentKind === "system"
          ? "内部主体费率只服务测试、清算或演练用途，不要当客户优惠入口。"
          : "选择一个 UID 后维护全市场或单市场手续费覆盖，市场默认费率仍回市场运营。";
  const feePathStatus =
    selectedUser
      ? `${accountUserKindLabel(selectedKind!)} · UID ${selectedUser.id}`
      : defaultUser && defaultKind
        ? `未指定 UID · 默认${accountUserKindLabel(defaultKind)}`
        : "当前分类无主体";
  const rows: Array<{
    title: string;
    cue: string;
    status: string;
    actionLabel: string;
    disabled?: boolean;
    onClick: () => void;
    tone: "neutral" | "warn" | "robot" | "contract";
  }> = [
    {
      title: "开测试账户",
      cue: "创建登录主体、API Key、现货初始模板和 UID 费率模板。",
      status: "账户开立",
      actionLabel: "去开户",
      onClick: () => onOpenView("onboarding"),
      tone: "neutral",
    },
    {
      title: selectedUser ? "核查当前 UID" : "核查单个 UID",
      cue: selectedUser
        ? "同屏看现货余额、合约保证金、合约持仓、委托和 UID 时间线。"
        : "进入单 UID 核查后选择或默认打开当前分类的第一个主体。",
      status: selectedUser ? selectedLabel : defaultUser ? `未指定 UID · 默认 #${defaultUser.id}` : "当前分类无主体",
      actionLabel: "去单 UID 核查",
      disabled: !canOpenSingleUserView,
      onClick: () => onOpenView("detail"),
      tone: "neutral",
    },
    {
      title: feePathTitle,
      cue: feePathCue,
      status: feePathStatus,
      actionLabel: "去费率维护",
      disabled: !canOpenSingleUserView,
      onClick: () => onOpenView("maintenance"),
      tone: "warn",
    },
    {
      title: "维护身份 / 现货资金",
      cue: "维护角色、登录/API 启停、现货入账/扣款和测试资金恢复；合约保证金仍回合约清算。",
      status: selectedKind
        ? accountUserKindLabel(selectedKind)
        : defaultKind
          ? `未指定 UID · 默认${accountUserKindLabel(defaultKind)}`
          : "当前分类无主体",
      actionLabel: "去账户维护",
      disabled: !canOpenSingleUserView,
      onClick: () => onOpenView("maintenance"),
      tone: "warn",
    },
    {
      title: selectedUser ? "查当前 UID 订单成交" : "查当前分类订单成交",
      cue: selectedUser
        ? "带 UID 和账户数据域进入只读订单成交审计，区分客户、机器人和系统记录。"
        : directoryAuditScope === "all"
          ? "未指定 UID 时进入全部数据域审计，可再按账户和数据域筛选。"
          : `按当前${scopeLabel}进入${orderAuditDataScopeLabel(directoryAuditScope)}，避免先混入其他数据域。`,
      status: selectedUser ? orderAuditDataScopeLabel(directoryAuditScope) : directoryAuditScope === "all" ? "全部数据域" : `${scopeLabel} · ${orderAuditDataScopeLabel(directoryAuditScope)}`,
      actionLabel: "去订单审计",
      onClick: () => onOpenOrderAudit(selectedUser ? { userId: selectedUser.id, status: "all", dataScope: directoryAuditScope } : { status: "all", dataScope: directoryAuditScope }),
      tone: "neutral",
    },
    {
      title: "看合约保证金",
      cue: "进入合约清算的保证金账户，不在账户页混做保证金调账或清算处置。",
      status: "合约清算",
      actionLabel: "去保证金账户",
      onClick: onOpenContractAccounts,
      tone: "contract",
    },
    {
      title: "看机器人绑定",
      cue: "进入机器人账号，核对特殊 UID、市场绑定、API Key 和对账留存边界。",
      status: selectedKind === "spot_robot" || selectedKind === "contract_robot" ? accountUserKindLabel(selectedKind) : "机器人账号",
      actionLabel: "去机器人账号",
      onClick: onOpenBots,
      tone: "robot",
    },
  ];
  const toneClass = (tone: "neutral" | "warn" | "robot" | "contract") => {
    if (tone === "warn") return "border-amber-400/16 bg-amber-400/6 hover:bg-amber-400/10";
    if (tone === "robot") return "border-cyan-400/16 bg-cyan-400/6 hover:bg-cyan-400/10";
    if (tone === "contract") return "border-violet-400/16 bg-violet-400/6 hover:bg-violet-400/10";
    return "border-white/8 bg-slate-950/25 hover:bg-white/8";
  };

  return (
    <section className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">主体快速路径</h3>
        <p className="mt-1 text-sm text-slate-500">按测试后台常见动作进入现有分段或业务域；现货钱包、合约保证金和机器人绑定仍分开核对。</p>
      </div>
      <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs ${accountJudgmentTone === "danger" ? "bg-rose-500/16 text-rose-100" : accountJudgmentTone === "warn" ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
              {accountJudgment}
            </span>
            <span className="text-sm text-slate-200">范围 {scopedAccountCount}</span>
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
          <div className="mt-1 text-sm text-slate-200">{accountMainDomain}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
          <div className={`mt-1 text-sm ${accountJudgmentTone === "danger" ? "text-rose-100" : accountJudgmentTone === "warn" ? "text-amber-100" : "text-slate-400"}`}>{accountNextText}</div>
        </div>
      </div>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => (
          <button
            key={row.title}
            type="button"
            disabled={row.disabled}
            onClick={row.onClick}
            className={`min-h-[116px] rounded-xl border p-4 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${toneClass(row.tone)}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium text-slate-100">{row.title}</span>
              <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-300">{row.status}</span>
            </div>
            <div className="mt-2 text-xs leading-5 text-slate-500">{row.cue}</div>
            <div className="mt-3 text-xs text-cyan-100">{row.actionLabel}</div>
	          </button>
	        ))}
	      </div>
		    </section>
		  );
		}

function AccountLedgerDirectory({ onOpenSection }: { onOpenSection: (section: AdminSection) => void }) {
  const rows: { domain: string; owner: string; scope: string; action: string; section: AdminSection }[] = [
    {
      domain: "登录主体",
      owner: "账户与资金",
      scope: "用户身份、密码、API Key、费率、启停状态",
      action: "留在账户目录",
      section: "accounts",
    },
    {
      domain: "现货钱包",
      owner: "账户与资金",
      scope: "现货余额、入账/扣款、现货订单和现货资金流水",
      action: "查看账户资金",
      section: "accounts",
    },
    {
      domain: "合约保证金",
      owner: "合约清算",
      scope: "保证金钱包、逐仓持仓、资金费、强平、ADL、保险基金",
      action: "去合约清算",
      section: "contracts",
    },
    {
      domain: "机器人资源",
      owner: "机器人账号",
      scope: "机器人 UID、市场绑定、API Key、运行资金快照",
      action: "去机器人账号",
      section: "bots",
    },
  ];

  return (
    <div className="mb-4 overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
      <div className="min-w-[820px]">
        <div className="grid grid-cols-[0.9fr_0.9fr_2fr_0.9fr] gap-2 border-b border-white/8 px-3 py-2 text-xs uppercase tracking-[0.14em] text-slate-500">
          <span>资金域</span>
          <span>主入口</span>
          <span>负责范围</span>
          <span className="text-right">路径</span>
        </div>
        <div className="divide-y divide-white/8 text-sm">
          {rows.map((row) => (
            <div key={row.domain} className="grid grid-cols-[0.9fr_0.9fr_2fr_0.9fr] items-center gap-2 px-3 py-2">
              <div className="font-medium text-slate-100">{row.domain}</div>
              <div className="text-slate-300">{row.owner}</div>
              <div className="min-w-0 truncate text-slate-500" title={row.scope}>{row.scope}</div>
              <div className="text-right">
                <button type="button" onClick={() => onOpenSection(row.section)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12">
                  {row.action}
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="border-t border-white/8 px-3 py-2 text-xs leading-5 text-slate-400">
          这里是账户目录，不是统一账户系统。现货余额和合约保证金仍是两套账本；机器人在本页是身份索引，在机器人账号页是资源绑定，在合约清算页才看保证金和风险。
        </div>
      </div>
    </div>
  );
}

function AccountDirectoryReviewStrip({
  options,
  activeScope,
}: {
  options: AccountUserScopeOption[];
  activeScope: AccountUserScope;
}) {
  const kindOptions = options.filter((option): option is AccountUserScopeOption & { key: AccountUserKind } =>
    accountUserKindKeys.includes(option.key as AccountUserKind),
  );

  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">账户分类核对口径</h3>
        <p className="mt-1 text-sm text-slate-500">按账户类型明确主账本、核对路径和不能混用的边界；这里只读展示，不触发调账、清算或机器人控制。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">账户分类</th>
              <th className="px-3 py-2 font-normal">主账本 / 资源域</th>
              <th className="px-3 py-2 font-normal">核对路径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {kindOptions.map((option) => {
              const review = accountDirectoryReview(option.key);
              const active = accountScopeIncludesKind(activeScope, option.key);
              return (
                <tr key={option.key} className={`border-t border-white/8 align-top ${active ? "bg-white/[0.025]" : ""}`}>
                  <td className="px-3 py-2">
                    <span className={`inline-flex rounded-full px-2 py-0.5 ${accountDirectoryReviewClass(review.tone)}`}>{option.label}</span>
                    <div className="mt-1 text-slate-500">{option.count} 个 · {option.hint}</div>
                  </td>
                  <td className="px-3 py-2 leading-5 text-slate-300">{review.primaryLedger}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{review.verificationPath}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function feeRateIsZero(value: string | number | null | undefined) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) && numeric === 0;
}

function AccountFeeGovernanceStrip({
  users,
  markets,
}: {
  users: AdminUser[];
  markets: Record<string, AdminMarketItem>;
}) {
  const marketItems = Object.values(markets);
  const nonZeroMarketDefaults = marketItems.filter((market) => (
    !feeRateIsZero(market.default_maker_fee_rate) || !feeRateIsZero(market.default_taker_fee_rate)
  ));
  const usersWithFeeProfiles = users.filter((user) => (user.fee_profiles?.length ?? 0) > 0);
  const feeProfileCount = users.reduce((total, user) => total + (user.fee_profiles?.length ?? 0), 0);
  const customerUsers = users.filter((user) => accountUserKindForUser(user) === "customer");
  const customerUsersWithFeeProfiles = customerUsers.filter((user) => (user.fee_profiles?.length ?? 0) > 0);
  const customerFeeProfileCount = customerUsers.reduce((total, user) => total + (user.fee_profiles?.length ?? 0), 0);
  const robotUsers = users.filter((user) => {
    const kind = accountUserKindForUser(user);
    return kind === "spot_robot" || kind === "contract_robot";
  });
  const robotFeeProfileCount = robotUsers.reduce((total, user) => total + (user.fee_profiles?.length ?? 0), 0);
  const rows = [
    {
      domain: "默认费率",
      owner: "账户开立 / 市场运营",
      status: `新增账户默认 0 · 创建交易币对默认 0 · 现有非零市场 ${nonZeroMarketDefaults.length}/${marketItems.length}`,
      policy: "新建账户会按表单写入全市场 UID 费率；新建市场、模板和 API schema 默认 maker/taker 为 0。",
      boundary: "已有市场默认值和已有用户费率不在本页批量改写；如要迁移到全 0，需要单独评估历史测试、机器人策略和费用口径。",
    },
    {
      domain: "客户 / UID 覆盖",
      owner: "账户维护",
      status: `客户 ${customerUsersWithFeeProfiles.length}/${customerUsers.length} · 全部 UID ${usersWithFeeProfiles.length} · ${feeProfileCount} 条覆盖`,
      policy: "普通外部客户的单独手续费放在 UID 覆盖里；现货撮合和合约撮合都会先查 UID+市场覆盖，再回退市场默认。",
      boundary: `客户 UID 覆盖只影响后续成交，不回算历史成交、现货流水、合约保证金流水、total_fees 或 PnL；当前客户覆盖 ${customerFeeProfileCount} 条。`,
    },
    {
      domain: "市场默认",
      owner: "市场运营",
      status: `${marketItems.length} 市场 · ${nonZeroMarketDefaults.length} 个当前默认非 0`,
      policy: "市场默认是没有 UID 覆盖时的兜底费率；市场级费率仍回市场运营维护，不在账户卡里批量改市场。",
      boundary: "市场默认不是客户专属优惠，也不是机器人策略参数；不要用它替代单 UID 费率治理。",
    },
    {
      domain: "机器人费率",
      owner: "机器人账号 / 账户维护",
      status: `${robotUsers.length} 个机器人 UID · ${robotFeeProfileCount} 条机器人费率覆盖`,
      policy: "机器人本质仍是特殊 UID，可用同一套费率覆盖参与对账；新增机器人默认 maker/taker 为 0。",
      boundary: "机器人高频成交留存、策略表现和库存核对回机器人运营；费率配置不等于策略参数，也不改变成交落库策略。",
    },
    {
      domain: "审计与对账",
      owner: "订单与成交 / 账户与资金 / 合约清算",
      status: "后续成交生效",
      policy: "成交手续费回订单与成交核对，现货手续费落账回现货流水，合约手续费和 total_fees 回合约清算核对。",
      boundary: "本表只说明治理口径，不保存费率、不触发调账、不重算费用、不修改历史记录。",
    },
  ];

  return (
    <section className="mb-4 rounded-2xl border border-cyan-400/12 bg-cyan-400/6 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-cyan-50">手续费治理口径</h3>
        <p className="mt-1 text-sm text-slate-400">默认 0、按 UID 覆盖、按市场兜底分开理解；这里只说明治理边界，实际保存仍在下方账户费率区逐项执行。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">治理域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">费率原则</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.policy}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AccountMaintenanceStatusStrip({
  changedFields,
  pendingLabel,
}: {
  changedFields: string[];
  pendingLabel?: string | null;
}) {
  const pending = Boolean(pendingLabel);
  const hasChanges = changedFields.length > 0;
  const title = pending ? "提交中" : hasChanges ? "有未保存修改" : "当前无未保存修改";
  const detail = pending
    ? `${pendingLabel} 正在执行，完成前先不要重复提交账户维护动作。`
    : hasChanges
      ? `本地修改尚未保存：${changedFields.join("、")}。保存前仍会弹出确认。`
      : "当前表单和已加载账户快照一致；现货调账输入属于单次执行指令，不计入未保存修改。";
  const boxClass = pending
    ? "border-cyan-400/18 bg-cyan-400/8 text-cyan-50"
    : hasChanges
      ? "border-amber-400/18 bg-amber-400/8 text-amber-50"
      : "border-white/8 bg-slate-950/25 text-slate-300";

  return (
    <div className={`mt-3 rounded-2xl border px-3 py-2 ${boxClass}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">账户维护状态</div>
          <div className="text-sm font-medium">{title}</div>
          <div className="mt-1 text-xs leading-5 text-slate-400">{detail}</div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {(hasChanges ? changedFields : ["只读核对"]).map((item) => (
            <span key={item} className="rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-200">{item}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

function accountMaintenanceBoundaryCopy(kind: AccountUserKind) {
  if (kind === "spot_robot") {
    return {
      subject: "现货机器人特殊 UID",
      operatingScope: "可维护身份、API、UID 费率和现货测试资金；市场绑定、策略参数和实例状态回机器人账号或机器人运营。",
      recordPolicy: "机器人订单成交主要服务运行对账，不能当普通客户完整业务记录；高频留存压力回订单审计和系统运行资源看候选。",
      feePolicy: "新增默认 0，只有压测或对账需要时才覆盖 UID 费率；不改变机器人交易记录留存策略。",
      toneClass: "border-cyan-400/16 bg-cyan-400/6 text-cyan-50",
    };
  }
  if (kind === "contract_robot") {
    return {
      subject: "合约机器人特殊 UID",
      operatingScope: "可维护身份、API 和 UID 费率；合约保证金、仓位、资金费和清算风险回合约清算核对。",
      recordPolicy: "机器人合约成交用于做市和对账，不等同普通外部客户完整记录；策略和实例问题回机器人运营。",
      feePolicy: "新增默认 0，覆盖费率只影响后续合约成交手续费，不改保证金、PnL、资金费或清算事实。",
      toneClass: "border-violet-400/16 bg-violet-400/6 text-violet-50",
    };
  }
  if (kind === "system") {
    return {
      subject: "系统控制账户",
      operatingScope: "只适合内部清算、流动性或演练用途；身份、API 和 UID 费率修改后应回系统与审计核对操作记录。",
      recordPolicy: "系统控制或未知来源记录不能混入普通客户活跃度，也不能替代机器人策略对账。",
      feePolicy: "系统 UID 费率不是客户优惠入口；仅在明确内部用途时覆盖，历史成交和流水不回算。",
      toneClass: "border-amber-400/16 bg-amber-400/6 text-amber-50",
    };
  }
  if (kind === "admin") {
    return {
      subject: "管理测试账户",
      operatingScope: "用于后台登录、测试演练和内部核查；可维护身份、API、UID 费率和现货测试资金。",
      recordPolicy: "管理主体记录归系统控制与审计口径，不应被看成普通客户业务事实或机器人运行对账。",
      feePolicy: "管理 UID 费率只服务测试演练；客户优惠应维护客户 UID，机器人费率应维护机器人 UID。",
      toneClass: "border-fuchsia-400/16 bg-fuchsia-400/6 text-fuchsia-50",
    };
  }
  return {
    subject: "普通客户 UID",
    operatingScope: "可维护身份、API、客户 UID 手续费和现货测试资金；合约保证金仍回合约清算，不做统一账户抵扣。",
    recordPolicy: "客户订单、成交、费用和资金事实按完整业务记录核对；机器人对账和系统控制证据不要混入客户记录。",
    feePolicy: "单 UID 或单市场费率只影响后续成交，历史成交、现货流水、合约 total_fees 和 PnL 不回算。",
    toneClass: "border-emerald-400/16 bg-emerald-400/6 text-emerald-50",
  };
}

function AccountMaintenanceBoundaryStrip({
  user,
  kind,
  feeProfileCount,
  marketCount,
}: {
  user: AdminUser;
  kind: AccountUserKind;
  feeProfileCount: number;
  marketCount: number;
}) {
  const copy = accountMaintenanceBoundaryCopy(kind);
  const profileCoverage = marketCount > 0 ? `${feeProfileCount}/${marketCount}` : String(feeProfileCount);
  return (
    <div className={`mt-3 rounded-2xl border px-3 py-3 ${copy.toneClass}`}>
      <div className="grid gap-3 lg:grid-cols-[0.85fr_1.1fr_1.1fr]">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前对象</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs ${accountUserKindClass(kind)}`}>{accountUserKindLabel(kind)}</span>
            <span className="font-mono text-xs text-slate-300">UID {user.id}</span>
          </div>
          <div className="mt-1 text-sm font-medium">{copy.subject}</div>
          <div className="mt-1 text-xs text-slate-400">UID 费率覆盖 {profileCoverage}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">本页维护</div>
          <div className="mt-1 text-xs leading-5 text-slate-300">{copy.operatingScope}</div>
          <div className="mt-2 text-xs leading-5 text-slate-400">{copy.feePolicy}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">不要混用</div>
          <div className="mt-1 text-xs leading-5 text-slate-300">{copy.recordPolicy}</div>
          <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
            <span className="rounded-full bg-white/8 px-2 py-0.5">后续成交生效</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5">不回算历史</span>
            <span className="rounded-full bg-white/8 px-2 py-0.5">不改账本源</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SpotBalanceAdjustmentRunbookStrip() {
  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">现货调账恢复演练口径</h3>
        <p className="mt-1 text-sm text-slate-500">现货入账、扣款和测试资金重置仍在 UID 行执行；这里先给运营核对口径，避免把现货钱包、合约保证金和机器人资源混成一个统一账户。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1180px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">操作</th>
              <th className="px-3 py-2 font-normal">执行前核对</th>
              <th className="px-3 py-2 font-normal">执行后核对</th>
              <th className="px-3 py-2 font-normal">失败 / 误操作恢复</th>
              <th className="px-3 py-2 font-normal">边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {spotBalanceAdjustmentRunbooks.map((row) => (
              <tr key={row.key} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 ${spotBalanceAdjustmentRunbookClass(row.tone)}`}>{row.operation}</span>
                </td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.preCheck}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.postCheck}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.recoveryPath}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="border-t border-white/8 px-3 py-2 text-xs leading-5 text-slate-400">
          合约保证金调账、资金费补偿、强平坏账和保险基金调整仍回到“合约清算 / 系统与审计”核对；本页不承担统一账户或清算审批职责。
        </div>
      </div>
    </div>
  );
}

type BotDirectoryKind = "spot_maker" | "spot_flow" | "perp_maker" | "perp_flow" | "perp_hedge" | "unknown";
type BotDirectoryReview = {
  kind: BotDirectoryKind;
  label: string;
  primaryDomain: string;
  fundingPath: string;
  riskPath: string;
  boundary: string;
  tone: "spot" | "flow" | "perp" | "hedge" | "unknown";
};

function botDirectoryKind(market: AdminMarketItem | undefined, bot: MarketBotAccount): BotDirectoryKind {
  if (!market) return "unknown";
  if (market.product_type === "PERP") {
    if (bot.role === "flow") return "perp_flow";
    return bot.role === "hedge" ? "perp_hedge" : "perp_maker";
  }
  if (market.product_type === "SPOT") return bot.role === "flow" ? "spot_flow" : "spot_maker";
  return "unknown";
}

function botDirectoryAccountScope(kind: BotDirectoryKind): AccountUserScope {
  if (kind === "spot_maker" || kind === "spot_flow") return "spot_robot";
  if (kind === "perp_maker" || kind === "perp_flow" || kind === "perp_hedge") return "contract_robot";
  return "robot";
}

function botDirectoryReview(kind: BotDirectoryKind): BotDirectoryReview {
  if (kind === "spot_flow") {
    return {
      kind,
      label: "SPOT FLOW",
      primaryDomain: "机器人运营 + 账户与资金",
      fundingPath: "账户与资金核对现货余额、冻结和现货流水；机器人运营核对 FLOW 队列、成交来源和暂停原因。",
      riskPath: "风险监控核对盘口一致性、FLOW 暂停和成交异常；不进入合约清算。",
      boundary: "FLOW 只服务现货本地沙盒成交流，不应使用合约保证金、资金费、强平或保险基金。",
      tone: "flow",
    };
  }
  if (kind === "perp_maker") {
    return {
      kind,
      label: "PERP maker",
      primaryDomain: "机器人运营 + 合约清算",
      fundingPath: "合约清算核对合约保证金账户、逐仓仓位、合约流水和资金费；机器人账号只看绑定和资源快照。",
      riskPath: "合约清算核对仓位风险、强平/ADL、保险基金和风险阶梯；机器人运营核对实例、策略和日志。",
      boundary: "PERP maker 不使用现货钱包抵扣保证金；风险处置不在机器人账号页执行。",
      tone: "perp",
    };
  }
  if (kind === "perp_flow") {
    return {
      kind,
      label: "PERP FLOW",
      primaryDomain: "机器人运营 + 合约清算",
      fundingPath: "合约清算核对 FLOW UID 的合约保证金账户和合约流水；机器人账号只看绑定和资源快照。",
      riskPath: "机器人运营核对 FLOW 队列、暂停原因、source 分布和 IOC 成功率；合约清算核对保证金占用和资金费影响。",
      boundary: "PERP FLOW 是本地沙盒成交流资源，不是客户 UID，也不是现货钱包；启动、暂停和参数回机器人运营。",
      tone: "flow",
    };
  }
  if (kind === "perp_hedge") {
    return {
      kind,
      label: "PERP hedge",
      primaryDomain: "机器人运营 + 合约清算",
      fundingPath: "合约清算核对 hedge UID 的合约保证金、仓位和流水；机器人运营核对对冲策略绑定。",
      riskPath: "风险监控和合约清算核对净敞口、强平距离、ADL 和保险基金影响。",
      boundary: "hedge 是运行角色，不是独立资金账本；不要把 hedge 资金和现货机器人余额混用。",
      tone: "hedge",
    };
  }
  if (kind === "spot_maker") {
    return {
      kind,
      label: "SPOT maker",
      primaryDomain: "机器人运营 + 账户与资金",
      fundingPath: "账户与资金核对现货 base/quote 余额、冻结和现货流水；机器人运营核对策略、挂单和实例。",
      riskPath: "风险监控核对盘口、订单簿一致性、实例 heartbeat 和日志；无资金费、强平或 ADL。",
      boundary: "SPOT maker 只使用现货钱包，不展示合约保证金或清算风险。",
      tone: "spot",
    };
  }
  return {
    kind,
    label: "未知机器人",
    primaryDomain: "机器人账号",
    fundingPath: "先回市场绑定确认 product_type 和 role，再进入对应资金或清算域核对。",
    riskPath: "未知类型不推断运行风险或清算归属。",
    boundary: "不要把未知机器人当作现货或合约资金账本处理。",
    tone: "unknown",
  };
}

function botDirectoryReviewClass(tone: BotDirectoryReview["tone"]) {
  if (tone === "perp") return "bg-violet-400/14 text-violet-100";
  if (tone === "hedge") return "bg-fuchsia-400/14 text-fuchsia-100";
  if (tone === "flow") return "bg-emerald-400/14 text-emerald-100";
  if (tone === "spot") return "bg-cyan-400/14 text-cyan-100";
  return "bg-amber-400/14 text-amber-100";
}

function BotDirectoryReviewStrip({
  rows,
}: {
  rows: { symbol: string; market?: AdminMarketItem; bot: MarketBotAccount }[];
}) {
  const expectedKinds: BotDirectoryKind[] = ["spot_maker", "spot_flow", "perp_maker", "perp_flow", "perp_hedge"];
  const counts = rows.reduce<Record<BotDirectoryKind, number>>((acc, row) => {
    const kind = botDirectoryKind(row.market, row.bot);
    acc[kind] = (acc[kind] ?? 0) + 1;
    return acc;
  }, { spot_maker: 0, spot_flow: 0, perp_maker: 0, perp_flow: 0, perp_hedge: 0, unknown: 0 });
  const reviewRows = expectedKinds.map((kind) => ({ review: botDirectoryReview(kind), count: counts[kind] ?? 0 }));
  if (counts.unknown > 0) reviewRows.push({ review: botDirectoryReview("unknown"), count: counts.unknown });

  return (
    <div className="mt-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">机器人分类核对口径</h3>
        <p className="mt-1 text-sm text-slate-500">按产品类型和运行角色区分现货机器人、SPOT FLOW、PERP maker、PERP FLOW 和 hedge；这里只读展示，不执行调账、清算或实例控制。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1180px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">机器人分类</th>
              <th className="px-3 py-2 font-normal">绑定数</th>
              <th className="px-3 py-2 font-normal">主后台 / 资源域</th>
              <th className="px-3 py-2 font-normal">资金核对</th>
              <th className="px-3 py-2 font-normal">风险 / 运行核对</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {reviewRows.map(({ review, count }) => (
              <tr key={review.kind} className={`border-t border-white/8 align-top ${count > 0 ? "bg-white/[0.025]" : ""}`}>
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] ${botDirectoryReviewClass(review.tone)}`}>{review.label}</span>
                </td>
                <td className="px-3 py-2 font-mono text-slate-100">{count}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{review.primaryDomain}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{review.fundingPath}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{review.riskPath}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{review.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function GlobalBotAccountsPanel({
  markets,
  marketSymbols,
  marketBots,
  users,
  dataRetentionDomains,
  onOpenOrderAudit,
  onOpenAccounts,
  onOpenContractAccounts,
  onOpenRobotOperations,
  onOpenAccount,
  onOpenMarket,
}: {
  markets: Record<string, AdminMarketItem>;
  marketSymbols: string[];
  marketBots: Record<string, MarketBotAccount[]>;
  users: AdminUser[];
  dataRetentionDomains?: DataRetentionDomainSummary;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
  onOpenAccounts: () => void;
  onOpenContractAccounts: () => void;
  onOpenRobotOperations: () => void;
  onOpenAccount: (userId: number, accountScope: AccountUserScope) => void;
  onOpenMarket: (symbol: string) => void;
}) {
  const rows = marketSymbols.flatMap((symbol) => (marketBots[symbol] ?? []).map((bot) => ({ symbol, market: markets[symbol], bot })));
  const robotUsers = users.filter((user) => user.role === "mm_bot");
  const spotBindings = rows.filter((row) => row.market?.product_type !== "PERP").length;
  const perpBindings = rows.filter((row) => row.market?.product_type === "PERP").length;
  const makerCount = rows.filter((row) => row.bot.role === "maker").length;
  const flowCount = rows.filter((row) => row.bot.role === "flow").length;
  const hedgeCount = rows.filter((row) => row.bot.role === "hedge").length;
  const uniqueBotUids = new Set(rows.map((row) => row.bot.uid)).size;
  type BotDirectoryView = "overview" | "classification" | "reconciliation" | "directory";
  const [botDirectoryView, setBotDirectoryView] = useState<BotDirectoryView>("overview");
  const [botDirectoryPage, setBotDirectoryPage] = useState(1);
  const botDirectoryTotalPages = Math.max(1, Math.ceil(rows.length / AUDIT_TABLE_PAGE_SIZE));
  const safeBotDirectoryPage = Math.min(botDirectoryPage, botDirectoryTotalPages);
  const visibleBotDirectoryRows = rows.slice(
    (safeBotDirectoryPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeBotDirectoryPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const botDirectoryViewOptions: Array<{
    key: BotDirectoryView;
    label: string;
    hint: string;
    badge: string;
    tone?: "neutral" | "warn";
  }> = [
    { key: "overview", label: "资源总览", hint: "身份 / 绑定 / 资金", badge: String(rows.length) },
    { key: "classification", label: "分类边界", hint: "SPOT / PERP / 角色", badge: "5" },
    {
      key: "reconciliation",
      label: "对账留存",
      hint: "明细候选 / 客户保护",
      badge: retentionValue(dataRetentionDomains?.trades.robot_only),
      tone: (dataRetentionDomains?.trades.robot_only ?? 0) > 0 ? "warn" : "neutral",
    },
    { key: "directory", label: "账号目录", hint: "UID / API / 快照", badge: String(uniqueBotUids), tone: rows.length === 0 ? "warn" : "neutral" },
  ];
  const robotAuditCount = (dataRetentionDomains?.orders.robot_retention_eligible ?? 0) + (dataRetentionDomains?.trades.robot_only ?? 0);
  const botQuickJudgment =
    rows.length === 0
      ? {
          status: "需处理",
          tone: "danger" as const,
          focus: "账号绑定",
          next: "先回市场运营补机器人账号，再启动或核对策略实例。",
        }
      : robotAuditCount > 0
        ? {
            status: "需关注",
            tone: "warn" as const,
            focus: "机器人对账候选",
            next: "先看对账留存，确认哪些明细只是机器人运行材料，不要误读成客户完整记录。",
          }
        : {
            status: "正常",
            tone: "neutral" as const,
            focus: perpBindings > 0 ? "资金账本与合约保证金" : "账号资源池",
            next: "按 UID、市场绑定、资金快照或实例日志继续核对；机器人明细仍按对账口径查看。",
          };
  const botQuickJudgmentClass =
    botQuickJudgment.tone === "danger"
      ? "bg-rose-500/16 text-rose-100"
      : botQuickJudgment.tone === "warn"
        ? "bg-amber-400/16 text-amber-100"
        : "bg-emerald-400/15 text-emerald-100";
  const botQuickPaths: Array<{
    title: string;
    cue: string;
    status: string;
    tone: "cyan" | "violet" | "emerald" | "slate";
    onClick: () => void;
  }> = [
    {
      title: "查机器人对账",
      cue: "进入订单与成交，筛选机器人数据域；这是运行对账口径，不是普通客户完整记录。",
      status: robotAuditCount > 0 ? `${retentionValue(robotAuditCount)} 条候选` : "data_domain=robot",
      tone: "cyan",
      onClick: () => onOpenOrderAudit({ dataScope: "robot", status: "all" }),
    },
    {
      title: "查资金账本",
      cue: "进入账户与资金，按 UID 核对现货资金账本、费率覆盖和登录主体。",
      status: `${uniqueBotUids} 个 UID`,
      tone: "emerald",
      onClick: onOpenAccounts,
    },
    {
      title: "看合约保证金",
      cue: "进入合约清算的保证金账户页，核对 PERP 机器人保证金、仓位和风险归属。",
      status: `PERP ${perpBindings}`,
      tone: "violet",
      onClick: onOpenContractAccounts,
    },
    {
      title: "管策略实例",
      cue: "进入机器人运营，处理策略参数、实例启动停止、日志和 FLOW guard。",
      status: `maker/flow ${makerCount}/${flowCount}`,
      tone: "slate",
      onClick: onOpenRobotOperations,
    },
  ];
  const botQuickPathClass = (tone: "cyan" | "violet" | "emerald" | "slate") => {
    if (tone === "cyan") return "border-cyan-400/16 bg-cyan-400/6 hover:bg-cyan-400/10";
    if (tone === "violet") return "border-violet-400/16 bg-violet-400/6 hover:bg-violet-400/10";
    if (tone === "emerald") return "border-emerald-400/16 bg-emerald-400/6 hover:bg-emerald-400/10";
    return "border-white/8 bg-slate-950/25 hover:bg-white/8";
  };
  useEffect(() => {
    setBotDirectoryPage(1);
  }, [rows.length]);
  const exportBotDirectory = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_bot_directory_${stamp}.csv`,
      [
        "symbol",
        "product_type",
        "uid",
        "username",
        "bot_label",
        "role",
        "strategy_role",
        "enabled",
        "bot_kind",
        "api_key_status",
        "api_secret_status",
        "primary_resource_domain",
        "funding_verification_path",
        "risk_verification_path",
        "boundary_note",
        "audit_data_domain",
        "detail_retention_policy",
        "customer_record_boundary",
      ],
      rows.map(({ symbol, market, bot }) => {
        const review = botDirectoryReview(botDirectoryKind(market, bot));
        return [
          symbol,
          market?.product_type ?? "",
          bot.uid,
          bot.username,
          bot.bot_label,
          bot.role,
          bot.strategy_role ?? "",
          bot.is_enabled ? "enabled" : "paused",
          review.label,
          bot.api_key ? "present" : "missing",
          bot.api_secret_present === false ? "missing" : "present",
          review.primaryDomain,
          review.fundingPath,
          review.riskPath,
          review.boundary,
          "robot",
          "机器人高频订单/成交明细优先作为对账候选，后续适合聚合、抽样、短保留或汇总表。",
          "客户参与成交和普通客户费用、流水、仓位、资金费事实不能被机器人留存策略覆盖。",
        ];
      }),
    );
  };
  return (
    <section className="space-y-4">
      <div className="panel rounded-2xl p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h2 className="font-display text-xl">机器人账号资源池</h2>
            <p className="mt-1 text-sm text-slate-400">这里查看全局 UID、API key、角色、启用状态、服务市场和资金快照；现货调账回到账户与资金，合约保证金、仓位和清算风险回到合约清算。</p>
          </div>
          <button type="button" onClick={exportBotDirectory} className="w-fit rounded-xl bg-white/8 px-3 py-2 text-sm text-slate-100 transition hover:bg-white/12">
            导出当前机器人口径 CSV
          </button>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-4">
          <SurveillanceMetric label="市场绑定账号" value={String(rows.length)} />
          <SurveillanceMetric label="启用账号" value={String(rows.filter((row) => row.bot.is_enabled).length)} />
          <SurveillanceMetric label="maker / flow" value={`${rows.filter((row) => row.bot.role === "maker").length} / ${rows.filter((row) => row.bot.role === "flow").length}`} />
          <SurveillanceMetric label="独立 mm_bot 主体" value={String(robotUsers.length)} />
        </div>
        <div className="mt-4">
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">机器人账号分区</div>
          <div className="grid gap-2 md:grid-cols-4">
            {botDirectoryViewOptions.map((item) => {
              const active = botDirectoryView === item.key;
              const toneClass = item.tone === "warn" ? "bg-amber-400/12 text-amber-100" : "bg-white/8 text-slate-300";
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setBotDirectoryView(item.key)}
                  className={`min-h-[68px] rounded-2xl border px-4 py-3 text-left transition ${
                    active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{item.label}</span>
                    <span className={`rounded-full px-2 py-0.5 text-[11px] ${toneClass}`}>{item.badge}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">{item.hint}</div>
                </button>
              );
            })}
          </div>
        </div>
        {botDirectoryView === "overview" && (
          <>
            <div className="mt-4 border-y border-white/8 bg-white/[0.025] px-3 py-3">
              <div className="mb-3">
                <div className="text-sm font-medium text-slate-100">机器人账号快速路径</div>
                <div className="mt-1 text-xs leading-5 text-slate-500">
                  机器人是特殊测试 UID；账号资源在这里看，订单成交、现货资金、合约保证金和策略实例分别回对应后台域核对。
                </div>
              </div>
              <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <span className={`rounded-full px-2 py-0.5 text-xs ${botQuickJudgmentClass}`}>{botQuickJudgment.status}</span>
                    <span className="text-sm text-slate-200">绑定 {rows.length} · UID {uniqueBotUids}</span>
                  </div>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
                  <div className="mt-1 text-sm text-slate-200">{botQuickJudgment.focus}</div>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
                  <div className={`mt-1 text-sm ${botQuickJudgment.tone === "danger" ? "text-rose-100" : botQuickJudgment.tone === "warn" ? "text-amber-100" : "text-slate-400"}`}>
                    {botQuickJudgment.next}
                  </div>
                </div>
              </div>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
                {botQuickPaths.map((path) => (
                  <button
                    key={path.title}
                    type="button"
                    onClick={path.onClick}
                    className={`min-h-[104px] rounded-xl border p-3 text-left transition ${botQuickPathClass(path.tone)}`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-medium text-slate-100">{path.title}</span>
                      <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-300">{path.status}</span>
                    </div>
                    <div className="mt-2 text-xs leading-5 text-slate-500">{path.cue}</div>
                  </button>
                ))}
              </div>
            </div>
            <BotDomainBoundaryStrip
              bindingCount={rows.length}
              uniqueBotUids={uniqueBotUids}
              robotUserCount={robotUsers.length}
              spotBindings={spotBindings}
              perpBindings={perpBindings}
              makerCount={makerCount}
              flowCount={flowCount}
              hedgeCount={hedgeCount}
            />
          </>
        )}
        {botDirectoryView === "classification" && <BotDirectoryReviewStrip rows={rows} />}
        {botDirectoryView === "reconciliation" && (
          <BotReconciliationRetentionStrip
            summary={dataRetentionDomains}
            bindingCount={rows.length}
            uniqueBotUids={uniqueBotUids}
            robotUserCount={robotUsers.length}
            makerCount={makerCount}
            flowCount={flowCount}
            hedgeCount={hedgeCount}
            onOpenOrderAudit={onOpenOrderAudit}
          />
        )}
      </div>
      {botDirectoryView === "directory" && (
        <section className="panel overflow-hidden rounded-2xl">
          <div className="border-b border-white/8 px-4 py-3">
            <h3 className="font-display text-lg">账号目录</h3>
            <p className="mt-1 text-sm text-slate-500">这里只查看 UID、API Key、启用状态和资金快照；订单对账回订单与成交，资金核查回账户与资金，绑定编辑回市场详情。</p>
          </div>
          {rows.length > AUDIT_TABLE_PAGE_SIZE && (
            <div className="px-4 pt-3">
              <AuditTablePager
                page={safeBotDirectoryPage}
                total={rows.length}
                pageSize={AUDIT_TABLE_PAGE_SIZE}
                label="机器人账号目录分页"
                onPageChange={setBotDirectoryPage}
              />
            </div>
          )}
          <div className="overflow-auto">
            <table className="min-w-[1240px] text-left text-sm">
              <thead className="bg-white/5 text-xs uppercase tracking-[0.14em] text-slate-500">
                <tr>
                  <th className="px-4 py-3">市场</th>
                  <th className="px-4 py-3">UID / 用户名</th>
                  <th className="px-4 py-3">角色</th>
                  <th className="px-4 py-3">API Key</th>
                  <th className="px-4 py-3">状态</th>
                  <th className="px-4 py-3">资金状态</th>
                  <th className="px-4 py-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/8">
                {visibleBotDirectoryRows.map(({ symbol, market, bot }) => {
                  const isPerp = market?.product_type === "PERP";
                  const directoryKind = botDirectoryKind(market, bot);
                  return (
                    <tr key={`${symbol}-${bot.id}`} className="text-slate-300">
                      <td className="px-4 py-3">
                        <div className="font-mono text-slate-100">{symbol}</div>
                        <div className="text-xs text-slate-500">{market?.product_type ?? "-"}</div>
                      </td>
                      <td className="px-4 py-3">
                        <div>{bot.uid}</div>
                        <div className="text-xs text-slate-500">{bot.username}</div>
                      </td>
                      <td className="px-4 py-3">{bot.role} / {bot.strategy_role ?? "-"}</td>
                      <td className="px-4 py-3 font-mono">{bot.api_key}</td>
                      <td className="px-4 py-3">{bot.is_enabled ? "enabled" : "paused"}</td>
                      <td className="px-4 py-3">
                        {isPerp
                          ? `${fmt(bot.contract_account?.available_margin, 4)} 可用保证金 / PnL ${fmt(bot.contract_account?.unrealized_pnl, 4)}`
                          : `${bot.quote_balance.asset} ${fmt(bot.quote_balance.available, 2)} / ${bot.base_balance.asset} ${fmt(bot.base_balance.available, 4)}`}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex flex-wrap justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => onOpenOrderAudit({
                              product: auditProductFromProductType(market?.product_type),
                              symbol,
                              userId: bot.uid,
                              status: "all",
                              dataScope: "robot",
                            })}
                            className="rounded-xl bg-cyan-400/16 px-3 py-1.5 text-xs text-cyan-100"
                          >
                            查订单成交
                          </button>
                          <button
                            type="button"
                            onClick={() => onOpenAccount(bot.uid, botDirectoryAccountScope(directoryKind))}
                            className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12"
                          >
                            查资金账本
                          </button>
                          <button type="button" onClick={() => onOpenMarket(symbol)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12">市场绑定</button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {rows.length === 0 && <tr><td colSpan={7} className="px-4 py-8 text-center text-slate-500">暂无市场绑定机器人。</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  );
}

function BotReconciliationRetentionStrip({
  summary,
  bindingCount,
  uniqueBotUids,
  robotUserCount,
  makerCount,
  flowCount,
  hedgeCount,
  onOpenOrderAudit,
}: {
  summary?: DataRetentionDomainSummary;
  bindingCount: number;
  uniqueBotUids: number;
  robotUserCount: number;
  makerCount: number;
  flowCount: number;
  hedgeCount: number;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
}) {
  const topMarket = summary?.markets?.[0];
  const topSource = summary?.robot_trade_sources?.[0] ?? topMarket?.robot_trade_sources?.[0];
  const sourceRows = (
    summary?.robot_trade_sources?.length
      ? summary.robot_trade_sources
      : (summary?.markets ?? []).flatMap((market) => market.robot_trade_sources ?? [])
  ).slice(0, 6);
  const sourceSummaryLoaded = Boolean(summary);
  const sourceAuditTarget = (source: RobotTradeSourceSummary): OrderAuditTarget => ({
    product: auditProductFromProductType(source.product_type),
    symbol: source.symbol ?? null,
    dataScope: "robot",
    status: "all",
  });
  const rows: Array<{
    domain: string;
    tone: "customer" | "robot" | "system" | "market";
    current: string;
    policy: string;
    verification: string;
    boundary: string;
    auditTarget: OrderAuditTarget;
  }> = [
    {
      domain: "机器人 UID",
      tone: "robot",
      current: `${robotUserCount} 个 mm_bot 登录主体 · ${uniqueBotUids} 个绑定 UID · ${bindingCount} 个市场绑定`,
      policy: "机器人底层仍是 users 表特殊 UID，可复用 API key、费率、现货钱包或合约保证金接口。",
      verification: "机器人账号 / 账户与资金 / 合约清算",
      boundary: "特殊 UID 不等于客户业务主体；不要把机器人活跃度直接算作客户活跃度。",
      auditTarget: { dataScope: "robot", status: "all" },
    },
    {
      domain: "明细对账",
      tone: "robot",
      current: `旧订单候选 ${retentionValue(summary?.orders.robot_retention_eligible)} · robot-only 成交 ${retentionValue(summary?.trades.robot_only)} · 角色 ${makerCount}/${flowCount}/${hedgeCount}`,
      policy: "机器人订单和纯机器人成交主要用于运行复盘、资金快照、费用核对和策略排障。",
      verification: "订单与成交 data_domain=robot / 机器人运营日志 / 系统运行资源",
      boundary: "当前不清理、不归档、不重算费用；后续减少明细保留必须先设计汇总和抽样证据。",
      auditTarget: { dataScope: "robot", status: "all" },
    },
    {
      domain: "汇总候选",
      tone: "market",
      current: `最高压力 ${topMarket ? `${topMarket.symbol} ${retentionValue(topMarket.pressure_score)}` : "-"} · 主要来源 ${robotTradeSourceSummary(topSource)}`,
      policy: "优先按市场、source、时间桶、成交额、maker/taker fee 聚合，再评估是否短保留高频明细。",
      verification: "系统与审计 / 运行资源 / data_retention_domains",
      boundary: "这里只读展示候选方向，不创建汇总表、不裁剪历史、不改 real_ioc_sandbox 落库。",
      auditTarget: {
        product: auditProductFromProductType(topMarket?.product_type),
        symbol: topMarket?.symbol ?? null,
        dataScope: "robot",
        status: "all",
      },
    },
    {
      domain: "客户保护",
      tone: "customer",
      current: `客户旧订单 ${retentionValue(summary?.orders.customer_retention_eligible)} · 客户相关成交 ${retentionValue(summary?.trades.customer_involved)}`,
      policy: "任一侧涉及普通外部客户的订单、成交、费用和资金事实优先按客户业务记录完整核对。",
      verification: "账户与资金 / 订单与成交 data_domain=customer / 合约清算",
      boundary: "机器人留存、抽样或聚合策略不能覆盖客户记录、合约清算事实或管理员操作证据。",
      auditTarget: { dataScope: "customer", status: "all" },
    },
    {
      domain: "系统控制 / 未知",
      tone: "system",
      current: `旧订单 ${retentionValue(summary?.orders.system_retention_eligible)} · 成交 ${retentionValue(summary?.trades.system_or_unknown)}`,
      policy: "系统流动性、seed、fallback、contract_liq 或未知来源先作为控制证据核对。",
      verification: "系统与审计 / 操作记录 / 市场运营",
      boundary: "不能把系统记录误读为客户活跃，也不能直接当成机器人策略收益。",
      auditTarget: { dataScope: "system", status: "all" },
    },
  ];

  return (
    <div className="mt-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">机器人对账与留存口径</h3>
        <p className="mt-1 text-sm text-slate-500">把机器人特殊 UID、运行明细、汇总候选和客户记录保护放在同一张只读表里；这里不触发裁剪、归档、调账、实例控制或后端写入。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1360px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">口径</th>
              <th className="px-3 py-2 font-normal">当前摘要</th>
              <th className="px-3 py-2 font-normal">治理方向</th>
              <th className="px-3 py-2 font-normal">核对入口</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">审计入口</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 ${dataDomainToneClass(row.tone)}`}>{row.domain}</span>
                </td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.current}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.policy}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{row.verification}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => onOpenOrderAudit(row.auditTarget)}
                    className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                  >
                    去审计
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-4">
        <div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
          <h4 className="text-sm font-medium text-slate-200">source 压力明细</h4>
          <p className="text-xs text-slate-500">快速看出机器人高频成交来自 FLOW、MM、seed 还是未知来源；这里只定位压力，不做 source 级查询或汇总入账。</p>
        </div>
        <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
          <table className="min-w-[1180px] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="px-3 py-2 font-normal">source</th>
                <th className="px-3 py-2 font-normal">市场</th>
                <th className="px-3 py-2 font-normal">成交笔数</th>
                <th className="px-3 py-2 font-normal">quote 成交额</th>
                <th className="px-3 py-2 font-normal">手续费</th>
                <th className="px-3 py-2 font-normal">对账去向</th>
                <th className="px-3 py-2 font-normal">边界</th>
                <th className="px-3 py-2 text-right font-normal">审计入口</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {sourceRows.map((source) => (
                <tr key={`${source.symbol ?? "all"}-${source.product_type ?? "all"}-${source.source}`} className="border-t border-white/8 align-top">
                  <td className="px-3 py-2 font-mono text-cyan-100">{source.source || "unknown"}</td>
                  <td className="px-3 py-2">
                    <div className="font-mono text-slate-100">{source.symbol ?? "全部市场"}</div>
                    <div className="text-[11px] text-slate-500">{source.product_type ?? "-"}</div>
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-200">{retentionValue(source.trade_count)}</td>
                  <td className="px-3 py-2 font-mono text-slate-200">{fmt(source.quote_amount ?? "0", 2)}</td>
                  <td className="px-3 py-2 font-mono text-slate-300">maker {fmt(source.maker_fee ?? "0", 6)} · taker {fmt(source.taker_fee ?? "0", 6)}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">订单与成交 data_domain=robot / 机器人运营日志</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">按 source 看压力来源；明细仍回订单成交和账本核对，不在机器人账号页汇总入账。</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => onOpenOrderAudit(sourceAuditTarget(source))}
                      className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                    >
                      看 source 审计
                    </button>
                  </td>
                </tr>
              ))}
              {sourceRows.length === 0 && (
                <tr>
                  <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={8}>
                    {sourceSummaryLoaded ? "暂无 robot-only source 分布。" : "系统状态聚合加载中，source 压力明细稍后显示。"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      <p className="mt-2 text-xs text-slate-600">明细仍以订单、成交、现货流水、合约保证金流水和机器人日志为准；本表只是资源池入口的治理口径索引。</p>
    </div>
  );
}

function BotDomainBoundaryStrip({
  bindingCount,
  uniqueBotUids,
  robotUserCount,
  spotBindings,
  perpBindings,
  makerCount,
  flowCount,
  hedgeCount,
}: {
  bindingCount: number;
  uniqueBotUids: number;
  robotUserCount: number;
  spotBindings: number;
  perpBindings: number;
  makerCount: number;
  flowCount: number;
  hedgeCount: number;
}) {
  const rows = [
    {
      domain: "机器人身份",
      owner: "机器人账号",
      status: `${robotUserCount} 个 mm_bot 登录主体 · ${uniqueBotUids} 个市场绑定 UID`,
      boundary: "查看 API Key、启用状态和资源池身份；不在这里执行调账或清算。",
    },
    {
      domain: "市场绑定",
      owner: "市场运营",
      status: `${bindingCount} 个绑定 · SPOT ${spotBindings} / PERP ${perpBindings}`,
      boundary: "绑定某个机器人到市场和策略角色；具体编辑回到市场详情的机器人资源。",
    },
    {
      domain: "策略与实例",
      owner: "机器人运营",
      status: `maker ${makerCount} · flow ${flowCount} · hedge ${hedgeCount}`,
      boundary: "策略参数、启动、停止、重启、日志、heartbeat 和 guard 都归运行域。",
    },
    {
      domain: "资金快照",
      owner: "账户与资金 / 合约清算",
      status: "SPOT 看现货余额，PERP 看合约保证金",
      boundary: "这里只展示快照；现货入账/扣款去账户与资金，保证金、仓位和清算风险去合约清算。",
    },
  ];

  return (
    <div className="mt-4 overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
      <table className="min-w-[920px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="px-3 py-2 font-normal">机器人域</th>
            <th className="px-3 py-2 font-normal">主入口</th>
            <th className="px-3 py-2 font-normal">当前状态</th>
            <th className="px-3 py-2 font-normal">边界说明</th>
          </tr>
        </thead>
        <tbody className="text-slate-300">
          {rows.map((row) => (
            <tr key={row.domain} className="border-t border-white/8 align-top">
              <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
              <td className="px-3 py-2">{row.owner}</td>
              <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
              <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BotOperationsBoundaryStrip({
  market,
  bots,
  instance,
  strategyVersion,
}: {
  market?: AdminMarketItem;
  bots: MarketBotAccount[];
  instance?: MakerInstanceStatus;
  strategyVersion?: string;
}) {
  const enabledMakers = bots.filter((bot) => bot.is_enabled && bot.role === "maker").length;
  const enabledFlows = bots.filter((bot) => bot.is_enabled && bot.role === "flow").length;
  const blockerCount = instance?.start_readiness?.blockers?.length ?? 0;
  const warningCount = instance?.start_readiness?.warnings?.length ?? 0;
  const rows = [
    {
      domain: "策略参数",
      owner: "机器人运营",
      status: `${strategyDisplayName(strategyVersion)} · maker ${enabledMakers} / flow ${enabledFlows}`,
      boundary: "保存运行参数；SPOT 热生效，PERP 策略切换以实例重启后的运行策略为准。",
    },
    {
      domain: "实例控制",
      owner: "Bot Orchestrator",
      status: `${adminStatusText(instance?.status)} · pid ${instance?.pid ?? "-"}`,
      boundary: "启动、停止、停止并撤单、重启都只作用当前市场的做市实例。",
    },
    {
      domain: "日志与风险",
      owner: "机器人运营 / 风险监控",
      status: `阻断 ${blockerCount} · 警告 ${warningCount} · heartbeat ${instance?.heartbeat_status ?? "-"}`,
      boundary: "先看 heartbeat、pid identity、guard、队列和日志；跨域风险再进入风险监控。",
    },
    {
      domain: "资金/清算核对",
      owner: market?.product_type === "PERP" ? "合约清算" : "账户与资金",
      status: market?.product_type === "PERP" ? "PERP 保证金 / 仓位 / 强平风险" : "SPOT 现货余额 / 冻结 / 调账",
      boundary: "机器人运营只显示运行侧摘要，不执行现货调账、保证金处理、资金费、强平或 ADL。",
    },
  ];

  return (
    <section className="panel overflow-hidden rounded-2xl">
      <div className="border-b border-white/8 px-4 py-3">
        <h3 className="font-display text-lg">机器人运营边界</h3>
        <p className="mt-1 text-sm text-slate-400">运行域只处理策略、实例、日志和 guard；资金账本和清算动作回对应后台域。</p>
      </div>
      <div className="overflow-auto">
        <table className="min-w-[960px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-4 py-2 font-normal">运行域</th>
              <th className="px-4 py-2 font-normal">主入口</th>
              <th className="px-4 py-2 font-normal">当前状态</th>
              <th className="px-4 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-4 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-4 py-2">{row.owner}</td>
                <td className="px-4 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-4 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OverviewDashboard({
  users,
  marketSymbols,
  markets,
  marketSurveillance,
  marketBots,
  makerInstances,
  positions,
  marketStates,
  fundingJobs,
  liquidationEvents,
  adlEvents,
  insuranceEvents,
  maintenance,
  adminOperations,
  deploymentChecklist,
  systemStatus,
  onOpenSection,
  onOpenTarget,
  onOpenOrderAudit,
  onOpenRiskItem,
  onOpenMarket,
}: {
  users: AdminUser[];
  marketSymbols: string[];
  markets: Record<string, AdminMarketItem>;
  marketSurveillance: Record<string, MarketSurveillanceItem>;
  marketBots: Record<string, MarketBotAccount[]>;
  makerInstances: Record<string, MakerInstanceStatus>;
  positions: ContractPositionAdminItem[];
  marketStates: ContractPriceState[];
  fundingJobs: ContractFundingJob[];
  liquidationEvents: ContractLiquidationEventAdminItem[];
  adlEvents: ContractAdlEventAdminItem[];
  insuranceEvents: ContractInsuranceEventAdminItem[];
  maintenance: ContractMaintenanceStatus;
  adminOperations: AdminOperationAuditItem[];
  deploymentChecklist?: DeploymentChecklist;
  systemStatus?: SystemStatus;
  onOpenSection: (section: AdminSection) => void;
  onOpenTarget: (target: AdminBusinessTarget) => void;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
  onOpenRiskItem: (item: RiskQueueItem) => void;
  onOpenMarket: (symbol: string) => void;
}) {
  const activeMarkets = marketSymbols.filter((symbol) => markets[symbol]?.is_active).length;
  const runningInstances = marketSymbols.filter((symbol) => makerInstances[symbol]?.running).length;
  const enabledBots = marketSymbols.reduce((sum, symbol) => sum + (marketBots[symbol] ?? []).filter((bot) => bot.is_enabled).length, 0);
  const enabledMakers = marketSymbols.reduce((sum, symbol) => sum + (marketBots[symbol] ?? []).filter((bot) => bot.is_enabled && bot.role === "maker").length, 0);
  const enabledFlows = marketSymbols.reduce((sum, symbol) => sum + (marketBots[symbol] ?? []).filter((bot) => bot.is_enabled && bot.role === "flow").length, 0);
  const perpMarketCount = marketSymbols.filter((symbol) => markets[symbol]?.product_type === "PERP").length;
  const spotMarketCount = marketSymbols.filter((symbol) => markets[symbol]?.product_type === "SPOT").length;
  const activePositions = positions.filter((item) => item.position.side !== "flat" && Number(item.position.quantity || 0) > 0);
  const accountKindCounts = users.reduce<Record<AccountUserKind, number>>((current, user) => {
    const kind = accountUserKindForUser(user);
    current[kind] += 1;
    return current;
  }, { customer: 0, admin: 0, spot_robot: 0, contract_robot: 0, system: 0 });
  const customerBusinessAccountCount = accountKindCounts.customer;
  const robotAccountCount = accountKindCounts.spot_robot + accountKindCounts.contract_robot;
  const systemControlAccountCount = accountKindCounts.admin + accountKindCounts.system;
  const failedFundingJobs = fundingJobs.filter((item) => item.status === "failed").length;
  const failedOperations = adminOperations.filter(isFailedAdminOperation).length;
  const adminHealthStatus = deploymentChecklist?.status ?? systemStatus?.status ?? "loading";
  const riskSnapshot = buildRiskQueueSnapshot({
    deploymentChecklist,
    systemStatus,
    marketSymbols,
    marketSurveillance,
    makerInstances,
    positions,
    marketStates,
    fundingJobs,
    liquidationEvents,
    adlEvents,
    insuranceEvents,
    maintenance,
    adminOperations,
  });
  const topRiskQueue = riskSnapshot.sortedQueue.slice(0, 5);
  const rows = marketSymbols
    .map((symbol) => ({
      symbol,
      market: markets[symbol],
      surveillance: marketSurveillance[symbol],
      instance: makerInstances[symbol],
      bots: marketBots[symbol] ?? [],
    }))
    .sort((left, right) => {
      const leftRank = left.instance?.running ? 0 : left.surveillance?.status === "critical" ? 1 : 2;
      const rightRank = right.instance?.running ? 0 : right.surveillance?.status === "critical" ? 1 : 2;
      return leftRank - rightRank || left.symbol.localeCompare(right.symbol);
    });

  return (
    <div className="space-y-4">
      <details className="panel rounded-xl p-4"><summary className="cursor-pointer text-sm text-slate-300">更多操作指引</summary>
      <TestAdminQuickPaths
        marketCount={marketSymbols.length}
        activeMarketCount={activeMarkets}
        userCount={users.length}
        customerBusinessAccountCount={customerBusinessAccountCount}
        robotAccountCount={robotAccountCount}
        enabledBotCount={enabledBots}
        runningInstanceCount={runningInstances}
        activePositionCount={riskSnapshot.activePositions.length}
        stressedPositionCount={riskSnapshot.stressedPositions.length}
        failedFundingJobCount={failedFundingJobs}
        riskQueueCount={riskSnapshot.sortedQueue.length}
        criticalRiskCount={riskSnapshot.criticalCount}
        failedOperationCount={failedOperations}
        orderCount={systemStatus?.counts.orders}
        tradeCount={systemStatus?.counts.trades}
        systemStatus={adminHealthStatus}
        onOpenTarget={onOpenTarget}
      />
      </details>
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-2xl border border-white/8 bg-slate-950/25 px-4 py-3 text-sm text-slate-200 transition hover:border-cyan-300/25 hover:bg-cyan-400/6">
          <span className="font-display text-base">后台域与数据边界</span>
          <span className="text-xs text-slate-500 group-open:hidden">展开</span>
          <span className="hidden text-xs text-slate-500 group-open:inline">收起</span>
        </summary>
        <div className="mt-4 space-y-4">
          <AdminDomainMap
            marketCount={marketSymbols.length}
            activeMarketCount={activeMarkets}
            spotMarketCount={spotMarketCount}
            perpMarketCount={perpMarketCount}
            userCount={users.length}
            enabledBotCount={enabledBots}
            runningInstanceCount={runningInstances}
            activePositionCount={activePositions.length}
            failedFundingJobCount={failedFundingJobs}
            riskQueueCount={riskSnapshot.sortedQueue.length}
            criticalRiskCount={riskSnapshot.criticalCount}
            systemStatus={adminHealthStatus}
            failedOperationCount={failedOperations}
            onOpenSection={onOpenSection}
          />
          <DataDomainBoundaryStrip
            customerBusinessAccountCount={customerBusinessAccountCount}
            robotAccountCount={robotAccountCount}
            systemAccountCount={systemControlAccountCount}
            orderCount={systemStatus?.counts.orders}
            tradeCount={systemStatus?.counts.trades}
            activeMarketCount={activeMarkets}
            marketCount={marketSymbols.length}
            bookMarketCount={systemStatus?.counts.book_markets}
            runningInstanceCount={runningInstances}
            enabledBotCount={enabledBots}
            failedOperationCount={failedOperations}
            systemStatus={adminHealthStatus}
            onOpenTarget={onOpenTarget}
            onOpenOrderAudit={onOpenOrderAudit}
          />
        </div>
      </details>
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-display text-xl">运营待处理</h2>
            <p className="mt-1 text-sm text-slate-500">首屏聚合部署、系统、市场、机器人、合约风险和后台操作失败；执行动作仍回对应业务域。</p>
          </div>
          <button type="button" onClick={() => onOpenSection("risk")} className="rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100">
            查看完整风险监控
          </button>
        </div>
        <div className="mb-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
          <SurveillanceMetric label="待处理" value={String(riskSnapshot.sortedQueue.length)} />
          <SurveillanceMetric label="高优先级" value={String(riskSnapshot.criticalCount)} />
          <SurveillanceMetric label="需关注" value={String(riskSnapshot.warnCount)} />
          <SurveillanceMetric label="风险仓位" value={`${riskSnapshot.stressedPositions.length} / ${riskSnapshot.activePositions.length}`} />
          <SurveillanceMetric label="价格源异常" value={String(riskSnapshot.sourceIssues.length)} />
          <SurveillanceMetric label="系统一致性" value={`${riskSnapshot.invariantDiffCount} / ${riskSnapshot.versionAlertCount}`} />
        </div>
        {topRiskQueue.length === 0 ? (
          <div className="rounded-2xl border border-emerald-400/12 bg-emerald-400/6 px-4 py-5 text-sm text-emerald-50/90">当前没有需要立即处理的运营事项。</div>
        ) : (
          <RiskQueueTable items={topRiskQueue} onOpenItem={onOpenRiskItem} />
        )}
      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
	          <div>
	            <h2 className="font-display text-xl">运营总览</h2>
	            <p className="mt-1 text-sm text-slate-500">按币对汇总市场、登录主体、机器人策略、实例和风险状态；常用改参入口在市场详情里。</p>
	          </div>
	          <span className="rounded-lg bg-white/8 px-3 py-1 text-xs text-slate-300">
	            {deploymentChecklist?.status ?? systemStatus?.status ?? "loading"}
	          </span>
	        </div>
	        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
	          <SurveillanceMetric label="运行实例" value={`${runningInstances} / ${marketSymbols.length}`} />
	          <SurveillanceMetric label="启用市场" value={`${activeMarkets} / ${marketSymbols.length}`} />
	          <SurveillanceMetric label="启用机器人" value={String(enabledBots)} />
	          <SurveillanceMetric label="Maker / Flow" value={`${enabledMakers} / ${enabledFlows}`} />
	          <SurveillanceMetric label="登录主体" value={String(users.length)} />
	          <SurveillanceMetric label="当前委托" value={String(systemStatus?.counts.open_orders ?? "-")} />
	        </div>
	        <p className="mt-3 text-xs leading-5 text-slate-500">
	          登录主体包含客户、机器人、管理和系统主体；当前委托是全市场未完成订单口径，不代表系统主体挂单。
	        </p>
	      </section>
      <section className="panel overflow-hidden rounded-2xl">
        <div className="grid grid-cols-[1fr_0.75fr_0.8fr_0.8fr_1fr_0.6fr] gap-2 border-b border-white/8 bg-white/5 px-4 py-3 text-xs uppercase tracking-[0.14em] text-slate-500">
          <span>币对</span>
          <span>实例</span>
          <span>机器人</span>
          <span>Spread</span>
          <span>24H 成交额</span>
          <span className="text-right">操作</span>
        </div>
        <div className="divide-y divide-white/8">
          {rows.map(({ symbol, market, surveillance, instance, bots }) => {
            const makers = bots.filter((bot) => bot.is_enabled && bot.role === "maker").length;
            const flows = bots.filter((bot) => bot.is_enabled && bot.role === "flow").length;
            return (
              <div key={symbol} className="grid grid-cols-[1fr_0.75fr_0.8fr_0.8fr_1fr_0.6fr] items-center gap-2 px-4 py-3 text-sm">
                <div className="min-w-0">
                  <div className="font-mono text-white">{symbol}</div>
                  <div className="mt-1 truncate text-xs text-slate-500">{market?.market_type ?? "-"} · {market?.is_active ? "active" : "paused"}</div>
                </div>
                <span className={`w-fit rounded-lg px-2.5 py-1 text-xs ${adminStatusClass(instance?.status)}`}>{adminStatusText(instance?.status)}</span>
                <span className="font-mono text-slate-200">{makers} maker · {flows} flow</span>
                <span className="font-mono text-slate-300">{surveillance ? `${fmt(surveillance.metrics.spread_pct, 4)}%` : "-"}</span>
                <span className="font-mono text-slate-300">{surveillance ? fmt(surveillance.metrics.quote_volume_24h, 0) : "-"}</span>
                <div className="text-right">
                  <button type="button" onClick={() => onOpenMarket(symbol)} className="rounded-lg bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100 hover:bg-cyan-400/20">
                    管理
                  </button>
                </div>
              </div>
            );
          })}
          {rows.length === 0 && <div className="px-4 py-8 text-center text-sm text-slate-500">暂无市场。</div>}
        </div>
      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
	          <h3 className="font-display text-lg">当前推荐机器人运行形态</h3>
	          <span className="rounded-lg bg-emerald-400/12 px-3 py-1 text-xs text-emerald-100">1 maker + 1 flow</span>
	        </div>
	        <div className="grid gap-2 md:grid-cols-3">
	          <SurveillanceMetric label="Maker 机器人" value="被动挂单 / 维护深度" />
	          <SurveillanceMetric label="Flow 机器人" value="主动成交 / 生成 K 线" />
	          <SurveillanceMetric label="策略选择" value="Lite 精简做市（当前唯一现货策略）" />
	        </div>
	        <p className="mt-3 text-xs leading-5 text-slate-500">
	          Maker / Flow 是 mm_bot 测试 UID 的运行角色；客户 UID 仍回账户与资金和订单与成交按客户记录核对。
	        </p>
	      </section>
    </div>
  );
}

function TestAdminQuickPaths({
  marketCount,
  activeMarketCount,
  userCount,
  customerBusinessAccountCount,
  robotAccountCount,
  enabledBotCount,
  runningInstanceCount,
  activePositionCount,
  stressedPositionCount,
  failedFundingJobCount,
  riskQueueCount,
  criticalRiskCount,
  failedOperationCount,
  orderCount,
  tradeCount,
  systemStatus,
  onOpenTarget,
}: {
  marketCount: number;
  activeMarketCount: number;
  userCount: number;
  customerBusinessAccountCount: number;
  robotAccountCount: number;
  enabledBotCount: number;
  runningInstanceCount: number;
  activePositionCount: number;
  stressedPositionCount: number;
  failedFundingJobCount: number;
  riskQueueCount: number;
  criticalRiskCount: number;
  failedOperationCount: number;
  orderCount?: number;
  tradeCount?: number;
  systemStatus: string;
  onOpenTarget: (target: AdminBusinessTarget) => void;
}) {
  const systemOk = systemStatus === "ok" || systemStatus === "healthy";
  const systemStatusLabel = systemOk ? "系统正常" : systemStatus === "loading" ? "加载中" : `系统 ${systemStatus}`;
  const marketTone = runningInstanceCount === 0 || activeMarketCount === 0 ? "warn" : "ok";
  const rows: Array<{
    title: string;
    cue: string;
    status: string;
    tone: "ok" | "warn" | "danger" | "neutral";
    target: AdminBusinessTarget;
    actionLabel: string;
  }> = [
    {
      title: "先看系统能不能用",
      cue: "部署、进程、数据库、失败操作。",
      status: `${systemStatusLabel} · 待处理 ${riskQueueCount} · 失败 ${failedOperationCount}`,
      tone: failedOperationCount > 0 || criticalRiskCount > 0 || !systemOk ? "warn" : "ok",
      target: { section: "system", systemTab: "overview" },
      actionLabel: "去系统与审计",
    },
    {
      title: "看市场 / 机器人是否在跑",
      cue: "市场状态、实例、日志、guard。",
      status: `实例 ${runningInstanceCount}/${marketCount} · 市场 ${activeMarketCount}/${marketCount}`,
      tone: marketTone,
      target: { section: "maker_config" },
      actionLabel: "去铺单策略",
    },
    {
      title: "开测试账户 / 入账 / 费率",
      cue: "账户目录、现货入账、UID 费率。",
      status: `主体 ${userCount} · 客户 ${customerBusinessAccountCount}`,
      tone: userCount > 0 ? "ok" : "warn",
      target: { section: "accounts", accountScope: "customer" },
      actionLabel: "去账户与资金",
    },
    {
      title: "查订单 / 成交 / 手续费",
      cue: "产品、市场、UID、状态、数据域。",
      status: `订单 ${dataDomainCount(orderCount)} · 成交 ${dataDomainCount(tradeCount)}`,
      tone: "neutral",
      target: { section: "orders" },
      actionLabel: "去订单与成交",
    },
    {
      title: "看合约风险 / 资金费",
      cue: "保证金、仓位、资金费、强平 / ADL。",
      status: `风险仓位 ${stressedPositionCount}/${activePositionCount} · 失败资金费 ${failedFundingJobCount}`,
      tone: failedFundingJobCount > 0 || stressedPositionCount > 0 || criticalRiskCount > 0 ? "warn" : "ok",
      target: { section: "contracts", contractTab: "overview" },
      actionLabel: "去合约清算",
    },
    {
      title: "查机器人 UID / 对账边界",
      cue: "绑定、API Key、资金快照、对账入口。",
      status: `机器人账号 ${robotAccountCount} · 启用 ${enabledBotCount}`,
      tone: robotAccountCount > 0 ? "neutral" : "warn",
      target: { section: "bots" },
      actionLabel: "去机器人账号",
    },
  ];
  const toneClass = (tone: "ok" | "warn" | "danger" | "neutral") => {
    if (tone === "ok") return "bg-emerald-400/14 text-emerald-100";
    if (tone === "warn") return "bg-amber-400/14 text-amber-100";
    if (tone === "danger") return "bg-rose-500/14 text-rose-100";
    return "bg-white/8 text-slate-300";
  };

  return (
    <section className="panel rounded-2xl p-4">
	      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
	        <div>
	          <h2 className="font-display text-xl">测试后台快速路径</h2>
	          <p className="mt-1 text-sm text-slate-500">按常见测试场景直接进入对应后台域；保持单控制台、短路径和现有确认边界。</p>
	        </div>
	        <span className="rounded-lg bg-emerald-400/12 px-3 py-1 text-xs text-emerald-100">轻量测试模式</span>
	      </div>
	      <div className="mb-3 grid gap-2 text-xs text-slate-400 md:grid-cols-3">
	        <div className="rounded-xl border border-emerald-400/12 bg-emerald-400/6 px-3 py-2">
	          <span className="text-emerald-100">测试沙盒</span>
	          <span className="ml-2 text-slate-500">本机演示、策略验证和后台演练，不是公开交易所生产系统。</span>
	        </div>
	        <div className="rounded-xl border border-cyan-400/12 bg-cyan-400/6 px-3 py-2">
	          <span className="text-cyan-100">资金边界</span>
	          <span className="ml-2 text-slate-500">现货入账/扣款只服务测试资金，合约保证金仍是独立账本。</span>
	        </div>
	        <div className="rounded-xl border border-amber-400/12 bg-amber-400/6 px-3 py-2">
	          <span className="text-amber-100">生产化能力</span>
	          <span className="ml-2 text-slate-500">权限、审批、不可变审计和日终清算包留作后续专题。</span>
	        </div>
	      </div>
	      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
	        {rows.map((row) => (
          <button
            key={row.title}
            type="button"
            onClick={() => onOpenTarget(row.target)}
            className="min-h-[112px] rounded-2xl border border-white/8 bg-slate-950/25 p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-400/8"
          >
            <div className="font-medium text-slate-100">{row.title}</div>
            <div className={`mt-2 w-fit rounded-full px-2.5 py-1 text-[11px] ${toneClass(row.tone)}`}>{row.status}</div>
            <div className="mt-2 min-h-[24px] text-xs leading-5 text-slate-500">{row.cue}</div>
            <div className="mt-3 text-xs text-cyan-100">{row.actionLabel}</div>
          </button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/8 pt-3 text-xs">
        <span className="mr-1 text-slate-500">交易与流动性观察</span>
        <a href={appBasePath + "/spot/trade/BTCUSDT"} className="rounded-xl bg-white/8 px-3 py-1.5 text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
          现货交易
        </a>
        <a href={appBasePath + "/spot/orderbook/BTCUSDT"} className="rounded-xl bg-white/8 px-3 py-1.5 text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
          现货流动性
        </a>
        <a href={appBasePath + "/contracts/trade/BTCUSDT-PERP"} className="rounded-xl bg-white/8 px-3 py-1.5 text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
          合约交易
        </a>
        <a href={appBasePath + "/contracts/orderbook/BTCUSDT-PERP"} className="rounded-xl bg-white/8 px-3 py-1.5 text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
          合约流动性
        </a>
      </div>
    </section>
  );
}

function AdminDomainMap({
  marketCount,
  activeMarketCount,
  spotMarketCount,
  perpMarketCount,
  userCount,
  enabledBotCount,
  runningInstanceCount,
  activePositionCount,
  failedFundingJobCount,
  riskQueueCount,
  criticalRiskCount,
  systemStatus,
  failedOperationCount,
  onOpenSection,
}: {
  marketCount: number;
  activeMarketCount: number;
  spotMarketCount: number;
  perpMarketCount: number;
  userCount: number;
  enabledBotCount: number;
  runningInstanceCount: number;
  activePositionCount: number;
  failedFundingJobCount: number;
  riskQueueCount: number;
  criticalRiskCount: number;
  systemStatus: string;
  failedOperationCount: number;
  onOpenSection: (section: AdminSection) => void;
}) {
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    boundary: string;
    section: AdminSection;
  }> = [
    {
      domain: "市场运营",
      owner: "市场运营",
      status: `市场 ${activeMarketCount}/${marketCount} · SPOT ${spotMarketCount} · PERP ${perpMarketCount}`,
      boundary: "管理市场身份、交易规则、产品参数和市场级机器人资源；不直接做账户调账或清算处置。",
      section: "markets",
    },
    {
      domain: "账户与资金",
      owner: "账户与资金",
      status: `主体 ${userCount}`,
      boundary: "管理登录主体、API Key、费率和现货钱包；合约保证金只是索引，账本仍回合约清算。",
      section: "accounts",
    },
    {
      domain: "订单与成交",
      owner: "订单审计",
      status: "SPOT/PERP 统一审计",
      boundary: "跨产品查单、查成交和导出当前结果；不执行撤单、清算、调账或实例控制。",
      section: "orders",
    },
    {
      domain: "合约清算",
      owner: "合约清算",
      status: `活跃持仓 ${activePositionCount} · 失败资金费 ${failedFundingJobCount}`,
      boundary: "处理保证金、仓位、资金费、强平、ADL、保险基金和风险参数；不合并现货钱包。",
      section: "contracts",
    },
    {
      domain: "风险监控",
      owner: "风险监控",
      status: `待处理 ${riskQueueCount} · 高优先级 ${criticalRiskCount}`,
      boundary: "值班分流台，聚合仓位、清算、市场、机器人、系统和操作失败；真正处置回业务域。",
      section: "risk",
    },
    {
      domain: "机器人运营",
      owner: "Bot Orchestrator",
      status: `运行实例 ${runningInstanceCount} · 启用机器人 ${enabledBotCount}`,
      boundary: "处理策略参数、实例启动停止、日志和 guard；资金、保证金和清算风险回账户或合约域。",
      section: "maker_config",
    },
    {
      domain: "机器人账号",
      owner: "机器人资源池",
      status: `启用机器人 ${enabledBotCount}`,
      boundary: "管理机器人身份、绑定、API Key 和资金快照；实例运行回机器人运营，调账回账户与资金。",
      section: "bots",
    },
    {
      domain: "系统与审计",
      owner: "系统与审计",
      status: `${systemStatus} · 失败操作 ${failedOperationCount}`,
      boundary: "运维观察、部署检查、危险操作目录和操作记录回看；不作为清理、重建或调账快捷执行台。",
      section: "system",
    },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3">
        <h2 className="font-display text-xl">后台运营域地图</h2>
        <p className="mt-1 text-sm text-slate-500">一级目录按交易所后台职责分区；这里是只读导航，不新增执行入口，也不改变任何资金或清算 source-of-truth。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1120px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">后台域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <button type="button" onClick={() => onOpenSection(row.section)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
                    打开
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function dataDomainToneClass(tone: "customer" | "robot" | "system" | "market") {
  if (tone === "customer") return "bg-emerald-400/14 text-emerald-100";
  if (tone === "robot") return "bg-cyan-400/14 text-cyan-100";
  if (tone === "system") return "bg-amber-400/14 text-amber-100";
  return "bg-violet-400/14 text-violet-100";
}

function dataDomainCount(value?: number) {
  return typeof value === "number" ? value.toLocaleString("en-US") : "-";
}

function DataDomainBoundaryStrip({
  customerBusinessAccountCount,
  robotAccountCount,
  systemAccountCount,
  orderCount,
  tradeCount,
  activeMarketCount,
  marketCount,
  bookMarketCount,
  runningInstanceCount,
  enabledBotCount,
  failedOperationCount,
  systemStatus,
  onOpenTarget,
  onOpenOrderAudit,
}: {
  customerBusinessAccountCount: number;
  robotAccountCount: number;
  systemAccountCount: number;
  orderCount?: number;
  tradeCount?: number;
  activeMarketCount: number;
  marketCount: number;
  bookMarketCount?: number;
  runningInstanceCount: number;
  enabledBotCount: number;
  failedOperationCount: number;
  systemStatus: string;
  onOpenTarget: (target: AdminBusinessTarget) => void;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
}) {
  const rows: Array<{
    domain: string;
    tone: "customer" | "robot" | "system" | "market";
    status: string;
    owners: string;
    retention: string;
    boundary: string;
    actions: { label: string; onClick: () => void }[];
  }> = [
    {
      domain: "客户业务数据",
      tone: "customer",
      status: `客户 ${dataDomainCount(customerBusinessAccountCount)} · 订单 ${dataDomainCount(orderCount)} · 成交 ${dataDomainCount(tradeCount)}`,
      owners: "账户与资金 / 订单与成交 / 合约清算",
      retention: "普通客户订单、成交、现货流水、合约流水、仓位、资金费和强平记录按业务事实完整保留。",
      boundary: "客户侧记录不能被机器人成交压缩策略影响；费用、PnL、保证金和余额仍回对应账本核对。",
      actions: [
        { label: "客户 UID", onClick: () => onOpenTarget({ section: "accounts", accountScope: "customer" }) },
        { label: "客户审计", onClick: () => onOpenOrderAudit({ dataScope: "customer", status: "all" }) },
      ],
    },
    {
      domain: "机器人运行数据",
      tone: "robot",
      status: `机器人 UID ${dataDomainCount(robotAccountCount)} · 启用 ${dataDomainCount(enabledBotCount)} · 运行 ${dataDomainCount(runningInstanceCount)}`,
      owners: "机器人运营 / 机器人账号",
      retention: "策略、实例、日志、guard、库存快照和纯机器人高频细节优先聚合、抽样或短期留存。",
      boundary: "机器人是特殊 UID 身份，但不等同普通外部客户活跃度；资金核对回账户与资金或合约清算。",
      actions: [
        { label: "机器人账号", onClick: () => onOpenTarget({ section: "bots" }) },
        { label: "机器人对账", onClick: () => onOpenOrderAudit({ dataScope: "robot", status: "all" }) },
      ],
    },
    {
      domain: "系统控制 / 审计数据",
      tone: "system",
      status: `${systemStatus} · 管理主体 + 系统主体 ${dataDomainCount(systemAccountCount)} · 失败操作 ${dataDomainCount(failedOperationCount)}`,
      owners: "系统与审计 / 风险监控",
      retention: "后台操作、危险确认、历史保留、部署健康、价格源和 orderbook invariants 作为运维证据保留。",
      boundary: "系统控制数据不混入普通客户业务记录；危险动作仍回业务页确认和操作记录回看。",
      actions: [
        { label: "操作审计", onClick: () => onOpenTarget({ section: "system", systemTab: "audit" }) },
        { label: "系统控制审计", onClick: () => onOpenOrderAudit({ dataScope: "system", status: "all" }) },
      ],
    },
    {
      domain: "市场公共数据",
      tone: "market",
      status: `市场 ${dataDomainCount(activeMarketCount)}/${dataDomainCount(marketCount)} · 盘口市场 ${dataDomainCount(bookMarketCount)} · 公开成交 ${dataDomainCount(tradeCount)}`,
      owners: "市场运营 / 交易页 / 订单与成交",
      retention: "K 线、ticker、公开成交、订单簿和 source 分布服务行情展示、交易体验和来源解释。",
      boundary: "公开行情不替代客户账本或清算报表；后台长表不能拖慢交易页、撮合和机器人运行。",
      actions: [
        { label: "市场运营", onClick: () => onOpenTarget({ section: "markets" }) },
        { label: "订单成交", onClick: () => onOpenOrderAudit({ status: "all" }) },
      ],
    },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3">
        <h2 className="font-display text-xl">后台数据域边界</h2>
        <p className="mt-1 text-sm text-slate-500">先区分客户业务事实、机器人运行材料、系统控制证据和市场公共数据；这里是只读口径，不触发清理历史、调账、重算费用或改变撮合账本。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1180px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">数据域</th>
              <th className="px-3 py-2 font-normal">当前摘要</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">保留原则</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 ${dataDomainToneClass(row.tone)}`}>{row.domain}</span>
                </td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{row.owners}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.retention}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <div className="flex flex-wrap justify-end gap-2">
                    {row.actions.map((action) => (
                      <button key={action.label} type="button" onClick={action.onClick} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
                        {action.label}
                      </button>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AdminEntryButton({
  title,
  subtitle,
  tone,
  onClick,
}: {
  title: string;
  subtitle: string;
  tone: "cyan" | "emerald" | "amber" | "violet" | "slate";
  onClick: () => void;
}) {
  const toneClass =
    tone === "cyan"
      ? "border-cyan-400/16 bg-cyan-400/6 hover:bg-cyan-400/12"
      : tone === "emerald"
        ? "border-emerald-400/16 bg-emerald-400/6 hover:bg-emerald-400/12"
        : tone === "amber"
          ? "border-amber-400/16 bg-amber-400/6 hover:bg-amber-400/12"
          : tone === "violet"
            ? "border-violet-400/16 bg-violet-400/6 hover:bg-violet-400/12"
            : "border-white/10 bg-white/5 hover:bg-white/8";
  return (
    <button type="button" onClick={onClick} className={`min-h-[82px] rounded-2xl border px-4 py-3 text-left transition ${toneClass}`}>
      <div className="text-base font-semibold text-white">{title}</div>
      <div className="mt-1 text-xs text-slate-400">{subtitle}</div>
    </button>
  );
}

function ContractClearingQuickPaths({
  accountsCount,
  activePositionsCount,
  riskAlertCount,
  openOrdersCount,
  fundingJobCount,
  failedFundingJobCount,
  liquidationCount,
  totalAdlResidual,
  totalBadDebt,
  riskTierCount,
  sourceIssueCount,
  maintenanceErrorCount,
  onTabChange,
}: {
  accountsCount: number;
  activePositionsCount: number;
  riskAlertCount: number;
  openOrdersCount: number;
  fundingJobCount: number;
  failedFundingJobCount: number;
  liquidationCount: number;
  totalAdlResidual: number;
  totalBadDebt: number;
  riskTierCount: number;
  sourceIssueCount: number;
  maintenanceErrorCount: number;
  onTabChange: (tab: ContractAdminTab) => void;
}) {
  const hasAdlResidual = totalAdlResidual > 0;
  const hasBadDebt = totalBadDebt > 0;
  const blockingCount = maintenanceErrorCount + failedFundingJobCount + (hasAdlResidual ? 1 : 0);
  const attentionCount = sourceIssueCount + riskAlertCount + (hasBadDebt ? 1 : 0);
  const judgment = blockingCount > 0 ? "需处理" : attentionCount > 0 ? "需关注" : "正常";
  const judgmentTone: "neutral" | "warn" | "danger" = blockingCount > 0 ? "danger" : attentionCount > 0 ? "warn" : "neutral";
  const focusText =
    maintenanceErrorCount > 0
      ? "先看自动维护和资金费任务"
      : failedFundingJobCount > 0
        ? "先处理资金费任务"
        : hasAdlResidual
          ? "先复核强平 / ADL"
          : riskAlertCount > 0
            ? "先看仓位风险"
            : sourceIssueCount > 0
              ? "先看下方市场状态控制台"
              : hasBadDebt
                ? "先核对保险基金"
                : "按保证金、仓位、资金费例行抽查";
  const nextText =
    blockingCount > 0
      ? "进入对应 tab 核对错误和流水，写入动作仍走原确认边界。"
      : attentionCount > 0
        ? "先定位异常来源，必要时再回风险监控或系统审计。"
        : "无需处置时只做抽样核对，不在总览执行清算动作。";
  const rows: Array<{
    title: string;
    cue: string;
    status: string;
    tab: ContractAdminTab;
    tone: "neutral" | "warn" | "danger";
  }> = [
    {
      title: "核对保证金账户",
      cue: "看钱包、可用保证金、占用保证金和最近合约流水。",
      status: accountsCount > 0 ? `账户 ${accountsCount}` : "暂无账户",
      tab: "accounts",
      tone: accountsCount > 0 ? "neutral" : "warn",
    },
    {
      title: "查看仓位风险",
      cue: "先看持仓、强平距离、保证金缓冲和风险阶梯。",
      status: `风险告警 ${riskAlertCount} / 活跃 ${activePositionsCount}`,
      tab: "positions",
      tone: riskAlertCount > 0 ? "warn" : "neutral",
    },
    {
      title: "处理资金费任务",
      cue: "核对失败任务、结算水位和逐账户资金费记录。",
      status: `失败 ${failedFundingJobCount} / 任务 ${fundingJobCount}`,
      tab: "funding",
      tone: failedFundingJobCount > 0 ? "danger" : "neutral",
    },
    {
      title: "复核强平 / ADL",
      cue: "看强平事件、残余坏账、ADL 候选和执行结果。",
      status: `ADL剩余 ${fmt(totalAdlResidual, 2)} / 强平 ${liquidationCount}`,
      tab: "liquidation",
      tone: hasAdlResidual ? "danger" : liquidationCount > 0 ? "warn" : "neutral",
    },
    {
      title: "核对保险基金",
      cue: "看基金余额、坏账覆盖和人工调整流水。",
      status: `坏账 ${fmt(totalBadDebt, 2)}`,
      tab: "insurance",
      tone: hasBadDebt ? "warn" : "neutral",
    },
    {
      title: "查看风险参数",
      cue: "只读核对交易模式、杠杆、维持保证金、风险阶梯和委托影响。",
      status: `阶梯 ${riskTierCount} · 当前委托 ${openOrdersCount}`,
      tab: "risk",
      tone: "neutral",
    },
  ];

  const toneClass = (tone: "neutral" | "warn" | "danger") => {
    if (tone === "danger") return "border-rose-400/20 bg-rose-500/8 text-rose-100";
    if (tone === "warn") return "border-amber-400/20 bg-amber-400/8 text-amber-100";
    return "border-white/8 bg-slate-950/25 text-slate-100";
  };

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h3 className="font-display text-lg">清算快速处置路径</h3>
          <p className="mt-1 text-sm text-slate-400">按合约后台常见排查动作进入现有 tab；这里只有导航，不执行资金费、强平、ADL、调账或参数保存。</p>
        </div>
        <span className={`w-fit rounded-full px-3 py-1 text-xs ${judgmentTone === "danger" ? "bg-rose-500/16 text-rose-100" : judgmentTone === "warn" ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
          {judgment}
        </span>
      </div>
      <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
          <div className="mt-1 text-sm text-slate-200">{judgment} · 待处理 {blockingCount} · 关注 {attentionCount}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
          <div className="mt-1 text-sm text-slate-200">{focusText}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
          <div className="mt-1 text-sm text-slate-400">{nextText}</div>
        </div>
      </div>
	      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => (
          <button
            key={row.title}
            type="button"
            onClick={() => onTabChange(row.tab)}
            className={`min-h-[112px] rounded-xl border p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-400/8 ${toneClass(row.tone)}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{row.title}</span>
              <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-200">{row.status}</span>
            </div>
            <div className="mt-2 text-xs leading-5 text-slate-500">{row.cue}</div>
          </button>
        ))}
      </div>
    </section>
  );
}

function ContractMarketStateConsole({
  marketStates,
  onRefresh,
  onRefreshMarketState,
  onSettleFunding,
  showFundingSettlementAction = false,
}: {
  marketStates: ContractPriceState[];
  onRefresh: () => void;
  onRefreshMarketState: (symbol: string) => void;
  onSettleFunding: (symbol: string) => void;
  showFundingSettlementAction?: boolean;
}) {
  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="font-display text-lg">合约市场状态控制台</h3>
          <p className="mt-1 text-sm text-slate-400">PERP 风控数据面：指数价、标记价、外部标记价、溢价指数、资金费率和数据源状态。</p>
        </div>
        <button type="button" onClick={onRefresh} className="w-fit rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100">
          重新加载
        </button>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        {marketStates.map((state) => {
          const fundingPct = Number(state.funding_rate) * 100;
          const premiumPct = Number(state.premium_index) * 100;
          const sourceOk = state.source_status === "ok" || state.source_status === "external_ok";
          return (
            <div key={state.symbol} className="rounded-2xl border border-white/8 bg-white/5 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="font-mono text-slate-100">{state.symbol}</div>
                  <div className="mt-1 text-xs text-slate-500">
                    index {state.index_symbol} · {state.index_source} · funding {state.funding_rate_mode}
                  </div>
                </div>
                <span className={`rounded-full px-2.5 py-1 text-xs ${sourceOk ? "bg-emerald-400/14 text-emerald-100" : "bg-amber-400/14 text-amber-100"}`}>
                  {state.source_status}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <SurveillanceMetric label="指数价" value={fmt(state.index_price, 4)} />
                <SurveillanceMetric label="标记价" value={fmt(state.mark_price, 4)} />
                <SurveillanceMetric label="外部标记价" value={fmt(state.external_mark_price, 4)} />
                <SurveillanceMetric label="溢价指数" value={`${fmt(premiumPct, 4)}%`} />
                <SurveillanceMetric label="资金费率" value={`${fmt(fundingPct, 4)}%`} />
                <SurveillanceMetric label="资金费模式" value={state.funding_rate_mode} />
                <SurveillanceMetric label="Impact Bid/Ask" value={`${fmt(state.impact_bid_price, 4)} / ${fmt(state.impact_ask_price, 4)}`} />
                <SurveillanceMetric label="下次资金费" value={bjDateTime(state.next_funding_time)} />
                <SurveillanceMetric label="更新时间" value={bjTime(state.updated_at)} />
              </div>
              {state.source_message && (
                <div className="mt-3 rounded-xl bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-400">
                  {state.source_message}
                </div>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => onRefreshMarketState(state.symbol)}
                  className="rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100"
                >
                  刷新指数/标记价
                </button>
              </div>
              {showFundingSettlementAction && (
                <div className="mt-3 border-t border-violet-300/18 pt-3">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div className="max-w-2xl">
                      <div className="text-sm font-semibold text-violet-100">低频危险动作：资金费结算</div>
                      <p className="mt-1 text-xs leading-5 text-violet-100/75">
                        仅用于测试清算演练或资金费补偿。结算会改写 {state.symbol} 合约保证金钱包余额、资金费事件和结算水位；执行前先核对 funding time、费率、水位和是否已有 settlement，提交仍会二次确认，并由后端 `confirm_execute` 拦截未确认请求。
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onSettleFunding(state.symbol)}
                      className="w-full rounded-2xl bg-violet-400/16 px-4 py-2 text-sm text-violet-100 sm:w-fit"
                    >
                      结算资金费率
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
        {marketStates.length === 0 && (
          <div className="rounded-2xl bg-white/5 px-4 py-8 text-center text-sm text-slate-500">暂无合约市场状态。</div>
        )}
      </div>
    </section>
  );
}

function accountUserKindForUser(user: { username: string; role: string }): AccountUserKind {
  const username = user.username.toLowerCase();
  if (username.startsWith("contract_liq_")) return "system";
  const isRobot = user.role === "mm_bot" || username.includes("_mm_") || username.includes("_flow_");
  if (isRobot) {
    if (username.includes("-perp") || username.includes("_perp") || username.includes("perp_")) return "contract_robot";
    return "spot_robot";
  }
  if (user.role === "admin") return "admin";
  return "customer";
}

function accountUserKindLabel(kind: AccountUserKind) {
  if (kind === "system") return "系统主体";
  if (kind === "contract_robot") return "合约机器人";
  if (kind === "spot_robot") return "现货机器人";
  if (kind === "admin") return "管理主体";
  return "客户 UID";
}

function accountFeePolicyCopy(kind: AccountUserKind) {
  if (kind === "spot_robot") {
    return {
      title: "机器人 UID 手续费政策",
      chip: "现货机器人对账",
      chipClass: "bg-cyan-400/12 text-cyan-100",
      fullMarketDetail: "现货机器人是特殊测试 UID，新增默认 0；只在压测或对账需要时覆盖全市场费率，不当成客户优惠入口。",
      singleMarketDetail: "逐市场覆盖这个现货机器人 UID 的费率；没有覆盖时撮合回退市场默认费率，策略参数仍回机器人运营维护。",
      confirmBoundary: "机器人特殊 UID，默认应为 0；本次只服务压测或对账，不改变机器人高频记录留存策略。",
    };
  }
  if (kind === "contract_robot") {
    return {
      title: "机器人 UID 手续费政策",
      chip: "合约机器人对账",
      chipClass: "bg-violet-400/12 text-violet-100",
      fullMarketDetail: "合约机器人是特殊测试 UID，新增默认 0；只在合约做市压测或对账需要时覆盖全市场费率。",
      singleMarketDetail: "逐市场覆盖这个合约机器人 UID 的费率；保证金、仓位、资金费和清算事实仍回合约清算核对。",
      confirmBoundary: "合约机器人特殊 UID，默认应为 0；本次只服务压测或对账，不改变合约保证金或清算口径。",
    };
  }
  if (kind === "system") {
    return {
      title: "系统 UID 手续费政策",
      chip: "系统控制对象",
      chipClass: "bg-amber-400/12 text-amber-100",
      fullMarketDetail: "系统主体不是普通客户或机器人策略账户；只有清算、内部铺盘或演练需要时才应覆盖 UID 费率。",
      singleMarketDetail: "逐市场覆盖系统 UID 前先确认清算或内部用途；不要用它替代客户优惠、机器人策略或市场默认费率。",
      confirmBoundary: "系统主体属于内部控制对象，不是客户优惠入口；执行后应回系统与审计核对操作记录。",
    };
  }
  if (kind === "admin") {
    return {
      title: "管理 UID 手续费政策",
      chip: "内部测试对象",
      chipClass: "bg-fuchsia-400/12 text-fuchsia-100",
      fullMarketDetail: "管理主体费率只服务后台测试或演练；不要把管理主体、客户优惠和机器人策略混在同一口径里。",
      singleMarketDetail: "逐市场覆盖管理 UID 前确认是否只是测试演练；客户单独费率应回客户 UID，机器人费率应回机器人 UID。",
      confirmBoundary: "管理主体属于内部测试对象，不是普通客户或机器人策略入口；执行后应回操作记录核对。",
    };
  }
  return {
    title: "客户 UID 手续费政策",
    chip: "客户完整记录",
    chipClass: "bg-emerald-400/12 text-emerald-100",
    fullMarketDetail: "普通外部客户的单独手续费放在这里维护；订单、成交、费用和资金事实仍按客户业务记录完整核对。",
    singleMarketDetail: "逐市场覆盖这个客户 UID 的费率；没有覆盖时撮合回退市场默认费率，只影响后续成交。",
    confirmBoundary: "普通客户 UID 覆盖，客户订单、成交、费用和资金事实仍按客户业务记录完整核对。",
  };
}

function accountUserScopeLabel(scope: AccountUserScope) {
  if (scope === "all") return "全部主体";
  if (scope === "internal") return "内部主体";
  if (scope === "robot") return "全部机器人";
  return accountUserKindLabel(scope);
}

function accountScopeFromParam(value: string): AccountUserScope {
  return accountUserScopeKeys.includes(value as AccountUserScope) ? value as AccountUserScope : "all";
}

function contractAccountScopeFromParam(value: string): ContractAccountScope {
  return contractAccountScopeKeys.includes(value as ContractAccountScope) ? value as ContractAccountScope : "all";
}

function contractAccountScopeFromAccountScope(scope: AccountUserScope): ContractAccountScope {
  return contractAccountScopeFromParam(scope);
}

function accountScopeIncludesKind(scope: AccountUserScope, kind: AccountUserKind) {
  if (scope === "all") return true;
  if (scope === "internal") return kind !== "customer";
  if (scope === "robot") return kind === "spot_robot" || kind === "contract_robot";
  return scope === kind;
}

function accountScopeIncludesUser(scope: AccountUserScope, user: { username: string; role: string }) {
  return accountScopeIncludesKind(scope, accountUserKindForUser(user));
}

function accountUserKindClass(kind: AccountUserKind) {
  if (kind === "system") return "bg-amber-400/14 text-amber-100";
  if (kind === "contract_robot") return "bg-violet-400/14 text-violet-100";
  if (kind === "spot_robot") return "bg-cyan-400/14 text-cyan-100";
  if (kind === "admin") return "bg-fuchsia-400/14 text-fuchsia-100";
  return "bg-emerald-400/14 text-emerald-100";
}

function accountDirectoryReview(kind: AccountUserKind): AccountDirectoryReview {
  if (kind === "admin") {
    return {
      primaryLedger: "管理主体 + 现货测试钱包",
      verificationPath: "账户与资金核对 API、费率、现货余额；合约保证金另回合约清算。",
      boundary: "管理主体不是统一资金账户；不要把后台登录身份、现货余额和合约保证金混成一套账。",
      tone: "admin",
    };
  }
  if (kind === "spot_robot") {
    return {
      primaryLedger: "现货钱包 + 机器人资源索引",
      verificationPath: "账户与资金看现货余额，机器人账号看绑定，机器人运营看实例和日志。",
      boundary: "现货机器人关联到合约保证金时也只是身份关联，不代表现货钱包可直接抵扣合约保证金。",
      tone: "spot_robot",
    };
  }
  if (kind === "contract_robot") {
    return {
      primaryLedger: "合约保证金 + PERP 机器人资源",
      verificationPath: "合约清算核对保证金、仓位、资金费和合约流水；机器人运营核对实例。",
      boundary: "合约机器人不走现货库存做市；不要在账户目录里执行保证金调账或清算动作。",
      tone: "contract_robot",
    };
  }
  if (kind === "system") {
    return {
      primaryLedger: "系统内部清算 / 流动性账户",
      verificationPath: "合约清算核对系统保证金、强平/ADL、保险基金和内部铺盘订单。",
      boundary: "系统主体不是客户余额，也不是普通机器人 UID；调整前必须回清算域和操作记录核对。",
      tone: "system",
    };
  }
  return {
    primaryLedger: "登录主体 + 现货钱包；合约保证金另账本",
    verificationPath: "账户与资金核对身份、API、现货余额；合约清算核对保证金、仓位和合约流水。",
    boundary: "客户 UID 下可以同时有现货和合约，但当前不是统一账户模型，不做跨账本净额抵扣。",
    tone: "customer",
  };
}

function accountDirectoryReviewClass(tone: AccountDirectoryReview["tone"]) {
  if (tone === "system") return "bg-amber-400/12 text-amber-100";
  if (tone === "contract_robot") return "bg-violet-400/12 text-violet-100";
  if (tone === "spot_robot") return "bg-cyan-400/12 text-cyan-100";
  if (tone === "admin") return "bg-fuchsia-400/12 text-fuchsia-100";
  return "bg-emerald-400/12 text-emerald-100";
}

function spotBalanceAdjustmentRunbookClass(tone: SpotBalanceAdjustmentRunbook["tone"]) {
  if (tone === "danger") return "bg-rose-400/12 text-rose-100";
  if (tone === "reset") return "bg-amber-400/12 text-amber-100";
  if (tone === "negative") return "bg-orange-400/12 text-orange-100";
  return "bg-emerald-400/12 text-emerald-100";
}

function spotBalanceAdjustmentPolicy(kind: AccountUserKind): SpotBalanceAdjustmentPolicy {
  if (kind === "spot_robot") {
    return {
      policy: "允许按现货钱包核对做市库存；机器人运行状态另去铺单策略。",
      preCheck: "确认机器人 UID、市场绑定、现货未完成订单和本次调账原因。",
      postCheck: "核对现货余额、机器人资金快照、挂单冻结资金和操作记录。",
      recoveryPath: "先暂停相关机器人或确认实例状态，再按流水差额做反向调整。",
      boundary: "现货机器人资金不自动转成合约保证金；合约机器人另回合约清算。",
    };
  }
  if (kind === "contract_robot") {
    return {
      policy: "不作为现货调账主对象；合约保证金调整回合约清算。",
      preCheck: "如确需处理现货余额，先确认它不是保证金、资金费或强平补偿。",
      postCheck: "只核对现货钱包变化；保证金、仓位和合约流水必须另行核对。",
      recoveryPath: "合约侧异常不要用现货反向调账修复，先到合约清算定位。",
      boundary: "避免把合约机器人保证金误当成现货余额维护。",
    };
  }
  if (kind === "system") {
    return {
      policy: "系统主体默认不走普通现货调账；先确认清算或内部铺盘归属。",
      preCheck: "确认系统主体用途、涉及市场、内部订单和清算事件。",
      postCheck: "核对系统操作记录、现货余额和相关市场状态，必要时回合约清算复核。",
      recoveryPath: "保留操作记录和导出快照，按业务域逐项恢复，不重复批量重置。",
      boundary: "系统主体不是客户 UID；不得用普通现货调账替代清算修复。",
    };
  }
  if (kind === "admin") {
    return {
      policy: "仅处理管理主体的现货测试钱包；后台登录身份不等于资金审批身份。",
      preCheck: "确认管理主体、资产、金额、原因和是否涉及演练数据保留。",
      postCheck: "核对现货余额、主体活动、API Key 状态和操作记录。",
      recoveryPath: "按操作记录追踪差额，必要时用反向现货调账恢复。",
      boundary: "不要把后台身份、现货测试余额和合约保证金合并理解。",
    };
  }
  return {
    policy: "客户现货钱包可在账户页调账；合约保证金仍是独立账本。",
    preCheck: "确认目标 UID、资产、金额、原因、未完成订单和冻结余额。",
    postCheck: "核对现货可用/冻结余额、现货资金流水、目标 UID 活动和操作记录。",
    recoveryPath: "失败或误操作先查流水和操作记录，再按差额反向调整。",
    boundary: "不做统一账户净额抵扣；合约仓位、资金费和强平回合约清算。",
  };
}

function contractAccountKindForUser(user: ContractAdminUser): ContractAccountKind {
  return accountUserKindForUser(user);
}

function contractAccountKindLabel(kind: ContractAccountKind) {
  if (kind === "system") return "系统主体";
  if (kind === "contract_robot") return "PERP 机器人 UID";
  if (kind === "spot_robot") return "SPOT 机器人 UID";
  if (kind === "admin") return "管理主体";
  return "客户保证金";
}

function contractAccountKindClass(kind: ContractAccountKind) {
  if (kind === "system") return "bg-amber-400/14 text-amber-100";
  if (kind === "contract_robot") return "bg-violet-400/14 text-violet-100";
  if (kind === "spot_robot") return "bg-cyan-400/14 text-cyan-100";
  if (kind === "admin") return "bg-fuchsia-400/14 text-fuchsia-100";
  return "bg-emerald-400/14 text-emerald-100";
}

function contractAccountScopeIncludesKind(scope: ContractAccountScope, kind: ContractAccountKind) {
  if (scope === "all") return true;
  if (scope === "internal") return kind !== "customer";
  if (scope === "robot") return kind === "spot_robot" || kind === "contract_robot";
  return scope === kind;
}

function defaultContractDetailFilters(): ContractDetailFilters {
  return { symbol: "all", query: "", fundingStatus: "all" };
}

function contractDetailQueryMatches(query: string, values: Array<string | number | null | undefined>) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return values.some((value) => String(value ?? "").toLowerCase().includes(normalized));
}

function contractDetailSymbolMatches(filterSymbol: string, symbol?: string | null) {
  return filterSymbol === "all" || symbol === filterSymbol;
}

function contractFundingStatusMatches(filterStatus: ContractDetailFilters["fundingStatus"], status?: string | null) {
  if (filterStatus === "all") return true;
  if (filterStatus === "active") return !["completed", "succeeded", "settled"].includes(status ?? "");
  return status === filterStatus;
}

function contractFilteredCountText(filtered: number, total: number, active: boolean) {
  return active ? `筛选 ${filtered} / ${total}` : `总 ${total} · 默认显示最近 200`;
}

function contractDetailBackendUserId(query: string, users: Array<{ id: number; username: string }>) {
  const normalized = query.trim().replace(/^#/, "").toLowerCase();
  if (!normalized) return null;
  if (/^\d+$/.test(normalized)) return Number(normalized);
  return users.find((user) => user.username.toLowerCase() === normalized)?.id ?? null;
}

function contractDetailTableQuery(
  path: string,
  filters: ContractDetailFilters | undefined,
  users: Array<{ id: number; username: string }>,
  options: { user?: boolean; fundingStatus?: boolean } = {},
) {
  const params = new URLSearchParams({ limit: "200" });
  if (filters?.symbol && filters.symbol !== "all") params.set("symbol", filters.symbol);
  if (options.user && filters?.query) {
    const userId = contractDetailBackendUserId(filters.query, users);
    if (userId !== null) params.set("user_id", String(userId));
  }
  if (options.fundingStatus && filters?.fundingStatus && filters.fundingStatus !== "all") {
    params.set("status", filters.fundingStatus);
  }
  return `${path}?${params.toString()}`;
}

function ContractClearingBoundaryStrip({
  accountsCount,
  activePositionsCount,
  openOrdersCount,
  failedFundingJobCount,
  liquidationCount,
  adlResidual,
  insuranceEventCount,
  totalBadDebt,
  riskTierCount,
  marketCount,
  sourceIssueCount,
  onTabChange,
}: {
  accountsCount: number;
  activePositionsCount: number;
  openOrdersCount: number;
  failedFundingJobCount: number;
  liquidationCount: number;
  adlResidual: number;
  insuranceEventCount: number;
  totalBadDebt: number;
  riskTierCount: number;
  marketCount: number;
  sourceIssueCount: number;
  onTabChange: (tab: ContractAdminTab) => void;
}) {
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    boundary: string;
    tab: ContractAdminTab;
  }> = [
    {
      domain: "价格与市场状态",
      owner: "合约清算 / 概览",
      status: `PERP ${marketCount} · 价格源异常 ${sourceIssueCount}`,
      boundary: "只读观察标记价、指数价、资金费水位和自动维护状态；市场级交易模式和产品参数回市场运营。",
      tab: "overview",
    },
    {
      domain: "保证金账本",
      owner: "保证金账户",
      status: `${accountsCount} 账户 · 当前委托 ${openOrdersCount}`,
      boundary: "只管理合约保证金、占用、PnL 和合约流水，不合并现货钱包，也不在这里维护机器人身份。",
      tab: "accounts",
    },
    {
      domain: "仓位风险",
      owner: "仓位风险",
      status: `活跃持仓 ${activePositionsCount}`,
      boundary: "查看逐仓风险、强平距离、维持保证金和风险告警；跨域待处理项进入风险监控汇总。",
      tab: "positions",
    },
    {
      domain: "订单与成交",
      owner: "订单与成交",
      status: `当前委托 ${openOrdersCount}`,
      boundary: "核对合约委托、成交、手续费和 realized PnL；现货订单与成交仍归现货交易域。",
      tab: "orders",
    },
    {
      domain: "资金费",
      owner: "资金费",
      status: `失败任务 ${failedFundingJobCount}`,
      boundary: "资金费任务、结算记录和用户资金费流水集中在这里；批量重试、结算仍保留确认。",
      tab: "funding",
    },
    {
      domain: "强平 / ADL",
      owner: "强平 / ADL",
      status: `强平 ${liquidationCount} · ADL 剩余 ${fmt(adlResidual, 4)}`,
      boundary: "强平事件、坏账残余和 ADL 执行集中管理；执行 ADL 仍逐事件确认，不放进总览快捷操作。",
      tab: "liquidation",
    },
    {
      domain: "保险基金",
      owner: "保险基金",
      status: `流水 ${insuranceEventCount} · 未覆盖坏账 ${fmt(totalBadDebt, 4)}`,
      boundary: "基金余额、坏账覆盖和调账集中在保险基金页；人工调账保持独立入口和确认边界。",
      tab: "insurance",
    },
    {
      domain: "风险参数",
      owner: "风险参数",
      status: `风险阶梯 ${riskTierCount}`,
      boundary: "维护 leverage、MMR、maintenance amount 等清算参数；保存参数仍在独立 tab 内确认。",
      tab: "risk",
    },
  ];

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h3 className="font-display text-base text-slate-100">合约清算边界</h3>
          <p className="mt-1 text-sm text-slate-500">把混在同一登录用户下的现货、合约、机器人和清算职责拆成运营入口；这里只导航，不改变资金模型。</p>
        </div>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">清算域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <button type="button" onClick={() => onTabChange(row.tab)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100">
                    打开
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ContractAdminPanel({
  accounts,
  positions,
  orders,
  trades,
  contractMarkets,
  marketStates,
  fundingEvents,
  ledgerEntries,
  insuranceFunds,
  insuranceEvents,
  adlEvents,
  insuranceAdjustAmount,
  insuranceAdjusting,
  adlExecutingEventId,
  fundingJobs,
  fundingSettlements,
  liquidationEvents,
  riskTierEdits,
  maintenance,
  detailLoadState,
  activeTab,
  accountScope,
  onTabChange,
  onAccountScopeChange,
  onRefreshMarketState,
  onSettleFunding,
  onRetryFundingJob,
  onRetryFailedFundingJobs,
  onInsuranceAdjustAmountChange,
  onAdjustInsuranceFund,
  onExecuteAdl,
  batchRetryingFundingJobs,
  onRiskTierChange,
  onAddRiskTier,
  onRemoveRiskTier,
  onSaveRiskTiers,
  onRefresh,
  onRefreshDetails,
  onOpenAccountDirectory,
  onOpenBotAccounts,
  onOpenContractAccounts,
  onOpenContractPositions,
  onOpenContractOrders,
  onOpenContractTrade,
  onOpenMarketProductConfig,
}: {
  accounts: ContractAccountAdminItem[];
  positions: ContractPositionAdminItem[];
  orders: ContractOrderAdminItem[];
  trades: TradeItem[];
  contractMarkets: AdminMarketItem[];
  marketStates: ContractPriceState[];
  fundingEvents: ContractFundingEventAdminItem[];
  ledgerEntries: ContractLedgerAdminItem[];
  insuranceFunds: ContractInsuranceFund[];
  insuranceEvents: ContractInsuranceEventAdminItem[];
  adlEvents: ContractAdlEventAdminItem[];
  insuranceAdjustAmount: string;
  insuranceAdjusting: boolean;
  adlExecutingEventId: string | null;
  fundingJobs: ContractFundingJob[];
  fundingSettlements: ContractFundingSettlement[];
  liquidationEvents: ContractLiquidationEventAdminItem[];
  riskTierEdits: Record<string, ContractRiskTierEdit[]>;
  maintenance: ContractMaintenanceStatus;
  detailLoadState: ContractDetailLoadState;
  activeTab: ContractAdminTab;
  accountScope: ContractAccountScope;
  onTabChange: (tab: ContractAdminTab) => void;
  onAccountScopeChange: (scope: ContractAccountScope) => void;
  onRefreshMarketState: (symbol: string) => void;
  onSettleFunding: (symbol: string) => void;
  onRetryFundingJob: (job: ContractFundingJob) => void;
  onRetryFailedFundingJobs: () => void;
  onInsuranceAdjustAmountChange: (value: string) => void;
  onAdjustInsuranceFund: () => void;
  onExecuteAdl: (event: ContractLiquidationEvent) => void;
  batchRetryingFundingJobs: boolean;
  onRiskTierChange: (symbol: string, index: number, field: keyof ContractRiskTierEdit, value: string | number) => void;
  onAddRiskTier: (symbol: string) => void;
  onRemoveRiskTier: (symbol: string, index: number) => void;
  onSaveRiskTiers: (symbol: string) => void;
  onRefresh: () => void;
  onRefreshDetails: (filters?: ContractDetailFilters) => void;
  onOpenAccountDirectory: () => void;
  onOpenBotAccounts: () => void;
  onOpenContractAccounts: () => void;
  onOpenContractPositions: () => void;
  onOpenContractOrders: () => void;
  onOpenContractTrade: (symbol: string) => void;
  onOpenMarketProductConfig: (symbol: string) => void;
}) {
  const [detailFilters, setDetailFilters] = useState<ContractDetailFilters>(() => defaultContractDetailFilters());
  const [ledgerPage, setLedgerPage] = useState(1);
  const [positionsPage, setPositionsPage] = useState(1);
  const [fundingJobsPage, setFundingJobsPage] = useState(1);
  const [fundingSettlementsPage, setFundingSettlementsPage] = useState(1);
  const [fundingEventsPage, setFundingEventsPage] = useState(1);
  const [liquidationPage, setLiquidationPage] = useState(1);
  const [adlPage, setAdlPage] = useState(1);
  const [selectedAdlEventId, setSelectedAdlEventId] = useState<string | null>(null);
  const [insuranceEventsPage, setInsuranceEventsPage] = useState(1);
  const [contractOrdersPage, setContractOrdersPage] = useState(1);
  const [contractTradesPage, setContractTradesPage] = useState(1);
  const detailQuery = detailFilters.query.trim();
  const detailFiltersActive = detailFilters.symbol !== "all" || detailQuery.length > 0 || detailFilters.fundingStatus !== "all";
  const contractSymbols = Array.from(new Set([
    ...marketStates.map((item) => item.symbol),
    ...positions.map((item) => item.position.symbol),
    ...orders.map((item) => item.order.symbol),
    ...trades.map((item) => item.symbol),
    ...fundingEvents.map((item) => item.event.symbol),
    ...ledgerEntries.map((item) => item.entry.symbol).filter(Boolean),
    ...insuranceEvents.map((item) => item.symbol).filter(Boolean),
    ...adlEvents.map((item) => item.symbol).filter(Boolean),
    ...fundingJobs.map((item) => item.symbol),
    ...fundingSettlements.map((item) => item.symbol),
    ...liquidationEvents.map((item) => item.event.symbol),
  ] as string[])).sort();
  const activePositions = positions.filter((item) => Number(item.position.quantity) > 0 && item.position.side !== "flat");
  const openOrders = orders.filter((item) => item.order.status === "new" || item.order.status === "partially_filled");
  const stressedPositions = activePositions.filter((item) => !["flat", "ok"].includes(item.position.risk_status ?? "ok"));
  const closestLiquidationDistance = activePositions.reduce<number | null>((best, item) => {
    const value = Number(item.position.liquidation_distance_pct);
    if (!Number.isFinite(value) || value <= 0) return best;
    return best === null || value < best ? value : best;
  }, null);
  const riskAlerts = Object.values(maintenance.risk_alerts ?? {}).flat();
  const maintenanceFundingSettlements = maintenance.metrics?.funding_settlements ?? [];
  const maintenanceFundingJobs = maintenance.metrics?.funding_jobs ?? [];
  const failedFundingJobCount = fundingJobs.filter((item) => item.status === "failed").length;
  const liquidationSettlements = maintenance.metrics?.liquidations ?? [];
  const maintenanceAdlEvents = maintenance.metrics?.adl_events ?? [];
  const liquidity = maintenance.liquidity;
  const maintenanceErrorCount = (maintenance.metrics?.errors?.length ?? 0) + (liquidity?.errors?.length ?? 0);
  const sourceIssueCount = marketStates.filter((state) => !["ok", "external_ok"].includes(state.source_status)).length;
  const totalWallet = accounts.reduce((sum, item) => sum + Number(item.account.wallet_balance || 0), 0);
  const totalAvailableMargin = accounts.reduce((sum, item) => sum + Number(item.account.available_margin || 0), 0);
  const totalUsedMargin = accounts.reduce((sum, item) => sum + Number(item.account.used_margin || 0), 0);
  const totalUnrealizedPnl = accounts.reduce((sum, item) => sum + Number(item.account.unrealized_pnl || 0), 0);
  const totalBadDebt = insuranceEvents.reduce((sum, item) => sum + Number(item.residual_bad_debt || 0), 0);
  const totalAdlResidual = liquidationEvents.reduce((sum, item) => sum + Number(item.event.adl_residual || 0), 0);
  const accountKindCounts = accounts.reduce<Record<ContractAccountKind, number>>((current, item) => {
    const kind = contractAccountKindForUser(item.user);
    current[kind] += 1;
    return current;
  }, { customer: 0, admin: 0, spot_robot: 0, contract_robot: 0, system: 0 });
  const scopedAccounts = accounts.filter((item) => contractAccountScopeIncludesKind(accountScope, contractAccountKindForUser(item.user)));
  const scopedLedgerEntries = ledgerEntries.filter((item) => contractAccountScopeIncludesKind(accountScope, contractAccountKindForUser(item.user)));
  const filteredScopedAccounts = scopedAccounts.filter((item) => contractDetailQueryMatches(detailQuery, [item.user.username, item.user.id, item.user.role]));
  const filteredScopedLedgerEntries = scopedLedgerEntries.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.entry.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.user.username,
      item.user.id,
      item.user.role,
      item.entry.entry_id,
      item.entry.change_type,
      item.entry.related_trade_id,
      item.entry.related_order_id,
      item.entry.related_event_id,
      item.entry.note,
    ])
  ));
  const ledgerTotalPages = Math.max(1, Math.ceil(filteredScopedLedgerEntries.length / AUDIT_TABLE_PAGE_SIZE));
  const safeLedgerPage = Math.min(ledgerPage, ledgerTotalPages);
  const visibleScopedLedgerEntries = filteredScopedLedgerEntries.slice(
    (safeLedgerPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeLedgerPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const marginLedgerRows = marginLedgerBuckets.map((bucket) => ({
    bucket,
    count: filteredScopedLedgerEntries.filter((item) => marginLedgerBucket(item.entry) === bucket).length,
  }));
  useEffect(() => {
    setLedgerPage(1);
  }, [activeTab, accountScope, detailFilters.symbol, detailFilters.fundingStatus, detailQuery, filteredScopedLedgerEntries.length]);
  const filteredPositions = positions.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.position.symbol) &&
    contractDetailQueryMatches(detailQuery, [item.user.username, item.user.id, item.user.role, item.position.side, item.position.risk_status])
  ));
  const positionsTotalPages = Math.max(1, Math.ceil(filteredPositions.length / AUDIT_TABLE_PAGE_SIZE));
  const safePositionsPage = Math.min(positionsPage, positionsTotalPages);
  const visiblePositions = filteredPositions.slice(
    (safePositionsPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safePositionsPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const positionRiskRows = positionRiskBuckets.map((bucket) => ({
    bucket,
    count: filteredPositions.filter((item) => positionRiskBucket(item.position) === bucket).length,
  }));
  useEffect(() => {
    setPositionsPage(1);
  }, [activeTab, detailFilters.symbol, detailQuery, filteredPositions.length]);
  const filteredOrders = orders.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.order.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.user.username,
      item.user.id,
      item.user.role,
      item.order.order_id,
      item.order.side,
      item.order.status,
      item.order.position_action,
    ])
  ));
  const filteredTrades = trades.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.symbol) &&
    contractDetailQueryMatches(detailQuery, [item.trade_id, item.symbol, item.side, item.taker_side])
  ));
  const contractClearingOrderTradeRows = contractClearingOrderTradeBuckets.map((bucket) => ({
    bucket,
    count: (() => {
      if (bucket === "live_margin") return filteredOrders.filter((item) => orderStateBucket(item.order.status) === "live").length;
      if (bucket === "filled_settlement") return filteredTrades.length;
      if (bucket === "close_reduce") {
        return filteredOrders.filter((item) => contractOrderIsCloseReduce(item.order)).length +
          filteredTrades.filter((item) => contractTradeIsCloseReduce(item)).length;
      }
      if (bucket === "rejected_canceled") {
        return filteredOrders.filter((item) => {
          const state = orderStateBucket(item.order.status);
          return state === "rejected" || state === "canceled" || state === "expired";
        }).length;
      }
      if (bucket === "robot_system") {
        return filteredOrders.filter((item) => contractOrderUserIsRobotOrSystem(item.user)).length +
          filteredTrades.filter((item) => contractTradeIsRobotOrSystem(item)).length;
      }
      return filteredOrders.filter((item) => orderStateBucket(item.order.status) === "other").length +
        filteredTrades.filter((item) => tradeSourceBucket(item) === "unknown").length;
    })(),
  }));
  const contractOrdersTotalPages = Math.max(1, Math.ceil(filteredOrders.length / AUDIT_TABLE_PAGE_SIZE));
  const safeContractOrdersPage = Math.min(contractOrdersPage, contractOrdersTotalPages);
  const visibleContractOrders = filteredOrders.slice(
    (safeContractOrdersPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeContractOrdersPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const contractTradesTotalPages = Math.max(1, Math.ceil(filteredTrades.length / AUDIT_TABLE_PAGE_SIZE));
  const safeContractTradesPage = Math.min(contractTradesPage, contractTradesTotalPages);
  const visibleContractTrades = filteredTrades.slice(
    (safeContractTradesPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeContractTradesPage * AUDIT_TABLE_PAGE_SIZE,
  );
  useEffect(() => {
    setContractOrdersPage(1);
    setContractTradesPage(1);
  }, [activeTab, detailFilters.symbol, detailFilters.fundingStatus, detailQuery, filteredOrders.length, filteredTrades.length]);
  const filteredInsuranceEvents = insuranceEvents.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.event_id,
      item.event_type,
      item.user_id,
      item.related_liquidation_event_id,
      item.note,
      item.margin_asset,
    ])
  ));
  const insuranceEventRows = insuranceEventBuckets.map((bucket) => ({
    bucket,
    count: filteredInsuranceEvents.filter((item) => insuranceEventBucket(item) === bucket).length,
  }));
  const insuranceEventsTotalPages = Math.max(1, Math.ceil(filteredInsuranceEvents.length / AUDIT_TABLE_PAGE_SIZE));
  const safeInsuranceEventsPage = Math.min(insuranceEventsPage, insuranceEventsTotalPages);
  const visibleInsuranceEvents = filteredInsuranceEvents.slice(
    (safeInsuranceEventsPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeInsuranceEventsPage * AUDIT_TABLE_PAGE_SIZE,
  );
  useEffect(() => {
    setInsuranceEventsPage(1);
  }, [activeTab, detailFilters.symbol, detailFilters.fundingStatus, detailQuery, filteredInsuranceEvents.length]);
  const filteredFundingJobs = fundingJobs.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.symbol) &&
    contractFundingStatusMatches(detailFilters.fundingStatus, item.status) &&
    contractDetailQueryMatches(detailQuery, [item.job_id, item.status, item.locked_by, item.settlement_id, item.last_error])
  ));
  const filteredFundingSettlements = fundingSettlements.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.symbol) &&
    contractFundingStatusMatches(detailFilters.fundingStatus, item.status) &&
    contractDetailQueryMatches(detailQuery, [item.settlement_id, item.status, item.error_message, item.funding_rate_mode])
  ));
  const filteredFundingEvents = fundingEvents.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.event.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.user.username,
      item.user.id,
      item.user.role,
      item.event.event_id,
      item.event.position_side,
      item.event.funding_rate_mode,
    ])
  ));
  const fundingOpsRows = fundingOpsBuckets.map((bucket) => {
    const count = bucket === "account_record"
      ? filteredFundingEvents.length
      : bucket === "unknown"
        ? filteredFundingJobs.filter((item) => fundingOpsBucket(item.status, "job") === "unknown").length
          + filteredFundingSettlements.filter((item) => fundingOpsBucket(item.status, "settlement") === "unknown").length
        : bucket === "settled"
          ? filteredFundingSettlements.filter((item) => fundingOpsBucket(item.status, "settlement") === "settled").length
          : filteredFundingJobs.filter((item) => fundingOpsBucket(item.status, "job") === bucket).length;
    return { bucket, count };
  });
  const fundingJobsTotalPages = Math.max(1, Math.ceil(filteredFundingJobs.length / AUDIT_TABLE_PAGE_SIZE));
  const safeFundingJobsPage = Math.min(fundingJobsPage, fundingJobsTotalPages);
  const visibleFundingJobs = filteredFundingJobs.slice(
    (safeFundingJobsPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeFundingJobsPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const fundingSettlementsTotalPages = Math.max(1, Math.ceil(filteredFundingSettlements.length / AUDIT_TABLE_PAGE_SIZE));
  const safeFundingSettlementsPage = Math.min(fundingSettlementsPage, fundingSettlementsTotalPages);
  const visibleFundingSettlements = filteredFundingSettlements.slice(
    (safeFundingSettlementsPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeFundingSettlementsPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const fundingEventsTotalPages = Math.max(1, Math.ceil(filteredFundingEvents.length / AUDIT_TABLE_PAGE_SIZE));
  const safeFundingEventsPage = Math.min(fundingEventsPage, fundingEventsTotalPages);
	  const visibleFundingEvents = filteredFundingEvents.slice(
	    (safeFundingEventsPage - 1) * AUDIT_TABLE_PAGE_SIZE,
	    safeFundingEventsPage * AUDIT_TABLE_PAGE_SIZE,
	  );
	  const fundingRecordsVisibleCount = filteredFundingJobs.length + filteredFundingSettlements.length + filteredFundingEvents.length;
	  const fundingRecordsTotalCount = fundingJobs.length + fundingSettlements.length + fundingEvents.length;
	  useEffect(() => {
	    setFundingJobsPage(1);
    setFundingSettlementsPage(1);
    setFundingEventsPage(1);
  }, [
    activeTab,
    detailFilters.symbol,
    detailFilters.fundingStatus,
    detailQuery,
    filteredFundingJobs.length,
    filteredFundingSettlements.length,
    filteredFundingEvents.length,
  ]);
  const filteredLiquidationEvents = liquidationEvents.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.event.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.user.username,
      item.user.id,
      item.user.role,
      item.event.event_id,
      item.event.position_side,
      item.event.reason,
      item.event.risk_status,
      item.event.adl_status,
    ])
  ));
  const filteredAdlEvents = adlEvents.filter((item) => (
    contractDetailSymbolMatches(detailFilters.symbol, item.symbol) &&
    contractDetailQueryMatches(detailQuery, [
      item.event_id,
      item.liquidation_event_id,
      item.username,
      item.user_id,
      item.position_side,
    ])
  ));
  const clearingEventRows = clearingEventBuckets.map((bucket) => ({
    bucket,
    count: filteredLiquidationEvents.filter((item) => clearingEventBucketFromLiquidation(item.event) === bucket).length
      + filteredAdlEvents.filter((item) => clearingEventBucketFromAdl(item) === bucket).length,
  }));
  const liquidationTotalPages = Math.max(1, Math.ceil(filteredLiquidationEvents.length / AUDIT_TABLE_PAGE_SIZE));
  const safeLiquidationPage = Math.min(liquidationPage, liquidationTotalPages);
  const visibleLiquidationEvents = filteredLiquidationEvents.slice(
    (safeLiquidationPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeLiquidationPage * AUDIT_TABLE_PAGE_SIZE,
  );
  const selectedAdlEvent = selectedAdlEventId
    ? filteredLiquidationEvents.find((item) => item.event.event_id === selectedAdlEventId) ?? null
    : null;
  const selectedAdlResidual = Number(selectedAdlEvent?.event.adl_residual || selectedAdlEvent?.event.residual_bad_debt || 0);
  const adlTotalPages = Math.max(1, Math.ceil(filteredAdlEvents.length / AUDIT_TABLE_PAGE_SIZE));
  const safeAdlPage = Math.min(adlPage, adlTotalPages);
  const visibleAdlEvents = filteredAdlEvents.slice(
    (safeAdlPage - 1) * AUDIT_TABLE_PAGE_SIZE,
    safeAdlPage * AUDIT_TABLE_PAGE_SIZE,
  );
  useEffect(() => {
    setLiquidationPage(1);
    setAdlPage(1);
  }, [activeTab, detailFilters.symbol, detailFilters.fundingStatus, detailQuery, filteredLiquidationEvents.length, filteredAdlEvents.length]);
  const scopedWallet = scopedAccounts.reduce((sum, item) => sum + Number(item.account.wallet_balance || 0), 0);
  const scopedAvailableMargin = scopedAccounts.reduce((sum, item) => sum + Number(item.account.available_margin || 0), 0);
  const scopedUsedMargin = scopedAccounts.reduce((sum, item) => sum + Number(item.account.used_margin || 0), 0);
  const scopedUnrealizedPnl = scopedAccounts.reduce((sum, item) => sum + Number(item.account.unrealized_pnl || 0), 0);
  const contractInternalAccountCount = accountKindCounts.admin + accountKindCounts.spot_robot + accountKindCounts.contract_robot + accountKindCounts.system;
  const contractRobotAccountCount = accountKindCounts.spot_robot + accountKindCounts.contract_robot;
  const accountScopeOptions: { key: ContractAccountScope; label: string; hint: string; count: number }[] = [
    { key: "all", label: "全部", hint: "全部保证金账户", count: accounts.length },
    { key: "customer", label: "客户保证金", hint: "普通客户 UID / 手动测试", count: accountKindCounts.customer },
    { key: "internal", label: "内部主体", hint: "管理 / 机器人 / 系统用途", count: contractInternalAccountCount },
    { key: "robot", label: "机器人保证金", hint: "SPOT / PERP 机器人 UID", count: contractRobotAccountCount },
    { key: "admin", label: "管理主体", hint: "后台管理员已有保证金账户", count: accountKindCounts.admin },
    { key: "spot_robot", label: "SPOT 机器人 UID", hint: "只表示该 UID 已有合约保证金记录", count: accountKindCounts.spot_robot },
    { key: "contract_robot", label: "PERP 机器人 UID", hint: "PERP_MM / PERP_FLOW 保证金", count: accountKindCounts.contract_robot },
    { key: "system", label: "系统主体", hint: "清算 seed / fallback / 内部流动性", count: accountKindCounts.system },
  ];
  const activeAccountScopeOption = accountScopeOptions.find((option) => option.key === accountScope) ?? accountScopeOptions[0];
  const primaryContractSymbol = contractMarkets[0]?.symbol ?? "BTCUSDT-PERP";
  const contractAccountJudgment =
    accounts.length === 0
      ? {
          status: "需处理",
          tone: "danger" as const,
          focus: "暂无保证金账户",
          next: "先确认是否已有合约交易、PERP 机器人模板或手动创建记录；本页不会为现货用户自动开合约保证金账户。",
        }
      : filteredScopedAccounts.length === 0
        ? {
            status: "需关注",
            tone: "warn" as const,
            focus: `${activeAccountScopeOption.label}筛选为空`,
            next: "先放宽账户范围、市场或关键字；不要把空筛选误读为没有合约保证金账本。",
          }
        : accountScope === "customer"
          ? {
              status: "正常",
              tone: "neutral" as const,
              focus: "客户保证金账户",
              next: "核对客户保证金、仓位、流水和资金费；现货钱包仍回账户与资金，不做统一账户抵扣。",
            }
          : accountScope === "robot" || accountScope === "spot_robot" || accountScope === "contract_robot"
            ? {
                status: "需关注",
                tone: "warn" as const,
                focus: "机器人保证金占用",
                next: "按机器人 UID 核对保证金、当前委托和策略日志；这是运行对账，不是客户完整记录。",
              }
            : accountScope === "admin" || accountScope === "system" || accountScope === "internal"
              ? {
                  status: "需关注",
                  tone: "warn" as const,
                  focus: "内部 / 系统保证金账户",
                  next: "核对管理、系统、seed 或 fallback 用途；不要把内部账户资金当成客户资产。",
                }
              : {
                  status: "正常",
                  tone: "neutral" as const,
                  focus: "全部保证金账户",
                  next: "先按客户、机器人、管理或系统用途缩小范围，再核对对应流水和风险。",
                };
  const contractAccountJudgmentClass =
    contractAccountJudgment.tone === "danger"
      ? "bg-rose-500/16 text-rose-100"
      : contractAccountJudgment.tone === "warn"
        ? "bg-amber-400/16 text-amber-100"
        : "bg-emerald-400/15 text-emerald-100";
  const contractMarketBySymbol = new Map(contractMarkets.map((market) => [market.symbol, market]));
  const riskTierSymbols = Array.from(new Set([
    ...contractMarkets.map((item) => item.symbol),
    ...marketStates.map((item) => item.symbol),
    ...Object.keys(riskTierEdits),
  ])).sort();
  const riskTierExportCount = riskTierSymbols.reduce((sum, symbol) => sum + (riskTierEdits[symbol]?.length ?? 0), 0);
  const riskTierCoverageGapCount = riskTierSymbols.filter((symbol) => !contractMarketBySymbol.has(symbol) || (riskTierEdits[symbol]?.length ?? 0) === 0).length;
  const riskParameterRows = riskParameterBuckets.map((bucket) => ({
    bucket,
    count: bucket === "risk_tier"
      ? riskTierExportCount
      : bucket === "coverage_gap"
        ? riskTierCoverageGapCount
        : contractMarkets.length,
  }));
  const exportLedgerScope = [
    accountScope,
    detailFilters.symbol,
    detailQuery || "all",
  ].map((item) => String(item || "all").replace(/[^a-zA-Z0-9_-]+/g, "-")).join("_");
  const exportContractLedger = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_contract_ledger_${exportLedgerScope}_${stamp}.csv`,
      [
        "created_at",
        "account",
        "user_id",
        "account_kind",
        "role",
        "symbol",
        "change_type",
        "margin_ledger_bucket",
        "margin_ledger_label",
        "ledger_verification_path",
        "ledger_boundary_note",
        "amount",
        "margin_asset",
        "wallet_before",
        "wallet_after",
        "available_before",
        "available_after",
        "used_margin_before",
        "used_margin_after",
        "unrealized_pnl_before",
        "unrealized_pnl_after",
        "realized_pnl_before",
        "realized_pnl_after",
        "total_fees_before",
        "total_fees_after",
        "related_trade_id",
        "related_order_id",
        "related_event_id",
        "note",
        "entry_id",
      ],
      filteredScopedLedgerEntries.map(({ user, entry }) => {
        const kind = contractAccountKindForUser(user);
        const bucket = marginLedgerBucket(entry);
        return [
          bjDateTime(entry.created_at),
          user.username,
          user.id,
          contractAccountKindLabel(kind),
          user.role,
          entry.symbol ?? "",
          entry.change_type,
          bucket,
          marginLedgerBucketLabel(bucket),
          marginLedgerVerificationPath(entry),
          marginLedgerBoundaryNote(entry),
          entry.amount,
          entry.margin_asset,
          entry.wallet_before,
          entry.wallet_after,
          entry.available_before,
          entry.available_after,
          entry.used_margin_before,
          entry.used_margin_after,
          entry.unrealized_pnl_before,
          entry.unrealized_pnl_after,
          entry.realized_pnl_before,
          entry.realized_pnl_after,
          entry.total_fees_before,
          entry.total_fees_after,
          entry.related_trade_id ?? "",
          entry.related_order_id ?? "",
          entry.related_event_id ?? "",
          entry.note ?? "",
          entry.entry_id,
        ];
      }),
    );
  };
  const exportRiskTiers = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_contract_risk_tiers_all_${stamp}.csv`,
      [
        "symbol",
        "product_type",
        "market_active",
        "contract_trading_mode",
        "margin_asset",
        "market_max_leverage",
        "market_default_leverage",
        "market_maintenance_margin_rate",
        "funding_rate_mode",
        "risk_parameter_bucket",
        "risk_parameter_label",
        "risk_parameter_verification_path",
        "risk_parameter_boundary_note",
        "tier",
        "notional_floor",
        "notional_cap",
        "max_leverage",
        "maintenance_margin_rate",
        "maintenance_amount",
        "export_source",
      ],
      riskTierSymbols.flatMap((symbol) => {
        const market = contractMarketBySymbol.get(symbol);
        const rows = riskTierEdits[symbol] ?? [];
        const reviewBucket: RiskParameterBucket = "risk_tier";
        return rows.map((row, index) => [
          symbol,
          market?.product_type ?? "PERP",
          market ? (market.is_active ? "true" : "false") : "",
          market?.contract_trading_mode ?? "",
          market?.margin_asset ?? market?.quote_asset ?? "",
          market?.max_leverage ?? "",
          market?.default_leverage ?? "",
          market?.maintenance_margin_rate ?? "",
          market?.funding_rate_mode ?? "",
          reviewBucket,
          riskParameterBucketLabel(reviewBucket),
          riskParameterVerificationPath(reviewBucket),
          riskParameterBoundaryNote(reviewBucket),
          row.tier || index + 1,
          row.notional_floor,
          row.notional_cap || "",
          row.max_leverage,
          row.maintenance_margin_rate,
          row.maintenance_amount,
          "current_page_state",
        ]);
      }),
    );
  };
  const exportFundingScope = [
    detailFilters.symbol,
    detailFilters.fundingStatus,
    detailQuery || "all",
  ].map((item) => String(item || "all").replace(/[^a-zA-Z0-9_-]+/g, "-")).join("_");
  const exportFundingJobs = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_funding_jobs_${exportFundingScope}_${stamp}.csv`,
      [
        "funding_time",
        "symbol",
        "status",
        "funding_ops_bucket",
        "funding_ops_label",
        "funding_verification_path",
        "funding_boundary_note",
        "attempt_count",
        "max_attempts",
        "locked_by",
        "locked_at",
        "heartbeat_at",
        "next_retry_at",
        "settlement_id",
        "last_error",
        "started_at",
        "completed_at",
        "updated_at",
        "job_id",
      ],
      filteredFundingJobs.map((item) => {
        const bucket = fundingOpsBucket(item.status, "job");
        return [
          bjDateTime(item.funding_time),
          item.symbol,
          item.status,
          bucket,
          fundingOpsBucketLabel(bucket),
          fundingOpsVerificationPath(bucket),
          fundingOpsBoundaryNote(bucket),
          item.attempt_count,
          item.max_attempts,
          item.locked_by ?? "",
          bjDateTime(item.locked_at),
          bjDateTime(item.heartbeat_at),
          bjDateTime(item.next_retry_at),
          item.settlement_id ?? "",
          item.last_error ?? "",
          bjDateTime(item.started_at),
          bjDateTime(item.completed_at),
          bjDateTime(item.updated_at),
          item.job_id,
        ];
      }),
    );
  };
  const exportFundingSettlements = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_funding_settlements_${exportFundingScope}_${stamp}.csv`,
      [
        "funding_time",
        "symbol",
        "status",
        "funding_ops_bucket",
        "funding_ops_label",
        "funding_verification_path",
        "funding_boundary_note",
        "settled_count",
        "total_amount",
        "funding_rate",
        "funding_rate_mode",
        "index_price",
        "mark_price",
        "error_message",
        "started_at",
        "completed_at",
        "updated_at",
        "settlement_id",
      ],
      filteredFundingSettlements.map((item) => {
        const bucket = fundingOpsBucket(item.status, "settlement");
        return [
          bjDateTime(item.funding_time),
          item.symbol,
          item.status,
          bucket,
          fundingOpsBucketLabel(bucket),
          fundingOpsVerificationPath(bucket),
          fundingOpsBoundaryNote(bucket),
          item.settled_count,
          item.total_amount,
          item.funding_rate,
          item.funding_rate_mode,
          item.index_price,
          item.mark_price,
          item.error_message ?? "",
          bjDateTime(item.started_at),
          bjDateTime(item.completed_at),
          bjDateTime(item.updated_at),
          item.settlement_id,
        ];
      }),
    );
  };
  const exportFundingEvents = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_funding_events_${exportFundingScope}_${stamp}.csv`,
      [
        "funding_time",
        "created_at",
        "account",
        "user_id",
        "role",
        "symbol",
        "position_side",
        "funding_ops_bucket",
        "funding_ops_label",
        "funding_verification_path",
        "funding_boundary_note",
        "quantity",
        "index_price",
        "mark_price",
        "funding_rate",
        "amount",
        "funding_rate_mode",
        "event_id",
      ],
      filteredFundingEvents.map(({ user, event }) => {
        const bucket = fundingOpsBucket(undefined, "event");
        return [
          bjDateTime(event.funding_time),
          bjDateTime(event.created_at),
          user.username,
          user.id,
          user.role,
          event.symbol,
          event.position_side,
          bucket,
          fundingOpsBucketLabel(bucket),
          fundingOpsVerificationPath(bucket),
          fundingOpsBoundaryNote(bucket),
          event.quantity,
          event.index_price,
          event.mark_price,
          event.funding_rate,
          event.amount,
          event.funding_rate_mode,
          event.event_id,
        ];
      }),
    );
  };
  const exportClearingScope = [
    detailFilters.symbol,
    detailQuery || "all",
  ].map((item) => String(item || "all").replace(/[^a-zA-Z0-9_-]+/g, "-")).join("_");
  const exportContractOrders = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_contract_orders_${exportClearingScope}_${stamp}.csv`,
      [
        "updated_at",
        "created_at",
        "account",
        "user_id",
        "role",
        "symbol",
        "product_type",
        "side",
        "position_action",
        "reduce_only",
        "leverage",
        "type",
        "tif",
        "status",
        "contract_clearing_bucket",
        "contract_clearing_label",
        "clearing_verification_path",
        "clearing_boundary_note",
        "price",
        "quantity",
        "filled_quantity",
        "remaining_quantity",
        "avg_price",
        "notional",
        "reject_reason",
        "order_id",
        "client_order_id",
      ],
      filteredOrders.map(({ user, order }) => {
        const bucket = contractOrderClearingBucket(order, user);
        return [
          bjDateTime(order.updated_at ?? order.created_at),
          bjDateTime(order.created_at),
          user.username,
          user.id,
          user.role,
          order.symbol,
          order.product_type ?? "",
          order.side,
          order.position_action ?? "",
          order.reduce_only ? "true" : "false",
          order.leverage ?? "",
          order.type,
          order.tif,
          order.status,
          bucket,
          contractClearingOrderTradeBucketLabel(bucket),
          contractClearingOrderTradeVerificationPath(bucket),
          contractClearingOrderTradeBoundaryNote(bucket),
          order.price ?? "",
          order.quantity,
          order.filled_quantity,
          order.remaining_quantity,
          order.avg_price ?? "",
          order.notional ?? "",
          order.reject_reason ?? "",
          order.order_id,
          order.client_order_id ?? "",
        ];
      }),
    );
  };
  const exportContractTrades = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_contract_trades_${exportClearingScope}_${stamp}.csv`,
      [
        "executed_at",
        "symbol",
        "product_type",
        "taker_side",
        "taker_position_action",
        "maker_position_action",
        "price",
        "quantity",
        "quote_amount",
        "taker_fee",
        "taker_fee_asset",
        "maker_fee",
        "maker_fee_asset",
        "taker_realized_pnl",
        "maker_realized_pnl",
        "source",
        "contract_clearing_bucket",
        "contract_clearing_label",
        "clearing_verification_path",
        "clearing_boundary_note",
        "trade_id",
      ],
      filteredTrades.map((item) => {
        const bucket = contractTradeClearingBucket(item);
        return [
          bjDateTime(item.executed_at ?? item.ts),
          item.symbol,
          item.product_type ?? "",
          item.taker_side ?? item.side ?? "",
          item.taker_position_action ?? item.position_action ?? "",
          item.maker_position_action ?? "",
          item.price,
          item.quantity,
          item.quote_amount ?? "",
          item.taker_fee ?? item.fee ?? "",
          item.taker_fee_asset ?? item.fee_asset ?? "",
          item.maker_fee ?? "",
          item.maker_fee_asset ?? "",
          item.taker_realized_pnl ?? item.realized_pnl ?? "",
          item.maker_realized_pnl ?? "",
          item.source ?? "",
          bucket,
          contractClearingOrderTradeBucketLabel(bucket),
          contractClearingOrderTradeVerificationPath(bucket),
          contractClearingOrderTradeBoundaryNote(bucket),
          item.trade_id,
        ];
      }),
    );
  };
  const exportPositions = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_contract_positions_${exportClearingScope}_${stamp}.csv`,
      [
        "updated_at",
        "account",
        "user_id",
        "role",
        "symbol",
        "product_type",
        "side",
        "quantity",
        "entry_price",
        "mark_price",
        "liquidation_price",
        "leverage",
        "margin_mode",
        "isolated_margin",
        "maintenance_margin",
        "unrealized_pnl",
        "realized_pnl",
        "risk_status",
        "position_risk_bucket",
        "position_risk_label",
        "risk_verification_path",
        "clearing_boundary_note",
        "liquidation_distance_pct",
        "margin_buffer",
        "risk_tier",
        "risk_notional_floor",
        "risk_notional_cap",
        "risk_max_leverage",
        "maintenance_margin_rate",
        "maintenance_amount",
      ],
      filteredPositions.map(({ user, position }) => {
        const bucket = positionRiskBucket(position);
        return [
          bjDateTime(position.updated_at),
          user.username,
          user.id,
          user.role,
          position.symbol,
          position.product_type,
          position.side,
          position.quantity,
          position.entry_price,
          position.mark_price,
          position.liquidation_price,
          position.leverage,
          position.margin_mode,
          position.isolated_margin,
          position.maintenance_margin,
          position.unrealized_pnl,
          position.realized_pnl,
          position.risk_status ?? "",
          bucket,
          positionRiskBucketLabel(bucket),
          positionRiskVerificationPath(position),
          positionRiskBoundaryNote(position),
          position.liquidation_distance_pct ?? "",
          position.margin_buffer ?? "",
          position.risk_tier ?? "",
          position.risk_notional_floor ?? "",
          position.risk_notional_cap ?? "",
          position.risk_max_leverage ?? "",
          position.maintenance_margin_rate ?? "",
          position.maintenance_amount ?? "",
        ];
      }),
    );
  };
  const exportLiquidationEvents = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_liquidations_${exportClearingScope}_${stamp}.csv`,
      [
        "liquidated_at",
        "created_at",
        "account",
        "user_id",
        "role",
        "symbol",
        "position_side",
        "quantity",
        "entry_price",
        "mark_price",
        "liquidation_price",
        "bankruptcy_price",
        "realized_pnl",
        "released_margin",
        "maintenance_margin",
        "margin_buffer",
        "risk_status",
        "insurance_covered",
        "residual_bad_debt",
        "adl_status",
        "clearing_event_bucket",
        "clearing_event_label",
        "clearing_verification_path",
        "clearing_boundary_note",
        "adl_covered",
        "adl_residual",
        "reason",
        "event_id",
      ],
      filteredLiquidationEvents.map(({ user, event }) => {
        const bucket = clearingEventBucketFromLiquidation(event);
        return [
          bjDateTime(event.liquidated_at),
          bjDateTime(event.created_at),
          user.username,
          user.id,
          user.role,
          event.symbol,
          event.position_side,
          event.quantity,
          event.entry_price,
          event.mark_price,
          event.liquidation_price,
          event.bankruptcy_price,
          event.realized_pnl,
          event.released_margin,
          event.maintenance_margin,
          event.margin_buffer,
          event.risk_status,
          event.insurance_covered,
          event.residual_bad_debt,
          event.adl_status,
          bucket,
          clearingEventBucketLabel(bucket),
          clearingEventVerificationPath(bucket),
          clearingEventBoundaryNote(bucket),
          event.adl_covered,
          event.adl_residual,
          event.reason,
          event.event_id,
        ];
      }),
    );
  };
  const exportAdlEvents = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_adl_events_${exportClearingScope}_${stamp}.csv`,
      [
        "created_at",
        "account",
        "user_id",
        "symbol",
        "position_side",
        "quantity",
        "entry_price",
        "mark_price",
        "execution_price",
        "realized_pnl",
        "released_margin",
        "bad_debt_before",
        "covered_amount",
        "residual_after",
        "pnl_pct",
        "effective_leverage",
        "rank_score",
        "status",
        "clearing_event_bucket",
        "clearing_event_label",
        "clearing_verification_path",
        "clearing_boundary_note",
        "reason",
        "liquidation_event_id",
        "event_id",
      ],
      filteredAdlEvents.map((item) => {
        const bucket = clearingEventBucketFromAdl(item);
        return [
          bjDateTime(item.created_at),
          item.username ?? "",
          item.user_id,
          item.symbol ?? "",
          item.position_side,
          item.quantity,
          item.entry_price,
          item.mark_price,
          item.execution_price,
          item.realized_pnl,
          item.released_margin,
          item.bad_debt_before,
          item.covered_amount,
          item.residual_after,
          item.pnl_pct,
          item.effective_leverage,
          item.rank_score,
          item.status,
          bucket,
          clearingEventBucketLabel(bucket),
          clearingEventVerificationPath(bucket),
          clearingEventBoundaryNote(bucket),
          item.reason,
          item.liquidation_event_id,
          item.event_id,
        ];
      }),
    );
  };
  const exportInsuranceEvents = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_insurance_events_${exportClearingScope}_${stamp}.csv`,
      [
        "created_at",
        "margin_asset",
        "symbol",
        "event_type",
        "amount",
        "balance_before",
        "balance_after",
        "residual_bad_debt",
        "insurance_event_bucket",
        "insurance_event_label",
        "insurance_verification_path",
        "insurance_boundary_note",
        "user_id",
        "market_id",
        "related_liquidation_event_id",
        "note",
        "event_id",
      ],
      filteredInsuranceEvents.map((item) => {
        const bucket = insuranceEventBucket(item);
        return [
          bjDateTime(item.created_at),
          item.margin_asset,
          item.symbol ?? "",
          item.event_type,
          item.amount,
          item.balance_before,
          item.balance_after,
          item.residual_bad_debt,
          bucket,
          insuranceEventBucketLabel(bucket),
          insuranceEventVerificationPath(bucket),
          insuranceEventBoundaryNote(bucket),
          item.user_id ?? "",
          item.market_id ?? "",
          item.related_liquidation_event_id ?? "",
          item.note ?? "",
          item.event_id,
        ];
      }),
    );
  };
  const contractTabs: { key: ContractAdminTab; label: string; hint: string; badge?: string; tone?: "warn" | "danger" | "neutral" }[] = [
    {
      key: "overview",
      label: "概览",
      hint: "市场状态 / 自动维护",
      badge: maintenanceErrorCount > 0 || sourceIssueCount > 0 ? String(maintenanceErrorCount + sourceIssueCount) : undefined,
      tone: maintenanceErrorCount > 0 ? "danger" : sourceIssueCount > 0 ? "warn" : "neutral",
    },
    { key: "accounts", label: "保证金账户", hint: "客户 / 管理 / 机器人 / 系统", badge: String(accounts.length) },
    {
      key: "positions",
      label: "仓位风险",
      hint: "持仓 / 风险告警",
      badge: activePositions.length > 0 ? String(activePositions.length) : undefined,
      tone: riskAlerts.length > 0 ? "warn" : "neutral",
    },
    {
      key: "orders",
      label: "订单与成交",
      hint: "当前委托 / 历史成交",
      badge: openOrders.length > 0 ? String(openOrders.length) : undefined,
    },
    {
      key: "funding",
      label: "资金费",
      hint: "费率 / 任务 / 结算",
      badge: failedFundingJobCount > 0 ? String(failedFundingJobCount) : undefined,
      tone: failedFundingJobCount > 0 ? "danger" : "neutral",
    },
    {
      key: "liquidation",
      label: "强平 / ADL",
      hint: "强平事件 / 减仓",
      badge: totalAdlResidual > 0 ? fmt(totalAdlResidual, 0) : liquidationEvents.length > 0 ? String(liquidationEvents.length) : undefined,
      tone: totalAdlResidual > 0 ? "danger" : "neutral",
    },
    {
      key: "insurance",
      label: "保险基金",
      hint: "基金余额 / 坏账",
      badge: totalBadDebt > 0 ? fmt(totalBadDebt, 0) : undefined,
      tone: totalBadDebt > 0 ? "warn" : "neutral",
    },
    { key: "risk", label: "风险参数", hint: "交易模式 / 风险阶梯" },
  ];
  return (
    <section className="space-y-4">
      <div className="panel rounded-2xl p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-display text-xl">合约清算</h2>
              <ContractDetailLoadBadge state={detailLoadState} />
            </div>
            <p className="mt-1 text-sm text-slate-400">PERP 保证金、逐仓持仓、资金费、强平和 ADL 独立于现货余额流水；进入本页后关键合约数据每 10 秒自动刷新。</p>
          </div>
          <button type="button" onClick={onRefresh} className="w-fit rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
            刷新
          </button>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
          <SurveillanceMetric label="保证金账户" value={String(accounts.length)} />
          <SurveillanceMetric label="活跃持仓" value={String(activePositions.length)} />
          <SurveillanceMetric label="当前委托" value={String(openOrders.length)} />
          <SurveillanceMetric label="历史委托" value={String(orders.length)} />
          <SurveillanceMetric label="合约成交" value={String(trades.length)} />
          <SurveillanceMetric label="强平事件" value={String(liquidationEvents.length)} />
        </div>
      </div>
      <section className="panel rounded-2xl p-3">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {contractTabs.map((tab) => {
            const active = activeTab === tab.key;
            const badgeClass =
              tab.tone === "danger"
                ? "bg-rose-500/16 text-rose-100"
                : tab.tone === "warn"
                  ? "bg-amber-400/16 text-amber-100"
                  : "bg-white/8 text-slate-300";
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => onTabChange(tab.key)}
                className={`min-h-[58px] rounded-xl px-3 py-2 text-left transition ${
                  active ? "bg-cyan-400/16 text-cyan-100" : "bg-white/6 text-slate-300 hover:bg-white/10"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{tab.label}</span>
                  {tab.badge && <span className={`rounded-full px-2 py-0.5 text-[11px] ${badgeClass}`}>{tab.badge}</span>}
                </div>
                <div className="mt-0.5 text-xs text-slate-500">{tab.hint}</div>
              </button>
            );
          })}
        </div>
      </section>
      {!["overview", "risk"].includes(activeTab) && (
        <ContractDetailFilterBar
          filters={detailFilters}
          symbols={contractSymbols}
          activeTab={activeTab}
          active={detailFiltersActive}
          loading={detailLoadState.loading}
          onFiltersChange={setDetailFilters}
          onBackendRefresh={() => onRefreshDetails(detailFilters)}
          onReset={() => {
            const nextFilters = defaultContractDetailFilters();
            setDetailFilters(nextFilters);
            onRefreshDetails(nextFilters);
          }}
        />
      )}
      {activeTab === "overview" && (
        <>
          <ContractClearingBoundaryStrip
            accountsCount={accounts.length}
            activePositionsCount={activePositions.length}
            openOrdersCount={openOrders.length}
            failedFundingJobCount={failedFundingJobCount}
            liquidationCount={liquidationEvents.length}
            adlResidual={totalAdlResidual}
            insuranceEventCount={insuranceEvents.length}
            totalBadDebt={totalBadDebt}
            riskTierCount={riskTierExportCount}
            marketCount={contractMarkets.length}
            sourceIssueCount={sourceIssueCount}
            onTabChange={onTabChange}
          />
          <section className="panel rounded-2xl p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="font-display text-lg">合约运营摘要</h3>
                <p className="mt-1 text-sm text-slate-400">首屏只保留需要马上判断的市场、资金和风险状态；明细进入上方分区。</p>
              </div>
              <span className={`rounded-full px-3 py-1 text-xs ${maintenanceErrorCount > 0 ? "bg-rose-500/16 text-rose-100" : sourceIssueCount > 0 || riskAlerts.length > 0 ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
                {maintenanceErrorCount > 0 ? "需处理" : sourceIssueCount > 0 || riskAlerts.length > 0 ? "需关注" : "正常"}
              </span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
              <SurveillanceMetric label="PERP 市场" value={String(marketStates.length)} />
              <SurveillanceMetric label="价格源异常" value={String(sourceIssueCount)} />
              <SurveillanceMetric label="风险告警" value={String(riskAlerts.length)} />
              <SurveillanceMetric label="失败资金费任务" value={String(failedFundingJobCount)} />
              <SurveillanceMetric label="保险坏账" value={fmt(totalBadDebt, 4)} />
              <SurveillanceMetric label="ADL剩余" value={fmt(totalAdlResidual, 4)} />
              <SurveillanceMetric label="钱包合计" value={fmt(totalWallet, 2)} />
              <SurveillanceMetric label="占用保证金" value={fmt(totalUsedMargin, 2)} />
              <SurveillanceMetric label="未实现PnL" value={fmt(totalUnrealizedPnl, 2)} />
              <SurveillanceMetric label="当前委托" value={String(openOrders.length)} />
              <SurveillanceMetric label="资金费任务" value={String(fundingJobs.length)} />
              <SurveillanceMetric label="维护错误" value={String(maintenanceErrorCount)} />
            </div>
          </section>
	          <ContractClearingQuickPaths
	            accountsCount={accounts.length}
	            activePositionsCount={activePositions.length}
	            riskAlertCount={riskAlerts.length}
	            openOrdersCount={openOrders.length}
	            fundingJobCount={fundingJobs.length}
	            failedFundingJobCount={failedFundingJobCount}
	            liquidationCount={liquidationEvents.length}
	            totalAdlResidual={totalAdlResidual}
	            totalBadDebt={totalBadDebt}
	            riskTierCount={riskTierExportCount}
	            sourceIssueCount={sourceIssueCount}
	            maintenanceErrorCount={maintenanceErrorCount}
	            onTabChange={onTabChange}
	          />
          <ContractMarketStateConsole
            marketStates={marketStates}
            onRefresh={onRefresh}
            onRefreshMarketState={onRefreshMarketState}
            onSettleFunding={onSettleFunding}
          />
        </>
      )}
      <div className="space-y-4">
        {activeTab === "overview" && (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-display text-lg">自动维护</h3>
            <p className="mt-1 text-sm text-slate-400">后台自动刷新合约价格状态、按 funding time 结算资金费率、扫描接近强平的持仓；fallback 合约盘口默认由机器人治理或手动 seed 工具接管。</p>
          </div>
          <span className={`w-fit rounded-full px-2.5 py-1 text-xs ${maintenanceErrorCount > 0 ? "bg-amber-400/14 text-amber-100" : "bg-emerald-400/14 text-emerald-100"}`}>
            {maintenanceErrorCount > 0 ? "warning" : "running"}
          </span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <SurveillanceMetric label="最近运行" value={bjDateTime(maintenance.metrics?.updated_at)} />
          <SurveillanceMetric label="价格刷新" value={`${maintenance.metrics?.price_refreshes ?? 0} / ${maintenance.metrics?.markets ?? 0}`} />
          <SurveillanceMetric label="自动资金费" value={maintenance.metrics?.auto_settle_enabled ? "enabled" : "disabled"} />
          <SurveillanceMetric label="本轮结算" value={String(maintenanceFundingSettlements.length)} />
          <SurveillanceMetric label="资金费任务失败" value={String(maintenance.metrics?.funding_job_failures ?? 0)} />
          <SurveillanceMetric label="自动强平" value={maintenance.metrics?.auto_liquidation_enabled ? "enabled" : "disabled"} />
          <SurveillanceMetric label="本轮强平" value={String(maintenance.metrics?.liquidation_count ?? liquidationSettlements.length)} />
          <SurveillanceMetric label="自动ADL" value={maintenance.metrics?.auto_adl_enabled ? "enabled" : "disabled"} />
          <SurveillanceMetric label="本轮ADL" value={String(maintenance.metrics?.adl_event_count ?? 0)} />
          <SurveillanceMetric label="风险告警" value={String(maintenance.metrics?.risk_alert_count ?? riskAlerts.length)} />
          <SurveillanceMetric label="fallback seed" value={`${liquidity?.reseeded_markets ?? 0} / ${liquidity?.markets ?? 0}`} />
          <SurveillanceMetric label="实例接管" value={String(liquidity?.skipped_markets ?? 0)} />
          <SurveillanceMetric label="seed 挂单" value={`+${liquidity?.placed_orders ?? 0} / -${liquidity?.canceled_orders ?? 0}`} />
          <SurveillanceMetric label="seed 拒单" value={String(liquidity?.rejected_orders ?? 0)} />
        </div>
        {(liquidity?.items?.length ?? 0) > 0 && (
          <div className="mt-3 rounded-xl border border-cyan-400/12 bg-cyan-400/6 px-3 py-2 text-xs leading-5 text-cyan-50/85">
            {liquidity?.items?.map((item) => item.status === "managed_instance_running" ? `${item.symbol} 托管实例运行中，内置重铺已跳过` : `${item.symbol} mid ${item.mid_price} 挂 ${item.placed_orders} 撤 ${item.canceled_orders}`).join("；")}
          </div>
        )}
        {maintenanceFundingSettlements.length > 0 && (
          <div className="mt-3 rounded-xl border border-violet-400/12 bg-violet-400/6 px-3 py-2 text-xs leading-5 text-violet-50/85">
            {maintenanceFundingSettlements.map((item) => `${item.symbol} ${bjDateTime(item.funding_time)} ${item.settled_count} 笔 @ ${item.funding_rate}`).join("；")}
          </div>
        )}
        {maintenanceFundingJobs.length > 0 && (
          <div className="mt-3 rounded-xl border border-indigo-400/12 bg-indigo-400/6 px-3 py-2 text-xs leading-5 text-indigo-50/85">
            {maintenanceFundingJobs.map((item) => `${item.symbol} ${bjDateTime(item.funding_time)} ${item.status} #${item.attempt_count}/${item.max_attempts}`).join("；")}
          </div>
        )}
        {riskAlerts.length > 0 && (
          <div className="mt-3 rounded-xl border border-amber-400/12 bg-amber-400/6 px-3 py-2 text-xs leading-5 text-amber-50/85">
            {riskAlerts.slice(0, 4).map((item) => `${item.symbol} ${item.username} ${item.side} ${item.risk_status} 距强平 ${fmt(Number(item.liquidation_distance_pct) * 100, 2)}%`).join("；")}
          </div>
        )}
        {liquidationSettlements.length > 0 && (
          <div className="mt-3 rounded-xl border border-rose-400/12 bg-rose-400/6 px-3 py-2 text-xs leading-5 text-rose-50/85">
            {liquidationSettlements.map((item) => `${item.symbol} #${item.user_id} ${item.position_side} ${fmt(item.quantity, 8)} @ ${fmt(item.mark_price, 4)} PnL ${fmt(item.realized_pnl, 4)}`).join("；")}
          </div>
        )}
        {maintenanceAdlEvents.length > 0 && (
          <div className="mt-3 rounded-xl border border-orange-400/12 bg-orange-400/6 px-3 py-2 text-xs leading-5 text-orange-50/85">
            {maintenanceAdlEvents.map((item) => `${item.symbol} ${item.status} ${item.event_count} 笔 覆盖 ${fmt(item.covered_amount, 4)} 剩余 ${fmt(item.residual_after, 4)}`).join("；")}
          </div>
        )}
        {(maintenance.metrics?.errors?.length ?? 0) > 0 && (
          <div className="mt-3 rounded-xl border border-rose-400/12 bg-rose-400/6 px-3 py-2 text-xs leading-5 text-rose-50/85">
            {maintenance.metrics?.errors?.map((item) => `${item.symbol}: ${item.error}`).join("；")}
          </div>
        )}
        {(liquidity?.errors?.length ?? 0) > 0 && (
          <div className="mt-3 rounded-xl border border-rose-400/12 bg-rose-400/6 px-3 py-2 text-xs leading-5 text-rose-50/85">
            {liquidity?.errors?.map((item) => `${item.symbol}: ${item.error}`).join("；")}
          </div>
        )}
      </section>
        )}
        {activeTab === "insurance" && (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h3 className="font-display text-lg">保险基金与坏账审计</h3>
            <p className="mt-1 text-sm text-slate-400">强平亏穿钱包时优先从合约保险基金覆盖，未覆盖部分留为 residual bad debt，供后续 ADL 或人工接管。</p>
          </div>
        </div>
	        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
	          {insuranceFunds.map((fund) => (
	            <SurveillanceMetric
	              key={fund.margin_asset}
              label={`${fund.margin_asset} 保险基金`}
              value={`${fmt(fund.balance, 4)} ${fund.margin_asset}`}
              hint={bjDateTime(fund.updated_at)}
            />
          ))}
          {insuranceFunds.length === 0 && <SurveillanceMetric label="保险基金" value="0 USDT" hint="暂无余额记录" />}
          <SurveillanceMetric label="保险基金流水" value={String(insuranceEvents.length)} />
          <SurveillanceMetric
            label="未覆盖坏账"
            value={fmt(insuranceEvents.reduce((sum, item) => sum + Number(item.residual_bad_debt || 0), 0), 4)}
          />
          <SurveillanceMetric label="ADL事件" value={String(adlEvents.length)} />
	          <SurveillanceMetric
	            label="ADL剩余"
	            value={fmt(liquidationEvents.reduce((sum, item) => sum + Number(item.event.adl_residual || 0), 0), 4)}
	          />
	        </div>
        <div className="mt-4 rounded-2xl border border-rose-400/18 bg-rose-500/6 p-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="max-w-3xl">
              <div className="text-sm font-semibold text-rose-100">低频危险动作：保险基金调账</div>
              <p className="mt-1 text-xs leading-5 text-rose-100/75">
                仅用于测试清算演练或人工纠偏。执行前先核对基金余额、未覆盖坏账、关联强平和系统操作记录；提交仍会二次确认，并由后端 `confirm_execute` 拦截未确认请求。
              </p>
            </div>
            <div className="flex w-full flex-col gap-2 sm:flex-row lg:w-auto">
              <input
                value={insuranceAdjustAmount}
                onChange={(event) => onInsuranceAdjustAmountChange(event.target.value)}
                className="min-w-0 rounded-2xl border border-rose-200/20 bg-slate-950/45 px-3 py-2 font-mono text-sm text-rose-50 outline-none focus:border-rose-200/45 sm:w-36"
                placeholder="USDT 调账"
              />
              <button
                type="button"
                disabled={insuranceAdjusting}
                onClick={onAdjustInsuranceFund}
                className="rounded-2xl bg-rose-500/18 px-4 py-2 text-sm text-rose-50 hover:bg-rose-500/24 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {insuranceAdjusting ? "处理中..." : "调整保险基金"}
              </button>
            </div>
          </div>
        </div>
	        <div className="mt-4">
	          <InsuranceEventReviewStrip rows={insuranceEventRows} />
	        </div>
		        <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
		          <h4 className="text-sm font-semibold text-slate-200">保险基金流水</h4>
	          <div className="flex flex-wrap items-center gap-2">
	            <button
	              type="button"
	              onClick={exportInsuranceEvents}
	              disabled={filteredInsuranceEvents.length === 0}
	              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前基金流水 CSV
	            </button>
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredInsuranceEvents.length, insuranceEvents.length, detailFiltersActive)}
	            </span>
	          </div>
	        </div>
	        <AuditTablePager
	          page={safeInsuranceEventsPage}
	          total={filteredInsuranceEvents.length}
	          pageSize={AUDIT_TABLE_PAGE_SIZE}
	          label="保险基金流水分页"
	          onPageChange={setInsuranceEventsPage}
	        />
	        <div className="mt-4 overflow-auto">
          <table className="min-w-[1060px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">资产</th>
                <th className="pb-2">类型</th>
                <th className="pb-2">金额</th>
                <th className="pb-2">余额变化</th>
                <th className="pb-2">未覆盖坏账</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">关联强平</th>
                <th className="pb-2">备注</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleInsuranceEvents.map((item) => {
                const amount = Number(item.amount);
                const residual = Number(item.residual_bad_debt || 0);
                const amountClass = amount > 0 ? "text-emerald-300" : amount < 0 ? "text-rose-300" : "text-slate-200";
                return (
                  <tr key={item.event_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.created_at)}</td>
                    <td className="py-2 pr-3">{item.margin_asset}</td>
                    <td className="py-2 pr-3">{item.event_type}</td>
                    <td className={`py-2 pr-3 ${amountClass}`}>{fmt(item.amount, 6)}</td>
                    <td className="py-2 pr-3">{fmt(item.balance_before, 4)} → {fmt(item.balance_after, 4)}</td>
                    <td className={`py-2 pr-3 ${residual > 0 ? "text-amber-200" : ""}`}>{fmt(item.residual_bad_debt, 6)}</td>
                    <td className="py-2 pr-3">{item.symbol ?? "-"}</td>
                    <td className="py-2 pr-3">{item.user_id ? `#${item.user_id}` : "-"}</td>
                    <td className="max-w-[170px] truncate py-2 pr-3 text-slate-400">{item.related_liquidation_event_id ?? "-"}</td>
                    <td className="max-w-[180px] truncate py-2 pr-3 text-slate-400">{item.note ?? "-"}</td>
                  </tr>
                );
              })}
	              {filteredInsuranceEvents.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={10}>当前筛选暂无保险基金流水。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
      </section>
        )}
        {activeTab === "risk" && (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-display text-lg">风险限额阶梯</h3>
            <p className="mt-1 text-sm text-slate-400">先看市场级风控开关，再维护按名义价值分层的最大杠杆和维持保证金率；最后一档上限留空表示无限。</p>
          </div>
          <button
            type="button"
            onClick={exportRiskTiers}
            disabled={riskTierExportCount === 0}
            className="w-fit rounded-2xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
          >
            导出当前风险参数 CSV
          </button>
        </div>
        <RiskParameterReviewStrip rows={riskParameterRows} />
        <div className="mb-4 rounded-2xl border border-amber-300/16 bg-amber-300/8 p-3">
          <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h4 className="text-sm font-semibold text-amber-100">市场级风控开关</h4>
              <p className="mt-1 text-xs leading-5 text-amber-100/80">
                这里是清算域的只读索引；`contract_trading_mode` 的编辑入口仍在市场运营，source-of-truth 仍是市场配置。
              </p>
            </div>
            <span className="w-fit rounded-full bg-white/8 px-2.5 py-1 text-xs text-slate-300">{contractMarkets.length} 个 PERP 市场</span>
          </div>
          <div className="grid gap-2 lg:grid-cols-2">
            {contractMarkets.map((market) => (
              <div key={`${market.symbol}-risk-mode`} className="rounded-xl border border-white/8 bg-slate-950/25 p-3">
                <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-mono text-sm text-slate-100">{market.symbol}</div>
                    <div className="mt-1 text-xs text-slate-500">{market.margin_asset ?? market.quote_asset} 保证金 · {market.is_active ? "市场启用" : "市场暂停"}</div>
                  </div>
                  <span className={`rounded-full px-2.5 py-1 text-xs ${contractTradingModeClass(market.contract_trading_mode)}`}>
                    {contractTradingModeLabel(market.contract_trading_mode)}
                  </span>
                </div>
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                  <SurveillanceMetric label="最大杠杆" value={`${fmt(market.max_leverage, 2)}x`} />
                  <SurveillanceMetric label="默认杠杆" value={`${fmt(market.default_leverage, 2)}x`} />
                  <SurveillanceMetric label="维持保证金率" value={`${fmt(Number(market.maintenance_margin_rate || 0) * 100, 4)}%`} />
                  <SurveillanceMetric label="资金费模式" value={market.funding_rate_mode ?? "-"} />
                </div>
                <div className="mt-3 rounded-xl bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-400">
                  {contractTradingModeDescription(market.contract_trading_mode)}
                </div>
                <button
                  type="button"
                  onClick={() => onOpenMarketProductConfig(market.symbol)}
                  className="mt-3 rounded-xl bg-cyan-400/16 px-3 py-1.5 text-xs text-cyan-100"
                >
                  去市场运营编辑
                </button>
              </div>
            ))}
            {contractMarkets.length === 0 && (
              <div className="rounded-xl border border-white/8 bg-white/5 px-3 py-6 text-center text-sm text-slate-500">
                暂无 PERP 市场配置。
              </div>
            )}
          </div>
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          {marketStates.map((state) => {
            const rows = riskTierEdits[state.symbol] ?? [];
            return (
              <div key={`${state.symbol}-risk-tiers`} className="rounded-2xl border border-white/8 bg-white/5 p-3">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-mono text-slate-100">{state.symbol}</div>
                    <div className="mt-1 text-xs text-slate-500">{rows.length} 档 · 按当前/目标名义价值选档</div>
                  </div>
                </div>
                <div className="overflow-auto">
                  <table className="min-w-[780px] text-left text-xs">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="pb-2">Tier</th>
                        <th className="pb-2">名义下限</th>
                        <th className="pb-2">名义上限</th>
                        <th className="pb-2">最大杠杆</th>
                        <th className="pb-2">维持率</th>
                        <th className="pb-2">维持扣减</th>
                        <th className="pb-2">操作</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono text-slate-200">
                      {rows.map((row, index) => (
                        <tr key={`${state.symbol}-tier-${index}`} className="border-t border-white/8">
                          <td className="py-2 pr-2">T{index + 1}</td>
                          <td className="py-2 pr-2">
                            <input
                              value={row.notional_floor}
                              onChange={(event) => onRiskTierChange(state.symbol, index, "notional_floor", event.target.value)}
                              className="w-28 rounded-xl border border-white/10 bg-slate-950/40 px-2 py-1.5 outline-none"
                            />
                          </td>
                          <td className="py-2 pr-2">
                            <input
                              value={row.notional_cap}
                              onChange={(event) => onRiskTierChange(state.symbol, index, "notional_cap", event.target.value)}
                              placeholder="∞"
                              className="w-28 rounded-xl border border-white/10 bg-slate-950/40 px-2 py-1.5 outline-none"
                            />
                          </td>
                          <td className="py-2 pr-2">
                            <input
                              value={row.max_leverage}
                              onChange={(event) => onRiskTierChange(state.symbol, index, "max_leverage", event.target.value)}
                              className="w-20 rounded-xl border border-white/10 bg-slate-950/40 px-2 py-1.5 outline-none"
                            />
                          </td>
                          <td className="py-2 pr-2">
                            <input
                              value={row.maintenance_margin_rate}
                              onChange={(event) => onRiskTierChange(state.symbol, index, "maintenance_margin_rate", event.target.value)}
                              className="w-24 rounded-xl border border-white/10 bg-slate-950/40 px-2 py-1.5 outline-none"
                            />
                          </td>
                          <td className="py-2 pr-2">
                            <input
                              value={row.maintenance_amount}
                              onChange={(event) => onRiskTierChange(state.symbol, index, "maintenance_amount", event.target.value)}
                              className="w-24 rounded-xl border border-white/10 bg-slate-950/40 px-2 py-1.5 outline-none"
                            />
                          </td>
                          <td className="py-2 pr-2">
                            <button
                              type="button"
                              disabled={rows.length <= 1}
                              onClick={() => onRemoveRiskTier(state.symbol, index)}
                              className="rounded-xl bg-rose-500/14 px-2 py-1 text-rose-100 disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              删除
                            </button>
                          </td>
                        </tr>
                      ))}
                      {rows.length === 0 && (
                        <tr>
                          <td className="py-6 text-center text-sm text-slate-500" colSpan={7}>暂无风险阶梯。</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
                <div className="mt-3 border-t border-amber-300/18 pt-3">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div className="max-w-2xl">
                      <div className="text-sm font-semibold text-amber-100">低频危险动作：风险阶梯维护</div>
                      <p className="mt-1 text-xs leading-5 text-amber-100/75">
                        增加和删除只调整本地草稿；保存会影响 {state.symbol} 的杠杆上限、维持保证金和强平价。执行前先核对阶梯连续性、最后一档上限、当前持仓名义价值和下单拒单影响；提交仍会二次确认，并由后端 `confirm_execute` 拦截未确认请求。
                      </p>
                    </div>
                    <div className="flex w-full flex-col gap-2 sm:flex-row lg:w-auto">
                      <button type="button" onClick={() => onAddRiskTier(state.symbol)} className="rounded-2xl bg-white/8 px-3 py-1.5 text-xs text-slate-100">
                        增加一档
                      </button>
                      <button type="button" onClick={() => onSaveRiskTiers(state.symbol)} className="rounded-2xl bg-amber-400/16 px-3 py-1.5 text-xs text-amber-100">
                        保存阶梯
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>
        )}
        {activeTab === "accounts" && (
      <>
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-lg">合约保证金账户摘要</h3>
            <p className="mt-1 text-sm text-slate-400">这里是清算域里的合约保证金账本索引，只列出已存在的保证金账户，打开本页不会为只使用现货钱包的主体开合约保证金账户；保证金账户按客户 UID、管理主体、SPOT 机器人 UID、PERP 机器人 UID 和系统主体分组。SPOT 机器人出现在这里只表示该 UID 已有合约保证金记录，不代表现货钱包被合并进合约账本。</p>
          </div>
          <span className="rounded-full bg-violet-400/14 px-3 py-1 text-xs text-violet-100">USDT 本位</span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-8">
          <SurveillanceMetric label="全部保证金账户" value={String(accounts.length)} />
          <SurveillanceMetric label="客户保证金" value={String(accountKindCounts.customer)} />
          <SurveillanceMetric label="管理主体" value={String(accountKindCounts.admin)} />
          <SurveillanceMetric label="SPOT 机器人 UID" value={String(accountKindCounts.spot_robot)} />
          <SurveillanceMetric label="PERP 机器人 UID" value={String(accountKindCounts.contract_robot)} />
          <SurveillanceMetric label="系统主体" value={String(accountKindCounts.system)} />
          <SurveillanceMetric label="钱包合计" value={fmt(totalWallet, 4)} />
          <SurveillanceMetric label="占用保证金" value={fmt(totalUsedMargin, 4)} />
        </div>
        <div className="mt-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span className={`rounded-full px-2 py-0.5 text-xs ${contractAccountJudgmentClass}`}>{contractAccountJudgment.status}</span>
              <span className="text-sm text-slate-200">{activeAccountScopeOption.label} · {filteredScopedAccounts.length} 账户</span>
            </div>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
            <div className="mt-1 text-sm text-slate-200">{contractAccountJudgment.focus}</div>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
            <div className={`mt-1 text-sm ${contractAccountJudgment.tone === "danger" ? "text-rose-100" : contractAccountJudgment.tone === "warn" ? "text-amber-100" : "text-slate-400"}`}>
              {contractAccountJudgment.next}
            </div>
          </div>
        </div>
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1 scrollbar">
          {accountScopeOptions.map((option) => {
            const active = accountScope === option.key;
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => onAccountScopeChange(option.key)}
                className={`shrink-0 rounded-xl px-3 py-2 text-left transition ${active ? "bg-violet-400/16 text-violet-100" : "bg-white/6 text-slate-300 hover:bg-white/10"}`}
              >
                <div className="flex items-center gap-2 text-sm font-medium">
                  <span>{option.label}</span>
                  <span className="rounded-full bg-white/8 px-2 py-0.5 text-[11px]">{option.count}</span>
                </div>
                <div className="mt-0.5 text-xs text-slate-500">{option.hint}</div>
              </button>
            );
          })}
        </div>
        {accounts.length === 0 && (
          <div className="mt-4 border-t border-white/8 pt-3">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <div className="text-sm font-semibold text-rose-100">保证金账户来源核查</div>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
                  这个空态通常表示当前运行态还没有任何客户或机器人主体触发合约保证金账户创建，也可能是 PERP 机器人模板未创建或当前只启动了现货/系统测试。先按来源核查，不要在这里手工合并现货钱包或批量开合约保证金账户。
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => onOpenContractTrade(primaryContractSymbol)}
                  className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
                >
                  去合约交易页
                </button>
                <button
                  type="button"
                  onClick={onOpenAccountDirectory}
                  className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
                >
                  查账户目录
                </button>
                <button
                  type="button"
                  onClick={onOpenBotAccounts}
                  className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
                >
                  查机器人账号
                </button>
              </div>
            </div>
            <div className="mt-3 grid gap-2 text-xs text-slate-400 md:grid-cols-3">
              <div>
                <div className="font-medium text-slate-200">合约交易来源</div>
                <div className="mt-1 leading-5">普通客户进入 {primaryContractSymbol} 合约交易或 API 下单后，才应出现客户保证金账户。</div>
              </div>
              <div>
                <div className="font-medium text-slate-200">机器人模板来源</div>
                <div className="mt-1 leading-5">PERP_MM / PERP_FLOW 机器人创建或同步模板后，才应出现合约机器人保证金账户。</div>
              </div>
              <div>
                <div className="font-medium text-slate-200">内部系统来源</div>
                <div className="mt-1 leading-5">seed、fallback、保险基金或清算内部账户只在对应测试或维护流程需要时出现，不能当成客户资产。</div>
              </div>
            </div>
          </div>
        )}
        <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
	          <SurveillanceMetric label="当前显示账户" value={String(filteredScopedAccounts.length)} />
          <SurveillanceMetric label="显示钱包" value={fmt(scopedWallet, 4)} />
          <SurveillanceMetric label="显示可用保证金" value={fmt(scopedAvailableMargin, 4)} />
          <SurveillanceMetric label="显示占用保证金" value={fmt(scopedUsedMargin, 4)} />
          <SurveillanceMetric label="显示未实现PnL" value={fmt(scopedUnrealizedPnl, 4)} />
        </div>
      </section>
      <section className="panel rounded-2xl p-4">
        <h3 className="mb-3 font-display text-lg">合约保证金账户</h3>
        <div className="overflow-auto">
          <table className="min-w-[1040px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">主体</th>
                <th className="pb-2">分类</th>
                <th className="pb-2">钱包</th>
                <th className="pb-2">可用保证金</th>
                <th className="pb-2">占用保证金</th>
                <th className="pb-2">未实现盈亏</th>
                <th className="pb-2">已实现盈亏</th>
                <th className="pb-2">手续费</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {filteredScopedAccounts.map((item) => {
                const kind = contractAccountKindForUser(item.user);
                return (
                  <tr key={item.user.id} className="border-t border-white/8">
                    <td className="py-2 pr-3 font-sans text-slate-100">{item.user.username} <span className="text-xs text-slate-500">#{item.user.id}</span></td>
                    <td className="py-2 pr-3 font-sans">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${contractAccountKindClass(kind)}`}>{contractAccountKindLabel(kind)}</span>
                    </td>
                    <td className="py-2 pr-3">{fmt(item.account.wallet_balance, 4)} {item.account.margin_asset}</td>
                    <td className="py-2 pr-3">{fmt(item.account.available_margin, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.account.used_margin, 4)}</td>
                    <td className={`py-2 pr-3 ${Number(item.account.unrealized_pnl) > 0 ? "text-emerald-300" : Number(item.account.unrealized_pnl) < 0 ? "text-rose-300" : ""}`}>{fmt(item.account.unrealized_pnl, 4)}</td>
                    <td className={`py-2 pr-3 ${Number(item.account.realized_pnl) > 0 ? "text-emerald-300" : Number(item.account.realized_pnl) < 0 ? "text-rose-300" : ""}`}>{fmt(item.account.realized_pnl, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.account.total_fees, 6)}</td>
                  </tr>
                );
              })}
	              {filteredScopedAccounts.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={8}>当前筛选暂无合约保证金账户。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
	      </section>
      <MarginLedgerReviewStrip rows={marginLedgerRows} />
		      <section className="panel rounded-2xl p-4">
		        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
		          <h3 className="font-display text-lg">合约保证金流水</h3>
		          <div className="flex flex-wrap items-center gap-2">
		            <button
		              type="button"
		              onClick={exportContractLedger}
		              disabled={filteredScopedLedgerEntries.length === 0}
		              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
		            >
		              导出当前流水 CSV
		            </button>
		            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
		              {contractFilteredCountText(filteredScopedLedgerEntries.length, scopedLedgerEntries.length, detailFiltersActive)}
		            </span>
		          </div>
		        </div>
            <AuditTablePager
              page={safeLedgerPage}
              total={filteredScopedLedgerEntries.length}
              pageSize={AUDIT_TABLE_PAGE_SIZE}
              label="保证金流水分页"
              onPageChange={setLedgerPage}
            />
		        <div className="overflow-auto">
          <table className="min-w-[1180px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">类型</th>
                <th className="pb-2">金额</th>
                <th className="pb-2">钱包</th>
                <th className="pb-2">可用保证金</th>
                <th className="pb-2">占用保证金</th>
                <th className="pb-2">关联</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleScopedLedgerEntries.map((item) => {
                const amount = Number(item.entry.amount);
                const amountClass = amount > 0 ? "text-emerald-300" : amount < 0 ? "text-rose-300" : "text-cyan-200";
                const related = item.entry.related_trade_id ?? item.entry.related_order_id ?? item.entry.related_event_id ?? "-";
                return (
                  <tr key={item.entry.entry_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.entry.created_at)}</td>
                    <td className="py-2 pr-3 font-sans text-slate-100">{item.user.username} <span className="text-xs text-slate-500">#{item.user.id}</span></td>
                    <td className="py-2 pr-3">{item.entry.symbol ?? "-"}</td>
                    <td className="py-2 pr-3">{item.entry.change_type}</td>
                    <td className={`py-2 pr-3 ${amountClass}`}>{fmt(item.entry.amount, 6)} {item.entry.margin_asset}</td>
                    <td className="py-2 pr-3">{fmt(item.entry.wallet_before, 4)} → {fmt(item.entry.wallet_after, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.entry.available_before, 4)} → {fmt(item.entry.available_after, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.entry.used_margin_before, 4)} → {fmt(item.entry.used_margin_after, 4)}</td>
                    <td className="max-w-[220px] truncate py-2 pr-3 text-slate-400">{related}</td>
                  </tr>
                );
              })}
	              {filteredScopedLedgerEntries.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={9}>当前筛选暂无合约保证金流水。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
      </section>
      </>
        )}
        {activeTab === "positions" && (
      <>
      <PositionRiskReviewStrip rows={positionRiskRows} />
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-lg">仓位风险概览</h3>
            <p className="mt-1 text-sm text-slate-400">按当前合约持仓聚合风险状态，具体逐仓指标在下方持仓卡片中查看。</p>
          </div>
          <span className={`rounded-full px-3 py-1 text-xs ${riskAlerts.length > 0 || stressedPositions.length > 0 ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
            {riskAlerts.length > 0 || stressedPositions.length > 0 ? "需关注" : "正常"}
          </span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          <SurveillanceMetric label="活跃仓位" value={String(activePositions.length)} />
          <SurveillanceMetric label="风险仓位" value={String(stressedPositions.length)} />
          <SurveillanceMetric label="维护告警" value={String(riskAlerts.length)} />
          <SurveillanceMetric label="最近强平距离" value={closestLiquidationDistance === null ? "-" : `${fmt(closestLiquidationDistance * 100, 2)}%`} />
          <SurveillanceMetric label="未实现PnL" value={fmt(totalUnrealizedPnl, 4)} />
        </div>
        {riskAlerts.length > 0 && (
          <div className="mt-3 rounded-xl border border-amber-400/12 bg-amber-400/6 px-3 py-2 text-xs leading-5 text-amber-50/85">
            {riskAlerts.slice(0, 6).map((item) => `${item.symbol} ${item.username} ${item.side} ${item.risk_status} 距强平 ${fmt(Number(item.liquidation_distance_pct) * 100, 2)}%`).join("；")}
          </div>
        )}
	      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
	          <h3 className="font-display text-lg">合约持仓</h3>
	          <div className="flex flex-wrap items-center gap-2">
	            <button
	              type="button"
	              onClick={exportPositions}
	              disabled={filteredPositions.length === 0}
	              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前仓位 CSV
	            </button>
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredPositions.length, positions.length, detailFiltersActive)}
	            </span>
	          </div>
	        </div>
	        {filteredPositions.length > AUDIT_TABLE_PAGE_SIZE && (
	          <div className="mb-3">
	            <AuditTablePager
	              page={safePositionsPage}
	              total={filteredPositions.length}
	              pageSize={AUDIT_TABLE_PAGE_SIZE}
	              label="仓位风险分页"
	              onPageChange={setPositionsPage}
	            />
	          </div>
	        )}
	        <div className="grid gap-3 xl:grid-cols-2">
	          {visiblePositions.map((item) => (
            <div key={`${item.user.id}-${item.position.symbol}-${item.position.side}`} className="rounded-2xl border border-white/8 bg-white/5 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="font-mono text-slate-100">{item.position.symbol}</div>
                  <div className="mt-1 text-xs text-slate-500">{item.user.username} · {item.user.role}</div>
                </div>
                <span className={`rounded-full px-2.5 py-1 text-xs ${item.position.side === "long" ? "bg-emerald-400/14 text-emerald-100" : item.position.side === "short" ? "bg-rose-500/14 text-rose-100" : "bg-white/8 text-slate-300"}`}>
                  {item.position.side}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <SurveillanceMetric label="数量" value={fmt(item.position.quantity, 8)} />
                <SurveillanceMetric label="均价" value={fmt(item.position.entry_price, 6)} />
                <SurveillanceMetric label="标记价" value={fmt(item.position.mark_price, 6)} />
                <SurveillanceMetric label="杠杆" value={`${fmt(item.position.leverage, 2)}x`} />
                <SurveillanceMetric label="保证金" value={fmt(item.position.isolated_margin, 4)} />
                <SurveillanceMetric label="强平价" value={fmt(item.position.liquidation_price, 6)} />
                <SurveillanceMetric label="未实现" value={fmt(item.position.unrealized_pnl, 4)} />
                <SurveillanceMetric label="已实现" value={fmt(item.position.realized_pnl, 4)} />
                <SurveillanceMetric label="风险阶梯" value={`T${item.position.risk_tier ?? 1} / ${fmt(item.position.risk_max_leverage ?? item.position.leverage, 2)}x`} />
                <SurveillanceMetric label="维持率" value={`${fmt(Number(item.position.maintenance_margin_rate ?? 0) * 100, 4)}%`} />
                <SurveillanceMetric label="维持扣减" value={fmt(item.position.maintenance_amount ?? 0, 4)} />
                <SurveillanceMetric label="风险状态" value={item.position.risk_status ?? "-"} />
                <SurveillanceMetric label="距强平" value={`${fmt(Number(item.position.liquidation_distance_pct ?? 0) * 100, 2)}%`} />
                <SurveillanceMetric label="保证金缓冲" value={fmt(item.position.margin_buffer, 4)} />
              </div>
            </div>
          ))}
		          {filteredPositions.length === 0 && (
		            <div className="rounded-2xl border border-white/8 bg-white/5 px-4 py-5 text-sm text-slate-400 xl:col-span-2">
		              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
		                <div>
		                  <div className="font-semibold text-slate-100">仓位风险空态核查</div>
		                  <p className="mt-1 max-w-3xl text-xs leading-5">
		                    {positions.length === 0
		                      ? "当前没有已加载合约持仓，通常表示还没有合约成交形成敞口、保证金账户为空，或当前只在做现货/机器人测试。先核对保证金账户和合约订单成交，不要把空仓误读成清算链路已经完整验收。"
		                      : "当前筛选没有命中持仓；先放宽市场、UID 或关键字，再判断是否真的没有合约敞口。"}
		                  </p>
		                </div>
		                <div className="flex flex-wrap gap-2">
		                  <button
		                    type="button"
		                    onClick={onOpenContractAccounts}
		                    className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
		                  >
		                    核对保证金账户
		                  </button>
		                  <button
		                    type="button"
		                    onClick={onOpenContractOrders}
		                    className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-cyan-400/16 hover:text-cyan-100"
		                  >
		                    查合约订单成交
		                  </button>
		                  <button
		                    type="button"
		                    onClick={() => onOpenContractTrade(primaryContractSymbol)}
		                    className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
		                  >
		                    去合约交易页
		                  </button>
		                </div>
		              </div>
		            </div>
		          )}
		        </div>
		      </section>
      </>
        )}
        {activeTab === "orders" && (
      <>
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-lg">订单与成交摘要</h3>
            <p className="mt-1 text-sm text-slate-400">先看活动委托和最近成交，再进入下方明细表核对单据。</p>
          </div>
          <span className="rounded-full bg-cyan-400/14 px-3 py-1 text-xs text-cyan-100">只读审计</span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <SurveillanceMetric label="当前委托" value={String(openOrders.length)} />
          <SurveillanceMetric label="历史委托" value={String(orders.length)} />
          <SurveillanceMetric label="合约成交" value={String(trades.length)} />
          <SurveillanceMetric label="最近成交" value={bjDateTime(trades[0]?.ts ?? trades[0]?.executed_at)} />
        </div>
	      </section>
      <ContractClearingOrderTradeReviewStrip rows={contractClearingOrderTradeRows} />
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
	          <h3 className="font-display text-lg">合约委托</h3>
	          <div className="flex flex-wrap items-center gap-2">
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredOrders.length, orders.length, detailFiltersActive)}
	            </span>
	            <button
	              type="button"
	              onClick={exportContractOrders}
	              disabled={filteredOrders.length === 0}
	              className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前合约委托 CSV
	            </button>
	          </div>
	        </div>
	        <AuditTablePager
	          page={safeContractOrdersPage}
	          total={filteredOrders.length}
	          pageSize={AUDIT_TABLE_PAGE_SIZE}
	          label="合约委托分页"
	          onPageChange={setContractOrdersPage}
	        />
	        <div className="overflow-auto">
          <table className="min-w-[1000px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">方向</th>
                <th className="pb-2">语义</th>
                <th className="pb-2">价格</th>
                <th className="pb-2">数量</th>
                <th className="pb-2">状态</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleContractOrders.map((item) => (
	                <tr key={item.order.order_id} className="border-t border-white/8">
                  <td className="py-2 pr-3">{bjDateTime(item.order.created_at)}</td>
                  <td className="py-2 pr-3 font-sans text-slate-100">{item.user.username}</td>
                  <td className="py-2 pr-3">{item.order.symbol}</td>
                  <td className={`py-2 pr-3 ${item.order.side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{item.order.side}</td>
                  <td className="py-2 pr-3">{item.order.position_action ?? "-"}{item.order.reduce_only ? " / reduce" : ""}</td>
                  <td className="py-2 pr-3">{fmt(item.order.price ?? item.order.avg_price, 6)}</td>
                  <td className="py-2 pr-3">{fmt(item.order.filled_quantity, 8)} / {fmt(item.order.quantity, 8)}</td>
	                  <td className="py-2 pr-3">{item.order.status}</td>
	                </tr>
	              ))}
	              {filteredOrders.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={8}>当前筛选暂无合约委托。</td>
	                </tr>
	              )}
	            </tbody>
	          </table>
	        </div>
	      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
	          <h3 className="font-display text-lg">合约成交</h3>
	          <div className="flex flex-wrap items-center gap-2">
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredTrades.length, trades.length, detailFiltersActive)}
	            </span>
	            <button
	              type="button"
	              onClick={exportContractTrades}
	              disabled={filteredTrades.length === 0}
	              className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前合约成交 CSV
	            </button>
	          </div>
	        </div>
	        <AuditTablePager
	          page={safeContractTradesPage}
	          total={filteredTrades.length}
	          pageSize={AUDIT_TABLE_PAGE_SIZE}
	          label="合约成交分页"
	          onPageChange={setContractTradesPage}
	        />
	        <div className="overflow-auto">
          <table className="min-w-[920px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">方向</th>
                <th className="pb-2">价格</th>
                <th className="pb-2">数量</th>
                <th className="pb-2">名义</th>
                <th className="pb-2">手续费</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleContractTrades.map((item) => (
	                <tr key={item.trade_id} className="border-t border-white/8">
                  <td className="py-2 pr-3">{bjDateTime(item.ts ?? item.executed_at)}</td>
                  <td className="py-2 pr-3">{item.symbol}</td>
                  <td className={`py-2 pr-3 ${item.side === "buy" || item.taker_side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{item.side ?? item.taker_side ?? "-"}</td>
                  <td className="py-2 pr-3">{fmt(item.price, 6)}</td>
                  <td className="py-2 pr-3">{fmt(item.quantity, 8)}</td>
                  <td className="py-2 pr-3">{fmt(item.quote_amount, 4)}</td>
	                  <td className="py-2 pr-3">{fmt(item.fee, 6)} {item.fee_asset ?? ""}</td>
	                </tr>
	              ))}
	              {filteredTrades.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={7}>当前筛选暂无合约成交。</td>
	                </tr>
	              )}
	            </tbody>
	          </table>
        </div>
      </section>
      </>
        )}
        {activeTab === "funding" && (
      <>
      <FundingOpsReviewStrip rows={fundingOpsRows} />
	      <ContractMarketStateConsole
	        marketStates={marketStates}
	        onRefresh={onRefresh}
	        onRefreshMarketState={onRefreshMarketState}
	        onSettleFunding={onSettleFunding}
	        showFundingSettlementAction
	      />
	      {fundingRecordsVisibleCount === 0 && (
	        <section className="panel rounded-2xl p-4">
	          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
	            <div>
	              <h3 className="font-display text-lg">资金费空态核查</h3>
	              <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-400">
	                {fundingRecordsTotalCount === 0
	                  ? "当前没有已加载资金费任务、结算水位或逐账户记录，通常表示还没有到资金费结算窗口、当前没有合约持仓，或当前测试环境未启动资金费维护任务。先核对仓位、保证金账户和合约订单成交，不要把空表误读为资金费链路已经完整跑过。"
	                  : "当前筛选没有命中资金费记录；先放宽市场、状态或关键字，再判断是否真的没有待处理资金费。"}
	              </p>
	            </div>
	            <div className="flex flex-wrap gap-2">
	              <button
	                type="button"
	                onClick={onOpenContractPositions}
	                className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
	              >
	                看仓位风险
	              </button>
	              <button
	                type="button"
	                onClick={onOpenContractAccounts}
	                className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-violet-400/16 hover:text-violet-100"
	              >
	                核对保证金账户
	              </button>
	              <button
	                type="button"
	                onClick={onOpenContractOrders}
	                className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 transition hover:bg-cyan-400/16 hover:text-cyan-100"
	              >
	                查合约订单成交
	              </button>
	            </div>
	          </div>
	        </section>
	      )}
		      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
		          <div className="flex flex-wrap items-center gap-2">
		            <h3 className="font-display text-lg">资金费任务队列</h3>
		            <button
		              type="button"
		              onClick={exportFundingJobs}
		              disabled={filteredFundingJobs.length === 0}
		              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
		            >
		              导出当前任务 CSV
		            </button>
		            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
		              {contractFilteredCountText(filteredFundingJobs.length, fundingJobs.length, detailFiltersActive)}
		            </span>
		          </div>
	        </div>
        <div className="mb-3 border-t border-rose-300/18 pt-3">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="max-w-2xl">
              <div className="text-sm font-semibold text-rose-100">低频危险动作：资金费任务批量重试</div>
              <p className="mt-1 text-xs leading-5 text-rose-100/75">
                仅在 failed 原因已处理后使用；已有 settlement 的周期不会重复扣款。单条任务可在表格行内逐条重试，批量重试仍会二次确认，并由后端 `confirm_execute` 拦截未确认请求。
              </p>
            </div>
            <button
              type="button"
              disabled={failedFundingJobCount === 0 || batchRetryingFundingJobs}
              onClick={onRetryFailedFundingJobs}
              className="w-full rounded-xl bg-rose-400/16 px-3 py-1.5 text-xs text-rose-100 disabled:cursor-not-allowed disabled:opacity-40 sm:w-fit"
            >
              {batchRetryingFundingJobs ? "重试中..." : `批量重试失败任务 (${failedFundingJobCount})`}
            </button>
          </div>
        </div>
        <AuditTablePager
          page={safeFundingJobsPage}
          total={filteredFundingJobs.length}
          pageSize={AUDIT_TABLE_PAGE_SIZE}
          label="资金费任务分页"
          onPageChange={setFundingJobsPage}
        />
        <div className="overflow-auto">
          <table className="min-w-[1180px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">资金费时间</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">任务状态</th>
                <th className="pb-2">尝试</th>
                <th className="pb-2">Worker / 心跳</th>
                <th className="pb-2">下次重试</th>
                <th className="pb-2">Settlement</th>
                <th className="pb-2">错误</th>
                <th className="pb-2">更新时间</th>
                <th className="pb-2">操作</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleFundingJobs.map((item) => {
                const canRetry = item.status !== "settled";
                return (
                  <tr key={item.job_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.funding_time)}</td>
                    <td className="py-2 pr-3">{item.symbol}</td>
                    <td className="py-2 pr-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${item.status === "settled" ? "bg-emerald-400/14 text-emerald-100" : item.status === "failed" ? "bg-rose-500/14 text-rose-100" : item.status === "running" ? "bg-cyan-400/14 text-cyan-100" : "bg-amber-400/14 text-amber-100"}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3">{item.attempt_count} / {item.max_attempts}</td>
                    <td className="py-2 pr-3">
                      <div>{item.locked_by ?? "-"}</div>
                      <div className="text-xs text-slate-500">{bjDateTime(item.heartbeat_at)}</div>
                    </td>
                    <td className="py-2 pr-3">{bjDateTime(item.next_retry_at)}</td>
                    <td className="py-2 pr-3">{item.settlement_id ?? "-"}</td>
                    <td className="max-w-[260px] truncate py-2 pr-3 text-rose-200/90">{item.last_error ?? "-"}</td>
                    <td className="py-2 pr-3">{bjDateTime(item.updated_at)}</td>
                    <td className="py-2 pr-3">
                      <button
                        type="button"
                        disabled={!canRetry}
                        onClick={() => onRetryFundingJob(item)}
                        className="rounded-xl bg-violet-400/16 px-3 py-1.5 text-xs text-violet-100 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        立即重试
                      </button>
                    </td>
                  </tr>
                );
              })}
	              {filteredFundingJobs.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={10}>当前筛选暂无自动资金费任务。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
	      </section>
		      <section className="panel rounded-2xl p-4">
		        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
		          <h3 className="font-display text-lg">资金费结算水位</h3>
		          <div className="flex flex-wrap items-center gap-2">
		            <button
		              type="button"
		              onClick={exportFundingSettlements}
		              disabled={filteredFundingSettlements.length === 0}
		              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
		            >
		              导出当前结算 CSV
		            </button>
		            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
		              {contractFilteredCountText(filteredFundingSettlements.length, fundingSettlements.length, detailFiltersActive)}
		            </span>
		          </div>
		        </div>
        <AuditTablePager
          page={safeFundingSettlementsPage}
          total={filteredFundingSettlements.length}
          pageSize={AUDIT_TABLE_PAGE_SIZE}
          label="资金费结算分页"
          onPageChange={setFundingSettlementsPage}
        />
        <div className="overflow-auto">
          <table className="min-w-[1040px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">资金费时间</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">状态</th>
                <th className="pb-2">人数</th>
                <th className="pb-2">净额</th>
                <th className="pb-2">费率</th>
                <th className="pb-2">指数 / 标记</th>
                <th className="pb-2">模式</th>
                <th className="pb-2">完成时间</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleFundingSettlements.map((item) => {
                const total = Number(item.total_amount);
                return (
                  <tr key={item.settlement_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.funding_time)}</td>
                    <td className="py-2 pr-3">{item.symbol}</td>
                    <td className="py-2 pr-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${item.status === "settled" ? "bg-emerald-400/14 text-emerald-100" : item.status === "failed" ? "bg-rose-500/14 text-rose-100" : "bg-amber-400/14 text-amber-100"}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3">{item.settled_count}</td>
                    <td className={`py-2 pr-3 ${total > 0 ? "text-emerald-300" : total < 0 ? "text-rose-300" : ""}`}>{fmt(item.total_amount, 6)}</td>
                    <td className="py-2 pr-3">{fmt(Number(item.funding_rate) * 100, 4)}%</td>
                    <td className="py-2 pr-3">{fmt(item.index_price, 4)} / {fmt(item.mark_price, 4)}</td>
                    <td className="py-2 pr-3">{item.funding_rate_mode}</td>
                    <td className="py-2 pr-3">{bjDateTime(item.completed_at ?? item.updated_at)}</td>
                  </tr>
                );
              })}
	              {filteredFundingSettlements.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={9}>当前筛选暂无资金费周期结算水位。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
	      </section>
		      <section className="panel rounded-2xl p-4">
		        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
		          <h3 className="font-display text-lg">资金费率记录</h3>
		          <div className="flex flex-wrap items-center gap-2">
		            <button
		              type="button"
		              onClick={exportFundingEvents}
		              disabled={filteredFundingEvents.length === 0}
		              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
		            >
		              导出当前记录 CSV
		            </button>
		            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
		              {contractFilteredCountText(filteredFundingEvents.length, fundingEvents.length, detailFiltersActive)}
		            </span>
		          </div>
		        </div>
        <AuditTablePager
          page={safeFundingEventsPage}
          total={filteredFundingEvents.length}
          pageSize={AUDIT_TABLE_PAGE_SIZE}
          label="资金费记录分页"
          onPageChange={setFundingEventsPage}
        />
        <div className="overflow-auto">
          <table className="min-w-[980px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">方向</th>
                <th className="pb-2">数量</th>
                <th className="pb-2">指数价</th>
                <th className="pb-2">标记价</th>
                <th className="pb-2">费率</th>
                <th className="pb-2">金额</th>
                <th className="pb-2">模式</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleFundingEvents.map((item) => {
                const amount = Number(item.event.amount);
                return (
                  <tr key={item.event.event_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.event.funding_time)}</td>
                    <td className="py-2 pr-3 font-sans text-slate-100">{item.user.username}</td>
                    <td className="py-2 pr-3">{item.event.symbol}</td>
                    <td className="py-2 pr-3">{item.event.position_side}</td>
                    <td className="py-2 pr-3">{fmt(item.event.quantity, 8)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.index_price, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.mark_price, 4)}</td>
                    <td className="py-2 pr-3">{fmt(Number(item.event.funding_rate) * 100, 4)}%</td>
                    <td className={`py-2 pr-3 ${amount > 0 ? "text-emerald-300" : amount < 0 ? "text-rose-300" : ""}`}>
                      {fmt(item.event.amount, 6)}
                    </td>
                    <td className="py-2 pr-3">{item.event.funding_rate_mode}</td>
                  </tr>
                );
              })}
	              {filteredFundingEvents.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={10}>当前筛选暂无资金费率结算记录。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
      </section>
      </>
        )}
        {activeTab === "liquidation" && (
      <>
      <ClearingEventReviewStrip rows={clearingEventRows} />
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-display text-lg">强平与 ADL 摘要</h3>
            <p className="mt-1 text-sm text-slate-400">高风险操作集中在本分区；执行 ADL 仍保留逐事件确认。</p>
          </div>
          <span className={`rounded-full px-3 py-1 text-xs ${totalAdlResidual > 0 ? "bg-rose-500/16 text-rose-100" : liquidationEvents.length > 0 ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
            {totalAdlResidual > 0 ? "待处理坏账" : liquidationEvents.length > 0 ? "有历史事件" : "正常"}
          </span>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          <SurveillanceMetric label="强平事件" value={String(liquidationEvents.length)} />
          <SurveillanceMetric label="ADL事件" value={String(adlEvents.length)} />
          <SurveillanceMetric label="保险覆盖" value={fmt(liquidationEvents.reduce((sum, item) => sum + Number(item.event.insurance_covered || 0), 0), 4)} />
          <SurveillanceMetric label="ADL覆盖" value={fmt(liquidationEvents.reduce((sum, item) => sum + Number(item.event.adl_covered || 0), 0), 4)} />
          <SurveillanceMetric label="ADL剩余" value={fmt(totalAdlResidual, 4)} />
        </div>
	      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
	          <h3 className="font-display text-lg">强平事件</h3>
	          <div className="flex flex-wrap items-center gap-2">
	            <button
	              type="button"
	              onClick={exportLiquidationEvents}
	              disabled={filteredLiquidationEvents.length === 0}
	              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前强平 CSV
	            </button>
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredLiquidationEvents.length, liquidationEvents.length, detailFiltersActive)}
	            </span>
	          </div>
	        </div>
          <div className="mb-3 border-t border-amber-300/18 pt-3">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="max-w-3xl">
                <div className="text-sm font-semibold text-amber-100">低频危险动作：逐事件 ADL 执行</div>
                <p className="mt-1 text-xs leading-5 text-amber-100/75">
                  先在下方强平事件表选择一条 ADL 剩余大于 0 的事件；执行会按盈利率和有效杠杆排序减掉盈利对手方仓位，并改写仓位、保险基金和清算记录。提交仍会二次确认，并由后端 `confirm_execute` 拦截未确认请求。
                </p>
                <div className="mt-2 rounded-xl bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-300">
                  {selectedAdlEvent ? (
                    <>
                      选中事件 <span className="font-mono text-slate-100">{selectedAdlEvent.event.event_id}</span>
                      {" · "}{selectedAdlEvent.user.username}
                      {" · "}{selectedAdlEvent.event.symbol}
                      {" · "}{selectedAdlEvent.event.position_side}
                      {" · ADL 剩余 "}<span className={selectedAdlResidual > 0 ? "font-mono text-amber-100" : "font-mono text-slate-400"}>{fmt(selectedAdlResidual, 6)}</span>
                    </>
                  ) : (
                    "当前未选中待执行 ADL 的强平事件。"
                  )}
                </div>
              </div>
              <button
                type="button"
                disabled={!selectedAdlEvent || selectedAdlResidual <= 0 || adlExecutingEventId === selectedAdlEvent.event.event_id}
                onClick={() => selectedAdlEvent && onExecuteAdl(selectedAdlEvent.event)}
                className="w-full rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 disabled:cursor-not-allowed disabled:opacity-40 sm:w-fit"
              >
                {selectedAdlEvent && adlExecutingEventId === selectedAdlEvent.event.event_id ? "执行中..." : "执行选中 ADL"}
              </button>
            </div>
          </div>
	        <AuditTablePager
	          page={safeLiquidationPage}
	          total={filteredLiquidationEvents.length}
	          pageSize={AUDIT_TABLE_PAGE_SIZE}
	          label="强平事件分页"
	          onPageChange={setLiquidationPage}
	        />
	        <div className="overflow-auto">
          <table className="min-w-[1540px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">方向</th>
                <th className="pb-2">数量</th>
                <th className="pb-2">入场</th>
                <th className="pb-2">标记</th>
                <th className="pb-2">强平价</th>
                <th className="pb-2">破产价</th>
                <th className="pb-2">已实现</th>
                <th className="pb-2">释放保证金</th>
                <th className="pb-2">保险覆盖</th>
                <th className="pb-2">未覆盖坏账</th>
                <th className="pb-2">ADL状态</th>
                <th className="pb-2">ADL覆盖</th>
                <th className="pb-2">ADL剩余</th>
                <th className="pb-2">原因</th>
                <th className="pb-2">ADL选择</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleLiquidationEvents.map((item) => {
                const realized = Number(item.event.realized_pnl);
                const residual = Number(item.event.residual_bad_debt || 0);
                const adlResidual = Number(item.event.adl_residual || 0);
                const adlSelected = selectedAdlEventId === item.event.event_id;
                return (
                  <tr key={item.event.event_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.event.liquidated_at)}</td>
                    <td className="py-2 pr-3 font-sans text-slate-100">{item.user.username}</td>
                    <td className="py-2 pr-3">{item.event.symbol}</td>
                    <td className="py-2 pr-3">{item.event.position_side}</td>
                    <td className="py-2 pr-3">{fmt(item.event.quantity, 8)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.entry_price, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.mark_price, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.liquidation_price, 4)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.bankruptcy_price, 4)}</td>
                    <td className={`py-2 pr-3 ${realized > 0 ? "text-emerald-300" : realized < 0 ? "text-rose-300" : ""}`}>{fmt(item.event.realized_pnl, 6)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.released_margin, 6)}</td>
                    <td className="py-2 pr-3">{fmt(item.event.insurance_covered, 6)}</td>
                    <td className={`py-2 pr-3 ${residual > 0 ? "text-amber-200" : ""}`}>{fmt(item.event.residual_bad_debt, 6)}</td>
                    <td className="py-2 pr-3">{item.event.adl_status ?? "-"}</td>
                    <td className="py-2 pr-3">{fmt(item.event.adl_covered, 6)}</td>
                    <td className={`py-2 pr-3 ${adlResidual > 0 ? "text-amber-200" : ""}`}>{fmt(item.event.adl_residual, 6)}</td>
                    <td className="py-2 pr-3">{item.event.reason}</td>
                    <td className="py-2 pr-3">
                      <button
                        type="button"
                        disabled={adlResidual <= 0 || adlExecutingEventId === item.event.event_id}
                        onClick={() => setSelectedAdlEventId(item.event.event_id)}
                        className={`rounded-xl px-3 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-40 ${adlSelected ? "bg-amber-400/22 text-amber-50" : "bg-white/8 text-slate-100"}`}
                      >
                        {adlSelected ? "已选中" : adlResidual > 0 ? "选中ADL" : "无需ADL"}
                      </button>
                    </td>
                  </tr>
                );
              })}
	              {filteredLiquidationEvents.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={18}>当前筛选暂无强平事件。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
	      </section>
	      <section className="panel rounded-2xl p-4">
	        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
	          <h3 className="font-display text-lg">ADL 自动减仓事件</h3>
	          <div className="flex flex-wrap items-center gap-2">
	            <button
	              type="button"
	              onClick={exportAdlEvents}
	              disabled={filteredAdlEvents.length === 0}
	              className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
	            >
	              导出当前 ADL CSV
	            </button>
	            <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">
	              {contractFilteredCountText(filteredAdlEvents.length, adlEvents.length, detailFiltersActive)}
	            </span>
	          </div>
	        </div>
	        <AuditTablePager
	          page={safeAdlPage}
	          total={filteredAdlEvents.length}
	          pageSize={AUDIT_TABLE_PAGE_SIZE}
	          label="ADL事件分页"
	          onPageChange={setAdlPage}
	        />
	        <div className="overflow-auto">
          <table className="min-w-[1180px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th>
                <th className="pb-2">市场</th>
                <th className="pb-2">主体</th>
                <th className="pb-2">方向</th>
                <th className="pb-2">数量</th>
                <th className="pb-2">执行价</th>
                <th className="pb-2">已实现</th>
                <th className="pb-2">覆盖坏账</th>
                <th className="pb-2">剩余坏账</th>
                <th className="pb-2">PnL% / 有效杠杆</th>
                <th className="pb-2">关联强平</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-200">
	              {visibleAdlEvents.map((item) => {
                const realized = Number(item.realized_pnl);
                return (
                  <tr key={item.event_id} className="border-t border-white/8">
                    <td className="py-2 pr-3">{bjDateTime(item.created_at)}</td>
                    <td className="py-2 pr-3">{item.symbol ?? "-"}</td>
                    <td className="py-2 pr-3 font-sans text-slate-100">{item.username ?? `#${item.user_id}`}</td>
                    <td className="py-2 pr-3">{item.position_side}</td>
                    <td className="py-2 pr-3">{fmt(item.quantity, 8)}</td>
                    <td className="py-2 pr-3">{fmt(item.execution_price, 4)}</td>
                    <td className={`py-2 pr-3 ${realized > 0 ? "text-emerald-300" : realized < 0 ? "text-rose-300" : ""}`}>{fmt(item.realized_pnl, 6)}</td>
                    <td className="py-2 pr-3">{fmt(item.covered_amount, 6)}</td>
                    <td className="py-2 pr-3">{fmt(item.residual_after, 6)}</td>
                    <td className="py-2 pr-3">{fmt(Number(item.pnl_pct) * 100, 2)}% / {fmt(item.effective_leverage, 2)}x</td>
                    <td className="max-w-[170px] truncate py-2 pr-3 text-slate-400">{item.liquidation_event_id}</td>
                  </tr>
                );
              })}
	              {filteredAdlEvents.length === 0 && (
	                <tr>
	                  <td className="py-8 text-center text-sm text-slate-500" colSpan={11}>当前筛选暂无 ADL 自动减仓事件。</td>
	                </tr>
	              )}
            </tbody>
          </table>
        </div>
      </section>
      </>
        )}
        </div>
    </section>
  );
}

function ContractDetailFilterBar({
  filters,
  symbols,
  activeTab,
  active,
  loading,
  onFiltersChange,
  onBackendRefresh,
  onReset,
}: {
  filters: ContractDetailFilters;
  symbols: string[];
  activeTab: ContractAdminTab;
  active: boolean;
  loading: boolean;
  onFiltersChange: Dispatch<SetStateAction<ContractDetailFilters>>;
  onBackendRefresh: () => void;
  onReset: () => void;
}) {
  const showFundingStatus = activeTab === "funding";
  const symbolOptions = filters.symbol !== "all" && !symbols.includes(filters.symbol) ? [filters.symbol, ...symbols] : symbols;
  const gridClass = showFundingStatus
    ? "xl:grid-cols-[1fr_1.4fr_1fr_auto_auto]"
    : "xl:grid-cols-[1fr_1.6fr_auto_auto]";
  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-display text-lg">明细筛选</h3>
          <p className="mt-1 text-sm text-slate-400">先在当前已加载明细内即时筛选；按市场、完整用户名或 UID 可从后端重新加载最近 200 条筛选数据。</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs ${active ? "bg-cyan-400/14 text-cyan-100" : "bg-white/8 text-slate-300"}`}>
          {active ? "筛选中" : "全部明细"}
        </span>
      </div>
      <div className={`grid gap-3 md:grid-cols-2 ${gridClass}`}>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">市场</span>
          <select
            value={filters.symbol}
            onChange={(event) => onFiltersChange((current) => ({ ...current, symbol: event.target.value }))}
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100 outline-none"
          >
            <option value="all">全部市场</option>
            {symbolOptions.map((symbol) => (
              <option key={symbol} value={symbol}>{symbol}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">账户 / 事件关键字</span>
          <input
            value={filters.query}
            onChange={(event) => onFiltersChange((current) => ({ ...current, query: event.target.value }))}
            placeholder="用户名、UID、订单ID、事件ID、错误关键字"
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600"
          />
        </label>
        {showFundingStatus && (
          <label className="text-xs text-slate-400">
            <span className="mb-1 block">资金费任务状态</span>
            <select
              value={filters.fundingStatus}
              onChange={(event) => onFiltersChange((current) => ({ ...current, fundingStatus: event.target.value as ContractDetailFilters["fundingStatus"] }))}
              className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100 outline-none"
            >
              <option value="all">全部状态</option>
              <option value="active">未完成 / 处理中</option>
              <option value="failed">failed</option>
              <option value="settled">settled</option>
            </select>
          </label>
        )}
        <button
          type="button"
          disabled={loading}
          onClick={onBackendRefresh}
          className="self-end rounded-xl bg-cyan-400/16 px-3 py-2 text-sm text-cyan-100 hover:bg-cyan-400/22 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "加载中" : "加载筛选数据"}
        </button>
        <button
          type="button"
          disabled={loading || !active}
          onClick={onReset}
          className="self-end rounded-xl bg-white/8 px-3 py-2 text-sm text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-40"
        >
          重置筛选
        </button>
      </div>
    </section>
  );
}

function adminStatusText(status?: string) {
  if (status === "starting") return "启动中";
  if (status === "running") return "运行中";
  if (status === "stale") return "心跳异常";
  if (status === "stopping") return "停止中";
  if (status === "error") return "异常";
  if (status === "stopped") return "已停止";
  return status ?? "-";
}

function adminStatusClass(status?: string) {
  if (status === "running") return "bg-emerald-400/15 text-emerald-100";
  if (status === "starting" || status === "stopping") return "bg-cyan-400/15 text-cyan-100";
  if (status === "stale") return "bg-amber-400/16 text-amber-100";
  if (status === "error") return "bg-rose-500/16 text-rose-100";
  return "bg-white/8 text-slate-300";
}

function AdminSidebar({
  section,
  onSectionChange,
  userCount,
  marketCount,
  status,
  selectedMarketSymbol,
  marketSymbols,
  onMarketChange,
  selectedUserId,
  users,
  onUserChange,
}: {
  section: AdminSection;
  onSectionChange: (section: AdminSection) => void;
  userCount: number;
  marketCount: number;
  status?: string;
  selectedMarketSymbol: string;
  marketSymbols: string[];
  onMarketChange: (symbol: string) => void;
  selectedUserId: number | null;
  users: AdminUser[];
  onUserChange: (userId: number) => void;
}) {
  const statusClass =
    status === "critical"
      ? "bg-rose-500/16 text-rose-100"
      : status === "warn"
        ? "bg-amber-400/16 text-amber-100"
        : "bg-emerald-400/15 text-emerald-100";
  const accountGroupKeys: AccountUserKind[] = ["customer", "admin", "spot_robot", "contract_robot", "system"];
  const accountGroups = accountGroupKeys
    .map((kind) => ({
      kind,
      label: accountUserKindLabel(kind),
      users: users.filter((user) => accountUserKindForUser(user) === kind),
    }))
    .filter((group) => group.users.length > 0);

  return (
    <aside className="hidden xl:block panel ui-sidebar overflow-y-auto rounded-2xl p-3 scrollbar">
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-white/8 pb-3">
        <div>
          <div className="text-sm font-medium text-white">管理后台</div>
          {section !== "maker_config" && section !== "flow_config" && <div className="mt-1 text-xs text-slate-500">{userCount} 主体 · {marketCount} 市场</div>}
        </div>
        {section !== "maker_config" && section !== "flow_config" && <span className={`rounded-full px-2 py-1 text-[11px] ${statusClass}`}>{status === "critical" ? "需处理" : status === "warn" ? "需关注" : status ? "正常" : "读取中"}</span>}
      </div>
      <nav aria-label="后台功能" className="space-y-4">
        {adminNavGroups.map((group) => (
          <div key={group.title}>
            <div className="mb-2 px-2 text-xs uppercase tracking-[0.16em] text-slate-500">{group.title}</div>
            <div className="space-y-1">
              {group.items.map((item) => {
                const active = section === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    aria-current={active ? "page" : undefined}
                    onClick={() => onSectionChange(item.key)}
                    className={`w-full rounded-xl px-3 py-2 text-left transition ${
                      active ? "bg-cyan-400/16 text-cyan-100" : "text-slate-300 hover:bg-white/6"
                    }`}
                  >
                    <div className="text-sm font-medium">{item.label}</div>
                    <div className="mt-0.5 truncate text-xs text-slate-500">{item.hint}</div>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
      <div className="mt-4 rounded-xl border border-white/8 bg-slate-950/25 p-3" hidden={!(["markets", "accounts", "bots"] as string[]).includes(section)}>
        <div>
          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-slate-500">当前市场</div>
          <div className="max-h-48 space-y-1 overflow-y-auto pr-1 scrollbar">
            {marketSymbols.map((symbol) => {
              const active = selectedMarketSymbol === symbol;
              return (
                <button
                  key={symbol}
                  type="button"
                  onClick={() => onMarketChange(symbol)}
                  className={`w-full rounded-xl px-3 py-2 text-left text-sm transition ${
                    active ? "bg-cyan-400/16 text-cyan-100" : "text-slate-300 hover:bg-white/6"
                  }`}
                >
                  {symbol}
                </button>
              );
            })}
          </div>
        </div>
        <div className="mt-3" hidden={section === "markets"}>
          <div className="mb-2 text-xs uppercase tracking-[0.16em] text-slate-500">当前账户 · 按身份</div>
          <div className="max-h-56 space-y-3 overflow-y-auto pr-1 scrollbar">
            {accountGroups.map((group) => (
              <div key={group.kind}>
                <div className="mb-1 flex items-center justify-between gap-2 px-2 text-[11px] text-slate-500">
                  <span>{group.label}</span>
                  <span className="font-mono">{group.users.length}</span>
                </div>
                <div className="space-y-1">
                  {group.users.map((user) => {
                    const active = selectedUserId === user.id;
                    return (
                      <button
                        key={user.id}
                        type="button"
                        onClick={() => onUserChange(user.id)}
                        className={`w-full rounded-xl px-3 py-2 text-left text-sm transition ${
                          active ? accountUserKindClass(group.kind) : "text-slate-300 hover:bg-white/6"
                        }`}
                      >
                        <span className="block truncate">{user.username}</span>
                        <span className="mt-0.5 block text-[11px] text-slate-500">UID {user.id} · {user.role}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </aside>
  );
}

function MarketScopeHeader({
  section,
  selectedMarketSymbol,
  marketSymbols,
  markets,
  surveillance,
  onMarketChange,
}: {
  section: AdminSection;
  selectedMarketSymbol: string;
  marketSymbols: string[];
  markets: Record<string, AdminMarketItem>;
  surveillance?: MarketSurveillanceItem;
  onMarketChange: (symbol: string) => void;
}) {
  const market = markets[selectedMarketSymbol];
  const title = section === "markets" ? "市场运营" : section === "strategies" ? "机器人运营" : "管理范围";
  const status = surveillance?.status ?? (market?.is_active ? "ok" : "paused");
  const statusClass =
    status === "critical"
      ? "bg-rose-500/16 text-rose-100"
      : status === "warn"
        ? "bg-amber-400/16 text-amber-100"
        : "bg-emerald-400/15 text-emerald-100";

  return (
    <div className="panel rounded-2xl p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h2 className="font-display text-xl">{title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-400">
            <span>{selectedMarketSymbol || "-"}</span>
            <span>{market?.market_type === "mainstream" ? "主流" : market?.market_type === "listed" ? "独立上市" : "-"}</span>
            <span className={`rounded-full px-2 py-0.5 text-xs ${statusClass}`}>{status}</span>
          </div>
        </div>
        <label className="block min-w-[220px]">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">market</span>
          <select
            value={selectedMarketSymbol}
            onChange={(event) => onMarketChange(event.target.value)}
            className="w-full rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-slate-100 outline-none"
          >
            {marketSymbols.map((symbol) => (
              <option key={symbol} value={symbol}>{symbol}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        <SurveillanceMetric label="买一 / 卖一" value={`${fmt(surveillance?.metrics.best_bid, 8)} / ${fmt(surveillance?.metrics.best_ask, 8)}`} />
        <SurveillanceMetric label="Spread" value={`${fmt(surveillance?.metrics.spread_pct, 4)}%`} />
        <SurveillanceMetric label="盘口层数" value={surveillance ? `${surveillance.metrics.bid_level_count} / ${surveillance.metrics.ask_level_count}` : "-"} />
        <SurveillanceMetric label="活跃挂单" value={surveillance ? `${surveillance.metrics.open_order_count}` : "-"} />
        <SurveillanceMetric label="24H 成交额" value={surveillance ? `${fmt(surveillance.metrics.quote_volume_24h, 0)}` : "-"} />
      </div>
    </div>
  );
}

function MakerInstancePanel({
  market,
  bots,
  instance,
  strategyVersion,
  onStart,
  onStop,
  onStopAndCancel,
  onRestart,
}: {
  market?: AdminMarketItem;
  bots: MarketBotAccount[];
  instance?: MakerInstanceStatus;
  strategyVersion: string;
  onStart: () => void;
  onStop: () => void;
  onStopAndCancel: () => void;
  onRestart: () => void;
}) {
	  const enabledMakers = bots.filter((bot) => bot.is_enabled && bot.role === "maker").length;
	  const enabledFlows = bots.filter((bot) => bot.is_enabled && bot.role === "flow").length;
	  const status = instance?.status ?? "stopped";
  const runningStrategyKey = normalizedStrategyKey(instance?.strategy_version ?? strategyVersion);
  const configuredStrategyKey = normalizedStrategyKey(instance?.configured_strategy_version ?? strategyVersion);
  const strategyMismatch = Boolean(
    instance?.running
    && runningStrategyKey
    && configuredStrategyKey
    && (instance.runtime_strategy_mismatch || runningStrategyKey !== configuredStrategyKey)
  );
  const statusClass =
    status === "running"
      ? "bg-emerald-400/15 text-emerald-100"
      : status === "starting" || status === "stopping" || status === "switching" || status === "rollback"
        ? "bg-cyan-400/15 text-cyan-100"
      : status === "stale"
        ? "bg-amber-400/16 text-amber-100"
        : status === "error"
          ? "bg-rose-500/16 text-rose-100"
        : "bg-white/8 text-slate-300";
	  const canStart = Boolean(market?.is_active && enabledMakers > 0 && !instance?.running);
  const instanceBoundaryRows = [
    {
      action: "启动",
      preCheck: "市场启用、maker 账号、已保存策略和启动预检。",
      postCheck: "PID、heartbeat、运行策略、盘口双边和 FLOW guard。",
      boundary: "开始真实挂单并可能成交；不做调账或清算。",
    },
    {
      action: "停止",
      preCheck: "确认普通停止是否允许保留当前挂单。",
      postCheck: "实例状态、heartbeat 停止、剩余挂单和盘口影响。",
      boundary: "只停进程，默认不主动撤旧挂单。",
    },
    {
      action: "停止并撤挂单",
      preCheck: "确认撤单范围、机器人挂单数量和演示窗口。",
      postCheck: "撤单数量、当前委托、盘口恢复和操作记录。",
      boundary: "停实例并撤机器人当前挂单；不改账户余额。",
    },
    {
      action: "重启",
      preCheck: "确认策略版本、参数、PID 基线和短暂盘口空窗。",
      postCheck: "新 PID、heartbeat、运行策略、旧挂单清理和日志。",
      boundary: "先停旧实例并撤旧挂单，再启动新实例。",
    },
  ];
	  return (
	    <section className="panel rounded-3xl p-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-display text-lg">{market?.symbol ?? "-"} 做市实例</h3>
            <span className={`rounded-full px-2.5 py-1 text-xs ${statusClass}`}>{status}</span>
          </div>
          <p className="mt-1 text-sm text-slate-400">
            运行策略 {strategyDisplayName(runningStrategyKey)} · 已保存 {strategyDisplayName(configuredStrategyKey)} · maker {enabledMakers} · flow {enabledFlows} · pid {instance?.pid ?? "-"}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={!canStart}
            onClick={onStart}
            className="rounded-2xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100 disabled:cursor-not-allowed disabled:opacity-45"
          >
            启动
          </button>
          <button
            type="button"
            disabled={!instance?.running}
            onClick={onStop}
            className="rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100 disabled:cursor-not-allowed disabled:opacity-45"
          >
            停止
          </button>
          <button
            type="button"
            disabled={bots.length === 0}
            onClick={onStopAndCancel}
            className="rounded-2xl bg-rose-500/16 px-4 py-2 text-sm text-rose-100 disabled:cursor-not-allowed disabled:opacity-45"
          >
            停止并撤挂单
          </button>
          <button
            type="button"
            disabled={!market?.is_active || enabledMakers === 0}
            onClick={onRestart}
            className="rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100 disabled:cursor-not-allowed disabled:opacity-45"
          >
            重启
          </button>
        </div>
      </div>
	      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
	        <SurveillanceMetric label="运行指标" value={instance?.metrics_present ? "已接收" : "暂无"} />
	        <SurveillanceMetric label="最后心跳" value={bjDateTime(instance?.last_heartbeat_at ?? instance?.last_metrics_ts)} />
	        <SurveillanceMetric label="PID 文件" value={basename(instance?.pid_file)} />
	        <SurveillanceMetric label="日志文件" value={basename(instance?.log_path)} />
	      </div>
      <div className="mt-4 overflow-auto">
        <table className="min-w-[840px] w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="pb-2 pr-3 font-medium">实例动作</th>
              <th className="pb-2 pr-3 font-medium">执行前核对</th>
              <th className="pb-2 pr-3 font-medium">执行后核对</th>
              <th className="pb-2 font-medium">边界</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/6 text-slate-300">
            {instanceBoundaryRows.map((row) => (
              <tr key={row.action}>
                <td className="py-2 pr-3 font-medium text-slate-100">{row.action}</td>
                <td className="py-2 pr-3">{row.preCheck}</td>
                <td className="py-2 pr-3">{row.postCheck}</td>
                <td className="py-2">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
	      {instance?.last_error && (
	        <div className="mt-3 rounded-2xl border border-rose-500/15 bg-rose-500/8 px-3 py-2 text-sm text-rose-100">
	          {instance.last_error}
        </div>
      )}
      {strategyMismatch && (
        <div className="mt-3 rounded-2xl border border-amber-400/16 bg-amber-400/8 px-3 py-2 text-sm text-amber-100">
          已保存策略为 {strategyDisplayName(configuredStrategyKey)}，但当前进程仍运行 {strategyDisplayName(runningStrategyKey)}；点击“重启”后才会加载新策略。
        </div>
      )}
      {(!market?.is_active || enabledMakers === 0) && (
        <div className="mt-3 rounded-2xl border border-amber-400/12 bg-amber-400/8 px-3 py-2 text-sm text-amber-100">
          {!market?.is_active ? "市场未启用，不能启动做市实例。" : "当前币对没有启用的 maker 机器人，不能启动做市实例。"}
        </div>
      )}
      {instance?.last_log_lines?.length ? (
        <details className="mt-3 rounded-2xl border border-white/8 bg-slate-950/30 p-3">
          <summary className="cursor-pointer text-sm text-slate-300">
            最近日志 · {logScopeText(instance.log_scope)} · run {shortRunId(instance.run_id)}
          </summary>
          <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
            <span className={`rounded-full px-2 py-0.5 ${instance.log_scope === "current_run" ? "bg-emerald-400/12 text-emerald-100" : "bg-amber-400/12 text-amber-100"}`}>
              {logScopeText(instance.log_scope)}
            </span>
            <span className="font-mono">offset {instance.log_start_offset ?? "-"} / {instance.log_size ?? "-"}</span>
            <span>{instance.log_started_at ? bjDateTime(instance.log_started_at) : "-"}</span>
          </div>
          <pre className="mt-3 max-h-44 overflow-auto whitespace-pre-wrap text-xs leading-5 text-slate-400 scrollbar">
            {instance.last_log_lines.join("\n")}
          </pre>
        </details>
      ) : null}
    </section>
  );
}

function quickConfigValue(config: LiquidityRuntimeConfig, key: string) {
  const value = config[key];
  if (typeof value === "boolean") return value;
  if (value === undefined || value === null) return "";
  return String(value);
}

function basename(path?: string | null) {
  if (!path) return "-";
  return path.split("/").filter(Boolean).pop() ?? path;
}

function shortRunId(value?: string | null) {
  if (!value) return "-";
  return value.length > 22 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function logScopeText(scope?: string | null) {
  if (scope === "current_run") return "本次运行";
  if (scope === "full_file") return "全文件尾部";
  return scope ?? "-";
}

function ConfigQuickField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | boolean;
  onChange: (value: string | boolean) => void;
}) {
  if (typeof value === "boolean") {
    return (
      <label className="flex min-h-[46px] items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-3 text-sm">
        <span className="text-slate-300">{label}</span>
        <input type="checkbox" checked={value} onChange={(event) => onChange(event.target.checked)} className="h-4 w-4 accent-cyan-400" />
      </label>
    );
  }
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
      />
    </label>
  );
}

function StrategySchemaFieldEditor({
  field,
  value,
  onChange,
}: {
  field: StrategySchemaField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = field.label || field.path || "value";
  const kind = field.kind ?? "string";
  if (kind === "boolean" || typeof value === "boolean") {
    const checked = value === true || value === "true";
    return (
      <label className="flex min-h-[46px] items-center justify-between gap-3 rounded-2xl border border-white/10 bg-white/5 px-3 text-sm">
        <span className="truncate text-slate-300" title={field.help ?? label}>{label}</span>
        <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="h-4 w-4 shrink-0 accent-cyan-400" />
      </label>
    );
  }
  return (
    <label className="block min-w-0">
      <span className="mb-1 block truncate text-xs text-slate-500" title={field.help ?? label}>{label}</span>
      <input
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(event) => onChange(event.target.value)}
        inputMode={kind === "integer" || kind === "number" ? "decimal" : undefined}
        className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
      />
    </label>
  );
}

function StrategyControlPanel({
  symbol,
  productType,
  strategyVersion,
  onStrategyChange,
  onSaveStrategy,
  onSaveAndRestart,
  strategyState,
  strategyConfig,
  instance,
  onStrategyConfigChange,
  applyStatus,
  embedded = false,
}: {
  symbol: string;
  productType?: string;
  strategyVersion: string;
  onStrategyChange: (strategy: string) => void;
  onSaveStrategy: () => void;
  onSaveAndRestart?: () => void;
  strategyState?: MarketStrategyState;
  strategyConfig: LiquidityRuntimeConfig;
  instance?: MakerInstanceStatus;
  onStrategyConfigChange: (symbol: string, path: Array<string | number>, nextValue: unknown) => void;
  applyStatus?: StrategyApplyStatus;
  embedded?: boolean;
}) {
  const selectedStrategy = strategyState?.items?.find((item) => item.strategy_key === strategyVersion)
    ?? strategyState?.selected;
  const schemaSections = selectedStrategy?.schema?.sections ?? [];
  const strategyOptions = Array.from(
    new Map(
      [
        ...(strategyState?.items ?? []),
        ...(strategyState?.templates ?? []).map((item) => ({
          strategy_key: item.strategy_key,
          display_name: item.display_name,
        })),
      ].map((item) => [item.strategy_key, item]),
    ).values(),
  );
  const schemaFields = strategySchemaFields(selectedStrategy);
  const isPerpMarket = productType === "PERP";
  const isPerpStrategy = strategyVersion.startsWith("PERP_");
  const liteFields = [
    ["levels_per_side", "每边层数"],
    ["top_offset_bps", "首档偏离 bps"],
    ["near_band_bps", "近档 bps"],
    ["near_depth_quote", "近档金额"],
    ["mid_depth_quote", "中档累计金额"],
    ["far_depth_quote", "远档累计金额"],
    ["planner_interval_ms", "报价周期 ms"],
    ["idle_turnover_usdt_per_min", "目标成交额/分"],
    ["flow_enabled", "背景成交流"],
  ] as const;
  const perpFields = [
    ["poll_interval_ms", "报价轮询 ms"],
    ["levels", "单侧层数"],
    ["gap_ticks", "层间距 ticks"],
    ["quantity", "单笔数量"],
    ["leverage", "启动杠杆"],
    ["refresh_external", "刷新外部价格"],
  ] as const;
  const quickFields =
    strategyVersion === "LITE"
      ? liteFields
      : isPerpStrategy
        ? perpFields
        : [];
  const strategyHint =
    strategyVersion === "LITE"
      ? "Lite 适合默认演示：用层数、bps、USDT 深度和每分钟成交额控制盘口。"
      : isPerpStrategy
        ? "PERP_MM 是合约默认做市策略，使用合约保证金账户做市。"
        : "当前策略按服务端模板运行。";
  const visibleStrategyOptions = strategyOptionsForProduct(productType, strategyOptions);
  const savedStrategyKey = normalizedStrategyKey(strategyState?.selected?.strategy_key ?? instance?.configured_strategy_version ?? strategyVersion);
  const selectedStrategyKey = normalizedStrategyKey(strategyVersion);
  const runningStrategyKey = normalizedStrategyKey(instance?.strategy_version ?? savedStrategyKey);
  const savedChanged = selectedStrategyKey !== savedStrategyKey;
  const runningMismatch = Boolean(
    instance?.running
    && runningStrategyKey
    && savedStrategyKey
    && (instance.runtime_strategy_mismatch || runningStrategyKey !== savedStrategyKey)
  );
  const needsRestart = isPerpMarket && runningMismatch;
  const restartNotice = needsRestart
    ? `待重启：运行中是 ${strategyDisplayName(runningStrategyKey)}，已保存是 ${strategyDisplayName(savedStrategyKey)}。`
    : isPerpMarket && instance?.running
      ? `运行中：${strategyDisplayName(runningStrategyKey)}；参数保存后轮询读取，策略切换通常需重启。`
    : instance?.running
      ? `运行中：${strategyDisplayName(runningStrategyKey)}；保存后约 1 秒内热生效。`
      : "实例未运行：下次启动读取已保存策略。";
  const showSaveAndRestart = Boolean(isPerpMarket && onSaveAndRestart);
  const saveButtonText = isPerpMarket
    ? "仅保存配置"
    : instance?.running
      ? (savedChanged ? "保存并热切换" : "保存并热生效")
      : "保存参数";
  const effectModeLabel = isPerpMarket
    ? (instance?.running ? "保存后需重启" : "下次启动生效")
    : (instance?.running ? "保存后热生效" : "下次启动生效");
  const saveAfterCheckText = isPerpMarket
    ? "执行后核对保存策略、运行策略、实例日志、合约订单和保证金占用；资金费、强平和 ADL 仍回合约清算。"
    : "执行后核对保存策略、运行策略、实例日志、盘口、FLOW 状态和机器人订单成交；现货资金账本仍回账户与资金。";

  return (
    <section className={embedded ? "rounded-2xl border border-white/8 bg-slate-950/25 p-4" : "panel rounded-2xl p-4"}>
      <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <h3 className="font-display text-lg">{symbol} 策略参数</h3>
          <p className="mt-1 text-sm text-slate-400">{strategyHint}</p>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <span className={`rounded-full px-2 py-0.5 ${needsRestart ? "bg-amber-400/16 text-amber-100" : instance?.running ? "bg-emerald-400/14 text-emerald-100" : "bg-white/8 text-slate-300"}`}>
              {restartNotice}
            </span>
            {savedChanged && <span className="rounded-full bg-cyan-400/14 px-2 py-0.5 text-cyan-100">有未保存选择</span>}
          </div>
          <div className="mt-2 rounded-2xl border border-white/8 bg-white/[0.03] px-3 py-2 text-xs leading-5 text-slate-400">
            <span className="text-slate-200">保存边界：</span>{strategySaveBoundary({ product_type: productType })}
          </div>
        </div>
      </div>

      <div className="mb-4 grid gap-3 lg:grid-cols-[280px_1fr]">
        <label className="block">
          <span className="mb-1 block text-xs text-slate-500">策略</span>
          <select
            value={strategyVersion}
            onChange={(event) => onStrategyChange(event.target.value.toUpperCase())}
            className="w-full rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm outline-none"
          >
            {visibleStrategyOptions.map((option) => (
              <option key={option.strategy_key} value={option.strategy_key}>
                {option.display_name ?? strategyDisplayName(option.strategy_key)}
              </option>
            ))}
          </select>
        </label>
        <div className="grid gap-2 text-xs sm:grid-cols-3">
          <div className="border-l border-white/10 pl-3">
            <div className="text-slate-500">字段数</div>
            <div className="mt-1 text-slate-100">{schemaFields.length} 项</div>
          </div>
          <div className="border-l border-white/10 pl-3">
            <div className="text-slate-500">运行策略</div>
            <div className="mt-1 text-slate-100">{instance?.running ? strategyDisplayName(runningStrategyKey) : "实例未运行"}</div>
          </div>
          <div className="border-l border-white/10 pl-3">
            <div className="text-slate-500">生效方式</div>
            <div className="mt-1 text-slate-100">{effectModeLabel}</div>
          </div>
        </div>
      </div>

      <div className="mb-4 border-y border-white/8 bg-white/[0.025] px-3 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-medium text-slate-100">低频配置动作</div>
            <div className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">{saveAfterCheckText}</div>
            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
              <span className="rounded-full bg-white/8 px-2 py-0.5">执行前会二次确认</span>
              <span className="rounded-full bg-white/8 px-2 py-0.5">不调账</span>
              <span className="rounded-full bg-white/8 px-2 py-0.5">不清算</span>
              {isPerpMarket && <span className="rounded-full bg-amber-400/14 px-2 py-0.5 text-amber-100">重启会撤旧机器人挂单</span>}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <button type="button" onClick={onSaveStrategy} className="rounded-xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100">
              {saveButtonText}
            </button>
            {showSaveAndRestart && (
              <button type="button" onClick={onSaveAndRestart} className="rounded-xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
                保存并重启生效
              </button>
            )}
          </div>
        </div>
      </div>
      {applyStatus && (
        <div className={`mb-4 rounded-2xl border px-3 py-2 text-sm ${
          applyStatus.state === "waiting_strategy_switch" || applyStatus.state === "restart_required" || applyStatus.state === "waiting_instance_restart"
            ? "border-amber-400/16 bg-amber-400/8 text-amber-100"
            : applyStatus.state === "instance_not_running"
              ? "border-white/10 bg-white/5 text-slate-300"
              : "border-emerald-400/16 bg-emerald-400/8 text-emerald-100"
        }`}>
          <div className="font-medium">{applyStatus.label}</div>
          <div className="mt-1 text-xs leading-5 text-slate-300">{applyStatus.detail}</div>
        </div>
      )}
      <div className="grid gap-3">
        <div className="rounded-2xl border border-cyan-400/12 bg-cyan-400/6 p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="font-display text-sm">常用参数</h4>
              <div className="mt-1 text-xs text-slate-500">
                {selectedStrategy?.display_name ?? strategyDisplayName(strategyVersion)} · 改完到低频配置动作保存
              </div>
            </div>
            <span className="rounded-full bg-white/8 px-2.5 py-1 text-xs text-slate-300">{strategyDisplayName(strategyVersion)}</span>
          </div>
          {quickFields.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {quickFields.map(([key, label]) => (
                <ConfigQuickField
                  key={key}
                  label={label}
                  value={quickConfigValue(strategyConfig, key)}
                  onChange={(value) => onStrategyConfigChange(symbol, [key], value)}
                />
              ))}
            </div>
          ) : (
            <div className="rounded-2xl bg-white/5 px-3 py-5 text-sm text-slate-500">
              当前策略没有预设常用参数，请展开“全部参数字段”编辑。
            </div>
          )}
        </div>
        <details className="rounded-2xl border border-white/10 bg-slate-950/25 p-3">
          <summary className="cursor-pointer font-display text-sm text-slate-100">
            全部参数字段 · {schemaFields.length} 项
          </summary>
          <div className="mt-3 scrollbar max-h-[420px] space-y-4 overflow-y-auto pr-1">
            {schemaSections.length > 0 ? (
              schemaSections.map((section) => (
                <div key={section.key} className="rounded-2xl border border-white/8 bg-white/[0.03] p-3">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <h5 className="font-display text-sm text-slate-100">{section.label ?? section.key}</h5>
                    <span className="rounded-full bg-white/6 px-2 py-0.5 text-[11px] text-slate-400">{section.fields?.length ?? 0} 项</span>
                  </div>
                  <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {(section.fields ?? []).map((field) => (
                      <StrategySchemaFieldEditor
                        key={field.path}
                        field={field}
                        value={getValueAtPath(strategyConfig, field.path)}
                        onChange={(value) => onStrategyConfigChange(symbol, field.path.split(".").filter(Boolean), value)}
                      />
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <div className="rounded-2xl bg-white/5 px-3 py-6 text-center text-sm text-slate-500">当前策略模板没有可编辑参数字段</div>
            )}
          </div>
        </details>
      </div>
    </section>
  );
}

function MarketBotBindingBoundaryStrip({
  market,
  bots,
  form,
}: {
  market: AdminMarketItem;
  bots: MarketBotAccount[];
  form: MarketBotFormState;
}) {
  const isPerp = market.product_type === "PERP";
  const enabledCount = bots.filter((bot) => bot.is_enabled).length;
  const flowCount = bots.filter((bot) => bot.role === "flow").length;
  const makerCount = bots.filter((bot) => bot.role === "maker").length;
  const productLabel = isPerp ? "PERP 合约机器人" : "SPOT 现货机器人";
  const walletLabel = isPerp ? `${market.margin_asset ?? market.quote_asset} 合约保证金` : `${market.quote_asset}/${market.base_asset} 现货资金模板`;
  const roleSummary = isPerp ? `maker / FLOW资源 = ${makerCount} / ${flowCount}` : `maker / flow = ${makerCount} / ${flowCount}`;
  const roleBoundary =
    form.role === "flow"
      ? "FLOW 只服务本地沙盒成交流；不代表客户成交留存口径。"
      : form.role === "hedge"
        ? "hedge 是预留运行角色；资金和风险仍回对应账本核对。"
        : "maker 负责盘口流动性；策略启停仍回机器人运营。";
  return (
    <div className="mb-4 border-y border-white/8 bg-white/[0.025] px-3 py-3">
      <div className="grid gap-3 lg:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前对象</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs ${isPerp ? "bg-violet-400/14 text-violet-100" : "bg-cyan-400/14 text-cyan-100"}`}>{productLabel}</span>
            <span className="text-sm text-slate-200">{market.symbol}</span>
          </div>
          <div className="mt-1 text-xs leading-5 text-slate-500">已绑定 {bots.length} 个，启用 {enabledCount} 个；{roleSummary}。</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">本页会写入</div>
          <div className="mt-1 text-sm leading-6 text-slate-200">mm_bot 测试 UID、API 凭据、市场角色和 {walletLabel}。</div>
          <div className="mt-1 text-xs leading-5 text-slate-500">{roleBoundary}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">不要误解成</div>
          <div className="mt-1 text-sm leading-6 text-slate-300">不启动实例、不调账、不清算、不合并现货钱包和合约保证金。</div>
          <div className="mt-1 text-xs leading-5 text-slate-500">机器人高频订单成交按对账口径看；普通客户完整记录仍回订单、账本和清算域。</div>
        </div>
      </div>
      {isPerp && flowCount > 0 && (
        <div className="mt-3 rounded-xl border border-violet-400/14 bg-violet-400/6 px-3 py-2 text-xs leading-5 text-violet-100/85">
          当前市场存在 {flowCount} 个 PERP FLOW 资源。它用于本地沙盒合约成交流和运行对账，启动、暂停、参数和 guard 回机器人运营；本页只维护账号绑定和保证金模板。
        </div>
      )}
    </div>
  );
}

function MarketBotsPanel({
  market,
  bots,
  nextBotIndex,
  form,
  onFormChange,
  onCreate,
  onCreateDefaults,
  onCreateFlow,
  onUpdate,
}: {
  market?: AdminMarketItem;
  bots: MarketBotAccount[];
  nextBotIndex: number;
  form: MarketBotFormState;
  onFormChange: (patch: Partial<MarketBotFormState>) => void;
  onCreate: () => void;
  onCreateDefaults: () => void;
  onCreateFlow: () => void;
  onUpdate: (botId: number, edit: MarketBotEditState) => void;
}) {
  const [botEdits, setBotEdits] = useState<Record<number, MarketBotEditState>>({});

  useEffect(() => {
    setBotEdits((current) =>
      Object.fromEntries(bots.map((bot) => [bot.id, current[bot.id] ?? defaultMarketBotEdit(bot)]))
    );
  }, [bots]);

  const updateBotEdit = (bot: MarketBotAccount, patch: Partial<MarketBotEditState>) => {
    setBotEdits((current) => ({
      ...current,
      [bot.id]: {
        ...(current[bot.id] ?? defaultMarketBotEdit(bot)),
        ...patch,
      },
    }));
  };

  const resetBotEdit = (bot: MarketBotAccount) => {
    setBotEdits((current) => ({
      ...current,
      [bot.id]: defaultMarketBotEdit(bot),
    }));
  };

  if (!market) {
    return (
      <section className="panel rounded-3xl p-4 text-sm text-slate-400">
        请选择市场。
      </section>
    );
  }
  const referencePrice = Number(form.referencePrice || market.reference_price || 0);
  const baseNotional = Number(form.initialBaseNotional || 0);
  const derivedBaseAmount = referencePrice > 0 && baseNotional > 0 ? baseNotional / referencePrice : undefined;
  const isPerp = market.product_type === "PERP";
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
	          <h3 className="font-display text-lg">{market.symbol} 机器人账号资源</h3>
          <p className="mt-1 text-sm text-slate-400">
            {isPerp
              ? `每个 PERP 机器人拥有独立 UID、API key 和 ${market.margin_asset ?? market.quote_asset} 保证金钱包。`
              : `每个机器人拥有独立 UID、API key、初始 ${market.quote_asset} 和 ${market.base_asset} 资产快照。`}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={onCreateDefaults} className="rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
            补齐默认 2 个
          </button>
          {!isPerp && (
            <button onClick={onCreateFlow} className="rounded-2xl bg-violet-400/16 px-4 py-2 text-sm text-violet-100">
              补 flow 机器人
            </button>
          )}
          <button onClick={onCreate} className="rounded-2xl bg-emerald-400/16 px-4 py-2 text-sm text-emerald-100">
            创建机器人
          </button>
        </div>
      </div>
      <MarketBotBindingBoundaryStrip market={market} bots={bots} form={form} />
      <details className="mb-4 rounded-2xl border border-cyan-400/12 bg-cyan-400/6 p-3">
        <summary className="cursor-pointer list-none">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <h4 className="font-display text-sm">新增机器人</h4>
            <span className="text-xs text-slate-500">
              参考价反推：{derivedBaseAmount ? `${fmt(derivedBaseAmount, market.qty_precision)} ${market.base_asset}` : "-"}
            </span>
          </div>
        </summary>
	        <div className="mt-3 grid gap-3">
	          <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	            <h5 className="font-display text-sm text-slate-100">身份与凭据</h5>
	            <p className="mt-1 text-xs leading-5 text-slate-500">创建或绑定 mm_bot 登录主体，API Key / Secret 仅服务机器人下单脚本。</p>
	            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">username</span>
	                <input
	                  value={form.username}
	                  onChange={(event) => onFormChange({ username: event.target.value })}
	                  placeholder={`${market.symbol.toLowerCase()}_mm_${nextBotIndex}`}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">bind uid</span>
	                <input
	                  value={form.uid}
	                  onChange={(event) => onFormChange({ uid: event.target.value })}
	                  placeholder="留空则新建 UID"
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">api key</span>
	                <input
	                  value={form.apiKey}
	                  onChange={(event) => onFormChange({ apiKey: event.target.value })}
	                  placeholder="留空自动生成"
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">api secret</span>
	                <input
	                  type="password"
	                  value={form.apiSecret}
	                  onChange={(event) => onFormChange({ apiSecret: event.target.value })}
	                  placeholder="留空自动生成"
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">login password</span>
	                <input
	                  type="password"
	                  value={form.password}
	                  onChange={(event) => onFormChange({ password: event.target.value })}
	                  placeholder="默认 mm123"
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	            </div>
	          </div>
	          <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	            <h5 className="font-display text-sm text-slate-100">策略绑定</h5>
	            <p className="mt-1 text-xs leading-5 text-slate-500">绑定市场内的机器人角色；实例启动、停止和策略运行仍回机器人运营。</p>
	            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">bot label</span>
	                <input
	                  value={form.botLabel}
	                  onChange={(event) => onFormChange({ botLabel: event.target.value })}
	                  placeholder={`${market.symbol}-MM-${nextBotIndex}`}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">role</span>
	                <select
	                  value={form.role}
	                  onChange={(event) => onFormChange({ role: event.target.value as "maker" | "flow" | "hedge" })}
	                  className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none"
	                >
	                  <option value="maker">maker</option>
	                  {!isPerp && <option value="flow">flow</option>}
	                  <option value="hedge">hedge</option>
	                </select>
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs text-slate-500">策略角色</span>
	                <input
	                  value={form.strategyRole}
	                  onChange={(event) => onFormChange({ strategyRole: event.target.value })}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	            </div>
	          </div>
	          <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	            <h5 className="font-display text-sm text-slate-100">{isPerp ? "合约保证金模板" : "现货资金模板"}</h5>
	            <p className="mt-1 text-xs leading-5 text-slate-500">{marketBotTemplateBoundary(market)}</p>
	            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">reference price</span>
	                <input
	                  value={form.referencePrice}
	                  onChange={(event) => onFormChange({ referencePrice: event.target.value })}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">{isPerp ? "margin wallet" : "initial quote"}</span>
	                <input
	                  value={form.initialQuoteAmount}
	                  onChange={(event) => onFormChange({ initialQuoteAmount: event.target.value })}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">base notional</span>
	                <input
	                  value={form.initialBaseNotional}
	                  onChange={(event) => onFormChange({ initialBaseNotional: event.target.value })}
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	              <label className="block min-w-0">
	                <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">base amount override</span>
	                <input
	                  value={form.initialBaseAmount}
	                  onChange={(event) => onFormChange({ initialBaseAmount: event.target.value })}
	                  placeholder="留空则用参考价反推"
	                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
	                />
	              </label>
	            </div>
	          </div>
	          <label className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-3 py-3 text-sm">
	            <span className="text-slate-300">创建后启用该机器人配置</span>
	            <input type="checkbox" checked={form.isEnabled} onChange={(event) => onFormChange({ isEnabled: event.target.checked })} className="h-4 w-4 accent-cyan-400" />
	          </label>
	        </div>
	      </details>
      <div className="grid gap-3 xl:grid-cols-2">
        {bots.map((bot) => {
          const edit = botEdits[bot.id] ?? defaultMarketBotEdit(bot);
          const isPerpFlowBot = isPerp && bot.role === "flow";
          return (
            <div key={bot.id} className="rounded-2xl border border-white/8 bg-white/5 p-3">
              <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-display text-base text-slate-100">{bot.bot_label}</span>
                    <span className={bot.is_enabled ? "rounded-full bg-emerald-400/14 px-2 py-0.5 text-xs text-emerald-100" : "rounded-full bg-rose-500/14 px-2 py-0.5 text-xs text-rose-100"}>
                      {bot.is_enabled ? "enabled" : "paused"}
                    </span>
                  </div>
                  <div className="mt-1 break-all text-xs text-slate-500">
                    uid {bot.uid} · {bot.username} · key {bot.api_key}
                  </div>
                  {isPerpFlowBot && (
                    <div className="mt-1 text-xs leading-5 text-violet-100/80">
                      PERP FLOW 运行资源；用于合约成交流和对账，不是客户 UID 或现货钱包。
                    </div>
                  )}
                </div>
                <span className={`rounded-full px-2 py-0.5 text-xs ${isPerpFlowBot ? "bg-violet-400/14 text-violet-100" : "bg-white/8 text-slate-300"}`}>
                  {isPerpFlowBot ? "PERP FLOW / 对账" : `${bot.role} / ${bot.strategy_role ?? "-"}`}
                </span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                <SurveillanceMetric label={isPerp ? "初始保证金" : "初始 quote"} value={`${fmt(bot.initial_quote_amount, 2)} ${isPerp ? market.margin_asset ?? market.quote_asset : market.quote_asset}`} />
                {isPerp ? (
                  <>
                    <SurveillanceMetric label="保证金钱包" value={`${fmt(bot.contract_account?.wallet_balance, 4)} ${bot.contract_account?.margin_asset ?? market.margin_asset ?? market.quote_asset}`} />
                    <SurveillanceMetric label="可用保证金" value={fmt(bot.contract_account?.available_margin, 4)} />
                    <SurveillanceMetric label="占用保证金" value={fmt(bot.contract_account?.used_margin, 4)} />
                    <SurveillanceMetric label="PnL" value={fmt(bot.contract_account?.unrealized_pnl, 4)} />
                  </>
                ) : (
                  <>
                    <SurveillanceMetric label="初始 base" value={`${fmt(bot.initial_base_amount, market.qty_precision)} ${market.base_asset}`} />
                    <SurveillanceMetric label="base 市值" value={`${fmt(bot.initial_base_notional, 2)} ${market.quote_asset}`} />
                  </>
                )}
                <SurveillanceMetric label="参考价" value={fmt(bot.reference_price, market.price_precision)} />
                {!isPerp && (
                  <>
                    <SurveillanceMetric label={`${bot.quote_balance.asset} 可用`} value={fmt(bot.quote_balance.available, 2)} />
                    <SurveillanceMetric label={`${bot.base_balance.asset} 可用`} value={fmt(bot.base_balance.available, market.qty_precision)} />
                  </>
                )}
                <SurveillanceMetric label="创建时间" value={bjDateTime(bot.created_at)} />
                <SurveillanceMetric label="API secret" value={maskSecret(bot.api_secret)} />
              </div>
              <details className="mt-4 border-t border-white/8 pt-3">
                <summary className="cursor-pointer list-none">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="text-xs uppercase tracking-[0.16em] text-slate-500">编辑机器人</div>
                    <div className="flex flex-wrap gap-2">
                      <button type="button" onClick={(event) => { event.preventDefault(); resetBotEdit(bot); }} className="rounded-full bg-white/8 px-3 py-1.5 text-xs text-slate-200">
                        重置输入
                      </button>
	                      <button type="button" onClick={(event) => { event.preventDefault(); onUpdate(bot.id, edit); }} className="rounded-full bg-emerald-400/16 px-3 py-1.5 text-xs text-emerald-100">
	                        保存账号配置
	                      </button>
                    </div>
                  </div>
                </summary>
	                <div className="mt-3 grid gap-3">
	                  <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	                    <h5 className="font-display text-sm text-slate-100">身份与凭据</h5>
	                    <p className="mt-1 text-xs leading-5 text-slate-500">修改登录主体、API Key / Secret 或密码会影响机器人脚本认证。</p>
	                    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">username</span>
	                        <input value={edit.username} onChange={(event) => updateBotEdit(bot, { username: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">api key</span>
	                        <input value={edit.apiKey} onChange={(event) => updateBotEdit(bot, { apiKey: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">api secret</span>
	                        <input type="password" value={edit.apiSecret} onChange={(event) => updateBotEdit(bot, { apiSecret: event.target.value })} placeholder={bot.api_secret_present ? "保持当前 secret" : "输入新 secret"} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">new password</span>
	                        <input type="password" value={edit.password} onChange={(event) => updateBotEdit(bot, { password: event.target.value })} placeholder="留空不修改" className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                    </div>
	                  </div>
	                  <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	                    <h5 className="font-display text-sm text-slate-100">策略绑定</h5>
	                    <p className="mt-1 text-xs leading-5 text-slate-500">
                        {isPerpFlowBot ? "该账号是 PERP FLOW 运行资源；启动、暂停和参数回机器人运营，本页只维护账号绑定。" : "修改角色会影响该账号在市场内承担 maker、flow 或 hedge 的运行职责。"}
                      </p>
	                    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">bot label</span>
	                        <input value={edit.botLabel} onChange={(event) => updateBotEdit(bot, { botLabel: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">role</span>
	                        <select value={edit.role} onChange={(event) => updateBotEdit(bot, { role: event.target.value as "maker" | "flow" | "hedge" })} className="w-full rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-2.5 text-sm outline-none">
	                          <option value="maker">maker</option>
	                          {!isPerp && <option value="flow">flow</option>}
                            {isPerpFlowBot && <option value="flow">flow（现有 PERP FLOW 资源）</option>}
	                          <option value="hedge">hedge</option>
	                        </select>
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs text-slate-500">策略角色</span>
	                        <input value={edit.strategyRole} onChange={(event) => updateBotEdit(bot, { strategyRole: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                    </div>
	                  </div>
	                  <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-3">
	                    <h5 className="font-display text-sm text-slate-100">{isPerp ? "合约保证金模板" : "现货资金模板"}</h5>
	                    <p className="mt-1 text-xs leading-5 text-slate-500">{marketBotTemplateBoundary(market)}</p>
	                    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">reference price</span>
	                        <input value={edit.referencePrice} onChange={(event) => updateBotEdit(bot, { referencePrice: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">{isPerp ? "margin wallet" : "initial quote"}</span>
	                        <input value={edit.initialQuoteAmount} onChange={(event) => updateBotEdit(bot, { initialQuoteAmount: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">base notional</span>
	                        <input value={edit.initialBaseNotional} onChange={(event) => updateBotEdit(bot, { initialBaseNotional: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                      <label className="block min-w-0">
	                        <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">base amount override</span>
	                        <input value={edit.initialBaseAmount} onChange={(event) => updateBotEdit(bot, { initialBaseAmount: event.target.value })} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none" />
	                      </label>
	                    </div>
	                  </div>
	                  <label className="flex items-center justify-between rounded-2xl border border-white/10 bg-white/5 px-3 py-3 text-sm">
	                    <span className="text-slate-300">启用该机器人配置</span>
	                    <input type="checkbox" checked={edit.isEnabled} onChange={(event) => updateBotEdit(bot, { isEnabled: event.target.checked })} className="h-4 w-4 accent-cyan-400" />
	                  </label>
	                </div>
	              </details>
            </div>
          );
        })}
      </div>
      {bots.length === 0 && (
        <div className="rounded-2xl border border-white/8 bg-slate-950/25 p-4 text-sm text-slate-400">
	          当前币对还没有绑定机器人账号。
        </div>
      )}
    </section>
  );
}

type SystemIssueSeverity = "critical" | "warn" | "info";
type SystemIssueItem = {
  key: string;
  severity: SystemIssueSeverity;
  domain: string;
  title: string;
  detail: string;
  action: string;
  targetSection?: AdminSection;
  targetSystemTab?: SystemAdminTab;
  targetOperationDomain?: AdminOperationAuditFilters["domain"];
  targetOperationStatus?: AdminOperationAuditFilters["status"];
  targetOperationType?: string;
  targetOperationSymbol?: string;
};

const systemIssueRank: Record<SystemIssueSeverity, number> = { critical: 0, warn: 1, info: 2 };

function systemIssueClass(severity: SystemIssueSeverity) {
  if (severity === "critical") return "bg-rose-500/14 text-rose-100";
  if (severity === "warn") return "bg-amber-400/14 text-amber-100";
  return "bg-cyan-400/14 text-cyan-100";
}

function systemIssueLabel(severity: SystemIssueSeverity) {
  if (severity === "critical") return "高";
  if (severity === "warn") return "关注";
  return "提示";
}

function SystemAuditPanel({
  checklist,
  status,
  operations,
  operationFilters,
  operationLoading,
  operationError,
  lastOperationResult,
  activeTab,
  defaultPerpSymbol,
  onOpenSection,
  onOpenTarget,
  onOpenOrderAudit,
  onOpenOperationTarget,
  onTabChange,
  onOperationFiltersChange,
  onRefreshOperations,
  onResetOperationFilters,
  onRefresh,
}: {
  checklist?: DeploymentChecklist;
  status?: SystemStatus;
  operations: AdminOperationAuditItem[];
  operationFilters: AdminOperationAuditFilters;
  operationLoading: boolean;
  operationError: string | null;
  lastOperationResult: AdminOperationResult | null;
  activeTab: SystemAdminTab;
  defaultPerpSymbol?: string;
  onOpenSection: (section: AdminSection) => void;
  onOpenTarget: (target: AdminBusinessTarget) => void;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
  onOpenOperationTarget: (item: AdminOperationAuditItem) => void;
  onTabChange: (tab: SystemAdminTab) => void;
  onOperationFiltersChange: Dispatch<SetStateAction<AdminOperationAuditFilters>>;
  onRefreshOperations: (filters?: AdminOperationAuditFilters) => Promise<void>;
  onResetOperationFilters: () => void;
  onRefresh: () => void;
}) {
  const invariantItems = status?.orderbook_invariants ?? [];
  const wsMetrics = status?.websocket.metrics ?? {};
  const splitCount = invariantItems.reduce((sum, item) => sum + (item.engine_only_count ?? 0) + (item.db_only_count ?? 0), 0);
  const versionAlertCount = invariantItems.reduce(
    (sum, item) => sum + (item.orderbook?.same_seq_diff_count ?? 0) + (item.orderbook?.seq_rollback_count ?? 0),
    0,
  );
  const wsIssueCount = Number(wsMetrics.dropped_sockets ?? 0) + Number(wsMetrics.send_timeouts ?? 0) + Number(wsMetrics.send_failures ?? 0);
  const checklistIssueCount = (checklist?.summary.critical ?? 0) + (checklist?.summary.warn ?? 0);
  const historyError = status?.history_retention?.last_error;
  const failedOperations = operations.filter(isFailedAdminOperation);
  const issueItems: SystemIssueItem[] = [];

  (checklist?.checks ?? []).forEach((item) => {
    if (item.severity === "ok") return;
    issueItems.push({
      key: `check-${item.code}`,
      severity: item.severity === "critical" ? "critical" : "warn",
      domain: "部署检查",
      title: item.label,
      detail: item.detail,
      action: item.action,
      targetSection: "system",
    });
  });
  (status?.warnings ?? []).forEach((warning, index) => {
    issueItems.push({
      key: `warning-${index}`,
      severity: "warn",
      domain: "系统状态",
      title: "运行告警",
      detail: warning,
      action: "查看系统状态",
      targetSection: "system",
    });
  });
  if (splitCount > 0) {
    issueItems.push({
      key: "orderbook-split",
      severity: "critical",
      domain: "盘口一致性",
      title: "簿 / DB 差异",
      detail: `${splitCount} 个 live order 差异`,
      action: "查看一致性明细",
      targetSection: "system",
    });
  }
  if (versionAlertCount > 0) {
    issueItems.push({
      key: "orderbook-version",
      severity: "warn",
      domain: "盘口一致性",
      title: "盘口版本异常",
      detail: `${versionAlertCount} 次 same-seq-diff / rollback`,
      action: "查看一致性明细",
      targetSection: "system",
    });
  }
  if (wsIssueCount > 0) {
    issueItems.push({
      key: "ws-issues",
      severity: "warn",
      domain: "WebSocket",
      title: "发送异常",
      detail: `dropped / timeout / failure = ${wsMetrics.dropped_sockets ?? 0} / ${wsMetrics.send_timeouts ?? 0} / ${wsMetrics.send_failures ?? 0}`,
      action: "查看连接指标",
      targetSection: "system",
    });
  }
  if (historyError) {
    issueItems.push({
      key: "history-retention",
      severity: "warn",
      domain: "历史保留",
      title: "保留任务异常",
      detail: historyError,
      action: "查看运行资源",
      targetSection: "system",
    });
  }
  failedOperations.slice(0, 8).forEach((item) => {
    const operationFilters = adminOperationFiltersForItem(item);
    issueItems.push({
      key: `operation-${item.operation_id}`,
      severity: failedAdminOperationSeverity(item) === "critical" ? "critical" : "warn",
      domain: "操作记录",
      title: operationTypeLabel(item.operation_type),
      detail: `${operationDomainLabel(item.domain)} · ${adminOperationTargetText(item)} · ${adminOperationActorText(item)} · ${item.summary}`,
      action: "查看操作记录",
      targetSection: "system",
      targetSystemTab: "audit",
      targetOperationDomain: operationFilters.domain,
      targetOperationStatus: operationFilters.status,
      targetOperationType: operationFilters.operationType,
      targetOperationSymbol: operationFilters.targetSymbol,
    });
  });

  const sortedIssues = issueItems.sort((left, right) => systemIssueRank[left.severity] - systemIssueRank[right.severity]);
  const systemStatus = checklist?.status === "critical" || status?.status === "critical"
    ? "critical"
    : checklist?.status === "warn" || status?.status === "warn"
      ? "warn"
      : checklist?.status ?? status?.status ?? "loading";
  const statusClass =
    systemStatus === "ok"
      ? "bg-emerald-400/15 text-emerald-100"
      : systemStatus === "critical"
        ? "bg-rose-500/16 text-rose-100"
        : "bg-amber-400/16 text-amber-100";
  const tabs: { key: SystemAdminTab; label: string; hint: string; badge?: string; tone?: "danger" | "warn" | "neutral" }[] = [
    { key: "overview", label: "运维总览", hint: "状态 / 待处理", badge: sortedIssues.length ? String(sortedIssues.length) : undefined, tone: sortedIssues.some((item) => item.severity === "critical") ? "danger" : sortedIssues.length ? "warn" : "neutral" },
    { key: "deployment", label: "部署检查", hint: "凭据 / CORS / 限流", badge: checklistIssueCount ? String(checklistIssueCount) : undefined, tone: (checklist?.summary.critical ?? 0) > 0 ? "danger" : checklistIssueCount ? "warn" : "neutral" },
    { key: "consistency", label: "一致性", hint: "盘口 / WS / 簿DB", badge: splitCount + versionAlertCount + wsIssueCount ? String(splitCount + versionAlertCount + wsIssueCount) : undefined, tone: splitCount > 0 ? "danger" : versionAlertCount + wsIssueCount > 0 ? "warn" : "neutral" },
    { key: "runtime", label: "运行资源", hint: "进程 / DB / 保留", tone: status?.warnings?.length ? "warn" : "neutral" },
    { key: "operations", label: "操作边界", hint: "低频 / 危险动作", tone: "neutral" },
    { key: "audit", label: "操作记录", hint: "失败 / 历史", badge: failedOperations.length ? String(failedOperations.length) : operations.length ? String(operations.length) : undefined, tone: failedOperations.length ? "warn" : "neutral" },
  ];
  const openOperationAudit = (filters: Partial<AdminOperationAuditFilters>) => {
    const nextFilters: AdminOperationAuditFilters = {
      ...defaultAdminOperationFilters(),
      ...filters,
      operationType: adminOperationTypeInput(filters.operationType ?? "") || "all",
      targetSymbol: filters.targetSymbol ? normalizeMarketSymbolInput(filters.targetSymbol) : "",
    };
    onOperationFiltersChange(nextFilters);
    onTabChange("audit");
    void onRefreshOperations(nextFilters);
  };
  const [operationBoundaryView, setOperationBoundaryView] = useState<OperationBoundaryView>("catalog");
  const openOperationBoundaryView = (view: OperationBoundaryView) => {
    setOperationBoundaryView(view);
    onTabChange("operations");
  };

  return (
    <>
      <section className="panel rounded-3xl p-4">
        <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-display text-xl">系统与审计</h2>
              <span className={`rounded-full px-3 py-1 text-xs ${statusClass}`}>{systemStatus}</span>
            </div>
            <p className="mt-1 text-sm text-slate-400">运维视角聚合部署检查、运行资源、盘口一致性、WebSocket、操作失败和低频维护动作边界。</p>
          </div>
          <button type="button" onClick={onRefresh} className="rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12">
            刷新系统状态
          </button>
        </div>
        <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
          {tabs.map((tab) => {
            const active = activeTab === tab.key;
            const badgeClass =
              tab.tone === "danger"
                ? "bg-rose-500/16 text-rose-100"
                : tab.tone === "warn"
                  ? "bg-amber-400/16 text-amber-100"
                  : "bg-white/8 text-slate-300";
            return (
	              <button
	                key={tab.key}
	                type="button"
	                onClick={() => {
                  if (tab.key === "operations") setOperationBoundaryView("catalog");
	                  onTabChange(tab.key);
	                }}
	                className={`min-h-[58px] rounded-xl px-3 py-2 text-left transition ${active ? "bg-cyan-400/16 text-cyan-100" : "bg-white/6 text-slate-300 hover:bg-white/10"}`}
	              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{tab.label}</span>
                  {tab.badge && <span className={`rounded-full px-2 py-0.5 text-[11px] ${badgeClass}`}>{tab.badge}</span>}
                </div>
                <div className="mt-1 truncate text-xs text-slate-500">{tab.hint}</div>
              </button>
            );
          })}
        </div>
        {activeTab === "overview" && (
          <>
            <SystemAuditBoundaryStrip
              systemStatus={systemStatus}
              issueCount={sortedIssues.length}
              criticalIssueCount={sortedIssues.filter((item) => item.severity === "critical").length}
              checklistIssueCount={checklistIssueCount}
              splitCount={splitCount}
              versionAlertCount={versionAlertCount}
              wsIssueCount={wsIssueCount}
              warningCount={status?.warnings.length ?? 0}
              historyError={historyError}
              operationCount={operations.length}
              failedOperationCount={failedOperations.length}
              onTabChange={onTabChange}
              onOpenOperationsView={openOperationBoundaryView}
            />
            <SystemOpsQuickPaths
              issueCount={sortedIssues.length}
              criticalIssueCount={sortedIssues.filter((item) => item.severity === "critical").length}
              checklistIssueCount={checklistIssueCount}
              splitCount={splitCount}
              versionAlertCount={versionAlertCount}
              wsIssueCount={wsIssueCount}
              warningCount={status?.warnings.length ?? 0}
              historyError={historyError}
              operationCount={operations.length}
              failedOperationCount={failedOperations.length}
              databaseMode={status?.database.mode}
              databaseSizeMb={status?.database.size_mb}
              onTabChange={onTabChange}
              onOpenAudit={openOperationAudit}
              onOpenOperationsView={openOperationBoundaryView}
            />
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-8">
              <SurveillanceMetric label="待处理" value={String(sortedIssues.length)} />
              <SurveillanceMetric label="高优先级" value={String(sortedIssues.filter((item) => item.severity === "critical").length)} />
              <SurveillanceMetric label="部署问题" value={`${checklist?.summary.critical ?? 0} / ${checklist?.summary.warn ?? 0}`} />
              <SurveillanceMetric label="系统告警" value={String(status?.warnings.length ?? 0)} />
              <SurveillanceMetric label="簿/DB差异" value={String(splitCount)} />
              <SurveillanceMetric label="版本异常" value={String(versionAlertCount)} />
              <SurveillanceMetric label="WS异常" value={String(wsIssueCount)} />
              <SurveillanceMetric label="数据库" value={status ? `${status.database.mode}${status.database.size_mb != null ? ` · ${fmt(status.database.size_mb, 1)} MB` : ""}` : "-"} />
            </div>
            {lastOperationResult && <SystemLastOperationPanel item={lastOperationResult} onOpenSection={onOpenSection} onOpenTarget={onOpenTarget} />}
            <div className="mt-4">
              {sortedIssues.length > 0 ? (
                <SystemIssueTable
                  items={sortedIssues}
                  onOpen={(item) => {
                    if (item.targetSystemTab) {
                      onTabChange(item.targetSystemTab);
                      if (item.targetSystemTab === "audit" && item.targetOperationStatus && item.targetOperationStatus !== "all") {
                        const nextFilters: AdminOperationAuditFilters = {
                          ...defaultAdminOperationFilters(),
                          domain: item.targetOperationDomain ?? "all",
                          status: item.targetOperationStatus,
                          operationType: item.targetOperationType ?? "all",
                          targetSymbol: item.targetOperationSymbol ?? "",
                        };
                        onOperationFiltersChange(nextFilters);
                        void onRefreshOperations(nextFilters);
                      }
                    }
                    else if (item.domain === "盘口一致性" || item.domain === "WebSocket") onTabChange("consistency");
                    else if (item.domain === "历史保留" || item.domain === "系统状态") onTabChange("runtime");
                    else if (item.domain === "部署检查") onTabChange("deployment");
                    if (item.targetSection && item.targetSection !== "system") onOpenSection(item.targetSection);
                  }}
                />
              ) : (
                <div className="rounded-2xl border border-emerald-400/12 bg-emerald-400/8 px-4 py-3 text-sm text-emerald-100">
                  当前没有系统待处理事项。
                </div>
              )}
            </div>
          </>
        )}
      </section>
      {activeTab === "deployment" && <DeploymentChecklistPanel checklist={checklist} />}
      {activeTab === "consistency" && <SystemConsistencyPanel status={status} />}
      {activeTab === "runtime" && (
        <SystemRuntimePanel
          status={status}
          operationCount={operations.length}
          failedOperationCount={failedOperations.length}
          onOpenOrderAudit={onOpenOrderAudit}
        />
      )}
      {activeTab === "operations" && (
        <SystemOperationsPanel
          defaultPerpSymbol={defaultPerpSymbol}
          activeView={operationBoundaryView}
          onViewChange={setOperationBoundaryView}
          onOpenTarget={onOpenTarget}
          onOpenAudit={openOperationAudit}
        />
      )}
      {activeTab === "audit" && (
        <SystemOperationAuditPanel
          systemStatus={status}
          operations={operations}
          filters={operationFilters}
          loading={operationLoading}
          error={operationError}
          lastOperationResult={lastOperationResult}
          onOpenSection={onOpenSection}
          onOpenTarget={onOpenTarget}
          onOpenOperationTarget={onOpenOperationTarget}
          onFiltersChange={onOperationFiltersChange}
          onRefresh={onRefreshOperations}
          onReset={onResetOperationFilters}
        />
      )}
    </>
  );
}

function SystemOpsQuickPaths({
  issueCount,
  criticalIssueCount,
  checklistIssueCount,
  splitCount,
  versionAlertCount,
  wsIssueCount,
  warningCount,
  historyError,
  operationCount,
  failedOperationCount,
  databaseMode,
  databaseSizeMb,
  onTabChange,
  onOpenAudit,
  onOpenOperationsView,
}: {
  issueCount: number;
  criticalIssueCount: number;
  checklistIssueCount: number;
  splitCount: number;
  versionAlertCount: number;
  wsIssueCount: number;
  warningCount: number;
  historyError?: string | null;
  operationCount: number;
  failedOperationCount: number;
  databaseMode?: string;
  databaseSizeMb?: number | null;
  onTabChange: (tab: SystemAdminTab) => void;
  onOpenAudit: (filters: Partial<AdminOperationAuditFilters>) => void;
  onOpenOperationsView: (view: OperationBoundaryView) => void;
}) {
  const consistencyIssueCount = splitCount + versionAlertCount + wsIssueCount;
  const runtimeStatus = `${databaseMode ?? "-"}${databaseSizeMb != null ? ` · ${fmt(databaseSizeMb, 1)} MB` : ""}`;
  const needsAction = criticalIssueCount > 0 || failedOperationCount > 0 || checklistIssueCount > 0;
  const needsAttention = issueCount > 0 || consistencyIssueCount > 0 || warningCount > 0 || Boolean(historyError);
  const judgment = needsAction ? "需处理" : needsAttention ? "需关注" : "正常";
  const judgmentTone: "neutral" | "warn" | "danger" = needsAction ? "danger" : needsAttention ? "warn" : "neutral";
  const mainDomain =
    failedOperationCount > 0
      ? "操作记录"
      : checklistIssueCount > 0
        ? "部署门禁"
        : consistencyIssueCount > 0
          ? "一致性 / 连接"
          : warningCount > 0 || historyError
            ? "运行资源 / 留存"
            : "常规观察";
  const nextText =
    needsAction
      ? "先看运维总览或失败操作记录，确认是否影响测试继续。"
      : needsAttention
        ? "按信号进入部署、一致性或运行资源分段核对。"
        : "当前只需抽样观察，不在系统页执行写操作。";
  const rows: Array<{
    title: string;
    cue: string;
    status: string;
    tone: "neutral" | "warn" | "danger";
    actionLabel: string;
    onClick: () => void;
  }> = [
    {
      title: "看运维待处理",
      cue: "回到运维总览，先看部署、系统、盘口、连接和失败操作的聚合队列。",
      status: `待处理 ${issueCount} · 高 ${criticalIssueCount}`,
      tone: criticalIssueCount > 0 ? "danger" : issueCount > 0 ? "warn" : "neutral",
      actionLabel: "去运维总览",
      onClick: () => onTabChange("overview"),
    },
    {
      title: "看部署门禁",
      cue: "核对访问凭据、管理入口、网络暴露、持久化、撮合进程和写入保护。",
      status: checklistIssueCount > 0 ? `问题 ${checklistIssueCount}` : "无阻断",
      tone: checklistIssueCount > 0 ? "warn" : "neutral",
      actionLabel: "去部署检查",
      onClick: () => onTabChange("deployment"),
    },
    {
      title: "查盘口 / WS 一致性",
      cue: "核对 orderbook invariants、簿/DB 差异、版本回滚和 WebSocket 发送异常。",
      status: `簿/DB ${splitCount} · 版本 ${versionAlertCount} · WS ${wsIssueCount}`,
      tone: splitCount > 0 ? "danger" : consistencyIssueCount > 0 ? "warn" : "neutral",
      actionLabel: "去一致性",
      onClick: () => onTabChange("consistency"),
    },
    {
      title: "看运行资源 / 留存",
      cue: "查看进程、数据库体量、历史保留、数据留存治理和机器人留存候选统计。",
      status: `${runtimeStatus} · 告警 ${warningCount}${historyError ? " · 保留异常" : ""}`,
      tone: historyError || warningCount > 0 ? "warn" : "neutral",
      actionLabel: "去运行资源",
      onClick: () => onTabChange("runtime"),
    },
    {
      title: "看危险操作目录",
      cue: "核对调账、资金费、ADL、保险基金、清历史、实例控制等动作归属和证据口径。",
      status: "只读目录",
      tone: "neutral",
      actionLabel: "去危险目录",
      onClick: () => onOpenOperationsView("catalog"),
    },
    {
      title: "查操作记录",
      cue: "回看成功维护和前端观察到的失败事实；失败记录会自动带失败状态筛选。",
      status: `失败 ${failedOperationCount} / 最近 ${operationCount}`,
      tone: failedOperationCount > 0 ? "warn" : "neutral",
      actionLabel: failedOperationCount > 0 ? "看失败记录" : "去操作记录",
      onClick: () => {
        if (failedOperationCount > 0) {
          onOpenAudit({ status: "failed" });
          return;
        }
        onTabChange("audit");
      },
    },
  ];

  const toneClass = (tone: "neutral" | "warn" | "danger") => {
    if (tone === "danger") return "border-rose-400/20 bg-rose-500/8 text-rose-100";
    if (tone === "warn") return "border-amber-400/20 bg-amber-400/8 text-amber-100";
    return "border-white/8 bg-slate-950/25 text-slate-100";
  };

  return (
    <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/20 p-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">系统运维快速路径</h3>
        <p className="mt-1 text-sm text-slate-500">按本地沙盒运维常见动作进入现有系统 tab；这里只做查看、筛选和定位，不执行清历史、重建、重启、调账或凭据变更。</p>
      </div>
      <div className="mb-3 grid gap-2 border-y border-white/8 py-3 md:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">当前判断</div>
          <div className="mt-1 text-sm text-slate-200">{judgment} · 待处理 {issueCount} · 高优先级 {criticalIssueCount}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">现在看什么</div>
          <div className="mt-1 text-sm text-slate-200">{mainDomain}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">下一步</div>
          <div className={`mt-1 text-sm ${judgmentTone === "danger" ? "text-rose-100" : judgmentTone === "warn" ? "text-amber-100" : "text-slate-400"}`}>{nextText}</div>
        </div>
      </div>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => (
          <button
            key={row.title}
            type="button"
            onClick={row.onClick}
            className={`min-h-[112px] rounded-xl border p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-400/8 ${toneClass(row.tone)}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{row.title}</span>
              <span className="shrink-0 rounded-full bg-white/8 px-2 py-0.5 text-[11px] text-slate-200">{row.status}</span>
            </div>
            <div className="mt-2 min-h-[40px] text-xs leading-5 text-slate-500">{row.cue}</div>
            <div className="mt-3 text-xs text-cyan-100">{row.actionLabel}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function SystemAuditBoundaryStrip({
  systemStatus,
  issueCount,
  criticalIssueCount,
  checklistIssueCount,
  splitCount,
  versionAlertCount,
  wsIssueCount,
  warningCount,
  historyError,
  operationCount,
  failedOperationCount,
  onTabChange,
  onOpenOperationsView,
}: {
  systemStatus: string;
  issueCount: number;
  criticalIssueCount: number;
  checklistIssueCount: number;
  splitCount: number;
  versionAlertCount: number;
  wsIssueCount: number;
  warningCount: number;
  historyError?: string | null;
  operationCount: number;
  failedOperationCount: number;
  onTabChange: (tab: SystemAdminTab) => void;
  onOpenOperationsView: (view: OperationBoundaryView) => void;
}) {
  const rows: Array<{
    domain: string;
    owner: string;
    status: string;
    boundary: string;
    tab: SystemAdminTab;
  }> = [
    {
      domain: "运维总览",
      owner: "系统与审计",
      status: `${systemStatus} · 待处理 ${issueCount} · 高优先级 ${criticalIssueCount}`,
      boundary: "聚合部署、运行、一致性和失败操作的值班队列；只做分流，不直接执行清算、调账、清历史或重启。",
      tab: "overview",
    },
    {
      domain: "部署检查",
      owner: "部署检查",
      status: `问题 ${checklistIssueCount}`,
      boundary: "检查凭据、CORS、限流、生产部署和启动形态；不在这里改配置文件、写密钥或改变运行模式。",
      tab: "deployment",
    },
    {
      domain: "一致性 / 连接",
      owner: "一致性",
      status: `簿DB差异 ${splitCount} · 版本异常 ${versionAlertCount} · WS异常 ${wsIssueCount}`,
      boundary: "观察 orderbook invariants、engine/DB 差异和 WebSocket 发送状态；重建或清理类动作仍回业务入口并保留确认。",
      tab: "consistency",
    },
    {
      domain: "运行资源",
      owner: "运行资源",
      status: `系统告警 ${warningCount} · 历史保留 ${historyError ? "异常" : "正常"}`,
      boundary: "查看单进程、PID、数据库体量、历史保留和内存盘口摘要；不在系统页直接扩容、迁库或清库。",
      tab: "runtime",
    },
    {
      domain: "危险 / 低频操作目录",
      owner: "操作边界",
      status: "目录与控制点",
      boundary: "列出调账、清历史、资金费、ADL、保险基金、风险阶梯和实例停机等动作归属；这里只导航和查记录。",
      tab: "operations",
    },
    {
      domain: "操作记录",
      owner: "操作记录",
      status: `记录 ${operationCount} · 失败 ${failedOperationCount}`,
      boundary: "回看关键维护动作成功结果和前端观察到的失败事实；不是不可变审计账本，也不自动重试或补偿。",
      tab: "audit",
    },
  ];

  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">系统与审计边界</h3>
        <p className="mt-1 text-sm text-slate-500">系统页是运维观察、审计回看和危险操作目录，不是清算、调账、清理或实例控制的快捷执行台。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">系统域</th>
              <th className="px-3 py-2 font-normal">主入口</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 text-right font-normal">去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
                <td className="px-3 py-2">{row.owner}</td>
	                <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
	                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
	                <td className="px-3 py-2 text-right">
	                  <button
	                    type="button"
	                    onClick={() => row.tab === "operations" ? onOpenOperationsView("catalog") : onTabChange(row.tab)}
	                    className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-200 transition hover:bg-cyan-400/16 hover:text-cyan-100"
	                  >
	                    打开
	                  </button>
	                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const operationTypeLabels: Record<string, string> = {
  adjust_contract_account: "合约保证金调账",
  adjust_spot_balance: "现货调账",
  cancel_fallback_liquidity: "撤兜底流动性",
	  create_default_flow_bot: "补 FLOW 机器人",
	  create_default_market_bots: "补默认机器人",
	  create_market_bot: "创建机器人账号",
		  create_user: "创建登录主体",
	  execute_adl: "执行 ADL",
  flow_control_pause: "暂停 FLOW",
  flow_control_start: "启动 FLOW",
	  admin_operation_failure: "前端观察到的失败",
  frontend_operation_failure: "前端观察到的失败",
  history_retention_run: "历史保留",
  maker_instance_restart: "重启实例",
  maker_instance_start: "启动实例",
  maker_instance_stop: "停止实例",
  maker_instance_stop_and_cancel: "停止并撤单",
  orderbook_rebuild: "重建订单簿",
  reset_contract_maker_state: "重置合约机器人",
  reset_spot_market: "撤销现货挂单",
  reset_market: "撤销市场挂单",
  reset_test_users: "重置测试主体资金",
  reset_user_balances: "重置单 UID 现货资金",
  rotate_api_key: "轮换 API Key",
  retry_failed_funding_jobs: "批量重试资金费",
  retry_funding_job: "重试资金费",
  save_risk_tiers: "保存风险阶梯",
  seed_contract_book: "铺合约盘口",
  seed_contract_orderbook: "铺合约盘口",
  seed_spot_book: "铺现货盘口",
  seed_spot_orderbook: "铺现货盘口",
  settle_funding: "结算资金费",
  update_contract_trading_mode: "切换合约交易模式",
  update_market_bot: "保存机器人账号",
  update_market_fees: "保存市场默认费率",
  update_market_strategy: "保存机器人策略",
  update_user_fees: "保存单市场费率",
  update_user_fees_all: "保存全市场费率",
  update_user_identity: "保存主体身份",
  update_risk_tiers: "更新风险阶梯",
  wipe_market_data: "清市场历史",
  wipe_market_klines: "清 K 线",
  wipe_spot_market_data: "清市场历史",
  adjust_insurance_fund: "保险基金调账",
};

function operationDomainLabel(domain: string) {
  const labels: Record<string, string> = {
    account: "账户",
    market: "市场",
    bot: "机器人",
    contract: "合约",
    system: "系统",
  };
  return labels[domain] ?? domain;
}

function operationTypeLabel(type: string) {
  return operationTypeLabels[type] ?? type.replace(/_/g, " ");
}

function adminOperationSeverity(domainRaw: string, operationTypeRaw: string): "danger" | "warn" {
  const domain = adminOperationAuditDomain(domainRaw);
  const operationType = adminOperationTypeInput(operationTypeRaw);
  const dangerTypes = new Set([
    "reset_test_users",
    "reset_spot_market",
    "reset_market",
    "wipe_spot_market_data",
    "wipe_market_data",
    "update_contract_trading_mode",
    "adjust_contract_account",
    "settle_funding",
    "retry_failed_funding_jobs",
    "execute_adl",
    "adjust_insurance_fund",
    "update_risk_tiers",
    "save_risk_tiers",
    "maker_instance_stop_and_cancel",
    "history_retention_run",
    "orderbook_rebuild",
  ]);
  if (dangerTypes.has(operationType)) return "danger";
  if (domain === "contract" && ["settle_funding", "execute_adl", "adjust_insurance_fund", "adjust_contract_account"].includes(operationType)) return "danger";
  return "warn";
}

function adminOperationApprovalProfile({
  domain,
  operationType,
  severity,
}: {
  domain: string;
  operationType: string;
  severity?: "danger" | "warn";
}): AdminOperationApprovalProfile {
  const normalizedDomain = adminOperationAuditDomain(domain);
  const normalizedOperationType = adminOperationTypeInput(operationType);
  const resolvedSeverity = severity ?? adminOperationSeverity(normalizedDomain, normalizedOperationType);
  const changesFundsOrClearing =
    normalizedDomain === "account" ||
    normalizedOperationType === "adjust_contract_account" ||
    normalizedOperationType === "settle_funding" ||
    normalizedOperationType === "retry_failed_funding_jobs" ||
    normalizedOperationType === "execute_adl" ||
    normalizedOperationType === "adjust_insurance_fund" ||
    normalizedOperationType === "update_risk_tiers" ||
    normalizedOperationType === "save_risk_tiers";
  const changesRuntime =
    normalizedDomain === "bot" ||
    normalizedOperationType === "seed_contract_orderbook" ||
    normalizedOperationType === "seed_contract_book" ||
    normalizedOperationType === "orderbook_rebuild" ||
    normalizedOperationType === "update_contract_trading_mode";
  if (resolvedSeverity === "danger" && changesFundsOrClearing) {
    return {
      level: "approval_required",
      label: "缺正式审批",
      evidencePackage: "操作记录 + 业务 source-of-truth + 执行前/后资金或清算快照",
      gap: "缺审批单、复核人、执行窗口、回滚/补偿单和日终对账包",
      tone: "danger",
    };
  }
  if (resolvedSeverity === "danger") {
    return {
      level: "change_window_required",
      label: "需变更窗口",
      evidencePackage: "操作记录 + 目标市场/实例状态 + 执行前后盘口或历史范围快照",
      gap: "缺发布/演练单、复核人、影响范围确认和回滚记录",
      tone: "warn",
    };
  }
  if (changesRuntime) {
    return {
      level: "runtime_confirmed",
      label: "运行窗口确认",
      evidencePackage: "操作记录 + 实例状态 + 队列/盘口/策略核对",
      gap: "缺正式运行窗口、复核人和自动回滚记录",
      tone: "limited",
    };
  }
  return {
    level: "sandbox_confirmed",
    label: "沙盒确认",
    evidencePackage: "前端确认 + 后端 guard + 操作记录 + 业务页复核",
    gap: "缺生产级审批流、岗位权限和不可变审计归档",
    tone: "limited",
  };
}

function operationStatusClass(status: string) {
  if (status === "success") return "bg-emerald-400/15 text-emerald-100";
  if (status === "failed" || status === "error") return "bg-rose-500/16 text-rose-100";
  return "bg-amber-400/16 text-amber-100";
}

function operationTargetLabel(item: AdminOperationAuditItem) {
  if (item.domain === "account") return "看账户";
  if (item.domain === "market") return "看市场";
  if (item.domain === "bot") return "看实例";
  if (item.domain === "contract") {
    const targetTab = contractTabForOperationType(item.operation_type);
    if (targetTab === "accounts") return "看保证金";
    if (targetTab === "orders") return "看合约订单";
    if (targetTab === "funding") return "看资金费";
    if (targetTab === "liquidation") return "看强平ADL";
    if (targetTab === "insurance") return "看保险基金";
    if (targetTab === "risk") return "看风险参数";
    return "看合约概览";
  }
  if (item.domain === "system") {
    const operationType = adminOperationTypeInput(item.operation_type);
    if (operationType === "orderbook_rebuild") return "看一致性";
    if (operationType === "history_retention_run") return "看运行资源";
  }
  return "看系统";
}

function jsonPreview(value: unknown) {
  if (value == null) return "-";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function csvCell(value: unknown) {
  const raw = value == null ? "" : String(value);
  const normalized = raw.replace(/\r?\n/g, " ").trim();
  const safe = /^[=+\-@\t]/.test(normalized) ? `'${normalized}` : normalized;
  return `"${safe.replace(/"/g, '""')}"`;
}

function downloadCsv(filename: string, headers: string[], rows: unknown[][]) {
  const csv = [
    headers.map(csvCell).join(","),
    ...rows.map((row) => row.map(csvCell).join(",")),
  ].join("\n");
  const blob = new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function SystemLastOperationPanel({
  item,
  onOpenSection,
  onOpenTarget,
}: {
  item: AdminOperationResult;
  onOpenSection?: (section: AdminSection) => void;
  onOpenTarget?: (target: AdminBusinessTarget) => void;
}) {
  const openRecoveryTarget = () => {
    const targetSection = item.recovery?.targetSection;
    if (!targetSection) return;
    if (onOpenTarget) {
      onOpenTarget({
        section: targetSection,
        targetSymbol: item.recovery?.targetSymbol,
        marketDetailTab: item.recovery?.targetMarketDetailTab,
        contractTab: item.recovery?.targetContractTab,
        systemTab: item.recovery?.targetSystemTab,
      });
      return;
    }
    onOpenSection?.(targetSection);
  };
  return (
    <div className={`mt-4 rounded-2xl border px-4 py-3 ${item.status === "success" ? "border-emerald-400/14 bg-emerald-400/8" : "border-rose-400/14 bg-rose-500/8"}`}>
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs ${operationStatusClass(item.status)}`}>{item.status === "success" ? "成功" : "异常"}</span>
            <h3 className="font-display text-base text-slate-100">{item.title}</h3>
          </div>
          <p className="mt-1 text-sm text-slate-300">{item.message}</p>
          <p className="mt-1 text-xs text-slate-500">{bjDateTime(item.ts)}</p>
        </div>
        {item.result != null && (
          <pre className="max-h-[180px] w-full overflow-auto rounded-xl bg-slate-950/35 p-3 text-xs leading-5 text-slate-300 xl:max-w-[520px]">
            {jsonPreview(item.result)}
          </pre>
        )}
      </div>
      {item.status === "error" && item.recovery && (
        <div className="mt-3 border-t border-white/10 pt-3">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="text-sm font-medium text-rose-100">{item.recovery.title}</div>
              <ul className="mt-2 space-y-1 text-xs leading-5 text-slate-300">
                {item.recovery.steps.map((step) => (
                  <li key={step}>- {step}</li>
                ))}
              </ul>
            </div>
            {item.recovery.targetSection && (onOpenTarget || onOpenSection) && (
              <button
                type="button"
                onClick={openRecoveryTarget}
                className="rounded-xl bg-white/10 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/15"
              >
                {item.recovery.targetLabel ?? "打开相关页面"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function SystemOperationDetailPanel({
  item,
  onClose,
  onOpenOperationTarget,
}: {
  item: AdminOperationAuditItem;
  onClose: () => void;
  onOpenOperationTarget: (item: AdminOperationAuditItem) => void;
}) {
  const evidence = adminOperationEvidence(item);
  const approval = adminOperationApprovalProfile({ domain: item.domain, operationType: item.operation_type });
  const archive = adminOperationArchiveProfile(item);
  const resultText = jsonPreview(item.result);
  const statusClass = operationStatusClass(item.status);
  return (
    <div className="mb-4 border-y border-white/8 py-3">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base text-slate-100">操作记录详情</h3>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-500">
            <span className="font-mono">{item.operation_id}</span>
            <span>{bjDateTime(item.created_at)}</span>
            <span className={`rounded-full px-2 py-0.5 ${statusClass}`}>{item.status}</span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => onOpenOperationTarget(item)}
            className="rounded-xl bg-cyan-400/14 px-3 py-1.5 text-xs text-cyan-100 hover:bg-cyan-400/20"
          >
            {operationTargetLabel(item)}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12"
          >
            关闭
          </button>
        </div>
      </div>
      <div className="grid gap-3 text-xs md:grid-cols-2 xl:grid-cols-4">
        {[
          ["业务域", operationDomainLabel(item.domain)],
          ["操作类型", `${operationTypeLabel(item.operation_type)} / ${item.operation_type}`],
          ["目标", adminOperationTargetText(item)],
          ["执行人", adminOperationActorText(item)],
        ].map(([label, value]) => (
          <div key={label} className="border-t border-white/8 pt-2">
            <div className="text-slate-500">{label}</div>
            <div className="mt-1 break-words text-slate-200">{value}</div>
          </div>
        ))}
      </div>
      <div className="mt-3 text-xs leading-5 text-slate-400">
        <span className="text-slate-500">摘要：</span>{item.summary}
      </div>
      <div className="mt-3 grid gap-3 text-xs lg:grid-cols-3">
        <div className="border-t border-white/8 pt-2">
          <div className="font-medium text-slate-100">证据口径</div>
          <div className="mt-1 text-slate-300">{evidence.level}</div>
          <div className="mt-1 leading-5 text-slate-400">{evidence.verificationPath}</div>
          <div className="mt-1 leading-5 text-slate-500">{evidence.gap}</div>
        </div>
        <div className="border-t border-white/8 pt-2">
          <div className="font-medium text-slate-100">审批缺口</div>
          <div className="mt-1 text-slate-300">{approval.label}</div>
          <div className="mt-1 leading-5 text-slate-400">{approval.evidencePackage}</div>
          <div className="mt-1 leading-5 text-slate-500">{approval.gap}</div>
        </div>
        <div className="border-t border-white/8 pt-2">
          <div className="font-medium text-slate-100">归档交接</div>
          <div className="mt-1 text-slate-300">{archive.source} / {archive.status}</div>
          <div className="mt-1 leading-5 text-slate-400">{archive.proofBoundary}</div>
          <div className="mt-1 leading-5 text-slate-500">{archive.gap}</div>
          <div className="mt-1 leading-5 text-cyan-100/80">后续：{archive.followUp}</div>
        </div>
      </div>
      <details className="mt-3 border-t border-white/8 pt-2 text-xs">
        <summary className="cursor-pointer text-slate-300">原始结果 JSON</summary>
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-xl bg-black/20 p-3 font-mono text-[11px] leading-5 text-slate-300">{resultText}</pre>
      </details>
    </div>
  );
}

function SystemOperationAuditPanel({
  systemStatus,
  operations,
  filters,
  loading,
  error,
  lastOperationResult,
  onOpenSection,
  onOpenTarget,
  onOpenOperationTarget,
  onFiltersChange,
  onRefresh,
  onReset,
}: {
  systemStatus?: SystemStatus;
  operations: AdminOperationAuditItem[];
  filters: AdminOperationAuditFilters;
  loading: boolean;
  error: string | null;
  lastOperationResult: AdminOperationResult | null;
  onOpenSection: (section: AdminSection) => void;
  onOpenTarget: (target: AdminBusinessTarget) => void;
  onOpenOperationTarget: (item: AdminOperationAuditItem) => void;
  onFiltersChange: Dispatch<SetStateAction<AdminOperationAuditFilters>>;
  onRefresh: (filters?: AdminOperationAuditFilters) => Promise<void>;
  onReset: () => void;
}) {
  const domainOptions: { value: AdminOperationAuditFilters["domain"]; label: string }[] = [
    { value: "all", label: "全部业务域" },
    { value: "account", label: "账户" },
    { value: "market", label: "市场" },
    { value: "bot", label: "机器人" },
    { value: "contract", label: "合约" },
    { value: "system", label: "系统" },
  ];
  const statusOptions: { value: AdminOperationAuditFilters["status"]; label: string }[] = [
    { value: "all", label: "全部状态" },
    { value: "success", label: "成功" },
    { value: "failed", label: "失败" },
    { value: "error", label: "异常" },
  ];
  const operationTypeOptions = Array.from(
    new Set([
      ...Object.keys(operationTypeLabels),
      ...adminOperationFailureRules.map((rule) => rule.operationType),
      ...operations.map((item) => item.operation_type),
      filters.operationType !== "all" ? filters.operationType : "",
    ].filter(Boolean)),
  ).sort((left, right) => operationTypeLabel(left).localeCompare(operationTypeLabel(right)));
  const limitOptions: AdminOperationAuditFilters["limit"][] = ["50", "100", "200"];
  const successCount = operations.filter((item) => item.status === "success").length;
  const failedCount = operations.filter(isFailedAdminOperation).length;
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(operations.length / AUDIT_TABLE_PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visibleOperations = operations.slice((safePage - 1) * AUDIT_TABLE_PAGE_SIZE, safePage * AUDIT_TABLE_PAGE_SIZE);
  const [selectedOperationId, setSelectedOperationId] = useState<string | null>(null);
  const selectedOperation = selectedOperationId ? operations.find((item) => item.operation_id === selectedOperationId) ?? null : null;
  useEffect(() => {
    setPage(1);
  }, [operations]);
  useEffect(() => {
    if (selectedOperationId && !operations.some((item) => item.operation_id === selectedOperationId)) {
      setSelectedOperationId(null);
    }
  }, [operations, selectedOperationId]);
  const exportCurrentOperations = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const scope = [
      filters.domain !== "all" ? filters.domain : "all",
      filters.status !== "all" ? filters.status : "all",
      filters.operationType !== "all" ? adminOperationTypeInput(filters.operationType) : "all",
      filters.targetSymbol ? normalizeMarketSymbolInput(filters.targetSymbol) : "all",
    ].join("_");
    downloadCsv(
      `admin_operations_${scope}_${stamp}.csv`,
      [
        "created_at",
        "status",
        "domain",
        "domain_raw",
        "operation",
        "operation_type",
        "target_type",
        "target_id",
        "target_symbol",
	        "actor",
	        "summary",
	        "evidence_level",
	        "verification_path",
	        "evidence_gap",
	        "approval_readiness",
	        "approval_evidence_package",
	        "approval_gap",
          "archive_source",
          "archive_status",
          "archive_proof_boundary",
          "archive_gap",
          "archive_follow_up",
	        "result_json",
	      ],
	      operations.map((item) => {
	        const evidence = adminOperationEvidence(item);
	        const approval = adminOperationApprovalProfile({ domain: item.domain, operationType: item.operation_type });
          const archive = adminOperationArchiveProfile(item);
	        return [
	          bjDateTime(item.created_at),
	          item.status,
	          operationDomainLabel(item.domain),
	          item.domain,
	          operationTypeLabel(item.operation_type),
	          item.operation_type,
	          item.target_type,
	          item.target_id ?? "",
	          item.target_symbol ?? "",
	          item.actor_username ?? (item.actor_user_id != null ? `#${item.actor_user_id}` : ""),
	          item.summary,
	          evidence.level,
	          evidence.verificationPath,
	          evidence.gap,
	          approval.label,
	          approval.evidencePackage,
	          approval.gap,
            archive.source,
            archive.status,
            archive.proofBoundary,
            archive.gap,
            archive.followUp,
	          jsonPreview(item.result),
	        ];
	      }),
	    );
	  };
	  const evidenceClass = (tone: AdminOperationEvidence["tone"]) =>
	    tone === "proof"
	      ? "bg-emerald-400/12 text-emerald-100"
	      : tone === "needs_check"
	        ? "bg-rose-500/16 text-rose-100"
	        : "bg-amber-400/12 text-amber-100";
	  const approvalClass = (tone: AdminOperationApprovalProfile["tone"]) =>
	    tone === "danger"
	      ? "bg-rose-500/16 text-rose-100"
	      : tone === "warn"
	        ? "bg-amber-400/12 text-amber-100"
	        : "bg-white/8 text-slate-300";
	  const archiveClass = (archive: AdminOperationArchiveProfile) =>
	    archive.source === "前端观察失败"
	      ? "bg-rose-500/16 text-rose-100"
	      : archive.source.includes("资金") || archive.source.includes("清算")
	        ? "bg-amber-400/12 text-amber-100"
	        : "bg-white/8 text-slate-300";
	  return (
	    <section className="panel rounded-3xl p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-lg">管理员操作记录</h2>
          <p className="mt-1 text-sm text-slate-400">持久记录高风险和低频维护动作的成功结果，以及后台页面观察到的接口失败；最近执行结果继续显示本机页面内详情。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white/8 px-3 py-1 text-xs text-slate-300">当前 {operations.length} 条</span>
          <button
            type="button"
            onClick={exportCurrentOperations}
            disabled={loading || operations.length === 0}
            className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-50"
          >
            导出当前操作记录 CSV
          </button>
        </div>
      </div>
      <AuditEvidenceBoundaryStrip
        operationCount={operations.length}
        successCount={successCount}
        failedCount={failedCount}
        filters={filters}
      />
      <AuditArchiveGapIndex
        systemStatus={systemStatus}
        operationCount={operations.length}
        failedCount={failedCount}
        filters={filters}
      />
      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-[1fr_1fr_1.15fr_1.2fr_0.8fr_auto_auto]">
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">业务域</span>
          <select
            value={filters.domain}
            onChange={(event) => onFiltersChange((prev) => ({ ...prev, domain: event.target.value as AdminOperationAuditFilters["domain"] }))}
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100"
          >
            {domainOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">结果状态</span>
          <select
            value={filters.status}
            onChange={(event) => onFiltersChange((prev) => ({ ...prev, status: event.target.value as AdminOperationAuditFilters["status"] }))}
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100"
          >
            {statusOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">操作类型</span>
          <select
            value={filters.operationType}
            onChange={(event) => onFiltersChange((prev) => ({ ...prev, operationType: adminOperationTypeInput(event.target.value) || "all" }))}
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100"
          >
            <option value="all">全部操作</option>
            {operationTypeOptions.map((item) => <option key={item} value={item}>{operationTypeLabel(item)}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">市场 / 标的</span>
          <input
            value={filters.targetSymbol}
            onChange={(event) => onFiltersChange((prev) => ({ ...prev, targetSymbol: event.target.value }))}
            placeholder="BTCUSDT-PERP"
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-600"
          />
        </label>
        <label className="text-xs text-slate-400">
          <span className="mb-1 block">条数</span>
          <select
            value={filters.limit}
            onChange={(event) => onFiltersChange((prev) => ({ ...prev, limit: event.target.value as AdminOperationAuditFilters["limit"] }))}
            className="w-full rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-sm text-slate-100"
          >
            {limitOptions.map((item) => <option key={item} value={item}>最近 {item} 条</option>)}
          </select>
        </label>
        <button
          type="button"
          onClick={() => void onRefresh()}
          disabled={loading}
          className="self-end rounded-xl bg-cyan-400/16 px-3 py-2 text-sm text-cyan-100 hover:bg-cyan-400/22 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {loading ? "筛选中" : "应用筛选"}
        </button>
        <button
          type="button"
          onClick={onReset}
          disabled={loading}
          className="self-end rounded-xl bg-white/8 px-3 py-2 text-sm text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-60"
        >
          重置
        </button>
      </div>
      {error && <div className="mb-4 rounded-xl border border-rose-400/14 bg-rose-500/8 px-3 py-2 text-sm text-rose-100">{error}</div>}
      {lastOperationResult && <SystemLastOperationPanel item={lastOperationResult} onOpenSection={onOpenSection} onOpenTarget={onOpenTarget} />}
      {selectedOperation && (
        <SystemOperationDetailPanel
          item={selectedOperation}
          onClose={() => setSelectedOperationId(null)}
          onOpenOperationTarget={onOpenOperationTarget}
        />
      )}
      <AuditTablePager page={safePage} total={operations.length} pageSize={AUDIT_TABLE_PAGE_SIZE} label="操作记录分页" onPageChange={setPage} />
	      <div className="mt-4 overflow-auto">
	        <table className="min-w-[2140px] text-left text-sm">
	          <thead className="text-slate-500">
	            <tr>
	              <th className="pb-2 pr-3">时间</th>
	              <th className="pb-2 pr-3">状态</th>
	              <th className="pb-2 pr-3">域</th>
	              <th className="pb-2 pr-3">操作</th>
	              <th className="pb-2 pr-3">目标</th>
	              <th className="pb-2 pr-3">执行人</th>
	              <th className="pb-2 pr-3">摘要</th>
	              <th className="pb-2 pr-3">证据口径</th>
	              <th className="pb-2 pr-3">审批缺口</th>
	              <th className="pb-2 pr-3">归档交接</th>
	              <th className="pb-2 pr-3">详情 / 定位</th>
	            </tr>
	          </thead>
	          <tbody className="text-slate-200">
	            {visibleOperations.map((item) => {
	              const evidence = adminOperationEvidence(item);
	              const approval = adminOperationApprovalProfile({ domain: item.domain, operationType: item.operation_type });
	              const archive = adminOperationArchiveProfile(item);
	              return (
	                <tr key={item.operation_id} className="border-t border-white/8 align-top">
	                  <td className="py-2 pr-3 font-mono text-xs text-slate-400">{bjDateTime(item.created_at)}</td>
	                  <td className="py-2 pr-3"><span className={`rounded-full px-2 py-0.5 text-xs ${operationStatusClass(item.status)}`}>{item.status}</span></td>
	                  <td className="py-2 pr-3">{operationDomainLabel(item.domain)}</td>
	                  <td className="py-2 pr-3 font-medium text-slate-100">{operationTypeLabel(item.operation_type)}</td>
	                  <td className="py-2 pr-3 font-mono text-xs text-slate-300">{item.target_symbol ?? item.target_id ?? item.target_type}</td>
	                  <td className="py-2 pr-3">{item.actor_username ?? `#${item.actor_user_id ?? "-"}`}</td>
	                  <td className="max-w-[360px] py-2 pr-3 text-xs leading-5 text-slate-400">{item.summary}</td>
	                  <td className="max-w-[360px] py-2 pr-3 text-xs leading-5">
	                    <span className={`inline-flex rounded-full px-2 py-0.5 ${evidenceClass(evidence.tone)}`}>{evidence.level}</span>
	                    <div className="mt-1 text-slate-300">{evidence.verificationPath}</div>
	                    <div className="mt-1 text-slate-500">{evidence.gap}</div>
	                  </td>
	                  <td className="max-w-[320px] py-2 pr-3 text-xs leading-5">
	                    <span className={`inline-flex rounded-full px-2 py-0.5 ${approvalClass(approval.tone)}`}>{approval.label}</span>
	                    <div className="mt-1 text-slate-300">{approval.evidencePackage}</div>
	                    <div className="mt-1 text-slate-500">{approval.gap}</div>
	                  </td>
	                  <td className="max-w-[360px] py-2 pr-3 text-xs leading-5">
	                    <span className={`inline-flex rounded-full px-2 py-0.5 ${archiveClass(archive)}`}>{archive.source}</span>
	                    <div className="mt-1 text-slate-300">{archive.proofBoundary}</div>
	                    <div className="mt-1 text-slate-500">{archive.gap}</div>
	                    <div className="mt-1 text-cyan-100/80">后续：{archive.followUp}</div>
	                  </td>
	                  <td className="py-2 pr-3">
	                    <button
	                      type="button"
	                      onClick={() => setSelectedOperationId(item.operation_id)}
	                      className="mb-1 block rounded-xl bg-cyan-400/14 px-3 py-1.5 text-xs text-cyan-100 hover:bg-cyan-400/20"
	                    >
	                      详情
	                    </button>
	                    <button
	                      type="button"
	                      onClick={() => onOpenOperationTarget(item)}
	                      className="block rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12"
	                    >
	                      {operationTargetLabel(item)}
	                    </button>
	                  </td>
	                </tr>
	              );
	            })}
	            {operations.length === 0 && (
	              <tr><td className="py-8 text-center text-sm text-slate-500" colSpan={11}>暂无管理员操作记录。</td></tr>
	            )}
	          </tbody>
	        </table>
      </div>
    </section>
  );
}

function AuditEvidenceBoundaryStrip({
  operationCount,
  successCount,
  failedCount,
  filters,
}: {
  operationCount: number;
  successCount: number;
  failedCount: number;
  filters: AdminOperationAuditFilters;
}) {
  const scopeParts = [
    filters.domain !== "all" ? operationDomainLabel(filters.domain) : "全部业务域",
    filters.status !== "all" ? filters.status : "全部状态",
    filters.operationType !== "all" ? operationTypeLabel(filters.operationType) : "全部操作",
    filters.targetSymbol ? normalizeMarketSymbolInput(filters.targetSymbol) : "全部市场",
    `最近 ${filters.limit} 条`,
  ];
  const rows: Array<{
    layer: string;
    status: string;
    use: string;
    boundary: string;
    tone: "active" | "limited" | "future";
  }> = [
    {
      layer: "记录来源",
      status: "管理员写入 + 前端观察失败",
      use: "回看调账、清历史、资金费、ADL、保险基金、风险参数和实例控制等维护动作。",
      boundary: "不是订单、成交、合约流水或资金账本的 source-of-truth。",
      tone: "active",
    },
    {
      layer: "成功记录",
      status: `当前 ${successCount} 条`,
      use: "确认高风险或低频维护动作是否完成、执行人是谁、目标是什么、返回摘要是什么。",
      boundary: "只记录已接入的管理员动作；不代表所有系统内部任务、机器人行为或撮合事件。",
      tone: "active",
    },
    {
      layer: "失败记录",
      status: `当前 ${failedCount} 条`,
      use: "把后台页面观察到的接口失败带入总览、风险监控和操作记录，便于值班排查。",
      boundary: "失败记录不自动重试、不补偿、不证明底层业务状态已经回滚。",
      tone: failedCount > 0 ? "limited" : "active",
    },
    {
      layer: "查询口径",
      status: scopeParts.join(" · "),
      use: "按业务域、结果状态、操作类型、市场/标的和条数做最近记录回看。",
      boundary: "当前接口仍是最近记录查询，不是全量分页审计检索或合规归档搜索。",
      tone: "limited",
    },
	    {
	      layer: "导出口径",
	      status: `当前已加载 ${operationCount} 条`,
	      use: "导出当前页面已加载筛选结果，包含证据级别、核对路径、证据缺口、审批准备度和证据包口径。",
	      boundary: "导出不扩大查询范围、不触发后端写入，也不是日终审计包、审批单或不可变归档。",
	      tone: "limited",
	    },
    {
      layer: "审批缺口",
      status: "只读派生口径",
      use: "提示这条操作还缺正式审批、变更窗口、复核人、回滚记录或日终对账包，便于运营交接。",
      boundary: "不创建审批单、不改变权限、不阻断既有确认流程，也不替代 maker/checker。",
      tone: "limited",
    },
    {
      layer: "审计缺口",
      status: "不可变审计未启用",
	      use: "当前页面可以帮助运营理解发生过什么、该去哪核对、这条记录还不能证明什么。",
	      boundary: "签名链、WORM 存储、审批单关联、保留策略和日终对账仍需后续专题。",
	      tone: "future",
	    },
  ];
  const toneClass = (tone: "active" | "limited" | "future") =>
    tone === "active"
      ? "bg-emerald-400/12 text-emerald-100"
      : tone === "limited"
        ? "bg-amber-400/12 text-amber-100"
        : "bg-white/8 text-slate-300";

  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">审计证据口径</h3>
	        <p className="mt-1 text-sm text-slate-500">操作记录用于运营回看和故障定位；证据口径和审批缺口列只提示核对路径、证据包和缺口，真正的不可变审计、审批单关联和日终对账仍是后续专题。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">证据层</th>
              <th className="px-3 py-2 font-normal">当前口径</th>
              <th className="px-3 py-2 font-normal">可用于</th>
              <th className="px-3 py-2 font-normal">不能替代</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.layer} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.layer}</td>
                <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.status}</span></td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.use}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AuditArchiveGapIndex({
  systemStatus,
  operationCount,
  failedCount,
  filters,
}: {
  systemStatus?: SystemStatus;
  operationCount: number;
  failedCount: number;
  filters: AdminOperationAuditFilters;
}) {
  const retention = systemStatus?.history_retention;
  const deleted = retention?.last_result?.deleted;
  const deletedTotal = Object.values(deleted ?? {}).reduce((sum, value) => sum + (typeof value === "number" ? value : 0), 0);
  const scopeParts = [
    filters.domain !== "all" ? operationDomainLabel(filters.domain) : "全部业务域",
    filters.status !== "all" ? filters.status : "全部状态",
    filters.operationType !== "all" ? operationTypeLabel(filters.operationType) : "全部操作",
    filters.targetSymbol ? normalizeMarketSymbolInput(filters.targetSymbol) : "全部市场",
  ];
  const retentionStatus = retention
    ? `${retention.auto_enabled ? "自动开启" : retention.enabled ? "手动可用" : "未启用"} · ${retention.sqlite_only ? "SQLite" : "非 SQLite"}`
    : "未加载";
  const dataScale = systemStatus
    ? `订单 ${retentionValue(systemStatus.counts.orders)} · 成交 ${retentionValue(systemStatus.counts.trades)} · 现货流水 ${retentionValue(systemStatus.counts.ledger_entries)}`
    : "未加载";
  const rows: Array<{
    source: string;
    current: string;
    proves: string;
    missing: string;
    action: string;
    tone: "ready" | "limited" | "gap";
  }> = [
    {
      source: "管理员操作记录",
      current: `当前筛选 ${operationCount} 条 · ${scopeParts.join(" / ")}`,
      proves: "可证明已接入维护动作的执行时间、执行人、目标、状态和精简结果。",
      missing: "不能覆盖所有订单、成交、机器人行为或后台自动任务，也不是签名不可改记录。",
      action: "继续回业务 source-of-truth 核对订单、流水、仓位、资金费或实例状态。",
      tone: "ready",
    },
    {
      source: "前端观察失败",
      current: `当前筛选 ${failedCount} 条`,
      proves: "可证明后台页面观察到某次高风险/低频接口失败，并保留恢复定位。",
      missing: "不能证明业务已经回滚、没有部分写入，也不会自动补偿或重试。",
      action: "先打开失败操作目标页，再按业务账本或运行态确认真实状态。",
      tone: failedCount > 0 ? "limited" : "ready",
    },
    {
      source: "当前结果 CSV",
      current: `当前已加载 ${operationCount} 条`,
      proves: "可交接当前筛选范围、证据口径、审批缺口和核对路径。",
      missing: "不是全量审计检索，不扩大查询范围，也不是日终对账包或审批附件。",
      action: "需要全量审计时应设计服务端分页、归档存储和导出任务。",
      tone: "limited",
    },
    {
      source: "历史保留",
      current: `${retentionStatus} · 最近裁剪 ${retentionValue(deletedTotal)}`,
      proves: "可说明本机 SQLite 沙盒历史裁剪配置、最近裁剪量和运行错误。",
      missing: "历史保留会控制本地体量，不等于 WORM 归档、长期留存或可追溯审计链。",
      action: "生产化前需要外部归档、保留策略、恢复演练和裁剪前证据包。",
      tone: retention?.last_error ? "gap" : "limited",
    },
    {
      source: "业务数据源",
      current: dataScale,
      proves: "订单、成交、现货流水、合约流水、仓位和保证金仍是业务核对源。",
      missing: "页面操作记录不能替代账本一致性校验、清算报告或跨表日终平衡。",
      action: "关键维护后按业务域抽样核对，并在后续专题补日终对账包。",
      tone: "limited",
    },
    {
      source: "生产审计归档",
      current: "未启用",
      proves: "当前只明确缺口，不声称已具备生产合规审计。",
      missing: "缺签名链、WORM/对象锁、审批单关联、双人复核、导出归档和日终报告。",
      action: "作为后续独立专题处理，不在本页顺手实现权限或归档系统。",
      tone: "gap",
    },
  ];
  const toneClass = (tone: "ready" | "limited" | "gap") =>
    tone === "ready"
      ? "bg-emerald-400/12 text-emerald-100"
      : tone === "limited"
        ? "bg-amber-400/12 text-amber-100"
        : "bg-rose-500/14 text-rose-100";

  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">审计归档缺口索引</h3>
        <p className="mt-1 text-sm text-slate-500">把操作记录、失败事实、当前 CSV、历史保留和业务账本分开看，避免把本机回看误读成生产审计归档。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1240px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">证据来源</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">能证明</th>
              <th className="px-3 py-2 font-normal">缺口</th>
              <th className="px-3 py-2 font-normal">后续核对</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.source} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.source}</td>
                <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.current}</span></td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.proves}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.missing}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SystemIssueTable({ items, onOpen }: { items: SystemIssueItem[]; onOpen: (item: SystemIssueItem) => void }) {
  return (
    <div className="overflow-auto">
      <table className="min-w-[900px] text-left text-sm">
        <thead className="text-slate-500">
          <tr>
            <th className="pb-2 pr-3">级别</th>
            <th className="pb-2 pr-3">域</th>
            <th className="pb-2 pr-3">事项</th>
            <th className="pb-2 pr-3">详情</th>
            <th className="pb-2 pr-3">入口</th>
          </tr>
        </thead>
        <tbody className="text-slate-200">
          {items.map((item) => (
            <tr key={item.key} className="border-t border-white/8">
              <td className="py-2 pr-3"><span className={`rounded-full px-2 py-0.5 text-xs ${systemIssueClass(item.severity)}`}>{systemIssueLabel(item.severity)}</span></td>
              <td className="py-2 pr-3 text-slate-300">{item.domain}</td>
              <td className="py-2 pr-3 font-medium text-slate-100">{item.title}</td>
              <td className="max-w-[420px] py-2 pr-3 text-xs leading-5 text-slate-400">{item.detail}</td>
              <td className="py-2 pr-3">
                <button type="button" onClick={() => onOpen(item)} className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12">
                  {item.action}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SystemConsistencyPanel({ status }: { status?: SystemStatus }) {
  if (!status) {
    return <section className="panel rounded-3xl p-4 text-sm text-slate-400">加载一致性状态...</section>;
  }
  const invariantItems = status.orderbook_invariants ?? [];
  const wsMetrics = status.websocket.metrics ?? {};
  const splitCount = invariantItems.reduce((sum, item) => sum + (item.engine_only_count ?? 0) + (item.db_only_count ?? 0), 0);
  const versionAlertCount = invariantItems.reduce(
    (sum, item) => sum + (item.orderbook?.same_seq_diff_count ?? 0) + (item.orderbook?.seq_rollback_count ?? 0),
    0,
  );
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg">一致性与连接</h2>
          <p className="mt-1 text-sm text-slate-400">盘口内存簿、数据库 live orders、盘口版本和 WebSocket 发送指标集中在这里。</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs ${splitCount > 0 ? "bg-rose-500/16 text-rose-100" : versionAlertCount > 0 ? "bg-amber-400/16 text-amber-100" : "bg-emerald-400/15 text-emerald-100"}`}>
          {splitCount > 0 ? "需处理" : versionAlertCount > 0 ? "需关注" : "正常"}
        </span>
      </div>
      <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
        <SurveillanceMetric label="簿/DB差异" value={String(splitCount)} />
        <SurveillanceMetric label="版本异常" value={String(versionAlertCount)} />
        <SurveillanceMetric label="Public WS" value={String(status.websocket.public_connections)} />
        <SurveillanceMetric label="Private WS" value={String(status.websocket.private_connections)} />
        <SurveillanceMetric label="发送失败" value={`${wsMetrics.send_failures ?? 0} / ${wsMetrics.send_timeouts ?? 0}`} hint="failure / timeout" />
        <SurveillanceMetric label="丢弃连接" value={String(wsMetrics.dropped_sockets ?? 0)} />
      </div>
      <div className="mb-4 border-t border-white/8 pt-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-display text-base text-slate-100">低频维护边界：重建订单簿</h3>
              <span className="rounded-full bg-rose-500/14 px-2 py-0.5 text-xs text-rose-100">API-only</span>
              <span className="rounded-full bg-amber-400/12 px-2 py-0.5 text-xs text-amber-100">confirm_execute</span>
            </div>
            <p className="mt-1 max-w-4xl text-sm leading-6 text-slate-500">
              重建内存订单簿会从数据库 live GTC 限价单恢复目标市场盘口，适合处理 Engine / DB 差异或重启恢复后的核对。当前后台不提供执行按钮；如通过维护 API 执行，必须保留确认、操作记录和执行前后差异快照。
            </p>
          </div>
          <div className="text-xs leading-5 text-slate-500 xl:max-w-[360px]">
            执行后回本页核对 Engine / DB、差异样本、same seq diff 和 seq rollback；普通查看、WS 指标和盘口摘要仍保持只读。
          </div>
        </div>
      </div>
      <div className="overflow-auto">
        <table className="min-w-[1120px] text-left text-sm">
          <thead className="text-slate-500">
            <tr>
              <th className="pb-2 pr-3">市场</th>
              <th className="pb-2 pr-3">产品</th>
              <th className="pb-2 pr-3">Engine / DB</th>
              <th className="pb-2 pr-3">差异</th>
              <th className="pb-2 pr-3">same seq diff</th>
              <th className="pb-2 pr-3">seq rollback</th>
              <th className="pb-2 pr-3">队列 / in-flight</th>
              <th className="pb-2 pr-3">最近状态</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-200">
            {invariantItems.map((item) => (
              <tr key={item.symbol} className="border-t border-white/8">
                <td className="py-2 pr-3 font-sans text-slate-100">{item.symbol}</td>
                <td className="py-2 pr-3">{item.product_type ?? "-"}</td>
                <td className="py-2 pr-3">{item.engine_open_order_count ?? "-"} / {item.db_live_open_order_count ?? "-"}</td>
                <td className={`py-2 pr-3 ${(item.engine_only_count ?? 0) + (item.db_only_count ?? 0) > 0 ? "text-rose-200" : ""}`}>{item.engine_only_count ?? 0} / {item.db_only_count ?? 0}</td>
                <td className={`py-2 pr-3 ${(item.orderbook?.same_seq_diff_count ?? 0) > 0 ? "text-amber-200" : ""}`}>{item.orderbook?.same_seq_diff_count ?? 0}</td>
                <td className={`py-2 pr-3 ${(item.orderbook?.seq_rollback_count ?? 0) > 0 ? "text-amber-200" : ""}`}>{item.orderbook?.seq_rollback_count ?? 0}</td>
                <td className="py-2 pr-3">{item.flow?.action_queue_size ?? "-"} / {item.flow?.in_flight ?? "-"}</td>
                <td className="py-2 pr-3">{item.flow?.last_status ?? "-"}</td>
              </tr>
            ))}
            {invariantItems.length === 0 && (
              <tr><td className="py-8 text-center text-sm text-slate-500" colSpan={8}>暂无一致性样本。</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function retentionValue(value?: number) {
  return typeof value === "number" ? value.toLocaleString("en-US") : "-";
}

function robotTradeSourceSummary(source?: RobotTradeSourceSummary) {
  if (!source) return "-";
  return `${source.source} · ${retentionValue(source.trade_count)} 笔 · ${fmt(source.quote_amount ?? "0", 2)} quote`;
}

function HistoryRetentionBoundaryPanel({
  retention,
  summary,
}: {
  retention?: SystemStatus["history_retention"];
  summary?: DataRetentionDomainSummary;
}) {
  const config = retention?.config ?? retention?.last_result?.config;
  const deleted = retention?.last_result?.deleted;
  const customerRecordsPreserved = config?.customer_records_preserved !== false;
  const lastRunLabel = retention?.last_result
    ? retention.last_result.dry_run ? "最近 dry-run" : "最近真实裁剪"
    : "暂无运行结果";
  const rows: Array<{
    object: string;
    keep: string;
    candidate: string;
    lastDeleted: string;
    source: string;
    boundary: string;
  }> = [
    {
      object: "非 live 订单（非客户候选）",
      keep: `每市场 ${retentionValue(config?.order_keep_per_market)}${customerRecordsPreserved ? " · 客户保护" : ""}`,
      candidate: `可裁 ${retentionValue(summary?.orders.prune_candidate)} / 旧单 ${retentionValue(summary?.orders.retention_eligible)}`,
      lastDeleted: retentionValue(deleted?.orders),
      source: "订单审计、盘口恢复和历史回看",
      boundary: "普通客户订单和 live open orders 不裁剪；机器人/系统旧订单才进入沙盒体量控制候选。",
    },
    {
      object: "成交（非客户候选）",
      keep: `每市场 ${retentionValue(config?.trade_keep_per_market)}${customerRecordsPreserved ? " · 客户保护" : ""}`,
      candidate: `可裁 ${retentionValue(summary?.trades.prune_candidate)} / 总成交 ${retentionValue(summary?.trades.total)}`,
      lastDeleted: retentionValue(deleted?.trades),
      source: "成交审计、费用/PnL 抽样和 K 线来源核对",
      boundary: "任一侧涉及普通客户的成交不裁剪；robot-only 和系统控制/未知来源成交才是短保留候选。",
    },
    {
      object: "K 线",
      keep: `每市场/周期 ${retentionValue(config?.kline_keep_per_market_interval)}`,
      candidate: "展示历史尾部",
      lastDeleted: retentionValue(deleted?.klines),
      source: "行情图表和历史形态观察",
      boundary: "只保留本地展示历史；不负责外部行情归档或重算证明。",
    },
    {
      object: "现货流水（非客户候选）",
      keep: `每用户 ${retentionValue(config?.ledger_keep_per_user)}${customerRecordsPreserved ? " · 客户保护" : ""}`,
      candidate: "非客户主体尾部",
      lastDeleted: retentionValue(deleted?.ledger_entries),
      source: "现货余额核对和调账回看",
      boundary: "普通客户现货流水不裁剪；机器人/系统流水可用于沙盒短保留和对账抽样。",
    },
    {
      object: "合约流水（非客户候选）",
      keep: `每用户 ${retentionValue(config?.contract_ledger_keep_per_user)}${customerRecordsPreserved ? " · 客户保护" : ""}`,
      candidate: "非客户主体尾部",
      lastDeleted: retentionValue(deleted?.contract_ledger_entries),
      source: "保证金、资金费、强平、ADL 和保险基金核对",
      boundary: "普通客户合约保证金流水不裁剪；机器人/系统清算流水仍不等于生产永久账本。",
    },
  ];
  return (
    <div className="mt-4">
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-display text-base text-slate-100">历史保留与归档边界</h3>
          <span className={`rounded-full px-2 py-0.5 text-xs ${retention?.enabled ? "bg-emerald-400/12 text-emerald-100" : "bg-white/8 text-slate-300"}`}>{retention?.enabled ? "enabled" : "disabled"}</span>
          <span className="rounded-full bg-white/8 px-2 py-0.5 text-xs text-slate-300">{lastRunLabel}</span>
        </div>
        <p className="mt-1 text-sm text-slate-500">历史保留用于控制本地沙盒数据库体量；当前默认保护普通客户订单、成交和资金流水，真实裁剪只面向非客户候选且必须显式确认。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1280px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">对象</th>
              <th className="px-3 py-2 font-normal">当前保留</th>
              <th className="px-3 py-2 font-normal">当前候选</th>
              <th className="px-3 py-2 font-normal">最近裁剪</th>
              <th className="px-3 py-2 font-normal">主要用途</th>
              <th className="px-3 py-2 font-normal">边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.object} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.object}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.keep}</td>
                <td className="px-3 py-2 font-mono text-cyan-100">{row.candidate}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.lastDeleted}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.source}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SystemDataRetentionGovernancePanel({
  status,
  operationCount,
  failedOperationCount,
}: {
  status: SystemStatus;
  operationCount: number;
  failedOperationCount: number;
}) {
  const retention = status.history_retention;
  const config = retention?.config ?? retention?.last_result?.config;
  const deleted = retention?.last_result?.deleted;
  const domains = status.data_retention_domains;
  const rows: Array<{
    domain: string;
    tone: "customer" | "robot" | "system" | "market" | "clearing";
    current: string;
    retention: string;
    verification: string;
    boundary: string;
  }> = [
    {
      domain: "客户业务记录",
      tone: "customer",
      current: `旧订单 ${retentionValue(domains?.orders.customer_retention_eligible)} · 客户相关成交 ${retentionValue(domains?.trades.customer_involved)} · 现货流水 ${retentionValue(status.counts.ledger_entries)}`,
      retention: "普通外部客户订单、成交、费用、现货流水、合约保证金流水和资金事实按业务事实完整核对。",
      verification: "账户与资金 / 订单与成交 / 合约清算",
      boundary: "客户记录不能被机器人短保留或聚合策略影响；本页不清理、不归档、不改账本。",
    },
    {
      domain: "机器人对账记录",
      tone: "robot",
      current: `旧订单候选 ${retentionValue(domains?.orders.robot_retention_eligible)} · robot-only 成交 ${retentionValue(domains?.trades.robot_only)}`,
      retention: "MM/FLOW 高频明细后续更适合聚合、抽样、短保留或汇总表；保留必要对账证据和资金快照。",
      verification: "机器人运营 / 机器人账号 / 订单与成交",
      boundary: "机器人仍是特殊 UID，但机器人活跃度不等同客户活跃度；本轮不改 real_ioc_sandbox 成交落库。",
    },
    {
      domain: "合约清算事实",
      tone: "clearing",
      current: `非客户合约流水保留每用户 ${retentionValue(config?.contract_ledger_keep_per_user)} · 最近裁剪 ${retentionValue(deleted?.contract_ledger_entries)}`,
      retention: "保证金、仓位、资金费、强平、ADL、保险基金和风险阶梯按清算事实核对，不能混入机器人策略日志。",
      verification: "合约清算 / 风险监控",
      boundary: "普通客户合约流水默认受保护；不合并现货钱包和合约保证金，历史保留也不是生产清算永久账本或日终对账包。",
    },
    {
      domain: "系统控制证据",
      tone: "system",
      current: `操作记录 ${retentionValue(operationCount)} · 失败 ${retentionValue(failedOperationCount)} · 历史保留 ${retention?.enabled ? "enabled" : "disabled"}`,
      retention: "后台危险确认、失败事实、dry-run/真实裁剪结果、部署健康和一致性指标作为运维证据保留。",
      verification: "系统与审计 / 操作记录",
      boundary: "操作记录不能替代订单、成交、流水、仓位或资金费 source-of-truth，也不是不可变审计账本。",
    },
    {
      domain: "市场公共数据",
      tone: "market",
      current: `盘口市场 ${retentionValue(status.counts.book_markets)} · K线每市场/周期 ${retentionValue(config?.kline_keep_per_market_interval)}`,
      retention: "K 线、公开成交、订单簿、ticker 和 source 分布服务交易体验、流动性观察和市场状态解释。",
      verification: "市场运营 / 交易页 / 流动性观察",
      boundary: "公开行情不替代客户账本或清算报表；后台长表不能拖慢交易页、撮合和机器人运行。",
    },
  ];
  const toneClass = (tone: "customer" | "robot" | "system" | "market" | "clearing") => {
    if (tone === "clearing") return "bg-violet-400/14 text-violet-100";
    return dataDomainToneClass(tone);
  };

  return (
    <div className="mt-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">数据留存治理口径</h3>
        <p className="mt-1 text-sm text-slate-500">按后台运营视角区分客户完整记录、机器人对账材料、清算事实、系统证据和市场公共数据；这里只读说明，不触发裁剪或归档。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1240px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">数据类别</th>
              <th className="px-3 py-2 font-normal">当前摘要</th>
              <th className="px-3 py-2 font-normal">保留原则</th>
              <th className="px-3 py-2 font-normal">核对入口</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.domain} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.domain}</span>
                </td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.current}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.retention}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{row.verification}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RobotRetentionCandidatePanel({
  summary,
  onOpenOrderAudit,
}: {
  summary?: DataRetentionDomainSummary;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
}) {
  const orders = summary?.orders;
  const trades = summary?.trades;
  const summaryLoaded = Boolean(summary);
  const marketRows = (summary?.markets ?? []).slice(0, 8);
  const sourceRows = (
    summary?.robot_trade_sources?.length
      ? summary.robot_trade_sources
      : (summary?.markets ?? []).flatMap((market) => market.robot_trade_sources ?? [])
  ).slice(0, 6);
  const orderPruneCandidate = typeof orders?.prune_candidate === "number"
    ? orders.prune_candidate
    : typeof orders?.robot_retention_eligible === "number" || typeof orders?.system_retention_eligible === "number"
      ? (orders?.robot_retention_eligible ?? 0) + (orders?.system_retention_eligible ?? 0)
      : undefined;
  const tradePruneCandidate = typeof trades?.prune_candidate === "number"
    ? trades.prune_candidate
    : typeof trades?.robot_only === "number" || typeof trades?.system_or_unknown === "number"
      ? (trades?.robot_only ?? 0) + (trades?.system_or_unknown ?? 0)
      : undefined;
  const sourceAuditTarget = (source: RobotTradeSourceSummary): OrderAuditTarget => ({
    product: auditProductFromProductType(source.product_type),
    symbol: source.symbol ?? null,
    dataScope: "robot",
    status: "all",
  });
  const rows: Array<{
    bucket: string;
    current: string;
    policy: string;
    nextStep: string;
    boundary: string;
    tone: "customer" | "robot" | "system" | "market";
    auditTarget: OrderAuditTarget;
  }> = [
    {
      bucket: "客户保护记录",
      current: `旧订单 ${retentionValue(orders?.customer_retention_eligible)} · 客户相关成交 ${retentionValue(trades?.customer_involved)}`,
      policy: "任一侧涉及普通外部客户的成交和旧订单应按客户业务事实完整核对。",
      nextStep: "后续做归档时先保护客户订单、成交、费用、流水、仓位和资金费证据链。",
      boundary: "不能被机器人短保留、抽样或聚合策略覆盖。",
      tone: "customer",
      auditTarget: { dataScope: "customer", status: "all" },
    },
    {
      bucket: "机器人对账候选",
      current: `旧订单 ${retentionValue(orders?.robot_retention_eligible)} · robot-only 成交 ${retentionValue(trades?.robot_only)}`,
      policy: "MM/FLOW/机器人 UID 之间的高频明细是后续聚合、抽样、短保留或汇总表候选。",
      nextStep: "先设计按市场、source、时间桶、成交量和费用的汇总表，再评估是否减少明细长期保留。",
      boundary: "当前仍不删除、不改落库、不影响真实撮合、账本、K 线和费用计算。",
      tone: "robot",
      auditTarget: { dataScope: "robot", status: "all" },
    },
    {
      bucket: "系统控制 / 未知来源",
      current: `旧订单 ${retentionValue(orders?.system_retention_eligible)} · 系统控制/未知成交 ${retentionValue(trades?.system_or_unknown)}`,
      policy: "系统流动性、contract_liq、seed/fallback 或未知来源需要先作为控制证据核对。",
      nextStep: "后续应拆清系统流动性来源，再决定归档、抽样或排除客户活跃度统计。",
      boundary: "系统记录不能误读为客户活跃，也不能直接当机器人策略表现。",
      tone: "system",
      auditTarget: { dataScope: "system", status: "all" },
    },
    {
      bucket: "当前执行边界",
      current: `可裁旧订单 ${retentionValue(orderPruneCandidate)} / 全部旧订单 ${retentionValue(orders?.retention_eligible)} · 可裁成交 ${retentionValue(tradePruneCandidate)} / 总成交 ${retentionValue(trades?.total)}`,
      policy: "真实裁剪候选只包含机器人和系统控制/未知来源；客户业务事实从执行候选里排除。",
      nextStep: "真正裁剪仍只能走历史保留 dry-run / confirm_execute，并需要另做机器人汇总表专题。",
      boundary: summary?.policy?.source_of_truth_unchanged ? "source-of-truth 未改变" : "未加载 source-of-truth 标记",
      tone: "market",
      auditTarget: { status: "all" },
    },
  ];

  return (
    <div className="mt-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">机器人留存候选统计</h3>
        <p className="mt-1 text-sm text-slate-500">按当前数据库只读聚合旧订单和成交，先识别客户保护记录、机器人对账候选和系统控制/未知来源；这里不触发清理、归档、重算或写入。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1360px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">数据桶</th>
              <th className="px-3 py-2 font-normal">当前数量</th>
              <th className="px-3 py-2 font-normal">留存策略方向</th>
              <th className="px-3 py-2 font-normal">后续动作</th>
              <th className="px-3 py-2 font-normal">本阶段边界</th>
              <th className="px-3 py-2 text-right font-normal">审计入口</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.bucket} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <span className={`inline-flex rounded-full px-2 py-0.5 ${dataDomainToneClass(row.tone)}`}>{row.bucket}</span>
                </td>
                <td className="px-3 py-2 font-mono text-slate-200">{row.current}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.policy}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{row.nextStep}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => onOpenOrderAudit(row.auditTarget)}
                    className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                  >
                    去审计
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3">
        <div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
          <h4 className="text-sm font-medium text-slate-200">按市场分布</h4>
          <p className="text-xs text-slate-500">用于定位机器人对账候选压力最高的市场；这里只读观察，不执行裁剪或归档。</p>
        </div>
        <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1360px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">市场</th>
              <th className="px-3 py-2 font-normal">产品</th>
              <th className="px-3 py-2 font-normal">机器人压力</th>
              <th className="px-3 py-2 font-normal">主要来源</th>
              <th className="px-3 py-2 font-normal">客户相关</th>
              <th className="px-3 py-2 font-normal">机器人对账</th>
              <th className="px-3 py-2 font-normal">系统控制 / 未知</th>
              <th className="px-3 py-2 font-normal">边界</th>
              <th className="px-3 py-2 text-right font-normal">审计</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {marketRows.map((market) => (
              <tr key={market.symbol} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{market.symbol}</td>
                <td className="px-3 py-2 font-mono text-slate-300">{market.product_type ?? "-"}</td>
                <td className="px-3 py-2 font-mono text-slate-200">{retentionValue(market.pressure_score)}</td>
                <td className="px-3 py-2 font-mono text-slate-300">{robotTradeSourceSummary(market.robot_trade_sources?.[0])}</td>
                <td className="px-3 py-2 font-mono text-emerald-100">旧单 {retentionValue(market.orders.customer_retention_eligible)} · 成交 {retentionValue(market.trades.customer_involved)}</td>
                <td className="px-3 py-2 font-mono text-cyan-100">旧单 {retentionValue(market.orders.robot_retention_eligible)} · 成交 {retentionValue(market.trades.robot_only)}</td>
                <td className="px-3 py-2 font-mono text-amber-100">旧单 {retentionValue(market.orders.system_retention_eligible)} · 成交 {retentionValue(market.trades.system_or_unknown)}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">按市场定位候选压力；不裁剪、不归档、不重算。</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => onOpenOrderAudit({
                      product: auditProductFromProductType(market.product_type),
                      symbol: market.symbol,
                      dataScope: "robot",
                      status: "all",
                    })}
                    className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                  >
                    去审计
                  </button>
                </td>
              </tr>
            ))}
            {marketRows.length === 0 && (
              <tr>
                <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={9}>
                  {summaryLoaded ? "暂无市场分布统计。" : "系统状态聚合加载中，市场分布稍后显示。"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </div>
      <div className="mt-4">
        <div className="mb-2 flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
          <h4 className="text-sm font-medium text-slate-200">source 压力明细</h4>
          <p className="text-xs text-slate-500">快速判断机器人高频明细主要来自 FLOW、MM、seed、合约清算还是未知来源；这里只读定位，不做 source 级入账。</p>
        </div>
        <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
          <table className="min-w-[1180px] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="px-3 py-2 font-normal">source</th>
                <th className="px-3 py-2 font-normal">市场</th>
                <th className="px-3 py-2 font-normal">成交笔数</th>
                <th className="px-3 py-2 font-normal">quote 成交额</th>
                <th className="px-3 py-2 font-normal">手续费</th>
                <th className="px-3 py-2 font-normal">对账去向</th>
                <th className="px-3 py-2 font-normal">边界</th>
                <th className="px-3 py-2 text-right font-normal">审计入口</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {sourceRows.map((source) => (
                <tr key={`${source.symbol ?? "all"}-${source.product_type ?? "all"}-${source.source}`} className="border-t border-white/8 align-top">
                  <td className="px-3 py-2 font-mono text-cyan-100">{source.source || "unknown"}</td>
                  <td className="px-3 py-2">
                    <div className="font-mono text-slate-100">{source.symbol ?? "全部市场"}</div>
                    <div className="text-[11px] text-slate-500">{source.product_type ?? "-"}</div>
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-200">{retentionValue(source.trade_count)}</td>
                  <td className="px-3 py-2 font-mono text-slate-200">{fmt(source.quote_amount ?? "0", 2)}</td>
                  <td className="px-3 py-2 font-mono text-slate-300">maker {fmt(source.maker_fee ?? "0", 6)} · taker {fmt(source.taker_fee ?? "0", 6)}</td>
                  <td className="px-3 py-2 leading-5 text-slate-400">订单与成交 data_domain=robot / 机器人运营日志 / 系统运行资源</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">source 只用于定位压力来源；明细仍回订单、成交、流水和机器人日志核对。</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => onOpenOrderAudit(sourceAuditTarget(source))}
                      className="rounded-lg bg-cyan-400/14 px-2.5 py-1.5 text-xs text-cyan-100 transition hover:bg-cyan-400/20"
                    >
                      看 source 审计
                    </button>
                  </td>
                </tr>
              ))}
              {sourceRows.length === 0 && (
                <tr>
                  <td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={8}>
                    {summaryLoaded ? "暂无 robot-only source 分布。" : "系统状态聚合加载中，source 压力明细稍后显示。"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {(summary?.markets?.length ?? 0) > marketRows.length && (
        <p className="mt-2 text-xs text-slate-500">仅显示机器人对账候选压力最高的前 {marketRows.length} 个市场；完整口径在 `/api/v1/admin/system-status` 的 data_retention_domains.markets 中。</p>
      )}
    </div>
  );
}

function SystemRuntimePanel({
  status,
  operationCount,
  failedOperationCount,
  onOpenOrderAudit,
}: {
  status?: SystemStatus;
  operationCount: number;
  failedOperationCount: number;
  onOpenOrderAudit: (target: OrderAuditTarget) => void;
}) {
  const [runtimeView, setRuntimeView] = useState<SystemRuntimeView>("summary");
  if (!status) {
    return <section className="panel rounded-3xl p-4 text-sm text-slate-400">加载运行资源...</section>;
  }
  const retention = status.history_retention;
  const dataDomains = status.data_retention_domains;
  const robotSourceCount = dataDomains?.robot_trade_sources?.length
    ?? (dataDomains?.markets ?? []).reduce((sum, market) => sum + (market.robot_trade_sources?.length ?? 0), 0);
  const pressureMarketCount = (dataDomains?.markets ?? []).filter((market) => market.pressure_score > 0).length;
  const runtimeViewOptions: Array<{
    key: SystemRuntimeView;
    label: string;
    hint: string;
    badge: string;
    tone?: "warn" | "neutral";
  }> = [
    { key: "summary", label: "资源摘要", hint: "进程 / DB / 数据规模", badge: status.status, tone: status.status === "ok" ? "neutral" : "warn" },
    { key: "retention", label: "留存治理", hint: "保留阈值 / 数据域", badge: retention?.enabled ? "enabled" : "disabled", tone: retention?.last_error ? "warn" : "neutral" },
    { key: "robot", label: "机器人候选", hint: "客户保护 / source 压力", badge: robotSourceCount ? `${robotSourceCount} source` : `${pressureMarketCount} 市场`, tone: "neutral" },
    { key: "books", label: "盘口数据", hint: "内存挂单 / 深度", badge: `${status.books?.length ?? 0} 市场`, tone: "neutral" },
  ];
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg">运行资源</h2>
          <p className="mt-1 text-sm text-slate-400">单进程运行、数据库体量、历史保留和当前内存盘口摘要。</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs ${status.status === "ok" ? "bg-emerald-400/15 text-emerald-100" : "bg-amber-400/16 text-amber-100"}`}>{status.status}</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-8">
        <SurveillanceMetric label="PID" value={String(status.process.pid)} />
        <SurveillanceMetric label="运行时长" value={formatUptime(status.uptime_seconds)} />
        <SurveillanceMetric label="Python" value={status.process.python} />
        <SurveillanceMetric label="RSS 峰值" value={`${fmt(status.process.rss_mb, 1)} MB`} />
        <SurveillanceMetric label="数据库" value={status.database.mode} />
        <SurveillanceMetric label="DB 大小" value={status.database.size_mb != null ? `${fmt(status.database.size_mb, 1)} MB` : "-"} />
        <SurveillanceMetric label="历史保留" value={retention?.enabled ? "enabled" : "disabled"} />
        <SurveillanceMetric label="保留周期" value={retention ? `${retention.interval_seconds}s` : "-"} />
      </div>
      <div className="mt-4">
        <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-600">运行资源分区</div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {runtimeViewOptions.map((item) => {
            const active = runtimeView === item.key;
            const badgeClass = item.tone === "warn" ? "bg-amber-400/12 text-amber-100" : "bg-white/8 text-slate-300";
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => setRuntimeView(item.key)}
                className={`min-h-[68px] rounded-2xl border px-4 py-3 text-left transition ${
                  active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.label}</span>
                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${badgeClass}`}>{item.badge}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{item.hint}</div>
              </button>
            );
          })}
        </div>
      </div>
      {runtimeView === "summary" && (
        <div className="mt-4 grid gap-3 xl:grid-cols-2">
          <ActivitySection title="数据规模" empty={false}>
            <div className="grid gap-2 sm:grid-cols-2">
              <SurveillanceMetric label="主体 / 活跃" value={`${status.counts.users} / ${status.counts.active_users}`} />
              <SurveillanceMetric label="市场 / 活跃" value={`${status.counts.markets} / ${status.counts.active_markets}`} />
              <SurveillanceMetric label="订单 / 挂单" value={`${status.counts.orders} / ${status.counts.open_orders}`} />
              <SurveillanceMetric label="成交 / 流水" value={`${status.counts.trades} / ${status.counts.ledger_entries}`} />
            </div>
          </ActivitySection>
          <ActivitySection title="历史保留" empty={false}>
            <div className="space-y-2 text-sm text-slate-300">
              <div className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/25 px-3 py-2"><span>auto</span><span className="font-mono">{retention?.auto_enabled ? "enabled" : "disabled"}</span></div>
              <div className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/25 px-3 py-2"><span>sqlite only</span><span className="font-mono">{retention?.sqlite_only ? "true" : "false"}</span></div>
              <div className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/25 px-3 py-2"><span>last error</span><span className="max-w-[360px] truncate font-mono text-amber-100">{retention?.last_error ?? "-"}</span></div>
            </div>
          </ActivitySection>
        </div>
      )}
      {runtimeView === "retention" && (
        <>
          <HistoryRetentionBoundaryPanel retention={retention} summary={status.data_retention_domains} />
          <SystemDataRetentionGovernancePanel status={status} operationCount={operationCount} failedOperationCount={failedOperationCount} />
        </>
      )}
      {runtimeView === "robot" && (
        <RobotRetentionCandidatePanel summary={status.data_retention_domains} onOpenOrderAudit={onOpenOrderAudit} />
      )}
      {runtimeView === "books" && (
        <div className="mt-4">
          <div className="mb-3">
            <h3 className="font-display text-base text-slate-100">内存盘口摘要</h3>
            <p className="mt-1 text-sm text-slate-500">只读查看当前内存订单簿市场、挂单数量和买卖盘层数；不重建、不撤单、不清理历史。</p>
          </div>
          <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
            <table className="min-w-[720px] text-left text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-normal">市场</th>
                  <th className="px-3 py-2 font-normal">内存挂单</th>
                  <th className="px-3 py-2 font-normal">买盘层数</th>
                  <th className="px-3 py-2 font-normal">卖盘层数</th>
                </tr>
              </thead>
              <tbody className="font-mono text-slate-200">
                {(status.books ?? []).map((item) => (
                  <tr key={item.symbol} className="border-t border-white/8">
                    <td className="px-3 py-2 font-sans text-slate-100">{item.symbol}</td>
                    <td className="px-3 py-2">{item.open_orders}</td>
                    <td className="px-3 py-2">{item.bid_levels}</td>
                    <td className="px-3 py-2">{item.ask_levels}</td>
                  </tr>
                ))}
                {(status.books ?? []).length === 0 && (
                  <tr><td className="px-3 py-8 text-center text-sm text-slate-500" colSpan={4}>暂无内存盘口摘要。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function SystemOperationsPanel({
  defaultPerpSymbol,
  activeView,
  onViewChange,
  onOpenTarget,
  onOpenAudit,
}: {
  defaultPerpSymbol?: string;
  activeView: OperationBoundaryView;
  onViewChange: (view: OperationBoundaryView) => void;
  onOpenTarget: (target: AdminBusinessTarget) => void;
  onOpenAudit: (filters: Partial<AdminOperationAuditFilters>) => void;
}) {
  const operations: {
    domain: Exclude<AdminOperationAuditFilters["domain"], "all">;
    operationType: string;
    title: string;
    risk: string;
    preCheck: string;
    postCheck: string;
    recovery: string;
    section: AdminSection;
    targetSymbol?: string;
    targetMarketDetailTab?: MarketDetailTab;
    targetContractTab?: ContractAdminTab;
    targetSystemTab?: SystemAdminTab;
    button: string;
    severity: "danger" | "warn";
	  }[] = [
	    { domain: "account", operationType: "create_user", title: "创建登录主体", risk: "创建登录主体、生成 API Key/Secret，并可写入现货初始余额模板。", preCheck: "确认用户名、角色、初始现货金额、费率模板和是否需要机器人绑定。", postCheck: "刷新账户与资金，核对主体分类、现货余额、凭据存在状态和操作结果。", recovery: "核对登录主体是否已创建、现货流水和初始模板。", section: "accounts", button: "去账户与资金", severity: "warn" },
	    { domain: "account", operationType: "adjust_spot_balance", title: "现货余额调账", risk: "改写指定 UID 现货钱包并生成账变。", preCheck: "确认目标 UID、主体分类、资产、金额正负和备注；核对可用/冻结余额。", postCheck: "刷新账户与资金，核对现货流水、余额变化和操作记录。", recovery: "核对该 UID 现货钱包、现货流水和操作记录。", section: "accounts", button: "去账户与资金", severity: "warn" },
	    { domain: "account", operationType: "update_user_identity", title: "保存主体身份", risk: "修改角色、登录/API 启停或密码会影响访问边界。", preCheck: "确认目标 UID、主体分类、角色、启停状态和密码变更范围。", postCheck: "刷新账户与资金，核对角色、启停状态、登录/API 状态和失败操作记录。", recovery: "核对目标主体身份、登录状态和最近操作记录。", section: "accounts", button: "去账户与资金", severity: "warn" },
	    { domain: "account", operationType: "update_user_fees_all", title: "保存全市场费率", risk: "改写单个 UID 在所有市场的手续费覆盖，影响后续成交费用。", preCheck: "确认目标 UID、账户分类、maker/taker 值和是否为客户 UID 或机器人 UID。", postCheck: "刷新账户与资金，核对 UID fee_profiles、订单与成交后续费用和操作记录。", recovery: "如误设，重新保存正确 UID 费率；历史成交和流水不回算。", section: "accounts", button: "去账户与资金", severity: "warn" },
	    { domain: "account", operationType: "update_user_fees", title: "保存单市场费率", risk: "改写单个 UID 在单个市场的手续费覆盖，优先于市场默认费率。", preCheck: "确认目标 UID、市场、账户分类和 maker/taker 值。", postCheck: "刷新账户与资金，核对该市场 UID 覆盖和后续成交费用。", recovery: "重新保存该 UID + 市场费率；历史成交和流水不回算。", section: "accounts", button: "去账户与资金", severity: "warn" },
	    { domain: "account", operationType: "reset_user_balances", title: "重置单 UID 现货资金", risk: "覆盖测试 UID 的现货资金模板。", preCheck: "确认目标 UID、主体分类和演示窗口，记录重置前余额摘要。", postCheck: "核对余额模板、API Key 状态和最近现货流水。", recovery: "核对该 UID 现货钱包、API Key 和最近流水。", section: "accounts", button: "去账户与资金", severity: "warn" },
    { domain: "account", operationType: "reset_test_users", title: "批量重置测试主体资金", risk: "批量改写 trader/admin 等测试主体现货余额。", preCheck: "确认影响范围、演示窗口和无进行中的重要测试。", postCheck: "抽查 trader/admin 测试主体，并导出账户目录摘要留档。", recovery: "先抽查 trader 测试主体，再看账户目录摘要。", section: "accounts", button: "去账户与资金", severity: "danger" },
    { domain: "account", operationType: "rotate_api_key", title: "轮换 API Key", risk: "旧 key 立即失效，影响外部脚本。", preCheck: "确认调用方可切换新 key，不在页面或文档暴露 secret。", postCheck: "确认新 key 可用、旧 key 失效，外部脚本已更新。", recovery: "核对调用方是否已切换新 key。", section: "accounts", button: "去账户与资金", severity: "warn" },
    { domain: "market", operationType: "reset_spot_market", title: "撤销现货市场挂单", risk: "改变订单状态和当前盘口。", preCheck: "确认目标市场、撤单范围和做市实例状态。", postCheck: "核对订单审计、盘口一致性和机器人是否重新铺盘。", recovery: "核对挂单、盘口一致性和订单状态。", section: "markets", targetMarketDetailTab: "logs", button: "去市场日志", severity: "danger" },
    { domain: "market", operationType: "wipe_spot_market_data", title: "清理行情历史", risk: "删除测试成交、K 线和历史展示数据。", preCheck: "确认仅在演示/测试库执行，已备份或明确不需要历史。", postCheck: "确认市场详情、K 线、订单审计和成交范围符合预期。", recovery: "确认市场详情、K 线和订单审计范围。", section: "markets", targetMarketDetailTab: "logs", button: "去市场日志", severity: "danger" },
    { domain: "market", operationType: "wipe_market_klines", title: "清除历史 K 线", risk: "影响行情图表和历史回看。", preCheck: "确认清除历史 K 线，不清订单、成交、余额或挂单。", postCheck: "刷新 K 线，核对重建状态、来源分布和空态提示。", recovery: "刷新市场 K 线并检查重建结果。", section: "markets", targetMarketDetailTab: "logs", button: "去市场日志", severity: "warn" },
    { domain: "market", operationType: "update_market_fees", title: "保存市场默认费率", risk: "改写市场默认 maker/taker 手续费，影响没有 UID 覆盖的后续成交。", preCheck: "确认目标市场、默认 maker/taker 值和已有 UID 覆盖数量。", postCheck: "核对市场运营费率口径、后续成交费用和操作记录。", recovery: "重新保存正确市场默认费率；已有 UID 覆盖和历史费用不回算。", section: "markets", targetMarketDetailTab: "rules", button: "去交易规则", severity: "warn" },
    { domain: "market", operationType: "update_contract_trading_mode", title: "切换合约交易模式", risk: "normal / reduce_only / paused 会影响合约开平仓能力。", preCheck: "确认目标 PERP、现有仓位/挂单和切换后的运营后果。", postCheck: "核对产品参数、交易页模式、拒单原因和失败操作记录。", recovery: "核对 PERP 市场产品参数、交易页状态和失败操作。", section: "markets", targetSymbol: defaultPerpSymbol, targetMarketDetailTab: "product", button: "去产品配置", severity: "danger" },
    { domain: "contract", operationType: "adjust_contract_account", title: "合约保证金调账", risk: "直接改写指定 UID 的合约保证金钱包和可用保证金；当前主界面不提供执行按钮。", preCheck: "确认 UID(user_id)、主体分类、保证金币种、金额方向、备注、持仓和风险状态。", postCheck: "核对保证金账户、合约流水、仓位风险和 adjust_contract_account 操作记录。", recovery: "核对该 UID 合约保证金账户、合约流水和 adjust_contract_account 操作记录。", section: "contracts", targetContractTab: "accounts", button: "去保证金账户", severity: "danger" },
    { domain: "contract", operationType: "settle_funding", title: "手动结算资金费", risk: "改写合约保证金、资金费事件和结算水位。", preCheck: "确认市场、资金费时间、水位和未重复结算状态。", postCheck: "核对资金费事件、结算水位、任务状态和合约流水。", recovery: "核对资金费任务、结算水位和合约流水。", section: "contracts", targetContractTab: "funding", button: "去资金费", severity: "danger" },
    { domain: "contract", operationType: "retry_failed_funding_jobs", title: "批量重试资金费", risk: "批量重放 failed 资金费任务。", preCheck: "确认 failed 原因已处理，排除 already_settled 或重复结算。", postCheck: "核对任务状态、失败残留、资金费事件和合约流水。", recovery: "核对任务状态、already_settled 和失败原因。", section: "contracts", targetContractTab: "funding", button: "去资金费", severity: "danger" },
    { domain: "contract", operationType: "execute_adl", title: "执行 ADL", risk: "处理坏账并改写仓位、保险基金和清算记录。", preCheck: "确认强平事件、残余坏账、保险基金余额和候选排序。", postCheck: "核对 ADL 历史、仓位变化、保险基金流水和剩余坏账。", recovery: "核对强平事件、ADL 历史和保险基金流水。", section: "contracts", targetContractTab: "liquidation", button: "去强平 / ADL", severity: "danger" },
    { domain: "contract", operationType: "adjust_insurance_fund", title: "保险基金调账", risk: "改写清算基金余额和基金流水。", preCheck: "确认调账原因、金额方向、关联清算事件和备注。", postCheck: "核对基金余额、基金流水、坏账覆盖和关联事件。", recovery: "核对基金余额、坏账覆盖和调账原因。", section: "contracts", targetContractTab: "insurance", button: "去保险基金", severity: "danger" },
    { domain: "contract", operationType: "update_risk_tiers", title: "保存风险阶梯", risk: "影响杠杆上限、维持保证金和强平价。", preCheck: "确认目标市场、阶梯连续性、杠杆上限和维持率后果。", postCheck: "核对风险参数、仓位风险、下单拒单和操作记录。", recovery: "核对风险参数、交易拒单和仓位风险。", section: "contracts", targetContractTab: "risk", button: "去风险参数", severity: "danger" },
    { domain: "contract", operationType: "seed_contract_orderbook", title: "铺合约盘口", risk: "由系统流动性主体写入合约订单簿。", preCheck: "确认目标市场、系统流动性主体保证金和是否撤旧单。", postCheck: "核对合约订单、盘口深度、系统主体保证金和失败记录。", recovery: "核对合约盘口、系统主体和合约订单。", section: "contracts", targetContractTab: "orders", button: "去合约订单", severity: "warn" },
    { domain: "bot", operationType: "create_market_bot", title: "创建机器人账号", risk: "创建或绑定 mm_bot 登录主体、API 凭据、市场角色和初始资金模板。", preCheck: "确认目标市场、UID 绑定、角色、API 来源、SPOT 现货模板或 PERP 保证金模板。", postCheck: "核对机器人账号资源池、账户与资金或合约清算、实例启动预检和失败操作记录。", recovery: "核对机器人账号是否已创建、UID 是否绑定、API 凭据和资金模板是否已写入。", section: "bots", button: "去机器人账号", severity: "warn" },
    { domain: "bot", operationType: "update_market_bot", title: "保存机器人账号", risk: "可能修改机器人登录主体、API 凭据、策略绑定、资金模板和启停状态。", preCheck: "确认变更分区、目标 UID、当前实例是否运行、资金模板是否会影响 SPOT reset 或 PERP 保证金同步。", postCheck: "核对机器人账号资源池、实例预检、现货余额或合约保证金账户。", recovery: "核对机器人账号配置、API 凭据、启停状态和相关资金域。", section: "bots", button: "去机器人账号", severity: "warn" },
	    { domain: "bot", operationType: "create_default_market_bots", title: "补默认机器人账号", risk: "批量补齐默认 maker 账号，可能生成多个 mm_bot、API 凭据和资金模板。", preCheck: "确认当前市场已有绑定数、默认目标数量、参考价和初始资金模板。", postCheck: "核对机器人账号资源池、实例预检和资金域快照。", recovery: "核对默认账号是否已部分创建，避免重复补齐。", section: "bots", button: "去机器人账号", severity: "warn" },
	    { domain: "bot", operationType: "create_default_flow_bot", title: "补 FLOW 机器人账号", risk: "为 SPOT 市场补 FLOW 账号并启用本地成交流角色。", preCheck: "确认该市场没有已绑定 FLOW 账号、FLOW 用途和初始现货模板。", postCheck: "核对机器人账号资源池、FLOW 队列和现货余额快照。", recovery: "核对是否已有 FLOW 账号，避免重复创建或误启用。", section: "bots", button: "去机器人账号", severity: "warn" },
    { domain: "bot", operationType: "flow_control_start", title: "启动 FLOW", risk: "开启币对级 FLOW；本地 IOC 会进入撮合、账本和 K 线，虚拟成交只进入内存 T&S 和展示 K 线，不落库。", preCheck: "确认 FLOW 模式、connector、source policy、队列/in-flight、价格带、允许层数和当前盘口深度。", postCheck: "核对 FLOW 状态、pause reason、成交来源、IOC 成功率、订单与成交审计和操作记录。", recovery: "启动失败时先核对 FLOW 机器人账号、guard、队列积压、价格源和 connector，不要直接放大成交参数。", section: "flow_config", button: "去刷量策略", severity: "warn" },
    { domain: "bot", operationType: "flow_control_pause", title: "暂停 FLOW", risk: "暂停主动成交流；MM 仍可继续铺单，背景成交可能明显下降。", preCheck: "确认暂停原因、当前演示窗口、是否需要保留 MM 盘口和成交样本。", postCheck: "核对 FLOW 状态为暂停、MM 盘口仍在、source 分布不再新增 flow 成交和操作记录。", recovery: "暂停失败时核对 flow_control 已保存状态、runtime bundle 和协调器状态。", section: "flow_config", button: "去刷量策略", severity: "warn" },
	    { domain: "bot", operationType: "update_market_strategy", title: "保存机器人策略", risk: "写入市场策略选择和参数，可能影响正在运行的做市报价、挂单清理和 PERP 策略切换窗口。", preCheck: "确认目标市场、策略版本、关键参数、实例是否运行、SPOT 热生效或 PERP 重启边界。", postCheck: "核对策略 apply status、运行策略、实例日志、盘口深度和操作记录。", recovery: "保存失败时先核对已保存策略、运行策略和最近 apply status，不要直接重启实例。", section: "maker_config", button: "去铺单策略", severity: "warn" },
    { domain: "bot", operationType: "maker_instance_start", title: "启动做市实例", risk: "拉起当前市场做市进程，开始真实挂单并可能产生成交。", preCheck: "确认市场启用、maker 账号、已保存策略、启动预检、价格源和 FLOW guard。", postCheck: "核对 PID identity、heartbeat、运行策略、盘口双边、FLOW 状态和操作记录。", recovery: "启动失败时先看实例日志、凭据、余额、价格源和启动预检，不要连续重复启动。", section: "maker_config", button: "去铺单策略", severity: "warn" },
    { domain: "bot", operationType: "maker_instance_stop", title: "停止做市实例", risk: "停止当前市场做市进程；普通停止默认不撤销已有机器人挂单。", preCheck: "确认是否允许保留当前挂单，记录 PID、heartbeat 和活跃挂单基线。", postCheck: "核对实例状态、heartbeat 停止、剩余挂单、盘口影响和操作记录。", recovery: "停止失败时先核对 PID 和实例状态；需要清理残留挂单时走停止并撤单。", section: "maker_config", button: "去铺单策略", severity: "warn" },
	    { domain: "bot", operationType: "maker_instance_stop_and_cancel", title: "停止并撤单", risk: "停止实例并撤销该策略当前挂单。", preCheck: "确认市场、实例 PID、撤单范围和 FLOW/盘口影响。", postCheck: "核对实例状态、当前挂单、盘口恢复和操作记录。", recovery: "核对实例状态、盘口和操作记录。", section: "maker_config", button: "去铺单策略", severity: "danger" },
	    { domain: "bot", operationType: "maker_instance_restart", title: "重启做市实例", risk: "短时影响盘口维护和 FLOW 状态。", preCheck: "确认策略版本、参数、PID/heartbeat 基线和重启窗口。", postCheck: "核对 PID identity、heartbeat、运行策略、日志和盘口。", recovery: "核对实例心跳、PID 和运行策略。", section: "maker_config", button: "去铺单策略", severity: "warn" },
    { domain: "system", operationType: "orderbook_rebuild", title: "重建内存订单簿", risk: "从数据库 live GTC 限价单重建目标市场内存订单簿，可能改变当前盘口展示和撮合恢复状态；当前主界面不提供执行按钮。", preCheck: "确认目标市场、Engine / DB 差异、活跃挂单范围、演示窗口和重建前快照。", postCheck: "核对一致性页 Engine / DB、差异样本、盘口版本异常和 orderbook_rebuild 操作记录。", recovery: "核对目标市场盘口、live orders、订单簿版本和操作结果 JSON。", section: "system", targetSystemTab: "consistency", button: "去一致性", severity: "danger" },
    { domain: "system", operationType: "history_retention_run", title: "手动历史保留", risk: "按保留配置裁剪旧订单、成交、K 线、现货流水和合约流水历史；不影响 live open orders。", preCheck: "先执行 dry-run，确认将裁剪的对象、数量、数据库备份和演示窗口；真实执行必须显式 confirm_execute。", postCheck: "核对运行资源页 last result、数据规模、业务页抽样和 history_retention_run 操作记录。", recovery: "若误裁剪只能从数据库备份或外部归档恢复；当前不是生产不可变归档。", section: "system", targetSystemTab: "runtime", button: "去运行资源", severity: "danger" },
  ];
  const criticalCount = operations.filter((item) => item.severity === "danger").length;
  const operationEntryPath = (item: (typeof operations)[number]) => [
    item.section,
    item.targetSymbol ? `market=${item.targetSymbol}` : "",
    item.targetMarketDetailTab ? `marketTab=${item.targetMarketDetailTab}` : "",
    item.targetContractTab ? `contractTab=${item.targetContractTab}` : "",
    item.targetSystemTab ? `systemTab=${item.targetSystemTab}` : "",
  ].filter(Boolean).join(" / ");
  const approvalProfileForOperation = (item: (typeof operations)[number]) =>
    adminOperationApprovalProfile({ domain: item.domain, operationType: item.operationType, severity: item.severity });
  const approvalRows = [
    {
      scope: "资金 / 清算改写",
      count: operations.filter((item) => approvalProfileForOperation(item).level === "approval_required").length,
      readiness: "缺正式审批",
      evidence: "操作记录、账户/合约流水、仓位/资金费/强平/保险基金业务快照",
      gap: "审批单、复核人、执行窗口、补偿/回滚单、日终对账包",
    },
    {
      scope: "市场 / 历史 / 实例高风险维护",
      count: operations.filter((item) => approvalProfileForOperation(item).level === "change_window_required").length,
      readiness: "需变更窗口",
      evidence: "操作记录、目标市场状态、盘口/订单/历史范围或实例状态快照",
      gap: "演练单、影响范围确认、复核人、回滚记录",
    },
    {
      scope: "机器人 / FLOW / 策略运行",
      count: operations.filter((item) => approvalProfileForOperation(item).level === "runtime_confirmed").length,
      readiness: "运行窗口确认",
      evidence: "操作记录、PID/heartbeat、运行策略、队列、盘口和成交来源",
      gap: "正式运行窗口、策略变更审批、自动回滚记录",
    },
    {
      scope: "沙盒确认维护",
      count: operations.filter((item) => approvalProfileForOperation(item).level === "sandbox_confirmed").length,
      readiness: "当前可用确认",
      evidence: "前端确认、后端 guard、操作记录、业务页复核",
      gap: "生产级 RBAC、岗位权限、不可变审计归档",
    },
  ];
  const boundaryViewOptions: Array<{
    key: OperationBoundaryView;
    label: string;
    hint: string;
    badge?: string;
    tone?: "neutral" | "warn" | "danger";
  }> = [
    { key: "catalog", label: "危险目录", hint: "低频 / 高风险", badge: String(criticalCount), tone: criticalCount > 0 ? "danger" : "neutral" },
    { key: "governance", label: "后续治理", hint: "控制点 / 路线图" },
    { key: "roles", label: "岗位草案", hint: "后续职责" },
    { key: "approval", label: "审批证据", hint: "后续证据包", badge: String(approvalRows.length), tone: "warn" },
  ];
  const exportOperationBoundary = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(
      `admin_operation_boundaries_${stamp}.csv`,
      [
        "severity",
        "severity_label",
        "domain",
        "domain_label",
        "operation_type",
        "operation_label",
        "title",
        "risk_boundary",
        "pre_execution_check",
        "post_execution_check",
        "recovery_check",
        "approval_readiness",
        "approval_evidence_package",
        "approval_gap",
        "owning_section",
        "business_entry",
        "execution_boundary",
        "frontend_confirm_boundary",
        "backend_guard_boundary",
        "audit_lookup",
        "source",
      ],
      operations.map((item) => {
        const approval = approvalProfileForOperation(item);
        return [
          item.severity,
          item.severity === "danger" ? "高风险" : "需确认",
          item.domain,
          operationDomainLabel(item.domain),
          item.operationType,
          operationTypeLabel(item.operationType),
          item.title,
          item.risk,
          item.preCheck,
          item.postCheck,
          item.recovery,
          approval.label,
          approval.evidencePackage,
          approval.gap,
          item.section,
          operationEntryPath(item),
          "业务域执行；系统与审计只做目录和记录定位",
          "执行前保留前端确认提示",
          "写入类接口继续要求 confirm_execute",
          `系统与审计 -> 操作记录 -> ${operationTypeLabel(item.operationType)}`,
          "current_page_static_boundary_catalog",
        ];
      }),
    );
  };
  return (
    <section className="space-y-4">
	      <section className="panel rounded-3xl p-4">
        <div className="mb-3">
          <h2 className="font-display text-lg">危险操作与后续治理</h2>
          <p className="mt-1 text-sm text-slate-400">默认先看低频危险动作归属；岗位、审批和生产化治理只是后续专题边界，不改变当前测试后台权限或执行流程。</p>
        </div>
	        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
	          {boundaryViewOptions.map((item) => {
	            const active = activeView === item.key;
            const toneClass =
              item.tone === "danger"
                ? "bg-rose-500/14 text-rose-100"
                : item.tone === "warn"
                  ? "bg-amber-400/12 text-amber-100"
                  : "bg-white/8 text-slate-300";
            return (
              <button
	                key={item.key}
	                type="button"
	                onClick={() => onViewChange(item.key)}
                className={`min-h-[68px] rounded-2xl border px-4 py-3 text-left transition ${
                  active ? "border-cyan-300/40 bg-cyan-400/12 text-cyan-50" : "border-white/8 bg-white/5 text-slate-300 hover:bg-white/8"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.label}</span>
                  {item.badge && <span className={`rounded-full px-2 py-0.5 text-[11px] ${toneClass}`}>{item.badge}</span>}
                </div>
                <div className="mt-1 text-xs text-slate-500">{item.hint}</div>
              </button>
            );
          })}
        </div>
      </section>
	      {activeView === "governance" && (
        <>
          <GovernanceBoundaryStrip operationCount={operations.length} criticalCount={criticalCount} />
          <GovernanceRoadmapStrip />
        </>
      )}
	      {activeView === "roles" && <RolePermissionMatrixStrip />}
	      {activeView === "approval" && (
        <section className="panel rounded-3xl p-4">
        <div className="mb-3">
          <h2 className="font-display text-lg">审批准备度与证据包口径</h2>
          <p className="mt-1 text-sm text-slate-400">当前只做审批缺口和证据包口径提示；不会创建审批单、改变执行权限或阻断既有确认流程。</p>
        </div>
        <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
          <table className="min-w-[1180px] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="px-3 py-2 font-normal">范围</th>
                <th className="px-3 py-2 font-normal">动作数</th>
                <th className="px-3 py-2 font-normal">当前准备度</th>
                <th className="px-3 py-2 font-normal">应留证据包</th>
                <th className="px-3 py-2 font-normal">仍缺能力</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {approvalRows.map((row) => (
                <tr key={row.scope} className="border-t border-white/8 align-top">
                  <td className="px-3 py-2 font-medium text-slate-100">{row.scope}</td>
                  <td className="px-3 py-2 font-mono text-slate-200">{row.count}</td>
                  <td className="px-3 py-2"><span className="rounded-full bg-amber-400/12 px-2 py-0.5 text-amber-100">{row.readiness}</span></td>
                  <td className="px-3 py-2 leading-5 text-slate-400">{row.evidence}</td>
                  <td className="px-3 py-2 leading-5 text-slate-500">{row.gap}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      )}
	      {activeView === "catalog" && (
        <section className="panel rounded-3xl p-4">
        <div className="mb-3 flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <h2 className="font-display text-lg">低频维护与危险动作边界</h2>
            <p className="mt-1 text-sm text-slate-400">系统页只做目录和审计定位；清算、调账、清历史、重置和停机动作仍回业务域执行，并继续使用确认提示。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <button
              type="button"
              onClick={exportOperationBoundary}
              className="rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12"
            >
              导出当前边界 CSV
            </button>
            <span className="rounded-full bg-rose-500/14 px-3 py-1 text-rose-100">高风险 {criticalCount}</span>
            <span className="rounded-full bg-amber-400/14 px-3 py-1 text-amber-100">需确认 {operations.length - criticalCount}</span>
          </div>
        </div>
        <div className="overflow-auto">
          <table className="min-w-[1800px] text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2 pr-3">级别</th>
                <th className="pb-2 pr-3">业务域</th>
                <th className="pb-2 pr-3">操作</th>
                <th className="pb-2 pr-3">审计类型</th>
                <th className="pb-2 pr-3">风险边界</th>
                <th className="pb-2 pr-3">执行前核对</th>
                <th className="pb-2 pr-3">执行后 / 失败核对</th>
                <th className="pb-2 pr-3">审批准备度</th>
                <th className="pb-2 pr-3">控制边界</th>
                <th className="pb-2 pr-3">入口</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {operations.map((item) => {
                const severityClass = item.severity === "danger" ? "bg-rose-500/14 text-rose-100" : "bg-amber-400/14 text-amber-100";
                return (
                  <tr key={`${item.domain}-${item.operationType}`} className="border-t border-white/8 align-top">
                    <td className="py-2 pr-3">
                      <span className={`rounded-full px-2 py-1 text-xs ${severityClass}`}>{item.severity === "danger" ? "高风险" : "需确认"}</span>
                    </td>
                    <td className="py-2 pr-3 text-slate-100">{operationDomainLabel(item.domain)}</td>
                    <td className="py-2 pr-3 font-medium text-slate-100">{item.title}</td>
                    <td className="py-2 pr-3 font-mono text-xs text-cyan-100">{item.operationType}</td>
                    <td className="max-w-[260px] py-2 pr-3 text-xs leading-5 text-slate-400">{item.risk}</td>
                    <td className="max-w-[260px] py-2 pr-3 text-xs leading-5 text-slate-300">{item.preCheck}</td>
                    <td className="max-w-[280px] py-2 pr-3 text-xs leading-5 text-slate-300">{item.postCheck}</td>
                    <td className="max-w-[260px] py-2 pr-3 text-xs leading-5">
                      {(() => {
                        const approval = approvalProfileForOperation(item);
                        return (
                          <div className="space-y-1">
                            <span className="inline-flex rounded-full bg-amber-400/12 px-2 py-0.5 text-[11px] text-amber-100">{approval.label}</span>
                            <div className="text-slate-400">{approval.gap}</div>
                          </div>
                        );
                      })()}
                    </td>
                    <td className="max-w-[230px] py-2 pr-3">
                      <div className="flex flex-wrap gap-1.5 text-[11px]">
                        <span className="rounded-full bg-white/8 px-2 py-0.5 text-slate-300">业务域执行</span>
                        <span className="rounded-full bg-amber-400/12 px-2 py-0.5 text-amber-100">前端确认</span>
                        <span className="rounded-full bg-cyan-400/12 px-2 py-0.5 text-cyan-100">后端 guard</span>
                        <span className="rounded-full bg-violet-400/12 px-2 py-0.5 text-violet-100">操作记录</span>
                      </div>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => onOpenTarget({
                            section: item.section,
                            targetSymbol: item.targetSymbol,
                            marketDetailTab: item.targetMarketDetailTab,
                            contractTab: item.targetContractTab,
                            systemTab: item.targetSystemTab,
                          })}
                          className="rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12"
                        >
                          {item.button}
                        </button>
                        <button
                          type="button"
                          onClick={() => onOpenAudit({ domain: item.domain, operationType: item.operationType, limit: "100" })}
                          className="rounded-xl bg-cyan-400/12 px-3 py-1.5 text-xs text-cyan-100 hover:bg-cyan-400/18"
                        >
                          看记录
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
      )}
    </section>
  );
}

function RolePermissionMatrixStrip() {
  const rows: Array<{
    role: string;
    current: string;
    futureScope: string;
    futureWrite: string;
    review: string;
    boundary: string;
    tone: "read" | "operate" | "review" | "restricted";
  }> = [
    {
      role: "市场运营",
      current: "同一管理员入口查看市场、策略、实例和订单审计。",
      futureScope: "市场配置、产品参数、盘口状态、机器人运行和订单/成交只读核对。",
      futureWrite: "上币、交易规则、产品模式、策略保存和实例控制应按市场范围授权。",
      review: "切换 PERP 交易模式、清市场历史、停止并撤单需要变更窗口或复核。",
      boundary: "不应直接执行合约保证金调账、ADL、保险基金调账或系统历史裁剪。",
      tone: "operate",
    },
    {
      role: "账户运营",
      current: "同一管理员入口查看和维护登录主体、现货余额、费率和凭据。",
      futureScope: "主体目录、身份凭据、现货钱包、UID 时间线和机器人绑定索引。",
      futureWrite: "创建登录主体、主体启停、费率维护和现货测试资金维护应按账户范围授权。",
      review: "批量重置、API Key 轮换、现货扣款类动作需要复核和执行后流水核对。",
      boundary: "不合并现货钱包与合约保证金，不在账户页处理清算风险。",
      tone: "operate",
    },
    {
      role: "清算运营",
      current: "同一管理员入口查看合约保证金、仓位、资金费、强平、ADL 和保险基金。",
      futureScope: "合约保证金账本、仓位风险、资金费任务、强平/ADL、保险基金和风险参数。",
      futureWrite: "资金费结算/重试、ADL、保险基金维护和保证金维护应受清算岗位约束。",
      review: "所有改写保证金、清算事件、保险基金或风险阶梯的动作应有 maker-checker。",
      boundary: "不处理登录凭据、现货调账、机器人策略保存或系统历史裁剪。",
      tone: "review",
    },
    {
      role: "风控值班",
      current: "同一管理员入口查看风险监控、仓位风险、价格源和操作失败。",
      futureScope: "风险监控队列、仓位风险、价格源异常、资金费/清算异常和系统一致性。",
      futureWrite: "应以只读分流、升级、冻结建议或模式切换申请为主。",
      review: "可发起 reduce_only / paused 或清算处置建议，但执行应由市场/清算复核。",
      boundary: "不直接调账、不直接执行 ADL、不绕过清算岗位处理坏账。",
      tone: "read",
    },
    {
      role: "系统维护",
      current: "同一管理员入口查看部署检查、运行资源、历史保留和危险动作目录。",
      futureScope: "部署门禁、进程/数据库状态、历史保留、系统一致性和运维告警。",
      futureWrite: "历史裁剪、重建、停机、配置变更和备份恢复应限制在运维窗口。",
      review: "真实裁剪、重建、停机或配置变更需要变更单、备份证明和恢复演练记录。",
      boundary: "不直接改写用户资金、清算账本、机器人策略或市场风控参数。",
      tone: "restricted",
    },
    {
      role: "只读审计",
      current: "同一管理员入口查看操作记录、导出当前筛选结果和业务定位。",
      futureScope: "操作记录、失败事实、证据口径、归档交接、导出包和业务源核对路径。",
      futureWrite: "原则上不执行写入动作，只可读取、筛选、导出和进入业务页只读核对。",
      review: "应能核查审批单、复核人、执行窗口、业务流水和不可变归档是否齐全。",
      boundary: "不能替代业务 source-of-truth，也不能补写审批或修改操作结果。",
      tone: "read",
    },
  ];
  const toneClass = (tone: "read" | "operate" | "review" | "restricted") =>
    tone === "read"
      ? "bg-cyan-400/12 text-cyan-100"
      : tone === "operate"
        ? "bg-emerald-400/12 text-emerald-100"
        : tone === "review"
          ? "bg-amber-400/12 text-amber-100"
          : "bg-rose-500/14 text-rose-100";
  const toneLabel = (tone: "read" | "operate" | "review" | "restricted") =>
    tone === "read" ? "只读优先" : tone === "operate" ? "业务执行" : tone === "review" ? "复核优先" : "运维隔离";
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-3">
        <h2 className="font-display text-lg">岗位权限草案矩阵</h2>
        <p className="mt-1 text-sm text-slate-400">先用只读矩阵定义未来岗位边界；当前不会隐藏按钮、不会改变接口权限，也不会把单管理员模式伪装成生产 RBAC。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1500px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">岗位</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">未来可见范围</th>
              <th className="px-3 py-2 font-normal">未来写入范围</th>
              <th className="px-3 py-2 font-normal">复核 / 审批边界</th>
              <th className="px-3 py-2 font-normal">本阶段隔离边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.role} className="border-t border-white/8 align-top">
                <td className="px-3 py-2">
                  <div className="font-medium text-slate-100">{row.role}</div>
                  <span className={`mt-1 inline-flex rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{toneLabel(row.tone)}</span>
                </td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.current}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.futureScope}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.futureWrite}</td>
                <td className="px-3 py-2 leading-5 text-slate-300">{row.review}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function GovernanceRoadmapStrip() {
  const rows: Array<{
    phase: string;
    readiness: string;
    target: string;
    prerequisite: string;
    boundary: string;
    tone: "ready" | "planned" | "blocked";
  }> = [
    {
      phase: "当前控制点",
      readiness: "已启用",
      target: "前端确认、后端 confirm_execute guard、操作记录、失败事实、证据/审批/归档缺口口径。",
      prerequisite: "继续保持写入动作只回业务域执行，系统页只做目录、核对和审计定位。",
      boundary: "不是生产权限系统，也不是不可变审计。",
      tone: "ready",
    },
    {
      phase: "岗位权限 / RBAC",
      readiness: "后续专题",
      target: "按运营、清算、风控、系统维护、只读审计等岗位拆分可见范围和可执行动作。",
      prerequisite: "先定义用户身份、岗位、资源范围、API key 归属和现货/合约账本边界。",
      boundary: "不在当前前端信息架构阶段顺手限制按钮，避免制造假权限感。",
      tone: "planned",
    },
    {
      phase: "审批流 / maker-checker",
      readiness: "后续专题",
      target: "高风险动作生成审批对象，支持提交、复核、驳回、执行窗口和回滚核对。",
      prerequisite: "需要确定审批单数据模型、状态机、执行幂等、失败恢复和审计关联。",
      boundary: "当前只提示审批准备度和证据包，不阻断既有沙盒确认流程。",
      tone: "planned",
    },
    {
      phase: "不可变审计归档",
      readiness: "后续专题",
      target: "操作记录、审批单、业务快照和导出包进入签名链或对象锁归档。",
      prerequisite: "需要归档存储、签名/哈希、保留策略、恢复演练和导出校验。",
      boundary: "当前 SQLite 历史保留和 CSV 导出不能被解释为 WORM 归档。",
      tone: "blocked",
    },
    {
      phase: "日终对账 / 清算报告",
      readiness: "后续专题",
      target: "订单、成交、现货流水、合约保证金流水、仓位、资金费、保险基金和审计记录形成日终包。",
      prerequisite: "需要先定义账本口径、截点时间、差异处理、坏账/保险基金报告和重放验证。",
      boundary: "不在本专题合并现货钱包与合约保证金，也不改变账本 source-of-truth。",
      tone: "blocked",
    },
  ];
  const toneClass = (tone: "ready" | "planned" | "blocked") =>
    tone === "ready"
      ? "bg-emerald-400/12 text-emerald-100"
      : tone === "planned"
        ? "bg-amber-400/12 text-amber-100"
        : "bg-rose-500/14 text-rose-100";
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-3">
        <h2 className="font-display text-lg">生产化治理路线图</h2>
        <p className="mt-1 text-sm text-slate-400">把权限、审批、不可变审计和日终对账拆成后续专题，避免把只读信息架构优化误认为生产级治理已经完成。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1260px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">阶段</th>
              <th className="px-3 py-2 font-normal">当前准备度</th>
              <th className="px-3 py-2 font-normal">目标能力</th>
              <th className="px-3 py-2 font-normal">前置条件</th>
              <th className="px-3 py-2 font-normal">本阶段边界</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.phase} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.phase}</td>
                <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.readiness}</span></td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.target}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.prerequisite}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function GovernanceBoundaryStrip({
  operationCount,
  criticalCount,
}: {
  operationCount: number;
  criticalCount: number;
}) {
  const rows: Array<{
    layer: string;
    status: string;
    boundary: string;
    next: string;
    tone: "active" | "limited" | "future";
  }> = [
    {
      layer: "身份与访问",
      status: "管理员登录 / Admin API Key",
      boundary: "当前只区分后台登录和 admin key，不做角色级 RBAC、岗位分权或双人复核。",
      next: "生产级 RBAC 与岗位权限作为独立专题，不在本页顺手实现。",
      tone: "limited",
    },
    {
      layer: "前端确认",
      status: `危险目录 ${operationCount} · 高风险 ${criticalCount}`,
      boundary: "调账、清历史、资金费、ADL、保险基金、风险阶梯和实例停机等动作继续保留前端确认。",
      next: "后续可把确认文案升级为审批单或演练单，但不弱化当前确认。",
      tone: "active",
    },
    {
      layer: "后端 guard",
      status: "写入类接口要求 confirm_execute",
      boundary: "当前后端以显式确认字段阻断误调用；这不是细粒度权限，也不替代审批。",
      next: "后续 RBAC/审批应叠加在 guard 前后，不能移除现有 guard。",
      tone: "active",
    },
    {
      layer: "操作记录",
      status: "成功结果 + 前端观察失败",
      boundary: "记录关键维护动作成功结果和页面观察到的失败事实；仍不是不可变审计账本。",
      next: "不可变审计、签名链路、导出归档和保留策略后续专题处理。",
      tone: "limited",
    },
    {
      layer: "审批流",
      status: "未启用",
      boundary: "当前没有 maker/checker、四眼复核、审批状态机或审批驳回恢复路径。",
      next: "若进入生产化，应先设计审批对象、风险等级、执行窗口和回滚核对。",
      tone: "future",
    },
    {
      layer: "日终对账",
      status: "未启用",
      boundary: "当前导出多为当前页面结果，不是日终对账包、全量账本快照或清算报告。",
      next: "日终对账、账本校验、保险基金报告和审计归档应单独做数据口径专题。",
      tone: "future",
    },
  ];
  const toneClass = (tone: "active" | "limited" | "future") =>
    tone === "active"
      ? "bg-emerald-400/12 text-emerald-100"
      : tone === "limited"
        ? "bg-amber-400/12 text-amber-100"
        : "bg-white/8 text-slate-300";

  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-3">
        <h2 className="font-display text-lg">权限与审批治理边界</h2>
        <p className="mt-1 text-sm text-slate-400">当前先把控制点讲清楚：确认、后端 guard 和操作记录已经存在；RBAC、审批流、不可变审计和日终对账仍是后续专题。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">治理层</th>
              <th className="px-3 py-2 font-normal">当前状态</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 font-normal">后续专题</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.layer} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.layer}</td>
                <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.status}</span></td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.next}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DeploymentChecklistPanel({ checklist }: { checklist?: DeploymentChecklist }) {
  if (!checklist) {
    return (
      <section className="panel mb-4 rounded-3xl p-4 text-sm text-slate-400">
        加载部署检查...
      </section>
    );
  }
  const statusLabel = checklist.status === "ok" ? "可部署" : checklist.status === "critical" ? "需处理" : "需关注";
  const statusClass =
    checklist.status === "ok"
      ? "bg-emerald-400/15 text-emerald-100"
      : checklist.status === "critical"
        ? "bg-rose-500/16 text-rose-100"
        : "bg-amber-400/16 text-amber-100";
  const priorityChecks = [
    ...checklist.checks.filter((item) => item.severity === "critical"),
    ...checklist.checks.filter((item) => item.severity === "warn"),
    ...checklist.checks.filter((item) => item.severity === "ok"),
  ];

  return (
    <section className="panel mb-4 rounded-3xl p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg">部署检查</h2>
          <p className="mt-1 text-sm text-slate-400">上线前重点看默认凭据、数据库、CORS、交易写入限流和账户挂单上限。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-3 py-1 text-xs ${statusClass}`}>{statusLabel}</span>
          <span className="rounded-full bg-white/6 px-3 py-1 text-xs text-slate-300">
            critical {checklist.summary.critical} · warn {checklist.summary.warn}
          </span>
        </div>
      </div>
      <DeploymentReadinessBoundaryStrip checklist={checklist} />
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {priorityChecks.map((item) => {
          const badgeClass =
            item.severity === "ok"
              ? "bg-emerald-400/12 text-emerald-100"
              : item.severity === "critical"
                ? "bg-rose-500/12 text-rose-100"
                : "bg-amber-400/12 text-amber-100";
          return (
            <div key={item.code} className="rounded-2xl border border-white/8 bg-white/5 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="font-medium text-slate-100">{item.label}</div>
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${badgeClass}`}>{item.severity}</span>
              </div>
              <div className="text-xs leading-5 text-slate-400">{item.detail}</div>
              <div className="mt-2 text-xs leading-5 text-cyan-100/80">{item.action}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DeploymentReadinessBoundaryStrip({ checklist }: { checklist: DeploymentChecklist }) {
  const criticalCodes = new Set(checklist.checks.filter((item) => item.severity === "critical").map((item) => item.code));
  const warnCodes = new Set(checklist.checks.filter((item) => item.severity === "warn").map((item) => item.code));
  const hasCode = (codes: string[]) => codes.some((code) => criticalCodes.has(code) || warnCodes.has(code));
  const rows: Array<{
    gate: string;
    status: string;
    boundary: string;
    next: string;
    tone: "blocked" | "warn" | "ok";
  }> = [
    {
      gate: "访问凭据",
      status: hasCode(["default_passwords", "demo_api_keys"]) ? "需处理" : "已通过",
      boundary: "默认网页登录密码和 demo API key/secret 是公网部署前硬门禁。",
      next: "在账户与资金或初始化脚本中轮换凭据；本页只提示，不展示 secret、不写配置。",
      tone: hasCode(["default_passwords", "demo_api_keys"]) ? "blocked" : "ok",
    },
    {
      gate: "管理入口",
      status: hasCode(["admin_api_key", "weak_admin_auth"]) ? "需关注" : "已通过",
      boundary: "后台登录和 Admin API Key 只适合当前轻量管理模型，不等于生产级 RBAC。",
      next: "生产化 RBAC、岗位权限和双人复核仍走独立专题；不在部署检查页顺手实现。",
      tone: hasCode(["admin_api_key", "weak_admin_auth"]) ? "warn" : "ok",
    },
    {
      gate: "网络暴露",
      status: hasCode(["cors_origins", "public_host", "insecure_origin"]) ? "需关注" : "已通过",
      boundary: "CORS、公开 host 和来源策略决定这个后台是否能被外部页面调用。",
      next: "公网部署前收紧 CORS 和访问来源；本页不直接修改环境变量或代理配置。",
      tone: hasCode(["cors_origins", "public_host", "insecure_origin"]) ? "warn" : "ok",
    },
    {
      gate: "数据持久化",
      status: hasCode(["sqlite_database", "database_backup", "large_sqlite"]) ? "需关注" : "已通过",
      boundary: "SQLite 适合本机演示和短期联调，不是长期生产级清算数据库。",
      next: "长期运行前规划 PostgreSQL、备份、恢复演练和迁移窗口；本页不执行迁库或备份。",
      tone: hasCode(["sqlite_database", "database_backup", "large_sqlite"]) ? "warn" : "ok",
    },
    {
      gate: "撮合进程",
      status: hasCode(["multi_worker", "runtime_process", "worker_count"]) ? "需处理" : "单进程口径",
      boundary: "撮合、订单簿、market lock、WS 订阅和机器人运行态仍依赖单进程内存。",
      next: "保持 1 个 uvicorn worker；多 worker/多后端一致性是后续架构专题。",
      tone: hasCode(["multi_worker", "runtime_process", "worker_count"]) ? "blocked" : "ok",
    },
    {
      gate: "交易写入保护",
      status: hasCode(["private_rate_limit", "account_open_order_limits"]) ? "需关注" : "已通过",
      boundary: "交易写入限流和账户挂单上限是本地沙盒避免自压垮的基本护栏。",
      next: "如需提高容量，先压测撮合、DB 写入、WS 推送和前端渲染；不在本页一键放开。",
      tone: hasCode(["private_rate_limit", "account_open_order_limits"]) ? "warn" : "ok",
    },
  ];
  const toneClass = (tone: "blocked" | "warn" | "ok") =>
    tone === "blocked"
      ? "bg-rose-500/16 text-rose-100"
      : tone === "warn"
        ? "bg-amber-400/12 text-amber-100"
        : "bg-emerald-400/12 text-emerald-100";

  return (
    <div className="mb-4">
      <div className="mb-3">
        <h3 className="font-display text-base text-slate-100">部署安全门禁口径</h3>
        <p className="mt-1 text-sm text-slate-500">部署检查只负责暴露门禁状态和处理边界；凭据轮换、CORS 收紧、迁库、备份和多进程架构都回对应专题处理。</p>
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1080px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="px-3 py-2 font-normal">门禁项</th>
              <th className="px-3 py-2 font-normal">当前口径</th>
              <th className="px-3 py-2 font-normal">边界说明</th>
              <th className="px-3 py-2 font-normal">处理去向</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.gate} className="border-t border-white/8 align-top">
                <td className="px-3 py-2 font-medium text-slate-100">{row.gate}</td>
                <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 ${toneClass(row.tone)}`}>{row.status}</span></td>
                <td className="px-3 py-2 leading-5 text-slate-400">{row.boundary}</td>
                <td className="px-3 py-2 leading-5 text-slate-500">{row.next}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatUptime(seconds: number) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function SystemStatusPanel({ status }: { status?: SystemStatus }) {
  if (!status) {
    return (
      <section className="panel mb-4 rounded-3xl p-4 text-sm text-slate-400">
        加载系统状态...
      </section>
    );
  }
  const statusClass = status.status === "ok" ? "bg-emerald-400/15 text-emerald-100" : "bg-amber-400/16 text-amber-100";
  const invariantItems = status.orderbook_invariants ?? [];
  const splitCount = invariantItems.reduce((sum, item) => sum + (item.engine_only_count ?? 0) + (item.db_only_count ?? 0), 0);
  const versionAlertCount = invariantItems.reduce(
    (sum, item) => sum + (item.orderbook?.same_seq_diff_count ?? 0) + (item.orderbook?.seq_rollback_count ?? 0),
    0,
  );
  const wsMetrics = status.websocket.metrics ?? {};
  return (
    <section className="panel mb-4 rounded-3xl p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg">系统状态</h2>
          <p className="mt-1 text-sm text-slate-400">4c6g 部署重点看内存、SQLite 文件、活跃挂单和数据量。</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs ${statusClass}`}>{status.status}</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-11">
        <SurveillanceMetric label="运行时长" value={formatUptime(status.uptime_seconds)} />
        <SurveillanceMetric label="RSS 峰值" value={`${fmt(status.process.rss_mb, 1)} MB`} />
        <SurveillanceMetric label="数据库" value={`${status.database.mode}${status.database.size_mb != null ? ` · ${fmt(status.database.size_mb, 1)} MB` : ""}`} />
        <SurveillanceMetric label="市场 / 主体" value={`${status.counts.active_markets}/${status.counts.markets} · ${status.counts.active_users}/${status.counts.users}`} />
        <SurveillanceMetric label="挂单 / 成交" value={`${status.counts.open_orders} / ${status.counts.trades}`} />
        <SurveillanceMetric label="WS 连接" value={`${status.websocket.public_connections} / ${status.websocket.private_connections}`} />
        <SurveillanceMetric label="WS 丢弃" value={`${wsMetrics.dropped_sockets ?? 0} / ${wsMetrics.send_timeouts ?? 0}`} />
        <SurveillanceMetric label="簿/DB差异" value={String(splitCount)} />
        <SurveillanceMetric label="版本异常" value={String(versionAlertCount)} />
        <SurveillanceMetric
          label="下单限流"
          value={status.rate_limits.enabled ? `${status.rate_limits.order_submit_rate_per_second}/s · ${status.rate_limits.order_submit_burst}` : "off"}
        />
        <SurveillanceMetric
          label="账户挂单上限"
          value={`${status.risk_limits.max_open_orders_per_user_market} / ${status.risk_limits.max_open_orders_per_user_total}`}
        />
      </div>
      {status.warnings.length > 0 && (
        <div className="mt-3 space-y-2">
          {status.warnings.map((warning) => (
            <div key={warning} className="rounded-2xl bg-amber-400/10 px-3 py-2 text-xs leading-5 text-amber-100">
              {warning}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function MarketSurveillancePanel({ item }: { item?: MarketSurveillanceItem }) {
  if (!item) {
    return (
      <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3 text-sm text-slate-400">
        加载市场监控...
      </div>
    );
  }
  const statusLabel = item.status === "ok" ? "正常" : item.status === "critical" ? "异常" : "关注";
  const statusClass =
    item.status === "ok"
      ? "bg-emerald-400/15 text-emerald-100"
      : item.status === "critical"
        ? "bg-rose-500/16 text-rose-100"
        : "bg-amber-400/16 text-amber-100";
  const imbalance = `${fmt(Number(item.metrics.book_imbalance) * 100, 1)}%`;

  return (
    <div className="mt-4 rounded-3xl border border-white/8 bg-slate-950/25 p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-display text-sm">市场监控</h4>
        <span className={`rounded-full px-2.5 py-1 text-xs ${statusClass}`}>{statusLabel}</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <SurveillanceMetric label="买一 / 卖一" value={`${fmt(item.metrics.best_bid, 8)} / ${fmt(item.metrics.best_ask, 8)}`} />
        <SurveillanceMetric label="顶层归属" value={`买 ${item.top.bid?.label ?? "-"} · 卖 ${item.top.ask?.label ?? "-"}`} />
        <SurveillanceMetric label="Spread" value={`${fmt(item.metrics.spread_pct, 4)}% · ${fmt(item.metrics.spread_quote, 8)}`} />
        <SurveillanceMetric label="盘口层数" value={`${item.metrics.bid_level_count} / ${item.metrics.ask_level_count}`} />
        <SurveillanceMetric label="0.5% / 2% 深度" value={`${fmt(item.metrics.depth_amount_0_5pct, 0)} / ${fmt(item.metrics.depth_amount_2pct, 0)}`} />
        <SurveillanceMetric label="买盘占比" value={imbalance} />
        <SurveillanceMetric label="活跃挂单" value={`${item.metrics.open_order_count} · ${fmt(item.metrics.open_order_notional, 0)}`} />
        <SurveillanceMetric label="24H 成交额" value={`${fmt(item.metrics.quote_volume_24h, 0)} · ${item.metrics.trade_count_24h} 笔`} />
      </div>
      <div className="mt-3 grid gap-3 xl:grid-cols-3">
        <div className="rounded-2xl bg-white/5 p-3">
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">诊断</div>
          <div className="space-y-2">
            {item.checks.slice(0, 3).map((check) => (
              <div key={check.code} className="rounded-xl bg-slate-950/35 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm text-slate-100">{check.label}</span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${check.severity === "ok" ? "bg-emerald-400/12 text-emerald-100" : check.severity === "critical" ? "bg-rose-500/12 text-rose-100" : "bg-amber-400/12 text-amber-100"}`}>
                    {check.severity}
                  </span>
                </div>
                <div className="mt-1 text-xs leading-5 text-slate-400">{check.detail}</div>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-2xl bg-white/5 p-3">
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">挂单角色</div>
          <div className="space-y-2">
            {item.open_orders_by_role.slice(0, 6).map((row) => (
              <div key={`${row.side}-${row.role}`} className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/35 px-3 py-2 text-xs">
                <span className={row.side === "buy" ? "text-emerald-300" : "text-rose-300"}>{row.side} · {row.role}</span>
                <span className="font-mono text-slate-100">{row.open_order_count} · {fmt(row.notional, 0)}</span>
              </div>
            ))}
            {item.open_orders_by_role.length === 0 && <div className="py-3 text-sm text-slate-500">暂无挂单</div>}
          </div>
        </div>
        <div className="rounded-2xl bg-white/5 p-3">
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">挂单账户</div>
          <div className="space-y-2">
            {item.top_open_order_users.slice(0, 5).map((row) => (
              <div key={row.username} className="rounded-xl bg-slate-950/35 px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <span className="truncate text-slate-100">{row.username}</span>
                  <span className="shrink-0 text-slate-500">{row.role}</span>
                </div>
                <div className="mt-1 font-mono text-slate-400">{row.open_order_count} 单 · {fmt(row.notional, 0)}</div>
              </div>
            ))}
            {item.top_open_order_users.length === 0 && <div className="py-3 text-sm text-slate-500">暂无账户挂单</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

function AccountingReconciliationPanel({
  summary,
  shadow,
  outbox,
  proofs,
  robotCheckpoints,
  gateReadiness,
  running,
  proofRunning,
  robotCheckpointRunning,
  onRun,
  onCreateProof,
  onCreateRobotCheckpoint,
}: {
  summary: ReconciliationSummary | null;
  shadow: ShadowAccountingSummary | null;
  outbox: FinancialOutboxSummary | null;
  proofs: AccountingProofPage | null;
  robotCheckpoints: RobotFinancialCheckpointPage | null;
  gateReadiness: AccountingGateReadiness | null;
  running: boolean;
  proofRunning: boolean;
  robotCheckpointRunning: boolean;
  onRun: () => void;
  onCreateProof: () => void;
  onCreateRobotCheckpoint: () => void;
}) {
  const [domain, setDomain] = useState("all");
  const domains = [
    ["all", "总览"], ["spot", "现货"], ["contract", "合约"], ["funding", "资金费"],
    ["liquidation", "保险/强平/ADL"], ["robot", "机器人"], ["orders", "订单/成交"], ["accounting", "影子复式"], ["outbox", "可靠事件"],
  ];
  const checks = summary?.checks.filter((item) => domain === "all" || item.domain === domain) ?? [];
  const unmetGateCriteria = gateReadiness?.gate_e.criteria.filter((item) => !item.passed) ?? [];
  const gateReadinessUnavailable = gateReadiness === null;
  const statusClass = summary?.status === "failed"
    ? "border-rose-400/25 bg-rose-400/8 text-rose-50"
    : summary?.status === "warning"
      ? "border-amber-400/25 bg-amber-400/8 text-amber-50"
      : "border-emerald-400/25 bg-emerald-400/8 text-emerald-50";
  const exportResult = () => {
    if (!summary) return;
    downloadCsv(
      `accounting_reconciliation_${summary.run_id}.csv`,
      ["run_id", "domain", "check_code", "severity", "status", "difference_count", "max_difference", "sample_entity_id", "sample_user_id", "sample_json", "recommendation"],
      summary.checks.map((item) => [
        summary.run_id, item.domain, item.code, item.severity, item.status, item.difference_count,
        item.max_difference ?? "", item.samples?.[0]?.entity_id ?? "", item.samples?.[0]?.user_id ?? "",
        JSON.stringify(item.samples ?? []), item.recommendation,
      ]),
    );
  };
  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="font-display text-xl">账务与对账</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-slate-400">数据库已提交账务是最终事实。这里按水位只读检查现货、合约、资金费、清算与影子复式账本；运行对账只写入证明记录，不修改余额、仓位或历史异常。</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={exportResult} disabled={!summary} className="rounded-xl bg-white/8 px-4 py-2 text-sm text-slate-100 disabled:opacity-40">导出对账结果</button>
          <button type="button" onClick={onCreateProof} disabled={proofRunning || running} className="rounded-xl bg-violet-400/16 px-4 py-2 text-sm font-medium text-violet-100 disabled:opacity-50">{proofRunning ? "固定水位并生成中..." : "生成证明检查点"}</button>
          <button type="button" onClick={onRun} disabled={running} className="rounded-xl bg-cyan-400/16 px-4 py-2 text-sm font-medium text-cyan-100 disabled:opacity-50">{running ? "对账扫描中..." : "运行只读对账"}</button>
        </div>
      </div>
      <div className={`mb-4 rounded-2xl border p-4 ${statusClass}`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] opacity-70">当前账务健康</div>
            <div className="mt-1 text-lg font-medium">{summary ? (summary.allow_trading ? "未发现阻断级差异" : "存在阻断级差异，不允许据此宣称账务健康") : "尚未生成对账证明"}</div>
          </div>
          <div className="font-mono text-xs tabular-nums opacity-80">{summary ? `${summary.run_id} · ${summary.elapsed_ms} ms` : "-"}</div>
        </div>
      </div>
      <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-7">
        <SurveillanceMetric label="状态" value={summary?.status ?? "not_run"} />
        <SurveillanceMetric label="阻断差异" value={String(summary?.blocking_count ?? 0)} />
        <SurveillanceMetric label="全部差异" value={String(summary?.difference_count ?? 0)} />
        <SurveillanceMetric label="最大差异" value={summary?.max_difference ?? "0"} />
        <SurveillanceMetric label="影子交易" value={String(shadow?.transactions ?? 0)} />
        <SurveillanceMetric label="复式分录" value={String(shadow?.entries ?? 0)} />
        <SurveillanceMetric label="Source of truth" value={shadow?.source_of_truth ? "是" : "否 · shadow"} />
      </div>
      <div className="mb-4 rounded-2xl border border-cyan-400/12 bg-cyan-400/5 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-cyan-100/60">事务型数据库 Outbox</div>
            <div className="mt-1 text-xs leading-5 text-slate-400">资金流水与事件同事务落库；投递语义为 at-least-once，消费者按 event_id 幂等。现有实时广播仍保留，不是资金最终事实。</div>
          </div>
          <div className="font-mono text-xs text-cyan-100/70">{outbox?.checkpoint?.name ?? "checkpoint missing"}</div>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-3 xl:grid-cols-8">
          <SurveillanceMetric label="Pending" value={String(outbox?.counts.pending ?? 0)} />
          <SurveillanceMetric label="Processing" value={String(outbox?.counts.processing ?? 0)} />
          <SurveillanceMetric label="Delivered" value={String(outbox?.counts.delivered ?? 0)} />
          <SurveillanceMetric label="Dead" value={String(outbox?.counts.dead ?? 0)} />
          <SurveillanceMetric label="Receipts" value={String(outbox?.counts.receipts ?? 0)} />
          <SurveillanceMetric label="Retry events" value={String(outbox?.counts.retry_events ?? 0)} />
          <SurveillanceMetric label="受控重放" value={String(outbox?.counts.replay_requests ?? 0)} />
          <SurveillanceMetric label="Runtime error" value={outbox?.runtime?.last_error ? "有" : "无"} />
        </div>
      </div>
      <div className="mb-4 rounded-2xl border border-violet-400/14 bg-violet-400/5 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-violet-100/65">连续账务证明链</div>
            <div className="mt-1 max-w-4xl text-xs leading-5 text-slate-400">固定一个 SQLite 一致性水位，在同一事务内运行全量对账，并保存业务快照、复式重建快照及前序 SHA-256。检查点不可更新、不可删除；生成时会短暂阻挡资金写入，不是日常交易路径。</div>
          </div>
          <div className="max-w-[360px] truncate font-mono text-xs text-violet-100/70" title={proofs?.verification.latest?.proof_hash ?? ""}>{proofs?.verification.latest?.proof_hash ?? "no checkpoint"}</div>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-3 xl:grid-cols-7">
          <SurveillanceMetric label="检查点" value={String(proofs?.verification.checkpoint_count ?? 0)} />
          <SurveillanceMetric label="链异常" value={String(proofs?.verification.invalid_count ?? 0)} />
          <SurveillanceMetric label="最新序号" value={proofs?.verification.latest ? `#${proofs.verification.latest.sequence_no}` : "-"} />
          <SurveillanceMetric label="最新状态" value={proofs?.verification.latest?.status ?? "not_created"} />
          <SurveillanceMetric label="快照一致" value={proofs?.verification.latest ? (proofs.verification.latest.business_snapshot_hash === proofs.verification.latest.accounting_snapshot_hash ? "是" : "否") : "-"} />
          <SurveillanceMetric label="快照行" value={String(proofs?.verification.latest?.counts.total_rows ?? 0)} />
          <SurveillanceMetric label="签名 / WORM" value={proofs?.verification.signed_or_worm ? "已启用" : "未启用"} />
        </div>
      </div>
      <div className="mb-4 rounded-2xl border border-cyan-400/14 bg-cyan-400/5 p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-cyan-100/65">机器人金融 checkpoint</div>
            <div className="mt-1 max-w-4xl text-xs leading-5 text-slate-400">只覆盖 Outbox 切换后的机器人现货与合约资金事实，固定期初、期末、期间借贷、数量与校验值。客户记录不进入该域；当前仅生成证明，不授权删除原始金融记录。</div>
          </div>
          <button type="button" onClick={onCreateRobotCheckpoint} disabled={robotCheckpointRunning || proofRunning || running} className="rounded-xl bg-cyan-400/16 px-4 py-2 text-sm font-medium text-cyan-100 disabled:opacity-50">
            {robotCheckpointRunning ? "固定机器人水位中..." : "生成机器人 checkpoint"}
          </button>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-3 xl:grid-cols-8">
          <SurveillanceMetric label="检查点" value={String(robotCheckpoints?.verification.checkpoint_count ?? 0)} />
          <SurveillanceMetric label="链异常" value={String(robotCheckpoints?.verification.invalid_count ?? 0)} />
          <SurveillanceMetric label="最新状态" value={robotCheckpoints?.verification.latest?.status ?? "not_created"} />
          <SurveillanceMetric label="区间资金事实" value={String(robotCheckpoints?.verification.latest?.counts.source_count ?? 0)} />
          <SurveillanceMetric label="客户记录" value={String(robotCheckpoints?.verification.latest?.counts.customer_source_count ?? 0)} />
          <SurveillanceMetric label="无效分组" value={String(robotCheckpoints?.verification.latest?.counts.invalid_item_count ?? 0)} />
          <SurveillanceMetric label="原始删除" value={String(robotCheckpoints?.verification.raw_records_deleted ?? 0)} />
          <SurveillanceMetric label="财务裁剪" value={robotCheckpoints?.verification.financial_pruning_authorized ? "已授权" : "禁止"} />
        </div>
      </div>
      <div className={`mb-4 rounded-2xl border p-3 ${gateReadiness?.gate_e.ready ? "border-emerald-400/18 bg-emerald-400/6" : "border-amber-400/18 bg-amber-400/6"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs uppercase tracking-[0.16em] text-slate-300">Gate E 长期证据门禁</div>
            <div className="mt-1 max-w-4xl text-xs leading-5 text-slate-400">
              {gateReadinessUnavailable
                ? "门禁证据正在加载或接口不可用，当前不允许据此判定 Gate E；Gate F 继续 fail-closed。"
                : gateReadiness.gate_e.ready
                ? "当前运行证据已满足本地 Gate E 量化条件；仍不代表允许切换 source-of-truth。"
                : `正在收集证据：还有 ${unmetGateCriteria.length} 项条件未满足。每日采集器按 UTC 日期幂等运行，同日重复不会被当成多日 soak。`}
            </div>
          </div>
          <div className="rounded-full bg-slate-950/35 px-3 py-1 font-mono text-xs text-slate-200">Gate F: {gateReadiness?.gate_f.status ?? "closed (fail-closed)"}</div>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-3 xl:grid-cols-9">
          <SurveillanceMetric label="Gate E" value={gateReadiness?.gate_e.status ?? "unavailable"} />
          <SurveillanceMetric label="每日采集" value={gateReadiness?.evidence_collector.latest?.status ?? (gateReadiness?.evidence_collector.enabled ? "waiting" : "disabled")} hint={gateReadiness?.evidence_collector.latest?.observation_date ?? undefined} />
          <SurveillanceMetric label="账务 proof" value={gateReadiness ? String(gateReadiness.gate_e.observation.accounting_proof_count) : "-"} />
          <SurveillanceMetric label="观察小时" value={gateReadiness ? String(gateReadiness.gate_e.observation.observation_hours) : "-"} />
          <SurveillanceMetric label="UTC 日期" value={gateReadiness ? String(gateReadiness.gate_e.observation.distinct_utc_dates.length) : "-"} />
          <SurveillanceMetric label="机器人 checkpoint" value={gateReadiness ? String(gateReadiness.gate_e.observation.robot_checkpoint_count) : "-"} />
          <SurveillanceMetric label="非空机器人区间" value={gateReadiness ? String(gateReadiness.gate_e.observation.nonempty_robot_checkpoint_count) : "-"} />
          <SurveillanceMetric label="机器人资金事实" value={gateReadiness ? String(gateReadiness.gate_e.observation.robot_source_count) : "-"} />
          <SurveillanceMetric label="未满足条件" value={gateReadiness ? String(unmetGateCriteria.length) : "-"} />
        </div>
        {unmetGateCriteria.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {unmetGateCriteria.map((item) => (
              <span key={item.code} className="rounded-full bg-slate-950/35 px-2 py-1 font-mono text-[11px] text-amber-100/80" title={item.evidence}>
                {item.code}: {String(item.actual)} / {String(item.required)}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="mb-4 flex gap-2 overflow-x-auto pb-1">
        {domains.map(([key, label]) => <button key={key} type="button" onClick={() => setDomain(key)} className={`shrink-0 rounded-xl px-3 py-2 text-sm ${domain === key ? "bg-violet-400/16 text-violet-100" : "bg-white/6 text-slate-300"}`}>{label}</button>)}
      </div>
      <div className="overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
        <table className="min-w-[1180px] text-left text-xs">
          <thead className="text-slate-500"><tr><th className="px-3 py-2 font-normal">域</th><th className="px-3 py-2 font-normal">不变量</th><th className="px-3 py-2 font-normal">等级</th><th className="px-3 py-2 font-normal">结果</th><th className="px-3 py-2 text-right font-normal">差异</th><th className="px-3 py-2 text-right font-normal">最大差异</th><th className="px-3 py-2 font-normal">样本证据</th><th className="px-3 py-2 font-normal">建议动作</th></tr></thead>
          <tbody>{checks.map((item) => {
            const sample = item.samples?.[0];
            return <tr key={item.code} className="border-t border-white/8 align-top"><td className="px-3 py-2 text-slate-300">{item.domain}</td><td className="px-3 py-2 font-mono text-slate-100">{item.code}</td><td className="px-3 py-2 text-slate-400">{item.severity}</td><td className={`px-3 py-2 ${item.status === "failed" ? "text-rose-300" : item.status === "warning" ? "text-amber-300" : "text-emerald-300"}`}>{item.status}</td><td className="px-3 py-2 text-right font-mono tabular-nums text-slate-100">{item.difference_count}</td><td className="px-3 py-2 text-right font-mono tabular-nums text-slate-300">{item.max_difference ?? "-"}</td><td className="max-w-[260px] px-3 py-2 font-mono text-[11px] leading-5 text-slate-400"><div className="truncate" title={sample ? JSON.stringify(sample) : ""}>{sample ? `${String(sample.entity_id ?? "-")} · UID ${String(sample.user_id ?? "-")}` : "-"}</div>{item.samples?.length > 1 && <div className="text-slate-600">样本 {item.samples.length} 条，导出查看完整字段</div>}</td><td className="px-3 py-2 leading-5 text-slate-500">{item.recommendation}</td></tr>;
          })}</tbody>
        </table>
        {!summary && <div className="px-4 py-8 text-center text-sm text-slate-500">运行首次只读对账后显示水位、差异和处置建议。</div>}
      </div>
      <div className="mt-4 rounded-2xl border border-amber-400/14 bg-amber-400/6 px-4 py-3 text-xs leading-5 text-amber-50/80">默认没有“一键自动修账”。任何更正都必须通过显式 adjustment/reversal 留下原因、前后值、关联 run_id 与操作者证据。影子复式账本未经连续验证，不是当前 source-of-truth。</div>
    </section>
  );
}

function SurveillanceMetric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 overflow-hidden rounded-2xl bg-white/5 px-3 py-2">
      <div className="truncate text-xs uppercase tracking-[0.16em] text-slate-500">{label}</div>
      <div className="mt-1 min-w-0 truncate font-mono text-sm tabular-nums text-slate-100" title={value}>{value}</div>
      {hint && <div className="mt-1 truncate text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

function ContractDetailLoadBadge({ state }: { state: ContractDetailLoadState }) {
  const className = state.error
    ? "bg-rose-500/16 text-rose-100"
    : state.loading
      ? "bg-cyan-400/16 text-cyan-100"
      : state.loaded
        ? "bg-emerald-400/15 text-emerald-100"
        : "bg-white/8 text-slate-300";
  const label = state.error
    ? "明细失败"
    : state.loading
      ? "明细加载中"
      : state.loaded
        ? `明细 ${bjTime(state.updatedAt)}`
        : "明细按需";
  return <span className={`rounded-full px-2 py-1 text-xs ${className}`}>{label}</span>;
}

function AccountDetailPanel({
  user,
  kind,
  contractAccounts,
  contractPositions,
  contractOrders,
	  contractLedgerEntries,
	  contractDetailLoadState,
	  activity,
	  activityVisible,
	  activityLoading,
	  onToggleActivity,
	  onOpenSection,
	  onOpenContractAccounts,
	  onOpenOrderAudit,
	}: {
	  user: AdminUser;
	  kind: AccountUserKind;
	  contractAccounts: ContractAccountAdminItem[];
	  contractPositions: ContractPositionAdminItem[];
	  contractOrders: ContractOrderAdminItem[];
	  contractLedgerEntries: ContractLedgerAdminItem[];
	  contractDetailLoadState: ContractDetailLoadState;
	  activity?: AdminUserActivity;
	  activityVisible: boolean;
	  activityLoading: boolean;
	  onToggleActivity: () => void;
	  onOpenSection: (section: AdminSection) => void;
	  onOpenContractAccounts: () => void;
	  onOpenOrderAudit: (target: OrderAuditTarget) => void;
	}) {
  const usdtBalance = user.balances.find((item) => item.asset === "USDT");
  const activePositions = contractPositions.filter((item) => item.position.side !== "flat" && Number(item.position.quantity || 0) > 0);
  const liveContractOrders = contractOrders.filter((item) => isLiveOrderStatus(item.order.status));
  const contractWallet = sumDecimalStrings(contractAccounts.map((item) => item.account.wallet_balance));
  const contractAvailable = sumDecimalStrings(contractAccounts.map((item) => item.account.available_margin));
  const contractUsed = sumDecimalStrings(contractAccounts.map((item) => item.account.used_margin));
  const contractUnrealized = sumDecimalStrings(contractAccounts.map((item) => item.account.unrealized_pnl));
  const timelineItems = buildAccountTimeline({ activity, contractOrders, contractLedgerEntries });
  const exportAccountReview = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const safeUsername = user.username.replace(/[^a-zA-Z0-9_-]+/g, "-") || `user-${user.id}`;
    const activityState = activity ? "loaded" : "not_loaded";
    const contractState = contractDetailLoadState.error
      ? "error"
      : contractDetailLoadState.loading
        ? "loading"
        : contractDetailLoadState.loaded
          ? "loaded"
          : "not_loaded";
    const rows: unknown[][] = [];
    const addRow = (
      recordType: string,
      domain: string,
      event: string,
      marketOrAsset: unknown,
      statusOrType: unknown,
      side: unknown,
      amount: unknown,
      quantity: unknown,
      price: unknown,
      balanceBefore: unknown,
      balanceAfter: unknown,
      riskStatus: unknown,
      detail: unknown,
      ts: unknown,
      sourceId: unknown,
    ) => {
      rows.push([
        recordType,
        user.id,
        user.username,
        accountUserKindLabel(kind),
        user.role,
        user.is_active ? "active" : "paused",
        activityState,
        contractState,
        domain,
        event,
        marketOrAsset,
        statusOrType,
        side,
        amount,
        quantity,
        price,
        balanceBefore,
        balanceAfter,
        riskStatus,
        detail,
        ts ? bjDateTime(Number(ts)) : "",
        sourceId,
      ]);
    };

    addRow(
      "summary",
      "账户",
      "UID 摘要",
      "",
      user.is_active ? "active" : "paused",
      "",
      "",
      "",
      "",
      "",
      "",
      "",
      `spot_assets=${user.balances.length}; contract_accounts=${contractAccounts.length}; active_positions=${activePositions.length}; live_contract_orders=${liveContractOrders.length}; contract_ledger_entries=${contractLedgerEntries.length}`,
      Date.now(),
      `user:${user.id}`,
    );
    user.balances.forEach((item) => {
      const total = Number(item.available || 0) + Number(item.frozen || 0);
      addRow("spot_balance", "现货", "现货余额", item.asset, "balance", "", total, "", "", item.available, item.frozen, "", `available=${item.available}; frozen=${item.frozen}`, "", item.asset);
    });
    contractAccounts.forEach(({ account }) => {
      addRow(
        "contract_account",
        "合约",
        "合约保证金账户",
        account.margin_asset,
        "margin_account",
        "",
        account.wallet_balance,
        "",
        "",
        account.available_margin,
        account.used_margin,
        "",
        `unrealized_pnl=${account.unrealized_pnl}; realized_pnl=${account.realized_pnl}; total_fees=${account.total_fees}`,
        account.updated_at,
        `contract-account:${account.user_id}:${account.margin_asset}`,
      );
    });
    activePositions.forEach(({ position }) => {
      addRow(
        "contract_position",
        "合约",
        "合约持仓风险",
        position.symbol,
        position.risk_status ?? "",
        position.side,
        position.unrealized_pnl,
        position.quantity,
        position.mark_price,
        position.entry_price,
        position.liquidation_price,
        position.risk_status ?? "",
        `leverage=${position.leverage}; risk_tier=${position.risk_tier ?? ""}; maintenance_margin_rate=${position.maintenance_margin_rate ?? ""}; liquidation_distance_pct=${position.liquidation_distance_pct ?? ""}`,
        position.updated_at,
        `position:${position.symbol}:${position.side}`,
      );
    });
    contractOrders.forEach(({ order }) => {
      addRow(
        isLiveOrderStatus(order.status) ? "contract_live_order" : "contract_recent_order",
        "合约",
        "合约委托",
        order.symbol,
        order.status,
        order.side,
        order.notional,
        `${order.remaining_quantity}/${order.quantity}`,
        order.price,
        "",
        "",
        "",
        `position_action=${order.position_action ?? ""}; reduce_only=${order.reduce_only ? "true" : "false"}; type=${order.type}; tif=${order.tif ?? ""}; reject_reason=${order.reject_reason ?? ""}`,
        order.updated_at ?? order.created_at,
        order.client_order_id ?? order.order_id,
      );
    });
    contractLedgerEntries.forEach(({ entry }) => {
      addRow(
        "contract_ledger",
        "合约",
        "合约流水",
        entry.symbol ?? entry.margin_asset,
        entry.change_type,
        "",
        entry.amount,
        "",
        "",
        entry.wallet_before,
        entry.wallet_after,
        "",
        `available=${entry.available_before}->${entry.available_after}; used=${entry.used_margin_before}->${entry.used_margin_after}; note=${entry.note ?? ""}; related_event_id=${entry.related_event_id ?? ""}`,
        entry.created_at,
        entry.entry_id,
      );
    });
    timelineItems.forEach((item) => {
      addRow("timeline", item.domain, item.event, item.market ?? "", "", "", item.amount ?? "", "", "", "", "", item.tone ?? "", item.detail, item.ts, item.key);
    });

    downloadCsv(
      `admin_account_review_${safeUsername}_${stamp}.csv`,
      [
        "record_type",
        "user_id",
        "username",
        "account_kind",
        "role",
        "account_status",
        "spot_activity_state",
        "contract_detail_state",
        "domain",
        "event",
        "market_or_asset",
        "status_or_type",
        "side",
        "amount",
        "quantity",
        "price",
        "balance_before",
        "balance_after",
        "risk_status",
        "detail",
        "time",
        "source_id",
      ],
      rows,
    );
  };

  return (
    <section className="mb-4 rounded-3xl border border-violet-400/14 bg-violet-400/6 p-4">
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-display text-lg">单 UID 核查视图</h3>
            <span className={`rounded-full px-2 py-1 text-xs ${accountUserKindClass(kind)}`}>{accountUserKindLabel(kind)}</span>
            <span className={user.is_active ? "rounded-full bg-emerald-400/15 px-2 py-1 text-xs text-emerald-100" : "rounded-full bg-rose-500/15 px-2 py-1 text-xs text-rose-100"}>
              {user.is_active ? "active" : "paused"}
            </span>
            <ContractDetailLoadBadge state={contractDetailLoadState} />
          </div>
          <p className="mt-1 text-sm text-slate-400">
            {user.username} · {user.role} · 同一登录主体下分开展示现货余额和合约保证金，不合并为统一资金。
          </p>
        </div>
        <div className="flex flex-wrap gap-2 lg:justify-end">
	          <button type="button" onClick={exportAccountReview} className="self-start rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12">
	            导出当前核查 CSV
	          </button>
	          <button onClick={onToggleActivity} className="self-start rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12">
	            {activityVisible ? "收起现货活动" : activity ? "查看现货活动" : "加载现货活动"}
	          </button>
	          <button
	            type="button"
	            onClick={() => onOpenOrderAudit({ userId: user.id, status: "all", dataScope: orderAuditDataScopeForUser(user) })}
	            className="self-start rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100 hover:bg-white/12"
	          >
	            查订单成交
	          </button>
	          <button type="button" onClick={onOpenContractAccounts} className="self-start rounded-2xl bg-violet-400/14 px-4 py-2 text-sm text-violet-100 hover:bg-violet-400/20">
	            看合约清算
	          </button>
          {(kind === "spot_robot" || kind === "contract_robot") && (
            <button type="button" onClick={() => onOpenSection("bots")} className="self-start rounded-2xl bg-cyan-400/14 px-4 py-2 text-sm text-cyan-100 hover:bg-cyan-400/20">
              看机器人绑定
            </button>
          )}
        </div>
      </div>
      <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
        <ActivityMetric label="API Key" value={user.api_key} />
        <ActivityMetric label="现货资产数" value={String(user.balances.length)} />
        <ActivityMetric label="USDT 可用" value={fmt(usdtBalance?.available, 2)} />
        <ActivityMetric label="USDT 冻结" value={fmt(usdtBalance?.frozen, 2)} />
        <ActivityMetric label="保证金钱包" value={fmt(contractWallet, 2)} />
        <ActivityMetric label="可用保证金" value={fmt(contractAvailable, 2)} />
        <ActivityMetric label="占用保证金" value={fmt(contractUsed, 2)} />
        <ActivityMetric label="未实现PnL" value={fmt(contractUnrealized, 2)} />
        <ActivityMetric label="合约持仓" value={String(activePositions.length)} />
        <ActivityMetric label="合约当前委托" value={String(liveContractOrders.length)} />
        <ActivityMetric label="合约最近委托" value={String(contractOrders.length)} />
        <ActivityMetric label="合约流水" value={String(contractLedgerEntries.length)} />
      </div>
      <div className="mb-4 rounded-2xl border border-white/8 bg-slate-950/25 px-3 py-2 text-xs leading-5 text-slate-400">
        操作边界：主体身份、API、费率和现货入账/扣款在下方 UID 维护卡处理；合约保证金、持仓、资金费、强平和保险基金继续归合约清算域。这里是只读运营摘要。
      </div>
      <AccountDomainBoundaryStrip
        user={user}
        kind={kind}
        contractAccounts={contractAccounts}
        activePositions={activePositions}
        liveContractOrders={liveContractOrders}
        contractWallet={contractWallet}
        contractAvailable={contractAvailable}
        usdtBalance={usdtBalance}
      />
      <div className="mb-4">
        <ActivitySection title="UID 时间线" empty={false}>
          {timelineItems.length > 0 ? (
            <AccountTimelineTable items={timelineItems} />
          ) : (
            <div className="py-4 text-sm text-slate-500">暂无已加载事件。</div>
          )}
          {!activity && (
            <div className="mt-2 rounded-xl border border-white/8 bg-slate-950/30 px-3 py-2 text-xs leading-5 text-slate-400">
              当前时间线已包含已加载合约委托和保证金流水；点击“加载现货活动”后会合并最近现货订单、成交和资金流水。
            </div>
          )}
        </ActivitySection>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <ActivitySection title="现货余额" empty={user.balances.length === 0}>
          <SpotBalanceMiniTable items={user.balances} />
        </ActivitySection>
        <ActivitySection title="合约保证金账户" empty={contractAccounts.length === 0}>
          <ContractAccountMiniTable items={contractAccounts} />
        </ActivitySection>
        <ActivitySection title="合约持仓风险" empty={activePositions.length === 0}>
          <ContractPositionMiniTable items={activePositions} />
        </ActivitySection>
        <ActivitySection title="合约委托" empty={contractOrders.length === 0}>
          <OrderActivityTable items={contractOrders.map((item) => item.order)} />
        </ActivitySection>
        <ActivitySection title="合约流水" empty={contractLedgerEntries.length === 0}>
          <ContractLedgerActivityTable items={contractLedgerEntries.map((item) => item.entry)} />
        </ActivitySection>
      </div>
      {activityVisible && (
        <UserActivityPanel
          activity={activity}
          loading={activityLoading}
        />
      )}
    </section>
  );
}

function UserActivityPanel({ activity, loading }: { activity?: AdminUserActivity; loading: boolean }) {
  if (loading && !activity) {
    return (
      <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3 text-sm text-slate-400">
        加载 UID 活动...
      </div>
    );
  }
  if (!activity) {
    return (
      <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3 text-sm text-slate-400">
        暂无 UID 活动数据。
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-display text-sm">UID 活动</h4>
        <span className="text-xs text-slate-500">
          {activity.user.username} · {activity.user.role}
        </span>
      </div>
      <div className="mb-3 grid gap-2 sm:grid-cols-4">
        <ActivityMetric label="当前挂单" value={String(activity.summary.open_order_count)} />
        <ActivityMetric label="最近订单" value={String(activity.summary.recent_order_count)} />
        <ActivityMetric label="最近成交" value={String(activity.summary.recent_trade_count)} />
        <ActivityMetric label="资金流水" value={String(activity.summary.recent_ledger_count)} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-4">
        <ActivitySection title="当前挂单" empty={activity.open_orders.length === 0}>
          <OrderActivityTable items={activity.open_orders} />
        </ActivitySection>
        <ActivitySection title="最近订单" empty={activity.recent_orders.length === 0}>
          <OrderActivityTable items={activity.recent_orders} />
        </ActivitySection>
        <ActivitySection title="最近成交" empty={activity.recent_trades.length === 0}>
          <TradeActivityTable items={activity.recent_trades} />
        </ActivitySection>
        <ActivitySection title="资金流水" empty={activity.recent_ledger.length === 0}>
          <LedgerActivityTable items={activity.recent_ledger} />
        </ActivitySection>
      </div>
    </div>
  );
}

function ActivityMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 overflow-hidden rounded-xl bg-white/5 px-3 py-2">
      <div className="truncate text-xs uppercase tracking-[0.16em] text-slate-500">{label}</div>
      <div className="mt-1 min-w-0 truncate font-mono text-sm tabular-nums text-slate-100" title={value}>{value}</div>
    </div>
  );
}

const AUDIT_TABLE_PAGE_SIZE = 50;

function AuditTablePager({
  page,
  total,
  pageSize,
  label,
  onPageChange,
}: {
  page: number;
  total: number;
  pageSize: number;
  label: string;
  onPageChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(total, page * pageSize);
  const buttonClass = "rounded-xl bg-white/8 px-3 py-1.5 text-xs text-slate-100 hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <div className="mb-2 flex flex-col gap-2 rounded-xl border border-white/8 bg-slate-950/25 px-3 py-2 text-xs text-slate-400 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <span className="font-medium text-slate-200">{label}</span>
        <span className="ml-2 font-mono tabular-nums">{start}-{end} / {total}</span>
        <span className="ml-2 text-slate-500">屏幕分页，CSV 仍导出当前筛选全部结果</span>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={buttonClass} disabled={page <= 1} onClick={() => onPageChange(Math.max(1, page - 1))}>上一页</button>
        <span className="font-mono tabular-nums text-slate-300">{page} / {totalPages}</span>
        <button type="button" className={buttonClass} disabled={page >= totalPages} onClick={() => onPageChange(Math.min(totalPages, page + 1))}>下一页</button>
      </div>
    </div>
  );
}

function AccountDomainBoundaryStrip({
  user,
  kind,
  contractAccounts,
  activePositions,
  liveContractOrders,
  contractWallet,
  contractAvailable,
  usdtBalance,
}: {
  user: AdminUser;
  kind: AccountUserKind;
  contractAccounts: ContractAccountAdminItem[];
  activePositions: ContractPositionAdminItem[];
  liveContractOrders: ContractOrderAdminItem[];
  contractWallet: string | number;
  contractAvailable: string | number;
  usdtBalance?: AdminUser["balances"][number];
}) {
  const isRobot = kind === "spot_robot" || kind === "contract_robot";
  const rows = [
    {
      domain: "登录主体",
      owner: "账户与资金",
      status: `${user.username} #${user.id} · ${user.is_active ? "active" : "paused"}`,
      boundary: "身份、密码、API Key、费率和启停状态，不代表统一资金账户。",
    },
    {
      domain: "现货钱包",
      owner: "账户与资金",
      status: `${user.balances.length} 资产 · USDT 可用 ${fmt(usdtBalance?.available, 2)} / 冻结 ${fmt(usdtBalance?.frozen, 2)}`,
      boundary: "现货余额、冻结和现货流水独立于合约保证金。",
    },
    {
      domain: "合约保证金",
      owner: "合约清算",
      status: `${contractAccounts.length} 账户 · 钱包 ${fmt(contractWallet, 2)} / 可用 ${fmt(contractAvailable, 2)} · 持仓 ${activePositions.length} / 委托 ${liveContractOrders.length}`,
      boundary: "保证金、逐仓持仓、资金费、强平、ADL 和保险基金都回到清算域。",
    },
    {
      domain: "机器人资源",
      owner: "机器人账号",
      status: isRobot ? `${accountUserKindLabel(kind)} · 绑定和 API Key 去机器人账号核对` : "非机器人身份",
      boundary: "机器人绑定、策略角色和运行资金快照不在账户卡里执行。",
    },
  ];

  return (
    <div className="mb-4 overflow-auto rounded-2xl border border-white/8 bg-slate-950/25">
      <table className="min-w-[920px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="px-3 py-2 font-normal">数据域</th>
            <th className="px-3 py-2 font-normal">主入口</th>
            <th className="px-3 py-2 font-normal">当前状态</th>
            <th className="px-3 py-2 font-normal">边界说明</th>
          </tr>
        </thead>
        <tbody className="text-slate-300">
          {rows.map((row) => (
            <tr key={row.domain} className="border-t border-white/8 align-top">
              <td className="px-3 py-2 font-medium text-slate-100">{row.domain}</td>
              <td className="px-3 py-2">{row.owner}</td>
              <td className="px-3 py-2 font-mono text-slate-200">{row.status}</td>
              <td className="px-3 py-2 leading-5 text-slate-500">{row.boundary}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActivitySection({ title, empty, children }: { title: string; empty: boolean; children: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-2xl bg-white/5 p-3">
      <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">{title}</div>
      {empty ? <div className="py-4 text-sm text-slate-500">暂无记录</div> : children}
    </div>
  );
}

function AccountTimelineTable({ items }: { items: AccountTimelineItem[] }) {
  return (
    <div className="max-h-72 overflow-auto">
      <table className="min-w-[820px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">时间</th>
            <th className="py-1 pr-3 font-normal">域</th>
            <th className="py-1 pr-3 font-normal">事件</th>
            <th className="py-1 pr-3 font-normal">市场/资产</th>
            <th className="py-1 pr-3 font-normal">详情</th>
            <th className="py-1 pr-3 font-normal">金额/数量</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => {
            const amountClass =
              item.tone === "positive"
                ? "text-emerald-300"
                : item.tone === "negative"
                  ? "text-rose-300"
                  : "text-slate-300";
            const domainClass = item.domain === "合约" ? "bg-violet-400/14 text-violet-100" : "bg-cyan-400/14 text-cyan-100";
            return (
              <tr key={item.key} className="border-t border-white/8">
                <td className="py-1.5 pr-3">{bjTime(item.ts)}</td>
                <td className="py-1.5 pr-3 font-sans">
                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${domainClass}`}>{item.domain}</span>
                </td>
                <td className="py-1.5 pr-3 font-sans text-slate-100">{item.event}</td>
                <td className="py-1.5 pr-3">{item.market ?? "-"}</td>
                <td className="max-w-[260px] truncate py-1.5 pr-3 text-slate-400" title={item.detail}>{item.detail}</td>
                <td className={`py-1.5 pr-3 ${amountClass}`}>{item.amount ?? "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OrderActivityTable({ items }: { items: OrderItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[620px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">时间</th>
            <th className="py-1 pr-3 font-normal">市场</th>
            <th className="py-1 pr-3 font-normal">方向</th>
            <th className="py-1 pr-3 font-normal">价格</th>
            <th className="py-1 pr-3 font-normal">数量</th>
            <th className="py-1 pr-3 font-normal">状态</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={item.order_id} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{bjTime(item.updated_at ?? item.created_at)}</td>
              <td className="py-1.5 pr-3">{item.symbol}</td>
              <td className={`py-1.5 pr-3 ${item.side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{item.side}</td>
              <td className="py-1.5 pr-3">{fmt(item.price, 6)}</td>
              <td className="py-1.5 pr-3">{fmt(item.remaining_quantity, 6)} / {fmt(item.quantity, 6)}</td>
              <td className="py-1.5 pr-3">{item.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AuditOrderTable({ items }: { items: AdminOrderAuditItem[] }) {
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(items.length / AUDIT_TABLE_PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visibleItems = items.slice((safePage - 1) * AUDIT_TABLE_PAGE_SIZE, safePage * AUDIT_TABLE_PAGE_SIZE);
  useEffect(() => {
    setPage(1);
  }, [items]);

  return (
    <>
      <AuditTablePager page={safePage} total={items.length} pageSize={AUDIT_TABLE_PAGE_SIZE} label="订单审计分页" onPageChange={setPage} />
      <div className="max-h-[520px] overflow-auto">
        <table className="min-w-[920px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-normal">时间</th>
              <th className="py-1 pr-3 font-normal">账户</th>
              <th className="py-1 pr-3 font-normal">产品</th>
              <th className="py-1 pr-3 font-normal">市场</th>
              <th className="py-1 pr-3 font-normal">方向</th>
              <th className="py-1 pr-3 font-normal">开平</th>
              <th className="py-1 pr-3 font-normal">价格</th>
              <th className="py-1 pr-3 font-normal">数量</th>
              <th className="py-1 pr-3 font-normal">状态</th>
              <th className="py-1 pr-3 font-normal">订单号</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-200">
            {visibleItems.map(({ user, order }) => (
              <tr key={order.order_id} className="border-t border-white/8">
                <td className="py-1.5 pr-3">{bjTime(order.updated_at ?? order.created_at)}</td>
                <td className="py-1.5 pr-3 font-sans text-slate-100">{user.username}</td>
                <td className="py-1.5 pr-3">{order.product_type ?? "-"}</td>
                <td className="py-1.5 pr-3">{order.symbol}</td>
                <td className={`py-1.5 pr-3 ${order.side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{order.side}</td>
                <td className="py-1.5 pr-3">{order.position_action ?? "-"}{order.reduce_only ? " / RO" : ""}</td>
                <td className="py-1.5 pr-3">{fmt(order.price, 6)}</td>
                <td className="py-1.5 pr-3">{fmt(order.remaining_quantity, 6)} / {fmt(order.quantity, 6)}</td>
                <td className="py-1.5 pr-3">{order.status}</td>
                <td className="py-1.5 pr-3 text-slate-500">{order.client_order_id ?? order.order_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function AuditTradeTable({ items }: { items: AdminTradeAuditItem[] }) {
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(items.length / AUDIT_TABLE_PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visibleItems = items.slice((safePage - 1) * AUDIT_TABLE_PAGE_SIZE, safePage * AUDIT_TABLE_PAGE_SIZE);
  useEffect(() => {
    setPage(1);
  }, [items]);

  return (
    <>
      <AuditTablePager page={safePage} total={items.length} pageSize={AUDIT_TABLE_PAGE_SIZE} label="成交审计分页" onPageChange={setPage} />
      <div className="max-h-[520px] overflow-auto">
        <table className="min-w-[920px] text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-normal">时间</th>
              <th className="py-1 pr-3 font-normal">产品</th>
              <th className="py-1 pr-3 font-normal">市场</th>
              <th className="py-1 pr-3 font-normal">Taker</th>
              <th className="py-1 pr-3 font-normal">Maker</th>
              <th className="py-1 pr-3 font-normal">方向</th>
              <th className="py-1 pr-3 font-normal">价格</th>
              <th className="py-1 pr-3 font-normal">数量</th>
              <th className="py-1 pr-3 font-normal">名义</th>
              <th className="py-1 pr-3 font-normal">来源</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-200">
            {visibleItems.map(({ taker_user, maker_user, trade }) => (
              <tr key={`${trade.trade_id}-${taker_user.id}-${maker_user.id}`} className="border-t border-white/8">
                <td className="py-1.5 pr-3">{bjTime(trade.executed_at ?? trade.ts)}</td>
                <td className="py-1.5 pr-3">{trade.product_type ?? "-"}</td>
                <td className="py-1.5 pr-3">{trade.symbol}</td>
                <td className="py-1.5 pr-3 font-sans text-slate-100">{taker_user.username}</td>
                <td className="py-1.5 pr-3 font-sans text-slate-100">{maker_user.username}</td>
                <td className={`py-1.5 pr-3 ${(trade.side ?? trade.taker_side) === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{trade.side ?? trade.taker_side ?? "-"}</td>
                <td className="py-1.5 pr-3">{fmt(trade.price, 6)}</td>
                <td className="py-1.5 pr-3">{fmt(trade.quantity, 6)}</td>
                <td className="py-1.5 pr-3">{fmt(trade.quote_amount, 4)}</td>
                <td className="py-1.5 pr-3">{trade.source ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TradeActivityTable({ items }: { items: TradeItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[620px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">时间</th>
            <th className="py-1 pr-3 font-normal">市场</th>
            <th className="py-1 pr-3 font-normal">角色</th>
            <th className="py-1 pr-3 font-normal">方向</th>
            <th className="py-1 pr-3 font-normal">价格</th>
            <th className="py-1 pr-3 font-normal">数量</th>
            <th className="py-1 pr-3 font-normal">手续费</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={item.trade_id} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{bjTime(item.ts ?? item.executed_at)}</td>
              <td className="py-1.5 pr-3">{item.symbol}</td>
              <td className="py-1.5 pr-3">{item.liquidity_role ?? "-"}</td>
              <td className={`py-1.5 pr-3 ${item.side === "buy" ? "text-emerald-300" : "text-rose-300"}`}>{item.side ?? item.taker_side ?? "-"}</td>
              <td className="py-1.5 pr-3">{fmt(item.price, 6)}</td>
              <td className="py-1.5 pr-3">{fmt(item.quantity, 6)}</td>
              <td className="py-1.5 pr-3">{fmt(item.fee, 8)} {item.fee_asset ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LedgerActivityTable({ items }: { items: LedgerItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[700px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">时间</th>
            <th className="py-1 pr-3 font-normal">资产</th>
            <th className="py-1 pr-3 font-normal">类型</th>
            <th className="py-1 pr-3 font-normal">变动</th>
            <th className="py-1 pr-3 font-normal">可用</th>
            <th className="py-1 pr-3 font-normal">冻结</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={item.entry_id} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{bjTime(item.created_at)}</td>
              <td className="py-1.5 pr-3">{item.asset}</td>
              <td className="py-1.5 pr-3">{item.change_type}</td>
              <td className={`py-1.5 pr-3 ${Number(item.amount) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{fmt(item.amount, 8)}</td>
              <td className="py-1.5 pr-3">{fmt(item.available_before, 4)} → {fmt(item.available_after, 4)}</td>
              <td className="py-1.5 pr-3">{fmt(item.frozen_before, 4)} → {fmt(item.frozen_after, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SpotBalanceMiniTable({ items }: { items: AdminUser["balances"] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[460px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">资产</th>
            <th className="py-1 pr-3 font-normal">可用</th>
            <th className="py-1 pr-3 font-normal">冻结</th>
            <th className="py-1 pr-3 font-normal">合计</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => {
            const total = Number(item.available || 0) + Number(item.frozen || 0);
            return (
              <tr key={item.asset} className="border-t border-white/8">
                <td className="py-1.5 pr-3">{item.asset}</td>
                <td className="py-1.5 pr-3">{fmt(item.available, 8)}</td>
                <td className="py-1.5 pr-3">{fmt(item.frozen, 8)}</td>
                <td className="py-1.5 pr-3">{fmt(total, 8)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ContractAccountMiniTable({ items }: { items: ContractAccountAdminItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[680px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">保证金币种</th>
            <th className="py-1 pr-3 font-normal">保证金钱包</th>
            <th className="py-1 pr-3 font-normal">可用保证金</th>
            <th className="py-1 pr-3 font-normal">占用保证金</th>
            <th className="py-1 pr-3 font-normal">未实现PnL</th>
            <th className="py-1 pr-3 font-normal">更新时间</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={`${item.user.id}-${item.account.margin_asset}`} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{item.account.margin_asset}</td>
              <td className="py-1.5 pr-3">{fmt(item.account.wallet_balance, 4)}</td>
              <td className="py-1.5 pr-3">{fmt(item.account.available_margin, 4)}</td>
              <td className="py-1.5 pr-3">{fmt(item.account.used_margin, 4)}</td>
              <td className={`py-1.5 pr-3 ${Number(item.account.unrealized_pnl || 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{fmt(item.account.unrealized_pnl, 4)}</td>
              <td className="py-1.5 pr-3">{bjTime(item.account.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ContractPositionMiniTable({ items }: { items: ContractPositionAdminItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[760px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">市场</th>
            <th className="py-1 pr-3 font-normal">方向</th>
            <th className="py-1 pr-3 font-normal">数量</th>
            <th className="py-1 pr-3 font-normal">均价</th>
            <th className="py-1 pr-3 font-normal">标记价</th>
            <th className="py-1 pr-3 font-normal">强平价</th>
            <th className="py-1 pr-3 font-normal">风险</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={`${item.user.id}-${item.position.symbol}-${item.position.side}`} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{item.position.symbol}</td>
              <td className={`py-1.5 pr-3 ${item.position.side === "long" ? "text-emerald-300" : item.position.side === "short" ? "text-rose-300" : "text-slate-300"}`}>{item.position.side}</td>
              <td className="py-1.5 pr-3">{fmt(item.position.quantity, 6)}</td>
              <td className="py-1.5 pr-3">{fmt(item.position.entry_price, 2)}</td>
              <td className="py-1.5 pr-3">{fmt(item.position.mark_price, 2)}</td>
              <td className="py-1.5 pr-3">{fmt(item.position.liquidation_price, 2)}</td>
              <td className="py-1.5 pr-3">{item.position.risk_status ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ContractLedgerActivityTable({ items }: { items: ContractLedgerItem[] }) {
  return (
    <div className="max-h-56 overflow-auto">
      <table className="min-w-[820px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-3 font-normal">时间</th>
            <th className="py-1 pr-3 font-normal">市场</th>
            <th className="py-1 pr-3 font-normal">类型</th>
            <th className="py-1 pr-3 font-normal">变动</th>
            <th className="py-1 pr-3 font-normal">钱包</th>
            <th className="py-1 pr-3 font-normal">可用</th>
            <th className="py-1 pr-3 font-normal">备注</th>
          </tr>
        </thead>
        <tbody className="font-mono text-slate-200">
          {items.map((item) => (
            <tr key={item.entry_id} className="border-t border-white/8">
              <td className="py-1.5 pr-3">{bjTime(item.created_at)}</td>
              <td className="py-1.5 pr-3">{item.symbol ?? "-"}</td>
              <td className="py-1.5 pr-3">{item.change_type}</td>
              <td className={`py-1.5 pr-3 ${Number(item.amount || 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{fmt(item.amount, 8)} {item.margin_asset}</td>
              <td className="py-1.5 pr-3">{fmt(item.wallet_before, 4)} → {fmt(item.wallet_after, 4)}</td>
              <td className="py-1.5 pr-3">{fmt(item.available_before, 4)} → {fmt(item.available_after, 4)}</td>
              <td className="py-1.5 pr-3">{item.note ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
