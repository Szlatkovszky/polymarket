"""Deterministic risk engine. The research layer cannot mutate these limits."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from research_lab.money import D

RISK_V1 = "risk-v1"


@dataclass(frozen=True)
class Risk:
    """Versioned paper-risk limits.

    New numeric limits require a new ``version``, tests, and human approval.
    The research/LLM layer must never raise these, disable kills, or pick stake.
    """

    version: str
    starting_cash: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    min_conservative_edge: Decimal
    max_open_positions: int
    max_cluster_notional: Decimal
    ops_cost_reserve_per_trade: Decimal
    min_fill_shares: Decimal
    unit_shares: Decimal

    @staticmethod
    def v1() -> "Risk":
        return Risk(
            version=RISK_V1,
            starting_cash=D("10000.00"),
            max_position_notional=D("250.00"),
            max_daily_loss=D("150.00"),
            max_drawdown=D("400.00"),
            min_conservative_edge=D("0.03"),
            max_open_positions=8,
            max_cluster_notional=D("500.00"),
            ops_cost_reserve_per_trade=D("0.25"),
            min_fill_shares=D("5"),
            unit_shares=D("20"),
        )


RISK_REGISTRY: Mapping[str, Risk] = {
    RISK_V1: Risk.v1(),
}


def load_risk(version: str) -> Risk:
    try:
        return RISK_REGISTRY[version]
    except KeyError as exc:
        raise ValueError(
            f"unknown risk version {version!r}; refusing to start. "
            "Do not invent limits at runtime."
        ) from exc


@dataclass(frozen=True)
class AccountView:
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    start_of_day_equity: Decimal
    open_position_count: int
    cluster_notional: Decimal
    paused: bool
    valuation_complete: bool


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str

    @staticmethod
    def ok() -> "GateResult":
        return GateResult(True, "ok")

    @staticmethod
    def deny(reason: str) -> "GateResult":
        return GateResult(False, reason)


def evaluate_entry_gates(
    risk: Risk,
    account: AccountView,
    *,
    fee_known: bool,
    forecast_expired: bool,
    rules_review_present: bool,
    model_authorized: bool,
    rules_hash_matches: bool,
    valuation_complete: bool | None = None,
) -> GateResult:
    """Hard refusals. Order is stable so tests can assert the first failing reason."""

    if account.paused:
        return GateResult.deny("paused")
    complete = account.valuation_complete if valuation_complete is None else valuation_complete
    if not complete:
        return GateResult.deny("incomplete_valuation")
    daily_loss = account.start_of_day_equity - account.equity
    if daily_loss >= risk.max_daily_loss:
        return GateResult.deny("daily_loss")
    drawdown = account.peak_equity - account.equity
    if drawdown >= risk.max_drawdown:
        return GateResult.deny("drawdown")
    if not fee_known:
        return GateResult.deny("unknown_fee")
    if forecast_expired:
        return GateResult.deny("expired_forecast")
    if not rules_review_present:
        return GateResult.deny("missing_rules_review")
    if not rules_hash_matches:
        return GateResult.deny("rules_hash_mismatch")
    if not model_authorized:
        return GateResult.deny("model_not_authorized")
    if account.open_position_count >= risk.max_open_positions:
        return GateResult.deny("max_open_positions")
    if account.cluster_notional >= risk.max_cluster_notional:
        return GateResult.deny("cluster_exposure")
    if account.cash <= 0:
        return GateResult.deny("no_cash")
    return GateResult.ok()


def position_share_target(risk: Risk) -> Decimal:
    """Stake comes from risk config, never from the research envelope."""
    return risk.unit_shares
