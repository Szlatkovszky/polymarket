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

    @staticmethod
    def from_env() -> "ResearchBudget":
        ceiling = D(os.environ.get("RESEARCH_COST_CEILING_USD", "5") or "0")
        unit = D(os.environ.get("RESEARCH_NETWORK_UNIT_COST_USD", "0.001") or "0")
        return ResearchBudget(cost_ceiling_usd=ceiling, run_unit_cost_usd=unit)

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
            "note": (
                "Ceiling is config-only. Specialist numbers are not a stake. "
                "risk-v2 remains the only decision authority."
            ),
        }


def weather_specialist_enabled() -> bool:
    return _flag("WEATHER_SPECIALIST_ENABLED", "1")
