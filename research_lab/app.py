"""Local HTTP API. PAPER/DEMO only. No wallet, no CLOB orders, no redeem."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from research_lab import __version__
from research_lab.adapters import build_adapters
from research_lab.evaluation import (
    cluster_bootstrap,
    mean_brier,
    mean_log_loss,
    net_trade_pnl,
    observations_from_settled_rows,
    ops_cost_adjusted_pnl,
)
from research_lab.forecast import ForecastValidationError, validate_decision_envelope
from research_lab.grok import GrokResearchClient
from research_lab.lab import Lab, LabError
from research_lab.money import D
from research_lab.specialist import PlaceholderSpecialist, ResearchRequest

_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"


class RulesReviewBody(BaseModel):
    market_id: str
    rules_hash: str
    cluster_id: str
    trading_cutoff: str
    reviewer: str
    expected_resolution: str = ""
    notes: str = ""


class AuthorizeModelBody(BaseModel):
    model_version: str
    notes: str = ""


class CloseBody(BaseModel):
    market_id: str


class SettleBody(BaseModel):
    market_id: str
    payoff: str = Field(description="0, 0.5, or 1 on the held token")
    source_url: str
    note: str = ""


class OpsCostBody(BaseModel):
    amount: str
    note: str


class WorkerBody(BaseModel):
    forecast_id: str | None = None


def _mode() -> str:
    mode = os.environ.get("LAB_MODE", "PAPER").strip().upper()
    if mode not in {"PAPER", "DEMO"}:
        raise RuntimeError("LAB_MODE must be PAPER or DEMO")
    return mode


def build_lab_from_env() -> Lab:
    gamma, clob = build_adapters()
    data_dir = Path(os.environ.get("LAB_DATA_DIR", "data"))
    risk_version = os.environ.get("LAB_RISK_VERSION", "risk-v1")
    return Lab.open(
        mode=_mode(),
        data_dir=data_dir,
        risk_version=risk_version,
        gamma=gamma,
        clob=clob,
    )


def create_app(lab: Lab | None = None) -> FastAPI:
    lab_holder: dict[str, Lab] = {}
    grok = GrokResearchClient()
    specialist = PlaceholderSpecialist()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        lab_holder["lab"] = lab or build_lab_from_env()
        yield
        lab_holder.pop("lab", None)

    app = FastAPI(
        title="Polymarket Research Lab",
        version=__version__,
        description="PAPER/DEMO research lab. No live trading.",
        lifespan=lifespan,
    )

    def current() -> Lab:
        try:
            return lab_holder["lab"]
        except KeyError as exc:
            raise HTTPException(503, "lab not started") from exc

    @app.get("/health")
    def health() -> dict[str, Any]:
        inst = current()
        return {
            "ok": True,
            "mode": inst.mode,
            "live_trading": False,
            "version": __version__,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        inst = current()
        snap = inst.snapshot()
        html = _TEMPLATE.read_text(encoding="utf-8")
        return (
            html.replace("{{MODE}}", snap["mode"])
            .replace("{{CASH}}", snap["cash"])
            .replace("{{EQUITY}}", snap["equity"])
            .replace("{{PAUSED}}", "yes" if snap["paused"] else "no")
            .replace("{{RISK}}", snap["risk_version"])
            .replace("{{POSITIONS}}", str(len(snap["positions"])))
            .replace("{{DECISIONS}}", str(len(snap["decisions"])))
            .replace("{{VALUATION}}", "complete" if snap["valuation_complete"] else "incomplete")
        )

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return current().snapshot()

    @app.get("/api/export.csv")
    def export_csv() -> PlainTextResponse:
        inst = current()
        try:
            body = inst.export_csv()
        except LabError as exc:
            raise HTTPException(403, str(exc)) from exc
        return PlainTextResponse(body, media_type="text/csv")

    @app.post("/api/ingest")
    def ingest() -> dict[str, Any]:
        ids = current().ingest_markets()
        return {"ingested": ids, "source": getattr(current().gamma, "source_name", "unknown")}

    @app.get("/api/markets")
    def markets() -> dict[str, Any]:
        inst = current()
        rows = []
        for m in inst.store.list_markets():
            review = inst.store.get_rules_review(m.market_id)
            rows.append(
                {
                    "market_id": m.market_id,
                    "question": m.question,
                    "rules_hash": m.rules_hash,
                    "yes_token_id": m.yes_token_id,
                    "no_token_id": m.no_token_id,
                    "resolution_source": m.resolution_source,
                    "reviewed": review is not None,
                    "cluster_id": None if review is None else review["cluster_id"],
                }
            )
        return {"mode": inst.mode, "markets": rows}

    @app.post("/api/rules-review")
    def rules_review(body: RulesReviewBody) -> dict[str, str]:
        try:
            current().review_rules(**body.model_dump())
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "recorded"}

    @app.post("/api/models/authorize")
    def authorize(body: AuthorizeModelBody) -> dict[str, str]:
        current().authorize_model(body.model_version, body.notes)
        return {
            "status": "authorized_for_paper_use_only",
            "note": "This is not a statistical qualification or live promotion.",
        }

    @app.post("/api/forecast")
    def forecast(payload: dict[str, Any]) -> dict[str, Any]:
        inst = current()
        try:
            if payload.get("action") == "ABSTAIN":
                validate_decision_envelope(payload)
                return {"status": "abstained", "imported": False}
            body = payload["forecast"] if payload.get("action") == "PROPOSE" else payload
            if payload.get("action") == "PROPOSE":
                validate_decision_envelope(payload)
            result = inst.import_forecast(body)
        except ForecastValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except LabError as exc:
            raise HTTPException(409 if "immutable" in str(exc) else 400, str(exc)) from exc
        return {
            "action": result.action,
            "reason": result.reason,
            "token_side": result.token_side,
            "position_id": result.position_id,
            "idempotent": result.idempotent,
            "details": result.details,
        }

    @app.post("/api/worker/run")
    def worker(body: WorkerBody | None = None) -> dict[str, Any]:
        inst = current()
        payload = body or WorkerBody()
        if not payload.forecast_id:
            raise HTTPException(400, "forecast_id required")
        try:
            result = inst.process_forecast(payload.forecast_id)
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"action": result.action, "reason": result.reason, "details": result.details}

    @app.post("/api/close")
    def close(body: CloseBody) -> dict[str, Any]:
        try:
            return current().close_position(body.market_id)
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settle")
    def settle(body: SettleBody) -> dict[str, Any]:
        try:
            return current().settle(
                body.market_id,
                payoff=body.payoff,
                source_url=body.source_url,
                note=body.note,
            )
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/pause")
    def pause() -> dict[str, str]:
        current().pause()
        return {"status": "paused"}

    @app.post("/api/resume")
    def resume() -> dict[str, str]:
        current().resume()
        return {"status": "running"}

    @app.post("/api/ops-cost")
    def ops_cost(body: OpsCostBody) -> dict[str, str]:
        try:
            current().add_ops_cost(body.amount, body.note)
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "recorded"}

    @app.get("/api/evaluation")
    def evaluation() -> dict[str, Any]:
        inst = current()
        settled = [
            {**row, "p_yes": _forecast_p(inst, row.get("forecast_id"))}
            for row in inst.store.list_positions("SETTLED")
        ]
        obs = observations_from_settled_rows(settled)
        ops = sum((D(r["amount"]) for r in inst.store.list_ops_costs()), D(0))
        trade = net_trade_pnl(obs)
        boot = cluster_bootstrap(obs, n_resamples=200)
        return {
            "n_settled": len(obs),
            "net_trade_pnl": str(trade),
            "ops_costs": str(ops),
            "ops_cost_adjusted_pnl": str(ops_cost_adjusted_pnl(trade, ops)),
            "mean_brier": mean_brier(obs),
            "mean_log_loss": mean_log_loss(obs),
            "cluster_bootstrap": boot.as_dict(),
            "edge_proven": False,
        }

    @app.get("/api/grok/status")
    def grok_status() -> dict[str, Any]:
        return grok.status()

    @app.post("/api/grok/propose")
    def grok_propose() -> dict[str, Any]:
        return grok.propose(None)

    @app.get("/api/specialist/placeholder")
    def specialist_status() -> dict[str, Any]:
        inst = current()
        markets = inst.store.list_markets()
        if not markets:
            est = specialist.estimate(
                ResearchRequest(
                    market_id="",
                    condition_id=None,
                    rules_text="",
                    rules_hash="",
                    as_of="",
                    cutoff_at=None,
                    resolution_source=None,
                )
            )
        else:
            m = markets[0]
            est = specialist.estimate(
                ResearchRequest(
                    market_id=m.market_id,
                    condition_id=m.condition_id,
                    rules_text=m.rules_text,
                    rules_hash=m.rules_hash,
                    as_of="",
                    cutoff_at=m.cutoff_at,
                    resolution_source=m.resolution_source,
                )
            )
        return {
            "status": est.status,
            "reason": est.reason,
            "calibration_version": est.calibration_version,
            "p_yes": None if est.p_yes is None else str(est.p_yes),
        }

    return app


def _forecast_p(lab: Lab, forecast_id: str | None) -> str:
    if not forecast_id:
        return "0"
    row = lab.store.get_forecast(forecast_id)
    if row is None:
        return "0"
    return str(row["p_yes"])
