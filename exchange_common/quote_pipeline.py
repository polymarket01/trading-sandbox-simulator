"""Spot/Perp shared quote-pipeline contracts.

This module deliberately contains only deterministic, process-local data
structures and policy helpers.  Exchange adapters may keep product-specific
margin/position logic, but the quote cadence, reference freshness, patch
identity and self-trade boundary are shared here.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable

from .common import now_ms


STP_MODES = frozenset({"cancel_taker", "cancel_maker", "decrement_and_cancel", "reject"})


class LatencyTracker:
    """Bounded percentile windows for the shared quote pipeline stages.

    The tracker is intentionally process-local and allocation-bounded.  It is
    suitable for the single-writer sandbox and does not turn per-quote timing
    into another persistence or network dependency.
    """

    def __init__(self, *, max_samples: int = 4096) -> None:
        self.max_samples = max(32, int(max_samples))
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.max_samples))
        self._counts: Counter[str] = Counter()

    def observe(self, stage: str, elapsed_ms: float) -> None:
        name = str(stage or "unknown")
        value = max(0.0, float(elapsed_ms))
        self._samples[name].append(value)
        self._counts[name] += 1

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
        return round(float(ordered[index]), 3)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for stage, values in self._samples.items():
            ordered = list(values)
            result[stage] = {
                "count": int(self._counts.get(stage, 0)),
                "window": len(ordered),
                "p50_ms": self._percentile(ordered, 50),
                "p95_ms": self._percentile(ordered, 95),
                "p99_ms": self._percentile(ordered, 99),
                "max_ms": round(max(ordered), 3) if ordered else 0.0,
            }
        return result


@dataclass(frozen=True, slots=True)
class ReferencePriceState:
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    received_at_ms: int = 0
    source: str = "unknown"
    stale_after_ms: int = 1_000
    independent_model: bool = False

    @property
    def age_ms(self) -> int:
        if self.received_at_ms <= 0:
            return 10**9
        return max(0, now_ms() - int(self.received_at_ms))

    def age_at(self, timestamp_ms: int | None = None) -> int:
        timestamp = now_ms() if timestamp_ms is None else int(timestamp_ms)
        if self.received_at_ms <= 0:
            return 10**9
        return max(0, timestamp - int(self.received_at_ms))

    def is_stale(self, timestamp_ms: int | None = None) -> bool:
        return self.age_at(timestamp_ms) > max(1, int(self.stale_after_ms))

    @property
    def status(self) -> str:
        if self.bid is None or self.ask is None or self.mid is None:
            return "missing"
        return "stale" if self.is_stale() else "fresh"


@dataclass(frozen=True, slots=True)
class QuoteLevel:
    side: str
    price: Decimal
    quantity: Decimal
    client_order_id: str
    level_index: int = 0
    band: str = ""
    position_action: str = "open"
    reduce_only: bool = False
    leverage: Decimal | None = None
    account: str | None = None


@dataclass(frozen=True, slots=True)
class QuotePlan:
    symbol: str
    product_type: str
    reference: ReferencePriceState
    levels: tuple[QuoteLevel, ...] = ()
    generation: int = 0
    epoch: int = 0
    created_at_ms: int = 0
    reason: str = ""

    def by_client_order_id(self) -> dict[str, QuoteLevel]:
        return {level.client_order_id: level for level in self.levels}

    def side(self, side: str) -> tuple[QuoteLevel, ...]:
        return tuple(level for level in self.levels if level.side == side)


@dataclass(frozen=True, slots=True)
class QuoteDiff:
    """Changed-only quote patch, keyed by client order identity."""

    cancels: tuple[QuoteLevel, ...] = ()
    amends: tuple[tuple[QuoteLevel, QuoteLevel], ...] = ()
    places: tuple[QuoteLevel, ...] = ()
    noop_count: int = 0

    @classmethod
    def between(
        cls,
        current: Iterable[QuoteLevel | dict[str, Any]],
        desired: Iterable[QuoteLevel | dict[str, Any]],
    ) -> "QuoteDiff":
        current_map = {_level_key(item): _coerce_level(item) for item in current}
        desired_map = {_level_key(item): _coerce_level(item) for item in desired}
        cancels: list[QuoteLevel] = []
        amends: list[tuple[QuoteLevel, QuoteLevel]] = []
        places: list[QuoteLevel] = []
        noop = 0
        for key, old in current_map.items():
            if key not in desired_map:
                cancels.append(old)
        for key, new in desired_map.items():
            old = current_map.get(key)
            if old is None:
                places.append(new)
            elif _level_equal(old, new):
                noop += 1
            else:
                amends.append((old, new))
        return cls(tuple(cancels), tuple(amends), tuple(places), noop)

    @property
    def changed_count(self) -> int:
        return len(self.cancels) + len(self.amends) + len(self.places)


def _level_key(level: QuoteLevel | dict[str, Any]) -> str:
    if isinstance(level, QuoteLevel):
        return level.client_order_id
    return str(level.get("client_order_id") or level.get("tag") or "")


def _coerce_level(level: QuoteLevel | dict[str, Any]) -> QuoteLevel:
    if isinstance(level, QuoteLevel):
        return level
    return QuoteLevel(
        side=str(level.get("side") or ""),
        price=Decimal(str(level.get("price") or "0")),
        quantity=Decimal(str(level.get("quantity", level.get("remaining_quantity", "0")))),
        client_order_id=_level_key(level),
        level_index=int(level.get("level_index") or 0),
        band=str(level.get("band") or ""),
        position_action=str(level.get("position_action") or "open"),
        reduce_only=bool(level.get("reduce_only", False)),
        leverage=Decimal(str(level["leverage"])) if level.get("leverage") is not None else None,
        account=str(level.get("account")) if level.get("account") is not None else None,
    )


def _level_equal(left: QuoteLevel, right: QuoteLevel) -> bool:
    return (
        left.side == right.side
        and left.price == right.price
        and left.quantity == right.quantity
        and left.position_action == right.position_action
        and left.reduce_only == right.reduce_only
        and left.leverage == right.leverage
    )


@dataclass(frozen=True, slots=True)
class QuoteCadenceConfig:
    head_ms: int = 50
    near_ms: int = 100
    full_ms: int = 500
    aging_ms: int = 2_000
    ws_delta_flush_ms: int = 20
    snapshot_heartbeat_ms: int = 2_000
    min_interval_ms: int = 20


class QuoteCadenceController:
    """Fixed-deadline cadence helper; work time is subtracted from the period."""

    def __init__(self, config: QuoteCadenceConfig | None = None) -> None:
        self.config = config or QuoteCadenceConfig()

    def interval_ms(self, level_index: int, *, head_levels: int = 5, near_levels: int = 10) -> int:
        if int(level_index) <= max(0, int(head_levels)):
            raw = self.config.head_ms
        elif int(level_index) <= max(int(head_levels), int(near_levels)):
            raw = self.config.near_ms
        else:
            raw = self.config.full_ms
        return max(int(self.config.min_interval_ms), int(raw))

    def sleep_ms(self, started_at_ms: int, *, interval_ms: int, finished_at_ms: int | None = None) -> int:
        finished = now_ms() if finished_at_ms is None else int(finished_at_ms)
        elapsed = max(0, finished - int(started_at_ms))
        return max(0, int(interval_ms) - elapsed)

    def sleep_seconds(self, started_at_ms: int, *, interval_ms: int, finished_at_ms: int | None = None) -> float:
        return self.sleep_ms(started_at_ms, interval_ms=interval_ms, finished_at_ms=finished_at_ms) / 1000

    @staticmethod
    def latest_only(reference: ReferencePriceState, latest_timestamp_ms: int) -> bool:
        return int(latest_timestamp_ms) >= int(reference.received_at_ms)


@dataclass(frozen=True, slots=True)
class SelfTradeDecision:
    mode: str
    reason: str


class SelfTradePolicy:
    """Final matcher STP policy with an explicit cross-bot configuration."""

    def __init__(
        self,
        *,
        same_account_mode: str = "cancel_taker",
        bot_cross_mode: str = "cancel_taker",
    ) -> None:
        self.same_account_mode = self._normalize(same_account_mode)
        self.bot_cross_mode = self._normalize(bot_cross_mode)

    @staticmethod
    def _normalize(mode: str | None) -> str:
        value = str(mode or "cancel_taker").strip().lower()
        return value if value in STP_MODES or value == "allow" else "cancel_taker"

    def decide(
        self,
        *,
        taker_account_key: str | None,
        maker_account_key: str | None,
        taker_is_bot: bool = False,
        maker_is_bot: bool = False,
    ) -> SelfTradeDecision | None:
        if taker_account_key and maker_account_key and taker_account_key == maker_account_key:
            return SelfTradeDecision(self.same_account_mode, "same_account")
        if taker_is_bot and maker_is_bot and self.bot_cross_mode != "allow":
            return SelfTradeDecision(self.bot_cross_mode, "bot_cross_config")
        return None


class UserOrderInteractionPolicy:
    """User liquidity is a valid participant; only configured bot self-crosses are blocked."""

    @staticmethod
    def is_user_role(role: str | None) -> bool:
        return str(role or "").strip().lower() not in {"mm_bot", "flow_bot", "bot", "system"}

    def allows_quote_against_user(self, *, owner_role: str | None = None) -> bool:
        return True

    def should_block_cross(self, *, opposite_is_user: bool, same_bot_account: bool) -> bool:
        if opposite_is_user:
            return False
        return bool(same_bot_account)


class QuotePatchExecutor:
    """Small latest-wins gate for planner patches; user commands never use it."""

    def __init__(self, apply_patch: Callable[[QuotePlan], Any] | None = None) -> None:
        self.apply_patch = apply_patch
        self.latest_generation = -1
        self.pending: QuotePlan | None = None
        self.dropped_stale = 0

    def accept(self, plan: QuotePlan) -> bool:
        if int(plan.generation) <= self.latest_generation:
            self.dropped_stale += 1
            return False
        self.latest_generation = int(plan.generation)
        self.pending = plan
        return True

    def take_latest(self) -> QuotePlan | None:
        plan, self.pending = self.pending, None
        return plan

    def execute_latest(self) -> Any:
        plan = self.take_latest()
        if plan is None or self.apply_patch is None:
            return None
        return self.apply_patch(plan)


@dataclass(frozen=True, slots=True)
class PublishedBookState:
    symbol: str
    seq: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    updated_at_ms: int
    engine_version: int


class PublishedBookPublisher:
    """Monotonic publication guard shared by snapshot and WS adapters."""

    def __init__(self) -> None:
        self._latest: dict[str, PublishedBookState] = {}
        self.same_seq_diff = 0
        self.seq_rollback = 0

    @staticmethod
    def _levels(levels: Iterable[Iterable[Any]]) -> tuple[tuple[str, str], ...]:
        return tuple((str(item[0]), str(item[1])) for item in levels)

    def publish(
        self,
        symbol: str,
        *,
        seq: int,
        bids: Iterable[Iterable[Any]],
        asks: Iterable[Iterable[Any]],
        updated_at_ms: int,
        engine_version: int,
    ) -> PublishedBookState:
        normalized = PublishedBookState(
            symbol=str(symbol).upper(),
            seq=int(seq),
            bids=self._levels(bids),
            asks=self._levels(asks),
            updated_at_ms=int(updated_at_ms),
            engine_version=int(engine_version),
        )
        previous = self._latest.get(normalized.symbol)
        if previous is not None:
            if normalized.seq < previous.seq:
                self.seq_rollback += 1
                raise ValueError(f"published sequence rollback: {normalized.seq} < {previous.seq}")
            if normalized.seq == previous.seq and (normalized.bids != previous.bids or normalized.asks != previous.asks):
                self.same_seq_diff += 1
                raise ValueError(f"same sequence has different book content: {normalized.symbol} seq={normalized.seq}")
        self._latest[normalized.symbol] = normalized
        return normalized

    def latest(self, symbol: str) -> PublishedBookState | None:
        return self._latest.get(str(symbol).upper())
