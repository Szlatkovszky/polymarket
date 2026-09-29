"""Turn a weather specialist estimate into a forecast/decision envelope.

Does not size trades. risk-v2 remains the only stake/decision authority.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from research_lab.hashing import sha256_hex
from research_lab.money import D
from research_lab.specialist import (
    WEATHER_MODEL_VERSION,
    ResearchRequest,
    SpecialistEstimate,
    WeatherStationBaseline,
)
from research_lab.timeutil import isoformat_utc, parse_utc

_GAP_QUANT = Decimal("0.000001")


def forecast_id_for(
    *,
    market_id: str,
    as_of: str,
    rules_hash: str,
    model_version: str,
    p_yes: str,
) -> str:
    material = "|".join([market_id, as_of, rules_hash, model_version, p_yes])
    return "wxbl-" + sha256_hex(material)[:24]


def _model_version_of(estimate: SpecialistEstimate) -> str:
    return str(estimate.diagnostics.get("model_version") or WEATHER_MODEL_VERSION)


def _model_name_of(estimate: SpecialistEstimate) -> str:
    return str(estimate.diagnostics.get("model") or "weather-station-baseline-v2")


def _gap_str(model_p: str | None, mid: str | None) -> str | None:
    if model_p in (None, "") or mid in (None, ""):
        return None
    try:
        gap = (D(str(model_p)) - D(str(mid))).quantize(_GAP_QUANT)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return str(gap)


def comparison_block(
    *,
    model_p_yes: str | None,
    market_mid: str | None,
    market_mid_available_at: str | None,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    """Model vs stored YES mid. Diagnostic only; not an edge."""

    return {
        "market_mid": market_mid,
        "market_mid_available_at": market_mid_available_at,
        "unavailable_reason": None if market_mid is not None else unavailable_reason,
        "model_p_yes": model_p_yes,
        "model_minus_market_mid": _gap_str(model_p_yes, market_mid),
        "edge_proven": False,
        "note": (
            "YES mid from the latest stored book with captured_at <= as_of. "
            "The gap is diagnostic only. It is not statistical validation and "
            "does not prove an edge."
        ),
    }


def comparison_from_estimate(estimate: SpecialistEstimate) -> dict[str, Any]:
    variants = estimate.diagnostics.get("variants") or {}
    mid_row = variants.get("market_mid") if isinstance(variants, Mapping) else None
    if not isinstance(mid_row, Mapping):
        mid_row = {}
    mid = mid_row.get("p_yes")
    mid_s = None if mid in (None, "") else str(mid)
    available = mid_row.get("available_at")
    reason = mid_row.get("unavailable_reason")
    model_p = None if estimate.p_yes is None else str(estimate.p_yes)
    return comparison_block(
        model_p_yes=model_p,
        market_mid=mid_s,
        market_mid_available_at=None if available in (None, "") else str(available),
        unavailable_reason=None if reason in (None, "") else str(reason),
    )


def estimate_to_decision(
    estimate: SpecialistEstimate,
    request: ResearchRequest,
    *,
    expires_hours: float = 3.0,
) -> dict[str, Any]:
    """ABSTAIN has no forecast object. PROPOSE carries a schema-valid envelope."""

    if estimate.status == "ABSTAIN":
        return {
            "action": "ABSTAIN",
            "reason": estimate.reason,
            "model_name": _model_name_of(estimate),
            "model_version": _model_version_of(estimate),
            "calibration_version": estimate.calibration_version,
            "variants": estimate.diagnostics.get("variants"),
            "comparison": comparison_from_estimate(estimate),
            "diagnostics": dict(estimate.diagnostics),
            "imported": False,
            "edge_proven": False,
            "note": (
                "ABSTAIN is not imported into the paper path. "
                "Specialist does not choose stake."
            ),
        }

    if estimate.p_yes is None or estimate.p_low is None or estimate.p_high is None:
        return estimate_to_decision(
            SpecialistEstimate(
                status="ABSTAIN",
                calibration_version=estimate.calibration_version,
                reason="incomplete_probability",
                diagnostics=dict(estimate.diagnostics),
            ),
            request,
            expires_hours=expires_hours,
        )

    as_of = isoformat_utc(parse_utc(request.as_of))
    expires_at = isoformat_utc(parse_utc(as_of) + timedelta(hours=expires_hours))
    sources = list(estimate.diagnostics.get("sources") or [])
    if not sources:
        return estimate_to_decision(
            SpecialistEstimate(
                status="ABSTAIN",
                calibration_version=estimate.calibration_version,
                reason="no_sources_available_at",
                diagnostics=dict(estimate.diagnostics),
            ),
            request,
            expires_hours=expires_hours,
        )

    p_yes = float(estimate.p_yes)
    model_version = _model_version_of(estimate)
    forecast = {
        "forecast_id": forecast_id_for(
            market_id=request.market_id,
            as_of=as_of,
            rules_hash=request.rules_hash,
            model_version=model_version,
            p_yes=str(estimate.p_yes),
        ),
        "market_id": request.market_id,
        "model_version": model_version,
        "rules_hash": request.rules_hash,
        "as_of": as_of,
        "expires_at": expires_at,
        "p_yes": p_yes,
        "p_low": float(estimate.p_low),
        "p_high": float(estimate.p_high),
        "sources": sources,
        "thesis": str(estimate.diagnostics.get("thesis") or estimate.reason),
        "invalidation": str(
            estimate.diagnostics.get("invalidation")
            or "Rule change, new vintage, or look-ahead evidence."
        ),
    }
    return {
        "action": "PROPOSE",
        "reason": estimate.reason,
        "model_name": _model_name_of(estimate),
        "model_version": model_version,
        "calibration_version": estimate.calibration_version,
        "forecast": forecast,
        "variants": estimate.diagnostics.get("variants"),
        "comparison": comparison_from_estimate(estimate),
        "diagnostics": dict(estimate.diagnostics),
        "imported": False,
        "edge_proven": False,
        "note": (
            "Post forecast to POST /api/forecast to enter the paper path. "
            "risk-v2 sizes and may refuse. Not a live order. Not a proven edge."
        ),
    }


def run_weather_baseline(
    request: ResearchRequest,
    *,
    specialist: WeatherStationBaseline | None = None,
    expires_hours: float = 3.0,
) -> dict[str, Any]:
    model = specialist or WeatherStationBaseline()
    estimate = model.estimate(request)
    return estimate_to_decision(estimate, request, expires_hours=expires_hours)


def variants_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    raw = decision.get("variants") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}
