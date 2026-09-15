"""Simulated FOK fill, paper close/settle, and lab orchestration.

Fills are simulated across public book levels. They are not evidence that a
live order would have executed. Cash is Decimal. Fill, cash, and position
commit in one SQLite transaction.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from research_lab.core import (
    AccountView,
    DEFAULT_RISK_VERSION,
    Risk,
    evaluate_entry_gates,
    load_risk,
)
from research_lab.fees import parse_market_fee_rate
from research_lab.forecast import ForecastValidationError, ValidatedForecast, validate_forecast_dict
from research_lab.hashing import rules_hash_from_text
from research_lab.money import D, polymarket_taker_fee, q_cash, q_shares, round_fee_up
from research_lab.store import Store
from research_lab.timeutil import Clock, SystemUTCClock, isoformat_utc, parse_utc

ALLOWED_PAYOFFS = (D("0"), D("0.5"), D("1"))


class LabError(ValueError):
    pass


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    min_order_size: Decimal
    tick_size: Decimal
    raw: dict[str, Any]

    @staticmethod
    def from_clob(payload: Mapping[str, Any], token_id: str | None = None) -> "OrderBook":
        tid = token_id or str(payload.get("asset_id") or "")
        bids = tuple(
            sorted(
                (
                    BookLevel(price=D(level["price"]), size=D(level["size"]))
                    for level in payload.get("bids") or []
                ),
                key=lambda lv: lv.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (
                    BookLevel(price=D(level["price"]), size=D(level["size"]))
                    for level in payload.get("asks") or []
                ),
                key=lambda lv: lv.price,
            )
        )
        return OrderBook(
            token_id=tid,
            bids=bids,
            asks=asks,
            min_order_size=D(payload.get("min_order_size") or "1"),
            tick_size=D(payload.get("tick_size") or "0.01"),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class FokFill:
    filled: bool
    shares: Decimal
    notional: Decimal
    avg_price: Decimal
    fee: Decimal
    levels: tuple[dict[str, str], ...]
    reason: str


def simulate_fok(
    book: OrderBook,
    *,
    side: str,
    shares: Decimal,
    fee_rate: Decimal | None = None,
    fee_bps: int | None = None,
) -> FokFill:
    """Fill-or-kill across book levels. Partial depth → no fill."""

    qty = q_shares(shares)
    if qty <= 0:
        return FokFill(False, D(0), D(0), D(0), D(0), (), "non_positive_size")
    rate = fee_rate
    if rate is None and fee_bps is not None:
        rate = D(fee_bps) / D(10_000)
    if rate is None:
        return FokFill(False, D(0), D(0), D(0), D(0), (), "unknown_fee")
    if qty < book.min_order_size:
        return FokFill(False, D(0), D(0), D(0), D(0), (), "below_min_order_size")

    levels: Sequence[BookLevel]
    if side == "BUY":
        levels = book.asks
    elif side == "SELL":
        levels = book.bids
    else:
        raise LabError(f"invalid side {side}")

    remaining = qty
    notional = D(0)
    fee_exact = D(0)
    used: list[dict[str, str]] = []
    for level in levels:
        if remaining <= 0:
            break
        take = remaining if remaining <= level.size else level.size
        if take <= 0:
            continue
        notional += take * level.price
        fee_exact += polymarket_taker_fee(size=take, price=level.price, fee_rate=rate)
        used.append({"price": str(level.price), "size": str(take)})
        remaining -= take

    if remaining > 0:
        return FokFill(False, D(0), D(0), D(0), D(0), (), "insufficient_depth")

    fee = round_fee_up(fee_exact)
    avg = notional / qty
    return FokFill(
        filled=True,
        shares=qty,
        notional=q_cash(notional),
        avg_price=avg,
        fee=fee,
        levels=tuple(used),
        reason="filled",
    )


def all_in_buy_price(fill: FokFill, per_share_reserve: Decimal) -> Decimal:
    """Conservative unit cost: VWAP + taker fee + research reserves (not double-booked)."""
    if fill.shares <= 0:
        raise LabError("no shares")
    return (fill.notional + fill.fee) / fill.shares + per_share_reserve


def yes_no_cross_book_diagnostic(
    yes_book: OrderBook | None, no_book: OrderBook | None
) -> dict[str, Any]:
    """YES+NO ask sum is a data-quality check. Never an automatic trade."""
    base = {
        "tradeable": False,
        "note": "YES+NO gap is diagnostic only; never auto-traded. Basket RV is strategy B research.",
    }
    if yes_book is None or no_book is None or not yes_book.asks or not no_book.asks:
        return {**base, "status": "incomplete"}
    yes_ask = yes_book.asks[0].price
    no_ask = no_book.asks[0].price
    return {
        **base,
        "status": "ok",
        "yes_best_ask": str(yes_ask),
        "no_best_ask": str(no_ask),
        "sum_asks": str(yes_ask + no_ask),
        "apparent_gap_vs_1": str(D(1) - (yes_ask + no_ask)),
    }


def _parse_book_timestamp(raw: Mapping[str, Any]) -> datetime | None:
    ts = raw.get("timestamp")
    if ts is None or ts == "":
        return None
    text = str(ts).strip()
    if text.isdigit():
        n = int(text)
        seconds = n / 1000 if n > 10_000_000_000 else float(n)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        return parse_utc(text)
    except ValueError:
        return None


def book_age_seconds(now: datetime, captured_at: str, book_raw: Mapping[str, Any]) -> Decimal:
    """Max of receive age and server book time. Unknown server time → extremely stale."""
    recv = D(str((now - parse_utc(captured_at)).total_seconds()))
    server = _parse_book_timestamp(book_raw)
    if server is None:
        return D("1000000000000")
    server_age = D(str((now - server).total_seconds()))
    return recv if recv >= server_age else server_age


def ask_depth(book: OrderBook) -> Decimal:
    total = D(0)
    for level in book.asks:
        total += level.size
    return total


def best_spread(book: OrderBook) -> Decimal | None:
    if not book.bids or not book.asks:
        return None
    return book.asks[0].price - book.bids[0].price


@dataclass
class DecisionRecord:
    action: str
    reason: str
    token_side: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    position_id: int | None = None
    idempotent: bool = False


class Lab:
    """In-process PAPER/DEMO lab. Mode is fixed at construction; DBs never mix."""

    def __init__(
        self,
        *,
        mode: str,
        store: Store,
        risk: Risk,
        gamma: Any,
        clob: Any,
        clock: Clock | None = None,
    ) -> None:
        if mode not in ("PAPER", "DEMO"):
            raise LabError("mode must be PAPER or DEMO — live trading is not implemented")
        if store.mode != mode:
            raise LabError("store mode does not match lab mode")
        self.mode = mode
        self.store = store
        self.risk = risk
        self.gamma = gamma
        self.clob = clob
        self.clock = clock or SystemUTCClock()
        now = isoformat_utc(self.clock.now())
        self.store.init_account(
            cash=risk.starting_cash, risk_version=risk.version, now_iso=now
        )

    @classmethod
    def open(
        cls,
        *,
        mode: str,
        data_dir: Path,
        risk_version: str = DEFAULT_RISK_VERSION,
        gamma: Any,
        clob: Any,
        clock: Clock | None = None,
    ) -> "Lab":
        path = Path(data_dir) / f"{mode.lower()}.sqlite"
        store = Store(path, mode)
        return cls(
            mode=mode,
            store=store,
            risk=load_risk(risk_version),
            gamma=gamma,
            clob=clob,
            clock=clock,
        )

    def now(self) -> datetime:
        return self.clock.now()

    def pause(self) -> None:
        self.store.set_paused(True)

    def resume(self) -> None:
        self.store.set_paused(False)

    def ingest_markets(self, limit: int = 20) -> list[str]:
        raw_markets = self.gamma.list_markets(limit=limit)
        ids: list[str] = []
        captured = isoformat_utc(self.now())
        for raw in raw_markets:
            market_id = str(raw["id"])
            rules_text = _rules_text(raw)
            yes_id, no_id = _token_ids(raw)
            self.store.upsert_market(
                {
                    "market_id": market_id,
                    "condition_id": raw.get("conditionId") or raw.get("condition_id"),
                    "question": raw.get("question"),
                    "rules_text": rules_text,
                    "rules_hash": rules_hash_from_text(rules_text),
                    "yes_token_id": yes_id,
                    "no_token_id": no_id,
                    "cutoff_at": raw.get("endDate") or raw.get("end_date"),
                    "timezone": "UTC",
                    "resolution_source": raw.get("resolutionSource") or raw.get("resolution_source"),
                    "raw_json": json.dumps(raw, sort_keys=True),
                    "ingested_at": captured,
                }
            )
            self._snapshot_books(market_id, yes_id, no_id, captured)
            ids.append(market_id)
        return ids

    def refresh_books(self, market_id: str) -> None:
        market = self.store.get_market(market_id)
        if market is None:
            raise LabError(f"unknown market {market_id}")
        self._snapshot_books(
            market_id,
            market.yes_token_id,
            market.no_token_id,
            isoformat_utc(self.now()),
        )

    def _snapshot_books(
        self,
        market_id: str,
        yes_id: str | None,
        no_id: str | None,
        captured_at: str,
    ) -> None:
        market = self.store.get_market(market_id)
        if market is None:
            return
        for side, token_id in (("YES", yes_id), ("NO", no_id)):
            if not token_id:
                continue
            book = self.clob.get_book(token_id)
            raw_market = json.loads(market.raw_json)
            fee_rate = parse_market_fee_rate(raw_market)
            fee_bps = None if fee_rate is None else int((fee_rate * D(10_000)).to_integral_value())
            meta = {
                "source": getattr(self.clob, "source_name", "unknown"),
                "token_side": side,
                "mode": self.mode,
                "fee_rate": None if fee_rate is None else str(fee_rate),
                "feesEnabled": raw_market.get("feesEnabled"),
            }
            self.store.insert_book(
                market_id=market_id,
                token_id=token_id,
                token_side=side,
                snapshot=book,
                rules_hash=market.rules_hash,
                fee_bps=fee_bps,
                fee_rate=None if fee_rate is None else str(fee_rate),
                meta=meta,
                captured_at=captured_at,
            )

    def review_rules(
        self,
        *,
        market_id: str,
        rules_hash: str,
        cluster_id: str,
        trading_cutoff: str,
        reviewer: str,
        expected_resolution: str = "",
        notes: str = "",
    ) -> None:
        market = self.store.get_market(market_id)
        if market is None:
            raise LabError("market not ingested")
        if rules_hash != market.rules_hash:
            raise LabError("rules_hash must match the logged market text exactly")
        parse_utc(trading_cutoff)
        self.store.insert_rules_review(
            {
                "market_id": market_id,
                "rules_hash": rules_hash,
                "cluster_id": cluster_id,
                "trading_cutoff": isoformat_utc(parse_utc(trading_cutoff)),
                "expected_resolution": expected_resolution,
                "reviewer": reviewer,
                "reviewed_at": isoformat_utc(self.now()),
                "notes": notes,
            }
        )

    def authorize_model(self, model_version: str, notes: str = "") -> None:
        """Paper-use flag only — not a statistical qualification."""
        self.store.authorize_model(model_version, isoformat_utc(self.now()), notes)

    def import_forecast(self, payload: Mapping[str, Any], *, decide: bool = True) -> DecisionRecord:
        forecast = validate_forecast_dict(payload)
        existing = self.store.get_forecast(forecast.forecast_id)
        if existing is not None:
            if existing["content_hash"] == forecast.content_hash:
                return DecisionRecord(
                    action="NO_TRADE",
                    reason="idempotent_replay",
                    details={"forecast_id": forecast.forecast_id},
                    idempotent=True,
                )
            raise LabError(
                "forecast_id is immutable; mutated body rejected. Assign a new id for a new estimate."
            )
        self.store.insert_forecast(
            {
                "forecast_id": forecast.forecast_id,
                "canonical_json": forecast.canonical_json,
                "content_hash": forecast.content_hash,
                "market_id": forecast.market_id,
                "model_version": forecast.model_version,
                "rules_hash": forecast.rules_hash,
                "as_of": forecast.as_of_iso,
                "expires_at": forecast.expires_at_iso,
                "p_yes": str(forecast.p_yes),
                "p_low": str(forecast.p_low),
                "p_high": str(forecast.p_high),
                "imported_at": isoformat_utc(self.now()),
            }
        )
        if not decide:
            return DecisionRecord(action="STORED", reason="stored_pending_worker", details={})
        return self.process_forecast(forecast.forecast_id)

    def process_forecast(self, forecast_id: str) -> DecisionRecord:
        row = self.store.get_forecast(forecast_id)
        if row is None:
            raise LabError("unknown forecast_id")
        forecast = validate_forecast_dict(json.loads(row["canonical_json"]))
        decision = self._decide(forecast)
        self.store.insert_decision(
            {
                "forecast_id": forecast.forecast_id,
                "market_id": forecast.market_id,
                "action": decision.action,
                "reason": decision.reason,
                "token_side": decision.token_side,
                "details": decision.details,
                "decided_at": isoformat_utc(self.now()),
            }
        )
        return decision

    def _decide(self, forecast: ValidatedForecast) -> DecisionRecord:
        now = self.now()
        market = self.store.get_market(forecast.market_id)
        if market is None:
            return DecisionRecord("NO_TRADE", "market_not_ingested")
        try:
            self.refresh_books(forecast.market_id)
        except Exception as exc:  # noqa: BLE001 — adapters may fail; refuse trade
            return DecisionRecord("NO_TRADE", "book_refresh_failed", details={"error": str(exc)})

        if self.store.open_position_for_market(forecast.market_id) is not None:
            return DecisionRecord("NO_TRADE", "existing_open_position")

        review = self.store.get_rules_review(forecast.market_id)
        rules_present = review is not None and review["rules_hash"] == market.rules_hash
        hash_matches = forecast.rules_hash == market.rules_hash
        expired = forecast.expired(now) or parse_utc(forecast.as_of_iso) > now
        if review is not None:
            try:
                if parse_utc(review["trading_cutoff"]) <= now:
                    return DecisionRecord("NO_TRADE", "past_trading_cutoff")
            except ValueError:
                return DecisionRecord("NO_TRADE", "invalid_trading_cutoff")

        cluster_id = str(review["cluster_id"]) if review is not None else ""
        yes_book, yes_fee, yes_book_id, yes_captured = self._load_book(market.yes_token_id)
        no_book, no_fee, no_book_id, no_captured = self._load_book(market.no_token_id)
        fee_known = yes_fee is not None and no_fee is not None
        gap = yes_no_cross_book_diagnostic(yes_book, no_book)
        account = self._account_view(cluster_id)
        gates = evaluate_entry_gates(
            self.risk,
            account,
            fee_known=fee_known,
            forecast_expired=expired,
            rules_review_present=bool(rules_present),
            model_authorized=self.store.model_authorized(forecast.model_version),
            rules_hash_matches=hash_matches,
        )
        if not gates.allowed:
            return DecisionRecord("NO_TRADE", gates.reason, details={"yes_no_gap": gap})

        forecast_age_h = (now - parse_utc(forecast.as_of_iso)).total_seconds() / 3600
        if forecast_age_h > self.risk.max_forecast_age_hours:
            return DecisionRecord(
                "NO_TRADE",
                "forecast_too_old",
                details={"forecast_age_hours": forecast_age_h, "yes_no_gap": gap},
            )
        horizon_src = market.cutoff_at
        if review is not None and review["trading_cutoff"]:
            horizon_src = review["trading_cutoff"]
        if horizon_src:
            try:
                horizon = parse_utc(horizon_src) - now
                if horizon > timedelta(days=self.risk.max_settlement_days):
                    return DecisionRecord(
                        "NO_TRADE",
                        "settlement_horizon",
                        details={"horizon_days": str(horizon.days), "yes_no_gap": gap},
                    )
            except ValueError:
                return DecisionRecord("NO_TRADE", "invalid_settlement_horizon", details={"yes_no_gap": gap})

        yes_stale = (
            yes_book is None
            or yes_captured is None
            or book_age_seconds(now, yes_captured, yes_book.raw) > self.risk.max_book_age_seconds
        )
        no_stale = (
            no_book is None
            or no_captured is None
            or book_age_seconds(now, no_captured, no_book.raw) > self.risk.max_book_age_seconds
        )
        if yes_stale or no_stale:
            return DecisionRecord("NO_TRADE", "stale_book", details={"yes_no_gap": gap})

        reserve = self.risk.per_share_research_reserve()
        yes_buy = self._sized_buy(yes_book, yes_fee) if yes_book is not None else FokFill(
            False, D(0), D(0), D(0), D(0), (), "missing_book"
        )
        no_buy = self._sized_buy(no_book, no_fee) if no_book is not None else FokFill(
            False, D(0), D(0), D(0), D(0), (), "missing_book"
        )

        def side_ok(book: OrderBook | None, fill: FokFill) -> str | None:
            if book is None:
                return "missing_book"
            if not fill.filled:
                return fill.reason
            spread = best_spread(book)
            if spread is None:
                return "incomplete_book"
            if spread > self.risk.max_spread:
                return "wide_spread"
            ask = book.asks[0].price
            if ask < self.risk.price_band_low or ask > self.risk.price_band_high:
                return "price_band"
            return None

        yes_block = side_ok(yes_book, yes_buy)
        no_block = side_ok(no_book, no_buy)
        yes_all_in = (
            all_in_buy_price(yes_buy, reserve) if yes_buy.filled and yes_block is None else None
        )
        no_all_in = (
            all_in_buy_price(no_buy, reserve) if no_buy.filled and no_block is None else None
        )

        buy_yes_edge = (forecast.p_low - yes_all_in) if yes_all_in is not None else D("-1")
        buy_no_edge = ((D(1) - forecast.p_high) - no_all_in) if no_all_in is not None else D("-1")

        choice: tuple[str, FokFill, int | None, Decimal] | None = None
        if (
            yes_all_in is not None
            and buy_yes_edge >= self.risk.min_conservative_edge
            and buy_yes_edge >= buy_no_edge
        ):
            choice = ("YES", yes_buy, yes_book_id, buy_yes_edge)
        elif no_all_in is not None and buy_no_edge >= self.risk.min_conservative_edge:
            choice = ("NO", no_buy, no_book_id, buy_no_edge)

        if choice is None:
            return DecisionRecord(
                "NO_TRADE",
                "no_conservative_edge",
                details={
                    "buy_yes_edge": str(buy_yes_edge),
                    "buy_no_edge": str(buy_no_edge),
                    "yes_fill": yes_buy.reason if yes_block is None else yes_block,
                    "no_fill": no_buy.reason if no_block is None else no_block,
                    "yes_no_gap": gap,
                },
            )

        token_side, fill, book_id, edge = choice
        token_id = market.yes_token_id if token_side == "YES" else market.no_token_id
        cash_need = fill.notional + fill.fee
        if cash_need > account.cash:
            return DecisionRecord("NO_TRADE", "insufficient_cash", details={"yes_no_gap": gap})
        if cash_need > self.risk.max_trade_cost:
            return DecisionRecord("NO_TRADE", "max_trade_cost", details={"yes_no_gap": gap})
        if cash_need > self.risk.max_market_cost:
            return DecisionRecord("NO_TRADE", "max_market_cost", details={"yes_no_gap": gap})
        projected_cluster = account.cluster_notional + fill.notional
        if projected_cluster > self.risk.max_cluster_notional:
            return DecisionRecord("NO_TRADE", "cluster_exposure", details={"yes_no_gap": gap})
        if account.open_entry_cost + cash_need > self.risk.max_open_entry_cost:
            return DecisionRecord("NO_TRADE", "open_exposure", details={"yes_no_gap": gap})

        try:
            self.store.begin()
            position_id = self.store.insert_position(
                {
                    "market_id": forecast.market_id,
                    "forecast_id": forecast.forecast_id,
                    "token_side": token_side,
                    "token_id": token_id,
                    "shares": str(fill.shares),
                    "avg_price": str(fill.avg_price),
                    "entry_fee": str(fill.fee),
                    "cluster_id": cluster_id,
                    "opened_at": isoformat_utc(now),
                }
            )
            self.store.insert_fill(
                {
                    "position_id": position_id,
                    "forecast_id": forecast.forecast_id,
                    "market_id": forecast.market_id,
                    "side": "BUY",
                    "token_side": token_side,
                    "token_id": token_id,
                    "shares": str(fill.shares),
                    "avg_price": str(fill.avg_price),
                    "fee": str(fill.fee),
                    "notional": str(fill.notional),
                    "book_id": book_id,
                    "filled_at": isoformat_utc(now),
                }
            )
            self.store.add_cash(-cash_need)
            self.store.insert_ledger(
                kind="FILL_DEBIT",
                amount=-cash_need,
                ref=str(position_id),
                note=f"paper FOK buy {token_side}",
                created_at=isoformat_utc(now),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

        return DecisionRecord(
            action="BUY",
            reason="paper_fok_fill",
            token_side=token_side,
            position_id=position_id,
            details={
                "shares": str(fill.shares),
                "avg_price": str(fill.avg_price),
                "fee": str(fill.fee),
                "notional": str(fill.notional),
                "edge": str(edge),
                "levels": list(fill.levels),
                "simulated": True,
                "live_order": False,
                "strategy": "A_specialist_fair_value",
                "yes_no_gap": gap,
            },
        )

    def close_position(self, market_id: str) -> dict[str, Any]:
        pos = self.store.open_position_for_market(market_id)
        if pos is None:
            raise LabError("no open position")
        self.refresh_books(market_id)
        book, fee_rate, book_id, _captured = self._load_book(pos["token_id"])
        if book is None:
            raise LabError("missing_book")
        fill = simulate_fok(book, side="SELL", shares=D(pos["shares"]), fee_rate=fee_rate)
        if not fill.filled:
            raise LabError(f"close_fok_failed:{fill.reason}")
        proceeds = fill.notional - fill.fee
        now = isoformat_utc(self.now())
        try:
            self.store.begin()
            self.store.insert_fill(
                {
                    "position_id": pos["id"],
                    "forecast_id": pos["forecast_id"],
                    "market_id": market_id,
                    "side": "SELL",
                    "token_side": pos["token_side"],
                    "token_id": pos["token_id"],
                    "shares": str(fill.shares),
                    "avg_price": str(fill.avg_price),
                    "fee": str(fill.fee),
                    "notional": str(fill.notional),
                    "book_id": book_id,
                    "filled_at": now,
                }
            )
            self.store.add_cash(proceeds)
            self.store.insert_ledger(
                kind="FILL_CREDIT",
                amount=proceeds,
                ref=str(pos["id"]),
                note="paper FOK close",
                created_at=now,
            )
            self.store.close_position(int(pos["id"]), status="CLOSED", closed_at=now)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {
            "status": "CLOSED",
            "proceeds": str(q_cash(proceeds)),
            "fee": str(fill.fee),
            "avg_price": str(fill.avg_price),
            "simulated": True,
        }

    def settle(
        self,
        market_id: str,
        *,
        payoff: Decimal | str | int | float,
        source_url: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Manual settlement. Payoff is on the held token, not automatically YES."""

        pos = self.store.open_position_for_market(market_id)
        if pos is None:
            raise LabError("no open position")
        if not source_url:
            raise LabError("settlement requires a cited source_url")
        pay = D(payoff)
        if pay not in ALLOWED_PAYOFFS:
            raise LabError("payoff must be 0, 0.5, or 1 on the held token")
        shares = D(pos["shares"])
        credit = q_cash(shares * pay)
        now = isoformat_utc(self.now())
        try:
            self.store.begin()
            self.store.add_cash(credit)
            self.store.insert_ledger(
                kind="SETTLE",
                amount=credit,
                ref=str(pos["id"]),
                note=f"manual settle payoff={pay} source={source_url} {note}".strip(),
                created_at=now,
            )
            self.store.close_position(
                int(pos["id"]),
                status="SETTLED",
                closed_at=now,
                settle_payoff=str(pay),
                settle_source=source_url,
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {
            "status": "SETTLED",
            "token_side": pos["token_side"],
            "payoff_held_token": str(pay),
            "credit": str(credit),
            "source_url": source_url,
        }

    def add_ops_cost(self, amount: Decimal | str, note: str) -> None:
        amt = D(amount)
        if amt < 0:
            raise LabError("ops cost must be >= 0")
        now = isoformat_utc(self.now())
        self.store.insert_ops_cost(amt, note, now)
        # Ops ledger is recorded separately from trade cash so we do not double-count
        # the per-trade reserve that was only used for entry gating.

    def _sized_buy(self, book: OrderBook, fee_rate: Decimal | None) -> FokFill:
        depth = ask_depth(book)
        cap = q_shares(depth * self.risk.max_depth_fraction)
        if cap < book.min_order_size or cap < self.risk.min_fill_shares:
            return FokFill(False, D(0), D(0), D(0), D(0), (), "depth_participation")
        fill = simulate_fok(book, side="BUY", shares=cap, fee_rate=fee_rate)
        if not fill.filled:
            return fill
        cash_need = fill.notional + fill.fee
        if cash_need <= self.risk.max_trade_cost and cash_need <= self.risk.max_market_cost:
            return fill
        # Shrink by walking budget at best ask as a conservative upper bound.
        if not book.asks:
            return FokFill(False, D(0), D(0), D(0), D(0), (), "missing_book")
        worst = book.asks[-1].price if book.asks else D(1)
        reserve = self.risk.per_share_research_reserve()
        max_by_cost = self.risk.max_trade_cost / (worst + reserve + D("0.000001"))
        sized = q_shares(min(cap, max_by_cost))
        if sized < book.min_order_size:
            return FokFill(False, D(0), D(0), D(0), D(0), (), "max_trade_cost")
        return simulate_fok(book, side="BUY", shares=sized, fee_rate=fee_rate)

    def _load_book(
        self, token_id: str | None
    ) -> tuple[OrderBook | None, Decimal | None, int | None, str | None]:
        if not token_id:
            return None, None, None, None
        row = self.store.latest_book(token_id)
        if row is None:
            return None, None, None, None
        book = OrderBook.from_clob(json.loads(row["snapshot_json"]), token_id=token_id)
        rate_s = row["fee_rate"] if "fee_rate" in row.keys() else None
        fee_rate = D(rate_s) if rate_s not in (None, "") else None
        return book, fee_rate, int(row["id"]), str(row["captured_at"])

    def mark_open_positions(self) -> tuple[Decimal, bool]:
        """Exit-depth mark of OPEN inventory. Incomplete marks block new entries."""

        cash = self.store.cash()
        marked = D(0)
        complete = True
        for pos in self.store.list_positions("OPEN"):
            book, fee_rate, _, _ = self._load_book(pos["token_id"])
            if book is None:
                complete = False
                continue
            fill = simulate_fok(book, side="SELL", shares=D(pos["shares"]), fee_rate=fee_rate)
            if not fill.filled:
                complete = False
                continue
            marked += fill.notional - fill.fee
        equity = q_cash(cash + marked)
        if equity > self.store.peak_equity():
            self.store.set_peak_equity(equity)
        day = self.now().date().isoformat()
        self.store.upsert_day_mark(day, equity)
        return equity, complete

    def _account_view(self, cluster_id: str) -> AccountView:
        equity, complete = self.mark_open_positions()
        day = self.now().date().isoformat()
        start = self.store.upsert_day_mark(day, equity)
        return AccountView(
            cash=self.store.cash(),
            equity=equity,
            peak_equity=self.store.peak_equity(),
            start_of_day_equity=start,
            open_position_count=self.store.open_position_count(),
            cluster_notional=self.store.cluster_open_notional(cluster_id) if cluster_id else D(0),
            paused=self.store.is_paused(),
            valuation_complete=complete,
            entries_today=self.store.buy_fills_on_utc_day(self.now().date().isoformat()),
            open_entry_cost=self.store.open_entry_cost(),
        )

    def snapshot(self) -> dict[str, Any]:
        equity, complete = self.mark_open_positions()
        acc = self.store.account()
        return {
            "mode": self.mode,
            "live_trading": False,
            "edge_proven": False,
            "disclaimer": (
                "PAPER/DEMO simulation only. Fills are not live orders. "
                "No profitability is claimed. Success metrics are cost-adjusted "
                "out-of-sample economics and risk-limit adherence."
            ),
            "risk_version": acc["risk_version"],
            "strategy_lock": "A_specialist_fair_value",
            "strategy_note": (
                "Primary A: specialist fair value (weather station/date first; "
                "alt economic releases). Not a general LLM oracle. "
                "B basket/RV research only — YES+NO gap is diagnostic, never auto-trade. "
                "C market making later only."
            ),
            "legal_note": (
                "HU SZTFH block status uncertain → stay PAPER. No VPN. "
                "Money pilot needs separate legal clearance."
            ),
            "paused": bool(acc["paused"]),
            "cash": str(self.store.cash()),
            "equity": str(equity),
            "peak_equity": str(self.store.peak_equity()),
            "valuation_complete": complete,
            "markets": [
                {
                    "market_id": m.market_id,
                    "question": m.question,
                    "rules_hash": m.rules_hash,
                    "reviewed": self.store.get_rules_review(m.market_id) is not None,
                }
                for m in self.store.list_markets()
            ],
            "positions": self.store.list_positions(),
            "decisions": self.store.list_decisions(),
            "fills": self.store.list_fills(),
            "ops_costs": self.store.list_ops_costs(),
        }

    def export_csv(self) -> str:
        if self.mode != "PAPER":
            raise LabError("CSV export is PAPER state only; DEMO data is not exported")
        snap = self.snapshot()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["section", "field", "value"])
        writer.writerow(["account", "mode", snap["mode"]])
        writer.writerow(["account", "cash", snap["cash"]])
        writer.writerow(["account", "equity", snap["equity"]])
        writer.writerow(["account", "edge_proven", "false"])
        writer.writerow([])
        writer.writerow(
            [
                "position_id",
                "market_id",
                "status",
                "token_side",
                "shares",
                "avg_price",
                "entry_fee",
                "forecast_id",
                "settle_payoff",
            ]
        )
        for pos in snap["positions"]:
            writer.writerow(
                [
                    pos["id"],
                    pos["market_id"],
                    pos["status"],
                    pos["token_side"],
                    pos["shares"],
                    pos["avg_price"],
                    pos["entry_fee"],
                    pos["forecast_id"],
                    pos.get("settle_payoff") or "",
                ]
            )
        return buf.getvalue()


def _token_ids(raw: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids")
    ids: list[str]
    if isinstance(raw_ids, str):
        parsed = json.loads(raw_ids)
        ids = [str(x) for x in parsed]
    elif isinstance(raw_ids, list):
        ids = [str(x) for x in raw_ids]
    else:
        ids = []
    yes = ids[0] if len(ids) > 0 else None
    no = ids[1] if len(ids) > 1 else None
    return yes, no


def _rules_text(raw: Mapping[str, Any]) -> str:
    parts = [
        str(raw.get("question") or ""),
        str(raw.get("description") or ""),
        str(raw.get("resolutionSource") or raw.get("resolution_source") or ""),
        str(raw.get("endDate") or raw.get("end_date") or ""),
    ]
    return "\n".join(parts)
