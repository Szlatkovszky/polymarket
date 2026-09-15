"""Decimal cash helpers. Never use float for ledger amounts."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

CASH_QUANT = Decimal("0.000001")
SHARE_QUANT = Decimal("0.01")
PRICE_QUANT = Decimal("0.0001")
PROB_QUANT = Decimal("0.0000000001")


def D(value: Decimal | int | str | float) -> Decimal:
    """Parse a Decimal without accepting binary float artifacts when given str/int.

    float is accepted only as a last resort via str() so JSON numbers survive.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a monetary amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def q_cash(value: Decimal | int | str) -> Decimal:
    return D(value).quantize(CASH_QUANT, rounding=ROUND_HALF_EVEN)


def q_shares(value: Decimal | int | str) -> Decimal:
    return D(value).quantize(SHARE_QUANT, rounding=ROUND_HALF_EVEN)


def round_fee_up(value: Decimal) -> Decimal:
    """Estimated taker fee is always rounded up (never in the trader's favor)."""
    if value < 0:
        raise ValueError("fee cannot be negative")
    return D(value).quantize(CASH_QUANT, rounding=ROUND_CEILING)


def polymarket_taker_fee(
    *,
    size: Decimal,
    price: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    """Documented taker fee: size × fee_rate × price × (1 − price).

    PAPER estimate only — not a live exchange invoice. Do not use a baked-in
    category table; the rate must come from ``feesEnabled`` + ``feeSchedule``.
    """
    if fee_rate < 0:
        raise ValueError("fee_rate cannot be negative")
    return size * D(fee_rate) * price * (D(1) - price)


def polymarket_crypto_fee(
    *,
    size: Decimal,
    price: Decimal,
    fee_bps: int,
) -> Decimal:
    """Legacy wrapper: ``fee_bps`` / 10_000 as the taker rate."""
    if fee_bps < 0:
        raise ValueError("fee_bps cannot be negative")
    return polymarket_taker_fee(
        size=size, price=price, fee_rate=D(fee_bps) / D(10_000)
    )
