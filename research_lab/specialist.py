"""Specialist probability model interface + placeholder baseline.

Kapu A does not ship a fitted specialist. The placeholder always ABSTAINs.
Calibration hooks exist so a later model can record vintage without changing risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Mapping, Protocol


@dataclass(frozen=True)
class ResearchRequest:
    market_id: str
    condition_id: str | None
    rules_text: str
    rules_hash: str
    as_of: str
    cutoff_at: str | None
    resolution_source: str | None
    specialist_hints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpecialistEstimate:
    status: Literal["ABSTAIN", "ESTIMATE"]
    calibration_version: str
    p_yes: Decimal | None = None
    p_low: Decimal | None = None
    p_high: Decimal | None = None
    reason: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class SpecialistModel(Protocol):
    name: str

    def estimate(self, request: ResearchRequest) -> SpecialistEstimate: ...


class PlaceholderSpecialist:
    """Neutral baseline: ABSTAIN. A language-model confidence is not a CI."""

    name = "placeholder-abstain-v0"
    calibration_version = "unmeasured-v0"

    def estimate(self, request: ResearchRequest) -> SpecialistEstimate:
        return SpecialistEstimate(
            status="ABSTAIN",
            calibration_version=self.calibration_version,
            reason="specialist_not_implemented",
            diagnostics={
                "model": self.name,
                "rules_hash": request.rules_hash,
                "note": "Do not treat this placeholder as a calibrated probability.",
            },
        )
