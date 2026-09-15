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


def polymarket_crypto_fee(
    *,
    size: Decimal,
    price: Decimal,
    fee_bps: int,
) -> Decimal:
    """Public CLOB crypto-style fee: size * rate * price * (1 - price).

    ``fee_bps`` is the CLOB ``base_fee`` in basis points (e.g. 30 → 0.30%).
    This is an estimate for PAPER fills, not a live exchange invoice.
    """
    if fee_bps < 0:
        raise ValueError("fee_bps cannot be negative")
    rate = D(fee_bps) / D(10_000)
    return size * rate * price * (D(1) - price)
