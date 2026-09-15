"""Evaluation stubs.

These helpers measure closed-sample economics and calibration. They do not
prove an edge. Do not treat DEMO or paper fills as live profitability.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from research_lab.money import D, q_cash


@dataclass(frozen=True)
class ClosedObservation:
    cluster_id: str
    net_trade_pnl: Decimal
    p_yes: float
    y_yes: float
    market_id: str = ""


@dataclass(frozen=True)
class BootstrapResult:
    n_clusters: int
    n_resamples: int
    statistic: str
    point: float
    low: float
    high: float
    samples: tuple[float, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "n_clusters": self.n_clusters,
            "n_resamples": self.n_resamples,
            "statistic": self.statistic,
            "point": self.point,
            "percentile_2_5": self.low,
            "percentile_97_5": self.high,
            "warning": (
                "Cluster bootstrap on the closed paper sample is not walk-forward, "
                "not live-fill confirmed, and not proof of edge."
            ),
        }


def net_trade_pnl(observations: Sequence[ClosedObservation]) -> Decimal:
    total = D(0)
    for row in observations:
        total += row.net_trade_pnl
    return q_cash(total)


def ops_cost_adjusted_pnl(
    trade_pnl: Decimal,
    ops_costs: Decimal,
) -> Decimal:
    """Hook for ledgered operating costs. Do not double-count entry reserves."""
    return q_cash(D(trade_pnl) - D(ops_costs))


def brier_score(p_yes: float, y_yes: float) -> float:
    return (p_yes - y_yes) ** 2


def mean_brier(observations: Sequence[ClosedObservation]) -> float:
    if not observations:
        return float("nan")
    return sum(brier_score(o.p_yes, o.y_yes) for o in observations) / len(observations)


def log_loss(p_yes: float, y_yes: float, *, eps: float = 1e-12) -> float:
    p = min(1 - eps, max(eps, p_yes))
    y = min(1.0, max(0.0, y_yes))
    return -((y * math.log(p)) + ((1 - y) * math.log(1 - p)))


def mean_log_loss(observations: Sequence[ClosedObservation]) -> float:
    if not observations:
        return float("nan")
    return sum(log_loss(o.p_yes, o.y_yes) for o in observations) / len(observations)


def settled_yes_outcome(token_side: str, payoff_held: Decimal | str) -> float:
    """Map held-token payoff to a YES outcome label for scoring forecasts."""
    pay = float(D(payoff_held))
    side = token_side.upper()
    if side == "YES":
        return pay
    if side == "NO":
        return 1.0 - pay
    raise ValueError(f"unknown token_side {token_side}")


def trade_pnl_from_fill(
    *,
    shares: Decimal,
    avg_entry: Decimal,
    entry_fee: Decimal,
    exit_credit: Decimal,
) -> Decimal:
    cost = (shares * avg_entry) + entry_fee
    return q_cash(exit_credit - cost)


def cluster_bootstrap(
    observations: Sequence[ClosedObservation],
    *,
    statistic: Callable[[Sequence[ClosedObservation]], float] | None = None,
    n_resamples: int = 1000,
    rng: random.Random | None = None,
) -> BootstrapResult:
    """Resample event clusters with replacement on the closed sample.

    This is not a walk-forward engine and does not correct for multiple testing,
    label revision, or live-vs-paper fill gap.
    """

    def default_stat(rows: Sequence[ClosedObservation]) -> float:
        return float(net_trade_pnl(rows))

    stat = statistic or default_stat
    if not observations:
        return BootstrapResult(0, n_resamples, "net_trade_pnl", float("nan"), float("nan"), float("nan"), ())

    clusters: dict[str, list[ClosedObservation]] = {}
    for row in observations:
        clusters.setdefault(row.cluster_id or row.market_id or "ungrouped", []).append(row)
    keys = list(clusters)
    engine = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(n_resamples):
        drawn: list[ClosedObservation] = []
        for key in engine.choices(keys, k=len(keys)):
            drawn.extend(clusters[key])
        samples.append(stat(drawn))
    ordered = sorted(samples)
    lo_i = int(0.025 * (len(ordered) - 1))
    hi_i = int(0.975 * (len(ordered) - 1))
    return BootstrapResult(
        n_clusters=len(keys),
        n_resamples=n_resamples,
        statistic=getattr(statistic, "__name__", "net_trade_pnl"),
        point=stat(observations),
        low=ordered[lo_i],
        high=ordered[hi_i],
        samples=tuple(ordered),
    )


def observations_from_settled_rows(rows: Sequence[dict]) -> list[ClosedObservation]:
    """Build scoring rows from settled positions. CLOSED (sold) rows skip y_yes=unknown."""

    out: list[ClosedObservation] = []
    for row in rows:
        if row.get("status") != "SETTLED":
            continue
        payoff = D(row["settle_payoff"])
        shares = D(row["shares"])
        avg = D(row["avg_price"])
        fee = D(row["entry_fee"])
        credit = shares * payoff
        pnl = trade_pnl_from_fill(
            shares=shares, avg_entry=avg, entry_fee=fee, exit_credit=credit
        )
        p_yes = float(D(row.get("p_yes") or "0"))
        out.append(
            ClosedObservation(
                cluster_id=str(row.get("cluster_id") or row.get("review_cluster") or row["market_id"]),
                net_trade_pnl=pnl,
                p_yes=p_yes,
                y_yes=settled_yes_outcome(str(row["token_side"]), payoff),
                market_id=str(row["market_id"]),
            )
        )
    return out
