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
from research_lab.discovery import (
    classify_weather_market,
    classify_weather_markets,
    discover_limit_from_env,
    payload_hash,
)
from research_lab.hashing import rules_hash_from_text, sha256_hex
from research_lab.money import D, polymarket_taker_fee, q_cash, q_shares, round_fee_up
from research_lab.research_budget import ResearchBudget
from research_lab.specialist import (
    WEATHER_MODEL_VERSION,
    PlaceholderSpecialist,
    ResearchRequest,
    WeatherStationBaseline,
    research_request_from_dict,
    research_request_to_dict,
)
from research_lab.store import Store
from research_lab.timeutil import Clock, SystemUTCClock, isoformat_utc, parse_utc
from research_lab.weather_pipeline import run_weather_baseline
from research_lab.weather_source import build_weather_source

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
        weather_specialist: WeatherStationBaseline | None = None,
        research_budget: ResearchBudget | None = None,
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
        self.research_budget = research_budget or ResearchBudget.from_env()
        self.placeholder_specialist = PlaceholderSpecialist()
        self.weather_specialist = weather_specialist or WeatherStationBaseline(
            build_weather_source(self.research_budget),
            budget=self.research_budget,
        )
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
        weather_specialist: WeatherStationBaseline | None = None,
        research_budget: ResearchBudget | None = None,
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
            weather_specialist=weather_specialist,
            research_budget=research_budget,
        )

    def now(self) -> datetime:
        return self.clock.now()

    def pause(self) -> None:
        self.store.set_paused(True)

    def resume(self) -> None:
        self.store.set_paused(False)

    def ingest_markets(self, limit: int = 20, *, weather_only: bool = False) -> list[str]:
        raw_markets = self.gamma.list_markets(limit=limit)
        ids: list[str] = []
        captured = isoformat_utc(self.now())
        source = getattr(self.gamma, "source_name", "unknown")
        self._archive_raw(
            kind="gamma_list",
            source=source,
            payload=raw_markets,
            captured_at=captured,
        )
        for raw in raw_markets:
            classified = classify_weather_market(raw)
            if weather_only and not classified.weather_like:
                continue
            ids.append(self._persist_ingested_market(raw, classified, captured))
        return ids

    def discover_weather_markets(
        self, limit: int | None = None, *, ingest: bool = True
    ) -> dict[str, Any]:
        """Classify weather-like Gamma rows. Fixture default; network GET is opt-in."""

        cap = limit if limit is not None else discover_limit_from_env()
        raw_markets = self.gamma.list_markets(limit=cap)
        captured = isoformat_utc(self.now())
        source = getattr(self.gamma, "source_name", "unknown")
        self._archive_raw(
            kind="gamma_list",
            source=source,
            payload=raw_markets,
            captured_at=captured,
        )
        classified = classify_weather_markets(raw_markets)
        ingested: list[str] = []
        if ingest:
            for row in classified:
                if not row.weather_like:
                    continue
                ingested.append(self._persist_ingested_market(row.raw, row, captured))
        return {
            "source": source,
            "network": source == "network",
            "live_orders": False,
            "classified": [
                {
                    "market_id": row.market_id,
                    "weather_like": row.weather_like,
                    "reason": row.reason,
                    "specialist_parse_ok": row.specialist_parse_ok,
                    "specialist_parse_reason": row.specialist_parse_reason,
                    "question": row.question,
                    "rules_hash": row.rules_hash,
                    "resolution_source": row.resolution_source,
                }
                for row in classified
            ],
            "weather_like_ids": [row.market_id for row in classified if row.weather_like],
            "ingested": ingested,
            "captured_at": captured,
            "edge_proven": False,
        }

    def _persist_ingested_market(
        self,
        raw: Mapping[str, Any],
        classified: Any,
        captured: str,
    ) -> str:
        market_id = str(raw["id"])
        rules_text = classified.rules_text if classified is not None else _rules_text(raw)
        rules_hash = (
            classified.rules_hash if classified is not None else rules_hash_from_text(rules_text)
        )
        yes_id, no_id = _token_ids(raw)
        source = getattr(self.gamma, "source_name", "unknown")
        self.store.upsert_market(
            {
                "market_id": market_id,
                "condition_id": raw.get("conditionId") or raw.get("condition_id"),
                "question": raw.get("question"),
                "rules_text": rules_text,
                "rules_hash": rules_hash,
                "yes_token_id": yes_id,
                "no_token_id": no_id,
                "cutoff_at": raw.get("endDate") or raw.get("end_date"),
                "timezone": "UTC",
                "resolution_source": raw.get("resolutionSource") or raw.get("resolution_source"),
                "raw_json": json.dumps(raw, sort_keys=True),
                "ingested_at": captured,
            }
        )
        self._archive_raw(
            kind="gamma_market",
            source=source,
            payload=dict(raw),
            market_id=market_id,
            rules_hash=rules_hash,
            captured_at=captured,
        )
        self._snapshot_books(market_id, yes_id, no_id, captured)
        return market_id

    def _archive_raw(
        self,
        *,
        kind: str,
        source: str,
        payload: Any,
        captured_at: str,
        market_id: str | None = None,
        token_id: str | None = None,
        url: str | None = None,
        rules_hash: str | None = None,
    ) -> int:
        return self.store.insert_raw_archive(
            {
                "kind": kind,
                "source": source,
                "market_id": market_id,
                "token_id": token_id,
                "url": url,
                "payload": payload,
                "payload_hash": payload_hash(payload),
                "rules_hash": rules_hash,
                "captured_at": captured_at,
            }
        )

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
            self._archive_raw(
                kind="clob_book",
                source=getattr(self.clob, "source_name", "unknown"),
                payload=book,
                market_id=market_id,
                token_id=token_id,
                rules_hash=market.rules_hash,
                captured_at=captured_at,
            )
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
        expected_settlement_source: str = "",
        paper_model_version: str = "",
        notes: str = "",
        authorize_model_version: str | None = None,
    ) -> dict[str, Any]:
        market = self.store.get_market(market_id)
        if market is None:
            raise LabError("market not ingested")
        if rules_hash != market.rules_hash:
            raise LabError("rules_hash must match the logged market text exactly")
        parse_utc(trading_cutoff)
        settlement = (expected_settlement_source or expected_resolution or "").strip()
        model_version = (authorize_model_version or paper_model_version or "").strip()
        self.store.insert_rules_review(
            {
                "market_id": market_id,
                "rules_hash": rules_hash,
                "cluster_id": cluster_id,
                "trading_cutoff": isoformat_utc(parse_utc(trading_cutoff)),
                "expected_resolution": expected_resolution or settlement,
                "expected_settlement_source": settlement,
                "paper_model_version": model_version or None,
                "reviewer": reviewer,
                "reviewed_at": isoformat_utc(self.now()),
                "notes": notes,
            }
        )
        if model_version:
            self.authorize_model(
                model_version,
                notes=f"paper-use only; recorded with rules review of {market_id}",
            )
        return {
            "status": "recorded",
            "market_id": market_id,
            "rules_hash": rules_hash,
            "cluster_id": cluster_id,
            "trading_cutoff": isoformat_utc(parse_utc(trading_cutoff)),
            "expected_settlement_source": settlement,
            "paper_model_version": model_version or None,
            "authorized_for_paper_use_only": bool(model_version),
            "note": "PAPER review only. Not a statistical qualification or live promotion.",
        }

    def market_review_bundle(self, market_id: str) -> dict[str, Any]:
        market = self.store.get_market(market_id)
        if market is None:
            raise LabError(f"unknown market {market_id}")
        review = self.store.get_rules_review(market_id)
        return {
            "market_id": market.market_id,
            "question": market.question,
            "rules_text": market.rules_text,
            "rules_hash": market.rules_hash,
            "cutoff_at": market.cutoff_at,
            "resolution_source": market.resolution_source,
            "ingested_at": market.ingested_at,
            "reviewed": review is not None,
            "review": None if review is None else dict(review),
            "authorized_models": self.store.list_authorized_models(),
            "mode": self.mode,
            "live_trading": False,
            "note": (
                "Human must record the exact logged rules_hash, semantic cluster, "
                "trading cutoff, and expected settlement source. Authorizing a "
                "model is a PAPER-use flag, not a qualification."
            ),
        }

    def pending_reviews(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for market in self.store.list_markets():
            review = self.store.get_rules_review(market.market_id)
            out.append(
                {
                    "market_id": market.market_id,
                    "question": market.question,
                    "rules_hash": market.rules_hash,
                    "yes_token_id": market.yes_token_id,
                    "no_token_id": market.no_token_id,
                    "resolution_source": market.resolution_source,
                    "reviewed": review is not None,
                    "cluster_id": None if review is None else review["cluster_id"],
                    "expected_settlement_source": None
                    if review is None
                    else (review["expected_settlement_source"] or review["expected_resolution"]),
                    "paper_model_version": None
                    if review is None
                    else review["paper_model_version"],
                }
            )
        return out

    def authorize_model(self, model_version: str, notes: str = "") -> None:
        """Paper-use flag only — not a statistical qualification."""
        self.store.authorize_model(model_version, isoformat_utc(self.now()), notes)

    def yes_market_mid(self, market_id: str, *, as_of: str) -> tuple[Decimal | None, str | None]:
        """Contemporaneous YES mid from a book snapshot with captured_at <= as_of."""

        market = self.store.get_market(market_id)
        if market is None or not market.yes_token_id:
            return None, None
        as_of_dt = parse_utc(as_of)
        row = self.store.latest_book(market.yes_token_id)
        if row is None:
            return None, None
        captured = parse_utc(row["captured_at"])
        if captured > as_of_dt:
            return None, None
        book = OrderBook.from_clob(json.loads(row["snapshot_json"]), token_id=market.yes_token_id)
        if not book.bids or not book.asks:
            return None, isoformat_utc(captured)
        mid = (book.bids[0].price + book.asks[0].price) / D(2)
        return mid, isoformat_utc(captured)

    def weather_research_request(
        self, market_id: str, *, as_of: str | None = None
    ) -> ResearchRequest:
        market = self.store.get_market(market_id)
        if market is None:
            raise LabError(f"unknown market {market_id}")
        as_of_iso = isoformat_utc(parse_utc(as_of) if as_of else self.now())
        mid, mid_at = self.yes_market_mid(market_id, as_of=as_of_iso)
        hints: dict[str, Any] = {}
        if mid is not None:
            hints["market_mid"] = str(mid)
            hints["market_mid_available_at"] = mid_at
        return ResearchRequest(
            market_id=market.market_id,
            condition_id=market.condition_id,
            rules_text=market.rules_text,
            rules_hash=market.rules_hash,
            as_of=as_of_iso,
            cutoff_at=market.cutoff_at,
            resolution_source=market.resolution_source,
            specialist_hints=hints,
        )

    def weather_decision(
        self,
        market_id: str,
        *,
        as_of: str | None = None,
        expires_hours: float = 3.0,
    ) -> dict[str, Any]:
        """Build a weather baseline envelope. Does not import or trade."""

        request = self.weather_research_request(market_id, as_of=as_of)
        decision = run_weather_baseline(
            request,
            specialist=self.weather_specialist,
            expires_hours=expires_hours,
        )
        request_payload = research_request_to_dict(request)
        input_hash = sha256_hex(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        )
        estimate_id = self.store.insert_research_estimate(
            {
                "market_id": request.market_id,
                "rules_hash": request.rules_hash,
                "as_of": request.as_of,
                "model_version": str(decision.get("model_version") or WEATHER_MODEL_VERSION),
                "calibration_version": str(decision.get("calibration_version") or ""),
                "status": "ESTIMATE" if decision.get("action") == "PROPOSE" else "ABSTAIN",
                "reason": str(decision.get("reason") or ""),
                "variants": decision.get("variants") or {},
                "decision": {
                    "action": decision.get("action"),
                    "reason": decision.get("reason"),
                    "forecast_id": (decision.get("forecast") or {}).get("forecast_id"),
                    "variants": decision.get("variants"),
                },
                "request": request_payload,
                "input_hash": input_hash,
                "created_at": isoformat_utc(self.now()),
            }
        )
        self.store.insert_decision(
            {
                "forecast_id": (decision.get("forecast") or {}).get("forecast_id"),
                "market_id": request.market_id,
                "action": str(decision.get("action") or "ABSTAIN"),
                "reason": str(decision.get("reason") or ""),
                "token_side": None,
                "details": {
                    "logged_specialist": True,
                    "imported": False,
                    "estimate_id": estimate_id,
                    "input_hash": input_hash,
                },
                "decided_at": isoformat_utc(self.now()),
            }
        )
        decision["logged"] = True
        decision["estimate_id"] = estimate_id
        decision["input_hash"] = input_hash
        decision["yes_no_gap_tradeable"] = False
        return decision

    def replay_research_estimate(
        self, estimate_id: int, *, expires_hours: float = 3.0
    ) -> dict[str, Any]:
        """Re-run a logged specialist request. Does not import or trade."""

        row = self.store.get_research_estimate(estimate_id)
        if row is None:
            raise LabError(f"unknown research estimate {estimate_id}")
        payload = row.get("request")
        if not payload:
            raise LabError("research estimate is missing re-runnable request_json")
        request = research_request_from_dict(payload)
        decision = run_weather_baseline(
            request,
            specialist=self.weather_specialist,
            expires_hours=expires_hours,
        )
        decision["replay_of"] = estimate_id
        decision["input_hash"] = row.get("input_hash")
        decision["logged"] = False
        decision["imported"] = False
        decision["edge_proven"] = False
        return decision

    def paper_authorization(
        self, market_id: str, model_version: str
    ) -> tuple[bool, str]:
        """Whether PAPER import is allowed. Positions still go through risk-v2."""

        market = self.store.get_market(market_id)
        if market is None:
            return False, "market_not_ingested"
        review = self.store.get_rules_review(market_id)
        if review is None or review["rules_hash"] != market.rules_hash:
            return False, "missing_rules_review"
        if not self.store.model_authorized(model_version):
            return False, "model_not_authorized"
        pinned = (review["paper_model_version"] or "").strip()
        if pinned and pinned != model_version:
            return False, "model_not_authorized_for_market"
        return True, "ok"

    def kapu_b_status(self) -> dict[str, Any]:
        reviews = self.store.list_rules_reviews()
        estimates = self.store.list_research_estimates(limit=20)
        archive_n = len(self.store.list_raw_archive(limit=500))
        return {
            "gate": "B",
            "claimed": False,
            "edge_proven": False,
            "mode": self.mode,
            "live_trading": False,
            "hungary": "stay PAPER",
            "measurable": [
                "weather_like_discovery_via_get_only_adapters",
                "raw_archive_plus_market_meta_plus_book_snapshots_plus_rules_hash",
                "human_rules_review_hash_cluster_cutoff_settlement_source",
                "paper_only_model_authorize",
                "forecast_abstain_refusal_archive_with_rerunnable_inputs",
                "forward_paper_runner_dry_loop",
            ],
            "missing": [
                "recorded_live_vintages_and_official_daily_max_watcher",
                "fitted_calibration_vintage",
                "locked_forward_sample_and_cost_adjusted_pnl",
                "proven_edge",
                "live_order_wallet_redeem",
            ],
            "counts": {
                "markets": len(self.store.list_markets()),
                "reviews": len(reviews),
                "authorized_models": len(self.store.list_authorized_models()),
                "research_estimates": len(estimates),
                "raw_archive_preview": archive_n,
                "paper_runs_preview": len(self.store.list_paper_runs(limit=20)),
                "open_positions": self.store.open_position_count(),
            },
            "research_budget": self.research_budget.status(),
            "note": (
                "Kapu B scaffolding is a measurement path. No historical P&L here "
                "is evidence of an edge. Stay PAPER while HU eligibility is uncertain."
            ),
        }

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
            "research_budget": self.research_budget.status(),
            "weather_specialist": self.weather_specialist.name,
            "weather_calibration": self.weather_specialist.calibration_version,
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
