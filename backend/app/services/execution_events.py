from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal


ExecutionEventType = Literal[
    "order_accepted",
    "order_amended",
    "order_cancelled",
    "order_matched",
    "batch_amended",
    "bulk_quote_patch",
]


DEFAULT_EVENT_OUTBOX_MAXSIZE = 4_096


@dataclass(slots=True)
class ExecutionEvent:
    event_type: ExecutionEventType
    symbol: str
    sequence_number: int
    order_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(slots=True)
class EventOutbox:
    """Bounded matching-engine telemetry; never a financial source of truth.

    The canonical single-process runtime does not use this queue as a durable
    journal.  Keeping the newest telemetry while evicting the oldest item is
    therefore preferable to blocking the matching path or allowing an
    unbounded queue to grow with every quote-set operation.
    """

    queue: asyncio.Queue[ExecutionEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_EVENT_OUTBOX_MAXSIZE)
    )
    dropped: int = 0

    def publish_nowait(self, event: ExecutionEvent) -> None:
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            # This is an ephemeral telemetry ring.  Evict the oldest item so
            # consumers see recent state and the matching path never waits on
            # a slow diagnostic consumer.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                self.dropped += 1
                return
            self.queue.task_done()
            self.dropped += 1
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                # No await occurs between the eviction and retry in the
                # canonical event loop, but keep this defensive branch for
                # custom queue implementations used by tests/integrations.
                return

    async def get(self) -> ExecutionEvent:
        return await self.queue.get()

    def task_done(self) -> None:
        self.queue.task_done()

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "depth": self.queue.qsize(),
            "max_size": int(self.queue.maxsize),
            "dropped": int(self.dropped),
        }


def decimal_payload(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class ExecutionEventConsumer:
    def __init__(self, outbox: EventOutbox, handler: Callable[[ExecutionEvent], Awaitable[None]]) -> None:
        self.outbox = outbox
        self.handler = handler

    async def run(self) -> None:
        while True:
            event = await self.outbox.get()
            try:
                await self.handler(event)
            finally:
                self.outbox.task_done()
