"""Offline tests for same-day conditioning and sigma bookkeeping.

No network. No live orders. edge_proven stays false.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from research_lab.money import D
from research_lab.paper_runner import PaperRunConfig, run_forward_paper
from research_lab.same_day import (
    DIURNAL_END_HOUR,
    DIURNAL_START_HOUR,
    RESIDUAL_FRACTION,
    assess_same_day,
    conditioned_sigma,
    diurnal_fraction_remaining,
    predictive_probability,
    truncated_interval_prob,
)
from research_lab.sigma_calibration import (
    IDENTITY_CALIBRATION_ID,
    ForecastErrorPair,
    TableCalibrator,
    climatological_day_to_day_sigma,
    fit_forecast_error_by_lead,
    identity_stub_record,
    model_version_for,
)
from research_lab.specialist import (
    WEATHER_CALIBRATION_VERSION,
    WEATHER_MODEL_VERSION,
    ResearchRequest,
    WeatherStationBaseline,
)
from research_lab.timeutil import parse_utc
from research_lab.weather_math import interval_prob
from research_lab.weather_pipeline import run_weather_baseline
from research_lab.weather_source import (
    FixtureNWS,
    WeatherObservationRecord,
    WeatherSourceError,
)
from test_lab import _lab
from test_weather_specialist import (
    AS_OF,
    _F79_C,
    _klga_http_get,
    _klga_network_responses,
    _klga_request,
    _network_nws,
)

AS_OF_SAME = "2026-09-28T20:00:00+00:00"  # 16:00 EDT


def _obs(
    *,
    obs_id: str,
    observed_at: str,
    available_at: str,
    temperature_c: str | None,
    url: str | None = None,
) -> WeatherObservationRecord:
    return WeatherObservationRecord(
        station_id="KMIA",
        observation_id=obs_id,
        observed_at=observed_at,
        available_at=available_at,
        temperature_c=None if temperature_c is None else D(temperature_c),
        daily_max_c=None,
        daily_min_c=None,
        max_min_fields_present=False,
        series_complete_for_local_date=False,
        url=url or f"https://api.weather.gov/stations/KMIA/observations/{obs_id}",
    )


def test_diurnal_fraction_shrinks_through_the_afternoon() -> None:
    morning = diurnal_fraction_remaining(DIURNAL_START_HOUR)
    midday = diurnal_fraction_remaining(D(13))
    evening = diurnal_fraction_remaining(DIURNAL_END_HOUR)
    assert morning == D(1)
    assert evening == RESIDUAL_FRACTION
    assert morning > midday > evening
    early = conditioned_sigma(D("1.5"), morning)
    late = conditioned_sigma(D("1.5"), evening)
    assert early == D("1.500000")
    assert late < early
    assert late >= D("0.15")
    # The floor must not widen a scale that is already below it.
    assert conditioned_sigma(D("0.10"), D(1)) == D("0.10")


def test_truncation_puts_zero_mass_below_the_observed_floor() -> None:
    p, mu_used, shifted = truncated_interval_prob(
        D(20), D(1), D(10), D(15), D(18)
    )
    assert p == D("0.000000")
    assert shifted is False
    assert mu_used == D(20)


def test_incompatible_forecast_shifts_to_the_floor() -> None:
    # μ many sigmas below the floor → remaining rise is a half-normal at the floor.
    p, mu_used, shifted = truncated_interval_prob(
        D(20), D("0.5"), D(30), D(31), D(30)
    )
    assert shifted is True
    assert mu_used == D(30)
    assert p > D("0.9")


def test_same_day_ignores_later_and_other_date_observations() -> None:
    as_of = parse_utc(AS_OF_SAME)
    observations = [
        _obs(
            obs_id="earlier",
            observed_at="2026-09-28T18:00:00+00:00",
            available_at="2026-09-28T18:20:00+00:00",
            temperature_c="30.0",
        ),
        _obs(
            obs_id="floor",
            observed_at="2026-09-28T19:00:00+00:00",
            available_at="2026-09-28T19:20:00+00:00",
            temperature_c="32.0",
        ),
        _obs(
            obs_id="lookahead",
            observed_at="2026-09-28T20:30:00+00:00",
            available_at="2026-09-28T20:50:00+00:00",
            temperature_c="40.0",
        ),
        _obs(
            obs_id="previous-local-date",
            observed_at="2026-09-28T03:00:00+00:00",
            available_at="2026-09-28T03:20:00+00:00",
            temperature_c="35.0",
        ),
    ]
    state = assess_same_day(
        as_of=as_of,
        local_date="2026-09-28",
        timezone="America/New_York",
        observations=observations,
        observations_status="available",
        observations_note="unit",
    )
    assert state.mode == "conditioned"
    assert state.floor_c == D("32.0")
    assert state.observation_id == "floor"
    assert parse_utc(state.available_at or "") <= as_of
    assert state.n_temperatures == 2
    not_today = assess_same_day(
        as_of=as_of,
        local_date="2026-09-29",
        timezone="America/New_York",
        observations=observations,
        observations_status="available",
    )
    assert not_today.mode == "not_same_day"
    assert not_today.floor_c is None


def test_unavailable_observations_abstain_and_empty_fetch_falls_back() -> None:
    as_of = parse_utc(AS_OF_SAME)
    missing = assess_same_day(
        as_of=as_of,
        local_date="2026-09-28",
        timezone="America/New_York",
        observations=[],
        observations_status="unavailable",
        observations_note="observations_endpoint_unavailable",
    )
    assert missing.mode == "abstain"
    assert missing.reason == "same_day_observations_unavailable"
    empty = assess_same_day(
        as_of=as_of,
        local_date="2026-09-28",
        timezone="America/New_York",
        observations=[],
        observations_status="available",
        observations_note="observations_collection_empty",
    )
    assert empty.mode == "fallback_no_temperature_yet"
    assert empty.floor_c is None
    draw = predictive_probability(D(31), D("1.5"), D("30"), D("33"), empty)
    assert draw.p_yes == interval_prob(D(31), D("1.5"), D("30"), D("33"))
    assert draw.shifted_to_floor is False


def _same_day_archive(path: Path) -> None:
    archive = {
        "observation_lag_minutes": 20,
        "stations": {
            "KMIA": {
                "timezone": "America/New_York",
                "forecasts": [
                    {
                        "issued_at": "2026-09-28T12:00:00+00:00",
                        "available_at": "2026-09-28T12:00:00+00:00",
                        "model_run": "fixture-same-day",
                        "valid_date_local": "2026-09-28",
                        "predicted_max_c": "31.0",
                        "horizon_hours": "6",
                        "url": "https://api.weather.gov/gridpoints/MFL/1,1/forecast",
                    }
                ],
                "observations": [
                    {
                        "id": "floor",
                        "observed_at": "2026-09-28T19:00:00+00:00",
                        "available_at": "2026-09-28T19:20:00+00:00",
                        "temperature_c": "32.0",
                        "daily_max_c": None,
                        "daily_min_c": None,
                        "max_min_fields_present": False,
                        "series_complete_for_local_date": False,
                        "url": "https://api.weather.gov/stations/KMIA/observations/floor",
                    },
                    {
                        "id": "lookahead",
                        "observed_at": "2026-09-28T20:30:00+00:00",
                        "available_at": "2026-09-28T20:50:00+00:00",
                        "temperature_c": "40.0",
                        "daily_max_c": None,
                        "daily_min_c": None,
                        "max_min_fields_present": False,
                        "series_complete_for_local_date": False,
                        "url": "https://api.weather.gov/stations/KMIA/observations/lookahead",
                    },
                ],
            }
        },
        "error_sigma_c": {"KMIA": {"0-24": "1.5"}},
    }
    path.write_text(json.dumps(archive), encoding="utf-8")


def _same_day_request(**overrides) -> ResearchRequest:
    payload = {
        "market_id": "same-day",
        "condition_id": None,
        "rules_text": (
            "station KMIA daily maximum for the local calendar date 2026-09-28 "
            "in America/New_York is at least 32.0 C after half-up rounding to 0.1 C."
        ),
        "rules_hash": "e" * 64,
        "as_of": AS_OF_SAME,
        "cutoff_at": None,
        "resolution_source": "https://api.weather.gov/stations/KMIA",
        "specialist_hints": {"market_mid": "0.60", "market_mid_available_at": AS_OF_SAME},
    }
    payload.update(overrides)
    return ResearchRequest(**payload)


def test_specialist_conditions_on_same_day_floor_and_records_vintage(tmp_path: Path) -> None:
    archive = tmp_path / "nws.json"
    _same_day_archive(archive)
    source = FixtureNWS(archive)
    req = _same_day_request()
    est = WeatherStationBaseline(source).estimate(req)
    assert est.status == "ESTIMATE", est.reason
    assert est.diagnostics["edge_proven"] is False
    same = est.diagnostics["same_day"]
    assert same["mode"] == "conditioned"
    assert same["floor_c"] == "32.0"
    assert same["official_daily_max"] is False
    assert same["shifted_to_observed_max"] is False
    assert D(same["sigma_conditioned_c"]) < D(same["sigma_unconditional_c"])
    assert D(same["unconditional_p_yes"]) != est.p_yes
    # Bucket entirely below the observed floor is impossible.
    below = _same_day_request(
        rules_text=(
            "station KMIA daily maximum for the local calendar date 2026-09-28 "
            "in America/New_York be between 30.0 and 31.0 C after half-up rounding to 0.1 C."
        )
    )
    below_est = WeatherStationBaseline(source).estimate(below)
    assert below_est.status == "ESTIMATE"
    assert below_est.p_yes == D("0.000000")
    assert est.diagnostics["official_daily_max"]["complete"] is False
    urls = {row["url"] for row in est.diagnostics["sources"]}
    assert "https://api.weather.gov/stations/KMIA/observations/floor" in urls
    assert "https://api.weather.gov/stations/KMIA/observations/lookahead" not in urls
    for src in est.diagnostics["sources"]:
        assert parse_utc(src["available_at"]) <= parse_utc(AS_OF_SAME)
    assert est.diagnostics["variants"]["market_mid"]["p_yes"] == "0.600000"
    assert est.diagnostics["calibration"]["kind"] == "identity_stub"
    assert est.diagnostics["calibration"]["out_of_sample"] is False
    assert est.diagnostics["calibration"]["applies_to_probabilities"] is False
    assert est.diagnostics["calibration"]["edge_proven"] is False
    assert "archive" in est.diagnostics["calibration"]["note"]
    assert "Temp" in est.diagnostics["calibration"]["note"]
    decision = run_weather_baseline(req, specialist=WeatherStationBaseline(source))
    assert decision["comparison"]["market_mid"] == "0.600000"
    assert decision["comparison"]["edge_proven"] is False
    assert decision["comparison"]["model_minus_market_mid"] is not None
    assert decision["forecast"]["model_version"] == WEATHER_MODEL_VERSION


def test_network_same_day_without_observations_abstains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _network_nws(
        monkeypatch, _klga_http_get(missing={"stations/KLGA/observations"})
    )
    est = WeatherStationBaseline(source).estimate(_klga_request())
    assert est.status == "ABSTAIN"
    assert est.reason == "same_day_observations_unavailable"
    assert est.diagnostics["edge_proven"] is False
    assert est.p_yes is None


def test_network_empty_observation_collection_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _klga_network_responses()
    responses["stations/KLGA/observations"] = {"type": "FeatureCollection", "features": []}
    calls: list[str] = []

    def _get(url: str, *, timeout: float = 20.0) -> dict:
        del timeout
        calls.append(url)
        path = urlparse(url).path.lstrip("/")
        if path not in responses:
            raise WeatherSourceError(f"GET {url} -> 404")
        return json.loads(json.dumps(responses[path]))

    source = _network_nws(monkeypatch, _get)
    est = WeatherStationBaseline(source).estimate(_klga_request())
    assert est.status == "ESTIMATE", est.reason
    assert est.diagnostics["same_day"]["mode"] == "fallback_no_temperature_yet"
    expected = interval_prob(_F79_C, D("1.5"), D("25.95"), None)
    assert est.p_yes == expected
    obs_calls = [url for url in calls if urlparse(url).path.endswith("/observations")]
    assert obs_calls
    query = parse_qs(urlparse(obs_calls[0]).query)
    assert "start" in query
    assert "end" in query
    assert query["limit"] == ["500"]


def test_observations_query_stays_on_nws_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    source = _network_nws(monkeypatch, _klga_http_get(calls=calls))
    snap = source.snapshot(
        station_id="KLGA",
        local_date="2026-09-16",
        as_of="2026-09-16T12:00:00+00:00",
    )
    assert snap.observations_status == "available"
    obs_urls = [url for url in calls if "observations" in url]
    assert obs_urls
    # _klga_http_get records the path, not the query. The snapshot archive keeps the URL.
    assert any("start=" in url for url in snap.archive_urls)
    host = urlparse(next(url for url in snap.archive_urls if "observations" in url)).hostname
    assert host == "api.weather.gov"


def test_identity_stub_documents_the_missing_pairs() -> None:
    record = identity_stub_record()
    assert record.calibration_id == IDENTITY_CALIBRATION_ID
    assert record.calibration_id == WEATHER_CALIBRATION_VERSION
    assert record.out_of_sample is False
    assert record.applies_to_probabilities is False
    assert record.edge_proven is False
    assert "archived_issued_nws_forecasts" in record.missing
    assert "hourly_temp_column" in " ".join(record.missing)
    assert WEATHER_MODEL_VERSION == model_version_for(record.calibration_id)
    with pytest.raises(ValueError, match="edge_proven"):
        record.__class__(**{**record.to_dict(), "missing": tuple(record.missing), "edge_proven": True, "sigma_by_lead_c": {}})


def test_forecast_error_fit_changes_calibration_id_and_refuses_fixtures() -> None:
    pairs = [
        ForecastErrorPair(D(6), D(30), D(29)),
        ForecastErrorPair(D(6), D(30), D(31)),
        ForecastErrorPair(D(30), D(28), D(26)),
        ForecastErrorPair(D(30), D(28), D(30)),
    ]
    fixture = fit_forecast_error_by_lead(
        pairs,
        source="unit_fixture",
        out_of_sample=False,
        apply_to_probabilities=True,
    )
    assert fixture.applies_to_probabilities is False
    assert fixture.out_of_sample is False
    assert fixture.kind == "forecast_error_by_lead"
    assert "0-12" in fixture.sigma_by_lead_c
    assert "24-48" in fixture.sigma_by_lead_c
    assert "not_out_of_sample" in fixture.missing
    assert fixture.edge_proven is False
    with pytest.raises(ValueError, match="not applicable"):
        TableCalibrator(fixture)

    other = fit_forecast_error_by_lead(
        [
            ForecastErrorPair(D(6), D(30), D(28)),
            ForecastErrorPair(D(6), D(30), D(32)),
            ForecastErrorPair(D(30), D(28), D(24)),
            ForecastErrorPair(D(30), D(28), D(32)),
        ],
        source="unit_fixture",
        out_of_sample=False,
    )
    assert other.calibration_id != fixture.calibration_id
    assert model_version_for(other.calibration_id) != model_version_for(fixture.calibration_id)
    assert model_version_for(fixture.calibration_id) != WEATHER_MODEL_VERSION

    thin = fit_forecast_error_by_lead(
        [ForecastErrorPair(D(6), D(1), D(1))],
        source="paired_forecast_and_resolution_quantity",
        out_of_sample=True,
        apply_to_probabilities=True,
    )
    assert thin.calibration_id == IDENTITY_CALIBRATION_ID
    assert thin.applies_to_probabilities is False
    assert "insufficient_forecast_error_pairs" in thin.missing


def test_resolution_pair_table_changes_model_version_without_claiming_oos(tmp_path: Path) -> None:
    del tmp_path
    pairs = [
        ForecastErrorPair(D(18), D("33.2"), D("32.0")),
        ForecastErrorPair(D(18), D("33.2"), D("34.0")),
    ]
    record = fit_forecast_error_by_lead(
        pairs,
        source="paired_forecast_and_resolution_quantity",
        out_of_sample=False,
        apply_to_probabilities=True,
    )
    assert record.applies_to_probabilities is True
    assert record.out_of_sample is False
    assert "not_out_of_sample" in record.missing
    assert "not statistical validation" in record.note
    calibrator = TableCalibrator(record)
    model = WeatherStationBaseline(FixtureNWS(), calibrator=calibrator)
    assert model.model_version == model_version_for(record.calibration_id)
    assert model.model_version != WEATHER_MODEL_VERSION
    req = ResearchRequest(
        market_id="x",
        condition_id=None,
        rules_text=(
            "station KMIA daily maximum for the local calendar date 2026-09-16 "
            "in America/New_York is at least 32.0 C after half-up rounding to 0.1 C."
        ),
        rules_hash="f" * 64,
        as_of="2026-09-15T12:00:00+00:00",
        cutoff_at=None,
        resolution_source="https://api.weather.gov/stations/KMIA",
    )
    est = model.estimate(req)
    assert est.status == "ESTIMATE"
    assert est.diagnostics["edge_proven"] is False
    assert est.diagnostics["calibration"]["out_of_sample"] is False
    assert est.diagnostics["model_version"] == model.model_version
    raw_sigma = D(est.diagnostics["variants"]["raw_model"]["sigma_c"])
    cal_sigma = D(est.diagnostics["variants"]["calibrated_model"]["sigma_c"])
    assert cal_sigma == D(record.sigma_by_lead_c["12-24"])
    assert cal_sigma != raw_sigma
    assert est.diagnostics["variants"]["raw_model"]["p_yes"] != (
        est.diagnostics["variants"]["calibrated_model"]["p_yes"]
    )


def test_climatological_fallback_is_labeled_and_not_applied() -> None:
    quiet = climatological_day_to_day_sigma([D(10), D(12), D(14)])
    assert quiet.calibration_id == IDENTITY_CALIBRATION_ID
    assert "degenerate_day_to_day_dispersion" in quiet.missing
    short = climatological_day_to_day_sigma([D(10), D(12)])
    assert "insufficient_day_to_day_sample" in short.missing

    series = [D(10), D(13), D(12)]
    record = climatological_day_to_day_sigma(series)
    assert record.kind == "climatological_day_to_day"
    assert record.out_of_sample is False
    assert record.applies_to_probabilities is False
    assert record.edge_proven is False
    assert record.sigma_c == "2.828427"
    assert "not_forecast_error" in record.missing
    assert "not an out-of-sample" in record.note
    moved = climatological_day_to_day_sigma([D(10), D(14), D(11)])
    assert moved.calibration_id != record.calibration_id
    with pytest.raises(ValueError, match="not applicable"):
        TableCalibrator(record)


def test_market_mid_uses_latest_book_at_or_before_as_of(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    market = lab.store.get_market("900004")
    assert market is not None
    later = "2026-09-15T13:00:00+00:00"
    lab.store.insert_book(
        market_id="900004",
        token_id=market.yes_token_id or "",
        token_side="YES",
        snapshot={
            "bids": [{"price": "0.10", "size": "10"}],
            "asks": [{"price": "0.12", "size": "10"}],
        },
        rules_hash=market.rules_hash,
        fee_bps=None,
        meta={},
        captured_at=later,
    )
    mid, mid_at = lab.yes_market_mid("900004", as_of=AS_OF)
    assert mid == D("0.55")
    assert mid_at == AS_OF
    early, early_at = lab.yes_market_mid("900004", as_of="2026-09-15T11:00:00+00:00")
    assert early is None
    assert early_at is None
    decision = lab.weather_decision("900004", as_of=AS_OF)
    assert decision["action"] == "PROPOSE"
    assert decision["comparison"]["market_mid"] == "0.550000"
    assert decision["comparison"]["edge_proven"] is False
    assert decision["variants"]["market_mid"]["p_yes"] == "0.550000"
    gap = D(decision["comparison"]["model_minus_market_mid"])
    assert gap == D(decision["comparison"]["model_p_yes"]) - D("0.550000")
    later_decision = lab.weather_decision("900004", as_of=later)
    assert later_decision["comparison"]["market_mid"] == "0.110000"


def test_paper_run_forecast_output_shows_market_gap(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    summary = run_forward_paper(
        lab,
        PaperRunConfig(import_paper=False, max_cycles=1, as_of=AS_OF),
    )
    assert summary["edge_proven"] is False
    rows = {row["market_id"]: row for row in summary["cycles"][0]["markets"]}
    kmia = rows["900004"]
    assert kmia["action"] == "FORECAST"
    assert kmia["market_mid"] == "0.550000"
    assert kmia["comparison"]["edge_proven"] is False
    assert kmia["model_minus_market_mid"] is not None


def test_book_timestamp_parse_does_not_depend_on_string_order(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    market = lab.store.get_market("900004")
    assert market is not None
    # Zulu form sorts after '+00:00' as text, but it is the same instant as AS_OF
    # only if we compared strings. The stored book is already at AS_OF; a later
    # Zulu stamp must win on the instant, not on character order.
    zulu = "2026-09-15T12:30:00Z"
    lab.store.insert_book(
        market_id="900004",
        token_id=market.yes_token_id or "",
        token_side="YES",
        snapshot={
            "bids": [{"price": "0.20", "size": "5"}],
            "asks": [{"price": "0.22", "size": "5"}],
        },
        rules_hash=market.rules_hash,
        fee_bps=None,
        meta={},
        captured_at=zulu,
    )
    mid, mid_at = lab.yes_market_mid(
        "900004", as_of="2026-09-15T12:30:00+00:00"
    )
    assert mid == D("0.21")
    assert mid_at.startswith("2026-09-15T12:30:00")
    # The Zulu row is after AS_OF, so the earlier book remains the mid.
    earlier, _ = lab.yes_market_mid("900004", as_of=AS_OF)
    assert earlier == D("0.55")
