"""Weather-like market discovery on GET-only Gamma payloads.

Fixture mode is the CI default. Live public GET uses the existing adapters and
still cannot place orders. Classification is a measurement filter, not a claim
that a market is tradeable or that the specialist can price it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from research_lab.hashing import rules_hash_from_text, sha256_hex
from research_lab.timeutil import as_utc
from research_lab.weather_contract import (
    STATION_TIMEZONES,
    _STATION_RE,
    _local_date_from_rules,
    parse_weather_contract,
)

# Broad recall for Strategy A collection. Specialist still ABSTAINs unless the
# verified rules parse as station + local-date daily max with a matching source.
_WEATHER_HINT_RE = re.compile(
    r"(weather|temperature|daily\s+max(?:imum)?|nws|accuweather|"
    r"weather\.gov|celsius|fahrenheit|°\s*[cf]\b|precipitation|rainfall|"
    r"hottest|coldest|high\s+temp|timeseries\?site=)",
    re.IGNORECASE,
)
_TEMP_C_RE = re.compile(r"\b-?\d+(?:\.\d+)?\s*°?\s*[CF]\b", re.IGNORECASE)
_SITE_RE = re.compile(r"[?&]site=([A-Za-z]{4})\b", re.IGNORECASE)


@dataclass(frozen=True)
class WeatherMarketClass:
    market_id: str
    weather_like: bool
    reason: str
    specialist_parse_ok: bool
    specialist_parse_reason: str
    question: str | None
    rules_hash: str
    rules_text: str
    resolution_source: str | None
    raw: dict[str, Any]


def discover_limit_from_env(default: int = 50) -> int:
    raw = os.environ.get("POLYMARKET_DISCOVER_LIMIT", str(default)).strip()
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("POLYMARKET_DISCOVER_LIMIT must be an integer") from exc
    return max(1, min(limit, 200))


def rules_text_from_raw(raw: Mapping[str, Any]) -> str:
    parts = [
        str(raw.get("question") or ""),
        str(raw.get("description") or ""),
        str(raw.get("resolutionSource") or raw.get("resolution_source") or ""),
        str(raw.get("endDate") or raw.get("end_date") or ""),
    ]
    return "\n".join(parts)


def classify_weather_market(raw: Mapping[str, Any]) -> WeatherMarketClass:
    """Tag a Gamma-shaped market as weather-like for Kapu B collection."""

    market_id = str(raw.get("id") or raw.get("market_id") or "")
    rules_text = rules_text_from_raw(raw)
    rules_hash = rules_hash_from_text(rules_text)
    resolution = raw.get("resolutionSource") or raw.get("resolution_source")
    resolution_s = None if resolution is None else str(resolution)
    blob = " ".join(
        [
            str(raw.get("question") or ""),
            str(raw.get("description") or ""),
            str(raw.get("slug") or ""),
            resolution_s or "",
        ]
    )
    parsed = parse_weather_contract(
        rules_text=rules_text,
        rules_hash=rules_hash,
        resolution_source=resolution_s,
    )
    station_hit = (
        _STATION_RE.search(rules_text) is not None or _SITE_RE.search(blob) is not None
    )
    hint_hit = _WEATHER_HINT_RE.search(blob) is not None
    temp_hit = _TEMP_C_RE.search(blob) is not None
    weather_like = bool(parsed.ok or station_hit or hint_hit or temp_hit)
    if parsed.ok:
        reason = "parsed_station_date_contract"
    elif station_hit:
        reason = f"station_mentioned:{parsed.reason}"
    elif hint_hit:
        reason = "weather_keyword"
    elif temp_hit:
        reason = "temperature_token"
    else:
        reason = "not_weather_like"
    return WeatherMarketClass(
        market_id=market_id,
        weather_like=weather_like,
        reason=reason,
        specialist_parse_ok=bool(parsed.ok),
        specialist_parse_reason=parsed.reason,
        question=None if raw.get("question") is None else str(raw.get("question")),
        rules_hash=rules_hash,
        rules_text=rules_text,
        resolution_source=resolution_s,
        raw=dict(raw),
    )


def classify_weather_markets(rows: list[Mapping[str, Any]]) -> list[WeatherMarketClass]:
    return [classify_weather_market(row) for row in rows]


def payload_hash(payload: Any) -> str:
    return sha256_hex(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")))


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1", "yes"}:
            return True
        if low in {"false", "0", "no"}:
            return False
    return None


def tradable_skip_reason(raw: Mapping[str, Any]) -> str | None:
    """Primary reason a Gamma market is not a tradable book, or None if it is.

    Required: ``active=true``, ``closed=false``, ``acceptingOrders=true``.
    ``enableOrderBook`` is required only when the field is present.
    """

    closed = _coerce_bool(raw.get("closed"))
    active = _coerce_bool(raw.get("active"))
    if "acceptingOrders" in raw:
        accepting = _coerce_bool(raw.get("acceptingOrders"))
    elif "accepting_orders" in raw:
        accepting = _coerce_bool(raw.get("accepting_orders"))
    else:
        accepting = None
    if "enableOrderBook" in raw:
        orderbook = _coerce_bool(raw.get("enableOrderBook"))
    elif "enable_order_book" in raw:
        orderbook = _coerce_bool(raw.get("enable_order_book"))
    else:
        orderbook = None
    if closed is True:
        return "closed"
    if active is not True:
        return "inactive"
    if accepting is not True:
        return "not_accepting_orders"
    if orderbook is False:
        return "orderbook_disabled"
    return None


def adapter_skip_reason(exc: BaseException) -> str:
    """Map a per-market adapter failure onto a discover skip reason."""

    text = str(exc).lower()
    if (
        "no orderbook exists" in text
        or "-> 404" in text
        or " 404" in text
        or text.rstrip().endswith("404")
    ):
        return "no_orderbook"
    if "missing orderbook" in text or "missing_token" in text:
        return "missing_token_ids"
    return "adapter_error"


def market_blob(raw: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(raw.get("question") or ""),
            str(raw.get("description") or ""),
            str(raw.get("slug") or ""),
            str(raw.get("resolutionSource") or raw.get("resolution_source") or ""),
        ]
    )


def event_local_date(raw: Mapping[str, Any]) -> date | None:
    """Best-effort local event date from rules text. Unknown dates stay None."""

    iso, err, _details = _local_date_from_rules(rules_text_from_raw(raw))
    if err or not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def station_timezone_name(raw: Mapping[str, Any]) -> str | None:
    blob = market_blob(raw)
    for pattern in (_SITE_RE, _STATION_RE):
        match = pattern.search(blob)
        if match:
            tz = STATION_TIMEZONES.get(match.group(1).upper())
            if tz:
                return tz
    upper = blob.upper()
    for code, tz in STATION_TIMEZONES.items():
        if re.search(rf"\b{code}\b", upper):
            return tz
    return None


def event_date_floor_for(
    raw: Mapping[str, Any],
    *,
    now: datetime | None,
    min_event_date: date | None,
    use_station_today: bool,
) -> date | None:
    if min_event_date is not None:
        return min_event_date
    if not use_station_today or now is None:
        return None
    tz_name = station_timezone_name(raw)
    if tz_name:
        return as_utc(now).astimezone(ZoneInfo(tz_name)).date()
    return as_utc(now).date()


# Substring / station needles for a few US daily-high cities. Unknown names
# fall back to a case-insensitive substring of the market text.
_CITY_NEEDLES: dict[str, tuple[str, ...]] = {
    "nyc": ("new york", "nyc", "laguardia", "klga", "kjfk", "knyc"),
    "new york": ("new york", "nyc", "laguardia", "klga", "kjfk", "knyc"),
    "miami": ("miami", "kmia"),
    "atlanta": ("atlanta", "katl"),
    "chicago": ("chicago", "kord", "midway", "kmdw"),
    "seattle": ("seattle", "ksea"),
    "los angeles": ("los angeles", "klax"),
    "la": ("los angeles", "klax"),
    "paris": ("paris", "lfpg", "lfpo", "lfpb"),
    "tokyo": ("tokyo", "rjtt", "haneda"),
    "seoul": ("seoul", "incheon", "rksi"),
    "dallas": ("dallas", "kdal", "kdfw"),
    "denver": ("denver", "kden"),
    "phoenix": ("phoenix", "kphx"),
    "san francisco": ("san francisco", "ksfo"),
}


def market_matches_city(raw: Mapping[str, Any], city: str | None) -> bool:
    if city is None or not str(city).strip():
        return True
    blob = market_blob(raw).lower()
    key = str(city).strip().lower()
    needles = _CITY_NEEDLES.get(key, (key,))
    return any(needle in blob for needle in needles)


def market_matches_station(raw: Mapping[str, Any], station: str | None) -> bool:
    if station is None or not str(station).strip():
        return True
    code = str(station).strip()
    return re.search(rf"\b{re.escape(code)}\b", market_blob(raw), re.IGNORECASE) is not None


def discovery_skip_reason(
    raw: Mapping[str, Any],
    *,
    now: datetime | None = None,
    min_event_date: date | None = None,
    use_station_today: bool = False,
    city: str | None = None,
    station: str | None = None,
) -> str | None:
    """Why this row is excluded from ingest, or None if it may be classified."""

    tradable = tradable_skip_reason(raw)
    if tradable:
        return tradable
    floor = event_date_floor_for(
        raw,
        now=now,
        min_event_date=min_event_date,
        use_station_today=use_station_today,
    )
    if floor is not None:
        event_date = event_local_date(raw)
        if event_date is not None and event_date < floor:
            return "event_date_before_min"
    if not market_matches_city(raw, city):
        return "city_mismatch"
    if not market_matches_station(raw, station):
        return "station_mismatch"
    return None
