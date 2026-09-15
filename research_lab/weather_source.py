"""NWS / contract-matching weather archive.

Default is recorded fixtures so CI never needs the network. Optional live GET
is behind NWS_ALLOW_NETWORK=1 and WEATHER_DATA_SOURCE=network.

Caveats encoded here (NWS services docs):
- Observation lag is treated as ~20 minutes unless the archive says otherwise.
- Missing max/min fields stay None — never coerced to 0.
- An incomplete observation series is never turned into an official daily max.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from research_lab.money import D
from research_lab.research_budget import ResearchBudget
from research_lab.timeutil import isoformat_utc, parse_utc

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
NWS_HOSTS = frozenset(
    {
        "api.weather.gov",
        "weather.gov",
        "www.weather.gov",
        "forecast.weather.gov",
    }
)
DEFAULT_OBS_LAG = timedelta(minutes=20)


class WeatherSourceError(RuntimeError):
    pass


def weather_data_source_from_env() -> str:
    source = os.environ.get("WEATHER_DATA_SOURCE", "fixtures").strip().lower()
    if source not in {"fixtures", "network"}:
        raise WeatherSourceError("WEATHER_DATA_SOURCE must be fixtures or network")
    return source


def nws_network_allowed() -> bool:
    return os.environ.get("NWS_ALLOW_NETWORK", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _optional_celsius(value: Any) -> Decimal | None:
    """Parse a temperature. JSON null / missing → None. Explicit 0 stays 0."""

    if value is None:
        return None
    return D(value)


@dataclass(frozen=True)
class WeatherForecastRecord:
    station_id: str
    issued_at: str
    available_at: str
    model_run: str
    valid_date_local: str
    predicted_max_c: Decimal | None
    horizon_hours: Decimal
    url: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WeatherObservationRecord:
    station_id: str
    observation_id: str
    observed_at: str
    available_at: str
    temperature_c: Decimal | None
    daily_max_c: Decimal | None
    daily_min_c: Decimal | None
    max_min_fields_present: bool
    series_complete_for_local_date: bool
    url: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OfficialDailyMax:
    value_c: Decimal | None
    complete: bool
    reason: str
    source_url: str | None = None


@dataclass(frozen=True)
class WeatherSnapshot:
    station_id: str
    as_of: str
    forecast: WeatherForecastRecord | None
    observations: tuple[WeatherObservationRecord, ...]
    official_daily_max: OfficialDailyMax
    sigma_c: Decimal | None
    sigma_source: str
    climatology_p: Decimal | None
    climatology_n: int | None
    climatology_note: str | None
    provider: str
    archive_urls: tuple[str, ...]


class FixtureNWS:
    """Offline NWS-shaped archive. Never opens a socket."""

    source_name = "fixtures"

    def __init__(self, path: Path | None = None, *, observation_lag: timedelta | None = None) -> None:
        self.path = path or (FIXTURE_DIR / "nws_archive.json")
        self._archive = json.loads(self.path.read_text(encoding="utf-8"))
        lag_min = int(self._archive.get("observation_lag_minutes") or 20)
        self.observation_lag = observation_lag or timedelta(minutes=lag_min)

    def snapshot(
        self,
        *,
        station_id: str,
        local_date: str,
        as_of: str,
        event_id: str | None = None,
        month_key: str | None = None,
    ) -> WeatherSnapshot:
        stations: Mapping[str, Any] = self._archive.get("stations") or {}
        if station_id not in stations:
            raise WeatherSourceError(f"missing_station:{station_id}")
        as_of_dt = parse_utc(as_of)
        station = stations[station_id]
        forecasts = [
            _parse_forecast(station_id, row)
            for row in station.get("forecasts") or []
        ]
        usable_forecasts = [
            row
            for row in forecasts
            if parse_utc(row.available_at) <= as_of_dt
            and row.valid_date_local == local_date
        ]
        forecast = None
        if usable_forecasts:
            forecast = max(usable_forecasts, key=lambda row: parse_utc(row.issued_at))

        observations = []
        for raw in station.get("observations") or []:
            obs = _parse_observation(station_id, raw, lag=self.observation_lag)
            if parse_utc(obs.available_at) <= as_of_dt:
                observations.append(obs)

        official = official_daily_max(observations, local_date=local_date)
        sigma, sigma_source = _sigma_for(
            self._archive,
            station_id=station_id,
            horizon_hours=None if forecast is None else forecast.horizon_hours,
        )
        clim_p, clim_n, clim_note = _climatology(
            self._archive,
            station_id=station_id,
            month_key=month_key or (local_date[5:7] if local_date else None),
            event_id=event_id,
        )
        urls = [f"https://api.weather.gov/stations/{station_id}"]
        if forecast is not None:
            urls.append(forecast.url)
        urls.extend(obs.url for obs in observations)
        return WeatherSnapshot(
            station_id=station_id,
            as_of=isoformat_utc(as_of_dt),
            forecast=forecast,
            observations=tuple(observations),
            official_daily_max=official,
            sigma_c=sigma,
            sigma_source=sigma_source,
            climatology_p=clim_p,
            climatology_n=clim_n,
            climatology_note=clim_note,
            provider=self.source_name,
            archive_urls=tuple(urls),
        )


class NetworkNWS:
    """GET-only NWS client. Untrusted JSON is data, not instructions."""

    source_name = "network"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 20.0,
        *,
        budget: ResearchBudget | None = None,
        observation_lag: timedelta | None = None,
    ) -> None:
        if not nws_network_allowed():
            raise WeatherSourceError("network NWS adapter requires NWS_ALLOW_NETWORK=1")
        self.base_url = (
            base_url or os.environ.get("NWS_API_BASE") or "https://api.weather.gov"
        ).rstrip("/")
        _assert_nws_host(self.base_url)
        self.timeout = timeout
        self.budget = budget
        self.observation_lag = observation_lag or DEFAULT_OBS_LAG

    def snapshot(
        self,
        *,
        station_id: str,
        local_date: str,
        as_of: str,
        event_id: str | None = None,
        month_key: str | None = None,
    ) -> WeatherSnapshot:
        as_of_dt = parse_utc(as_of)
        station_url = urljoin(self.base_url + "/", f"stations/{station_id}")
        obs_url = urljoin(self.base_url + "/", f"stations/{station_id}/observations")
        station_payload = self._get(station_url)
        obs_payload = self._get(obs_url)
        if not isinstance(station_payload, dict):
            raise WeatherSourceError("unexpected NWS station payload")
        observations = _observations_from_nws(
            station_id, obs_payload, lag=self.observation_lag, as_of=as_of_dt
        )
        if not observations and not station_payload:
            raise WeatherSourceError(f"missing_station:{station_id}")
        official = official_daily_max(observations, local_date=local_date)
        # Live NWS daily-max *forecast* mapping is not a substitute for the
        # contractual official daily maximum. Without a recorded vintage this
        # adapter refuses to invent a predicted max.
        return WeatherSnapshot(
            station_id=station_id,
            as_of=isoformat_utc(as_of_dt),
            forecast=None,
            observations=tuple(observations),
            official_daily_max=official,
            sigma_c=None,
            sigma_source="network_unmeasured",
            climatology_p=None,
            climatology_n=None,
            climatology_note="live NWS path has no fixture climatology table",
            provider=self.source_name,
            archive_urls=(station_url, obs_url),
        )

    def _get(self, url: str) -> Any:
        if self.budget is not None:
            self.budget.charge(self.budget.run_unit_cost_usd, reason=f"nws_get:{url}")
        return _nws_http_get(url, timeout=self.timeout)


def build_weather_source(budget: ResearchBudget | None = None) -> FixtureNWS | NetworkNWS:
    source = weather_data_source_from_env()
    if source == "network":
        return NetworkNWS(budget=budget)
    return FixtureNWS()


def official_daily_max(
    observations: list[WeatherObservationRecord] | tuple[WeatherObservationRecord, ...],
    *,
    local_date: str,
) -> OfficialDailyMax:
    """Return the official field if present. Never max() an incomplete series."""

    del local_date  # local-date filtering belongs to the provider; do not infer.
    if not observations:
        return OfficialDailyMax(None, False, "no_observations")
    official_values = [
        obs
        for obs in observations
        if obs.max_min_fields_present and obs.daily_max_c is not None
    ]
    if official_values:
        latest = max(official_values, key=lambda row: parse_utc(row.observed_at))
        return OfficialDailyMax(
            latest.daily_max_c, True, "official_field", latest.url
        )
    if any(obs.daily_max_c is None or not obs.max_min_fields_present for obs in observations):
        return OfficialDailyMax(None, False, "missing_max_min_fields")
    return OfficialDailyMax(None, False, "incomplete_series")


def unofficial_series_max_c(
    observations: list[WeatherObservationRecord] | tuple[WeatherObservationRecord, ...],
) -> Decimal | None:
    """Diagnostic only. Callers must not treat this as official daily max."""

    temps = [obs.temperature_c for obs in observations if obs.temperature_c is not None]
    if not temps:
        return None
    return max(temps)


def _parse_forecast(station_id: str, row: Mapping[str, Any]) -> WeatherForecastRecord:
    issued = str(row["issued_at"])
    available = str(row.get("available_at") or issued)
    predicted = _optional_celsius(row.get("predicted_max_c"))
    return WeatherForecastRecord(
        station_id=station_id,
        issued_at=issued,
        available_at=available,
        model_run=str(row.get("model_run") or "unknown"),
        valid_date_local=str(row["valid_date_local"]),
        predicted_max_c=predicted,
        horizon_hours=D(row.get("horizon_hours") or "0"),
        url=str(row.get("url") or f"https://api.weather.gov/stations/{station_id}"),
        raw=dict(row),
    )


def _parse_observation(
    station_id: str,
    row: Mapping[str, Any],
    *,
    lag: timedelta,
) -> WeatherObservationRecord:
    observed_at = str(row["observed_at"])
    available = row.get("available_at")
    if available:
        available_at = str(available)
    else:
        available_at = isoformat_utc(parse_utc(observed_at) + lag)
    present = bool(row.get("max_min_fields_present"))
    daily_max = _optional_celsius(row.get("daily_max_c"))
    daily_min = _optional_celsius(row.get("daily_min_c"))
    if not present:
        # Belt and suspenders: incomplete fields cannot sneak through as 0.
        daily_max = None if row.get("daily_max_c") is None else daily_max
        if row.get("daily_max_c") is None:
            daily_max = None
        if row.get("daily_min_c") is None:
            daily_min = None
    return WeatherObservationRecord(
        station_id=station_id,
        observation_id=str(row.get("id") or observed_at),
        observed_at=observed_at,
        available_at=available_at,
        temperature_c=_optional_celsius(row.get("temperature_c")),
        daily_max_c=daily_max,
        daily_min_c=daily_min,
        max_min_fields_present=present,
        series_complete_for_local_date=bool(row.get("series_complete_for_local_date")),
        url=str(row.get("url") or f"https://api.weather.gov/stations/{station_id}/observations"),
        raw=dict(row),
    )


def _observations_from_nws(
    station_id: str,
    payload: Any,
    *,
    lag: timedelta,
    as_of,
) -> list[WeatherObservationRecord]:
    features = []
    if isinstance(payload, dict):
        features = list(payload.get("features") or [])
    out: list[WeatherObservationRecord] = []
    for feat in features:
        props = feat.get("properties") if isinstance(feat, dict) else None
        if not isinstance(props, dict):
            continue
        timestamp = props.get("timestamp")
        if not timestamp:
            continue
        temp = _nws_quantity_c(props.get("temperature"))
        daily_max = _nws_quantity_c(props.get("maxTemperatureLast24Hours"))
        daily_min = _nws_quantity_c(props.get("minTemperatureLast24Hours"))
        present = daily_max is not None or daily_min is not None
        # If the API omitted the field object entirely, treat max/min as missing.
        if "maxTemperatureLast24Hours" not in props and "minTemperatureLast24Hours" not in props:
            present = False
            daily_max = None
            daily_min = None
        elif daily_max is None and daily_min is None:
            present = False
        observed_at = str(timestamp)
        available_at = isoformat_utc(parse_utc(observed_at) + lag)
        if parse_utc(available_at) > as_of:
            continue
        out.append(
            WeatherObservationRecord(
                station_id=station_id,
                observation_id=str(props.get("@id") or timestamp),
                observed_at=observed_at,
                available_at=available_at,
                temperature_c=temp,
                daily_max_c=daily_max,
                daily_min_c=daily_min,
                max_min_fields_present=present,
                series_complete_for_local_date=False,
                url=str(props.get("@id") or f"https://api.weather.gov/stations/{station_id}/observations"),
                raw=dict(props),
            )
        )
    return out


def _nws_quantity_c(node: Any) -> Decimal | None:
    if node is None:
        return None
    if isinstance(node, dict):
        value = node.get("value")
        if value is None:
            return None
        unit = str(node.get("unitCode") or node.get("unit") or "").lower()
        amount = D(value)
        if "degf" in unit or unit.endswith("f"):
            amount = (amount - D(32)) * D(5) / D(9)
        return amount
    return D(node)


def _sigma_for(
    archive: Mapping[str, Any],
    *,
    station_id: str,
    horizon_hours: Decimal | None,
) -> tuple[Decimal | None, str]:
    table = (archive.get("error_sigma_c") or {}).get(station_id) or {}
    if horizon_hours is not None and table:
        hours = float(horizon_hours)
        items: list[tuple[float, float, Decimal, str]] = []
        for label, sigma in table.items():
            lo_s, hi_s = str(label).split("-", 1)
            items.append((float(lo_s), float(hi_s), D(sigma), str(label)))
        items.sort()
        for lo, hi, sigma, label in items:
            inclusive_top = hours == hi and (hi >= 72 or lo == items[-1][0])
            if lo <= hours < hi or inclusive_top:
                return sigma, f"station_horizon_table:{station_id}:{label}"
        lo, hi, sigma, label = items[-1]
        if hours >= lo:
            return sigma, f"station_horizon_table:{station_id}:{label}"
    prior = archive.get("unmeasured_prior_sigma_c")
    if prior is None:
        return None, "missing_sigma"
    return D(prior), "unmeasured_prior_v0"


def _climatology(
    archive: Mapping[str, Any],
    *,
    station_id: str,
    month_key: str | None,
    event_id: str | None,
) -> tuple[Decimal | None, int | None, str | None]:
    if not month_key or not event_id:
        return None, None, "climatology_unavailable"
    node = ((archive.get("climatology") or {}).get(station_id) or {}).get(month_key) or {}
    row = node.get(event_id)
    if not isinstance(row, dict):
        return None, None, "climatology_unavailable"
    p = row.get("p_yes")
    n = row.get("sample_n")
    note = row.get("note")
    return (
        None if p is None else D(p),
        None if n is None else int(n),
        None if note is None else str(note),
    )


def _assert_nws_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in NWS_HOSTS:
        raise WeatherSourceError(f"refusing host {host!r}; allowed={sorted(NWS_HOSTS)}")


def _nws_http_get(url: str, *, timeout: float) -> Any:
    _assert_nws_host(url)
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise WeatherSourceError("NWS adapter is HTTPS GET only")
    headers = {
        "User-Agent": "PolymarketResearchLab/0.1 (PAPER; research-lab; no live trading)",
        "Accept": "application/geo+json, application/json",
    }
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.get(url, headers=headers)
    if response.status_code != 200:
        raise WeatherSourceError(f"GET {url} -> {response.status_code}")
    return response.json()
