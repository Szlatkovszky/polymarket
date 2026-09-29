"""Same-day floor on the daily-max distribution.

For an event date that is today in the station timezone, a temperature already
observed cannot be exceeded *downward* by the eventual daily max. The predictive
distribution is left-truncated at that observed max, and the error scale shrinks
with the fraction of the local afternoon still ahead.

The diurnal window (08:00–18:00 local) and the residual fraction after 18:00
are assumptions, not a fitted nowcast. The floor is not an official daily max
and is not a settlement value. Nothing here is statistical validation and
nothing here proves an edge.

Changing ``DIURNAL_*``, ``RESIDUAL_FRACTION``, or ``MIN_SIGMA_C`` changes
probabilities and requires a bump of ``MODEL_FAMILY`` in ``sigma_calibration``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from research_lab.money import D
from research_lab.timeutil import parse_utc
from research_lab.weather_math import interval_mass, interval_prob, normal_cdf, quantize_prob

# Local civil hours during which the daily max is still typically undecided.
# Assumption, not a fitted parameter.
DIURNAL_START_HOUR = D(8)
DIURNAL_END_HOUR = D(18)
# After the window, keep a little spread so the distribution does not collapse
# to a point. Assumption, not a fitted parameter.
RESIDUAL_FRACTION = D("0.05")
MIN_SIGMA_C = D("0.15")
SIGMA_QUANT = D("0.000001")
# Below this survival, the unconditional forecast is numerically incompatible
# with the observed floor and the location is shifted up to that floor.
SURVIVAL_EPS = 1e-12


class SameDayTimezoneError(ValueError):
    pass


class TemperatureObservation(Protocol):
    observation_id: str
    observed_at: str
    available_at: str
    temperature_c: Decimal | None
    url: str


@dataclass(frozen=True)
class SameDayState:
    """How the daily-max distribution should treat observations so far."""

    mode: str
    reason: str
    floor_c: Decimal | None = None
    fraction_remaining: Decimal | None = None
    observation_id: str | None = None
    observed_at: str | None = None
    available_at: str | None = None
    url: str | None = None
    n_temperatures: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "floor_c": None if self.floor_c is None else str(self.floor_c),
            "fraction_remaining": None
            if self.fraction_remaining is None
            else str(self.fraction_remaining),
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "url": self.url,
            "n_temperatures": self.n_temperatures,
            "note": self.note,
            "edge_proven": False,
            "official_daily_max": False,
        }


def diurnal_fraction_remaining(local_hour: Decimal) -> Decimal:
    """Fraction of daily-max uncertainty still ahead. In ``[RESIDUAL_FRACTION, 1]``."""

    hour = D(local_hour)
    if hour <= DIURNAL_START_HOUR:
        return D(1)
    if hour >= DIURNAL_END_HOUR:
        return RESIDUAL_FRACTION
    span = DIURNAL_END_HOUR - DIURNAL_START_HOUR
    progressed = (hour - DIURNAL_START_HOUR) / span
    frac = D(1) - progressed * (D(1) - RESIDUAL_FRACTION)
    return frac.quantize(SIGMA_QUANT)


def conditioned_sigma(sigma: Decimal, fraction_remaining: Decimal) -> Decimal:
    """Shrink ``sigma`` by ``sqrt(fraction)``, floored at ``MIN_SIGMA_C``.

    The floor is an assumption so a late-day estimate does not divide by zero.
    It is not a measured instrument error.
    """

    if sigma is None or D(sigma) <= 0:
        raise ValueError("sigma must be positive; missing error scale is not 0")
    original = D(sigma)
    frac = D(fraction_remaining)
    if frac <= 0:
        raise ValueError("fraction_remaining must be positive")
    shrunk = (original * frac.sqrt()).quantize(SIGMA_QUANT)
    # The floor stops a late-day scale from collapsing. It must not widen
    # a scale that was already smaller than the floor.
    if shrunk < MIN_SIGMA_C:
        shrunk = MIN_SIGMA_C if MIN_SIGMA_C < original else original
    if shrunk > original:
        shrunk = original
    return shrunk


def local_calendar_date(instant: datetime, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise SameDayTimezoneError(timezone) from exc
    return instant.astimezone(zone).date().isoformat()


def local_hour_fraction(instant: datetime, timezone: str) -> Decimal:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise SameDayTimezoneError(timezone) from exc
    local = instant.astimezone(zone)
    return D(local.hour) + (D(local.minute) / D(60)) + (D(local.second) / D(3600))


def _survival(mu: Decimal, sigma: Decimal, floor: Decimal) -> float:
    z = (float(D(floor)) - float(D(mu))) / float(D(sigma))
    return max(0.0, 1.0 - normal_cdf(z))


def truncated_interval_prob(
    mu: Decimal,
    sigma: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
    floor: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """P(lower <= T < upper | T >= floor).

    Returns ``(probability, mu_used, shifted_to_floor)``. When the unconditional
    forecast puts negligible mass at or above ``floor``, ``mu`` is moved to the
    floor (remaining rise only) and the distribution stays left-truncated.
    """

    if sigma is None or D(sigma) <= 0:
        raise ValueError("sigma must be positive; missing error scale is not 0")
    mu_used = D(mu)
    floor_c = D(floor)
    shifted = False
    survival = _survival(mu_used, D(sigma), floor_c)
    if survival < SURVIVAL_EPS:
        mu_used = floor_c
        shifted = True
        survival = _survival(mu_used, D(sigma), floor_c)
    lo: Decimal | None = floor_c if lower is None or D(lower) < floor_c else D(lower)
    if upper is not None and lo is not None and lo >= D(upper):
        return quantize_prob(D(0)), mu_used, shifted
    mass = interval_mass(mu_used, D(sigma), lo, upper)
    if survival <= 0:
        p = 0.0
    else:
        p = mass / survival
    return quantize_prob(D(str(max(0.0, min(1.0, p))))), mu_used, shifted


def select_observed_max(
    observations: tuple[TemperatureObservation, ...] | list[TemperatureObservation],
    *,
    local_date: str,
    timezone: str,
    as_of: datetime,
) -> tuple[TemperatureObservation | None, int]:
    """Max ``temperature_c`` on ``local_date`` with ``available_at <= as_of``.

    Uses the hourly temperature, not ``daily_max_c``. The NWS
    ``maxTemperatureLast24Hours`` field is a rolling 24-hour max and can include
    the previous local date. It is not the calendar-day floor.
    """

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise SameDayTimezoneError(timezone) from exc
    chosen: TemperatureObservation | None = None
    n = 0
    for obs in observations:
        try:
            available = parse_utc(obs.available_at)
            observed = parse_utc(obs.observed_at)
        except ValueError:
            continue
        if available > as_of:
            continue
        if observed.astimezone(zone).date().isoformat() != local_date:
            continue
        if obs.temperature_c is None:
            continue
        n += 1
        if chosen is None or D(obs.temperature_c) > D(chosen.temperature_c):
            chosen = obs
        elif D(obs.temperature_c) == D(chosen.temperature_c) and available > parse_utc(
            chosen.available_at
        ):
            chosen = obs
    return chosen, n


def assess_same_day(
    *,
    as_of: datetime,
    local_date: str,
    timezone: str,
    observations: tuple[TemperatureObservation, ...] | list[TemperatureObservation],
    observations_status: str,
    observations_note: str | None = None,
) -> SameDayState:
    """Decide whether to truncate. Never invents a temperature."""

    try:
        today = local_calendar_date(as_of, timezone)
    except SameDayTimezoneError:
        return SameDayState(
            mode="abstain",
            reason="invalid_station_timezone",
            note=(
                "Station timezone could not be resolved. "
                "Refusing to guess whether the event date is today. "
                "No temperature was invented."
            ),
        )
    if today != local_date:
        return SameDayState(
            mode="not_same_day",
            reason="event_date_is_not_today",
            note=(
                "Event date is not today in the station timezone. "
                "Same-day conditioning does not apply."
            ),
        )
    if observations_status != "available":
        detail = observations_note or "observations_endpoint_unavailable"
        return SameDayState(
            mode="abstain",
            reason="same_day_observations_unavailable",
            note=(
                "Same-day event but observations could not be fetched "
                f"({detail}). Refusing to emit a daily-max distribution that "
                "ignores the max so far. No temperature was invented."
            ),
        )
    try:
        chosen, n = select_observed_max(
            observations,
            local_date=local_date,
            timezone=timezone,
            as_of=as_of,
        )
        fraction = diurnal_fraction_remaining(local_hour_fraction(as_of, timezone))
    except SameDayTimezoneError:
        return SameDayState(
            mode="abstain",
            reason="invalid_station_timezone",
            note=(
                "Station timezone could not be resolved while reading observations. "
                "No temperature was invented."
            ),
        )
    if chosen is None or chosen.temperature_c is None:
        return SameDayState(
            mode="fallback_no_temperature_yet",
            reason="same_day_no_temperature_yet",
            fraction_remaining=None,
            n_temperatures=0,
            note=(
                "Same-day event and the observation source returned no temperature "
                "for this local date at or before as_of "
                f"({observations_note or 'no_temperature'}). "
                "Unconditional forecast used. No temperature was invented. "
                "This is not statistical validation and does not prove an edge."
            ),
        )
    return SameDayState(
        mode="conditioned",
        reason="conditioned_on_observed_max",
        floor_c=D(chosen.temperature_c),
        fraction_remaining=fraction,
        observation_id=chosen.observation_id,
        observed_at=chosen.observed_at,
        available_at=chosen.available_at,
        url=chosen.url,
        n_temperatures=n,
        note=(
            "Observed max so far is a hard floor on the daily max. "
            "Sigma is scaled by sqrt(fraction of the 08:00-18:00 local window "
            "still ahead), with a residual fraction after 18:00. "
            "Those hours and the residual are assumptions, not a fitted nowcast. "
            "The floor is not an official daily max and is not a settlement value. "
            "This is not statistical validation and does not prove an edge."
        ),
    )


@dataclass(frozen=True)
class PredictiveDraw:
    p_yes: Decimal
    mu_c: Decimal
    sigma_c: Decimal
    shifted_to_floor: bool


def predictive_probability(
    mu: Decimal,
    sigma: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
    state: SameDayState,
) -> PredictiveDraw:
    """Interval probability after same-day truncation, or the unconditional one."""

    if (
        state.mode != "conditioned"
        or state.floor_c is None
        or state.fraction_remaining is None
    ):
        return PredictiveDraw(
            p_yes=interval_prob(mu, sigma, lower, upper),
            mu_c=D(mu),
            sigma_c=D(sigma),
            shifted_to_floor=False,
        )
    sigma_used = conditioned_sigma(D(sigma), state.fraction_remaining)
    p_yes, mu_used, shifted = truncated_interval_prob(
        mu, sigma_used, lower, upper, state.floor_c
    )
    return PredictiveDraw(
        p_yes=p_yes,
        mu_c=mu_used,
        sigma_c=sigma_used,
        shifted_to_floor=shifted,
    )
