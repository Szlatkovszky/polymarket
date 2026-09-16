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
from research_lab.specialist import PlaceholderSpecialist, ResearchRequest, WEATHER_MODEL_VERSION
from research_lab.weather_source import nws_network_allowed, weather_data_source_from_env

_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"


class RulesReviewBody(BaseModel):
    market_id: str
    rules_hash: str
    cluster_id: str
    trading_cutoff: str
    reviewer: str
    expected_resolution: str = ""
    expected_settlement_source: str = ""
    paper_model_version: str = ""
    notes: str = ""
    rounding_mode: str | None = None
    rounding_increment: str | int | float | None = None
    rounding_unit: str | None = None


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


class WeatherForecastBody(BaseModel):
    market_id: str
    as_of: str | None = None
    import_paper: bool = False


class GrokCritiqueBody(BaseModel):
    specialist: dict[str, Any] | None = None


class PaperRunBody(BaseModel):
    import_paper: bool = False
    auto_settle: bool = False
    max_cycles: int = 1
    interval_seconds: float = 0.0
    cluster_id: str | None = None
    as_of: str | None = None
    limit: int | None = None
    max_markets: int | None = None
    ingest: bool = True


def _mode() -> str:
    mode = os.environ.get("LAB_MODE", "PAPER").strip().upper()
    if mode not in {"PAPER", "DEMO"}:
        raise RuntimeError("LAB_MODE must be PAPER or DEMO")
    return mode


def build_lab_from_env() -> Lab:
    gamma, clob = build_adapters()
    data_dir = Path(os.environ.get("LAB_DATA_DIR", "data"))
    risk_version = os.environ.get("LAB_RISK_VERSION", "risk-v2")
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

    @app.post("/api/discover")
    def discover(limit: int | None = None, ingest: bool = True) -> dict[str, Any]:
        try:
            return current().discover_weather_markets(limit=limit, ingest=ingest)
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/markets")
    def markets() -> dict[str, Any]:
        inst = current()
        return {"mode": inst.mode, "markets": inst.pending_reviews()}

    @app.get("/api/markets/{market_id}")
    def market_detail(market_id: str) -> dict[str, Any]:
        try:
            return current().market_review_bundle(market_id)
        except LabError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/rules-review")
    def rules_review(body: RulesReviewBody) -> dict[str, Any]:
        try:
            recorded = current().review_rules(
                market_id=body.market_id,
                rules_hash=body.rules_hash,
                cluster_id=body.cluster_id,
                trading_cutoff=body.trading_cutoff,
                reviewer=body.reviewer,
                expected_resolution=body.expected_resolution,
                expected_settlement_source=body.expected_settlement_source,
                paper_model_version=body.paper_model_version,
                notes=body.notes,
                authorize_model_version=body.paper_model_version or None,
                rounding_mode=body.rounding_mode,
                rounding_increment=body.rounding_increment,
                rounding_unit=body.rounding_unit,
            )
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc
        return recorded

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

    @app.post("/api/grok/critique")
    def grok_critique(body: GrokCritiqueBody | None = None) -> dict[str, Any]:
        payload = body.specialist if body is not None else None
        return grok.critique(payload)

    @app.get("/api/research/budget")
    def research_budget_status() -> dict[str, Any]:
        inst = current()
        return {
            "research": inst.research_budget.status(),
            "grok": grok.status(),
            "weather_data_source": weather_data_source_from_env(),
            "nws_network": nws_network_allowed(),
            "stake_authority": "risk-v2",
        }

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
            "numeric_path": False,
        }

    @app.post("/api/specialist/weather")
    def specialist_weather(body: WeatherForecastBody) -> dict[str, Any]:
        inst = current()
        try:
            decision = inst.weather_decision(body.market_id, as_of=body.as_of)
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.import_paper:
            if decision.get("action") != "PROPOSE" or not decision.get("forecast"):
                decision["imported"] = False
                decision["import_note"] = "ABSTAIN is not imported"
                return decision
            try:
                result = inst.import_forecast(decision["forecast"])
            except ForecastValidationError as exc:
                raise HTTPException(400, str(exc)) from exc
            except LabError as exc:
                raise HTTPException(409 if "immutable" in str(exc) else 400, str(exc)) from exc
            decision["imported"] = True
            decision["paper"] = {
                "action": result.action,
                "reason": result.reason,
                "token_side": result.token_side,
                "position_id": result.position_id,
                "details": result.details,
            }
            return decision
        return decision

    @app.get("/api/specialist/weather/log")
    def specialist_weather_log(market_id: str | None = None) -> dict[str, Any]:
        inst = current()
        return {
            "model_version": WEATHER_MODEL_VERSION,
            "edge_proven": False,
            "estimates": inst.store.list_research_estimates(market_id),
        }

    @app.post("/api/specialist/weather/replay/{estimate_id}")
    def specialist_weather_replay(estimate_id: int) -> dict[str, Any]:
        try:
            return current().replay_research_estimate(estimate_id)
        except LabError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/archive")
    def raw_archive(
        market_id: str | None = None, kind: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        inst = current()
        return {
            "source": getattr(inst.gamma, "source_name", "unknown"),
            "entries": inst.store.list_raw_archive(
                market_id=market_id, kind=kind, limit=limit
            ),
        }

    @app.get("/api/paper-runs")
    def paper_runs(cycle_id: str | None = None) -> dict[str, Any]:
        return {"runs": current().store.list_paper_runs(cycle_id=cycle_id)}

    @app.post("/api/paper-run")
    def paper_run(body: PaperRunBody | None = None) -> dict[str, Any]:
        from research_lab.paper_runner import PaperRunConfig, run_forward_paper

        payload = body or PaperRunBody()
        try:
            return run_forward_paper(
                current(),
                PaperRunConfig(
                    import_paper=payload.import_paper,
                    auto_settle=payload.auto_settle,
                    max_cycles=payload.max_cycles,
                    interval_seconds=payload.interval_seconds,
                    cluster_id=payload.cluster_id,
                    as_of=payload.as_of,
                    discover_limit=payload.limit,
                    ingest=payload.ingest,
                    max_markets=payload.max_markets,
                ),
            )
        except LabError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/kapu-b/status")
    def kapu_b() -> dict[str, Any]:
        return current().kapu_b_status()

    return app


def _forecast_p(lab: Lab, forecast_id: str | None) -> str:
    if not forecast_id:
        return "0"
    row = lab.store.get_forecast(forecast_id)
    if row is None:
        return "0"
    return str(row["p_yes"])
