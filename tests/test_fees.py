"""Hand-computed taker fees for feeSchedule exponents 1 and 2.

Formula, exponent 1 printed and exponent applied to p×(1−p):
https://docs.polymarket.com/trading/fees
https://docs.polymarket.com/market-data/market-details

    fee = C × rate × (p × (1 − p))^exponent

100 shares, rate 0.05. Maker rebateRate is not subtracted.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_lab.fees import parse_market_fee_schedule
from research_lab.lab import OrderBook, simulate_fok
from research_lab.money import D, polymarket_taker_fee, round_fee_up
from test_lab import _forecast, _lab, _prepare_tradeable

# Exact products before the paper ceiling to 0.000001.
_EXACT = {
    1: {
        "0.05": D("0.2375"),
        "0.28": D("1.008"),
        "0.50": D("1.25"),
        "0.61": D("1.1895"),
        "0.95": D("0.2375"),
    },
    2: {
        "0.05": D("0.01128125"),
        "0.28": D("0.2032128"),
        "0.50": D("0.3125"),
        "0.61": D("0.28298205"),
        "0.95": D("0.01128125"),
    },
}


def _weather_schedule(**overrides: object) -> dict:
    schedule = {
        "exponent": 1,
        "rate": "0.05",
        "takerOnly": True,
        "rebateRate": "0.25",
    }
    schedule.update(overrides)
    return {
        "feesEnabled": True,
        "feeType": "weather_fees",
        "feeSchedule": schedule,
    }


def test_hand_computed_fees_exponent_1_and_2() -> None:
    size = D(100)
    rate = D("0.05")
    for exponent, prices in _EXACT.items():
        for price_s, expected in prices.items():
            got = polymarket_taker_fee(
                size=size, price=D(price_s), fee_rate=rate, exponent=exponent
            )
            assert got == expected, (exponent, price_s, got)
        # Symmetric around 0.50: the published curve, not min(p, 1-p)/p.
        assert prices["0.05"] == prices["0.95"]


def test_fee_rounds_up_and_does_not_credit_rebate() -> None:
    exact = _EXACT[2]["0.05"]
    assert round_fee_up(exact) == D("0.011282")
    gross = _EXACT[1]["0.50"]
    rebated = gross * (D(1) - D("0.25"))
    assert gross == D("1.25")
    assert rebated == D("0.9375")

    book = OrderBook.from_clob(
        {
            "asks": [{"price": "0.50", "size": "100"}],
            "bids": [{"price": "0.49", "size": "100"}],
            "min_order_size": "1",
            "tick_size": "0.01",
        },
        token_id="tok-weather",
    )
    parsed = parse_market_fee_schedule(_weather_schedule())
    assert parsed is not None
    fill = simulate_fok(
        book,
        side="BUY",
        shares=D(100),
        fee_rate=parsed.rate,
        fee_exponent=parsed.exponent,
    )
    assert fill.filled is True
    assert fill.fee == round_fee_up(gross)
    assert fill.fee != round_fee_up(rebated)


def test_fixture_exponent_2_paper_fill_uses_squared_curve(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _prepare_tradeable(lab)
    result = lab.import_forecast(_forecast(lab, "900001", forecast_id="fx-exp2-curve"))
    assert result.action == "BUY"
    pos = lab.store.open_position_for_market("900001")
    assert pos is not None
    exp2 = D(0)
    exp1 = D(0)
    for size, price in ((D(10), D("0.40")), (D(10), D("0.41")), (D(24), D("0.42"))):
        exp2 += polymarket_taker_fee(size=size, price=price, fee_rate=D("0.05"), exponent=2)
        exp1 += polymarket_taker_fee(size=size, price=price, fee_rate=D("0.05"), exponent=1)
    assert D(pos["entry_fee"]) == round_fee_up(exp2)
    assert D(pos["entry_fee"]) < round_fee_up(exp1)
    book = lab.store.latest_book("tok-yes-900001")
    assert book is not None and book["fee_exponent"] == 2


def test_unknown_exponent_refuses_fill() -> None:
    book = OrderBook.from_clob(
        {
            "asks": [{"price": "0.50", "size": "10"}],
            "bids": [{"price": "0.49", "size": "10"}],
            "min_order_size": "1",
            "tick_size": "0.01",
        },
        token_id="tok-unknown-exp",
    )
    assert parse_market_fee_schedule(_weather_schedule(exponent=3)) is None
    fill = simulate_fok(book, side="BUY", shares=D(5), fee_rate=D("0.05"), fee_exponent=3)
    assert fill.filled is False
    assert fill.reason == "unknown_fee"


def _patch_fee(lab, market_id: str, raw_fee: dict) -> None:
    market = lab.store.get_market(market_id)
    assert market is not None
    raw = json.loads(market.raw_json)
    raw.update(raw_fee)
    lab.store.conn.execute(
        "UPDATE markets SET raw_json = ? WHERE market_id = ?",
        (json.dumps(raw), market_id),
    )


def test_weather_fees_pass_fee_gate_other_gates_unchanged(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    _patch_fee(lab, "900001", _weather_schedule())
    lab.authorize_model("weather-station-model-v1-calibration-v1")
    blocked = lab.import_forecast(
        _forecast(lab, "900001", forecast_id="fx-weather-fee-noreview")
    )
    assert blocked.action == "NO_TRADE"
    assert blocked.reason == "missing_rules_review"

    _prepare_tradeable(lab)
    result = lab.import_forecast(
        _forecast(lab, "900001", forecast_id="fx-weather-fee-propose")
    )
    assert result.reason != "unknown_fee"
    assert result.action == "BUY"
    pos = lab.store.open_position_for_market("900001")
    assert pos is not None
    # 20% of YES asks 10+10+200 = 44 shares: 10 @ 0.40, 10 @ 0.41, 24 @ 0.42.
    expected = polymarket_taker_fee(size=D(10), price=D("0.40"), fee_rate=D("0.05"), exponent=1)
    expected += polymarket_taker_fee(size=D(10), price=D("0.41"), fee_rate=D("0.05"), exponent=1)
    expected += polymarket_taker_fee(size=D(24), price=D("0.42"), fee_rate=D("0.05"), exponent=1)
    assert D(pos["entry_fee"]) == round_fee_up(expected)
    assert D(pos["entry_fee"]) > round_fee_up(expected * (D(1) - D("0.25")))
    book = lab.store.latest_book("tok-yes-900001")
    assert book is not None
    assert book["fee_rate"] == "0.05"
    assert book["fee_exponent"] == 1

    _patch_fee(lab, "900002", _weather_schedule(exponent=3))
    _prepare_tradeable(lab, "900002")
    refused = lab.import_forecast(
        _forecast(lab, "900002", forecast_id="fx-weather-fee-exp3")
    )
    assert refused.action == "NO_TRADE"
    assert refused.reason == "unknown_fee"
    assert lab.store.open_position_for_market("900002") is None
