"""Taker fee from market ``feesEnabled`` + ``feeSchedule`` — never a category table."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from research_lab.money import D

SUPPORTED_FEE_EXPONENTS = frozenset({2})


def parse_market_fee_rate(raw: Mapping[str, Any] | None) -> Decimal | None:
    """Return taker ``fee_rate`` for size×rate×price×(1−price), or None if unknown.

    Unknown/missing/unsupported exponent → caller must NO TRADE.
    ``feesEnabled: false`` is a known zero rate, not unknown.
    """

    if not raw:
        return None
    if "feesEnabled" not in raw:
        return None
    enabled = raw["feesEnabled"]
    if enabled is False:
        return D(0)
    if enabled is not True:
        return None
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict):
        return None
    if "rate" not in schedule:
        return None
    exponent = schedule.get("exponent", 2)
    try:
        exp_i = int(exponent)
    except (TypeError, ValueError):
        return None
    if exp_i not in SUPPORTED_FEE_EXPONENTS:
        return None
    try:
        rate = D(schedule["rate"])
    except Exception:  # noqa: BLE001
        return None
    if rate < 0:
        return None
    return rate
