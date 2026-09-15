"""Process-local research spend ceiling.

Numeric specialist runs and optional Grok critique share this config so a
future wired client cannot silently overspend. Fixture weather reads cost 0.
The research layer still cannot size trades or mutate risk-v2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from research_lab.money import D, q_cash


class ResearchBudgetError(RuntimeError):
    pass


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


@dataclass
class ResearchBudget:
    cost_ceiling_usd: Decimal
    spent_usd: Decimal = field(default_factory=lambda: D(0))
    run_unit_cost_usd: Decimal = field(default_factory=lambda: D("0"))
    max_calls_per_cycle: int = 200
    min_interval_seconds: float = 0.0
    calls_this_cycle: int = 0

    @staticmethod
    def from_env() -> "ResearchBudget":
        ceiling = D(os.environ.get("RESEARCH_COST_CEILING_USD", "5") or "0")
        unit = D(os.environ.get("RESEARCH_NETWORK_UNIT_COST_USD", "0.001") or "0")
        try:
            max_calls = int(os.environ.get("RESEARCH_MAX_CALLS_PER_CYCLE", "200") or "200")
        except ValueError as exc:
            raise ResearchBudgetError("RESEARCH_MAX_CALLS_PER_CYCLE must be an integer") from exc
        try:
            min_interval = float(os.environ.get("RESEARCH_MIN_INTERVAL_SECONDS", "0") or "0")
        except ValueError as exc:
            raise ResearchBudgetError("RESEARCH_MIN_INTERVAL_SECONDS must be a number") from exc
        return ResearchBudget(
            cost_ceiling_usd=ceiling,
            run_unit_cost_usd=unit,
            max_calls_per_cycle=max(0, max_calls),
            min_interval_seconds=max(0.0, min_interval),
        )

    def reset_cycle(self) -> None:
        self.calls_this_cycle = 0

    def remaining_calls(self) -> int:
        return max(0, self.max_calls_per_cycle - self.calls_this_cycle)

    def note_call(self, *, reason: str, network: bool = False) -> None:
        if self.calls_this_cycle >= self.max_calls_per_cycle:
            raise ResearchBudgetError(
                f"research_call_ceiling calls={self.calls_this_cycle} "
                f"max={self.max_calls_per_cycle} reason={reason}"
            )
        self.calls_this_cycle += 1
        if network:
            self.charge(self.run_unit_cost_usd, reason=reason)

    def remaining_usd(self) -> Decimal:
        return q_cash(self.cost_ceiling_usd - self.spent_usd)

    def can_spend(self, amount: Decimal) -> bool:
        if amount <= 0:
            return True
        if self.cost_ceiling_usd < 0:
            return False
        return self.spent_usd + amount <= self.cost_ceiling_usd

    def charge(self, amount: Decimal, *, reason: str) -> None:
        amt = D(amount)
        if amt < 0:
            raise ResearchBudgetError("cannot charge a negative research cost")
        if amt == 0:
            return
        if not self.can_spend(amt):
            raise ResearchBudgetError(
                f"research_budget_exhausted remaining={self.remaining_usd()} "
                f"need={amt} reason={reason}"
            )
        self.spent_usd = q_cash(self.spent_usd + amt)

    def status(self) -> dict[str, object]:
        return {
            "cost_ceiling_usd": str(q_cash(self.cost_ceiling_usd)),
            "spent_usd": str(q_cash(self.spent_usd)),
            "remaining_usd": str(self.remaining_usd()),
            "network_unit_cost_usd": str(q_cash(self.run_unit_cost_usd)),
            "max_calls_per_cycle": self.max_calls_per_cycle,
            "calls_this_cycle": self.calls_this_cycle,
            "remaining_calls": self.remaining_calls(),
            "min_interval_seconds": self.min_interval_seconds,
            "note": (
                "Ceiling is config-only. Specialist numbers are not a stake. "
                "risk-v2 remains the only decision authority."
            ),
        }


def weather_specialist_enabled() -> bool:
    return _flag("WEATHER_SPECIALIST_ENABLED", "1")
