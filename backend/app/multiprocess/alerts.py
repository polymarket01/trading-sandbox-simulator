from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import OrderedDict
import hashlib
from typing import Any


@dataclass(slots=True)
class AlertRecord:
    fingerprint: str
    severity: str
    priority: int
    message: str
    count: int
    first_seen: int
    last_seen: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertAggregator:
    """One-minute fingerprint aggregation for runtime/operator alerts.

    The aggregator is deliberately in-memory. It is an observability aid for a
    single sampled run, not a financial or audit source of truth. Repeated
    health polling must update ``last_seen`` without producing a new warning
    record on every cycle.
    """

    def __init__(self, *, window_ms: int = 60_000, limit: int = 256) -> None:
        self.window_ms = max(1_000, int(window_ms))
        self.limit = max(16, int(limit))
        self._items: OrderedDict[str, AlertRecord] = OrderedDict()

    @staticmethod
    def fingerprint(message: str, *, code: str | None = None) -> str:
        body = f"{code or ''}|{message}".strip().lower().encode("utf-8")
        return hashlib.sha256(body).hexdigest()[:16]

    def record(
        self,
        message: str,
        *,
        now_ms: int,
        severity: str = "DEGRADED",
        priority: int = 1,
        code: str | None = None,
    ) -> AlertRecord:
        timestamp = int(now_ms)
        fingerprint = self.fingerprint(message, code=code)
        existing = self._items.get(fingerprint)
        if existing is not None and timestamp - existing.last_seen <= self.window_ms:
            existing.count += 1
            existing.last_seen = timestamp
            existing.severity = str(severity).upper()
            existing.priority = max(existing.priority, int(priority))
            self._items.move_to_end(fingerprint)
            return existing
        item = AlertRecord(
            fingerprint=fingerprint,
            severity=str(severity).upper(),
            priority=int(priority),
            message=str(message)[:500],
            count=1,
            first_seen=timestamp,
            last_seen=timestamp,
        )
        self._items[fingerprint] = item
        self._items.move_to_end(fingerprint)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return item

    def snapshot(self, *, now_ms: int) -> list[dict[str, Any]]:
        timestamp = int(now_ms)
        expired = [
            key
            for key, item in self._items.items()
            if timestamp - item.last_seen > self.window_ms * 10
        ]
        for key in expired:
            self._items.pop(key, None)
        return [
            item.as_dict()
            for item in sorted(
                self._items.values(),
                key=lambda value: (-value.priority, -value.last_seen),
            )
        ]

