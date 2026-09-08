"""Immutable Python contracts for the exchange command causal chain.

The legacy sandbox has several compatible representations of a command and of
an execution result.  This module is deliberately dependency-light so the
contracts can be used by the API, the ExchangeCore, the persistence writer and
offline replay tests without importing SQLAlchemy or a product service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping


ACK_STAGES = (
    "RECEIVED",
    "JOURNALED",
    "EXECUTED",
    "SETTLED",
    "DURABLE",
    "PUBLISHED",
    "REJECTED",
    "UNKNOWN_TIMEOUT",
    "UNKNOWN_AFTER_RESTART",
    "IDEMPOTENCY_CONFLICT",
)

FINAL_ACK_STAGES = {
    "DURABLE",
    "PUBLISHED",
    "REJECTED",
    "IDEMPOTENCY_CONFLICT",
    "UNKNOWN_TIMEOUT",
    "UNKNOWN_AFTER_RESTART",
}


def canonical_decimal(value: Decimal | int | str) -> str:
    """Return one lossless, exponent-free representation for a Decimal.

    Floats are intentionally not accepted.  A float reaching a financial
    command is an input-contract error, not something to silently stringify.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("financial decimal cannot be a float or bool")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("financial decimal must be finite")
    text = format(number.normalize(), "f")
    if text in {"", "-0", "0"}:
        return "0"
    return text


def canonicalize(value: Any) -> Any:
    """Recursively canonicalize JSON-compatible values without float math."""

    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, float):
        raise TypeError("canonical financial payload cannot contain float")
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if hasattr(value, "model_dump"):
        return canonicalize(value.model_dump(mode="python"))
    if hasattr(value, "as_dict"):
        return canonicalize(value.as_dict())
    raise TypeError(f"unsupported canonical payload type: {type(value)!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonicalize_result(value: Any) -> Any:
    """Canonicalize an observed result while keeping latency metrics non-financial.

    Execution results may contain float timing counters from existing service
    responses.  Financial quantities remain Decimal/string-only; observational
    floats are rendered as deterministic decimal strings for hashing.
    """

    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, Mapping):
        return {str(key): canonicalize_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize_result(item) for item in value]
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return canonicalize_result(value.model_dump(mode="python"))
    if hasattr(value, "as_dict"):
        return canonicalize_result(value.as_dict())
    raise TypeError(f"unsupported result type: {type(value)!r}")


def payload_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def request_fingerprint(
    *,
    command_type: str,
    account_id: int,
    account_domain: str,
    symbol: str | None,
    product_type: str,
    client_order_id: str | None,
    payload: Mapping[str, Any],
    logical_timestamp: int,
    strategy_instance: str | None,
    generation: int | None,
    config_version: str,
    priority_class: str,
) -> str:
    return payload_hash(
        {
            "command_type": command_type,
            "account_id": int(account_id),
            "account_domain": account_domain,
            "symbol": symbol.upper() if symbol else None,
            "product_type": product_type.upper(),
            "client_order_id": client_order_id,
            "payload": dict(payload),
            "logical_timestamp": int(logical_timestamp),
            "strategy_instance": strategy_instance,
            "generation": generation,
            "config_version": config_version,
            "priority_class": priority_class,
        }
    )


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    request_fingerprint: str
    client_order_id: str | None
    command_type: str
    account_id: int
    account_domain: str
    symbol: str | None
    market_id: str | None
    product_type: str
    epoch: str
    command_sequence: int
    logical_timestamp: int
    rules_version: str
    risk_version: str
    fee_version: str
    config_version: str
    strategy_instance: str | None
    generation: int | None
    priority_class: str
    payload_hash: str
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        command_id: str,
        command_type: str,
        account_id: int,
        account_domain: str = "SPOT",
        symbol: str | None = None,
        market_id: str | int | None = None,
        product_type: str = "SPOT",
        epoch: str = "",
        command_sequence: int = 0,
        logical_timestamp: int = 0,
        rules_version: str = "default",
        risk_version: str = "default",
        fee_version: str = "default",
        config_version: str = "default",
        strategy_instance: str | None = None,
        generation: int | None = None,
        priority_class: str = "NORMAL",
        client_order_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "CommandEnvelope":
        body = dict(payload or {})
        normalized_product = str(product_type or "SPOT").upper()
        normalized_domain = str(account_domain or normalized_product).upper()
        normalized_symbol = str(symbol).upper() if symbol else None
        normalized_priority = str(priority_class or "NORMAL").upper()
        return cls(
            command_id=str(command_id),
            request_fingerprint=request_fingerprint(
                command_type=str(command_type),
                account_id=int(account_id),
                account_domain=normalized_domain,
                symbol=normalized_symbol,
                product_type=normalized_product,
                client_order_id=client_order_id,
                payload=body,
                logical_timestamp=int(logical_timestamp),
                strategy_instance=strategy_instance if strategy_instance is not None else None,
                generation=generation,
                config_version=str(config_version or "default"),
                priority_class=normalized_priority,
            ),
            client_order_id=client_order_id,
            command_type=str(command_type),
            account_id=int(account_id),
            account_domain=normalized_domain,
            symbol=normalized_symbol,
            market_id=str(market_id) if market_id is not None else None,
            product_type=normalized_product,
            epoch=str(epoch or ""),
            command_sequence=int(command_sequence),
            logical_timestamp=int(logical_timestamp),
            rules_version=str(rules_version or "default"),
            risk_version=str(risk_version or "default"),
            fee_version=str(fee_version or "default"),
            config_version=str(config_version or "default"),
            strategy_instance=strategy_instance if strategy_instance is not None else None,
            generation=int(generation) if generation is not None else None,
            priority_class=normalized_priority,
            payload_hash=payload_hash(body),
            payload=body,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "request_fingerprint": self.request_fingerprint,
            "client_order_id": self.client_order_id,
            "command_type": self.command_type,
            "account_id": self.account_id,
            "account_domain": self.account_domain,
            "symbol": self.symbol,
            "market_id": self.market_id,
            "product_type": self.product_type,
            "epoch": self.epoch,
            "command_sequence": self.command_sequence,
            "logical_timestamp": self.logical_timestamp,
            "rules_version": self.rules_version,
            "risk_version": self.risk_version,
            "fee_version": self.fee_version,
            "config_version": self.config_version,
            "strategy_instance": self.strategy_instance,
            "generation": self.generation,
            "priority_class": self.priority_class,
            "payload_hash": self.payload_hash,
            "payload": canonicalize(self.payload),
        }


@dataclass(frozen=True, slots=True)
class SettlementBundle:
    settled: bool = False
    reserve: list[dict[str, Any]] = field(default_factory=list)
    release: list[dict[str, Any]] = field(default_factory=list)
    spot_balance_deltas: list[dict[str, Any]] = field(default_factory=list)
    perp_account_deltas: list[dict[str, Any]] = field(default_factory=list)
    position_pnl_deltas: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    ledger_sequence: int | None = None
    outbox_event_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return canonicalize({
            "settled": self.settled,
            "reserve": self.reserve,
            "release": self.release,
            "spot_balance_deltas": self.spot_balance_deltas,
            "perp_account_deltas": self.perp_account_deltas,
            "position_pnl_deltas": self.position_pnl_deltas,
            "fees": self.fees,
            "ledger_sequence": self.ledger_sequence,
            "outbox_event_ids": self.outbox_event_ids,
        })


@dataclass(frozen=True, slots=True)
class ExecutionBundle:
    execution_id: str
    command_id: str
    epoch: str
    accepted: bool
    rejected: bool
    reject_code: str | None = None
    reject_stage: str | None = None
    order_mutations: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    maker_taker: list[dict[str, Any]] = field(default_factory=list)
    maker_price: list[dict[str, Any]] = field(default_factory=list)
    stp_result: dict[str, Any] = field(default_factory=dict)
    settlement: SettlementBundle = field(default_factory=SettlementBundle)
    changed_price_levels: list[list[str]] = field(default_factory=list)
    priority_sequence: int = 0
    book_sequence: int = 0
    execution_sequence: int = 0
    event_sequence: int = 0
    state_hash_before: str | None = None
    state_hash_after: str | None = None
    result_hash: str = ""
    actual_result: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(
        cls,
        *,
        command: CommandEnvelope,
        execution_id: str,
        result: Mapping[str, Any],
        state_hash_before: str | None = None,
        state_hash_after: str | None = None,
        priority_sequence: int = 0,
        book_sequence: int = 0,
        execution_sequence: int = 0,
        event_sequence: int = 0,
        settlement: SettlementBundle | None = None,
    ) -> "ExecutionBundle":
        raw = dict(result)
        engine_result = raw.get("engine_result")
        if not isinstance(engine_result, Mapping):
            engine_result = {}
        fills = raw.get("fills")
        if not isinstance(fills, list):
            fills = engine_result.get("fills") if isinstance(engine_result.get("fills"), list) else []
        trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []
        order = raw.get("order")
        mutations = raw.get("order_mutations") if isinstance(raw.get("order_mutations"), list) else []
        if isinstance(order, Mapping):
            mutations = [dict(order), *mutations]
        accepted_count = raw.get("accepted_count")
        rejected_count = raw.get("rejected_count", raw.get("failed_count", 0))
        accepted = bool(
            raw.get("accepted")
            or raw.get("fast_path")
            or raw.get("quote_set")
            or (isinstance(accepted_count, int) and accepted_count > 0)
            or order is not None
        )
        rejected = bool(raw.get("rejected") or (isinstance(rejected_count, int) and rejected_count > 0 and not accepted))
        normalized_result = canonicalize_result(raw)
        bundle_body = {
            "command_id": command.command_id,
            "execution_id": execution_id,
            "epoch": command.epoch,
            "accepted": accepted,
            "rejected": rejected,
            "reject_code": raw.get("reject_code"),
            "reject_stage": raw.get("reject_stage"),
            "order_mutations": mutations,
            "fills": fills,
            "maker_taker": trades,
            "maker_price": [
                {"price": item.get("price"), "quantity": item.get("quantity")}
                for item in fills
                if isinstance(item, Mapping)
            ],
            "stp_result": {
                key: engine_result.get(key)
                for key in ("stp_action", "stp_reason", "stp_intercept_count", "stp_decremented_quantity")
                if key in engine_result
            },
            "settlement": (settlement or SettlementBundle()).as_dict(),
            "changed_price_levels": [
                *engine_result.get("changed_bids", []),
                *engine_result.get("changed_asks", []),
            ],
            "priority_sequence": int(priority_sequence),
            "book_sequence": int(book_sequence),
            "execution_sequence": int(execution_sequence),
            "event_sequence": int(event_sequence),
            "state_hash_before": state_hash_before,
            "state_hash_after": state_hash_after,
            "actual_result": normalized_result,
        }
        return cls(
            execution_id=str(execution_id),
            command_id=command.command_id,
            epoch=command.epoch,
            accepted=accepted,
            rejected=rejected,
            reject_code=str(raw.get("reject_code")) if raw.get("reject_code") is not None else None,
            reject_stage=str(raw.get("reject_stage")) if raw.get("reject_stage") is not None else None,
            order_mutations=[dict(item) for item in mutations if isinstance(item, Mapping)],
            fills=[dict(item) for item in fills if isinstance(item, Mapping)],
            maker_taker=[dict(item) for item in trades if isinstance(item, Mapping)],
            maker_price=[dict(item) for item in bundle_body["maker_price"]],
            stp_result=dict(bundle_body["stp_result"]),
            settlement=settlement or SettlementBundle(),
            changed_price_levels=[list(item) for item in bundle_body["changed_price_levels"]],
            priority_sequence=int(priority_sequence),
            book_sequence=int(book_sequence),
            execution_sequence=int(execution_sequence),
            event_sequence=int(event_sequence),
            state_hash_before=state_hash_before,
            state_hash_after=state_hash_after,
            result_hash=sha256(
                canonical_json(canonicalize_result(bundle_body)).encode("utf-8")
            ).hexdigest(),
            actual_result=dict(normalized_result),
        )

    def as_dict(self) -> dict[str, Any]:
        return canonicalize({
            "execution_id": self.execution_id,
            "command_id": self.command_id,
            "epoch": self.epoch,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "reject_code": self.reject_code,
            "reject_stage": self.reject_stage,
            "order_mutations": self.order_mutations,
            "fills": self.fills,
            "maker_taker": self.maker_taker,
            "maker_price": self.maker_price,
            "stp_result": self.stp_result,
            "settlement": self.settlement.as_dict(),
            "changed_price_levels": self.changed_price_levels,
            "priority_sequence": self.priority_sequence,
            "book_sequence": self.book_sequence,
            "execution_sequence": self.execution_sequence,
            "event_sequence": self.event_sequence,
            "state_hash_before": self.state_hash_before,
            "state_hash_after": self.state_hash_after,
            "result_hash": self.result_hash,
            "actual_result": self.actual_result,
        })


@dataclass(frozen=True, slots=True)
class CausalWatermarks:
    epoch: str = ""
    ingress_sequence: int = 0
    command_sequence: int = 0
    priority_sequence: int = 0
    matched_sequence: int = 0
    execution_sequence: int = 0
    settlement_sequence: int = 0
    ledger_sequence: int = 0
    durable_sequence: int = 0
    materialized_sequence: int = 0
    published_sequence: int = 0
    queue_depth: int = 0
    queue_oldest_age_ms: float = 0.0
    materialization_lag: int = 0
    status: str = "STARTING"
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # Queue age is an observational latency metric, not a financial
        # amount.  Keep it numeric for diagnostics; financial payloads still
        # pass through the strict float-rejecting canonicalizer above.
        return {
            "epoch": self.epoch,
            "ingress_sequence": self.ingress_sequence,
            "command_sequence": self.command_sequence,
            "priority_sequence": self.priority_sequence,
            "matched_sequence": self.matched_sequence,
            "execution_sequence": self.execution_sequence,
            "settlement_sequence": self.settlement_sequence,
            "ledger_sequence": self.ledger_sequence,
            "durable_sequence": self.durable_sequence,
            "materialized_sequence": self.materialized_sequence,
            "published_sequence": self.published_sequence,
            "queue_depth": self.queue_depth,
            "queue_oldest_age_ms": self.queue_oldest_age_ms,
            "materialization_lag": self.materialization_lag,
            "status": self.status,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    request_fingerprint: str
    status: str
    ack_stage: str
    epoch: str
    command_sequence: int
    execution_id: str | None = None
    result_hash: str | None = None
    reject_code: str | None = None
    reject_stage: str | None = None
    response: dict[str, Any] | None = None
    watermarks: CausalWatermarks = field(default_factory=CausalWatermarks)

    def as_dict(self) -> dict[str, Any]:
        body = canonicalize({
            "command_id": self.command_id,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status,
            "ack_stage": self.ack_stage,
            "epoch": self.epoch,
            "command_sequence": self.command_sequence,
            "execution_id": self.execution_id,
            "result_hash": self.result_hash,
            "reject_code": self.reject_code,
            "reject_stage": self.reject_stage,
        })
        body["response"] = self.response
        body["causal_watermarks"] = self.watermarks.as_dict()
        return body


def ack_stage_for_status(status: str) -> str:
    normalized = str(status or "").upper()
    if normalized in ACK_STAGES:
        return normalized
    if normalized in {"ACKED", "SUCCESS", "OK"}:
        return "DURABLE"
    if normalized.startswith("UNKNOWN"):
        return "UNKNOWN_TIMEOUT"
    return normalized or "RECEIVED"
