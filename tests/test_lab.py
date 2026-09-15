"""Offline behavioral tests for the PAPER research lab. No network."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_lab.adapters import FixtureClob, FixtureGamma, NetworkGamma, AdapterError
from research_lab.app import create_app
from research_lab.core import DEFAULT_RISK_VERSION, RISK_V2, AccountView, Risk, evaluate_entry_gates
from research_lab.evaluation import (
    ClosedObservation,
    brier_score,
    cluster_bootstrap,
    log_loss,
    net_trade_pnl,
    ops_cost_adjusted_pnl,
    settled_yes_outcome,
)
from research_lab.fees import parse_market_fee_rate
from research_lab.forecast import ForecastValidationError, validate_forecast_dict
from research_lab.hashing import rules_hash_from_text
from research_lab.lab import Lab, LabError, OrderBook, simulate_fok, yes_no_cross_book_diagnostic
from research_lab.money import D, round_fee_up
from research_lab.specialist import PlaceholderSpecialist, ResearchRequest
from research_lab.store import Store
from research_lab.timeutil import FrozenClock, parse_utc

NOW = parse_utc("2026-09-15T12:00:00+00:00")
MODEL = "weather-station-model-v1-calibration-v1"


def _lab(tmp_path: Path, mode: str = "PAPER") -> Lab:
    gamma = FixtureGamma()
    clob = FixtureClob()
    lab = Lab.open(
        mode=mode,
        data_dir=tmp_path / mode.lower(),
        gamma=gamma,
        clob=clob,
        clock=FrozenClock(NOW),
    )
    lab.ingest_markets()
    return lab


def _forecast(lab: Lab, market_id: str, **overrides) -> dict:
    market = lab.store.get_market(market_id)
    assert market is not None
    body = {
        "forecast_id": "fx-900001-v1-2026-09-15T12:00:00Z",
        "market_id": market_id,
        "model_version": MODEL,
        "rules_hash": market.rules_hash,
        "as_of": "2026-09-15T12:00:00+00:00",
        "expires_at": "2026-09-15T13:00:00+00:00",
        "p_yes": 0.64,
        "p_low": 0.59,
        "p_high": 0.69,
        "sources": [
            {
                "url": "https://example.invalid/fixture-station-demo-1",
                "available_at": "2026-09-15T11:30:00+00:00",
            }
        ],
        "thesis": "Fixture thesis for accounting tests, not an edge claim.",
        "invalidation": "Rule change or new station file.",
    }
    body.update(overrides)
    return body


def _prepare_tradeable(lab: Lab, market_id: str = "900001") -> None:
    market = lab.store.get_market(market_id)
    assert market is not None
    lab.review_rules(
        market_id=market_id,
        rules_hash=market.rules_hash,
        cluster_id="fixture-weather",
        trading_cutoff="2026-09-20T12:00:00+00:00",
        reviewer="test",
        expected_resolution="manual fixture settle",
    )
    lab.authorize_model(MODEL, "paper-use only")


def test_unknown_fee_refuses_position(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab, "900002")
    result = lab.import_forecast(_forecast(lab, "900002", forecast_id="fx-unknown-fee-0001"))
    assert result.action == "NO_TRADE"
    assert result.reason == "unknown_fee"
    assert lab.store.open_position_for_market("900002") is None
    assert lab.store.cash() == D("10000")


def test_expired_forecast_refuses_position(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    result = lab.import_forecast(
        _forecast(
            lab,
            "900001",
            forecast_id="fx-expired-0001",
            as_of="2026-09-15T10:00:00+00:00",
            expires_at="2026-09-15T11:00:00+00:00",
            sources=[
                {
                    "url": "https://example.invalid/fixture-station-demo-1",
                    "available_at": "2026-09-15T09:30:00+00:00",
                }
            ],
        )
    )
    assert result.action == "NO_TRADE"
    assert result.reason == "expired_forecast"
    assert lab.store.list_positions("OPEN") == []


def test_missing_rules_review_refuses_position(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.authorize_model(MODEL)
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-noreview-0001"))
    assert result.action == "NO_TRADE"
    assert result.reason == "missing_rules_review"
    assert lab.store.list_positions("OPEN") == []


def test_idempotent_same_forecast_id(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    body = _forecast(lab, "900001", forecast_id="fx-idem-0001")
    first = lab.import_forecast(body)
    assert first.action == "BUY"
    cash_after = lab.store.cash()
    second = lab.import_forecast(body)
    assert second.idempotent is True
    assert second.reason == "idempotent_replay"
    assert lab.store.open_position_count() == 1
    assert lab.store.cash() == cash_after


def test_reject_mutated_same_forecast_id(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    body = _forecast(lab, "900001", forecast_id="fx-mutate-0001")
    lab.import_forecast(body)
    mutated = dict(body)
    mutated["p_yes"] = 0.61
    with pytest.raises(LabError, match="immutable"):
        lab.import_forecast(mutated)


def test_fok_fill_accounting(tmp_path: Path) -> None:
    book = OrderBook.from_clob(
        {
            "asks": [
                {"price": "0.40", "size": "10"},
                {"price": "0.41", "size": "10"},
            ],
            "bids": [],
            "min_order_size": "1",
            "tick_size": "0.01",
        },
        token_id="tok-yes-900001",
    )
    fill = simulate_fok(book, side="BUY", shares=D("15"), fee_bps=30)
    assert fill.filled is True
    assert fill.shares == D("15.00")
    assert fill.notional == D("6.050000")
    assert fill.avg_price == D("6.050000") / D("15")
    expected_fee = D("10") * D("0.003") * D("0.40") * D("0.60")
    expected_fee += D("5") * D("0.003") * D("0.41") * D("0.59")
    assert fill.fee == round_fee_up(expected_fee)

    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    before = lab.store.cash()
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-fok-acct-0001"))
    assert result.action == "BUY"
    assert result.token_side == "YES"
    pos = lab.store.open_position_for_market("900001")
    assert pos is not None
    shares = D(pos["shares"])
    notional = shares * D(pos["avg_price"])
    fee = D(pos["entry_fee"])
    assert lab.store.cash() == before - notional - fee
    fills = lab.store.list_fills()
    assert len(fills) == 1
    assert fills[0]["side"] == "BUY"


def test_settle_payoff_on_held_token(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-settle-yes-0001"))
    pos = lab.store.open_position_for_market("900001")
    assert pos is not None
    shares = D(pos["shares"])
    cash_before = lab.store.cash()
    out = lab.settle(
        "900001",
        payoff="1",
        source_url="https://example.invalid/manual-settle",
        note="held YES token pays 1",
    )
    assert out["token_side"] == "YES"
    assert out["payoff_held_token"] == "1"
    assert lab.store.cash() == cash_before + shares * D("1")
    assert lab.store.open_position_for_market("900001") is None
    settled = lab.store.list_positions("SETTLED")
    assert len(settled) == 1
    assert settled[0]["settle_payoff"] == "1"


def test_settle_payoff_on_held_no_token(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    lab.import_forecast(
        _forecast(
            lab,
            "900001",
            forecast_id="fx-settle-no-0001",
            p_yes=0.10,
            p_low=0.05,
            p_high=0.20,
        )
    )
    pos = lab.store.open_position_for_market("900001")
    assert pos is not None
    assert pos["token_side"] == "NO"
    shares = D(pos["shares"])
    cash_before = lab.store.cash()
    lab.settle("900001", payoff="1", source_url="https://example.invalid/no-pays")
    assert lab.store.cash() == cash_before + shares * D("1")
    assert settled_yes_outcome("NO", D("1")) == 0.0


def test_paper_and_demo_do_not_mix(tmp_path: Path) -> None:
    paper = _lab(tmp_path, "PAPER")
    demo = _lab(tmp_path, "DEMO")
    _prepare_tradeable(paper)
    paper.import_forecast(_forecast(paper, "900001", forecast_id="fx-mix-paper-0001"))
    assert paper.store.open_position_count() == 1
    assert demo.store.open_position_count() == 0
    assert demo.store.cash() == D("10000")
    with pytest.raises(LabError, match="PAPER"):
        demo.export_csv()
    csv_body = paper.export_csv()
    assert "900001" in csv_body
    assert "edge_proven" in csv_body
    with pytest.raises(RuntimeError, match="refusing"):
        Store(tmp_path / "paper" / "paper.sqlite", "DEMO")


def test_pause_and_look_ahead_source_rejected(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    lab.pause()
    paused = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-pause-0001"))
    assert paused.reason == "paused"
    lab.resume()
    with pytest.raises(ForecastValidationError, match="available_at"):
        validate_forecast_dict(
            _forecast(
                lab,
                "900001",
                forecast_id="fx-lookahead-0001",
                sources=[
                    {
                        "url": "https://example.invalid/future",
                        "available_at": "2026-09-15T12:30:00+00:00",
                    }
                ],
            )
        )


def test_stake_field_rejected_from_research_envelope(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    body = _forecast(lab, "900001")
    body["stake"] = 50
    with pytest.raises(ForecastValidationError):
        validate_forecast_dict(body)


def test_gates_daily_loss_drawdown() -> None:
    risk = Risk.v2()
    base = AccountView(
        cash=D("10000"),
        equity=D("10000"),
        peak_equity=D("10000"),
        start_of_day_equity=D("10000"),
        open_position_count=0,
        cluster_notional=D("0"),
        paused=False,
        valuation_complete=True,
    )
    lost = AccountView(**{**base.__dict__, "equity": D("9800")})
    assert evaluate_entry_gates(
        risk,
        lost,
        fee_known=True,
        forecast_expired=False,
        rules_review_present=True,
        model_authorized=True,
        rules_hash_matches=True,
    ).reason == "daily_loss"
    dd = AccountView(
        **{**base.__dict__, "equity": D("9500"), "start_of_day_equity": D("9500"), "peak_equity": D("10000")}
    )
    assert evaluate_entry_gates(
        risk,
        dd,
        fee_known=True,
        forecast_expired=False,
        rules_review_present=True,
        model_authorized=True,
        rules_hash_matches=True,
    ).reason == "drawdown"


def test_evaluation_hooks() -> None:
    rows = [
        ClosedObservation("weather", D("1.5"), 0.7, 1.0, "1"),
        ClosedObservation("weather", D("-0.5"), 0.7, 0.0, "2"),
        ClosedObservation("politics", D("0.2"), 0.4, 0.0, "3"),
    ]
    assert net_trade_pnl(rows) == D("1.200000")
    assert ops_cost_adjusted_pnl(D("1.2"), D("0.25")) == D("0.950000")
    assert brier_score(0.5, 1.0) == 0.25
    assert log_loss(0.5, 1.0) == pytest.approx(0.693147, rel=1e-5)
    boot = cluster_bootstrap(rows, n_resamples=50)
    assert boot.n_clusters == 2
    assert boot.as_dict()["warning"]


def test_specialist_and_network_guard() -> None:
    est = PlaceholderSpecialist().estimate(
        ResearchRequest("x", None, "", "", "", None, None)
    )
    assert est.status == "ABSTAIN"
    with pytest.raises(AdapterError, match="POLYMARKET_ALLOW_NETWORK"):
        NetworkGamma()


def test_http_paper_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_MODE", "PAPER")
    monkeypatch.setenv("POLYMARKET_DATA_SOURCE", "fixtures")
    lab = _lab(tmp_path)
    with TestClient(create_app(lab)) as client:
        health = client.get("/health").json()
        assert health["mode"] == "PAPER"
        assert health["live_trading"] is False
        ingest = client.post("/api/ingest")
        assert ingest.status_code == 200
        markets = client.get("/api/markets").json()["markets"]
        m = next(x for x in markets if x["market_id"] == "900001")
        client.post(
            "/api/rules-review",
            json={
                "market_id": "900001",
                "rules_hash": m["rules_hash"],
                "cluster_id": "fixture-weather",
                "trading_cutoff": "2026-09-20T12:00:00+00:00",
                "reviewer": "http-test",
            },
        )
        client.post("/api/models/authorize", json={"model_version": MODEL})
        body = _forecast(lab, "900001", forecast_id="fx-http-0001")
        posted = client.post("/api/forecast", json=body)
        assert posted.status_code == 200
        assert posted.json()["action"] == "BUY"
        dash = client.get("/")
        assert dash.status_code == 200
        assert "PAPER" in dash.text
        assert "No edge is proven" in dash.text
        csv = client.get("/api/export.csv")
        assert csv.status_code == 200
        assert "fx-http-0001" in csv.text
        settled = client.post(
            "/api/settle",
            json={
                "market_id": "900001",
                "payoff": "0",
                "source_url": "https://example.invalid/http-settle",
            },
        )
        assert settled.status_code == 200
        assert settled.json()["payoff_held_token"] == "0"
        abstain = client.post("/api/forecast", json={"action": "ABSTAIN", "reason": "no sources"})
        assert abstain.json()["imported"] is False
        grok = client.get("/api/grok/status").json()
        assert grok["wired"] is False


def test_risk_v2_defaults() -> None:
    risk = Risk.v2()
    assert risk.version == RISK_V2 == DEFAULT_RISK_VERSION
    assert risk.starting_cash == D("10000")
    assert risk.max_trade_cost == D("50.000000")
    assert risk.max_market_cost == D("100.000000")
    assert risk.max_cluster_notional == D("200.000000")
    assert risk.max_open_entry_cost == D("1000.000000")
    assert risk.max_daily_loss == D("150.000000")
    assert risk.max_drawdown == D("500.000000")
    assert risk.min_conservative_edge == D("0.03")
    assert risk.price_band_low == D("0.10")
    assert risk.max_spread == D("0.03")
    assert risk.max_depth_fraction == D("0.20")
    assert risk.max_book_age_seconds == 5
    assert risk.max_forecast_age_hours == 6
    assert risk.max_settlement_days == 14
    assert risk.max_entries_per_day == 20
    assert risk.per_share_research_reserve() == D("0.004")


def test_fee_schedule_and_unknown() -> None:
    gamma = FixtureGamma()
    assert parse_market_fee_rate(gamma.get_market("900001")) == D("0.05")
    assert parse_market_fee_rate(gamma.get_market("900002")) is None
    assert parse_market_fee_rate({"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1}}) is None
    assert parse_market_fee_rate({"feesEnabled": False}) == D(0)


def test_yes_no_gap_is_diagnostic_only() -> None:
    yes = OrderBook.from_clob(
        {
            "asks": [{"price": "0.40", "size": "10"}],
            "bids": [{"price": "0.39", "size": "10"}],
            "min_order_size": "1",
            "tick_size": "0.01",
        },
        token_id="y",
    )
    no = OrderBook.from_clob(
        {
            "asks": [{"price": "0.40", "size": "10"}],
            "bids": [{"price": "0.39", "size": "10"}],
            "min_order_size": "1",
            "tick_size": "0.01",
        },
        token_id="n",
    )
    diag = yes_no_cross_book_diagnostic(yes, no)
    assert diag["tradeable"] is False
    assert D(diag["apparent_gap_vs_1"]) > 0


def test_forecast_too_old(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    result = lab.import_forecast(
        _forecast(
            lab,
            "900001",
            forecast_id="fx-old-0001",
            as_of="2026-09-15T05:00:00+00:00",
            expires_at="2026-09-15T13:00:00+00:00",
            sources=[
                {
                    "url": "https://example.invalid/fixture-station-demo-1",
                    "available_at": "2026-09-15T04:30:00+00:00",
                }
            ],
        )
    )
    assert result.action == "NO_TRADE"
    assert result.reason == "forecast_too_old"
    assert lab.store.list_positions("OPEN") == []


def test_stale_book(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    assert isinstance(lab.clock, FrozenClock)
    lab.clock.set(NOW + timedelta(seconds=6))
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-stale-0001"))
    assert result.action == "NO_TRADE"
    assert result.reason == "stale_book"


def test_buy_does_not_use_yes_no_gap(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-gapdiag-0001"))
    assert result.action == "BUY"
    assert result.details["yes_no_gap"]["tradeable"] is False
    assert result.details["strategy"] == "A_specialist_fair_value"
    assert lab.risk.version == "risk-v2"


def test_rules_hash_matches_logged_text() -> None:
    gamma = FixtureGamma()
    market = gamma.get_market("900001")
    text = "\n".join(
        [
            str(market.get("question") or ""),
            str(market.get("description") or ""),
            str(market.get("resolutionSource") or ""),
            str(market.get("endDate") or ""),
        ]
    )
    assert len(rules_hash_from_text(text)) == 64
