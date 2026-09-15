"""Forward PAPER measurement runner (dry / loop-friendly).

Produces weather specialist envelopes and optionally imports them through
risk-v2. Never places live orders. Never auto-settles — payoff still requires
a human-cited source_url.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from research_lab.lab import Lab, LabError
from research_lab.research_budget import ResearchBudgetError
from research_lab.specialist import WEATHER_MODEL_VERSION
from research_lab.timeutil import isoformat_utc

SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class PaperRunConfig:
    import_paper: bool = False
    auto_settle: bool = False
    max_cycles: int = 1
    interval_seconds: float = 0.0
    cluster_id: str | None = None
    as_of: str | None = None
    discover_limit: int | None = None
    ingest: bool = True
    max_markets: int | None = None
    model_version: str = WEATHER_MODEL_VERSION


def run_forward_paper(
    lab: Lab,
    config: PaperRunConfig | None = None,
    *,
    sleep: SleepFn = time.sleep,
) -> dict[str, Any]:
    cfg = config or PaperRunConfig()
    if cfg.auto_settle:
        raise LabError(
            "never auto-settle; human payoff reference required via POST /api/settle"
        )
    if cfg.max_cycles < 1:
        raise LabError("refusing unbounded loop; pass a positive --max-cycles")
    cycles: list[dict[str, Any]] = []
    for index in range(cfg.max_cycles):
        cycles.append(_run_one_cycle(lab, cfg, cycle_index=index))
        if index + 1 < cfg.max_cycles and cfg.interval_seconds > 0:
            sleep(cfg.interval_seconds)
    return {
        "mode": lab.mode,
        "live_trading": False,
        "edge_proven": False,
        "auto_settle": False,
        "import_paper": cfg.import_paper,
        "cycles": cycles,
        "cycle_count": len(cycles),
        "research_budget": lab.research_budget.status(),
        "note": (
            "Forward paper collection only. Settlements stay manual with a cited "
            "source. This is not a live bot and not a proven edge."
        ),
    }


def _run_one_cycle(lab: Lab, cfg: PaperRunConfig, *, cycle_index: int) -> dict[str, Any]:
    lab.research_budget.reset_cycle()
    cycle_id = f"paper-{isoformat_utc(lab.now())}-{cycle_index}-{uuid.uuid4().hex[:8]}"
    created = isoformat_utc(lab.now())
    source = getattr(lab.gamma, "source_name", "unknown")
    network = source == "network"

    try:
        lab.research_budget.note_call(reason="gamma_list", network=network)
        discovery = lab.discover_weather_markets(limit=cfg.discover_limit, ingest=cfg.ingest)
    except ResearchBudgetError as exc:
        lab.store.insert_paper_run(
            {
                "cycle_id": cycle_id,
                "market_id": None,
                "as_of": cfg.as_of,
                "action": "REFUSE",
                "reason": "research_call_ceiling",
                "details": {"error": str(exc)},
                "created_at": created,
            }
        )
        return {
            "cycle_id": cycle_id,
            "action": "REFUSE",
            "reason": "research_call_ceiling",
            "markets": [],
            "discovery": None,
            "error": str(exc),
        }

    lab.store.insert_paper_run(
        {
            "cycle_id": cycle_id,
            "market_id": None,
            "as_of": cfg.as_of,
            "action": "DISCOVER",
            "reason": "weather_like",
            "details": {
                "weather_like_ids": discovery["weather_like_ids"],
                "ingested": discovery["ingested"],
                "source": discovery["source"],
            },
            "created_at": created,
        }
    )

    market_ids = list(discovery["weather_like_ids"])
    if cfg.max_markets is not None:
        market_ids = market_ids[: max(0, cfg.max_markets)]

    rows: list[dict[str, Any]] = []
    for market_id in market_ids:
        rows.append(_process_market(lab, cfg, cycle_id, market_id, network=network))
        interval = lab.research_budget.min_interval_seconds if network else 0.0
        if interval > 0:
            time.sleep(interval)

    return {
        "cycle_id": cycle_id,
        "source": source,
        "discovery": {
            "weather_like_ids": discovery["weather_like_ids"],
            "ingested": discovery["ingested"],
        },
        "markets": rows,
        "auto_settle": False,
        "settled": [],
    }


def _process_market(
    lab: Lab,
    cfg: PaperRunConfig,
    cycle_id: str,
    market_id: str,
    *,
    network: bool,
) -> dict[str, Any]:
    created = isoformat_utc(lab.now())
    as_of = cfg.as_of
    try:
        lab.research_budget.note_call(reason=f"weather_forecast:{market_id}", network=network)
        decision = lab.weather_decision(market_id, as_of=as_of)
    except ResearchBudgetError as exc:
        row = {
            "market_id": market_id,
            "action": "REFUSE",
            "reason": "research_call_ceiling",
            "imported": False,
            "error": str(exc),
        }
        lab.store.insert_paper_run(
            {
                "cycle_id": cycle_id,
                "market_id": market_id,
                "as_of": as_of,
                "action": "REFUSE",
                "reason": "research_call_ceiling",
                "details": row,
                "created_at": created,
            }
        )
        return row
    except LabError as exc:
        row = {
            "market_id": market_id,
            "action": "REFUSE",
            "reason": "lab_error",
            "imported": False,
            "error": str(exc),
        }
        lab.store.insert_paper_run(
            {
                "cycle_id": cycle_id,
                "market_id": market_id,
                "as_of": as_of,
                "action": "REFUSE",
                "reason": "lab_error",
                "details": row,
                "created_at": created,
            }
        )
        return row

    action = str(decision.get("action") or "ABSTAIN")
    reason = str(decision.get("reason") or "")
    paper: dict[str, Any] | None = None
    imported = False
    model_version = str(decision.get("model_version") or cfg.model_version)
    allowed, auth_reason = lab.paper_authorization(market_id, model_version)

    if cfg.cluster_id:
        review = lab.store.get_rules_review(market_id)
        cluster = None if review is None else str(review["cluster_id"])
        if cluster != cfg.cluster_id:
            # Still logged the specialist run; do not import off-cluster markets.
            allowed = False
            if auth_reason == "ok":
                auth_reason = "cluster_filter"

    if action == "ABSTAIN":
        run_action = "ABSTAIN"
        run_reason = reason
    elif not cfg.import_paper:
        run_action = "FORECAST"
        run_reason = "dry_run"
    elif not allowed:
        run_action = "REFUSE"
        run_reason = auth_reason
        if action == "PROPOSE" and decision.get("forecast") and auth_reason in {
            "missing_rules_review",
            "model_not_authorized",
            "model_not_authorized_for_market",
        }:
            result = lab.import_forecast(decision["forecast"])
            paper = {
                "action": result.action,
                "reason": result.reason,
                "token_side": result.token_side,
                "position_id": result.position_id,
            }
            imported = False
            run_reason = result.reason or auth_reason
    else:
        result = lab.import_forecast(decision["forecast"])
        paper = {
            "action": result.action,
            "reason": result.reason,
            "token_side": result.token_side,
            "position_id": result.position_id,
        }
        imported = result.action == "BUY"
        run_action = "IMPORT" if result.action == "BUY" else "REFUSE"
        run_reason = result.reason

    forecast_as_of = None
    if isinstance(decision.get("forecast"), dict):
        forecast_as_of = decision["forecast"].get("as_of")
    row = {
        "market_id": market_id,
        "action": run_action,
        "reason": run_reason,
        "specialist_action": action,
        "specialist_reason": reason,
        "imported": imported,
        "estimate_id": decision.get("estimate_id"),
        "input_hash": decision.get("input_hash"),
        "forecast_id": (decision.get("forecast") or {}).get("forecast_id"),
        "paper": paper,
        "auto_settle": False,
    }
    lab.store.insert_paper_run(
        {
            "cycle_id": cycle_id,
            "market_id": market_id,
            "as_of": as_of or forecast_as_of,
            "action": run_action,
            "reason": run_reason,
            "details": row,
            "created_at": isoformat_utc(lab.now()),
        }
    )
    return row
