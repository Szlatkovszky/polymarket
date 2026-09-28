"""Taker fee from market ``feesEnabled`` + ``feeSchedule`` — never a category table.

Formula (PAPER estimate only):

    fee = C × rate × (p × (1 − p))^exponent

Exponent 1 is the formula printed on
https://docs.polymarket.com/trading/fees and matches that page's weather table
(rate 0.05, 100 shares, p=0.50 → 1.25 USDC). Market details describe
``feeSchedule.exponent`` as the exponent on the price component of that curve:
https://docs.polymarket.com/market-data/market-details

The official CLOB v2 client applies it the same way
(https://github.com/Polymarket/clob-client-v2/blob/main/src/fees/index.ts):

    platformFeeRate = feeRate × (p × (1 − p))^exponent
    fee = C × platformFeeRate

``GET /fee-rate`` returns only ``base_fee`` in basis points
(https://docs.polymarket.com/api-reference/market-data/get-fee-rate). That
integer is not ``feeSchedule.rate`` and is not used here.

Paper fills are taker FOK. ``takerOnly: true`` means this fee applies.
``rebateRate`` is the maker pool fraction and is never credited.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from research_lab.money import D

SUPPORTED_FEE_EXPONENTS = frozenset({1, 2})


@dataclass(frozen=True)
class ParsedFeeSchedule:
    """Supported taker curve. ``rebate_rate`` is recorded and never subtracted."""

    rate: Decimal
    exponent: int
    taker_only: bool | None
    rebate_rate: Decimal | None


def parse_market_fee_rate(raw: Mapping[str, Any] | None) -> Decimal | None:
    """Return the schedule ``rate``, or None if the taker fee is unknown.

    Unknown/missing/unsupported exponent → caller must NO TRADE.
    ``feesEnabled: false`` is a known zero rate, not unknown.
    """

    parsed = parse_market_fee_schedule(raw)
    if parsed is None:
        return None
    return parsed.rate


def parse_market_fee_schedule(raw: Mapping[str, Any] | None) -> ParsedFeeSchedule | None:
    """Return a supported taker schedule, or None if the lab must refuse."""

    if not raw or not isinstance(raw, Mapping):
        return None
    if "feesEnabled" not in raw:
        return None
    enabled = raw["feesEnabled"]
    if enabled is False:
        return ParsedFeeSchedule(rate=D(0), exponent=1, taker_only=None, rebate_rate=None)
    if enabled is not True:
        return None
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, Mapping):
        return None
    if "rate" not in schedule or "exponent" not in schedule:
        return None
    exponent = _integral_exponent(schedule.get("exponent"))
    if exponent not in SUPPORTED_FEE_EXPONENTS:
        return None
    rate = _nonneg_decimal(schedule.get("rate"))
    if rate is None:
        return None
    taker_only, taker_ok = _taker_only(schedule)
    if not taker_ok:
        return None
    rebate, rebate_ok = _rebate_rate(schedule)
    if not rebate_ok:
        return None
    return ParsedFeeSchedule(
        rate=rate,
        exponent=exponent,
        taker_only=taker_only,
        rebate_rate=rebate,
    )


def _integral_exponent(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    try:
        dec = D(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dec.is_finite() or dec != dec.to_integral_value():
        return None
    return int(dec)


def _nonneg_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        dec = D(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dec.is_finite() or dec < 0:
        return None
    return dec


def _taker_only(schedule: Mapping[str, Any]) -> tuple[bool | None, bool]:
    """Paper fills are taker, so a missing or true flag still charges the fee.

    A non-boolean value is an unknown schedule. ``False`` does not mean the
    taker is free; the fees page still charges takers.
    """

    if "takerOnly" not in schedule or schedule.get("takerOnly") is None:
        return None, True
    flag = schedule.get("takerOnly")
    if not isinstance(flag, bool):
        return None, False
    return flag, True


def _rebate_rate(schedule: Mapping[str, Any]) -> tuple[Decimal | None, bool]:
    """Maker rebate fraction. Absent is fine. Out of [0, 1] refuses the schedule.

    The value is not applied to paper taker fills.
    """

    if "rebateRate" not in schedule or schedule.get("rebateRate") is None:
        return None, True
    rebate = _nonneg_decimal(schedule.get("rebateRate"))
    if rebate is None or rebate > 1:
        return None, False
    return rebate, True
