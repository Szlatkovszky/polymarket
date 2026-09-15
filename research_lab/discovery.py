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
from typing import Any, Mapping

from research_lab.hashing import rules_hash_from_text, sha256_hex
from research_lab.weather_contract import _STATION_RE, parse_weather_contract

# Broad recall for Strategy A collection. Specialist still ABSTAINs unless the
# verified rules parse as station + local-date daily max with a matching source.
_WEATHER_HINT_RE = re.compile(
    r"(weather|temperature|daily\s+max(?:imum)?|nws|accuweather|"
    r"weather\.gov|celsius|fahrenheit|°\s*[cf]\b|precipitation|rainfall|"
    r"hottest|coldest|high\s+temp)",
    re.IGNORECASE,
)
_TEMP_C_RE = re.compile(r"\b-?\d+(?:\.\d+)?\s*°?\s*C\b", re.IGNORECASE)


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
    station_hit = _STATION_RE.search(rules_text) is not None
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
