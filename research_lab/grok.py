"""Grok research-layer client stub.

Not wired. Risk endpoints are not tools for this client. Cost ceiling is
config-only so a future implementation cannot silently overspend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from research_lab.money import D


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
    def __init__(self, config: GrokConfig | None = None) -> None:
        self.config = config or GrokConfig.from_env()
        self.spent_usd = D(0)

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
            "note": (
                "Import a validated forecast via POST /api/forecast. "
                "Grok does not choose stake, raise limits, or disable kills."
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
