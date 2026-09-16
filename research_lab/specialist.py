"""Specialist probability models.

The weather station/date baseline is the primary *numeric* research path.
Placeholder ABSTAIN remains for non-weather or unimplemented families.
Grok is not a calibrated probability and does not size trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Mapping, Protocol

from research_lab.money import D
from research_lab.research_budget import ResearchBudget, weather_specialist_enabled
from research_lab.timeutil import isoformat_utc, parse_utc
from research_lab.weather_contract import (
    ContractParse,
    bounds_to_celsius,
    parse_weather_contract,
)
from research_lab.weather_math import (
    IdentityBoundaryHook,
    conservative_band,
    interval_prob,
    quantize_prob,
    rounding_rule_from_review_hint,
)
from research_lab.weather_source import (
    FixtureNWS,
    NetworkNWS,
    WeatherSourceError,
    WeatherSnapshot,
    build_weather_source,
    unofficial_series_max_c,
)

WEATHER_MODEL_VERSION = "weather-station-baseline-v1-calibration-identity-stub-v0"
WEATHER_CALIBRATION_VERSION = "identity-stub-v0"
CONSERVATIVE_DEDUCTION = D("0.05")


@dataclass(frozen=True)
class ResearchRequest:
    market_id: str
    condition_id: str | None
    rules_text: str
    rules_hash: str
    as_of: str
    cutoff_at: str | None
    resolution_source: str | None
    specialist_hints: Mapping[str, Any] = field(default_factory=dict)


def research_request_to_dict(request: ResearchRequest) -> dict[str, Any]:
    """Canonical raw inputs so a logged run can be replayed offline."""

    return {
        "market_id": request.market_id,
        "condition_id": request.condition_id,
        "rules_text": request.rules_text,
        "rules_hash": request.rules_hash,
        "as_of": request.as_of,
        "cutoff_at": request.cutoff_at,
        "resolution_source": request.resolution_source,
        "specialist_hints": dict(request.specialist_hints or {}),
    }


def research_request_from_dict(payload: Mapping[str, Any]) -> ResearchRequest:
    return ResearchRequest(
        market_id=str(payload.get("market_id") or ""),
        condition_id=payload.get("condition_id"),
        rules_text=str(payload.get("rules_text") or ""),
        rules_hash=str(payload.get("rules_hash") or ""),
        as_of=str(payload.get("as_of") or ""),
        cutoff_at=payload.get("cutoff_at"),
        resolution_source=payload.get("resolution_source"),
        specialist_hints=dict(payload.get("specialist_hints") or {}),
    )


@dataclass(frozen=True)
class SpecialistEstimate:
    status: Literal["ABSTAIN", "ESTIMATE"]
    calibration_version: str
    p_yes: Decimal | None = None
    p_low: Decimal | None = None
    p_high: Decimal | None = None
    reason: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class SpecialistModel(Protocol):
    name: str

    def estimate(self, request: ResearchRequest) -> SpecialistEstimate: ...


class IdentityCalibrator:
    """External-calibration hook. Currently identity; vintage is still logged."""

    version = WEATHER_CALIBRATION_VERSION

    def apply(
        self,
        mu: Decimal,
        sigma: Decimal,
        *,
        station_id: str,
        horizon_hours: Decimal,
    ) -> tuple[Decimal, Decimal]:
        del station_id, horizon_hours
        return mu, sigma


class PlaceholderSpecialist:
    """Neutral baseline: ABSTAIN. A language-model confidence is not a CI."""

    name = "placeholder-abstain-v0"
    calibration_version = "unmeasured-v0"

    def estimate(self, request: ResearchRequest) -> SpecialistEstimate:
        return SpecialistEstimate(
            status="ABSTAIN",
            calibration_version=self.calibration_version,
            reason="specialist_not_implemented",
            diagnostics={
                "model": self.name,
                "rules_hash": request.rules_hash,
                "note": "Do not treat this placeholder as a calibrated probability.",
            },
        )


class WeatherStationBaseline:
    """Station + local-date daily-max baseline.

    Target is the *contract resolution quantity* (station, local date, rounding)
    not city weather. Normal errors are a starting baseline only.

    Always logs four comparison tracks when an estimate is produced:
    raw model, calibrated model (identity stub allowed), historical base rate,
    contemporaneous market mid. Missing comparison tracks are logged as
    unavailable — they do not invent numbers.
    """

    name = "weather-station-baseline-v1"
    calibration_version = WEATHER_CALIBRATION_VERSION
    model_version = WEATHER_MODEL_VERSION

    def __init__(
        self,
        source: FixtureNWS | NetworkNWS | None = None,
        *,
        calibrator: IdentityCalibrator | None = None,
        budget: ResearchBudget | None = None,
        boundary_hook: IdentityBoundaryHook | None = None,
        conservative_deduction: Decimal = CONSERVATIVE_DEDUCTION,
    ) -> None:
        self.source = source if source is not None else build_weather_source(budget)
        self.calibrator = calibrator or IdentityCalibrator()
        self.budget = budget
        self.boundary_hook = boundary_hook or IdentityBoundaryHook()
        self.conservative_deduction = conservative_deduction

    def estimate(self, request: ResearchRequest) -> SpecialistEstimate:
        if not weather_specialist_enabled():
            return _abstain(self, request, "weather_specialist_disabled")
        if not (request.as_of or "").strip():
            return _abstain(self, request, "missing_as_of")
        try:
            as_of = isoformat_utc(parse_utc(request.as_of))
        except ValueError:
            return _abstain(self, request, "invalid_as_of")

        parsed: ContractParse = parse_weather_contract(
            rules_text=request.rules_text,
            rules_hash=request.rules_hash,
            resolution_source=request.resolution_source,
            hints=request.specialist_hints,
        )
        if not parsed.ok or parsed.contract is None:
            return _abstain(
                self,
                request,
                parsed.reason,
                extra={"parse": parsed.details},
            )
        contract = parsed.contract
        rounding_source = "rules_text"
        if contract.rounding.mode == "unspecified":
            override, rounding_source = rounding_rule_from_review_hint(
                _review_rounding_hint(request.specialist_hints),
                contract_unit=contract.unit,
            )
            if override is None:
                return _abstain(
                    self,
                    request,
                    rounding_source or "rounding_unspecified",
                    extra={
                        "parse": parsed.details,
                        "note": (
                            "NOAA city markets often omit the rounding algorithm. "
                            "Do not invent half-up; wait for human rules-review "
                            "to record rounding_mode plus rounding_increment."
                        ),
                    },
                )
            contract = replace(contract, rounding=override)

        try:
            snap = self.source.snapshot(
                station_id=contract.station_id,
                local_date=contract.local_date,
                as_of=as_of,
                event_id=contract.event_id,
                month_key=contract.month_key(),
            )
        except WeatherSourceError as exc:
            reason = str(exc)
            if reason.startswith("missing_station"):
                return _abstain(self, request, "missing_station", extra={"error": reason})
            return _abstain(self, request, "weather_source_error", extra={"error": reason})

        return self._estimate_from_snapshot(
            request, contract, snap, as_of, rounding_source=rounding_source
        )

    def _estimate_from_snapshot(
        self,
        request: ResearchRequest,
        contract,
        snap: WeatherSnapshot,
        as_of: str,
        *,
        rounding_source: str = "rules_text",
    ) -> SpecialistEstimate:
        fc = snap.forecast
        if fc is None:
            return _abstain(
                self,
                request,
                "no_vintage_available",
                extra=_snapshot_diag(snap, contract),
            )
        if parse_utc(fc.available_at) > parse_utc(as_of):
            return _abstain(self, request, "forecast_look_ahead")
        if fc.predicted_max_c is None:
            return _abstain(
                self,
                request,
                "missing_forecast_max",
                extra={
                    **_snapshot_diag(snap, contract),
                    "note": "Missing predicted max is not 0.",
                },
            )
        if snap.sigma_c is None or snap.sigma_c <= 0:
            return _abstain(
                self,
                request,
                "missing_error_scale",
                extra=_snapshot_diag(snap, contract),
            )

        unofficial = unofficial_series_max_c(snap.observations)
        if not snap.official_daily_max.complete:
            # Expected for in-progress days. Never promote unofficial max.
            unofficial_note = (
                "incomplete_series_not_used_as_official_daily_max"
                if unofficial is not None
                else snap.official_daily_max.reason
            )
        else:
            unofficial_note = "official_field_present"

        lower_native, upper_native = contract.rounding.underlying_interval(
            contract.rounded_lower,
            contract.rounded_upper,
            hook=self.boundary_hook,
        )
        # Forecast μ is Celsius. Map the underlying interval into C after
        # rounding in the contract's native unit (do not round in mixed units).
        lower_u, upper_u = bounds_to_celsius(
            lower_native, upper_native, contract.unit
        )
        raw_p = interval_prob(fc.predicted_max_c, snap.sigma_c, lower_u, upper_u)
        mu_c, sigma_c = self.calibrator.apply(
            fc.predicted_max_c,
            snap.sigma_c,
            station_id=contract.station_id,
            horizon_hours=fc.horizon_hours,
        )
        cal_p = interval_prob(mu_c, sigma_c, lower_u, upper_u)
        p_low, p_high = conservative_band(
            cal_p, deduction=self.conservative_deduction
        )

        market_mid = _hint_prob(request.specialist_hints, "market_mid")
        market_mid_at = request.specialist_hints.get("market_mid_available_at")
        if market_mid_at:
            try:
                if parse_utc(str(market_mid_at)) > parse_utc(as_of):
                    market_mid = None
                    market_mid_at = "rejected_look_ahead"
            except ValueError:
                market_mid = None
                market_mid_at = "invalid_timestamp"

        variants = {
            "raw_model": {
                "p_yes": str(raw_p),
                "mu_c": str(fc.predicted_max_c),
                "sigma_c": str(snap.sigma_c),
                "sigma_source": snap.sigma_source,
            },
            "calibrated_model": {
                "p_yes": str(cal_p),
                "mu_c": str(mu_c),
                "sigma_c": str(sigma_c),
                "calibrator": self.calibrator.version,
                "note": (
                    "Identity stub until an external calibration vintage exists. "
                    "Not a claim that the model is calibrated."
                ),
            },
            "historical_base_rate": {
                "p_yes": None if snap.climatology_p is None else str(snap.climatology_p),
                "sample_n": snap.climatology_n,
                "note": snap.climatology_note or "climatology_unavailable",
            },
            "market_mid": {
                "p_yes": None if market_mid is None else str(quantize_prob(market_mid)),
                "available_at": market_mid_at,
                "note": "Contemporaneous YES mid; missing stays unavailable, not 0.",
            },
        }

        sources = _sources_from_snapshot(snap, fc, as_of)
        if not sources:
            return _abstain(self, request, "no_sources_available_at")

        lo_s = "-inf" if contract.rounded_lower is None else str(contract.rounded_lower)
        hi_s = "+inf" if contract.rounded_upper is None else str(contract.rounded_upper)
        ulo_s = "-inf" if lower_u is None else str(lower_u)
        uhi_s = "+inf" if upper_u is None else str(upper_u)
        thesis = (
            f"Station {contract.station_id} local date {contract.local_date} "
            f"({contract.timezone}) daily max vs rounded interval "
            f"[{lo_s}, {hi_s}) {contract.unit}. "
            f"Predicted max {fc.predicted_max_c} C from {fc.model_run} issued "
            f"{fc.issued_at}, horizon {fc.horizon_hours}h, sigma {snap.sigma_c} C "
            f"({snap.sigma_source}). Interval prob uses Normal F(b)-F(a) after "
            f"{contract.rounding.mode} rounding to {contract.rounding.increment} C "
            f"(underlying [{ulo_s}, {uhi_s})). "
            "Normal is a starting baseline: tails understate extremes and "
            "regime shifts. Identity calibration stub. Not a profitability claim."
        )
        invalidation = (
            "New model vintage, official daily-max field missing at settlement, "
            "rules_hash change, non-NWS resolution source, look-ahead evidence "
            "(available_at > as_of), or incomplete series used as official max."
        )

        return SpecialistEstimate(
            status="ESTIMATE",
            calibration_version=self.calibrator.version,
            p_yes=cal_p,
            p_low=p_low,
            p_high=p_high,
            reason="weather_station_baseline",
            diagnostics={
                "model": self.name,
                "model_version": self.model_version,
                "calibration_version": self.calibrator.version,
                "limitations": [
                    "normal_tails_understate_extremes",
                    "regime_shifts_unmodeled",
                    "calibration_is_identity_stub",
                    "nws_forecast_max_is_not_the_official_daily_max",
                ],
                "contract": {
                    "station_id": contract.station_id,
                    "local_date": contract.local_date,
                    "timezone": contract.timezone,
                    "quantity": contract.quantity,
                    "event_id": contract.event_id,
                    "rounded_lower": str(contract.rounded_lower),
                    "rounded_upper": None
                    if contract.rounded_upper is None
                    else str(contract.rounded_upper),
                    "rounding_mode": contract.rounding.mode,
                    "rounding_increment": str(contract.rounding.increment),
                    "rounding_source": rounding_source,
                    "underlying_lower": None if lower_u is None else str(lower_u),
                    "underlying_upper": None if upper_u is None else str(upper_u),
                    "resolution_source": contract.resolution_source,
                },
                "forecast_vintage": {
                    "issued_at": fc.issued_at,
                    "available_at": fc.available_at,
                    "model_run": fc.model_run,
                    "predicted_max_c": str(fc.predicted_max_c),
                    "horizon_hours": str(fc.horizon_hours),
                },
                "observations_known_at_as_of": [
                    {
                        "id": obs.observation_id,
                        "observed_at": obs.observed_at,
                        "available_at": obs.available_at,
                        "temperature_c": None
                        if obs.temperature_c is None
                        else str(obs.temperature_c),
                        "daily_max_c": None
                        if obs.daily_max_c is None
                        else str(obs.daily_max_c),
                        "max_min_fields_present": obs.max_min_fields_present,
                    }
                    for obs in snap.observations
                ],
                "official_daily_max": {
                    "complete": snap.official_daily_max.complete,
                    "value_c": None
                    if snap.official_daily_max.value_c is None
                    else str(snap.official_daily_max.value_c),
                    "reason": snap.official_daily_max.reason,
                    "unofficial_series_max_c": None
                    if unofficial is None
                    else str(unofficial),
                    "unofficial_note": unofficial_note,
                },
                "variants": variants,
                "sources": sources,
                "thesis": thesis,
                "invalidation": invalidation,
                "stake_authority": "risk-v2",
                "edge_proven": False,
            },
        )


def _abstain(
    model: WeatherStationBaseline,
    request: ResearchRequest,
    reason: str,
    *,
    extra: Mapping[str, Any] | None = None,
) -> SpecialistEstimate:
    diag: dict[str, Any] = {
        "model": model.name,
        "model_version": model.model_version,
        "rules_hash": request.rules_hash,
        "reason": reason,
        "edge_proven": False,
        "stake_authority": "risk-v2",
    }
    if extra:
        diag.update(dict(extra))
    return SpecialistEstimate(
        status="ABSTAIN",
        calibration_version=model.calibration_version,
        reason=reason,
        diagnostics=diag,
    )


def _review_rounding_hint(hints: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = hints.get("rules_review_rounding")
    if isinstance(nested, Mapping) and nested:
        return dict(nested)
    flat: dict[str, Any] = {}
    for src, dest in (
        ("rounding_mode", "mode"),
        ("rounding_increment", "increment"),
        ("rounding_unit", "unit"),
    ):
        value = hints.get(src)
        if value not in (None, ""):
            flat[dest] = value
    return flat or None


def _snapshot_diag(snap: WeatherSnapshot, contract) -> dict[str, Any]:
    return {
        "station_id": contract.station_id,
        "local_date": contract.local_date,
        "provider": snap.provider,
        "official_daily_max_reason": snap.official_daily_max.reason,
    }


def _hint_prob(hints: Mapping[str, Any], key: str) -> Decimal | None:
    if key not in hints or hints[key] is None:
        return None
    value = D(hints[key])
    if value < 0 or value > 1:
        return None
    return value


def _sources_from_snapshot(snap: WeatherSnapshot, fc, as_of: str) -> list[dict[str, str]]:
    as_of_dt = parse_utc(as_of)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(url: str, available_at: str) -> None:
        try:
            avail = parse_utc(available_at)
        except ValueError:
            return
        if avail > as_of_dt:
            return
        key = (url, isoformat_utc(avail))
        if key in seen:
            return
        seen.add(key)
        rows.append({"url": url, "available_at": key[1]})

    add(f"https://api.weather.gov/stations/{snap.station_id}", fc.available_at)
    add(fc.url, fc.available_at)
    for obs in snap.observations:
        add(obs.url, obs.available_at)
    return rows
