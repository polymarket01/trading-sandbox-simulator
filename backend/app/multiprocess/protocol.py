from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from queue import Empty, Full
import time
from typing import Any
from uuid import uuid4


IPC_SCHEMA_VERSION = 1
REQUIRED_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "market_id",
        "stream_epoch",
        "command_id",
        "request_fingerprint",
        "command_sequence",
        "priority_sequence",
        "sent_at",
        "deadline",
        "kind",
        "payload",
    }
)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def canonical_fingerprint(*, market_id: str, kind: str, payload: dict[str, Any]) -> str:
    """Fingerprint the immutable request body, excluding retry metadata."""

    body = json.dumps(
        {
            "schema_version": IPC_SCHEMA_VERSION,
            "market_id": str(market_id).upper(),
            "kind": str(kind).upper(),
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IPCEnvelope:
    """Binary-transport envelope used by every internal IPC queue.

    ``multiprocessing.Queue`` serializes this slots dataclass as a compact
    binary frame.  JSON is reserved for the external REST/WS and low-rate UDS
    control edges; the high-frequency process boundary never re-encodes every
    book frame as JSON.
    """

    schema_version: int
    run_id: str
    market_id: str
    stream_epoch: str
    command_id: str
    request_fingerprint: str
    command_sequence: int
    priority_sequence: int
    sent_at: int
    deadline: int
    kind: str
    payload: dict[str, Any]

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        market_id: str,
        stream_epoch: str,
        kind: str,
        payload: dict[str, Any],
        command_sequence: int,
        priority_sequence: int | None = None,
        command_id: str | None = None,
        sent_at: int | None = None,
        deadline: int | None = None,
    ) -> "IPCEnvelope":
        timestamp = now_ms() if sent_at is None else int(sent_at)
        normalized_market = str(market_id).upper()
        normalized_kind = str(kind).upper()
        body = dict(payload)
        return cls(
            schema_version=IPC_SCHEMA_VERSION,
            run_id=str(run_id),
            market_id=normalized_market,
            stream_epoch=str(stream_epoch),
            command_id=str(command_id or uuid4()),
            request_fingerprint=canonical_fingerprint(
                market_id=normalized_market,
                kind=normalized_kind,
                payload=body,
            ),
            command_sequence=max(0, int(command_sequence)),
            priority_sequence=max(
                0,
                int(command_sequence if priority_sequence is None else priority_sequence),
            ),
            sent_at=timestamp,
            deadline=int(deadline if deadline is not None else timestamp + 2_000),
            kind=normalized_kind,
            payload=body,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self, *, expected_run_id: str | None = None) -> None:
        if int(self.schema_version) != IPC_SCHEMA_VERSION:
            raise ValueError(f"unsupported IPC schema version: {self.schema_version}")
        if expected_run_id is not None and self.run_id != expected_run_id:
            raise ValueError("run_id mismatch")
        if not self.market_id or not self.command_id or not self.request_fingerprint:
            raise ValueError("missing IPC identity")
        expected = canonical_fingerprint(
            market_id=self.market_id,
            kind=self.kind,
            payload=self.payload,
        )
        if expected != self.request_fingerprint:
            raise ValueError("request fingerprint mismatch")
        if self.command_sequence < 0 or self.priority_sequence < 0:
            raise ValueError("negative IPC sequence")
        if self.deadline < self.sent_at:
            raise ValueError("deadline precedes sent_at")


@dataclass(frozen=True, slots=True)
class PublishedBookEvent:
    run_id: str
    market_id: str
    stream_epoch: str
    stream_id: str
    seq: int
    previous_seq: int
    published_at: int
    snapshot: bool
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    engine_version: int


@dataclass(frozen=True, slots=True)
class ReliableWorkerEvent:
    run_id: str
    market_id: str
    stream_epoch: str
    event_type: str
    command_id: str
    request_fingerprint: str
    payload: dict[str, Any]


class TrackedMPQueue:
    """Portable bounded multiprocessing queue with a shared numeric depth.

    macOS does not implement ``sem_getvalue``, so the standard ``qsize`` is
    unavailable.  This wrapper shares only an atomic integer for metrics; no
    engine/book/account Python object is shared between processes.
    """

    def __init__(self, context: Any, *, maxsize: int) -> None:
        self.maxsize = max(1, int(maxsize))
        self._queue = context.Queue(maxsize=self.maxsize)
        # Metrics-only approximate depth.  A process can be SIGKILLed while
        # touching this counter; a process-shared mutex would then become a
        # non-robust orphan and could stall unrelated producers/consumers.
        # Queue capacity is enforced by multiprocessing.Queue's own bounded
        # semaphore, never by this lock-free diagnostic counter.
        self._depth = context.Value("q", 0, lock=False)

    def put(self, value: Any, block: bool = True, timeout: float | None = None) -> None:
        self._depth.value += 1
        try:
            if timeout is None:
                self._queue.put(value, block=block)
            else:
                self._queue.put(value, block=block, timeout=timeout)
        except BaseException:
            self._depth.value = max(0, self._depth.value - 1)
            raise

    def put_nowait(self, value: Any) -> None:
        self.put(value, block=False)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        if timeout is None:
            value = self._queue.get(block=block)
        else:
            value = self._queue.get(block=block, timeout=timeout)
        self._depth.value = max(0, self._depth.value - 1)
        return value

    def get_nowait(self) -> Any:
        return self.get(block=False)

    def qsize(self) -> int:
        return max(0, int(self._depth.value))

    def empty(self) -> bool:
        return self.qsize() <= 0

    def full(self) -> bool:
        return self.qsize() >= self.maxsize

    def close(self) -> None:
        self._queue.close()

    def join_thread(self) -> None:
        self._queue.join_thread()


def validate_envelope_dict(value: dict[str, Any]) -> IPCEnvelope:
    missing = REQUIRED_ENVELOPE_FIELDS - set(value)
    if missing:
        raise ValueError(f"missing IPC fields: {sorted(missing)}")
    envelope = IPCEnvelope(**{key: value[key] for key in REQUIRED_ENVELOPE_FIELDS})
    envelope.validate()
    return envelope


def put_latest(queue: Any, value: Any, *, max_attempts: int = 4) -> int:
    """Bounded latest-wins put; returns the number of discarded frames.

    A killed multiprocessing consumer can leave a pipe frame temporarily
    unavailable even though its semaphore reports Full.  Latest-only traffic
    must never spin forever at that edge: after a few non-blocking attempts the
    new value itself is counted as dropped and the producer continues.
    """

    dropped = 0
    for attempt in range(max(1, int(max_attempts))):
        try:
            queue.put_nowait(value)
            return dropped
        except Full:
            try:
                queue.get_nowait()
                dropped += 1
            except Empty:
                if attempt + 1 < max_attempts:
                    # macOS feeder timing can report Full before the item is
                    # visible. Yield briefly, but retain a hard time bound.
                    time.sleep(0.001)
    return dropped + 1


def drain_latest(queue: Any) -> tuple[Any | None, int]:
    latest = None
    count = 0
    while True:
        try:
            latest = queue.get_nowait()
            count += 1
        except Empty:
            return latest, max(0, count - 1)
