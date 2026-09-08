from __future__ import annotations

from dataclasses import dataclass


FAULT_STAGES = (
    "journal_before",
    "journal_after_match_before_settlement",
    "match_after_before_settlement",
    "settlement_first_leg",
    "settlement_second_leg",
    "execution_before_durable",
    "commit_after_publish_before_ack",
    "publish_after_commit",
    "materialize_mid_batch",
    "startup_replay",
)


@dataclass(frozen=True, slots=True)
class FaultClassification:
    stage: str
    expected_ack_stage: str
    may_have_durable_execution: bool
    auto_retry_allowed: bool


def classify_fault(stage: str) -> FaultClassification:
    normalized = str(stage)
    if normalized == "journal_before":
        return FaultClassification(normalized, "REJECTED", False, False)
    if normalized == "journal_after_match_before_settlement":
        return FaultClassification(normalized, "UNKNOWN_TIMEOUT", False, False)
    if normalized in {"materialize_mid_batch", "startup_replay"}:
        return FaultClassification(normalized, "UNKNOWN_AFTER_RESTART", True, False)
    if normalized in {"publish_after_commit", "commit_after_publish_before_ack"}:
        return FaultClassification(normalized, "DURABLE", True, False)
    if normalized in {"match_after_before_settlement", "settlement_first_leg", "settlement_second_leg", "execution_before_durable"}:
        return FaultClassification(normalized, "UNKNOWN_TIMEOUT", False, False)
    raise ValueError(f"unknown causal fault stage: {stage}")

