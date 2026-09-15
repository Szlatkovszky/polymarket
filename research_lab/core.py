"""Deterministic risk engine. The research layer cannot mutate these limits."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from research_lab.money import D, q_cash

RISK_V1 = "risk-v1"
RISK_V2 = "risk-v2"
DEFAULT_RISK_VERSION = RISK_V2

# 10_000 simulated dollars is the research starting book — not a suggested deposit.
SIM_EQUITY = D("10000.00")


@dataclass(frozen=True)
class Risk:
    """Versioned paper-risk limits (companion research 2026-09-15).

    New numeric limits require a new ``version``, tests, and human approval.
    The research/LLM layer must never raise these, disable kills, or pick stake.
    Percentage limits are of 10k sim equity unless noted.
    """

    version: str
    starting_cash: Decimal
    max_trade_cost: Decimal
    max_market_cost: Decimal
    max_cluster_notional: Decimal
    max_open_entry_cost: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    min_conservative_edge: Decimal
    price_band_low: Decimal
    price_band_high: Decimal
    max_spread: Decimal
    max_depth_fraction: Decimal
    max_book_age_seconds: int
    max_forecast_age_hours: int
    max_settlement_days: int
    max_entries_per_day: int
    ops_reserve_per_share: Decimal
    slippage_reserve_per_share: Decimal
    min_fill_shares: Decimal
    max_open_positions: int
    # Back-compat aliases used by older call sites
    max_position_notional: Decimal
    ops_cost_reserve_per_trade: Decimal
    unit_shares: Decimal

    @staticmethod
    def v2() -> "Risk":
        """Locked starting research defaults on 10k sim equity."""
        max_trade = q_cash(SIM_EQUITY * D("0.005"))  # 0.5% → 50
        max_market = q_cash(SIM_EQUITY * D("0.01"))  # 1% → 100
        return Risk(
            version=RISK_V2,
            starting_cash=SIM_EQUITY,
            max_trade_cost=max_trade,
            max_market_cost=max_market,
            max_cluster_notional=q_cash(SIM_EQUITY * D("0.02")),  # 2% → 200
            max_open_entry_cost=q_cash(SIM_EQUITY * D("0.10")),  # 10% → 1000
            max_daily_loss=q_cash(SIM_EQUITY * D("0.015")),  # 1.5% → 150
            max_drawdown=q_cash(SIM_EQUITY * D("0.05")),  # 5% → 500
            min_conservative_edge=D("0.03"),
            price_band_low=D("0.10"),
            price_band_high=D("0.90"),
            max_spread=D("0.03"),
            max_depth_fraction=D("0.20"),
            max_book_age_seconds=5,
            max_forecast_age_hours=6,
            max_settlement_days=14,
            max_entries_per_day=20,
            ops_reserve_per_share=D("0.002"),
            slippage_reserve_per_share=D("0.002"),
            min_fill_shares=D("1"),
            max_open_positions=20,
            max_position_notional=max_trade,
            ops_cost_reserve_per_trade=D("0"),
            unit_shares=D("0"),
        )

    @staticmethod
    def v1() -> "Risk":
        """Frozen pre-research scaffold. Do not use for new runs."""
        return Risk(
            version=RISK_V1,
            starting_cash=SIM_EQUITY,
            max_trade_cost=D("250.00"),
            max_market_cost=D("250.00"),
            max_cluster_notional=D("500.00"),
            max_open_entry_cost=D("1000.00"),
            max_daily_loss=D("150.00"),
            max_drawdown=D("400.00"),
            min_conservative_edge=D("0.03"),
            price_band_low=D("0.00"),
            price_band_high=D("1.00"),
            max_spread=D("1.00"),
            max_depth_fraction=D("1.00"),
            max_book_age_seconds=86400,
            max_forecast_age_hours=24,
            max_settlement_days=365,
            max_entries_per_day=100,
            ops_reserve_per_share=D("0.0125"),
            slippage_reserve_per_share=D("0"),
            min_fill_shares=D("5"),
            max_open_positions=8,
            max_position_notional=D("250.00"),
            ops_cost_reserve_per_trade=D("0.25"),
            unit_shares=D("20"),
        )

    def per_share_research_reserve(self) -> Decimal:
        """Assumptions (0.002 ops + 0.002 slippage), not observed costs."""
        return self.ops_reserve_per_share + self.slippage_reserve_per_share


RISK_REGISTRY: Mapping[str, Risk] = {
    RISK_V1: Risk.v1(),
    RISK_V2: Risk.v2(),
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
    entries_today: int = 0
    open_entry_cost: Decimal = D("0")


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
    if account.open_entry_cost >= risk.max_open_entry_cost:
        return GateResult.deny("open_exposure")
    if account.entries_today >= risk.max_entries_per_day:
        return GateResult.deny("daily_entry_cap")
    if account.cash <= 0:
        return GateResult.deny("no_cash")
    return GateResult.ok()


def position_share_target(risk: Risk) -> Decimal:
    """Deprecated: stake is computed from 0.5% cost cap and 20% depth, not a unit lot."""
    return risk.unit_shares
