"""Offline parser tests for NOAA city daily-high wording. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_lab.adapters import (
    FixtureGamma,
    NetworkGamma,
    WEATHER_SEARCH_QUERIES,
    markets_from_search_payload,
)
from research_lab.discovery import classify_weather_market, classify_weather_markets
from research_lab.hashing import rules_hash_from_text
from research_lab.lab import _rules_text
from research_lab.money import D
from research_lab.specialist import WeatherStationBaseline
from research_lab.weather_contract import (
    bounds_to_celsius,
    fahrenheit_to_celsius,
    parse_weather_contract,
    resolution_source_is_nws,
)
from test_lab import _lab
from test_weather_specialist import _kmia_rules, _request_for_market


def _fixture_parse(market_id: str):
    raw = FixtureGamma().get_market(market_id)
    text = _rules_text(raw)
    source = str(raw["resolutionSource"])
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash=rules_hash_from_text(text),
        resolution_source=source,
    )
    return raw, text, source, parsed


def test_parse_tokyo_24c_fixture_from_live_wording() -> None:
    raw, _text, source, parsed = _fixture_parse("900006")
    assert raw["question"] == (
        "Will the highest temperature in Tokyo be 24°C on September 16?"
    )
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "RJTT"
    assert parsed.contract.local_date == "2026-09-16"
    assert parsed.contract.timezone == "Asia/Tokyo"
    assert parsed.contract.quantity == "daily_max"
    assert parsed.contract.unit == "C"
    assert parsed.contract.rounded_lower == D(24)
    assert parsed.contract.rounded_upper == D(25)
    assert parsed.contract.event_id == "eq_24c"
    assert parsed.contract.rounding.mode == "unspecified"
    assert parsed.contract.rounding.increment == D(1)
    assert parsed.details["timezone_source"] == "station_metadata"
    assert resolution_source_is_nws(source)
    assert "wunderground" not in source.lower()


def test_parse_seoul_or_below_fixture() -> None:
    _raw, _text, source, parsed = _fixture_parse("900007")
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "RKSI"
    assert parsed.contract.timezone == "Asia/Seoul"
    assert parsed.contract.local_date == "2026-09-16"
    assert parsed.contract.rounded_lower is None
    assert parsed.contract.rounded_upper == D(23)
    assert parsed.contract.event_id == "le_22c"
    assert parsed.contract.rounding.mode == "unspecified"
    assert resolution_source_is_nws(source)


def test_parse_lax_between_fahrenheit_fixture() -> None:
    _raw, _text, _source, parsed = _fixture_parse("900008")
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "KLAX"
    assert parsed.contract.timezone == "America/Los_Angeles"
    assert parsed.contract.unit == "F"
    assert parsed.contract.rounded_lower == D(66)
    assert parsed.contract.rounded_upper == D(68)
    assert parsed.contract.event_id == "between_66_67f"
    lo_c, hi_c = bounds_to_celsius(
        parsed.contract.rounded_lower,
        parsed.contract.rounded_upper,
        parsed.contract.unit,
    )
    assert lo_c == fahrenheit_to_celsius(D(66))
    assert hi_c == fahrenheit_to_celsius(D(68))


def test_parse_or_higher_and_iso_date_from_question() -> None:
    text = (
        "Will the highest temperature in Seoul (Incheon) be 32°C or higher "
        "on 2026-09-16?\n"
        "Highest temperature recorded by NOAA at Incheon Intl Airport Station "
        'under the "Temp" column. Whole degrees Celsius.\n'
        "https://www.weather.gov/wrh/timeseries?site=rksi\n"
        "2026-09-16T12:00:00Z"
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="d" * 64,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=rksi",
    )
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "RKSI"
    assert parsed.contract.event_id == "ge_32c"
    assert parsed.contract.rounded_lower == D(32)
    assert parsed.contract.rounded_upper is None
    assert parsed.contract.local_date == "2026-09-16"


def test_kmia_fixture_path_still_parses() -> None:
    text, rules_hash, source = _kmia_rules()
    parsed = parse_weather_contract(
        rules_text=text, rules_hash=rules_hash, resolution_source=source
    )
    assert parsed.ok
    assert parsed.contract is not None
    assert parsed.contract.station_id == "KMIA"
    assert parsed.contract.timezone == "America/New_York"
    assert parsed.contract.rounding.mode == "half_up"
    assert parsed.contract.event_id == "ge_32.0c"


def test_classify_tokyo_fixture_parse_ok() -> None:
    classified = classify_weather_market(FixtureGamma().get_market("900006"))
    assert classified.weather_like is True
    assert classified.specialist_parse_ok is True
    assert classified.specialist_parse_reason == "ok"
    assert classified.reason == "parsed_station_date_contract"


def test_weather_underground_fallback_does_not_override_nws_primary() -> None:
    _raw, text, source, parsed = _fixture_parse("900006")
    assert "Weather Underground" in text
    assert parsed.ok
    assert resolution_source_is_nws(source)


def test_accuweather_only_still_mismatches() -> None:
    _raw, _text, _source, parsed = _fixture_parse("900005")
    assert parsed.ok is False
    assert parsed.reason == "resolution_source_mismatch"


def test_ambiguous_station_abstains() -> None:
    text = (
        "Will the highest temperature in Tokyo be 24°C on September 16?\n"
        "station KMIA daily maximum, NOAA Temp column, whole degrees Celsius "
        "on 16 Sep '26. https://www.weather.gov/wrh/timeseries?site=rjtt\n"
        "2026-09-16T12:00:00Z"
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="e" * 64,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=rjtt",
    )
    assert parsed.ok is False
    assert parsed.reason == "ambiguous_station"


def test_unknown_station_without_timezone_abstains() -> None:
    text = (
        "Will the highest temperature in Nowhere be 24°C on September 16?\n"
        "Highest temperature, NOAA Temp column, whole degrees Celsius on "
        "16 Sep '26. https://www.weather.gov/wrh/timeseries?site=zzzz\n"
        "2026-09-16T12:00:00Z"
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="f" * 64,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=zzzz",
    )
    assert parsed.ok is False
    assert parsed.reason == "missing_timezone"


def test_missing_rounding_without_noaa_display_rules_abstains() -> None:
    text = (
        "station KMIA daily maximum for the local calendar date 2026-09-16 "
        "in America/New_York is at least 32.0 C."
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="a" * 64,
        resolution_source="https://api.weather.gov/stations/KMIA",
    )
    assert parsed.ok is False
    assert parsed.reason == "missing_rounding_rule"


def test_wrh_timeseries_path_is_not_a_timezone() -> None:
    _raw, text, _source, parsed = _fixture_parse("900006")
    assert "wrh/timeseries" in text
    assert parsed.ok
    assert parsed.contract is not None
    assert parsed.contract.timezone == "Asia/Tokyo"


def test_specialist_abstains_unspecified_rounding_on_tokyo(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900006")
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "rounding_unspecified"


def test_fahrenheit_conversion_is_exact_fraction() -> None:
    assert fahrenheit_to_celsius(D(32)) == D(0)
    assert fahrenheit_to_celsius(D(66)) == D(34) * D(5) / D(9)


def test_markets_from_search_payload_flattens_events() -> None:
    payload = {
        "events": [
            {
                "id": "1021077",
                "markets": [
                    {"id": "4547129", "question": "Tokyo 24°C"},
                    {"id": "4547058", "question": "Seoul 22°C or below"},
                ],
            }
        ],
        "pagination": {"hasMore": True, "totalResults": 2},
    }
    rows = markets_from_search_payload(payload)
    assert [row["id"] for row in rows] == ["4547129", "4547058"]


def test_network_gamma_discover_uses_public_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_ALLOW_NETWORK", "1")
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url, *, params, allowed_hosts, timeout):  # type: ignore[no-untyped-def]
        calls.append((url, dict(params or {})))
        assert "public-search" in url
        query = params["q"]
        page = int(params["page"])
        if query != WEATHER_SEARCH_QUERIES[0]:
            return {"events": [], "pagination": {"hasMore": False}}
        if page == 1:
            return {
                "events": [
                    {
                        "id": "e-tokyo",
                        "markets": [FixtureGamma().get_market("900006")],
                    }
                ],
                "pagination": {"hasMore": True},
            }
        return {
            "events": [
                {
                    "id": "e-seoul",
                    "markets": [FixtureGamma().get_market("900007")],
                }
            ],
            "pagination": {"hasMore": False},
        }

    monkeypatch.setattr("research_lab.adapters._http_get", fake_http_get)
    rows = NetworkGamma().list_markets(limit=10)
    assert [row["id"] for row in rows] == ["900006", "900007"]
    assert calls
    assert calls[0][1]["q"] == "highest temperature"
    classified = classify_weather_markets(rows)
    assert {row.market_id for row in classified if row.weather_like} == {
        "900006",
        "900007",
    }
    assert all(row.specialist_parse_ok for row in classified)
