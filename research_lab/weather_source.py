"""NWS / contract-matching weather archive.

Default is recorded fixtures so CI never needs the network. Optional live GET
is behind NWS_ALLOW_NETWORK=1 and WEATHER_DATA_SOURCE=network.

Caveats encoded here (NWS services docs):
- Observation lag is treated as ~20 minutes unless the archive says otherwise.
- Missing max/min fields stay None — never coerced to 0.
- An incomplete observation series is never turned into an official daily max.
- Live NetworkNWS predicted_max comes from the gridpoint forecast (12-hour
  daytime high, hourly fallback). That is a forecast proxy, not the official
  daily maximum.
- Live error scale is NWS_DEFAULT_SIGMA_C (default 1.5 C): an uncalibrated
  research assumption, not fitted station skill.
- Stations outside the NWS domain (HTTP 404, e.g. RJTT) are missing_station.
- The live API returns the current document only. If generatedAt/updateTime
  is after research as_of, there is no usable vintage (no look-ahead).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from research_lab.money import D
from research_lab.research_budget import ResearchBudget
from research_lab.timeutil import isoformat_utc, parse_utc

HttpGet = Callable[..., Any]
DEFAULT_NETWORK_SIGMA_C = D("1.5")

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


def nws_default_sigma_c() -> tuple[Decimal | None, str]:
    """Conservative live-path scale. Uncalibrated; not a fitted skill estimate."""

    raw = os.environ.get("NWS_DEFAULT_SIGMA_C", str(DEFAULT_NETWORK_SIGMA_C)).strip()
    if not raw:
        return None, "missing_sigma"
    try:
        sigma = D(raw)
    except Exception:
        return None, "invalid_nws_default_sigma_c"
    if sigma <= 0:
        return None, "invalid_nws_default_sigma_c"
    return sigma, f"uncalibrated_assumption:NWS_DEFAULT_SIGMA_C={sigma}"


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
    """GET-only NWS client. Untrusted JSON is data, not instructions.

    PAPER research only. Does not invent an official daily max from incomplete
    observations. Predicted max is the gridpoint forecast high for ``local_date``.
    """

    source_name = "network"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 20.0,
        *,
        budget: ResearchBudget | None = None,
        observation_lag: timedelta | None = None,
        http_get: HttpGet | None = None,
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
        self._http_get = http_get or _nws_http_get

    def snapshot(
        self,
        *,
        station_id: str,
        local_date: str,
        as_of: str,
        event_id: str | None = None,
        month_key: str | None = None,
    ) -> WeatherSnapshot:
        del event_id, month_key
        as_of_dt = parse_utc(as_of)
        station_url = urljoin(self.base_url + "/", f"stations/{station_id}")
        obs_url = urljoin(self.base_url + "/", f"stations/{station_id}/observations")
        station_payload = self._get_station(station_url, station_id=station_id)
        obs_payload = self._get_optional(obs_url)
        if not isinstance(station_payload, dict):
            raise WeatherSourceError("unexpected NWS station payload")
        observations = _observations_from_nws(
            station_id, obs_payload, lag=self.observation_lag, as_of=as_of_dt
        )
        official = official_daily_max(observations, local_date=local_date)
        forecast, extra_urls = self._forecast_record(
            station_id=station_id,
            local_date=local_date,
            as_of_dt=as_of_dt,
            station_payload=station_payload,
        )
        sigma, sigma_source = nws_default_sigma_c()
        urls = [station_url, obs_url, *extra_urls]
        if forecast is not None:
            urls.append(forecast.url)
        # Preserve unique order.
        seen: set[str] = set()
        archive: list[str] = []
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                archive.append(url)
        return WeatherSnapshot(
            station_id=station_id,
            as_of=isoformat_utc(as_of_dt),
            forecast=forecast,
            observations=tuple(observations),
            official_daily_max=official,
            sigma_c=sigma,
            sigma_source=sigma_source,
            climatology_p=None,
            climatology_n=None,
            climatology_note=(
                "live NWS path has no fixture climatology table; "
                "sigma_c is an uncalibrated NWS_DEFAULT_SIGMA_C assumption"
            ),
            provider=self.source_name,
            archive_urls=tuple(archive),
        )

    def _get_station(self, url: str, *, station_id: str) -> dict[str, Any]:
        try:
            payload = self._get(url)
        except WeatherSourceError as exc:
            if _is_http_404(exc):
                raise WeatherSourceError(
                    f"missing_station:{station_id}:out_of_nws_domain"
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise WeatherSourceError("unexpected NWS station payload")
        return payload

    def _forecast_record(
        self,
        *,
        station_id: str,
        local_date: str,
        as_of_dt: datetime,
        station_payload: Mapping[str, Any],
    ) -> tuple[WeatherForecastRecord | None, tuple[str, ...]]:
        props = station_payload.get("properties")
        if not isinstance(props, dict):
            return None, ()
        timezone = str(props.get("timeZone") or "").strip()
        if not timezone:
            return None, ()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return None, ()

        forecast_url, hourly_url, hop_urls = self._resolve_forecast_urls(
            station_payload
        )
        twelveh = self._get_optional(forecast_url) if forecast_url else None
        record = _forecast_from_gridpoint(
            station_id=station_id,
            local_date=local_date,
            timezone=timezone,
            as_of_dt=as_of_dt,
            payload=twelveh,
            url=forecast_url or "",
            prefer_daytime=True,
        )
        if record is not None and record.predicted_max_c is not None:
            return record, hop_urls
        hourly = self._get_optional(hourly_url) if hourly_url else None
        hourly_record = _forecast_from_gridpoint(
            station_id=station_id,
            local_date=local_date,
            timezone=timezone,
            as_of_dt=as_of_dt,
            payload=hourly,
            url=hourly_url or "",
            prefer_daytime=False,
        )
        extra = hop_urls
        if hourly_url:
            extra = extra + (hourly_url,)
        if hourly_record is not None and hourly_record.predicted_max_c is not None:
            return hourly_record, extra
        if record is not None:
            return record, extra
        return None, extra

    def _resolve_forecast_urls(
        self, station_payload: Mapping[str, Any]
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        props = station_payload.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        forecast_url = _absolute_nws_url(
            self.base_url, props.get("forecast")
        )
        hourly_url = _absolute_nws_url(
            self.base_url, props.get("forecastHourly")
        )
        twelveh = forecast_url if _gridpoint_forecast_kind(forecast_url) == "twelveh" else None
        hourly = hourly_url if _gridpoint_forecast_kind(hourly_url) == "hourly" else None
        if twelveh or hourly:
            return twelveh or forecast_url, hourly, ()

        points_url = _points_url_from_station(self.base_url, station_payload)
        if points_url is None:
            return None, None, ()
        points_payload = self._get_optional(points_url)
        hop = (points_url,)
        if not isinstance(points_payload, dict):
            return None, None, hop
        point_props = points_payload.get("properties") or {}
        if not isinstance(point_props, dict):
            return None, None, hop
        twelveh = _absolute_nws_url(self.base_url, point_props.get("forecast"))
        hourly = _absolute_nws_url(self.base_url, point_props.get("forecastHourly"))
        return twelveh, hourly, hop

    def _get(self, url: str) -> Any:
        _assert_nws_host(url)
        if self.budget is not None:
            self.budget.charge(self.budget.run_unit_cost_usd, reason=f"nws_get:{url}")
        return self._http_get(url, timeout=self.timeout)

    def _get_optional(self, url: str | None) -> Any | None:
        if not url:
            return None
        try:
            return self._get(url)
        except WeatherSourceError as exc:
            if _is_http_404(exc):
                return None
            raise


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


def _is_http_404(exc: BaseException) -> bool:
    return "-> 404" in str(exc)


def _nws_coord(value: Any) -> str:
    text = format(D(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _absolute_nws_url(base_url: str, value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("/"):
        text = urljoin(base_url + "/", text.lstrip("/"))
    _assert_nws_host(text)
    return text


def _gridpoint_forecast_kind(url: str | None) -> str | None:
    if not url:
        return None
    path = (urlparse(url).path or "").rstrip("/")
    if "/gridpoints/" not in path:
        return None
    if path.endswith("/forecast/hourly"):
        return "hourly"
    if path.endswith("/forecast"):
        return "twelveh"
    return None


def _points_url_from_station(base_url: str, station_payload: Mapping[str, Any]) -> str | None:
    geometry = station_payload.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    lon, lat = coords[0], coords[1]
    url = urljoin(base_url + "/", f"points/{_nws_coord(lat)},{_nws_coord(lon)}")
    _assert_nws_host(url)
    return url


def _period_start_dt(period: Mapping[str, Any]) -> datetime | None:
    raw = period.get("startTime")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _period_local_date(period: Mapping[str, Any], timezone: str) -> str | None:
    start = _period_start_dt(period)
    if start is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return None
    return start.astimezone(zone).date().isoformat()


def _nws_period_temperature_c(period: Mapping[str, Any]) -> Decimal | None:
    temp = period.get("temperature")
    if isinstance(temp, dict):
        return _nws_quantity_c(temp)
    if temp is None:
        return None
    amount = D(temp)
    unit = str(period.get("temperatureUnit") or period.get("unit") or "").strip().lower()
    if unit in {"f", "fah", "fahrenheit"} or "degf" in unit:
        return (amount - D(32)) * D(5) / D(9)
    if unit in {"c", "celsius"} or "degc" in unit:
        return amount
    return None


def _predicted_max_c_for_local_date(
    periods: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    local_date: str,
    timezone: str,
    prefer_daytime: bool,
    as_of_dt: datetime,
    filter_start_to_as_of: bool,
) -> Decimal | None:
    daytime: list[Decimal] = []
    all_temps: list[Decimal] = []
    for period in periods:
        if not isinstance(period, Mapping):
            continue
        if _period_local_date(period, timezone) != local_date:
            continue
        start = _period_start_dt(period)
        if start is None:
            continue
        if filter_start_to_as_of and start.astimezone(as_of_dt.tzinfo) > as_of_dt:
            continue
        temp = _nws_period_temperature_c(period)
        if temp is None:
            continue
        all_temps.append(temp)
        if period.get("isDaytime") is True:
            daytime.append(temp)
    if prefer_daytime and daytime:
        return max(daytime)
    if all_temps and not prefer_daytime:
        return max(all_temps)
    return None


def _try_parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc(str(value))
    except ValueError:
        return None


def _horizon_hours(issued_at: str, local_date: str, timezone: str) -> Decimal:
    try:
        zone = ZoneInfo(timezone)
        start_local = datetime.fromisoformat(f"{local_date}T00:00:00").replace(tzinfo=zone)
    except (ZoneInfoNotFoundError, ValueError):
        return D(0)
    issued = parse_utc(issued_at)
    seconds = int((start_local.astimezone(issued.tzinfo) - issued).total_seconds())
    if seconds <= 0:
        return D(0)
    return D(seconds) / D(3600)


def _gridpoint_periods(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, dict):
        return []
    props = payload.get("properties")
    if not isinstance(props, dict):
        return []
    periods = props.get("periods") or []
    return [row for row in periods if isinstance(row, Mapping)]


def _forecast_from_gridpoint(
    *,
    station_id: str,
    local_date: str,
    timezone: str,
    as_of_dt: datetime,
    payload: Any,
    url: str,
    prefer_daytime: bool,
) -> WeatherForecastRecord | None:
    if not isinstance(payload, dict):
        return None
    props = payload.get("properties")
    if not isinstance(props, dict):
        return None
    generated = _try_parse_utc(props.get("generatedAt"))
    updated = _try_parse_utc(props.get("updateTime"))
    filter_start = False
    if generated is not None or updated is not None:
        issued_dt = updated or generated
        available_dt = generated or updated
        assert issued_dt is not None and available_dt is not None
        if available_dt > as_of_dt:
            # Live document was produced after as_of — no look-ahead.
            return None
        issued_at = isoformat_utc(issued_dt)
        available_at = isoformat_utc(available_dt)
        usable = _gridpoint_periods(payload)
    else:
        # Degraded path: no generatedAt/updateTime. Only periods whose
        # startTime <= as_of may contribute.
        filter_start = True
        usable = []
        for period in _gridpoint_periods(payload):
            start = _period_start_dt(period)
            if start is None:
                continue
            if start.astimezone(as_of_dt.tzinfo) <= as_of_dt:
                usable.append(period)
        if not usable:
            return None
        latest_start = max(
            start
            for period in usable
            if (start := _period_start_dt(period)) is not None
        )
        issued_at = isoformat_utc(latest_start)
        available_at = isoformat_utc(min(latest_start, as_of_dt))

    predicted = _predicted_max_c_for_local_date(
        usable,
        local_date=local_date,
        timezone=timezone,
        prefer_daytime=prefer_daytime,
        as_of_dt=as_of_dt,
        filter_start_to_as_of=filter_start,
    )
    generator = str(props.get("forecastGenerator") or "nws-gridpoint")
    return WeatherForecastRecord(
        station_id=station_id,
        issued_at=issued_at,
        available_at=available_at,
        model_run=f"{generator}:{issued_at}",
        valid_date_local=local_date,
        predicted_max_c=predicted,
        horizon_hours=_horizon_hours(issued_at, local_date, timezone),
        url=url or f"https://api.weather.gov/stations/{station_id}",
        raw={"properties": dict(props)},
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
