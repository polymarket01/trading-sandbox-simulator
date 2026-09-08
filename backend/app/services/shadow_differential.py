from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.causal_contracts import ExecutionBundle, canonical_json, payload_hash


@dataclass(frozen=True, slots=True)
class ShadowDifferential:
    compared: bool
    mismatch: bool
    mismatch_fields: tuple[str, ...]
    primary_hash: str
    shadow_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "mismatch": self.mismatch,
            "mismatch_fields": list(self.mismatch_fields),
            "primary_hash": self.primary_hash,
            "shadow_hash": self.shadow_hash,
        }


def compare_execution_bundles(
    primary: ExecutionBundle | Mapping[str, Any],
    shadow: ExecutionBundle | Mapping[str, Any],
) -> ShadowDifferential:
    left = primary.as_dict() if isinstance(primary, ExecutionBundle) else dict(primary)
    right = shadow.as_dict() if isinstance(shadow, ExecutionBundle) else dict(shadow)
    fields = (
        "accepted",
        "rejected",
        "fills",
        "maker_taker",
        "stp_result",
        "settlement",
        "changed_price_levels",
        "priority_sequence",
        "book_sequence",
        "state_hash_before",
        "state_hash_after",
    )
    mismatches = tuple(field for field in fields if left.get(field) != right.get(field))
    return ShadowDifferential(
        compared=True,
        mismatch=bool(mismatches),
        mismatch_fields=mismatches,
        primary_hash=payload_hash(left),
        shadow_hash=payload_hash(right),
    )

