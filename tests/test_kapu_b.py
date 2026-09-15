"""Offline Kapu B measurement-path tests. No network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_lab.adapters import AdapterError, FixtureClob, FixtureGamma, NetworkGamma
from research_lab.app import create_app
from research_lab.discovery import classify_weather_market, classify_weather_markets
from research_lab.lab import Lab, LabError
from research_lab.money import D
from research_lab.paper_runner import PaperRunConfig, run_forward_paper
from research_lab.research_budget import ResearchBudget, ResearchBudgetError
from research_lab.specialist import WEATHER_MODEL_VERSION
from research_lab.timeutil import FrozenClock
from test_lab import MODEL, NOW, _lab

AS_OF = "2026-09-15T12:00:00+00:00"


def _paper_lab(tmp_path: Path) -> Lab:
    return _lab(tmp_path, mode="PAPER")


def test_weather_like_classifier_fixtures() -> None:
    rows = FixtureGamma().list_markets(limit=20)
    classified = {row.market_id: row for row in classify_weather_markets(rows)}
    assert classified["900001"].weather_like is True
    assert classified["900004"].weather_like is True
    assert classified["900004"].specialist_parse_ok is True
    assert classified["900005"].weather_like is True
    assert classified["900005"].specialist_parse_ok is False
    assert classified["900002"].weather_like is False
    assert classified["900003"].weather_like is False
    coin = classify_weather_market(FixtureGamma().get_market("900002"))
    assert coin.reason == "not_weather_like"


def test_discover_and_ingest_archives_raw_and_rules_hash(tmp_path: Path) -> None:
    lab = Lab.open(
        mode="PAPER",
        data_dir=tmp_path / "paper",
        gamma=FixtureGamma(),
        clob=FixtureClob(),
        clock=FrozenClock(NOW),
    )
    result = lab.discover_weather_markets()
    assert result["source"] == "fixtures"
    assert result["network"] is False
    assert result["live_orders"] is False
    assert set(result["weather_like_ids"]) == {"900001", "900004", "900005"}
    assert set(result["ingested"]) == {"900001", "900004", "900005"}
    assert lab.store.get_market("900002") is None
    kmia = lab.store.get_market("900004")
    assert kmia is not None
    assert len(kmia.rules_hash) == 64
    kinds = {row["kind"] for row in lab.store.list_raw_archive(limit=100)}
    assert {"gamma_list", "gamma_market", "clob_book"} <= kinds
    books = lab.store.list_raw_archive(market_id="900004", kind="clob_book")
    assert len(books) == 2
    assert all(row["rules_hash"] == kmia.rules_hash for row in books)
    assert all(row["source"] == "fixtures" for row in lab.store.list_raw_archive(limit=100))


def test_full_ingest_still_archives_and_keeps_non_weather(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    ids = {m.market_id for m in lab.store.list_markets()}
    assert {"900001", "900002", "900003", "900004", "900005"} <= ids
    archive = lab.store.list_raw_archive(kind="gamma_list")
    assert archive
    assert archive[0]["payload_hash"]


def test_network_discovery_requires_explicit_flag() -> None:
    with pytest.raises(AdapterError, match="POLYMARKET_ALLOW_NETWORK"):
        NetworkGamma()


def test_rules_review_cli_fields_and_missing_review_refuses(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    market = lab.store.get_market("900004")
    assert market is not None
    bundle = lab.market_review_bundle("900004")
    assert bundle["rules_text"] == market.rules_text
    assert bundle["reviewed"] is False
    with pytest.raises(LabError, match="rules_hash must match"):
        lab.review_rules(
            market_id="900004",
            rules_hash="0" * 64,
            cluster_id="kmia-station-date",
            trading_cutoff="2026-09-16T22:00:00+00:00",
            reviewer="tester",
            expected_settlement_source="https://api.weather.gov/stations/KMIA",
        )
    recorded = lab.review_rules(
        market_id="900004",
        rules_hash=market.rules_hash,
        cluster_id="kmia-station-date",
        trading_cutoff="2026-09-16T22:00:00+00:00",
        reviewer="tester",
        expected_settlement_source="https://api.weather.gov/stations/KMIA",
        paper_model_version=WEATHER_MODEL_VERSION,
        authorize_model_version=WEATHER_MODEL_VERSION,
    )
    assert recorded["status"] == "recorded"
    assert recorded["expected_settlement_source"].endswith("/KMIA")
    assert recorded["authorized_for_paper_use_only"] is True
    review = lab.store.get_rules_review("900004")
    assert review is not None
    assert review["cluster_id"] == "kmia-station-date"
    assert review["paper_model_version"] == WEATHER_MODEL_VERSION
    # Unreviewed market still cannot open a weather paper position.
    other = lab.store.get_market("900001")
    assert other is not None
    lab.authorize_model(MODEL)
    from test_lab import _forecast

    refused = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-kapu-b-noreview"))
    assert refused.action == "NO_TRADE"
    assert refused.reason == "missing_rules_review"


def test_forecast_archive_is_rerunnable_including_abstain(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    propose = lab.weather_decision("900004", as_of=AS_OF)
    abstain = lab.weather_decision("900005", as_of=AS_OF)
    assert propose["action"] == "PROPOSE"
    assert abstain["action"] == "ABSTAIN"
    rows = lab.store.list_research_estimates("900004")
    assert rows[0]["request"]["rules_text"]
    assert rows[0]["input_hash"] == propose["input_hash"]
    replayed = lab.replay_research_estimate(propose["estimate_id"])
    assert replayed["action"] == propose["action"]
    assert replayed["replay_of"] == propose["estimate_id"]
    assert (replayed.get("forecast") or {}).get("p_yes") == (
        propose.get("forecast") or {}
    ).get("p_yes")
    mismatch_rows = lab.store.list_research_estimates("900005")
    assert mismatch_rows[0]["status"] == "ABSTAIN"
    assert mismatch_rows[0]["reason"] == "resolution_source_mismatch"
    decisions = [d for d in lab.store.list_decisions() if d["market_id"] == "900005"]
    assert any(d["action"] == "ABSTAIN" for d in decisions)


def test_paper_run_dry_does_not_import_or_settle(tmp_path: Path) -> None:
    lab = Lab.open(
        mode="PAPER",
        data_dir=tmp_path / "paper-dry",
        gamma=FixtureGamma(),
        clob=FixtureClob(),
        clock=FrozenClock(NOW),
    )
    summary = run_forward_paper(
        lab,
        PaperRunConfig(import_paper=False, max_cycles=2, interval_seconds=0, as_of=AS_OF),
    )
    assert summary["auto_settle"] is False
    assert summary["edge_proven"] is False
    assert summary["cycle_count"] == 2
    actions = {row["market_id"]: row for row in summary["cycles"][0]["markets"]}
    assert actions["900004"]["action"] == "FORECAST"
    assert actions["900004"]["imported"] is False
    assert actions["900005"]["action"] == "ABSTAIN"
    assert lab.store.open_position_count() == 0
    assert lab.store.list_positions("SETTLED") == []
    assert lab.store.list_paper_runs()


def test_paper_run_import_without_review_refuses_position(tmp_path: Path) -> None:
    lab = Lab.open(
        mode="PAPER",
        data_dir=tmp_path / "paper-refuse",
        gamma=FixtureGamma(),
        clob=FixtureClob(),
        clock=FrozenClock(NOW),
    )
    summary = run_forward_paper(
        lab,
        PaperRunConfig(import_paper=True, as_of=AS_OF, max_markets=10),
    )
    kmia = next(row for row in summary["cycles"][0]["markets"] if row["market_id"] == "900004")
    assert kmia["imported"] is False
    assert kmia["reason"] == "missing_rules_review"
    assert kmia["paper"]["reason"] == "missing_rules_review"
    assert lab.store.open_position_count() == 0


def test_paper_run_import_after_review_still_risk_v2(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    market = lab.store.get_market("900004")
    assert market is not None
    lab.review_rules(
        market_id="900004",
        rules_hash=market.rules_hash,
        cluster_id="kmia-station-date",
        trading_cutoff="2026-09-16T22:00:00+00:00",
        reviewer="tester",
        expected_settlement_source="https://api.weather.gov/stations/KMIA",
        authorize_model_version=WEATHER_MODEL_VERSION,
    )
    summary = run_forward_paper(
        lab,
        PaperRunConfig(
            import_paper=True,
            as_of=AS_OF,
            cluster_id="kmia-station-date",
        ),
    )
    kmia = next(row for row in summary["cycles"][0]["markets"] if row["market_id"] == "900004")
    assert kmia["paper"]["action"] == "BUY"
    assert kmia["imported"] is True
    pos = lab.store.open_position_for_market("900004")
    assert pos is not None
    assert lab.risk.version == "risk-v2"


def test_paper_run_never_auto_settles(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    with pytest.raises(LabError, match="never auto-settle"):
        run_forward_paper(lab, PaperRunConfig(auto_settle=True))


def test_research_call_ceiling_stops_paper_run(tmp_path: Path) -> None:
    budget = ResearchBudget(
        cost_ceiling_usd=D("5"),
        max_calls_per_cycle=1,
    )
    lab = Lab.open(
        mode="PAPER",
        data_dir=tmp_path / "paper-ceiling",
        gamma=FixtureGamma(),
        clob=FixtureClob(),
        clock=FrozenClock(NOW),
        research_budget=budget,
    )
    summary = run_forward_paper(lab, PaperRunConfig(as_of=AS_OF))
    cycle = summary["cycles"][0]
    # One call is consumed by the Gamma list; market forecasts hit the ceiling.
    assert cycle["markets"]
    assert all(row["reason"] == "research_call_ceiling" for row in cycle["markets"])
    assert lab.store.open_position_count() == 0


def test_note_call_ceiling_raises() -> None:
    budget = ResearchBudget(cost_ceiling_usd=D("1"), max_calls_per_cycle=0)
    with pytest.raises(ResearchBudgetError, match="research_call_ceiling"):
        budget.note_call(reason="test")


def test_http_kapu_b_paths(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    with TestClient(create_app(lab)) as client:
        discovered = client.post("/api/discover").json()
        assert "900004" in discovered["weather_like_ids"]
        detail = client.get("/api/markets/900004").json()
        assert detail["rules_hash"]
        assert "daily maximum" in detail["rules_text"]
        reviewed = client.post(
            "/api/rules-review",
            json={
                "market_id": "900004",
                "rules_hash": detail["rules_hash"],
                "cluster_id": "kmia-station-date",
                "trading_cutoff": "2026-09-16T22:00:00+00:00",
                "reviewer": "http-kapu-b",
                "expected_settlement_source": "https://api.weather.gov/stations/KMIA",
                "paper_model_version": WEATHER_MODEL_VERSION,
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "recorded"
        dry = client.post("/api/paper-run", json={"as_of": AS_OF, "max_cycles": 1})
        assert dry.status_code == 200
        body = dry.json()
        assert body["auto_settle"] is False
        assert body["import_paper"] is False
        blocked = client.post("/api/paper-run", json={"auto_settle": True})
        assert blocked.status_code == 400
        archive = client.get("/api/archive?kind=gamma_market").json()
        assert archive["entries"]
        status = client.get("/api/kapu-b/status").json()
        assert status["claimed"] is False
        assert status["edge_proven"] is False
        assert status["hungary"] == "stay PAPER"
        log = client.get("/api/specialist/weather/log?market_id=900004").json()
        estimate_id = log["estimates"][0]["id"]
        replay = client.post(f"/api/specialist/weather/replay/{estimate_id}").json()
        assert replay["replay_of"] == estimate_id


def test_unbounded_loop_refused(tmp_path: Path) -> None:
    lab = _paper_lab(tmp_path)
    with pytest.raises(LabError, match="unbounded loop"):
        run_forward_paper(lab, PaperRunConfig(max_cycles=0))
