"""Parse station/date daily-max contracts from verified rules text.

Ambiguous cutoff, timezone, source, rounding, or quantity → the caller must
ABSTAIN. Hints never override rules_text.

NOAA city daily-high markets (highest temperature in {city}) name an ICAO via
``?site=`` on weather.gov and often omit half-up rounding. Those parse as
``rounding.mode=unspecified``; the specialist must still ABSTAIN until a human
rules-review records ``rounding_mode`` plus ``rounding_increment``. Do not invent
half-up from display-precision language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

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

# Known ICAO/METAR → local civil timezone for the observation date.
# Unknown stations with no explicit IANA timezone → ABSTAIN.
STATION_TIMEZONES: dict[str, str] = {
    "RJTT": "Asia/Tokyo",
    "RKSI": "Asia/Seoul",
    "KLAX": "America/Los_Angeles",
    "KMIA": "America/New_York",
}

_STATION_RE = re.compile(
    r"\bstation\s+(DEMO-\d+|[A-Z]{4})\b",
)
_ICAO_LABEL_RE = re.compile(
    r"\b(?:icao|metar)\s+([A-Z]{4})\b",
    re.IGNORECASE,
)
_STATIONS_PATH_RE = re.compile(
    r"/stations/(DEMO-\d+|[A-Za-z]{4})\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)>\"]+", re.IGNORECASE)
_LOCAL_DATE_RE = re.compile(
    r"local calendar date\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:T|\b)")
_TZ_RE = re.compile(r"\b((?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific)/[A-Za-z_]+)\b")
_DAILY_MAX_RE = re.compile(
    r"\b("
    r"daily\s+max(?:imum)?"
    r"|highest\s+temperature"
    r"|highest\s+reading"
    r"|high\s+temp"
    r")\b",
    re.IGNORECASE,
)
_AT_LEAST_RE = re.compile(
    r"at\s+least\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\b",
    re.IGNORECASE,
)
_BETWEEN_AND_RE = re.compile(
    r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\b",
    re.IGNORECASE,
)
_BETWEEN_RANGE_RE = re.compile(
    r"between\s+(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\b",
    re.IGNORECASE,
)
_OR_BELOW_RE = re.compile(
    r"\bbe\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s+or\s+below\b",
    re.IGNORECASE,
)
_OR_HIGHER_RE = re.compile(
    r"\bbe\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s+or\s+higher\b",
    re.IGNORECASE,
)
_EXACT_BE_RE = re.compile(
    r"\bbe\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\b",
    re.IGNORECASE,
)
_ROUNDING_RE = re.compile(
    r"half-up rounding to\s+(0\.\d+)\s*°?\s*C\b",
    re.IGNORECASE,
)
_WHOLE_DEGREES_RE = re.compile(
    r"whole degrees?(?:\s+(?:celsius|fahrenheit|[CF]))?|"
    r"measures temperatures to whole degrees|"
    r"(?:\"Temp\"|'Temp'|Temp)\s+column",
    re.IGNORECASE,
)
_END_DATE_LINE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})T[0-9:+\-Zz.]+\s*$",
)
_ABBREV_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
    r"\.?\s+'?(\d{2})\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)

_MONTH_NUM = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


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


def fahrenheit_to_celsius(value: Decimal) -> Decimal:
    """Exact linear conversion. Do not quantize — interval math needs the fraction."""

    return (D(value) - D(32)) * D(5) / D(9)


def bounds_to_celsius(
    lower: Decimal | None,
    upper: Decimal | None,
    unit: str,
) -> tuple[Decimal | None, Decimal | None]:
    unit_u = (unit or "C").upper()
    if unit_u == "C":
        return lower, upper
    if unit_u != "F":
        raise ValueError(f"unsupported temperature unit {unit!r}")
    return (
        None if lower is None else fahrenheit_to_celsius(lower),
        None if upper is None else fahrenheit_to_celsius(upper),
    )


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

    stations, station_err = _stations_from_rules(text, resolution_source)
    if station_err:
        return ContractParse(False, None, station_err, {"stations": stations})
    station_id = stations[0]

    if _DAILY_MAX_RE.search(text) is None:
        return ContractParse(False, None, "quantity_not_daily_max", {})

    local_date, date_err, date_details = _local_date_from_rules(text)
    if date_err or local_date is None:
        return ContractParse(False, None, date_err or "missing_local_date", date_details)

    timezone, tz_source, tz_err = _timezone_from_rules(text, station_id)
    if tz_err or timezone is None:
        return ContractParse(
            False,
            None,
            tz_err or "missing_timezone",
            {"station_id": station_id, "timezone_source": tz_source},
        )

    threshold = _threshold_from_rules(text)
    if threshold is None:
        return ContractParse(False, None, "missing_temperature_threshold", {})
    unit, rounded_lower, rounded_upper, event_id = threshold
    if rounded_lower is not None and rounded_upper is not None and rounded_upper <= rounded_lower:
        return ContractParse(False, None, "invalid_temperature_interval", {})

    rounding, round_err, round_details = _rounding_from_rules(text)
    if round_err or rounding is None:
        return ContractParse(False, None, round_err or "missing_rounding_rule", round_details)

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
                    "Weather Underground fallback text is ignored unless the "
                    "primary resolutionSource host is weather.gov. "
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
        unit=unit,
        rounded_lower=rounded_lower,
        rounded_upper=rounded_upper,
        rounding=rounding,
        resolution_source=source,
        rules_hash=rules_hash,
        event_id=event_id,
    )
    return ContractParse(
        True,
        contract,
        "ok",
        {
            "event_id": event_id,
            "unit": unit,
            "rounding_mode": rounding.mode,
            "timezone_source": tz_source,
            "note": (
                "rounding_unspecified_requires_rules_review"
                if rounding.mode == "unspecified"
                else "ok"
            ),
        },
    )


def _stations_from_rules(
    text: str, resolution_source: str | None
) -> tuple[list[str], str | None]:
    found: list[str] = []

    def add(raw: str | None) -> None:
        if not raw:
            return
        token = raw.strip().upper()
        if not token:
            return
        if token not in found:
            found.append(token)

    for match in _STATION_RE.finditer(text):
        add(match.group(1))
    for match in _ICAO_LABEL_RE.finditer(text):
        add(match.group(1))
    blobs = [text]
    if resolution_source:
        blobs.append(resolution_source)
    for blob in blobs:
        for url in _URL_RE.findall(blob):
            parsed = urlparse(url)
            for value in parse_qs(parsed.query).get("site") or []:
                if re.fullmatch(r"[A-Za-z]{4}", value or ""):
                    add(value)
            for match in _STATIONS_PATH_RE.finditer(parsed.path or ""):
                add(match.group(1))

    if not found:
        return [], "missing_station"
    if len(found) > 1:
        return found, "ambiguous_station"
    return found, None


def _body_and_end_year(text: str) -> tuple[str, int | None]:
    lines = text.splitlines()
    end_year: int | None = None
    body_lines = list(lines)
    for index in range(len(lines) - 1, -1, -1):
        raw = lines[index].strip()
        if not raw:
            continue
        match = _END_DATE_LINE_RE.match(raw)
        if match:
            end_year = int(match.group(1)[0:4])
            body_lines = lines[:index] + lines[index + 1 :]
        break
    return "\n".join(body_lines), end_year


def _local_date_from_rules(text: str) -> tuple[str | None, str | None, dict[str, Any]]:
    body, end_year = _body_and_end_year(text)
    explicit = _LOCAL_DATE_RE.search(body)
    if explicit is not None:
        return explicit.group(1), None, {"date_source": "local_calendar_date"}

    candidates: set[date] = set()
    years_hint: list[int] = []
    if end_year is not None:
        years_hint.append(end_year)

    for match in _ABBREV_DATE_RE.finditer(body):
        day = int(match.group(1))
        month_key = match.group(2).lower().rstrip(".")
        if month_key == "sept":
            month_key = "sep"
        month = _MONTH_NUM.get(month_key) or _MONTH_NUM[month_key[:3]]
        yy = int(match.group(3))
        year = _expand_year(yy, years_hint[0] if years_hint else None)
        parsed = _try_date(year, month, day)
        if parsed is None:
            return None, "invalid_local_date", {"raw": match.group(0)}
        candidates.add(parsed)
        years_hint.append(year)

    for match in _MONTH_DAY_YEAR_RE.finditer(body):
        month = _MONTH_NUM[match.group(1).lower()]
        day = int(match.group(2))
        if match.group(3):
            year = int(match.group(3))
            years_hint.append(year)
        elif years_hint:
            year = years_hint[0]
        elif candidates:
            year = next(iter(candidates)).year
        else:
            return None, "missing_local_date_year", {"raw": match.group(0)}
        parsed = _try_date(year, month, day)
        if parsed is None:
            return None, "invalid_local_date", {"raw": match.group(0)}
        candidates.add(parsed)

    # ISO dates in the body (question/description), never the trailing endDate line.
    stripped = _URL_RE.sub(" ", body)
    for match in _ISO_DATE_RE.finditer(stripped):
        year_s, month_s, day_s = match.group(1).split("-")
        parsed = _try_date(int(year_s), int(month_s), int(day_s))
        if parsed is None:
            return None, "invalid_local_date", {"raw": match.group(1)}
        candidates.add(parsed)
        years_hint.append(parsed.year)

    if not candidates:
        return None, "missing_local_date", {}
    if len(candidates) > 1:
        return (
            None,
            "ambiguous_local_date",
            {"dates": sorted(d.isoformat() for d in candidates)},
        )
    chosen = next(iter(candidates))
    return chosen.isoformat(), None, {"date_source": "question_or_description"}


def _expand_year(yy: int, context_year: int | None) -> int:
    if context_year is not None and context_year % 100 == yy:
        return context_year
    if context_year is not None:
        return (context_year // 100) * 100 + yy
    return 2000 + yy if yy < 70 else 1900 + yy


def _try_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _timezone_from_rules(
    text: str, station_id: str
) -> tuple[str | None, str | None, str | None]:
    explicit = [match.group(1) for match in _TZ_RE.finditer(text)]
    unique_explicit = list(dict.fromkeys(explicit))
    mapped = STATION_TIMEZONES.get(station_id.upper())
    if len(unique_explicit) > 1:
        return None, "explicit", "ambiguous_timezone"
    if unique_explicit:
        tz = unique_explicit[0]
        if mapped and mapped != tz:
            return None, "contradictory", "ambiguous_timezone"
        return tz, "explicit", None
    if mapped:
        return mapped, "station_metadata", None
    return None, None, "missing_timezone"


def _question_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _threshold_from_rules(
    text: str,
) -> tuple[str, Decimal | None, Decimal | None, str] | None:
    question = _question_line(text)
    parsed = _threshold_in(question)
    if parsed is not None:
        return parsed
    return _threshold_in(text)


def _threshold_in(
    blob: str,
) -> tuple[str, Decimal | None, Decimal | None, str] | None:
    between_range = _BETWEEN_RANGE_RE.search(blob)
    if between_range is not None:
        lo = D(between_range.group(1))
        hi = D(between_range.group(2))
        unit = between_range.group(3).upper()
        # Inclusive integer pair 66-67°F → [66, 68) on the reported scale.
        return unit, lo, hi + D(1), _event_id("between", lo, hi, unit)
    between_and = _BETWEEN_AND_RE.search(blob)
    if between_and is not None:
        lo = D(between_and.group(1))
        hi = D(between_and.group(2))
        unit = between_and.group(3).upper()
        return unit, lo, hi, _event_id("between", lo, hi, unit)
    or_below = _OR_BELOW_RE.search(blob)
    if or_below is not None:
        value = D(or_below.group(1))
        unit = or_below.group(2).upper()
        return unit, None, value + D(1), _event_id("le", value, None, unit)
    or_higher = _OR_HIGHER_RE.search(blob)
    if or_higher is not None:
        value = D(or_higher.group(1))
        unit = or_higher.group(2).upper()
        return unit, value, None, _event_id("ge", value, None, unit)
    at_least = _AT_LEAST_RE.search(blob)
    if at_least is not None:
        value = D(at_least.group(1))
        unit = at_least.group(2).upper()
        return unit, value, None, _event_id("ge", value, None, unit)
    exact = _EXACT_BE_RE.search(blob)
    if exact is not None:
        value = D(exact.group(1))
        unit = exact.group(2).upper()
        return unit, value, value + D(1), _event_id("eq", value, None, unit)
    return None


def _event_id(kind: str, lo: Decimal, hi: Decimal | None, unit: str) -> str:
    suffix = unit.lower()
    if kind == "between" and hi is not None:
        return f"between_{lo}_{hi}{suffix}"
    if kind == "eq":
        return f"eq_{lo}{suffix}"
    if kind == "le":
        return f"le_{lo}{suffix}"
    return f"ge_{lo}{suffix}"


def _rounding_from_rules(
    text: str,
) -> tuple[RoundingRule | None, str | None, dict[str, Any]]:
    round_m = _ROUNDING_RE.search(text)
    if round_m is not None:
        return (
            RoundingRule(increment=D(round_m.group(1)), mode="half_up"),
            None,
            {"rounding_source": "half_up_text"},
        )
    if _WHOLE_DEGREES_RE.search(text) is not None:
        # Display precision only. NOAA city markets omit the rounding algorithm
        # (half-up vs truncation). Keep that honesty in the contract.
        return (
            RoundingRule(increment=D(1), mode="unspecified"),
            None,
            {
                "rounding_source": "noaa_temp_column_integer_display",
                "note": (
                    "Whole-degree Temp-column language is display precision, "
                    "not a rounding algorithm. Specialist must ABSTAIN until "
                    "human rules-review records rounding_mode plus rounding_increment."
                ),
            },
        )
    return None, "missing_rounding_rule", {}
