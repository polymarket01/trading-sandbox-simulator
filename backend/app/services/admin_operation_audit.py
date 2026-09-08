from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import to_millis
from app.models.admin_operation_audit import AdminOperationAudit
from app.models.user import User

MAX_RESULT_LIST_ITEMS = 20
MAX_RESULT_DICT_ITEMS = 40
MAX_RESULT_STRING_LENGTH = 500


def _json_safe(value, depth: int = 0):
    if depth > 5:
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return to_millis(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_RESULT_STRING_LENGTH else f"{value[:MAX_RESULT_STRING_LENGTH]}..."
    if isinstance(value, dict):
        items = list(value.items())[:MAX_RESULT_DICT_ITEMS]
        return {str(key): _json_safe(item, depth + 1) for key, item in items}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in list(value)[:MAX_RESULT_LIST_ITEMS]]
    return str(value)


def _operation_id() -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    return f"op_{stamp}_{uuid4().hex[:12]}"


async def record_admin_operation(
    session: AsyncSession,
    *,
    actor: User | None,
    request: Request | None,
    domain: str,
    operation_type: str,
    target_type: str,
    target_id: str | int | None = None,
    target_symbol: str | None = None,
    status: str = "success",
    summary: str,
    result: dict | list | str | int | float | bool | None = None,
) -> AdminOperationAudit:
    request_client = getattr(request, "client", None) if request is not None else None
    request_headers = getattr(request, "headers", None) if request is not None else None
    client_host = getattr(request_client, "host", None)
    user_agent = request_headers.get("user-agent") if request_headers is not None else None
    result_payload = _json_safe(result)
    if isinstance(result_payload, dict):
        if client_host:
            result_payload.setdefault("client_host", client_host)
        if user_agent:
            result_payload.setdefault("user_agent", user_agent[:160])
    operation = AdminOperationAudit(
        operation_id=_operation_id(),
        created_at=datetime.now(tz=UTC),
        actor_user_id=actor.id if actor is not None else None,
        actor_username=actor.username if actor is not None else None,
        domain=domain,
        operation_type=operation_type,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        target_symbol=target_symbol.upper() if target_symbol else None,
        status=status,
        summary=summary[:255],
        result_json=result_payload,
    )
    session.add(operation)
    await session.flush()
    return operation


def serialize_admin_operation(operation: AdminOperationAudit) -> dict:
    return {
        "id": operation.id,
        "operation_id": operation.operation_id,
        "created_at": to_millis(operation.created_at),
        "actor_user_id": operation.actor_user_id,
        "actor_username": operation.actor_username,
        "domain": operation.domain,
        "operation_type": operation.operation_type,
        "target_type": operation.target_type,
        "target_id": operation.target_id,
        "target_symbol": operation.target_symbol,
        "status": operation.status,
        "summary": operation.summary,
        "result": operation.result_json,
    }
