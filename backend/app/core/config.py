from __future__ import annotations

from decimal import Decimal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.constants import DEFAULT_PAPER_ACCOUNT_USDT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    instance_profile_path: str = ""
    public_base_path: str = ""
    public_origin: str = ""
    external_bot_api_enabled: bool = False
    app_name: str = "Spot Market Sandbox"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://sandbox:sandbox@db:5432/sandbox"
    # SQLite is a single-writer sandbox. Public orderbook reads use the
    # in-memory published snapshot, so a longer busy timeout only makes
    # financial writes wait for the write lock instead of failing with 500.
    sqlite_busy_timeout_ms: int = 15_000
    # sampled: runtime/order-book state is memory authoritative and only
    # low-frequency market history is written to market_history.db.
    # strict_durable (strict is a compatibility alias): full event sourcing.
    # memory remains a legacy compatibility mode and is not the new contract.
    persistence_mode: str = Field(
        default="sampled",
        validation_alias=AliasChoices("SANDBOX_PERSISTENCE_MODE", "PERSISTENCE_MODE"),
    )
    # Independent PaperTrading product contract.  It is intentionally not an
    # alias for sampled/memory: user facts are durable and restart-recoverable.
    paper_exchange_default_spot_usdt: Decimal = Field(
        default=DEFAULT_PAPER_ACCOUNT_USDT,
        validation_alias=AliasChoices("PAPER_EXCHANGE_DEFAULT_SPOT_USDT"),
    )
    paper_exchange_default_perp_usdt: Decimal = Field(
        default=DEFAULT_PAPER_ACCOUNT_USDT,
        validation_alias=AliasChoices("PAPER_EXCHANGE_DEFAULT_PERP_USDT"),
    )
    paper_exchange_session_ttl_seconds: int = Field(
        default=7 * 86400,
        validation_alias=AliasChoices("PAPER_EXCHANGE_SESSION_TTL_SECONDS"),
    )
    paper_exchange_cookie_name: str = Field(
        default="paper_session",
        validation_alias=AliasChoices("PAPER_EXCHANGE_COOKIE_NAME"),
    )
    paper_exchange_cookie_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("PAPER_EXCHANGE_COOKIE_SECURE"),
    )
    paper_exchange_register_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PAPER_EXCHANGE_REGISTER_ENABLED"),
    )
    paper_exchange_market_seed_count: int = Field(
        default=2,
        validation_alias=AliasChoices("PAPER_EXCHANGE_MARKET_SEED_COUNT"),
    )
    # Restart-only switch for FLOW execution.  In ``memory`` mode, ``False``
    # routes an authorized bot-only limit IOC through a display-only preview:
    # it never mutates orders, balances, positions or durable history.  Any
    # real engine fill, ambiguity, or customer participant remains durable.
    # ``strict`` always uses the real durable execution path.
    robot_flow_persistence_enabled: bool = False
    redis_url: str = "redis://redis:6379/0"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    default_ws_signature_ttl_ms: int = 60_000
    depth_levels: int = 20
    stats_push_interval_ms: int = 500
    recent_trade_limit: int = 200
    private_order_rate_limit_enabled: bool = True
    order_submit_rate_per_second: float = 30
    order_submit_burst: int = 60
    order_submit_batch_rate_per_second: float = 1
    order_submit_batch_burst: int = 2
    # LITE 随机老化后允许一次携带全部到期档位（演示 100 档盘口 50 买 + 50 卖以内）。
    order_submit_batch_max_orders: int = 200
    order_cancel_rate_per_second: float = 40
    order_cancel_burst: int = 80
    order_amend_rate_per_second: float = 30
    order_amend_burst: int = 60
    order_cancel_all_rate_per_second: float = 5
    order_cancel_all_burst: int = 10
    max_open_orders_per_user_market: int = 200
    max_open_orders_per_user_total: int = 1000
    contract_maintenance_enabled: bool = True
    contract_maintenance_interval_seconds: int = 30
    contract_maintenance_startup_delay_seconds: int = 30
    contract_price_fetch_external: bool = True
    contract_auto_funding_enabled: bool = True
    contract_auto_liquidation_enabled: bool = True
    contract_risk_watch_distance_pct: float = 0.10
    contract_risk_warning_distance_pct: float = 0.03
    contract_liquidity_enabled: bool = False
    contract_fast_path_reconcile_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CONTRACT_FAST_PATH_RECONCILE_ENABLED"),
    )
    contract_liquidity_interval_seconds: int = 60
    contract_liquidity_levels: int = 12
    contract_liquidity_gap_ticks: int = 1
    contract_liquidity_quantity: Decimal | None = None
    history_retention_enabled: bool = True
    history_retention_startup_delay_seconds: int = 120
    history_retention_sqlite_only: bool = True
    history_retention_interval_seconds: int = 300
    history_retention_order_keep_per_market: int = 50_000
    history_retention_trade_keep_per_market: int = 20_000
    history_retention_kline_keep_per_market_interval: int = 5_000
    history_retention_ledger_keep_per_user: int = 10_000
    history_retention_contract_ledger_keep_per_user: int = 10_000
    history_retention_batch_size: int = 50_000
    # Financial facts are immutable by default. Robot order/trade/ledger pruning
    # may only be re-enabled after a checkpoint has been generated and verified.
    history_retention_financial_pruning_enabled: bool = False
    # Sandbox SQLite size guard: when the local DB file grows past
    # sandbox_db_guard_max_bytes, applied event/journal history and old snapshot
    # metadata are pruned and the file is VACUUMed back toward
    # sandbox_db_guard_keep_bytes (demo sandbox only).
    sandbox_db_guard_enabled: bool = True
    sandbox_db_guard_max_bytes: int = 256 * 1024 * 1024
    sqlite_control_hard_limit_bytes: int = 1536 * 1024 * 1024
    sandbox_db_guard_keep_bytes: int = 100 * 1024 * 1024
    sandbox_db_guard_interval_seconds: int = 300
    sandbox_db_guard_startup_delay_seconds: int = 120
    sandbox_db_guard_keep_events: int = 5_000
    sandbox_db_guard_keep_snapshots: int = 50
    accounting_evidence_auto_enabled: bool = True
    accounting_evidence_interval_seconds: int = 300
    accounting_evidence_startup_delay_seconds: int = 120
    financial_outbox_startup_delay_seconds: int = 5
    accounting_evidence_stale_after_seconds: int = 900
    kline_persist_min_interval_ms: int = 3000
    display_kline_persistence_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("DISPLAY_KLINE_PERSISTENCE_ENABLED"),
    )
    # Sampled history is intentionally independent from the control DB.  The
    # runner supplies absolute paths in isolated verification and deployments
    # may override these with SANDBOX_* variables.
    sandbox_data_dir: str = Field(default="backend/data", validation_alias=AliasChoices("SANDBOX_DATA_DIR"))
    history_db_path: str = Field(
        default="backend/data/market_history.db",
        validation_alias=AliasChoices("HISTORY_DB_PATH", "SANDBOX_HISTORY_DB_PATH"),
    )
    sandbox_run_id: str | None = Field(default=None, validation_alias=AliasChoices("SANDBOX_RUN_ID"))
    history_sampler_enabled: bool = True
    history_sample_interval_seconds: float = 1.0
    history_unclosed_upsert_seconds: int = 15
    history_commit_interval_seconds: int = 10
    history_batch_size: int = 256
    history_queue_max: int = 4096
    history_1h_enabled: bool = True
    history_1m_keep_per_market: int = 10_080
    history_5m_keep_per_market: int = 8_640
    history_1h_keep_per_market: int = 4_320
    history_error_keep_per_category: int = 200
    history_minute_summary_keep: int = 10_080
    history_minute_summary_max_age_days: int = 30
    history_db_soft_limit_bytes: int = 192 * 1024 * 1024
    history_db_hard_limit_bytes: int = 256 * 1024 * 1024
    history_db_wal_limit_bytes: int = 64 * 1024 * 1024
    sandbox_data_max_bytes: int = 512 * 1024 * 1024
    history_storage_guard_interval_seconds: int = 60
    history_storage_startup_delay_seconds: int = 10
    history_rotation_keep_archives: int = 3
    exchange_snapshot_keep: int = 10
    exchange_snapshot_max_bytes: int = 50 * 1024 * 1024
    service_log_max_bytes: int = 10 * 1024 * 1024
    service_log_backup_count: int = 5
    exchange_durability_mode: str = "sandbox_fast"
    # legacy keeps the compatibility API path; unified uses the same Python
    # services behind a durable causal journal; shadow records/comparisons but
    # never executes a second order or settlement.
    core_mode: str = Field(
        default="legacy",
        validation_alias=AliasChoices("SANDBOX_CORE_MODE"),
    )
    sequencer_queue_max: int = Field(
        default=4096,
        validation_alias=AliasChoices("SANDBOX_SEQUENCER_QUEUE_MAX"),
    )
    causal_command_timeout_seconds: float = Field(
        default=15.0,
        validation_alias=AliasChoices("SANDBOX_CAUSAL_COMMAND_TIMEOUT_SECONDS"),
    )
    exchange_sequence_start: int = 1000
    exchange_command_queue_max: int = 10_000
    exchange_ack_cache_max_size: int = 2_000
    exchange_ack_cache_ttl_seconds: float = 120.0
    exchange_ack_failure_sample_limit: int = 8
    exchange_snapshot_dir: str = Field(
        default=".runtime/snapshots",
        validation_alias=AliasChoices("EXCHANGE_SNAPSHOT_DIR"),
    )
    exchange_snapshot_interval_seconds: float = 30.0
    exchange_snapshot_event_count: int = 1000
    exchange_materializer_critical_retry_seconds: float = 0.25
    # Final matching-engine STP.  Same-account protection is always active;
    # cross-bot interaction is an explicit configuration choice.
    stp_same_account_mode: str = Field(
        default="cancel_taker",
        validation_alias=AliasChoices("STP_SAME_ACCOUNT_MODE"),
    )
    stp_bot_cross_mode: str = Field(
        # Different bot accounts are allowed to provide liquidity to one
        # another by default; deployments that want bot-only isolation set
        # STP_BOT_CROSS_MODE=cancel_taker (or another final STP action).
        default="allow",
        validation_alias=AliasChoices("STP_BOT_CROSS_MODE"),
    )
    # Shared quote/publication cadence. Product risk checks may be slower, but
    # these are the default book-update budgets exposed to both Spot and Perp.
    quote_reference_stale_ms: int = Field(
        default=1_000,
        validation_alias=AliasChoices("QUOTE_REFERENCE_STALE_MS"),
    )
    quote_head_interval_ms: int = Field(
        default=50,
        validation_alias=AliasChoices("QUOTE_HEAD_INTERVAL_MS"),
    )
    quote_near_interval_ms: int = Field(
        default=100,
        validation_alias=AliasChoices("QUOTE_NEAR_INTERVAL_MS"),
    )
    quote_full_interval_ms: int = Field(
        default=500,
        validation_alias=AliasChoices("QUOTE_FULL_INTERVAL_MS"),
    )
    quote_aging_interval_ms: int = Field(
        default=2_000,
        validation_alias=AliasChoices("QUOTE_AGING_INTERVAL_MS"),
    )
    quote_ws_delta_flush_ms: int = Field(
        default=20,
        validation_alias=AliasChoices("QUOTE_WS_DELTA_FLUSH_MS"),
    )
    quote_snapshot_heartbeat_ms: int = Field(
        default=2_000,
        validation_alias=AliasChoices("QUOTE_SNAPSHOT_HEARTBEAT_MS"),
    )


settings = Settings()
