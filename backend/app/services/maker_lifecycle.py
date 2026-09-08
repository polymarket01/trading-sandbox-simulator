"""Serialize administrative operations per market; no work on the quoting hot path."""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import inspect

from fastapi import HTTPException


@asynccontextmanager
async def maker_operation(runtime, symbol):
    symbol = symbol.upper()
    owners = getattr(runtime, '_maker_operation_owners', None)
    if owners is None:
        owners = runtime._maker_operation_owners = {}
    task = asyncio.current_task()
    owner = owners.get(symbol)
    if owner is task:
        yield  # Internal calls within the same operation are reentrant.
        return
    if owner is not None:
        raise HTTPException(409, '该币对正在执行铺单启停或切换，请等待结果后刷新')
    owners[symbol] = task  # No await between check and assignment.
    try:
        yield
    finally:
        if owners.get(symbol) is task:
            owners.pop(symbol, None)


def maker_serialized(fn):
    # Resolve annotations in the original module, so FastAPI keeps its true schema.
    signature = inspect.signature(fn, eval_str=True)

    @wraps(fn)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        request = bound.get('request')
        service = bound.get('self')
        runtime = request.app.state.runtime if request is not None else (service.runtime if service.runtime is not None else service)
        symbol = bound.get('symbol') or bound['market'].symbol
        async with maker_operation(runtime, symbol):
            result = await fn(*args, **kwargs)
            state = switch_states(runtime).get(symbol.upper())
            explicit_start = fn.__name__ == 'start_maker_instance_for_market' or (
                fn.__name__ == 'maker_control' and bound['body'].action == 'start')
            if state and state.get('status') in {'failed', 'interrupted'} and explicit_start and (
                isinstance(result, dict) and (result.get('running') or result.get('config', {}).get('enabled'))
            ):
                state['status'] = 'resolved'
            return result

    wrapped.__signature__ = signature
    return wrapped


def switch_states(runtime):
    states = getattr(runtime, '_maker_switch_states', None)
    if states is None:
        states = runtime._maker_switch_states = {}
    return states
