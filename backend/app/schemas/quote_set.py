from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from app.schemas.api import DecimalModel


class QuoteLevel(DecimalModel):
    """One deterministic level in a maker quote plan.

    ``client_order_id`` is intentionally stable across generations.  A new
    generation changes the price/quantity of that level, not its identity, so
    the exchange core can amend in place and preserve no-op levels.
    """

    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    client_order_id: str | None = Field(default=None, min_length=1, max_length=64)
    tag: str | None = Field(default=None, min_length=1, max_length=64)
    position_action: str = "open"
    reduce_only: bool = False
    leverage: Decimal | None = Field(default=None, gt=0)


class QuoteSetReplaceRequest(DecimalModel):
    """High-level, latest-wins replacement of one maker's whole book."""

    command_id: str = Field(min_length=8, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    strategy_instance: str = Field(min_length=1, max_length=96)
    generation: int = Field(ge=0)
    fair_price: Decimal = Field(gt=0)
    bids: list[QuoteLevel] = Field(default_factory=list, max_length=100)
    asks: list[QuoteLevel] = Field(default_factory=list, max_length=100)
    logical_timestamp: int | datetime | None = None
    config_version: str = Field(default="default", min_length=1, max_length=128)
    config_hash: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_quote_set(self) -> "QuoteSetReplaceRequest":
        self.symbol = self.symbol.upper().strip()
        self.strategy_instance = self.strategy_instance.strip()
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.strategy_instance:
            raise ValueError("strategy_instance is required")
        for side, levels in (("bids", self.bids), ("asks", self.asks)):
            seen: set[str] = set()
            for index, level in enumerate(levels, start=1):
                identity = level.client_order_id or level.tag or f"{side}-{index:03d}"
                if identity in seen:
                    raise ValueError(f"duplicate quote level identity in {side}: {identity}")
                seen.add(identity)
                if level.position_action not in {"open", "close"}:
                    raise ValueError("position_action must be open or close")
                if level.position_action == "close":
                    level.reduce_only = True
        if self.bids and self.asks:
            if max(item.price for item in self.bids) >= min(item.price for item in self.asks):
                raise ValueError("quote set must not cross itself")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# Short aliases make the internal command name explicit without forcing API
# callers to depend on the implementation class name.
QuoteSetRequest = QuoteSetReplaceRequest
QuoteSetCommandSchema = QuoteSetReplaceRequest
