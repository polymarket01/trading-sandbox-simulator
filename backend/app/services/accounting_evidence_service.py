from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.accounting_evidence_run import AccountingEvidenceRun
from app.services.accounting_proof_service import (
    create_accounting_proof_checkpoint,
    serialize_accounting_proof,
)
from app.services.reconciliation_service import ReconciliationService
from app.services.robot_financial_checkpoint_service import (
    create_robot_financial_checkpoint,
    serialize_robot_checkpoint,
)


ACCOUNTING_EVIDENCE_RUN_LOCK = asyncio.Lock()
DEFAULT_STALE_AFTER_SECONDS = 15 * 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def serialize_accounting_evidence_run(row: AccountingEvidenceRun) -> dict:
    return {
        "run_key": row.run_key,
        "observation_date": row.observation_date.isoformat(),
        "status": row.status,
        "attempt_count": row.attempt_count,
        "reconciliation_run_id": row.reconciliation_run_id,
        "accounting_proof_checkpoint_id": row.accounting_proof_checkpoint_id,
        "robot_checkpoint_id": row.robot_checkpoint_id,
        "started_at": _as_utc(row.started_at).isoformat(),
        "completed_at": _as_utc(row.completed_at).isoformat() if row.completed_at else None,
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "detail": row.detail_json,
        "created_at": _as_utc(row.created_at).isoformat(),
        "updated_at": _as_utc(row.updated_at).isoformat(),
    }


async def list_accounting_evidence_runs(
    session: AsyncSession,
    *,
    limit: int = 30,
    offset: int = 0,
) -> dict:
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    total = int(await session.scalar(select(func.count()).select_from(AccountingEvidenceRun)) or 0)
    rows = (await session.execute(
        select(AccountingEvidenceRun)
        .order_by(AccountingEvidenceRun.observation_date.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    )).scalars().all()
    return {
        "items": [serialize_accounting_evidence_run(row) for row in rows],
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


async def latest_accounting_evidence_run(session: AsyncSession) -> dict | None:
    row = await session.scalar(
        select(AccountingEvidenceRun).order_by(AccountingEvidenceRun.observation_date.desc()).limit(1)
    )
    return serialize_accounting_evidence_run(row) if row else None


async def _mark_attempt_started(
    session: AsyncSession,
    *,
    observation_date: date,
    now: datetime,
    stale_after_seconds: int,
) -> tuple[AccountingEvidenceRun, bool, str]:
    run_key = f"daily-evidence-{observation_date.isoformat()}"
    row = await session.scalar(
        select(AccountingEvidenceRun).where(AccountingEvidenceRun.observation_date == observation_date)
    )
    if row is not None and row.status == "passed":
        return row, False, "already_passed"
    if row is not None and row.status == "running":
        started_at = _as_utc(row.started_at)
        if now - started_at < timedelta(seconds=max(1, stale_after_seconds)):
            return row, False, "already_running"
    if row is None:
        row = AccountingEvidenceRun(
            run_key=run_key,
            observation_date=observation_date,
            status="running",
            attempt_count=1,
            started_at=now,
            completed_at=None,
            last_error_code=None,
            last_error_message=None,
            detail_json={"automatic_repair": False, "source_of_truth": False},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "running"
        row.attempt_count += 1
        row.started_at = now
        row.completed_at = None
        row.last_error_code = None
        row.last_error_message = None
        row.updated_at = now
    await session.commit()
    return row, True, "started"


async def run_daily_accounting_evidence(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    database_url: str | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict:
    """Capture at most one durable full evidence bundle per UTC date.

    The run marker is committed first so process death remains visible. The
    reconciliation, immutable accounting proof, robot checkpoint and success
    link are then committed atomically in one SQLite write window.
    """
    observed_at = _as_utc(now or datetime.now(tz=UTC))
    observation_date = observed_at.date()
    async with ACCOUNTING_EVIDENCE_RUN_LOCK:
        row, should_run, action = await _mark_attempt_started(
            session,
            observation_date=observation_date,
            now=observed_at,
            stale_after_seconds=stale_after_seconds,
        )
        if not should_run:
            return {"action": action, "created": False, "item": serialize_accounting_evidence_run(row)}

        try:
            active_database_url = database_url or settings.database_url
            if active_database_url.startswith("sqlite"):
                await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.scalar(
                select(AccountingEvidenceRun).where(
                    AccountingEvidenceRun.observation_date == observation_date
                )
            )
            summary = await ReconciliationService().run(session, scope="full", commit=False)
            proof, proof_created = await create_accounting_proof_checkpoint(
                session,
                reconciliation_summary=summary,
                idempotency_key=f"daily-accounting-proof:{observation_date.isoformat()}",
            )
            robot_checkpoint, robot_created = await create_robot_financial_checkpoint(
                session,
                reconciliation_summary=summary,
                accounting_proof=proof,
                idempotency_key=f"daily-robot-checkpoint:{observation_date.isoformat()}",
            )
            completed_at = datetime.now(tz=UTC)
            passed = (
                int(summary.get("blocking_count") or 0) == 0
                and proof.status == "passed"
                and robot_checkpoint.status == "passed"
            )
            row.status = "passed" if passed else "failed"
            row.reconciliation_run_id = str(summary["run_id"])
            row.accounting_proof_checkpoint_id = proof.checkpoint_id
            row.robot_checkpoint_id = robot_checkpoint.checkpoint_id
            row.completed_at = completed_at
            row.last_error_code = None if passed else "evidence_invariant_failed"
            row.last_error_message = None if passed else "immutable evidence was committed with failed status"
            row.detail_json = {
                "automatic_repair": False,
                "source_of_truth": False,
                "blocking_count": int(summary.get("blocking_count") or 0),
                "difference_count": int(summary.get("difference_count") or 0),
                "proof_created": proof_created,
                "robot_checkpoint_created": robot_created,
                "proof_status": proof.status,
                "robot_checkpoint_status": robot_checkpoint.status,
                "robot_source_count": int((robot_checkpoint.counts_json or {}).get("source_count") or 0),
            }
            row.updated_at = completed_at
            await session.commit()
            return {
                "action": "captured" if passed else "captured_failed_evidence",
                "created": True,
                "item": serialize_accounting_evidence_run(row),
                "proof": serialize_accounting_proof(proof),
                "robot_checkpoint": serialize_robot_checkpoint(robot_checkpoint),
            }
        except Exception as exc:
            await session.rollback()
            failure_at = datetime.now(tz=UTC)
            failed_row = await session.scalar(
                select(AccountingEvidenceRun).where(
                    AccountingEvidenceRun.observation_date == observation_date
                )
            )
            if failed_row is not None:
                failed_row.status = "failed"
                failed_row.completed_at = failure_at
                failed_row.last_error_code = exc.__class__.__name__
                failed_row.last_error_message = str(exc)[:500]
                failed_row.detail_json = {
                    "automatic_repair": False,
                    "source_of_truth": False,
                    "exception_class": exc.__class__.__name__,
                }
                failed_row.updated_at = failure_at
                await session.commit()
            raise
