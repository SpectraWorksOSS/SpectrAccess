"""Portable declared-assumption record shared by connector outputs.

Matches RefCal's five-value assumption vocabulary without importing RefCal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AssumptionBasis(str, Enum):
    """Why an assumption is present rather than a measured fact."""

    DOCUMENTED = "documented"
    INFERRED_FROM_ABSENCE = "inferred_from_absence"
    OPERATOR_DECLARED = "operator_declared"
    PLACEHOLDER = "placeholder"
    DERIVED_FROM_CHAIN = "derived_from_chain"


@dataclass(frozen=True)
class AssumptionRecord:
    """One declared assumption with a stable identifier and retirement condition."""

    assumption_id: str
    statement: str
    basis: AssumptionBasis
    resolves_when: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.assumption_id, self.statement, self.resolves_when)
        ):
            raise ValueError("AssumptionRecord fields must be non-blank strings")
        if not isinstance(self.basis, AssumptionBasis):
            raise ValueError("AssumptionRecord basis must be an AssumptionBasis")

    def to_dict(self) -> dict[str, str]:
        return {
            "assumption_id": self.assumption_id,
            "statement": self.statement,
            "basis": self.basis.value,
            "resolves_when": self.resolves_when,
        }
