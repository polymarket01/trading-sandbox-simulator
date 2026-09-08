from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def state_hash(value: object) -> str:
    return sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotEnvelope:
    snapshot_seq: int
    created_at_ms: int
    state: dict[str, Any]
    snapshot_hash: str
    path: str
    schema_version: int = 1
    epoch: str = "legacy"
    watermarks: dict[str, Any] | None = None
    committed: bool = True


class StateSnapshotService:
    """Crash-safe local snapshot store used by the single-process sandbox."""

    def __init__(self, directory: str | Path, *, keep: int = 10, max_total_bytes: int = 50 * 1024 * 1024) -> None:
        self.directory = Path(directory)
        self.keep = max(1, int(keep))
        self.max_total_bytes = max(1, int(max_total_bytes))

    def write_snapshot(
        self,
        state: dict[str, Any],
        *,
        snapshot_seq: int,
        created_at_ms: int,
        config_version: str = "runtime",
        epoch: str = "legacy",
        watermarks: dict[str, Any] | None = None,
        committed: bool = True,
    ) -> SnapshotEnvelope:
        payload = {
            "schema_version": 2,
            "snapshot_seq": int(snapshot_seq),
            "created_at_ms": int(created_at_ms),
            "config_version": str(config_version),
            "epoch": str(epoch or "legacy"),
            "watermarks": dict(watermarks or {}),
            "committed": bool(committed),
            "state": state,
        }
        digest = state_hash(payload)
        document = {**payload, "snapshot_hash": digest}
        self.directory.mkdir(parents=True, exist_ok=True)
        final_path = self.directory / f"exchange-{int(snapshot_seq):020d}.json"
        fd, temporary_name = tempfile.mkstemp(prefix="exchange-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, final_path)
            self._fsync_directory()
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        self._prune_old()
        return SnapshotEnvelope(
            snapshot_seq=int(snapshot_seq),
            created_at_ms=int(created_at_ms),
            state=state,
            snapshot_hash=digest,
            path=str(final_path),
            schema_version=2,
            epoch=str(epoch or "legacy"),
            watermarks=dict(watermarks or {}),
            committed=bool(committed),
        )

    def load_latest_valid(self) -> SnapshotEnvelope | None:
        if not self.directory.exists():
            return None
        candidates = sorted(self.directory.glob("exchange-*.json"), reverse=True)
        for path in candidates:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                expected = str(document.get("snapshot_hash") or "")
                schema_version = int(document.get("schema_version") or 1)
                keys = ["schema_version", "snapshot_seq", "created_at_ms", "config_version", "state"]
                if schema_version >= 2:
                    keys = [
                        "schema_version",
                        "snapshot_seq",
                        "created_at_ms",
                        "config_version",
                        "epoch",
                        "watermarks",
                        "committed",
                        "state",
                    ]
                body = {key: document[key] for key in keys}
                if not expected or state_hash(body) != expected:
                    continue
                state = body.get("state")
                if not isinstance(state, dict):
                    continue
                if schema_version >= 2 and not bool(body.get("committed", False)):
                    continue
                return SnapshotEnvelope(
                    snapshot_seq=int(body["snapshot_seq"]),
                    created_at_ms=int(body["created_at_ms"]),
                    state=state,
                    snapshot_hash=expected,
                    path=str(path),
                    schema_version=schema_version,
                    epoch=str(body.get("epoch") or "legacy"),
                    watermarks=dict(body.get("watermarks") or {}),
                    committed=bool(body.get("committed", True)),
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return None

    def verify(self, envelope: SnapshotEnvelope) -> bool:
        if envelope.schema_version >= 2:
            body = {
                "schema_version": envelope.schema_version,
                "snapshot_seq": envelope.snapshot_seq,
                "created_at_ms": envelope.created_at_ms,
                "config_version": "runtime",
                "epoch": envelope.epoch,
                "watermarks": dict(envelope.watermarks or {}),
                "committed": bool(envelope.committed),
                "state": envelope.state,
            }
        else:
            body = {
                "schema_version": 1,
                "snapshot_seq": envelope.snapshot_seq,
                "created_at_ms": envelope.created_at_ms,
                "config_version": "runtime",
                "state": envelope.state,
            }
        return bool(envelope.committed) and state_hash(body) == envelope.snapshot_hash

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _prune_old(self) -> None:
        candidates = sorted(self.directory.glob("exchange-*.json"), reverse=True)
        for path in candidates[self.keep :]:
            try:
                path.unlink()
            except OSError:
                pass
        total = 0
        for path in sorted(self.directory.glob("exchange-*.json"), reverse=True):
            try:
                size = int(path.stat().st_size)
            except OSError:
                continue
            if total + size <= self.max_total_bytes or path == candidates[0]:
                total += size
                continue
            try:
                path.unlink()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class JournalTruncationGate:
    """All evidence required before deleting an event-log prefix."""

    snapshot_seq: int
    critical_materializer_seq: int
    outbox_receipt_seq: int
    reconciliation_passed: bool
    snapshot_hash_verified: bool
    backup_confirmed: bool

    def check(self, event_seq: int) -> tuple[bool, list[str]]:
        target = int(event_seq)
        reasons: list[str] = []
        if self.snapshot_seq < target:
            reasons.append("snapshot_seq_behind")
        if self.critical_materializer_seq < target:
            reasons.append("critical_materializer_behind")
        if self.outbox_receipt_seq < target:
            reasons.append("outbox_receipt_behind")
        if not self.reconciliation_passed:
            reasons.append("reconciliation_not_passed")
        if not self.snapshot_hash_verified:
            reasons.append("snapshot_hash_not_verified")
        if not self.backup_confirmed:
            reasons.append("backup_not_confirmed")
        return not reasons, reasons
