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
from research_lab.lab import LabError, _rules_text
from research_lab.money import D
from research_lab.specialist import WeatherStationBaseline
from research_lab.weather_contract import (
    bounds_to_celsius,
    fahrenheit_to_celsius,
    parse_weather_contract,
    resolution_source_is_nws,
)
from test_lab import _lab
from test_weather_specialist import AS_OF, _kmia_rules, _request_for_market


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


def test_tokyo_rules_review_without_rounding_still_abstains(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    market = lab.store.get_market("900006")
    assert market is not None
    recorded = lab.review_rules(
        market_id="900006",
        rules_hash=market.rules_hash,
        cluster_id="tokyo-rjtt-daily-high",
        trading_cutoff="2026-09-16T15:00:00+00:00",
        reviewer="tester",
        expected_settlement_source=str(market.resolution_source or ""),
    )
    assert recorded["rounding_mode"] is None
    assert recorded["rounding_increment"] is None
    decision = lab.weather_decision("900006", as_of=AS_OF)
    assert decision["action"] == "ABSTAIN"
    assert decision["reason"] == "rounding_unspecified"


def test_tokyo_rules_review_half_up_clears_rounding_gate(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    market = lab.store.get_market("900006")
    assert market is not None
    with pytest.raises(LabError, match="rounding_increment required"):
        lab.review_rules(
            market_id="900006",
            rules_hash=market.rules_hash,
            cluster_id="tokyo-rjtt-daily-high",
            trading_cutoff="2026-09-16T15:00:00+00:00",
            reviewer="tester",
            rounding_mode="half_up",
        )
    recorded = lab.review_rules(
        market_id="900006",
        rules_hash=market.rules_hash,
        cluster_id="tokyo-rjtt-daily-high",
        trading_cutoff="2026-09-16T15:00:00+00:00",
        reviewer="tester",
        expected_settlement_source=str(market.resolution_source or ""),
        rounding_mode="half_up",
        rounding_increment="1",
        rounding_unit="C",
    )
    assert recorded["rounding_mode"] == "half_up"
    assert recorded["rounding_increment"] == "1"
    assert recorded["rounding_unit"] == "C"
    stored = lab.store.get_rules_review("900006")
    assert stored is not None
    assert stored["rounding_mode"] == "half_up"
    assert stored["rounding_increment"] == "1"
    decision = lab.weather_decision("900006", as_of=AS_OF)
    assert decision["reason"] != "rounding_unspecified"
    assert decision["action"] == "PROPOSE"
    assert decision["imported"] is False
    contract = (decision.get("diagnostics") or {}).get("contract") or {}
    assert contract.get("rounding_mode") == "half_up"
    assert contract.get("rounding_increment") == "1"
    assert contract.get("rounding_source") == "rules_review"
    request = lab.store.list_research_estimates("900006")[0]["request"]
    assert request["specialist_hints"]["rules_review_rounding"] == {
        "mode": "half_up",
        "increment": "1",
        "unit": "C",
    }


def test_review_rounding_unit_mismatch_abstains(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    market = lab.store.get_market("900006")
    assert market is not None
    lab.review_rules(
        market_id="900006",
        rules_hash=market.rules_hash,
        cluster_id="tokyo-rjtt-daily-high",
        trading_cutoff="2026-09-16T15:00:00+00:00",
        reviewer="tester",
        rounding_mode="half_up",
        rounding_increment="1",
        rounding_unit="F",
    )
    decision = lab.weather_decision("900006", as_of=AS_OF)
    assert decision["action"] == "ABSTAIN"
    assert decision["reason"] == "rounding_unit_mismatch"


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
        if "public-search" not in url:
            return []
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


def _live_noaa_city_rules(
    *,
    city: str,
    place: str,
    site: str,
    threshold: str,
    day_phrase: str = "September 25",
    abbrev: str = "25 Sep '26",
    end: str = "2026-09-25T12:00:00Z",
) -> str:
    """Rules shaped like a live Polymarket NOAA daily-high market. Offline only."""

    return (
        f"Will the highest temperature in {city} be {threshold} on {day_phrase}?\n"
        "This market will resolve to the temperature range that contains the highest "
        f"temperature recorded by NOAA at the {place} in degrees Celsius on {abbrev}.\n"
        "The resolution source for this market will be information from NOAA, "
        'specifically the highest reading under the "Temp" column for all times on '
        f"this day, available here: https://www.weather.gov/wrh/timeseries?site={site}\n"
        "The resolution source for this market measures temperatures to whole degrees "
        "Celsius (eg, 9°C).\n"
        f"{end}"
    )


def test_parse_london_city_airport_from_live_rules() -> None:
    text = _live_noaa_city_rules(
        city="London",
        place="London City Airport Station",
        site="eglc",
        threshold="22°C or below",
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="1" * 64,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=eglc",
    )
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "EGLC"
    assert parsed.contract.timezone == "Europe/London"
    assert parsed.contract.local_date == "2026-09-25"
    assert parsed.contract.event_id == "le_22c"
    assert parsed.contract.rounding.mode == "unspecified"
    assert parsed.details["timezone_source"] == "station_metadata"


def test_parse_busan_gimhae_from_live_rules() -> None:
    text = _live_noaa_city_rules(
        city="Busan",
        place="Gimhae Intl Airport Station",
        site="rkpk",
        threshold="20°C or below",
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="2" * 64,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=rkpk",
    )
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == "RKPK"
    assert parsed.contract.timezone == "Asia/Seoul"
    assert parsed.contract.local_date == "2026-09-25"
    assert parsed.contract.event_id == "le_20c"
    assert parsed.contract.rounding.mode == "unspecified"


@pytest.mark.parametrize(
    ("city", "place", "site", "timezone"),
    [
        ("Paris", "Paris-Le Bourget Airport Station", "lfpb", "Europe/Paris"),
        ("Ankara", "Esenboğa Intl Airport Station", "ltac", "Europe/Istanbul"),
        ("Wellington", "Wellington Intl Airport Station", "nzwn", "Pacific/Auckland"),
        ("Wuhan", "Wuhan Tianhe International Airport Station", "zhhh", "Asia/Shanghai"),
        ("Shanghai", "Shanghai Pudong International Airport Station", "zspd", "Asia/Shanghai"),
        ("Tel Aviv", "Ben Gurion International Airport", "LLBG", "Asia/Jerusalem"),
    ],
)
def test_parse_verified_live_city_stations(
    city: str, place: str, site: str, timezone: str
) -> None:
    text = _live_noaa_city_rules(
        city=city,
        place=place,
        site=site,
        threshold="24°C",
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="3" * 64,
        resolution_source=f"https://www.weather.gov/wrh/timeseries?site={site}",
    )
    assert parsed.ok, parsed.reason
    assert parsed.contract is not None
    assert parsed.contract.station_id == site.upper()
    assert parsed.contract.timezone == timezone
    assert parsed.contract.event_id == "eq_24c"


def test_tel_aviv_without_resolution_source_field_is_not_missing_timezone() -> None:
    text = _live_noaa_city_rules(
        city="Tel Aviv",
        place="Ben Gurion International Airport",
        site="LLBG",
        threshold="26°C or below",
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="4" * 64,
        resolution_source=None,
    )
    assert parsed.ok is False
    assert parsed.reason == "missing_resolution_source"


def test_hong_kong_observatory_rules_abstain_missing_station() -> None:
    text = (
        "Will the highest temperature in Hong Kong be 25°C or below on September 25?\n"
        "This market will resolve to the temperature range that contains the highest "
        "temperature recorded by the Hong Kong Observatory in degrees Celsius on 25 Sep '26.\n"
        "The resolution source for this market will be information from the Hong Kong "
        'Observatory, specifically the "Absolute Daily Max (deg. C)" the specified date '
        "once information is finalized in the relevant \"Daily Extract\", available here: "
        "https://www.weather.gov.hk/en/cis/climat.htm\n"
        "The resolution source for this market measures temperatures in Celsius to one "
        "decimal place (eg, 9.1°C).\n"
        "2026-09-25T12:00:00Z"
    )
    parsed = parse_weather_contract(
        rules_text=text,
        rules_hash="5" * 64,
        resolution_source="https://www.weather.gov.hk/en/cis/climat.htm",
    )
    assert parsed.ok is False
    assert parsed.reason == "missing_station"
