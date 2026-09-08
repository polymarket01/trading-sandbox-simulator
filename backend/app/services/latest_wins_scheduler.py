from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any


Payload = dict[str, Any]
MergePayload = Callable[[Payload, Payload], Payload]
ConsumePayload = Callable[[str, Payload], Awaitable[None]]


class LatestWinsTaskScheduler:
    """Bounded async scheduler for non-authoritative broadcast work.

    There is at most one running task and one pending payload per key.  A
    slow socket therefore cannot turn every engine update into a retained
    asyncio Task holding a full order-book snapshot.  The caller decides how
    to merge the pending payload; the scheduler only owns task cardinality.
    """

    def __init__(self, consumer: ConsumePayload, *, name: str) -> None:
        self._consumer = consumer
        self._name = str(name)
        self._pending: dict[str, Payload] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._coalesced = 0
        self._callback_errors = 0

    def submit(self, key: str, payload: Payload, *, merge: MergePayload) -> asyncio.Task[None]:
        normalized = str(key)
        current = self._pending.get(normalized)
        if current is not None:
            self._pending[normalized] = merge(current, payload)
            self._coalesced += 1
        else:
            self._pending[normalized] = payload

        task = self._tasks.get(normalized)
        if task is None or task.done():
            task = asyncio.create_task(self._run(normalized), name=f"{self._name}-{normalized}")
            self._tasks[normalized] = task
        return task

    async def _run(self, key: str) -> None:
        try:
            while True:
                payload = self._pending.pop(key, None)
                if payload is None:
                    return
                try:
                    await self._consumer(key, payload)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._callback_errors += 1
                    logging.getLogger(__name__).exception(
                        "latest-wins callback failed scheduler=%s key=%s",
                        self._name,
                        key,
                    )
        finally:
            self._tasks.pop(key, None)

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "active_tasks": sum(1 for task in self._tasks.values() if not task.done()),
            "pending_keys": len(self._pending),
            "coalesced": int(self._coalesced),
            "callback_errors": int(self._callback_errors),
        }
