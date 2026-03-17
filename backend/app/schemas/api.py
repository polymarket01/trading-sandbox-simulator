from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

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
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_precision: int
    qty_precision: int
    is_active: bool


class MarketListResponse(DecimalModel):
    items: list[MarketItem]


class OrderCreateRequest(DecimalModel):
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market", "market_protected"]
    tif: Literal["gtc", "ioc"]
    quantity: Decimal
    price: Decimal | None = None
    protection_bps: int | None = None
    client_order_id: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "OrderCreateRequest":
        if self.type == "limit" and self.price is None:
            raise ValueError("limit order requires price")
        if self.type in {"market", "market_protected"} and self.tif != "ioc":
            raise ValueError("market and market_protected must be ioc")
        if self.type == "market_protected" and self.protection_bps is None:
            raise ValueError("market_protected requires protection_bps")
        return self


class OrderAmendRequest(DecimalModel):
    quantity: Decimal
    price: Decimal | None = None


class CancelAllRequest(BaseModel):
    symbol: str


class AdjustBalanceRequest(DecimalModel):
    asset: str
    amount: Decimal
    reason: str


class UpdateMarketRequest(DecimalModel):
    is_active: bool | None = None
    price_tick: Decimal | None = None
    qty_step: Decimal | None = None
    min_qty: Decimal | None = None
    min_notional: Decimal | None = None
    price_precision: int | None = None
    qty_precision: int | None = None


class UpdateMarketFeesRequest(DecimalModel):
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal


class UpdateUserFeesRequest(DecimalModel):
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
