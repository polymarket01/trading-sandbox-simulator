from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from json import loads
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.causal_chain import (
    CausalCommandJournal,
    CausalExecutionBundleRecord,
    CausalWatermark,
)
from app.services.causal_contracts import (
    CausalWatermarks,
    CommandEnvelope,
    CommandReceipt,
    ExecutionBundle,
    SettlementBundle,
    ack_stage_for_status,
    canonical_json,
    canonicalize_result,
)


_IN_FLIGHT = ("RECEIVED", "JOURNALED", "EXECUTED", "SETTLED")


class CausalCommandService:
    """Durable idempotency and execution-bundle boundary for the Python core."""

    def __init__(
        self,
        session_factory,
        *,
        run_id: str,
        epoch: str,
        mode: str = "legacy",
        sequence_start: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self.run_id = str(run_id)
        self.epoch = str(epoch)
        self.mode = str(mode or "legacy").lower()
        self._sequence = int(sequence_start)
        self._execution_sequence = int(sequence_start)
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._watermarks = CausalWatermarks(epoch=self.epoch, status="STARTING")

    async def allocate_command_sequence(self) -> int:
        async with self._lock:
            self._sequence += 1
            self._watermarks = CausalWatermarks(
                **{**self._watermarks.as_dict(), "command_sequence": self._sequence, "ingress_sequence": self._sequence}
            )
            return self._sequence

    async def begin(
        self,
        envelope: CommandEnvelope,
        *,
        _attempt: int = 0,
    ) -> tuple[CommandReceipt, bool]:
        """Journal intent before invoking matcher/risk/settlement code.

        The boolean is ``True`` only for a newly inserted command.  A retry
        with the same fingerprint receives the stored receipt; a different
        fingerprint is an explicit conflict.
        """
        for attempt in range(_attempt, 8):
            try:
                return await self._begin_once(envelope)
            except Exception as exc:
                if "database is locked" not in str(exc).lower() or attempt >= 7:
                    raise
                # _begin_once has released both its session and _db_lock.
                # Recursively calling begin while holding that lock deadlocked
                # the entire public command journal after the first busy error.
                await asyncio.sleep(min(2.0, 0.05 * (2**attempt)))
        raise RuntimeError("causal command journal retry exhausted before execution")

    async def _begin_once(self, envelope: CommandEnvelope) -> tuple[CommandReceipt, bool]:
        now = datetime.now(tz=UTC)
        async with self._db_lock, self._session_factory() as session:
            existing = await session.get(CausalCommandJournal, envelope.command_id)
            if existing is not None:
                if existing.request_fingerprint != envelope.request_fingerprint:
                    return self._receipt_from_row(existing, status="IDEMPOTENCY_CONFLICT"), False
                return self._receipt_from_row(existing), False
            row = CausalCommandJournal(
                command_id=envelope.command_id,
                request_fingerprint=envelope.request_fingerprint,
                client_order_id=envelope.client_order_id,
                command_type=envelope.command_type,
                account_id=envelope.account_id,
                account_domain=envelope.account_domain,
                symbol=envelope.symbol,
                market_id=envelope.market_id,
                product_type=envelope.product_type,
                epoch=envelope.epoch,
                command_sequence=envelope.command_sequence,
                logical_timestamp=envelope.logical_timestamp,
                rules_version=envelope.rules_version,
                risk_version=envelope.risk_version,
                fee_version=envelope.fee_version,
                config_version=envelope.config_version,
                strategy_instance=envelope.strategy_instance,
                generation=envelope.generation,
                priority_class=envelope.priority_class,
                payload_hash=envelope.payload_hash,
                payload_json=canonical_json(envelope.payload),
                status="JOURNALED",
                ack_stage="JOURNALED",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.flush()
                await self._upsert_watermark(session, command_sequence=envelope.command_sequence, status="HEALTHY")
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(CausalCommandJournal, envelope.command_id)
                if existing is None:
                    raise
                if existing.request_fingerprint != envelope.request_fingerprint:
                    return self._receipt_from_row(existing, status="IDEMPOTENCY_CONFLICT"), False
                return self._receipt_from_row(existing), False
        return self._receipt_from_row(row), True

    async def complete(self, envelope: CommandEnvelope, result: dict, **kwargs) -> CommandReceipt:
        """Persist an actual bundle with bounded SQLite busy retries."""
        for attempt in range(8):
            try:
                return await self._complete_once(envelope, result, **kwargs)
            except Exception as exc:
                if "database is locked" not in str(exc).lower() or attempt >= 7:
                    raise
                await asyncio.sleep(min(2.0, 0.05 * (2**attempt)))
        raise RuntimeError("causal execution commit retry exhausted")

    async def _complete_once(
        self,
        envelope: CommandEnvelope,
        result: dict,
        *,
        state_hash_before: str | None = None,
        state_hash_after: str | None = None,
        priority_sequence: int = 0,
        book_sequence: int = 0,
        settlement: SettlementBundle | None = None,
        response: dict | None = None,
        status: str = "DURABLE",
    ) -> CommandReceipt:
        async with self._lock:
            self._execution_sequence += 1
            execution_sequence = self._execution_sequence
        execution_id = f"exec-{envelope.command_id}-{execution_sequence}"
        bundle = ExecutionBundle.from_result(
            command=envelope,
            execution_id=execution_id,
            result=result,
            state_hash_before=state_hash_before,
            state_hash_after=state_hash_after,
            priority_sequence=priority_sequence,
            book_sequence=book_sequence,
            execution_sequence=execution_sequence,
            event_sequence=execution_sequence,
            settlement=settlement,
        )
        now = datetime.now(tz=UTC)
        stored_response = dict(response if response is not None else result)
        normalized_status = str(status or "DURABLE").upper()
        ack_stage = ack_stage_for_status(normalized_status)
        async with self._db_lock, self._session_factory() as session:
            row = await session.get(CausalCommandJournal, envelope.command_id)
            if row is None:
                raise RuntimeError(f"causal command was not journaled: {envelope.command_id}")
            if row.request_fingerprint != envelope.request_fingerprint:
                return self._receipt_from_row(row, status="IDEMPOTENCY_CONFLICT")
            existing_execution = await session.scalar(
                select(CausalExecutionBundleRecord).where(
                    CausalExecutionBundleRecord.command_id == envelope.command_id
                )
            )
            if existing_execution is not None:
                return self._receipt_from_row(row)
            session.add(
                CausalExecutionBundleRecord(
                    execution_id=execution_id,
                    command_id=envelope.command_id,
                    epoch=envelope.epoch,
                    execution_sequence=execution_sequence,
                    priority_sequence=int(priority_sequence),
                    book_sequence=int(book_sequence),
                    event_sequence=execution_sequence,
                    accepted=bundle.accepted,
                    rejected=bundle.rejected,
                    result_hash=bundle.result_hash,
                    bundle_json=canonical_json(bundle.as_dict()),
                    state=normalized_status,
                    created_at=now,
                    durable_at=now,
                )
            )
            row.execution_id = execution_id
            row.result_hash = bundle.result_hash
            row.status = normalized_status
            row.ack_stage = ack_stage
            row.response_json = canonical_json(canonicalize_result(stored_response))
            row.updated_at = now
            await self._upsert_watermark(
                session,
                command_sequence=envelope.command_sequence,
                execution_sequence=execution_sequence,
                durable_sequence=execution_sequence,
                matched_sequence=int(priority_sequence or 0),
                published_sequence=int(book_sequence or 0) if int(book_sequence or 0) > 0 else None,
                status="HEALTHY",
            )
            await session.commit()
        async with self._lock:
            self._watermarks = CausalWatermarks(
                **{
                    **self._watermarks.as_dict(),
                    "execution_sequence": max(self._watermarks.execution_sequence, execution_sequence),
                    "settlement_sequence": max(self._watermarks.settlement_sequence, execution_sequence),
                    "durable_sequence": max(self._watermarks.durable_sequence, execution_sequence),
                    "matched_sequence": max(self._watermarks.matched_sequence, int(priority_sequence or 0)),
                    "published_sequence": max(self._watermarks.published_sequence, int(book_sequence or 0)),
                    "status": "HEALTHY",
                }
            )
        return CommandReceipt(
            command_id=envelope.command_id,
            request_fingerprint=envelope.request_fingerprint,
            status=normalized_status,
            ack_stage=ack_stage,
            epoch=envelope.epoch,
            command_sequence=envelope.command_sequence,
            execution_id=execution_id,
            result_hash=bundle.result_hash,
            response=stored_response,
            watermarks=self._watermarks,
        )

    async def reject(
        self,
        envelope: CommandEnvelope,
        *,
        code: str,
        stage: str,
        reason: str,
        response: dict | None = None,
    ) -> CommandReceipt:
        now = datetime.now(tz=UTC)
        async with self._db_lock, self._session_factory() as session:
            row = await session.get(CausalCommandJournal, envelope.command_id)
            if row is None:
                raise RuntimeError(f"causal command was not journaled: {envelope.command_id}")
            row.status = "REJECTED"
            row.ack_stage = "REJECTED"
            row.reject_code = str(code)
            row.reject_stage = str(stage)
            row.response_json = canonical_json(response or {"error": reason})
            row.updated_at = now
            await self._upsert_watermark(session, command_sequence=envelope.command_sequence, status="HEALTHY")
            await session.commit()
        return CommandReceipt(
            command_id=envelope.command_id,
            request_fingerprint=envelope.request_fingerprint,
            status="REJECTED",
            ack_stage="REJECTED",
            epoch=envelope.epoch,
            command_sequence=envelope.command_sequence,
            reject_code=str(code),
            reject_stage=str(stage),
            response=response or {"error": reason},
            watermarks=self._watermarks,
        )

    async def mark_unknown(self, command_id: str, *, status: str = "UNKNOWN_TIMEOUT", reason: str) -> CommandReceipt | None:
        now = datetime.now(tz=UTC)
        async with self._db_lock, self._session_factory() as session:
            row = await session.get(CausalCommandJournal, str(command_id))
            if row is None:
                return None
            if row.status in {"DURABLE", "PUBLISHED", "REJECTED"}:
                return self._receipt_from_row(row)
            row.status = str(status)
            row.ack_stage = str(status)
            row.unknown_reason = str(reason)[:512]
            row.updated_at = now
            await session.commit()
            return self._receipt_from_row(row)

    async def recover_inflight(self, *, reason: str = "process restarted before durable final state") -> int:
        now = datetime.now(tz=UTC)
        async with self._db_lock, self._session_factory() as session:
            result = await session.execute(
                update(CausalCommandJournal)
                .where(CausalCommandJournal.status.in_(_IN_FLIGHT))
                .values(
                    status="UNKNOWN_AFTER_RESTART",
                    ack_stage="UNKNOWN_AFTER_RESTART",
                    unknown_reason=str(reason)[:512],
                    updated_at=now,
                )
            )
            await session.commit()
            recovered = int(result.rowcount or 0)
        async with self._lock:
            self._watermarks = CausalWatermarks(
                **{**self._watermarks.as_dict(), "status": "HEALTHY", "last_error": None}
            )
        return recovered

    async def get(self, command_id: str) -> CommandReceipt | None:
        async with self._db_lock, self._session_factory() as session:
            row = await session.get(CausalCommandJournal, str(command_id))
            return self._receipt_from_row(row) if row is not None else None

    async def get_for_account(self, command_id: str, account_id: int) -> CommandReceipt | None:
        async with self._db_lock, self._session_factory() as session:
            row = await session.scalar(
                select(CausalCommandJournal).where(
                    CausalCommandJournal.command_id == str(command_id),
                    CausalCommandJournal.account_id == int(account_id),
                )
            )
            return self._receipt_from_row(row) if row is not None else None

    async def mark_published(self, command_id: str, *, book_sequence: int) -> CommandReceipt | None:
        now = datetime.now(tz=UTC)
        async with self._db_lock, self._session_factory() as session:
            row = await session.get(CausalCommandJournal, str(command_id))
            if row is None:
                return None
            row.status = "PUBLISHED"
            row.ack_stage = "PUBLISHED"
            row.updated_at = now
            execution = await session.scalar(
                select(CausalExecutionBundleRecord).where(
                    CausalExecutionBundleRecord.command_id == str(command_id)
                )
            )
            if execution is not None:
                execution.state = "PUBLISHED"
                execution.published_at = now
            await self._upsert_watermark(
                session,
                published_sequence=int(book_sequence),
                status="HEALTHY",
            )
            await session.commit()
            return self._receipt_from_row(row)

    def watermarks_snapshot(self) -> dict:
        return {**self._watermarks.as_dict(),
                "sequence_semantics": "maximum observed per named clock; not a contiguous commit frontier",
                "replay_scope": "command receipts and execution bundles; no automatic matching replay"}

    async def _upsert_watermark(
        self,
        session,
        *,
        command_sequence: int | None = None,
        matched_sequence: int | None = None,
        execution_sequence: int | None = None,
        durable_sequence: int | None = None,
        published_sequence: int | None = None,
        status: str | None = None,
    ) -> None:
        row = await session.get(CausalWatermark, 1)
        now = datetime.now(tz=UTC)
        if row is None:
            row = CausalWatermark(
                id=1,
                run_id=self.run_id,
                epoch=self.epoch,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
        row.run_id = self.run_id
        row.epoch = self.epoch
        if command_sequence is not None:
            row.ingress_sequence = max(int(row.ingress_sequence), int(command_sequence))
            row.command_sequence = max(int(row.command_sequence), int(command_sequence))
        if matched_sequence is not None:
            row.matched_sequence = max(int(row.matched_sequence), int(matched_sequence))
            row.priority_sequence = max(int(row.priority_sequence), int(matched_sequence))
        if execution_sequence is not None:
            row.execution_sequence = max(int(row.execution_sequence), int(execution_sequence))
            row.settlement_sequence = max(int(row.settlement_sequence), int(execution_sequence))
        if durable_sequence is not None:
            row.durable_sequence = max(int(row.durable_sequence), int(durable_sequence))
        if published_sequence is not None:
            row.published_sequence = max(int(row.published_sequence), int(published_sequence))
        if status is not None:
            row.status = str(status)
        row.updated_at = now

    def _receipt_from_row(self, row: CausalCommandJournal, *, status: str | None = None) -> CommandReceipt:
        response = None
        if row.response_json:
            try:
                response = loads(row.response_json)
            except (TypeError, ValueError):
                response = None
        resolved_status = str(status or row.status)
        return CommandReceipt(
            command_id=str(row.command_id),
            request_fingerprint=str(row.request_fingerprint),
            status=resolved_status,
            ack_stage=ack_stage_for_status(resolved_status),
            epoch=str(row.epoch),
            command_sequence=int(row.command_sequence),
            execution_id=str(row.execution_id) if row.execution_id else None,
            result_hash=str(row.result_hash) if row.result_hash else None,
            reject_code=str(row.reject_code) if row.reject_code else None,
            reject_stage=str(row.reject_stage) if row.reject_stage else None,
            response=response,
            watermarks=self._watermarks,
        )
