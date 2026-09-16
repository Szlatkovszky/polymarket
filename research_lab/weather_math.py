"""Interval probabilities for the weather specialist baseline.

Normal is an *unproven starting baseline*. Extremes and regime shifts are
known failure modes; this module does not claim calibration or edge.

Contract events are evaluated on the *resolution quantity* after rounding,
not on a vague city-weather narrative.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from research_lab.money import D

PROB_QUANT = Decimal("0.000001")


def clip01(value: Decimal) -> Decimal:
    if value < 0:
        return D(0)
    if value > 1:
        return D(1)
    return value


def quantize_prob(value: Decimal) -> Decimal:
    return clip01(D(value)).quantize(PROB_QUANT)


def normal_cdf(z: float) -> float:
    """Standard normal CDF via erf. Domain is the whole real line."""

    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def interval_prob(
    mu: Decimal,
    sigma: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
) -> Decimal:
    """P(lower <= T < upper) under Normal(mu, sigma^2).

    ``None`` means an open end. ``F(b) - F(a)`` with a=-inf and/or b=+inf.
    Missing/invalid sigma never silently becomes 0.
    """

    if sigma is None or sigma <= 0:
        raise ValueError("sigma must be positive; missing error scale is not 0")
    mu_f = float(D(mu))
    sig_f = float(D(sigma))

    def cdf_at(bound: Decimal | None, *, right: bool) -> float:
        if bound is None:
            return 1.0 if right else 0.0
        z = (float(D(bound)) - mu_f) / sig_f
        return normal_cdf(z)

    p = cdf_at(upper, right=True) - cdf_at(lower, right=False)
    return quantize_prob(D(str(max(0.0, min(1.0, p)))))


class BoundaryHook(Protocol):
    def apply(
        self, lower: Decimal | None, upper: Decimal | None
    ) -> tuple[Decimal | None, Decimal | None]: ...


class IdentityBoundaryHook:
    """Configurable rounding/boundary hook default: no extra correction."""

    name = "identity-boundary-v0"

    def apply(
        self, lower: Decimal | None, upper: Decimal | None
    ) -> tuple[Decimal | None, Decimal | None]:
        return lower, upper


@dataclass(frozen=True)
class RoundingRule:
    """Map a rounded contract interval back to the underlying continuous T.

    Half-up to ``increment`` (positive temperatures): rounded value R >= t
    iff T >= t - increment/2. Hooks can replace this mapping.
    """

    increment: Decimal = D("0.1")
    mode: str = "half_up"

    def underlying_ge(self, rounded_threshold: Decimal) -> Decimal:
        if self.mode == "unspecified":
            raise ValueError(
                "unspecified rounding cannot map a reported interval to underlying T"
            )
        if self.mode != "half_up":
            raise ValueError(f"unsupported rounding mode {self.mode!r}")
        if self.increment <= 0:
            raise ValueError("rounding increment must be positive")
        return D(rounded_threshold) - (D(self.increment) / D(2))

    def underlying_lt(self, rounded_threshold: Decimal) -> Decimal:
        return self.underlying_ge(rounded_threshold)

    def underlying_interval(
        self,
        rounded_lower: Decimal | None,
        rounded_upper: Decimal | None,
        *,
        hook: BoundaryHook | None = None,
    ) -> tuple[Decimal | None, Decimal | None]:
        lower = None if rounded_lower is None else self.underlying_ge(rounded_lower)
        upper = None if rounded_upper is None else self.underlying_lt(rounded_upper)
        applied = hook or IdentityBoundaryHook()
        return applied.apply(lower, upper)


REVIEW_ROUNDING_MODES = frozenset({"half_up", "unspecified"})
CONCRETE_ROUNDING_MODES = frozenset({"half_up"})
REVIEW_ROUNDING_UNITS = frozenset({"C", "F"})


class RoundingReviewError(ValueError):
    """Invalid human-review rounding fields. Not a trading signal."""


def normalize_review_rounding(
    *,
    mode: str | None = None,
    increment: str | Decimal | int | float | None = None,
    unit: str | None = None,
) -> dict[str, str | None]:
    """Validate optional rules-review rounding fields.

    Empty / omitted fields stay None. The specialist ABSTAINs on unspecified
    until a concrete algorithm (currently only ``half_up``) plus a positive
    increment is recorded. This does not invent rounding from market text.
    """

    raw_mode = "" if mode is None else str(mode).strip().lower()
    raw_unit = "" if unit is None else str(unit).strip().upper()
    increment_set = increment not in (None, "")

    if raw_mode and raw_mode not in REVIEW_ROUNDING_MODES:
        raise RoundingReviewError(f"unsupported rounding_mode {raw_mode!r}")
    if raw_unit and raw_unit not in REVIEW_ROUNDING_UNITS:
        raise RoundingReviewError(f"unsupported rounding_unit {raw_unit!r}")

    increment_s: str | None = None
    if increment_set:
        try:
            parsed = D(str(increment).strip())
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise RoundingReviewError("invalid rounding_increment") from exc
        if parsed <= 0:
            raise RoundingReviewError("rounding_increment must be positive")
        increment_s = str(parsed)

    if raw_mode in CONCRETE_ROUNDING_MODES and increment_s is None:
        raise RoundingReviewError(
            "rounding_increment required when rounding_mode is a concrete algorithm"
        )
    if increment_s is not None and not raw_mode:
        raise RoundingReviewError("rounding_mode required when rounding_increment is set")

    return {
        "rounding_mode": raw_mode or None,
        "rounding_increment": increment_s,
        "rounding_unit": raw_unit or None,
    }


def review_rounding_hint(
    review: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Copy stored review rounding into a specialist hint, or None if omitted."""

    if review is None:
        return None
    mode = review.get("rounding_mode")
    increment = review.get("rounding_increment")
    unit = review.get("rounding_unit")
    hint: dict[str, str] = {}
    if mode not in (None, ""):
        hint["mode"] = str(mode)
    if increment not in (None, ""):
        hint["increment"] = str(increment)
    if unit not in (None, ""):
        hint["unit"] = str(unit)
    return hint or None


def rounding_rule_from_review_hint(
    hint: Mapping[str, Any] | None,
    *,
    contract_unit: str,
) -> tuple[RoundingRule | None, str]:
    """Map a rules-review hint to a RoundingRule.

    Returns ``(None, "rounding_unspecified")`` when the review omitted a
    concrete algorithm. Never invents half-up.
    """

    if not hint:
        return None, "rounding_unspecified"
    mode = str(hint.get("mode") or hint.get("rounding_mode") or "").strip().lower()
    increment = hint.get("increment", hint.get("rounding_increment"))
    unit_raw = hint.get("unit", hint.get("rounding_unit"))
    unit = "" if unit_raw in (None, "") else str(unit_raw).strip().upper()
    contract_u = (contract_unit or "").strip().upper()
    if unit and contract_u and unit != contract_u:
        return None, "rounding_unit_mismatch"
    if not mode or mode == "unspecified":
        return None, "rounding_unspecified"
    if mode not in CONCRETE_ROUNDING_MODES:
        return None, "unsupported_rounding_mode"
    if increment in (None, ""):
        return None, "rounding_unspecified"
    try:
        parsed = D(str(increment).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None, "invalid_rounding_increment"
    if parsed <= 0:
        return None, "invalid_rounding_increment"
    return RoundingRule(increment=parsed, mode=mode), "rules_review"


def conservative_band(
    p_yes: Decimal,
    *,
    deduction: Decimal = D("0.05"),
) -> tuple[Decimal, Decimal]:
    """Prototype envelope: ± deduction, clipped to [0, 1].

    This is a conservative scenario, not a 95% confidence interval and not
    a substitute for external calibration.
    """

    p = quantize_prob(p_yes)
    band = D(deduction)
    if band < 0:
        raise ValueError("conservative deduction must be >= 0")
    p_low = quantize_prob(p - band)
    p_high = quantize_prob(p + band)
    if p_low > p:
        p_low = p
    if p_high < p:
        p_high = p
    return p_low, p_high
