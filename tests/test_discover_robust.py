"""Offline discovery robustness. No network."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from research_lab.adapters import (
    GAMMA_PAGE_SIZE,
    AdapterError,
    FixtureClob,
    FixtureGamma,
    NetworkGamma,
)
from research_lab.discovery import discovery_skip_reason
from research_lab.lab import Lab
from research_lab.paper_runner import PaperRunConfig, run_forward_paper
from research_lab.publish_status import PublishError, publish_public_status
from research_lab.timeutil import FrozenClock
from test_lab import NOW


def _closed_paris() -> dict:
    return {
        "id": "4942639",
        "question": "Paris 22°C or below on Sep 27",
        "description": "Highest temperature recorded at the Paris station. closed sample.",
        "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=LFPG",
        "endDate": "2026-09-27T16:00:00Z",
        "slug": "paris-22c-sep-27",
        "closed": True,
        "active": True,
        "acceptingOrders": False,
        "enableOrderBook": True,
        "outcomes": "[\"Yes\", \"No\"]",
        "clobTokenIds": "[\"tok-yes-4942639\", \"tok-no-4942639\"]",
    }


def _missing_book_market() -> dict:
    raw = dict(FixtureGamma().get_market("900006"))
    raw["id"] = "900019"
    raw["clobTokenIds"] = "[\"tok-yes-missing\", \"tok-no-missing\"]"
    raw["acceptingOrders"] = True
    raw["closed"] = False
    raw["active"] = True
    raw["enableOrderBook"] = True
    return raw


class _ListingGamma:
    source_name = "fixtures"

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_markets(self, limit: int = 20, **_kwargs: object) -> list[dict]:
        return [dict(row) for row in self._rows[:limit]]

    def get_market(self, market_id: str) -> dict:
        for row in self._rows:
            if str(row["id"]) == str(market_id):
                return dict(row)
        raise AdapterError(f"fixture market not found: {market_id}")


class _MissingBookClob(FixtureClob):
    def get_book(self, token_id: str) -> dict:
        if "missing" in token_id:
            raise AdapterError(
                "GET https://clob.polymarket.com/book -> 404 "
                '{"error":"No orderbook exists for the requested token id"}'
            )
        return super().get_book(token_id)


def _lab_with(tmp_path: Path, gamma: object, clob: object) -> Lab:
    return Lab.open(
        mode="PAPER",
        data_dir=tmp_path / "paper",
        gamma=gamma,
        clob=clob,
        clock=FrozenClock(NOW),
    )


def test_closed_market_is_counted_and_not_ingested() -> None:
    reason = discovery_skip_reason(_closed_paris())
    assert reason == "closed"
    assert (
        discovery_skip_reason(
            {
                "active": True,
                "closed": False,
                "acceptingOrders": False,
                "enableOrderBook": True,
            }
        )
        == "not_accepting_orders"
    )
    assert (
        discovery_skip_reason(
            {
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": False,
            }
        )
        == "orderbook_disabled"
    )
    assert discovery_skip_reason(FixtureGamma().get_market("900004")) is None


def test_discover_skips_closed_and_missing_book_without_orphan(tmp_path: Path) -> None:
    rows = [_closed_paris(), _missing_book_market(), FixtureGamma().get_market("900004")]
    lab = _lab_with(tmp_path, _ListingGamma(rows), _MissingBookClob())
    result = lab.discover_weather_markets()
    assert result["edge_proven"] is False
    assert result["live_orders"] is False
    assert "900004" in result["ingested"]
    assert "4942639" not in result["ingested"]
    assert "900019" not in result["ingested"]
    assert any(row["market_id"] == "4942639" and row["reason"] == "closed" for row in result["skipped"])
    assert any(row["market_id"] == "900019" and row["reason"] == "no_orderbook" for row in result["skipped"])
    assert result["skipped_counts"]["closed"] >= 1
    assert result["skipped_counts"]["no_orderbook"] >= 1
    assert lab.store.get_market("4942639") is None
    assert lab.store.get_market("900019") is None
    assert lab.store.get_market("900004") is not None
    assert lab.store.conn.execute(
        "SELECT COUNT(*) AS n FROM books WHERE market_id = ?", ("900019",)
    ).fetchone()["n"] == 0
    assert lab.store.conn.execute(
        "SELECT COUNT(*) AS n FROM markets WHERE market_id = ?", ("900019",)
    ).fetchone()["n"] == 0
    assert lab.store.list_raw_archive(market_id="900019") == []
    books = lab.store.list_raw_archive(market_id="900004", kind="clob_book")
    assert len(books) == 2


def test_book_write_failure_rolls_back_market_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = _lab_with(tmp_path, FixtureGamma(), FixtureClob())

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("disk")

    monkeypatch.setattr(lab.store, "insert_book", boom)
    with pytest.raises(RuntimeError, match="disk"):
        lab.discover_weather_markets(market_ids=["900004"])
    assert lab.store.get_market("900004") is None


def test_market_id_ingests_only_that_market(tmp_path: Path) -> None:
    lab = _lab_with(tmp_path, FixtureGamma(), FixtureClob())
    result = lab.discover_weather_markets(market_ids=["nope", "900006", "900004"])
    assert result["ingested"] == ["900006", "900004"]
    assert lab.store.get_market("900001") is None
    assert lab.store.get_market("900006") is not None
    assert any(row["market_id"] == "nope" and row["reason"] == "adapter_error" for row in result["skipped"])


def test_min_event_date_and_city_filters(tmp_path: Path) -> None:
    lab = _lab_with(tmp_path, FixtureGamma(), FixtureClob())
    dated = lab.discover_weather_markets(min_event_date="2026-09-20")
    assert "900001" in dated["ingested"]
    assert "900004" not in dated["ingested"]
    assert dated["skipped_counts"].get("event_date_before_min", 0) >= 1
    assert dated["event_date_floor"] == "explicit"

    lab_city = _lab_with(tmp_path / "city", FixtureGamma(), FixtureClob())
    miami = lab_city.discover_weather_markets(city="miami")
    assert "900004" in miami["ingested"]
    assert "900006" not in miami["ingested"]
    assert miami["skipped_counts"].get("city_mismatch", 0) >= 1

    lab_station = _lab_with(tmp_path / "station", FixtureGamma(), FixtureClob())
    tokyo = lab_station.discover_weather_markets(station="RJTT")
    assert tokyo["ingested"] == ["900006"]


def test_paper_run_continues_after_missing_book(tmp_path: Path) -> None:
    rows = [_missing_book_market(), FixtureGamma().get_market("900004"), _closed_paris()]
    lab = _lab_with(tmp_path, _ListingGamma(rows), _MissingBookClob())
    summary = run_forward_paper(lab, PaperRunConfig(as_of="2026-09-15T12:00:00+00:00"))
    assert summary["edge_proven"] is False
    ids = [row["market_id"] for row in summary["cycles"][0]["markets"]]
    assert "900004" in ids
    assert "900019" not in ids
    assert lab.store.get_market("900019") is None
    assert summary["cycles"][0]["discovery"]["skipped_counts"]["no_orderbook"] >= 1


def test_network_catalog_pages_past_closed_markets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_ALLOW_NETWORK", "1")
    open_market = {
        "id": "nyc-0928",
        "question": "Will the highest temperature in NYC be 75°F on September 28?",
        "description": "NOAA daily maximum at station KLGA.",
        "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=KLGA",
        "endDate": "2026-09-29T04:00:00Z",
        "slug": "nyc-high-sep-28",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "clobTokenIds": "[\"yes-nyc\", \"no-nyc\"]",
    }
    closed_page = []
    for index in range(GAMMA_PAGE_SIZE):
        closed_page.append(
            {
                "id": f"closed-{index}",
                "question": "Will the highest temperature in Paris be 22°C on September 1?",
                "description": "NOAA station LFPG daily maximum.",
                "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=LFPG",
                "endDate": "2026-09-01T12:00:00Z",
                "active": True,
                "closed": True,
                "acceptingOrders": False,
                "enableOrderBook": True,
                "clobTokenIds": "[\"y\", \"n\"]",
            }
        )
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url, *, params, allowed_hosts, timeout):  # type: ignore[no-untyped-def]
        calls.append((url, dict(params or {})))
        if "public-search" in url:
            return {"events": [], "pagination": {"hasMore": False}}
        if url.rstrip("/").endswith("/events"):
            return []
        if url.rstrip("/").endswith("/markets"):
            assert params["active"] == "true"
            assert params["closed"] == "false"
            offset = int(params["offset"])
            if offset == 0:
                return closed_page
            if offset == GAMMA_PAGE_SIZE:
                return [open_market]
            return []
        raise AssertionError(url)

    monkeypatch.setattr("research_lab.adapters._http_get", fake_http_get)
    gamma = NetworkGamma()
    rows = gamma.list_markets(limit=1)
    assert [row["id"] for row in rows] == ["nyc-0928"]
    assert any(row["reason"] == "closed" for row in gamma.last_discovery_skips)
    market_calls = [params for url, params in calls if url.rstrip("/").endswith("/markets")]
    assert [int(params["offset"]) for params in market_calls] == [0, GAMMA_PAGE_SIZE]


def test_network_list_stops_when_budget_hook_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_ALLOW_NETWORK", "1")

    def fake_http_get(url, *, params, allowed_hosts, timeout):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("research_lab.adapters._http_get", fake_http_get)
    rows = NetworkGamma().list_markets(limit=5, on_http=lambda: False)
    assert rows == []


def test_discover_cli_market_id(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LAB_MODE"] = "PAPER"
    env["POLYMARKET_DATA_SOURCE"] = "fixtures"
    env["POLYMARKET_ALLOW_NETWORK"] = "0"
    env["WEATHER_DATA_SOURCE"] = "fixtures"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "research_lab",
            "discover",
            "--market-id",
            "900004",
            "--data-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["ingested"] == ["900004"]
    assert body["edge_proven"] is False
    assert body["live_orders"] is False


def test_publish_status_is_read_only_and_disclaims_edge(tmp_path: Path) -> None:
    lab = _lab_with(tmp_path, FixtureGamma(), FixtureClob())
    lab.ingest_markets()
    db_path = tmp_path / "paper" / "paper.sqlite"
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    docs = tmp_path / "docs"
    result = publish_public_status(db_path=db_path, docs_dir=docs, now=NOW)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == digest
    assert result["edge_proven"] is False
    assert result["live_trading"] is False
    status = json.loads((docs / "status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "PAPER"
    assert status["edge_proven"] is False
    assert status["live_trading"] is False
    assert "PAPER" in status["disclaimer"]
    assert "profitability" in status["disclaimer"].lower() or "no profitability" in status["disclaimer"].lower()
    html = (docs / "index.html").read_text(encoding="utf-8")
    assert "PAPER" in html
    assert "edge_proven" in html
    assert "false" in html
    assert status["cash"] in html
    with pytest.raises(PublishError, match="not found"):
        publish_public_status(db_path=tmp_path / "missing.sqlite", docs_dir=docs / "missing")
