from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request

from app.services.causal_command_service import CausalCommandService
from app.services.causal_contracts import CausalWatermarks, CommandEnvelope, CommandReceipt
from app.core.time_utils import to_millis


_CURRENT_COMMAND: ContextVar[CommandEnvelope | None] = ContextVar(
    "current_causal_command", default=None
)


def current_command() -> CommandEnvelope | None:
    return _CURRENT_COMMAND.get()


@contextmanager
def causal_command_context(envelope: CommandEnvelope):
    token = _CURRENT_COMMAND.set(envelope)
    try:
        yield
    finally:
        _CURRENT_COMMAND.reset(token)


def causal_service(request: Request) -> CausalCommandService:
    service = getattr(request.app.state.runtime, "causal_command_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="causal command journal unavailable")
    return service


def _payload_dict(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        return dict(payload.model_dump(mode="python"))
    return dict(vars(payload))


def command_id_for(request: Request, payload: Any = None) -> str:
    headers = getattr(request, "headers", {})
    header_id = str(headers.get("X-Command-Id") or "").strip()
    payload_id = str(getattr(payload, "command_id", None) or "").strip()
    return header_id or payload_id or str(uuid4())


async def begin_causal_command(
    request: Request,
    *,
    user,
    command_type: str,
    payload: Any = None,
    symbol: str | None = None,
    product_type: str = "SPOT",
    account_domain: str | None = None,
    client_order_id: str | None = None,
    strategy_instance: str | None = None,
    generation: int | None = None,
    priority_class: str = "NORMAL",
) -> tuple[CommandEnvelope, CommandReceipt, bool]:
    runtime = getattr(getattr(request.app, "state", None), "runtime", None)
    service = getattr(runtime, "causal_command_service", None)
    writer = getattr(runtime, "persistence_writer", None)
    if writer is not None and writer.critical_sink_status().get("status") == "HALTED":
        # Reject before journaling: retries against a halted matcher must not
        # append full quote ladders to an ever-growing UNKNOWN command log.
        raise HTTPException(status_code=503, detail={"code": "CRITICAL_SINK_HALTED"})
    body = _payload_dict(payload)
    command_id = command_id_for(request, payload)
    if hasattr(payload, "command_id"):
        try:
            payload.command_id = command_id
        except (AttributeError, TypeError):
            pass
    logical_timestamp = body.get("logical_timestamp")
    if isinstance(logical_timestamp, datetime):
        logical_timestamp = to_millis(logical_timestamp)
    if logical_timestamp is None:
        # A server-generated wall clock must not enter the request fingerprint:
        # an exact retry of the same command_id would otherwise conflict with
        # itself.  The durable row still records the command sequence/time at
        # journal commit; clients that need a logical timestamp may send one.
        logical_timestamp = 0
    sequence = await service.allocate_command_sequence() if service is not None else 0
    envelope = CommandEnvelope.create(
        command_id=command_id,
        command_type=command_type,
        account_id=int(user.id),
        account_domain=account_domain or product_type,
        symbol=(symbol or body.get("symbol")) if service is not None else None,
        market_id=body.get("market_id") if service is not None else None,
        product_type=product_type,
        epoch=str(getattr(runtime, "causal_epoch", "legacy")),
        command_sequence=sequence,
        logical_timestamp=int(logical_timestamp),
        rules_version=str(body.get("rules_version") or "default"),
        risk_version=str(body.get("risk_version") or "default"),
        fee_version=str(body.get("fee_version") or "default"),
        config_version=str(body.get("config_version") or "default"),
        strategy_instance=(strategy_instance or body.get("strategy_instance")) if service is not None else None,
        generation=(generation if generation is not None else body.get("generation")) if service is not None else None,
        priority_class=priority_class,
        client_order_id=(client_order_id or body.get("client_order_id")) if service is not None else None,
        payload=body if service is not None else {},
    )
    if service is None:
        return (
            envelope,
            CommandReceipt(
                command_id=envelope.command_id,
                request_fingerprint=envelope.request_fingerprint,
                status="LEGACY_COMPAT",
                ack_stage="RECEIVED",
                epoch=envelope.epoch,
                command_sequence=envelope.command_sequence,
                watermarks=CausalWatermarks(epoch=envelope.epoch, status="HEALTHY"),
            ),
            True,
        )
    receipt, is_new = await service.begin(envelope)
    return envelope, receipt, is_new


def existing_response_or_raise(receipt: CommandReceipt) -> dict[str, Any] | None:
    if receipt.status == "IDEMPOTENCY_CONFLICT":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "command_id": receipt.command_id,
            },
        )
    if not receipt.response:
        return None
    response = dict(receipt.response)
    response.setdefault("causal", receipt.as_dict())
    return response


async def complete_causal_command(
    request: Request,
    envelope: CommandEnvelope,
    result: dict[str, Any],
    *,
    response: dict[str, Any] | None = None,
    priority_sequence: int = 0,
    book_sequence: int = 0,
    status: str = "DURABLE",
) -> dict[str, Any]:
    service = getattr(
        getattr(getattr(request.app, "state", None), "runtime", None),
        "causal_command_service",
        None,
    )
    if service is None:
        return dict(response if response is not None else result)
    public_result = {
        str(key): value
        for key, value in result.items()
        if not str(key).startswith("_")
    }
    effective_book_sequence = int(book_sequence or 0)
    if effective_book_sequence <= 0:
        for key in ("published_sequence", "book_sequence", "exchange_sequence", "matched_sequence"):
            try:
                effective_book_sequence = max(effective_book_sequence, int(public_result.get(key) or 0))
            except (TypeError, ValueError):
                pass
    runtime = getattr(getattr(request.app, "state", None), "runtime", None)
    if effective_book_sequence <= 0 and runtime is not None and envelope.symbol:
        published = getattr(runtime, "published_orderbooks", {}).get(str(envelope.symbol).upper())
        if published is not None:
            effective_book_sequence = int(getattr(published, "seq", 0) or 0)
    receipt = await service.complete(
        envelope,
        public_result,
        priority_sequence=priority_sequence,
        book_sequence=effective_book_sequence,
        response=response if response is not None else public_result,
        status=status,
    )
    output = dict(response if response is not None else public_result)
    output["causal"] = receipt.as_dict()
    return output


async def mark_unknown_causal(request: Request, envelope: CommandEnvelope, *, reason: str):
    service = getattr(
        getattr(getattr(request.app, "state", None), "runtime", None),
        "causal_command_service",
        None,
    )
    if service is not None:
        return await service.mark_unknown(envelope.command_id, reason=reason)
    return None


async def reject_causal_command(
    request: Request,
    envelope: CommandEnvelope,
    *,
    code: str,
    stage: str,
    reason: str,
) -> CommandReceipt:
    service = getattr(
        getattr(getattr(request.app, "state", None), "runtime", None),
        "causal_command_service",
        None,
    )
    if service is None:
        return CommandReceipt(
            command_id=envelope.command_id,
            request_fingerprint=envelope.request_fingerprint,
            status="REJECTED",
            ack_stage="REJECTED",
            epoch=envelope.epoch,
            command_sequence=envelope.command_sequence,
            reject_code=code,
            reject_stage=stage,
            response={"error": reason},
            watermarks=CausalWatermarks(epoch=envelope.epoch, status="HEALTHY"),
        )
    return await service.reject(
        envelope,
        code=code,
        stage=stage,
        reason=reason,
        response={"error": reason},
    )
