"""Grok research-layer client stub.

Not wired. Optional structured *critique* only — never a numeric probability
path and never a stake. Risk endpoints are not tools for this client.
Cost ceiling is config-only so a future implementation cannot silently overspend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from typing import Any, Mapping

from research_lab.money import D
from research_lab.research_budget import ResearchBudget


@dataclass(frozen=True)
class GrokConfig:
    enabled: bool
    model: str
    cost_ceiling_usd: Decimal
    prompt_version: str = "research-layer-v1"

    @staticmethod
    def from_env() -> "GrokConfig":
        enabled = os.environ.get("GROK_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
        model = os.environ.get("GROK_MODEL", "").strip()
        ceiling = D(os.environ.get("GROK_COST_CEILING_USD", "0") or "0")
        return GrokConfig(enabled=enabled and bool(model), model=model, cost_ceiling_usd=ceiling)


class GrokNotWired(RuntimeError):
    pass


class GrokResearchClient:
    def __init__(
        self,
        config: GrokConfig | None = None,
        *,
        budget: ResearchBudget | None = None,
    ) -> None:
        self.config = config or GrokConfig.from_env()
        self.spent_usd = D(0)
        self.budget = budget

    def remaining_budget(self) -> Decimal:
        return self.config.cost_ceiling_usd - self.spent_usd

    def status(self) -> dict[str, object]:
        return {
            "wired": False,
            "enabled": self.config.enabled,
            "model": self.config.model or None,
            "prompt_version": self.config.prompt_version,
            "cost_ceiling_usd": str(self.config.cost_ceiling_usd),
            "spent_usd": str(self.spent_usd),
            "remaining_usd": str(self.remaining_budget()),
            "role": "structured_critique_only",
            "overrides_probability": False,
            "numeric_path": "weather_station_baseline",
            "note": (
                "Import a validated specialist forecast via POST /api/forecast. "
                "Grok does not choose stake, raise limits, or disable kills, "
                "and must not override specialist p_yes."
            ),
        }

    def propose(self, _request: object) -> dict[str, object]:
        if self.config.cost_ceiling_usd <= 0 or not self.config.enabled:
            return {
                "action": "ABSTAIN",
                "reason": "grok_not_wired",
                "model_name": self.config.model or "unset",
                "prompt_version": self.config.prompt_version,
            }
        raise GrokNotWired(
            "Grok API is not wired in this Kapu A build. "
            "Do not bypass the ceiling or post unsigned live orders."
        )

    def critique(self, specialist_payload: Mapping[str, Any] | None = None) -> dict[str, object]:
        """Structured critique interface. Never returns a replacement p_yes."""

        payload = dict(specialist_payload or {})
        if self.config.cost_ceiling_usd <= 0 or not self.config.enabled:
            return {
                "action": "ABSTAIN",
                "reason": "grok_critique_not_wired",
                "role": "structured_critique_only",
                "overrides_probability": False,
                "numeric_path": "weather_station_baseline",
                "model_name": self.config.model or "unset",
                "prompt_version": self.config.prompt_version,
                "specialist_action": payload.get("action"),
                "specialist_reason": payload.get("reason"),
                "remaining_usd": str(self.remaining_budget()),
                "note": (
                    "Critique is optional and non-binding. "
                    "Specialist remains the primary numeric path. "
                    "risk-v2 remains the only stake authority."
                ),
            }
        raise GrokNotWired(
            "Grok critique API is not wired. Specialist numbers stand; "
            "do not invent an LLM probability override."
        )
