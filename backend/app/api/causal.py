from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_current_user
from app.services.causal_command_service import CausalCommandService


router = APIRouter(prefix="/api/v2", tags=["causal-chain"])


@router.get("/commands/{command_id}")
async def get_command_status(
    command_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    service: CausalCommandService | None = getattr(
        request.app.state.runtime, "causal_command_service", None
    )
    if service is None:
        raise HTTPException(status_code=503, detail="causal command journal unavailable")
    receipt = await service.get_for_account(command_id, int(user.id))
    if receipt is None:
        raise HTTPException(status_code=404, detail="command not found")
    return receipt.as_dict()
