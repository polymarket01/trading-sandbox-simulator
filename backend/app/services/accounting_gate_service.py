from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.accounting_proof import AccountingProofCheckpoint
from app.models.accounting_evidence_run import AccountingEvidenceRun
from app.models.reconciliation_run import ReconciliationRun
from app.models.robot_financial_checkpoint import RobotFinancialCheckpoint
from app.services.accounting_proof_service import verify_accounting_proof_chain
from app.services.robot_financial_checkpoint_service import verify_robot_financial_checkpoint_chain


DEFAULT_MINIMUM_OBSERVATION_HOURS = 48
DEFAULT_MINIMUM_DISTINCT_UTC_DATES = 3
DEFAULT_MINIMUM_ACCOUNTING_PROOFS = 3
DEFAULT_MINIMUM_NONEMPTY_ROBOT_CHECKPOINTS = 1


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _criterion(code: str, passed: bool, actual: object, required: object, evidence: str) -> dict:
    return {
        "code": code,
        "passed": passed,
        "actual": actual,
        "required": required,
        "evidence": evidence,
    }


async def accounting_gate_readiness(
    session: AsyncSession,
    *,
    minimum_observation_hours: int = DEFAULT_MINIMUM_OBSERVATION_HOURS,
    minimum_distinct_utc_dates: int = DEFAULT_MINIMUM_DISTINCT_UTC_DATES,
    minimum_accounting_proofs: int = DEFAULT_MINIMUM_ACCOUNTING_PROOFS,
    minimum_nonempty_robot_checkpoints: int = DEFAULT_MINIMUM_NONEMPTY_ROBOT_CHECKPOINTS,
) -> dict:
    """Return a read-only, machine-evaluable Gate E/F evidence summary."""
    proofs = (await session.execute(
        select(AccountingProofCheckpoint).order_by(AccountingProofCheckpoint.sequence_no)
    )).scalars().all()
    robot_checkpoints = (await session.execute(
        select(RobotFinancialCheckpoint).order_by(RobotFinancialCheckpoint.sequence_no)
    )).scalars().all()
    latest_reconciliation = await session.scalar(
        select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1)
    )
    latest_evidence_run = await session.scalar(
        select(AccountingEvidenceRun).order_by(AccountingEvidenceRun.observation_date.desc()).limit(1)
    )
    proof_verification = await verify_accounting_proof_chain(session)
    robot_verification = await verify_robot_financial_checkpoint_chain(session)

    last_failed_proof_sequence = max(
        (row.sequence_no for row in proofs if row.status != "passed"), default=0
    )
    last_failed_robot_sequence = max(
        (row.sequence_no for row in robot_checkpoints if row.status != "passed"), default=0
    )
    # Failed checkpoints are immutable evidence, not a permanent impossibility
    # result. Gate E starts a new observation epoch strictly after the latest
    # failure in each chain and requires the full multi-day proof window again.
    eligible_proofs = [row for row in proofs if row.sequence_no > last_failed_proof_sequence]
    eligible_robot_checkpoints = [
        row for row in robot_checkpoints if row.sequence_no > last_failed_robot_sequence
    ]
    proof_dates = sorted({_as_utc(row.completed_at).date().isoformat() for row in eligible_proofs})
    if eligible_proofs:
        first_completed = _as_utc(eligible_proofs[0].completed_at)
        last_completed = _as_utc(eligible_proofs[-1].completed_at)
        observation_hours = max(0.0, (last_completed - first_completed).total_seconds() / 3600)
    else:
        first_completed = None
        last_completed = None
        observation_hours = 0.0
    nonempty_robot = [
        row for row in eligible_robot_checkpoints
        if row.status == "passed" and int((row.counts_json or {}).get("source_count") or 0) > 0
    ]
    total_robot_source_count = sum(
        int((row.counts_json or {}).get("source_count") or 0)
        for row in eligible_robot_checkpoints
    )
    latest_proof = proofs[-1] if proofs else None

    criteria = [
        _criterion(
            "accounting_proof_chain_valid",
            bool(proofs) and proof_verification["invalid_count"] == 0,
            proof_verification["invalid_count"],
            0,
            "accounting_proof_checkpoints immutable hash chain",
        ),
        _criterion(
            "accounting_proof_count",
            len(eligible_proofs) >= minimum_accounting_proofs,
            len(eligible_proofs),
            minimum_accounting_proofs,
            "independent committed accounting proof checkpoints",
        ),
        _criterion(
            "all_accounting_proofs_passed",
            bool(eligible_proofs) and all(row.status == "passed" for row in eligible_proofs),
            sum(1 for row in eligible_proofs if row.status != "passed"),
            0,
            "only the consecutive recovery epoch after the latest immutable failure can satisfy Gate E",
        ),
        _criterion(
            "observation_span_hours",
            observation_hours >= minimum_observation_hours,
            round(observation_hours, 3),
            minimum_observation_hours,
            "elapsed UTC time between first and latest proof",
        ),
        _criterion(
            "distinct_utc_dates",
            len(proof_dates) >= minimum_distinct_utc_dates,
            len(proof_dates),
            minimum_distinct_utc_dates,
            "distinct UTC proof completion dates",
        ),
        _criterion(
            "latest_business_shadow_snapshot_match",
            bool(latest_proof)
            and latest_proof.status == "passed"
            and latest_proof.business_snapshot_hash == latest_proof.accounting_snapshot_hash,
            latest_proof.status if latest_proof else "missing",
            "passed_and_equal_hashes",
            "latest immutable business/accounting snapshot proof",
        ),
        _criterion(
            "robot_checkpoint_chain_valid",
            bool(robot_checkpoints) and robot_verification["invalid_count"] == 0,
            robot_verification["invalid_count"],
            0,
            "robot checkpoint manifest, retained Accounting and Outbox sources",
        ),
        _criterion(
            "all_robot_checkpoints_passed",
            bool(eligible_robot_checkpoints)
            and all(row.status == "passed" for row in eligible_robot_checkpoints),
            sum(1 for row in eligible_robot_checkpoints if row.status != "passed"),
            0,
            "only the consecutive robot recovery epoch after the latest immutable failure can satisfy Gate E",
        ),
        _criterion(
            "nonempty_robot_checkpoint_count",
            len(nonempty_robot) >= minimum_nonempty_robot_checkpoints,
            len(nonempty_robot),
            minimum_nonempty_robot_checkpoints,
            "robot checkpoints with source_count greater than zero",
        ),
        _criterion(
            "latest_reconciliation_nonblocking",
            bool(latest_reconciliation)
            and latest_reconciliation.scope == "full"
            and latest_reconciliation.blocking_count == 0
            and latest_reconciliation.allow_trading == "yes",
            {
                "run_id": latest_reconciliation.run_id if latest_reconciliation else None,
                "scope": latest_reconciliation.scope if latest_reconciliation else None,
                "blocking_count": latest_reconciliation.blocking_count if latest_reconciliation else None,
                "allow_trading": latest_reconciliation.allow_trading if latest_reconciliation else None,
            },
            {"scope": "full", "blocking_count": 0, "allow_trading": "yes"},
            "latest persisted full reconciliation",
        ),
        _criterion(
            "financial_pruning_remains_disabled",
            robot_verification["financial_pruning_authorized"] is False
            and robot_verification["raw_records_deleted"] == 0,
            {
                "authorized": robot_verification["financial_pruning_authorized"],
                "raw_records_deleted": robot_verification["raw_records_deleted"],
            },
            {"authorized": False, "raw_records_deleted": 0},
            "checkpoint proof does not authorize destructive retention",
        ),
    ]
    gate_e_ready = all(item["passed"] for item in criteria)
    return {
        "gate_e": {
            "status": "ready" if gate_e_ready else "collecting_evidence",
            "ready": gate_e_ready,
            "criteria": criteria,
            "observation": {
                "first_completed_at": first_completed.isoformat() if first_completed else None,
                "latest_completed_at": last_completed.isoformat() if last_completed else None,
                "observation_hours": round(observation_hours, 3),
                "distinct_utc_dates": proof_dates,
                "accounting_proof_count": len(eligible_proofs),
                "robot_checkpoint_count": len(eligible_robot_checkpoints),
                "nonempty_robot_checkpoint_count": len(nonempty_robot),
                "robot_source_count": total_robot_source_count,
                "recovery_epoch": {
                    "after_failed_accounting_sequence": last_failed_proof_sequence or None,
                    "after_failed_robot_sequence": last_failed_robot_sequence or None,
                    "historical_accounting_proof_count": len(proofs),
                    "historical_failed_accounting_proof_count": sum(
                        1 for row in proofs if row.status != "passed"
                    ),
                    "historical_robot_checkpoint_count": len(robot_checkpoints),
                    "historical_failed_robot_checkpoint_count": sum(
                        1 for row in robot_checkpoints if row.status != "passed"
                    ),
                },
            },
        },
        "gate_f": {
            "status": "closed",
            "source_of_truth_switch_allowed": False,
            "reason": "Gate E readiness is necessary but not sufficient; PostgreSQL migration, operational approval, rollback rehearsal, RBAC/maker-checker and external audit retention remain separate gates",
        },
        "evidence_collector": {
            "enabled": settings.accounting_evidence_auto_enabled,
            "latest": None if latest_evidence_run is None else {
                "run_key": latest_evidence_run.run_key,
                "observation_date": latest_evidence_run.observation_date.isoformat(),
                "status": latest_evidence_run.status,
                "attempt_count": latest_evidence_run.attempt_count,
                "reconciliation_run_id": latest_evidence_run.reconciliation_run_id,
                "accounting_proof_checkpoint_id": latest_evidence_run.accounting_proof_checkpoint_id,
                "robot_checkpoint_id": latest_evidence_run.robot_checkpoint_id,
                "last_error_code": latest_evidence_run.last_error_code,
                "completed_at": _as_utc(latest_evidence_run.completed_at).isoformat() if latest_evidence_run.completed_at else None,
            },
            "interval_seconds": settings.accounting_evidence_interval_seconds,
            "automatic_repair": False,
        },
        "automatic_repair": False,
    }
