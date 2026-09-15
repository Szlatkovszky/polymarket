"""Parse station/date daily-max contracts from verified rules text.

Ambiguous cutoff, timezone, source, rounding, or quantity → the caller must
ABSTAIN. Hints never override rules_text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlparse

from research_lab.money import D
from research_lab.weather_math import RoundingRule

NWS_HOSTS = frozenset(
    {
        "api.weather.gov",
        "weather.gov",
        "www.weather.gov",
        "forecast.weather.gov",
    }
)

_STATION_RE = re.compile(
    r"\bstation\s+([A-Z]{4}|DEMO-\d+)\b",
    re.IGNORECASE,
)
_LOCAL_DATE_RE = re.compile(
    r"local calendar date\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_TZ_RE = re.compile(r"\b([A-Za-z]+/[A-Za-z_]+)\b")
_DAILY_MAX_RE = re.compile(r"\bdaily max(?:imum)?\b", re.IGNORECASE)
_AT_LEAST_RE = re.compile(
    r"at least\s+(-?\d+(?:\.\d+)?)\s*C\b",
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)\s*C\b",
    re.IGNORECASE,
)
_ROUNDING_RE = re.compile(
    r"half-up rounding to\s+(0\.\d+)\s*C\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WeatherContract:
    station_id: str
    local_date: str
    timezone: str
    quantity: str
    unit: str
    rounded_lower: Decimal | None
    rounded_upper: Decimal | None
    rounding: RoundingRule
    resolution_source: str
    rules_hash: str
    event_id: str

    def month_key(self) -> str:
        return self.local_date[5:7]


@dataclass(frozen=True)
class ContractParse:
    ok: bool
    contract: WeatherContract | None
    reason: str
    details: dict[str, Any]


def resolution_source_is_nws(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host in NWS_HOSTS


def parse_weather_contract(
    *,
    rules_text: str,
    rules_hash: str,
    resolution_source: str | None,
    hints: Mapping[str, Any] | None = None,
) -> ContractParse:
    """Extract station + local date + daily-max interval from rules text.

    ``hints`` may only corroborate values already present in ``rules_text``.
    Contradiction or missing required fields → not ok (ABSTAIN upstream).
    """

    text = rules_text or ""
    hints = dict(hints or {})
    if not text.strip():
        return ContractParse(False, None, "missing_rules_text", {})
    if not rules_hash:
        return ContractParse(False, None, "missing_rules_hash", {})

    station_m = _STATION_RE.search(text)
    if station_m is None:
        return ContractParse(False, None, "missing_station", {})
    station_id = station_m.group(1).upper()

    date_m = _LOCAL_DATE_RE.search(text)
    if date_m is None:
        return ContractParse(False, None, "missing_local_date", {})
    local_date = date_m.group(1)

    tz_m = _TZ_RE.search(text)
    if tz_m is None:
        return ContractParse(False, None, "missing_timezone", {})
    timezone = tz_m.group(1)

    if _DAILY_MAX_RE.search(text) is None:
        return ContractParse(False, None, "quantity_not_daily_max", {})

    between = _BETWEEN_RE.search(text)
    at_least = _AT_LEAST_RE.search(text)
    rounded_lower: Decimal | None
    rounded_upper: Decimal | None
    event_id: str
    if between is not None:
        rounded_lower = D(between.group(1))
        rounded_upper = D(between.group(2))
        if rounded_upper <= rounded_lower:
            return ContractParse(False, None, "invalid_temperature_interval", {})
        event_id = f"between_{rounded_lower}_{rounded_upper}c"
    elif at_least is not None:
        rounded_lower = D(at_least.group(1))
        rounded_upper = None
        event_id = f"ge_{rounded_lower}c"
    else:
        return ContractParse(False, None, "missing_temperature_threshold", {})

    round_m = _ROUNDING_RE.search(text)
    if round_m is None:
        return ContractParse(False, None, "missing_rounding_rule", {})
    rounding = RoundingRule(increment=D(round_m.group(1)), mode="half_up")

    source = (resolution_source or "").strip()
    if not source:
        return ContractParse(False, None, "missing_resolution_source", {})
    if not resolution_source_is_nws(source):
        return ContractParse(
            False,
            None,
            "resolution_source_mismatch",
            {
                "resolution_source": source,
                "note": (
                    "Contract names a non-NWS resolution provider. "
                    "Do not substitute NWS/fixture NWS archive."
                ),
            },
        )

    hint_station = str(hints["station_id"]).upper() if hints.get("station_id") else None
    if hint_station and hint_station != station_id:
        return ContractParse(
            False,
            None,
            "rules_hint_mismatch",
            {"rules_station": station_id, "hint_station": hint_station},
        )
    hint_date = str(hints["local_date"]) if hints.get("local_date") else None
    if hint_date and hint_date != local_date:
        return ContractParse(
            False,
            None,
            "rules_hint_mismatch",
            {"rules_local_date": local_date, "hint_local_date": hint_date},
        )
    hint_hash = str(hints["rules_hash"]) if hints.get("rules_hash") else None
    if hint_hash and hint_hash != rules_hash:
        return ContractParse(
            False,
            None,
            "rules_hash_mismatch",
            {"logged": rules_hash, "hint": hint_hash},
        )

    contract = WeatherContract(
        station_id=station_id,
        local_date=local_date,
        timezone=timezone,
        quantity="daily_max",
        unit="C",
        rounded_lower=rounded_lower,
        rounded_upper=rounded_upper,
        rounding=rounding,
        resolution_source=source,
        rules_hash=rules_hash,
        event_id=event_id,
    )
    return ContractParse(True, contract, "ok", {"event_id": event_id})
