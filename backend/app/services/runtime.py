from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from os import getpid
from pathlib import Path
from uuid import uuid4

from app.services.account_service import AccountService
from app.services.clearinghouse import Clearinghouse
from app.core.config import settings
from app.services.execution_events import EventOutbox
from app.services.exchange_core import ExchangeCore
from app.services.market_data_service import MarketDataService
from app.services.matching_engine import MatchingEngine
from app.services.rate_limiter import TokenBucketRateLimiter
from app.services.sequencer import SymbolSequencer
from app.services.state_snapshot import StateSnapshotService
from app.core.time_utils import to_millis
from app.ws.manager import WebSocketManager
from exchange_common.quote_pipeline import LatencyTracker, PublishedBookPublisher


LIQUIDITY_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "liquidity_runtime_overrides.json"
LIQUIDITY_LITE_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "liquidity_lite_runtime_overrides.json"
LIQUIDITY_STRATEGY_SELECTION_PATH = Path(__file__).resolve().parents[2] / "data" / "liquidity_strategy_selection.json"
SUPPORTED_LIQUIDITY_STRATEGIES = {"LITE", "PERP_MM", "CONTRACT_LADDER", "SIMPLE_BBO"}
DEFAULT_LIQUIDITY_STRATEGY_SELECTION = {"BTCUSDT": "LITE"}


@dataclass(frozen=True, slots=True)
class PublishedOrderBook:
    """Immutable last-committed book published for lock-free public reads."""

    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    seq: int
    updated_at_ms: int
    engine_version: int = 0
    depth_views: tuple[tuple[int, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]], ...] = ()
    snapshot_wire_cache: tuple[tuple[int, str], ...] = ()
    stream_epoch: str = ""


class AppRuntime:
    def __init__(self) -> None:
        self.started_at = datetime.now(tz=UTC)
        configured_run_id = str(settings.sandbox_run_id or os.getenv("SANDBOX_RUN_ID") or "").strip()
        self.run_id = configured_run_id or f"run-{getpid()}-{to_millis(self.started_at)}"
        self.causal_epoch = f"{self.run_id}-{uuid4().hex[:12]}"
        self.core_mode = str(settings.core_mode or "legacy").lower()
        self.engine = MatchingEngine()
        # ExchangeCore owns the same engine instance used by the compatibility
        # service layer.  Customer REST paths can therefore migrate gradually
        # without creating a second order book.
        self.exchange_core = ExchangeCore(
            engine=self.engine,
            sequence_start=settings.exchange_sequence_start,
            max_queue=settings.exchange_command_queue_max,
            ack_cache_max_size=settings.exchange_ack_cache_max_size,
            ack_cache_ttl_seconds=settings.exchange_ack_cache_ttl_seconds,
            failure_sample_limit=settings.exchange_ack_failure_sample_limit,
            epoch=self.causal_epoch,
            mode=self.core_mode,
        )
        snapshot_dir = Path(settings.exchange_snapshot_dir)
        if not snapshot_dir.is_absolute():
            snapshot_dir = Path(__file__).resolve().parents[3] / snapshot_dir
        self.state_snapshot = StateSnapshotService(
            snapshot_dir,
            keep=settings.exchange_snapshot_keep,
            max_total_bytes=settings.exchange_snapshot_max_bytes,
        )
        self.clearinghouse = Clearinghouse()
        self.event_outbox = EventOutbox()
        self.financial_outbox_metrics: dict[str, object] = {
            "delivered": 0,
            "last_event_id": None,
            "last_event_type": None,
            "last_delivered_at": None,
        }
        self.symbol_sequencers: dict[str, SymbolSequencer] = {}
        self.account_service = AccountService()
        self.market_data = MarketDataService()
        self.rate_limiter = TokenBucketRateLimiter()
        self.persistence_writer = None  # type: ignore[assignment]
        self.causal_command_service = None  # type: ignore[assignment]
        self.history_store = None  # type: ignore[assignment]
        self.storage_degraded = False
        self.sampling_metrics: dict[str, object] = {
            "sample_count": 0,
            "sampling_drops": 0,
            "last_sample_at_ms": 0,
            "last_kline_at_ms": 0,
        }
        self.minute_runtime_metrics: dict[str, dict[str, object]] = defaultdict(dict)
        self.runtime_trades_by_user: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=500))
        self.runtime_ledger_by_user: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=500))
        # Sequence numbers are process-local.  Clients need this epoch to
        # distinguish an out-of-order frame from a healthy backend restart,
        # otherwise a pre-restart high seq permanently rejects the new stream.
        self.orderbook_stream_id = f"{getpid()}-{to_millis(self.started_at)}"
        self.ws = WebSocketManager(stream_id=self.orderbook_stream_id)
        self.market_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.market_lock_owners: dict[str, asyncio.Task] = {}
        self.published_orderbooks: dict[str, PublishedOrderBook] = {}
        self.book_publisher = PublishedBookPublisher()
        self.quote_latency = LatencyTracker()
        # These caches are bounded by design. Snapshot JSON is populated only
        # by the new-connection/heartbeat path; the engine path does not pay
        # for full-depth frames that it will not send.
        self._delta_wire_cache: OrderedDict[
            tuple[str, int, int],
            tuple[
                tuple[int, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...], str],
                ...,
            ],
        ] = OrderedDict()
        self._delta_wire_cache_max_size = 256
        self.orderbook_cache_metrics: dict[str, int] = {
            "engine_snapshot_calls": 0,
            "snapshot_reuses": 0,
            "same_content_reuses": 0,
            "snapshot_json_frames_built": 0,
            "snapshot_json_bytes_built": 0,
            "same_seq_diff": 0,
            "seq_rollback": 0,
            "snapshot_wire_cache_hits": 0,
            "snapshot_wire_cache_misses": 0,
            "delta_json_frames_built": 0,
            "delta_json_bytes_built": 0,
            "delta_wire_cache_hits": 0,
            "delta_wire_cache_misses": 0,
        }
        self.financial_resource_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.sequence_numbers: dict[str, int] = defaultdict(int)
        self.contract_price_snapshots: dict[str, dict] = {}
        self.contract_price_persist_ms: dict[str, int] = {}
        self.contract_maintenance_metrics: dict[str, dict] = {}
        self.contract_risk_alerts: dict[str, list[dict]] = defaultdict(list)
        self.contract_auto_funding_watermarks: dict[str, int] = {}
        self.contract_funding_worker_id = f"contract-maintenance-{getpid()}-{self.started_at.strftime('%Y%m%d%H%M%S')}"
        self.contract_liquidity_metrics: dict[str, dict] = {}
        self.bot_metrics: dict[str, dict] = defaultdict(dict)
        self.liquidity_metrics: dict[str, dict] = defaultdict(dict)
        self.watcher_restart_count = 0
        self.orderbook_reconcile_metrics: dict[str, dict] = defaultdict(
            lambda: {
                "last_reconcile_at_ms": 0,
                "last_reason": None,
                "last_engine_only_removed": 0,
                "last_db_reloaded": 0,
                "total_reconciles": 0,
                "total_engine_only_removed": 0,
                "total_db_reloaded": 0,
            }
        )
        self.virtual_volume_metrics: dict[str, dict] = {}
        self.liquidity_runtime_overrides: dict[str, dict] = self._load_liquidity_runtime_overrides()
        self.liquidity_lite_runtime_overrides: dict[str, dict] = self._load_liquidity_lite_runtime_overrides()
        self.liquidity_strategy_selection: dict[str, str] = self._load_liquidity_strategy_selection()

    def record_runtime_trade_payloads(self, payloads: list[dict]) -> None:
        for payload in payloads:
            for field in ("taker_user_id", "maker_user_id"):
                user_id = payload.get(field)
                if user_id is None:
                    continue
                item = dict(payload)
                item["account_user_id"] = int(user_id)
                item["is_taker"] = field == "taker_user_id"
                self.runtime_trades_by_user[int(user_id)].appendleft(item)

    def record_runtime_ledger_deltas(self, deltas: list[dict], *, created_at_ms: int) -> None:
        for delta in deltas:
            user_id = delta.get("user_id")
            if user_id is None:
                continue
            self.runtime_ledger_by_user[int(user_id)].appendleft(
                {**dict(delta), "created_at": int(created_at_ms), "durable": False, "persistence": "runtime_memory"}
            )

    @asynccontextmanager
    async def financial_resources(self, keys) -> None:
        """Acquire shared financial resources in a stable global order."""
        normalized = sorted({str(key) for key in keys})
        async with AsyncExitStack() as stack:
            for key in normalized:
                await stack.enter_async_context(self.financial_resource_locks[key])
            yield

    @asynccontextmanager
    async def market_financial_guard(self, symbol: str, key_loader) -> None:
        """Serialize a market mutation, then lock every account it can settle."""
        normalized = symbol.upper()
        async with self.market_locks[normalized]:
            task = asyncio.current_task()
            if task is not None:
                self.market_lock_owners[normalized] = task
            try:
                keys = await key_loader()
                async with self.financial_resources(keys):
                    yield
            finally:
                if task is not None and self.market_lock_owners.get(normalized) is task:
                    self.market_lock_owners.pop(normalized, None)

    async def orderbook_snapshot(
        self,
        symbol: str,
        depth: int | None = None,
        *,
        advance: bool = False,
    ) -> tuple[dict[str, list[list[str]]], int, int]:
        normalized = symbol.upper()
        published = self.published_orderbooks.get(normalized)
        if published is None:
            # Startup-only fallback. Once a committed snapshot exists, every
            # REST/WS reader stays independent from the mutation/SQLite lock.
            if self.market_lock_owners.get(normalized) is asyncio.current_task():
                published = self.publish_orderbook_snapshot_unlocked(normalized)
            else:
                async with self.market_locks[normalized]:
                    published = self.published_orderbooks.get(normalized)
                    if published is None:
                        published = self.publish_orderbook_snapshot_unlocked(normalized)
        return self._slice_published_orderbook(published, depth), published.seq, published.updated_at_ms

    def orderbook_snapshot_unlocked(
        self,
        symbol: str,
        depth: int | None = None,
    ) -> tuple[dict[str, list[list[str]]], int, int]:
        published = self.publish_orderbook_snapshot_unlocked(symbol)
        return self._slice_published_orderbook(published, depth), published.seq, published.updated_at_ms

    def publish_orderbook_snapshot_unlocked(self, symbol: str) -> PublishedOrderBook:
        """Publish engine state only after commit or after DB-backed recovery.

        Callers must own the market mutation lock, except during single-threaded
        application bootstrap before request handling starts.
        """
        normalized = symbol.upper()
        publish_started = time.perf_counter()
        engine_version = self.engine.market_version(normalized)
        previous = self.published_orderbooks.get(normalized)
        if previous is not None and previous.engine_version == engine_version:
            self.orderbook_cache_metrics["snapshot_reuses"] += 1
            return previous
        self.orderbook_cache_metrics["engine_snapshot_calls"] += 1
        canonical_snapshot = self.engine.snapshot(normalized)
        bids = tuple((str(price), str(quantity)) for price, quantity in canonical_snapshot.get("bids", []))
        asks = tuple((str(price), str(quantity)) for price, quantity in canonical_snapshot.get("asks", []))
        if previous is not None and previous.bids == bids and previous.asks == asks:
            # A bookkeeping-only engine mutation does not change the wire
            # content or public sequence. Reuse all immutable views/frames.
            published = PublishedOrderBook(
                bids=previous.bids,
                asks=previous.asks,
                seq=previous.seq,
                updated_at_ms=previous.updated_at_ms,
                engine_version=engine_version,
                depth_views=previous.depth_views,
                snapshot_wire_cache=previous.snapshot_wire_cache,
                stream_epoch=previous.stream_epoch or self.causal_epoch,
            )
            self.published_orderbooks[normalized] = published
            self.orderbook_cache_metrics["same_content_reuses"] += 1
            self.quote_latency.observe("engine_to_published_book", (time.perf_counter() - publish_started) * 1000)
            return published
        seq, updated_at_ms = self.market_data.sync_orderbook_snapshot_meta(normalized, canonical_snapshot)
        try:
            self.book_publisher.publish(
                normalized,
                seq=seq,
                bids=bids,
                asks=asks,
                updated_at_ms=updated_at_ms,
                engine_version=engine_version,
            )
        except ValueError:
            self.orderbook_cache_metrics["same_seq_diff"] = int(self.book_publisher.same_seq_diff)
            self.orderbook_cache_metrics["seq_rollback"] = int(self.book_publisher.seq_rollback)
            raise
        self.orderbook_cache_metrics["same_seq_diff"] = int(self.book_publisher.same_seq_diff)
        self.orderbook_cache_metrics["seq_rollback"] = int(self.book_publisher.seq_rollback)
        depth_views: list[tuple[int, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]] = []
        for depth in (5, 20, 50, 100):
            depth_views.append((depth, bids[:depth], asks[:depth]))
        depth_views.append((0, bids, asks))
        published = PublishedOrderBook(
            bids=bids,
            asks=asks,
            seq=seq,
            updated_at_ms=updated_at_ms,
            engine_version=engine_version,
            depth_views=tuple(depth_views),
            # Built lazily by orderbook_snapshot_payload, never on every
            # high-frequency book mutation.
            snapshot_wire_cache=(),
            stream_epoch=self.causal_epoch,
        )
        self.published_orderbooks[normalized] = published
        self.exchange_core.record_published(self.exchange_core.watermarks_snapshot()["matched_seq"])
        self.quote_latency.observe("engine_to_published_book", (time.perf_counter() - publish_started) * 1000)
        return published

    @staticmethod
    def _slice_published_orderbook(
        published: PublishedOrderBook,
        depth: int | None = None,
    ) -> dict[str, list[list[str]]]:
        requested = 0 if depth is None else max(0, int(depth))
        view = next((item for item in published.depth_views if item[0] == requested), None)
        if view is not None:
            _, bids, asks = view
        else:
            bids = published.bids if depth is None else published.bids[:depth]
            asks = published.asks if depth is None else published.asks[:depth]
        return {
            "bids": [[price, quantity] for price, quantity in bids],
            "asks": [[price, quantity] for price, quantity in asks],
        }

    @staticmethod
    def _delta_levels(
        previous: tuple[tuple[str, str], ...],
        current: tuple[tuple[str, str], ...],
    ) -> list[list[str]]:
        previous_map = dict(previous)
        current_map = dict(current)
        changes: list[list[str]] = []
        for price, quantity in current:
            if previous_map.get(price) != quantity:
                changes.append([price, quantity])
        for price, _quantity in previous:
            if price not in current_map:
                changes.append([price, "0"])
        return changes

    def orderbook_delta_payload(
        self,
        symbol: str,
        previous: PublishedOrderBook | None,
        *,
        source: str = "runtime",
    ) -> dict:
        """Build a compact changed-level payload and per-depth wire cache."""
        normalized = symbol.upper()
        current = self.published_orderbooks.get(normalized)
        if current is None:
            current = self.publish_orderbook_snapshot_unlocked(normalized)
        previous = previous or current
        cache_key = (normalized, previous.engine_version, current.engine_version)
        cached_delta = self._delta_wire_cache.get(cache_key)
        if cached_delta is None:
            self.orderbook_cache_metrics["delta_wire_cache_misses"] += 1
            cached_items: list[tuple[int, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...], str]] = []
            previous_views = {depth: (bids, asks) for depth, bids, asks in previous.depth_views}
            for depth, current_bids, current_asks in current.depth_views:
                previous_bids, previous_asks = previous_views.get(
                    depth,
                    (previous.bids[:depth or None], previous.asks[:depth or None]),
                )
                bids = tuple(tuple(item) for item in self._delta_levels(previous_bids, current_bids))
                asks = tuple(tuple(item) for item in self._delta_levels(previous_asks, current_asks))
                # `source` is diagnostic metadata, not book content. Omitting
                # it from the cached wire lets fast and periodic publishers
                # reuse the exact same JSON for one immutable version pair.
                wire = json.dumps(
                    {
                        "channel": "orderbook",
                        "type": "delta",
                        "symbol": normalized,
                        "stream_id": self.orderbook_stream_id,
                        "stream_epoch": self.causal_epoch,
                        "seq": current.seq,
                        "ts": current.updated_at_ms,
                        "bids": [list(item) for item in bids],
                        "asks": [list(item) for item in asks],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cached_items.append((depth, bids, asks, wire))
            cached_delta = tuple(cached_items)
            self._delta_wire_cache[cache_key] = cached_delta
            self._delta_wire_cache.move_to_end(cache_key)
            while len(self._delta_wire_cache) > self._delta_wire_cache_max_size:
                self._delta_wire_cache.popitem(last=False)
            self.orderbook_cache_metrics["delta_json_frames_built"] += len(cached_delta)
            self.orderbook_cache_metrics["delta_json_bytes_built"] += sum(
                len(wire.encode("utf-8")) for _depth, _bids, _asks, wire in cached_delta
            )
        else:
            self.orderbook_cache_metrics["delta_wire_cache_hits"] += 1
            self._delta_wire_cache.move_to_end(cache_key)
        depth_wire_cache = tuple((depth, wire) for depth, _bids, _asks, wire in cached_delta)
        depth_payloads = {
            depth: ([list(item) for item in bids], [list(item) for item in asks])
            for depth, bids, asks, _wire in cached_delta
        }
        bids, asks = depth_payloads.get(100, depth_payloads.get(0, ([], [])))
        return {
            "channel": "orderbook",
            "type": "delta",
            "symbol": normalized,
            "stream_id": self.orderbook_stream_id,
            "stream_epoch": self.causal_epoch,
            "seq": current.seq,
            "ts": current.updated_at_ms,
            "bids": bids,
            "asks": asks,
            "source": source,
            "_book_fingerprint": json.dumps(
                {"bids": current.bids, "asks": current.asks},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "_wire_cache": tuple(depth_wire_cache),
        }

    def _ensure_snapshot_wire_cache(self, symbol: str, published: PublishedOrderBook) -> PublishedOrderBook:
        if published.snapshot_wire_cache:
            self.orderbook_cache_metrics["snapshot_wire_cache_hits"] += 1
            return published
        self.orderbook_cache_metrics["snapshot_wire_cache_misses"] += 1
        snapshot_wire_cache = tuple(
            (
                depth,
                json.dumps(
                    {
                        "channel": "orderbook",
                        "type": "snapshot",
                        "symbol": symbol,
                        "stream_id": self.orderbook_stream_id,
                        "stream_epoch": self.causal_epoch,
                        "seq": published.seq,
                        "ts": published.updated_at_ms,
                        "bids": [list(item) for item in bid_view],
                        "asks": [list(item) for item in ask_view],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            for depth, bid_view, ask_view in published.depth_views
        )
        self.orderbook_cache_metrics["snapshot_json_frames_built"] += len(snapshot_wire_cache)
        self.orderbook_cache_metrics["snapshot_json_bytes_built"] += sum(
            len(wire.encode("utf-8")) for _depth, wire in snapshot_wire_cache
        )
        published = PublishedOrderBook(
            bids=published.bids,
            asks=published.asks,
            seq=published.seq,
            updated_at_ms=published.updated_at_ms,
            engine_version=published.engine_version,
            depth_views=published.depth_views,
            snapshot_wire_cache=snapshot_wire_cache,
            stream_epoch=published.stream_epoch or self.causal_epoch,
        )
        self.published_orderbooks[symbol] = published
        return published

    def orderbook_snapshot_payload(self, symbol: str, *, source: str = "runtime") -> dict:
        normalized = symbol.upper()
        current = self.published_orderbooks.get(normalized)
        if current is None:
            current = self.publish_orderbook_snapshot_unlocked(normalized)
        current = self._ensure_snapshot_wire_cache(normalized, current)
        return {
            "channel": "orderbook",
            "type": "snapshot",
            "symbol": normalized,
            "stream_id": self.orderbook_stream_id,
            "stream_epoch": self.causal_epoch,
            "seq": current.seq,
            "ts": current.updated_at_ms,
            "bids": [list(item) for item in current.bids],
            "asks": [list(item) for item in current.asks],
            "source": source,
            "_book_fingerprint": json.dumps(
                {"bids": current.bids, "asks": current.asks},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "_wire_cache": current.snapshot_wire_cache,
        }

    def orderbook_cache_metrics_snapshot(self) -> dict[str, int]:
        return {key: int(value) for key, value in self.orderbook_cache_metrics.items()}

    @staticmethod
    def _slice_orderbook_snapshot(
        snapshot: dict[str, list[list[str]]],
        depth: int | None = None,
    ) -> dict[str, list[list[str]]]:
        if depth is None:
            return snapshot
        return {
            "bids": snapshot.get("bids", [])[:depth],
            "asks": snapshot.get("asks", [])[:depth],
        }

    def record_orderbook_reconcile(
        self,
        symbol: str,
        *,
        reason: str,
        engine_only_removed: int = 0,
        db_reloaded: int = 0,
    ) -> dict:
        normalized = symbol.upper()
        metrics = self.orderbook_reconcile_metrics[normalized]
        metrics["last_reconcile_at_ms"] = to_millis(datetime.now(tz=UTC))
        metrics["last_reason"] = reason
        metrics["last_engine_only_removed"] = int(engine_only_removed)
        metrics["last_db_reloaded"] = int(db_reloaded)
        metrics["total_reconciles"] = int(metrics.get("total_reconciles") or 0) + 1
        metrics["total_engine_only_removed"] = int(metrics.get("total_engine_only_removed") or 0) + int(engine_only_removed)
        metrics["total_db_reloaded"] = int(metrics.get("total_db_reloaded") or 0) + int(db_reloaded)
        return dict(metrics)

    def orderbook_reconcile_snapshot(self, symbol: str) -> dict:
        return dict(self.orderbook_reconcile_metrics[symbol.upper()])

    def _load_liquidity_runtime_overrides(self) -> dict[str, dict]:
        try:
            if not LIQUIDITY_RUNTIME_CONFIG_PATH.exists():
                return {}
            payload = json.loads(LIQUIDITY_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            return {str(symbol).upper(): value for symbol, value in payload.items() if isinstance(value, dict)}
        except Exception:
            return {}

    def _persist_liquidity_runtime_overrides(self) -> None:
        LIQUIDITY_RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIQUIDITY_RUNTIME_CONFIG_PATH.write_text(
            json.dumps(self.liquidity_runtime_overrides, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_liquidity_lite_runtime_overrides(self) -> dict[str, dict]:
        try:
            if not LIQUIDITY_LITE_RUNTIME_CONFIG_PATH.exists():
                return {}
            payload = json.loads(LIQUIDITY_LITE_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            return {str(symbol).upper(): value for symbol, value in payload.items() if isinstance(value, dict)}
        except Exception:
            return {}

    def _persist_liquidity_lite_runtime_overrides(self) -> None:
        LIQUIDITY_LITE_RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIQUIDITY_LITE_RUNTIME_CONFIG_PATH.write_text(
            json.dumps(self.liquidity_lite_runtime_overrides, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_liquidity_strategy_selection(self) -> dict[str, str]:
        selection = dict(DEFAULT_LIQUIDITY_STRATEGY_SELECTION)
        try:
            if not LIQUIDITY_STRATEGY_SELECTION_PATH.exists():
                return selection
            payload = json.loads(LIQUIDITY_STRATEGY_SELECTION_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return selection
            selection.update({
                str(symbol).upper(): str(version).upper()
                for symbol, version in payload.items()
                if str(version).upper() in SUPPORTED_LIQUIDITY_STRATEGIES
            })
            return selection
        except Exception:
            return selection

    def _persist_liquidity_strategy_selection(self) -> None:
        LIQUIDITY_STRATEGY_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIQUIDITY_STRATEGY_SELECTION_PATH.write_text(
            json.dumps(self.liquidity_strategy_selection, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get_liquidity_runtime_overrides(self, symbol: str) -> dict:
        return deepcopy(self.liquidity_runtime_overrides.get(symbol.upper(), {}))

    def set_liquidity_runtime_overrides(self, symbol: str, payload: dict) -> None:
        key = symbol.upper()
        cleaned = deepcopy(payload) if isinstance(payload, dict) else {}
        if cleaned:
            self.liquidity_runtime_overrides[key] = cleaned
        else:
            self.liquidity_runtime_overrides.pop(key, None)
        self._persist_liquidity_runtime_overrides()

    def get_liquidity_lite_runtime_overrides(self, symbol: str) -> dict:
        return deepcopy(self.liquidity_lite_runtime_overrides.get(symbol.upper(), {}))

    def set_liquidity_lite_runtime_overrides(self, symbol: str, payload: dict) -> None:
        key = symbol.upper()
        cleaned = deepcopy(payload) if isinstance(payload, dict) else {}
        if cleaned:
            self.liquidity_lite_runtime_overrides[key] = cleaned
        else:
            self.liquidity_lite_runtime_overrides.pop(key, None)
        self._persist_liquidity_lite_runtime_overrides()

    def get_liquidity_strategy_selection(self, symbol: str) -> str:
        return self.liquidity_strategy_selection.get(symbol.upper(), "LITE")

    def set_liquidity_strategy_selection(self, symbol: str, version: str) -> None:
        normalized = version.upper()
        if normalized not in SUPPORTED_LIQUIDITY_STRATEGIES:
            raise ValueError("unsupported strategy version")
        self.liquidity_strategy_selection[symbol.upper()] = normalized
        self._persist_liquidity_strategy_selection()

    def observe_sequence(self, symbol: str, sequence_number: int | None) -> None:
        if sequence_number is None:
            return
        key = symbol.upper()
        self.sequence_numbers[key] = max(self.sequence_numbers[key], int(sequence_number))

    def next_sequence(self, symbol: str) -> int:
        key = symbol.upper()
        self.sequence_numbers[key] += 1
        return self.sequence_numbers[key]

    def get_symbol_sequencer(self, symbol: str) -> SymbolSequencer:
        key = symbol.upper()
        sequencer = self.symbol_sequencers.get(key)
        if sequencer is None:
            sequencer = SymbolSequencer(
                symbol=key,
                engine=self.engine,
                outbox=self.event_outbox,
                next_sequence=self.next_sequence,
                observe_sequence=self.observe_sequence,
                max_queue=settings.sequencer_queue_max,
            )
            self.symbol_sequencers[key] = sequencer
        return sequencer

    async def stop_sequencers(self) -> None:
        await self.exchange_core.stop()
        for sequencer in list(self.symbol_sequencers.values()):
            await sequencer.stop()

    def exchange_diagnostics(self) -> dict:
        metrics = self.exchange_core.metrics_snapshot()
        metrics["causal_chain"] = {
            "mode": self.core_mode,
            "epoch": self.causal_epoch,
            "schema_ready": self.causal_command_service is not None,
            "watermarks": (
                self.causal_command_service.watermarks_snapshot()
                if self.causal_command_service is not None
                else None
            ),
        }
        metrics["core_status"] = metrics.get("status")
        metrics["core_materialization_lag_legacy"] = int(metrics.get("materialization_lag") or 0)
        if self.persistence_writer is not None:
            writer = self.persistence_writer.metrics_snapshot()
            writer_status = str(writer.get("status") or "UNKNOWN").upper()
            writer_lag = int(writer.get("materialization_lag") or 0)
            metrics["persistence_writer"] = writer
            metrics["materializer"] = {
                "status": writer_status,
                "materialization_lag": writer_lag,
                "materialized_seq": int(writer.get("critical_materialized_seq") or writer.get("materialized_seq") or 0),
                "blocked": (writer.get("critical_sink") or {}).get("blocked"),
            }
            writer_healthy = writer_status not in {"HALTED", "DEGRADED", "STOPPED"}
            if not writer_healthy:
                metrics["status"] = writer_status
            metrics["watcher_restart_allowed"] = bool(
                metrics.get("watcher_restart_allowed")
                and writer_healthy
                and writer_lag < 1000
            )
            if not metrics["watcher_restart_allowed"]:
                metrics["watcher_restart_gate_reason"] = (
                    "persistence_sink_unhealthy_or_lagging"
                    if not writer_healthy or writer_lag >= 1000
                    else metrics.get("watcher_restart_gate_reason")
                )
        metrics["watcher_restart_count"] = int(self.watcher_restart_count)
        metrics["orderbook_cache"] = self.orderbook_cache_metrics_snapshot()
        metrics["event_outbox"] = self.event_outbox.metrics_snapshot()
        metrics["fast_broadcast"] = {
            "spot": getattr(getattr(self, "order_service", None), "fast_broadcast_metrics", lambda: {})(),
            "perp": getattr(getattr(self, "contract_service", None), "fast_broadcast_metrics", lambda: {})(),
        }
        metrics["ws"] = self.ws.metrics_snapshot()
        core_latency = metrics.get("latency") if isinstance(metrics.get("latency"), dict) else {}
        runtime_latency = self.quote_latency.snapshot()
        # Keep core queue/engine/QuotePatch stages alongside runtime
        # planner/publisher/WS stages in one diagnostics payload.
        metrics["latency"] = {**core_latency, **runtime_latency}
        metrics["quote_pipeline"] = {
            "reference_stale_ms": int(settings.quote_reference_stale_ms),
            "head_interval_ms": int(settings.quote_head_interval_ms),
            "near_interval_ms": int(settings.quote_near_interval_ms),
            "full_interval_ms": int(settings.quote_full_interval_ms),
            "aging_interval_ms": int(settings.quote_aging_interval_ms),
            "ws_delta_flush_ms": int(settings.quote_ws_delta_flush_ms),
            "snapshot_heartbeat_ms": int(settings.quote_snapshot_heartbeat_ms),
            "stp_same_account_mode": str(settings.stp_same_account_mode),
            "stp_bot_cross_mode": str(settings.stp_bot_cross_mode),
        }
        return metrics
