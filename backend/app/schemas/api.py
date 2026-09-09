from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.decimal_utils import decimal_to_str
from app.core.time_utils import to_millis


class DecimalModel(BaseModel):
    model_config = ConfigDict(
        json_encoders={
            Decimal: decimal_to_str,
            datetime: to_millis,
        }
    )


class MarketItem(DecimalModel):
    symbol: str
    product_type: Literal["SPOT", "PERP"] = "SPOT"
    market_type: Literal["mainstream", "listed"]
    base_asset: str
    quote_asset: str
    margin_asset: str | None = None
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_leverage: Decimal = Decimal("1")
    default_leverage: Decimal = Decimal("1")
    maintenance_margin_rate: Decimal = Decimal("0")
    funding_rate: Decimal = Decimal("0")
    funding_interval_hours: int = 8
    index_price_source: str = "binance"
    mark_price_mode: str = "orderbook"
    funding_rate_mode: str = "binance"
    funding_interest_rate: Decimal = Decimal("0.0001")
    funding_clamp_rate: Decimal = Decimal("0.0005")
    funding_cap_rate: Decimal = Decimal("0.02")
    funding_impact_notional: Decimal = Decimal("25000")
    contract_trading_mode: Literal["normal", "reduce_only", "paused"] = "normal"
    reference_price: Decimal | None = None
    price_precision: int
    qty_precision: int
    is_active: bool


class MarketListResponse(DecimalModel):
    items: list[MarketItem]
    persistence: dict[str, Any] | None = None


class OrderCreateRequest(DecimalModel):
    # Additive causal id.  Existing clients may omit it; the API generates a
    # UUID and returns it in the causal receipt.
    command_id: str | None = Field(default=None, min_length=8, max_length=128)
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market", "market_protected"]
    tif: Literal["gtc", "ioc", "post_only"]
    quantity: Decimal
    price: Decimal | None = None
    protection_bps: int | None = None
    client_order_id: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "OrderCreateRequest":
        if self.type == "limit" and self.price is None:
            raise ValueError("limit order requires price")
        if self.tif == "post_only" and self.type != "limit":
            raise ValueError("post_only requires a limit order")
        if self.type in {"market", "market_protected"} and self.tif != "ioc":
            raise ValueError("market and market_protected must be ioc")
        if self.type == "market_protected" and self.protection_bps is None:
            raise ValueError("market_protected requires protection_bps")
        return self


class ContractOrderCreateRequest(DecimalModel):
    command_id: str | None = Field(default=None, min_length=8, max_length=128)
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market"]
    tif: Literal["gtc", "ioc", "post_only"]
    quantity: Decimal
    price: Decimal | None = None
    position_action: Literal["open", "close"] = "open"
    reduce_only: bool = False
    # PaperTrading accepts a per-order leverage override.  Keeping it
    # optional preserves the existing account-setting fallback for bots and
    # clients that do not send the field.
    leverage: Decimal | None = Field(default=None, gt=0)
    client_order_id: str | None = None

    @model_validator(mode="after")
    def validate_contract_order(self) -> "ContractOrderCreateRequest":
        if self.type == "limit" and self.price is None:
            raise ValueError("limit order requires price")
        if self.tif == "post_only" and self.type != "limit":
            raise ValueError("post_only requires a limit order")
        if self.type == "market" and self.tif != "ioc":
            raise ValueError("market orders must be ioc")
        if self.position_action == "close" or self.reduce_only:
            self.position_action = "close"
            self.reduce_only = True
        return self


class ContractLeverageUpdateRequest(DecimalModel):
    leverage: Decimal
    margin_mode: Literal["isolated", "cross"] = "isolated"
    position_mode: Literal["one_way", "hedge"] | None = None

    @model_validator(mode="after")
    def validate_leverage(self) -> "ContractLeverageUpdateRequest":
        if self.leverage <= Decimal("0"):
            raise ValueError("leverage must be positive")
        if self.margin_mode != "isolated":
            raise ValueError("only isolated margin is enabled in this MVP")
        return self


class ContractAccountAdjustRequest(DecimalModel):
    user_id: int
    margin_asset: str = "USDT"
    amount: Decimal
    reason: str = "admin contract wallet adjustment"
    confirm_execute: bool = False

    @model_validator(mode="after")
    def validate_amount(self) -> "ContractAccountAdjustRequest":
        if self.amount == Decimal("0"):
            raise ValueError("amount cannot be zero")
        self.margin_asset = self.margin_asset.upper().strip() or "USDT"
        self.reason = self.reason.strip() or "admin contract wallet adjustment"
        return self


class ContractInsuranceFundAdjustRequest(DecimalModel):
    margin_asset: str = "USDT"
    amount: Decimal
    reason: str = "admin contract insurance fund adjustment"
    confirm_execute: bool = False

    @model_validator(mode="after")
    def validate_amount(self) -> "ContractInsuranceFundAdjustRequest":
        if self.amount == Decimal("0"):
            raise ValueError("amount cannot be zero")
        self.margin_asset = self.margin_asset.upper().strip() or "USDT"
        self.reason = self.reason.strip() or "admin contract insurance fund adjustment"
        return self


class ContractAdlExecuteRequest(BaseModel):
    max_candidates: int = 20
    confirm_execute: bool = False

    @model_validator(mode="after")
    def validate_limit(self) -> "ContractAdlExecuteRequest":
        if self.max_candidates < 1 or self.max_candidates > 100:
            raise ValueError("max_candidates must be between 1 and 100")
        return self


class ContractRiskLimitTierRequest(DecimalModel):
    tier: int
    notional_floor: Decimal
    notional_cap: Decimal | None = None
    max_leverage: Decimal
    maintenance_margin_rate: Decimal
    maintenance_amount: Decimal = Decimal("0")

    @model_validator(mode="after")
    def validate_tier(self) -> "ContractRiskLimitTierRequest":
        if self.tier < 1:
            raise ValueError("tier must be positive")
        if self.notional_floor < Decimal("0"):
            raise ValueError("notional_floor cannot be negative")
        if self.notional_cap is not None and self.notional_cap <= self.notional_floor:
            raise ValueError("notional_cap must be greater than notional_floor")
        if self.max_leverage <= Decimal("0"):
            raise ValueError("max_leverage must be positive")
        if self.maintenance_margin_rate < Decimal("0"):
            raise ValueError("maintenance_margin_rate cannot be negative")
        if self.maintenance_amount < Decimal("0"):
            raise ValueError("maintenance_amount cannot be negative")
        return self


class ContractRiskLimitTierUpdateRequest(DecimalModel):
    tiers: list[ContractRiskLimitTierRequest]
    confirm_execute: bool = False

    @model_validator(mode="after")
    def validate_tiers(self) -> "ContractRiskLimitTierUpdateRequest":
        if not self.tiers:
            raise ValueError("tiers is required")
        ordered = sorted(self.tiers, key=lambda item: item.tier)
        expected = 1
        previous_cap: Decimal | None = None
        for item in ordered:
            if item.tier != expected:
                raise ValueError("tiers must start at 1 and be contiguous")
            if expected == 1 and item.notional_floor != Decimal("0"):
                raise ValueError("tier 1 notional_floor must be 0")
            if previous_cap is not None and item.notional_floor != previous_cap:
                raise ValueError("tier notional ranges must be contiguous")
            previous_cap = item.notional_cap
            expected += 1
        if ordered[-1].notional_cap is not None:
            raise ValueError("last tier notional_cap must be empty")
        self.tiers = ordered
        return self


class OrderAmendRequest(DecimalModel):
    quantity: Decimal
    price: Decimal | None = None


class OrderBatchAmendItem(DecimalModel):
    order_id: str
    quantity: Decimal
    price: Decimal | None = None


class OrderBatchAmendRequest(BaseModel):
    symbol: str
    orders: list[OrderBatchAmendItem]

    @model_validator(mode="after")
    def validate_shape(self) -> "OrderBatchAmendRequest":
        if not self.orders:
            raise ValueError("orders is required")
        if len(self.orders) > 200:
            raise ValueError("orders length cannot exceed 200")
        order_ids = [item.order_id for item in self.orders]
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("duplicate order_id in batch")
        return self


class OrderBatchCreateRequest(BaseModel):
    orders: list[OrderCreateRequest]

    @model_validator(mode="after")
    def validate_shape(self) -> "OrderBatchCreateRequest":
        if not self.orders:
            raise ValueError("orders is required")
        if len(self.orders) > 200:
            raise ValueError("orders length cannot exceed 200")
        return self


class ContractOrderAmendRequest(DecimalModel):
    quantity: Decimal
    price: Decimal | None = None


class ContractOrderBatchAmendItem(DecimalModel):
    order_id: str
    quantity: Decimal
    price: Decimal | None = None


class ContractOrderBatchAmendRequest(BaseModel):
    symbol: str
    orders: list[ContractOrderBatchAmendItem]

    @model_validator(mode="after")
    def validate_shape(self) -> "ContractOrderBatchAmendRequest":
        if not self.orders:
            raise ValueError("orders is required")
        if len(self.orders) > 200:
            raise ValueError("orders length cannot exceed 200")
        order_ids = [item.order_id for item in self.orders]
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("duplicate order_id in batch")
        return self


class ContractOrderBatchCreateRequest(BaseModel):
    orders: list[ContractOrderCreateRequest]

    @model_validator(mode="after")
    def validate_shape(self) -> "ContractOrderBatchCreateRequest":
        if not self.orders:
            raise ValueError("orders is required")
        if len(self.orders) > 200:
            raise ValueError("orders length cannot exceed 200")
        return self


class CancelAllRequest(BaseModel):
    symbol: str


class CancelBatchRequest(BaseModel):
    symbol: str
    order_ids: list[str] | None = None
    client_order_id_prefix: str | None = None
    max_orders: int = 50

    @model_validator(mode="after")
    def validate_shape(self) -> "CancelBatchRequest":
        prefix = (self.client_order_id_prefix or "").strip()
        ids = self.order_ids or []
        if not ids and not prefix:
            raise ValueError("order_ids or client_order_id_prefix is required")
        if self.max_orders < 1 or self.max_orders > 50:
            raise ValueError("max_orders must be between 1 and 50")
        if len(ids) > self.max_orders:
            raise ValueError("order_ids length cannot exceed max_orders")
        if prefix and len(prefix) > 128:
            raise ValueError("client_order_id_prefix is too long")
        self.client_order_id_prefix = prefix or None
        return self


class AdjustBalanceRequest(DecimalModel):
    asset: str
    amount: Decimal
    reason: str


class UpdateMarketRequest(DecimalModel):
    is_active: bool | None = None
    market_type: Literal["mainstream", "listed"] | None = None
    product_type: Literal["SPOT", "PERP"] | None = None
    margin_asset: str | None = None
    price_tick: Decimal | None = None
    qty_step: Decimal | None = None
    min_qty: Decimal | None = None
    min_notional: Decimal | None = None
    max_leverage: Decimal | None = None
    default_leverage: Decimal | None = None
    maintenance_margin_rate: Decimal | None = None
    funding_rate: Decimal | None = None
    funding_interval_hours: int | None = None
    index_price_source: str | None = None
    mark_price_mode: str | None = None
    funding_rate_mode: str | None = None
    funding_interest_rate: Decimal | None = None
    funding_clamp_rate: Decimal | None = None
    funding_cap_rate: Decimal | None = None
    funding_impact_notional: Decimal | None = None
    contract_trading_mode: Literal["normal", "reduce_only", "paused"] | None = None
    reference_price: Decimal | None = None
    price_precision: int | None = None
    qty_precision: int | None = None


class UpdateMarketFeesRequest(DecimalModel):
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal


class UpdateUserFeesRequest(DecimalModel):
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal


class UserUpdateRequest(BaseModel):
    role: Literal["manual_user", "mm_bot", "admin"] | None = None
    is_active: bool | None = None
    password: str | None = None


class MarketCreateRequest(DecimalModel):
    symbol: str
    default_maker_strategy: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    price_source_symbol: str | None = Field(default=None, pattern=r'^[\p{L}\p{N}]{2,30}$')
    product_type: Literal["SPOT", "PERP"] = "SPOT"
    market_type: Literal["mainstream", "listed"] = "listed"
    base_asset: str
    quote_asset: str = "USDT"
    margin_asset: str | None = None
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_leverage: Decimal = Decimal("1")
    default_leverage: Decimal = Decimal("1")
    maintenance_margin_rate: Decimal = Decimal("0")
    funding_rate: Decimal = Decimal("0")
    funding_interval_hours: int = 8
    index_price_source: str = "binance"
    mark_price_mode: str = "orderbook"
    funding_rate_mode: str = "binance"
    funding_interest_rate: Decimal = Decimal("0.0001")
    funding_clamp_rate: Decimal = Decimal("0.0005")
    funding_cap_rate: Decimal = Decimal("0.02")
    funding_impact_notional: Decimal = Decimal("25000")
    contract_trading_mode: Literal["normal", "reduce_only", "paused"] = "normal"
    price_precision: int | None = None
    qty_precision: int | None = None
    default_maker_fee_rate: Decimal = Decimal("0")
    default_taker_fee_rate: Decimal = Decimal("0")
    reference_price: Decimal | None = None
    create_default_bots: bool = False
    default_bot_count: int = 2
    default_bot_initial_quote_amount: Decimal = Decimal("1000000000")
    default_bot_initial_base_notional: Decimal = Decimal("1000000000")
    initial_base_balance: Decimal = Decimal("100000000")
    initial_quote_balance: Decimal = Decimal("100000000")
    is_active: bool = True

    @model_validator(mode="after")
    def validate_numbers(self) -> "MarketCreateRequest":
        for field_name in ("price_tick", "qty_step", "min_qty", "min_notional"):
            if getattr(self, field_name) <= Decimal("0"):
                raise ValueError(f"{field_name} must be positive")
        if self.max_leverage <= Decimal("0") or self.default_leverage <= Decimal("0"):
            raise ValueError("leverage must be positive")
        if self.default_leverage > self.max_leverage:
            raise ValueError("default_leverage cannot exceed max_leverage")
        self.index_price_source = self.index_price_source.lower().strip()
        self.mark_price_mode = self.mark_price_mode.lower().strip()
        self.funding_rate_mode = self.funding_rate_mode.lower().strip()
        if self.index_price_source not in {"binance", "manual"}:
            raise ValueError("index_price_source must be binance or manual")
        if self.mark_price_mode not in {"orderbook"}:
            raise ValueError("mark_price_mode must be orderbook")
        if self.funding_rate_mode not in {"binance", "formula"}:
            raise ValueError("funding_rate_mode must be binance or formula")
        if self.maintenance_margin_rate < Decimal("0") or self.funding_interval_hours <= 0:
            raise ValueError("invalid contract risk parameters")
        if self.funding_clamp_rate < Decimal("0") or self.funding_cap_rate <= Decimal("0") or self.funding_impact_notional <= Decimal("0"):
            raise ValueError("invalid funding parameters")
        if self.product_type == "PERP":
            self.margin_asset = (self.margin_asset or self.quote_asset).upper().strip()
            self.create_default_bots = False
        else:
            self.margin_asset = None
        if self.reference_price is not None and self.reference_price <= Decimal("0"):
            raise ValueError("reference_price must be positive")
        if self.create_default_bots and self.reference_price is None:
            raise ValueError("reference_price is required when create_default_bots is true")
        if self.default_bot_count < 0 or self.default_bot_count > 20:
            raise ValueError("default_bot_count must be between 0 and 20")
        if self.default_bot_initial_quote_amount < Decimal("0") or self.default_bot_initial_base_notional < Decimal("0"):
            raise ValueError("default bot initial amounts cannot be negative")
        if self.initial_base_balance < Decimal("0") or self.initial_quote_balance < Decimal("0"):
            raise ValueError("initial balances cannot be negative")
        if self.price_precision is not None and self.price_precision < 0:
            raise ValueError("price_precision cannot be negative")
        if self.qty_precision is not None and self.qty_precision < 0:
            raise ValueError("qty_precision cannot be negative")
        return self


class MarketBotCreateRequest(DecimalModel):
    uid: int | None = None
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    bot_label: str | None = None
    role: Literal["maker", "flow", "hedge"] = "maker"
    strategy_role: str | None = "maker"
    initial_quote_amount: Decimal = Decimal("1000000000")
    initial_base_notional: Decimal = Decimal("1000000000")
    initial_base_amount: Decimal | None = None
    reference_price: Decimal | None = None
    is_enabled: bool = True

    @model_validator(mode="after")
    def validate_bot(self) -> "MarketBotCreateRequest":
        if self.uid is not None and self.uid <= 0:
            raise ValueError("uid must be positive")
        if self.initial_quote_amount < Decimal("0"):
            raise ValueError("initial_quote_amount cannot be negative")
        if self.initial_base_notional < Decimal("0"):
            raise ValueError("initial_base_notional cannot be negative")
        if self.initial_base_amount is not None and self.initial_base_amount < Decimal("0"):
            raise ValueError("initial_base_amount cannot be negative")
        if self.reference_price is not None and self.reference_price <= Decimal("0"):
            raise ValueError("reference_price must be positive")
        if self.initial_base_amount is None and self.reference_price is None:
            raise ValueError("reference_price is required when initial_base_amount is not provided")
        return self


class MarketBotUpdateRequest(DecimalModel):
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    bot_label: str | None = None
    role: Literal["maker", "flow", "hedge"] | None = None
    strategy_role: str | None = None
    initial_quote_amount: Decimal | None = None
    initial_base_notional: Decimal | None = None
    initial_base_amount: Decimal | None = None
    reference_price: Decimal | None = None
    is_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "MarketBotUpdateRequest":
        for field_name in ("initial_quote_amount", "initial_base_notional", "initial_base_amount"):
            value = getattr(self, field_name)
            if value is not None and value < Decimal("0"):
                raise ValueError(f"{field_name} cannot be negative")
        if self.reference_price is not None and self.reference_price <= Decimal("0"):
            raise ValueError("reference_price must be positive")
        return self


class MarketBotDefaultsRequest(DecimalModel):
    bot_count: int = 2
    initial_quote_amount: Decimal = Decimal("1000000000")
    initial_base_notional: Decimal = Decimal("1000000000")
    reference_price: Decimal | None = None

    @model_validator(mode="after")
    def validate_defaults(self) -> "MarketBotDefaultsRequest":
        if self.bot_count < 1 or self.bot_count > 20:
            raise ValueError("bot_count must be between 1 and 20")
        if self.initial_quote_amount < Decimal("0") or self.initial_base_notional < Decimal("0"):
            raise ValueError("initial amounts cannot be negative")
        if self.reference_price is not None and self.reference_price <= Decimal("0"):
            raise ValueError("reference_price must be positive")
        return self


class MarketBotFlowDefaultRequest(DecimalModel):
    initial_quote_amount: Decimal = Decimal("1000000000")
    initial_base_notional: Decimal = Decimal("1000000000")
    reference_price: Decimal | None = None
    is_enabled: bool = True

    @model_validator(mode="after")
    def validate_flow_default(self) -> "MarketBotFlowDefaultRequest":
        if self.initial_quote_amount < Decimal("0") or self.initial_base_notional < Decimal("0"):
            raise ValueError("initial amounts cannot be negative")
        if self.reference_price is not None and self.reference_price <= Decimal("0"):
            raise ValueError("reference_price must be positive")
        return self


class ConfirmExecuteRequest(BaseModel):
    confirm_execute: bool = False


class AdminOperationFailureRequest(BaseModel):
    domain: str = "system"
    operation_type: str = "frontend_operation_failure"
    target_type: str = "operation"
    target_id: str | int | None = None
    target_symbol: str | None = None
    summary: str
    error: str
    context: dict[str, Any] = Field(default_factory=dict)


class SeedMarketBookRequest(DecimalModel):
    mid_price: Decimal
    levels: int = 12
    gap_ticks: int = 1
    quantity: Decimal | None = None
    cancel_existing: bool = True
    dry_run: bool = False
    confirm_execute: bool = False

    @model_validator(mode="after")
    def validate_seed_book(self) -> "SeedMarketBookRequest":
        if self.mid_price <= Decimal("0"):
            raise ValueError("mid_price must be positive")
        if self.levels < 1 or self.levels > 50:
            raise ValueError("levels must be between 1 and 50")
        if self.gap_ticks < 1 or self.gap_ticks > 100:
            raise ValueError("gap_ticks must be between 1 and 100")
        if self.quantity is not None and self.quantity <= Decimal("0"):
            raise ValueError("quantity must be positive")
        return self


class MarketSweepPreviewRequest(DecimalModel):
    side: Literal["buy", "sell"]
    quantity: Decimal | None = None
    quote_amount: Decimal | None = None
    depth: int = 50

    @model_validator(mode="after")
    def validate_sweep_preview(self) -> "MarketSweepPreviewRequest":
        has_quantity = self.quantity is not None
        has_quote_amount = self.quote_amount is not None
        if has_quantity == has_quote_amount:
            raise ValueError("provide exactly one of quantity or quote_amount")
        if self.quantity is not None and self.quantity <= Decimal("0"):
            raise ValueError("quantity must be positive")
        if self.quote_amount is not None and self.quote_amount <= Decimal("0"):
            raise ValueError("quote_amount must be positive")
        if self.depth < 1 or self.depth > 50:
            raise ValueError("depth must be between 1 and 50")
        return self


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = True


class RegisterRequest(BaseModel):
    username: str
    password: str
    confirm_password: str

    @model_validator(mode="after")
    def validate_registration(self) -> "RegisterRequest":
        import re

        if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", self.username.strip()):
            raise ValueError("用户名需为 3～24 位字母、数字或下划线")
        if not self.password:
            raise ValueError("密码不能为空")
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致")
        return self


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

    @model_validator(mode="after")
    def validate_passwords(self) -> "PasswordChangeRequest":
        if not self.current_password:
            raise ValueError("current_password is required")
        if len(self.new_password) < 8:
            raise ValueError("new_password must be at least 8 characters")
        if self.current_password == self.new_password:
            raise ValueError("new_password must be different from current_password")
        return self


class UserCreateRequest(DecimalModel):
    username: str
    password: str
    role: Literal["manual_user", "mm_bot", "admin"] = "manual_user"
    is_active: bool = True
    initial_balances: dict[str, Decimal] | None = None
    maker_fee_rate: Decimal | None = None
    taker_fee_rate: Decimal | None = None
