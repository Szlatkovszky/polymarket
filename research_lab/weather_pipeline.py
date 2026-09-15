"""Turn a weather specialist estimate into a forecast/decision envelope.

Does not size trades. risk-v2 remains the only stake/decision authority.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from research_lab.hashing import sha256_hex
from research_lab.specialist import (
    WEATHER_MODEL_VERSION,
    ResearchRequest,
    SpecialistEstimate,
    WeatherStationBaseline,
)
from research_lab.timeutil import isoformat_utc, parse_utc


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
            "model_name": str(estimate.diagnostics.get("model") or "weather-station-baseline-v1"),
            "model_version": str(
                estimate.diagnostics.get("model_version") or WEATHER_MODEL_VERSION
            ),
            "calibration_version": estimate.calibration_version,
            "variants": estimate.diagnostics.get("variants"),
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
    forecast = {
        "forecast_id": forecast_id_for(
            market_id=request.market_id,
            as_of=as_of,
            rules_hash=request.rules_hash,
            model_version=WEATHER_MODEL_VERSION,
            p_yes=str(estimate.p_yes),
        ),
        "market_id": request.market_id,
        "model_version": WEATHER_MODEL_VERSION,
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
        "model_name": "weather-station-baseline-v1",
        "model_version": WEATHER_MODEL_VERSION,
        "calibration_version": estimate.calibration_version,
        "forecast": forecast,
        "variants": estimate.diagnostics.get("variants"),
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
