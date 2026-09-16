"""Offline tests for Strategy A weather specialist + NWS fixture adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_lab.app import create_app
from research_lab.forecast import validate_decision_envelope, validate_forecast_dict
from research_lab.hashing import rules_hash_from_text
from research_lab.lab import Lab, _rules_text
from research_lab.money import D
from research_lab.specialist import (
    WEATHER_MODEL_VERSION,
    PlaceholderSpecialist,
    ResearchRequest,
    WeatherStationBaseline,
)
from research_lab.timeutil import parse_utc
from research_lab.weather_contract import parse_weather_contract, resolution_source_is_nws
from research_lab.weather_math import (
    IdentityBoundaryHook,
    RoundingReviewError,
    RoundingRule,
    conservative_band,
    interval_prob,
    normalize_review_rounding,
    normal_cdf,
    rounding_rule_from_review_hint,
)
from research_lab.weather_pipeline import estimate_to_decision, run_weather_baseline
from research_lab.weather_source import (
    FixtureNWS,
    NetworkNWS,
    WeatherSourceError,
    official_daily_max,
    unofficial_series_max_c,
)
from test_lab import _forecast, _lab, _prepare_tradeable

AS_OF = "2026-09-15T12:00:00+00:00"


def _kmia_rules() -> tuple[str, str, str]:
    from research_lab.adapters import FixtureGamma

    raw = FixtureGamma().get_market("900004")
    text = _rules_text(raw)
    return text, rules_hash_from_text(text), str(raw["resolutionSource"])


def _request_for_market(lab: Lab, market_id: str, **hint_overrides) -> ResearchRequest:
    market = lab.store.get_market(market_id)
    assert market is not None
    hints: dict = {}
    mid, mid_at = lab.yes_market_mid(market_id, as_of=AS_OF)
    if mid is not None:
        hints["market_mid"] = str(mid)
        hints["market_mid_available_at"] = mid_at
    hints.update(hint_overrides)
    return ResearchRequest(
        market_id=market.market_id,
        condition_id=market.condition_id,
        rules_text=market.rules_text,
        rules_hash=market.rules_hash,
        as_of=AS_OF,
        cutoff_at=market.cutoff_at,
        resolution_source=market.resolution_source,
        specialist_hints=hints,
    )


def test_normal_interval_math_known_values() -> None:
    # P(-1 <= Z < 1) ≈ 0.682689
    p = interval_prob(D(0), D(1), D(-1), D(1))
    assert float(p) == pytest.approx(0.682689, abs=1e-5)
    assert normal_cdf(0.0) == pytest.approx(0.5)
    # F(b)-F(a) open upper: P(T >= 0) = 0.5
    assert interval_prob(D(0), D(1), D(0), None) == D("0.500000")
    with pytest.raises(ValueError, match="sigma"):
        interval_prob(D(10), D(0), D(0), D(1))


def test_rounding_boundary_half_up() -> None:
    rule = RoundingRule(increment=D("0.1"), mode="half_up")
    lower, upper = rule.underlying_interval(D("32.0"), None, hook=IdentityBoundaryHook())
    assert lower == D("31.95")
    assert upper is None
    lower, upper = rule.underlying_interval(D("32.0"), D("33.0"))
    assert lower == D("31.95")
    assert upper == D("32.95")
    whole = RoundingRule(increment=D(1), mode="half_up")
    lo, hi = whole.underlying_interval(D(24), D(25))
    assert lo == D("23.5")
    assert hi == D("24.5")
    with pytest.raises(ValueError, match="unspecified rounding"):
        RoundingRule(increment=D(1), mode="unspecified").underlying_ge(D(24))


def test_normalize_review_rounding_requires_concrete_pair() -> None:
    empty = normalize_review_rounding()
    assert empty == {
        "rounding_mode": None,
        "rounding_increment": None,
        "rounding_unit": None,
    }
    unspecified = normalize_review_rounding(mode="unspecified")
    assert unspecified["rounding_mode"] == "unspecified"
    assert rounding_rule_from_review_hint(
        {"mode": "unspecified"}, contract_unit="C"
    ) == (None, "rounding_unspecified")
    filled = normalize_review_rounding(mode="half_up", increment="1", unit="c")
    assert filled == {
        "rounding_mode": "half_up",
        "rounding_increment": "1",
        "rounding_unit": "C",
    }
    with pytest.raises(RoundingReviewError, match="rounding_increment required"):
        normalize_review_rounding(mode="half_up")
    with pytest.raises(RoundingReviewError, match="rounding_mode required"):
        normalize_review_rounding(increment="1")
    rule, source = rounding_rule_from_review_hint(
        {"mode": "half_up", "increment": "1", "unit": "C"},
        contract_unit="C",
    )
    assert source == "rules_review"
    assert rule is not None
    assert rule.mode == "half_up"
    assert rule.increment == D(1)


def test_kmia_interval_uses_rounding_not_raw_threshold() -> None:
    # μ=33.2, σ=1.4, event rounded T >= 32.0 → T >= 31.95
    p_rounded = interval_prob(D("33.2"), D("1.4"), D("31.95"), None)
    p_raw = interval_prob(D("33.2"), D("1.4"), D("32.0"), None)
    assert p_rounded > p_raw
    p_low, p_high = conservative_band(p_rounded, deduction=D("0.05"))
    assert p_low <= p_rounded <= p_high


def test_parse_kmia_fixture_contract() -> None:
    text, rules_hash, source = _kmia_rules()
    parsed = parse_weather_contract(
        rules_text=text, rules_hash=rules_hash, resolution_source=source
    )
    assert parsed.ok
    assert parsed.contract is not None
    assert parsed.contract.station_id == "KMIA"
    assert parsed.contract.local_date == "2026-09-16"
    assert parsed.contract.timezone == "America/New_York"
    assert parsed.contract.rounded_lower == D("32.0")
    assert parsed.contract.event_id == "ge_32.0c"
    assert resolution_source_is_nws(source)


def test_abstain_missing_station(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900004")
    req = ResearchRequest(
        **{
            **req.__dict__,
            "rules_text": (
                req.rules_text.replace("station KMIA", "the airport").replace(
                    "https://api.weather.gov/stations/KMIA",
                    "https://api.weather.gov/stations",
                )
            ),
            "resolution_source": "https://api.weather.gov/stations",
        }
    )
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "missing_station"


def test_abstain_rules_source_mismatch_does_not_substitute_nws(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900005")
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "resolution_source_mismatch"
    assert "accuweather" in str(est.diagnostics.get("parse", {}).get("resolution_source", "")).lower()


def test_point_in_time_fixture_is_not_daily_max(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900001")
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason in {
        "quantity_not_daily_max",
        "missing_local_date",
        "missing_timezone",
        "resolution_source_mismatch",
    }


def test_rules_hint_mismatch_abstain(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900004", station_id="KABC")
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "rules_hint_mismatch"


def test_kmia_estimate_logs_all_variants_and_respects_available_at(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900004")
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ESTIMATE"
    assert est.p_yes is not None
    variants = est.diagnostics["variants"]
    assert set(variants) == {
        "raw_model",
        "calibrated_model",
        "historical_base_rate",
        "market_mid",
    }
    assert variants["calibrated_model"]["calibrator"] == "identity-stub-v0"
    assert variants["raw_model"]["p_yes"] == variants["calibrated_model"]["p_yes"]
    assert variants["historical_base_rate"]["p_yes"] == "0.42"
    assert variants["market_mid"]["p_yes"] is not None
    vintage = est.diagnostics["forecast_vintage"]
    assert vintage["model_run"] == "gfs-2026-09-15T06:00:00Z"
    assert vintage["predicted_max_c"] == "33.2"
    # Look-ahead forecast issued 12:30 must not be used.
    assert "12:30" not in vintage["issued_at"]
    obs_ids = {row["id"] for row in est.diagnostics["observations_known_at_as_of"]}
    assert "kmia-2026-09-15T11:00Z" in obs_ids
    # 11:50 obs + 20 min lag → available 12:10 > as_of 12:00
    assert "kmia-2026-09-15T11:50Z" not in obs_ids
    for src in est.diagnostics["sources"]:
        assert parse_utc(src["available_at"]) <= parse_utc(AS_OF)


def test_missing_max_is_not_zero_and_series_not_invented() -> None:
    snap = FixtureNWS().snapshot(
        station_id="KMIA", local_date="2026-09-16", as_of=AS_OF, event_id="ge_32.0c"
    )
    assert snap.official_daily_max.complete is False
    assert snap.official_daily_max.value_c is None
    assert snap.official_daily_max.reason == "missing_max_min_fields"
    unofficial = unofficial_series_max_c(snap.observations)
    assert unofficial == D("29.4")  # only the in-lag observation
    # Callers must not treat unofficial as official.
    official = official_daily_max(snap.observations, local_date="2026-09-16")
    assert official.value_c is None
    assert official.complete is False


def test_missing_forecast_max_abstains() -> None:
    req = ResearchRequest(
        market_id="x",
        condition_id=None,
        rules_text=(
            "station KABC daily maximum for the local calendar date 2026-09-16 "
            "in America/New_York is at least 32.0 C after half-up rounding to 0.1 C."
        ),
        rules_hash="a" * 64,
        as_of=AS_OF,
        cutoff_at=None,
        resolution_source="https://api.weather.gov/stations/KABC",
    )
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "missing_forecast_max"


def test_unknown_station_abstains() -> None:
    req = ResearchRequest(
        market_id="x",
        condition_id=None,
        rules_text=(
            "station KZZZ daily maximum for the local calendar date 2026-09-16 "
            "in America/New_York is at least 32.0 C after half-up rounding to 0.1 C."
        ),
        rules_hash="b" * 64,
        as_of=AS_OF,
        cutoff_at=None,
        resolution_source="https://api.weather.gov/stations/KZZZ",
    )
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "missing_station"


def test_look_ahead_as_of_drops_all_vintages() -> None:
    req = ResearchRequest(
        market_id="x",
        condition_id=None,
        rules_text=(
            "station KMIA daily maximum for the local calendar date 2026-09-16 "
            "in America/New_York is at least 32.0 C after half-up rounding to 0.1 C."
        ),
        rules_hash="c" * 64,
        as_of="2026-09-15T09:00:00+00:00",
        cutoff_at=None,
        resolution_source="https://api.weather.gov/stations/KMIA",
    )
    est = WeatherStationBaseline().estimate(req)
    assert est.status == "ABSTAIN"
    assert est.reason == "no_vintage_available"


def test_decision_envelope_validates_and_has_no_stake(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    req = _request_for_market(lab, "900004")
    decision = run_weather_baseline(req)
    assert decision["action"] == "PROPOSE"
    assert "stake" not in (decision.get("forecast") or {})
    envelope = validate_decision_envelope(decision)
    validated = validate_forecast_dict(envelope["forecast"])
    assert validated.model_version == WEATHER_MODEL_VERSION
    for src in validated.payload["sources"]:
        assert parse_utc(src["available_at"]) <= parse_utc(validated.as_of_iso)
    abstain = estimate_to_decision(
        PlaceholderSpecialist().estimate(req), req
    )
    # Placeholder reason is specialist_not_implemented; weather path maps ABSTAIN.
    assert abstain["action"] == "ABSTAIN"
    assert abstain.get("forecast") is None


def test_lab_logs_variants_and_does_not_auto_trade(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    decision = lab.weather_decision("900004", as_of=AS_OF)
    assert decision["action"] == "PROPOSE"
    assert decision["imported"] is False
    assert lab.store.open_position_count() == 0
    rows = lab.store.list_research_estimates("900004")
    assert len(rows) == 1
    assert set(rows[0]["variants"]) == {
        "raw_model",
        "calibrated_model",
        "historical_base_rate",
        "market_mid",
    }
    # Import still goes through risk-v2; without review it must not open.
    result = lab.import_forecast(decision["forecast"])
    assert result.action == "NO_TRADE"
    assert result.reason == "missing_rules_review"


def test_weather_import_into_paper_path_still_risk_v2(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab, "900004")
    lab.authorize_model(WEATHER_MODEL_VERSION, "paper-use only")
    decision = lab.weather_decision("900004", as_of=AS_OF)
    result = lab.import_forecast(decision["forecast"])
    assert result.action == "BUY"
    assert result.details["strategy"] == "A_specialist_fair_value"
    assert result.details["yes_no_gap"]["tradeable"] is False
    assert lab.risk.version == "risk-v2"


def test_network_nws_requires_flag() -> None:
    with pytest.raises(WeatherSourceError, match="NWS_ALLOW_NETWORK"):
        NetworkNWS()


def test_http_weather_flow_and_grok_does_not_override(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    with TestClient(create_app(lab)) as client:
        posted = client.post(
            "/api/specialist/weather",
            json={"market_id": "900004", "as_of": AS_OF},
        )
        assert posted.status_code == 200
        body = posted.json()
        assert body["action"] == "PROPOSE"
        assert body["imported"] is False
        critique = client.post("/api/grok/critique", json={"specialist": body}).json()
        assert critique["overrides_probability"] is False
        assert critique["numeric_path"] == "weather_station_baseline"
        assert "p_yes" not in critique
        mismatch = client.post(
            "/api/specialist/weather",
            json={"market_id": "900005", "as_of": AS_OF},
        ).json()
        assert mismatch["action"] == "ABSTAIN"
        assert mismatch["reason"] == "resolution_source_mismatch"
        budget = client.get("/api/research/budget").json()
        assert budget["stake_authority"] == "risk-v2"
        assert budget["weather_data_source"] == "fixtures"
        assert budget["nws_network"] is False
        grok = client.get("/api/grok/status").json()
        assert grok["wired"] is False
        assert grok["overrides_probability"] is False


def test_explicit_zero_is_not_treated_as_missing() -> None:
    from research_lab.weather_source import _optional_celsius

    assert _optional_celsius(None) is None
    assert _optional_celsius(0) == D(0)
    assert _optional_celsius("0") == D(0)
    est = PlaceholderSpecialist().estimate(
        ResearchRequest("x", None, "", "", "", None, None)
    )
    assert est.status == "ABSTAIN"


def test_existing_paper_path_unaffected(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-wx-compat-0001"))
    assert result.action == "BUY"
