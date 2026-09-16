"""CLI: research-lab serve | discover | rules-review | paper-run | weather-forecast.

PAPER/DEMO only. No live/wallet/settle-from-runner flags.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def main() -> None:
    if load_dotenv is not None:
        load_dotenv()
    parser = argparse.ArgumentParser(description="Polymarket Research Lab (PAPER/DEMO)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Start local HTTP API")
    serve.add_argument("--host", default=os.environ.get("LAB_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("LAB_PORT", "8000")))

    weather = sub.add_parser(
        "weather-forecast",
        help="Run the station/date weather baseline (PAPER envelope; no live order)",
    )
    weather.add_argument("--market-id", required=True)
    weather.add_argument("--as-of", default=None, help="Timezone-aware UTC instant")
    weather.add_argument(
        "--import-paper",
        action="store_true",
        help="POST the envelope through risk-v2 paper import (still not live)",
    )
    weather.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    discover = sub.add_parser(
        "discover",
        help="Discover weather-like markets (fixtures default; GET-only if flagged)",
    )
    discover.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )
    discover.add_argument("--limit", type=int, default=None)
    discover.add_argument(
        "--no-ingest",
        action="store_true",
        help="Classify only; still archives the Gamma list payload",
    )

    show_rules = sub.add_parser(
        "show-rules",
        help="Print logged rules text + hash for human review",
    )
    show_rules.add_argument("--market-id", required=True)
    show_rules.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    reviews = sub.add_parser("reviews", help="List ingested markets and review status")
    reviews.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    rules_review = sub.add_parser(
        "rules-review",
        help="Record human rules review (PAPER). Exact logged rules_hash required.",
    )
    rules_review.add_argument("--market-id", required=True)
    rules_review.add_argument(
        "--rules-hash",
        required=True,
        help="Must match the logged market rules_hash exactly",
    )
    rules_review.add_argument("--cluster-id", required=True)
    rules_review.add_argument("--trading-cutoff", required=True)
    rules_review.add_argument("--reviewer", required=True)
    rules_review.add_argument(
        "--expected-settlement-source",
        default="",
        help="Expected resolution/settlement source (URL or named provider)",
    )
    rules_review.add_argument("--expected-resolution", default="")
    rules_review.add_argument("--notes", default="")
    rules_review.add_argument(
        "--authorize-model",
        default="",
        help="PAPER-use model_version flag (not a statistical qualification)",
    )
    rules_review.add_argument(
        "--rounding-mode",
        default="",
        help="Optional human rounding algorithm (half_up | unspecified). "
        "Required with --rounding-increment to clear NOAA unspecified rounding.",
    )
    rules_review.add_argument(
        "--rounding-increment",
        default="",
        help="Optional rounding increment in the contract unit (e.g. 1 or 0.1). "
        "Required with a concrete --rounding-mode.",
    )
    rules_review.add_argument(
        "--rounding-unit",
        default="",
        help="Optional C or F; must match the contract unit when set.",
    )
    rules_review.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    authorize = sub.add_parser(
        "authorize-model",
        help="Authorize a model_version for PAPER use only",
    )
    authorize.add_argument("--model-version", required=True)
    authorize.add_argument("--notes", default="paper-use only")
    authorize.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    paper = sub.add_parser(
        "paper-run",
        help="Forward paper collection cycle (dry default; never auto-settles)",
    )
    paper.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )
    paper.add_argument("--as-of", default=None)
    paper.add_argument(
        "--import-paper",
        action="store_true",
        help="Import PROPOSE envelopes through risk-v2 when review+model allow",
    )
    paper.add_argument("--max-cycles", type=int, default=1)
    paper.add_argument("--interval-seconds", type=float, default=0.0)
    paper.add_argument("--cluster-id", default=None)
    paper.add_argument("--limit", type=int, default=None)
    paper.add_argument("--max-markets", type=int, default=None)

    replay = sub.add_parser(
        "replay-estimate",
        help="Re-run a logged research estimate from archived raw inputs",
    )
    replay.add_argument("--estimate-id", type=int, required=True)
    replay.add_argument(
        "--data-dir",
        default=os.environ.get("LAB_DATA_DIR", "data"),
    )

    args = parser.parse_args()
    if args.cmd == "serve":
        os.environ.setdefault("LAB_MODE", "PAPER")
        os.environ.setdefault("POLYMARKET_DATA_SOURCE", "fixtures")
        os.environ.setdefault("WEATHER_DATA_SOURCE", "fixtures")
        uvicorn.run("research_lab.app:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.cmd == "weather-forecast":
        _weather_forecast(args)
        return
    if args.cmd == "discover":
        _discover(args)
        return
    if args.cmd == "show-rules":
        _show_rules(args)
        return
    if args.cmd == "reviews":
        _reviews(args)
        return
    if args.cmd == "rules-review":
        _rules_review(args)
        return
    if args.cmd == "authorize-model":
        _authorize(args)
        return
    if args.cmd == "paper-run":
        _paper_run(args)
        return
    if args.cmd == "replay-estimate":
        _replay(args)
        return


def _prepare_lab(data_dir: str):
    os.environ.setdefault("LAB_MODE", "PAPER")
    os.environ.setdefault("POLYMARKET_DATA_SOURCE", "fixtures")
    os.environ.setdefault("WEATHER_DATA_SOURCE", "fixtures")
    os.environ["LAB_DATA_DIR"] = str(Path(data_dir))
    from research_lab.app import build_lab_from_env

    return build_lab_from_env()


def _weather_forecast(args: argparse.Namespace) -> None:
    from research_lab.forecast import validate_forecast_dict

    lab = _prepare_lab(args.data_dir)
    if not lab.store.list_markets():
        lab.ingest_markets()
    decision = lab.weather_decision(args.market_id, as_of=args.as_of)
    if args.import_paper and decision.get("action") == "PROPOSE":
        forecast = validate_forecast_dict(decision["forecast"]).payload
        result = lab.import_forecast(forecast)
        decision["imported"] = True
        decision["paper"] = {
            "action": result.action,
            "reason": result.reason,
            "token_side": result.token_side,
            "position_id": result.position_id,
        }
    printable = dict(decision)
    printable.pop("diagnostics", None)
    print(json.dumps(printable, indent=2, sort_keys=True))


def _discover(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    result = lab.discover_weather_markets(limit=args.limit, ingest=not args.no_ingest)
    print(json.dumps(result, indent=2, sort_keys=True))


def _show_rules(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    if lab.store.get_market(args.market_id) is None:
        lab.discover_weather_markets()
    bundle = lab.market_review_bundle(args.market_id)
    print(json.dumps(bundle, indent=2, sort_keys=True, default=str))


def _reviews(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    if not lab.store.list_markets():
        lab.discover_weather_markets()
    print(json.dumps({"markets": lab.pending_reviews()}, indent=2, sort_keys=True))


def _rules_review(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    if lab.store.get_market(args.market_id) is None:
        lab.discover_weather_markets()
    settlement = args.expected_settlement_source or args.expected_resolution
    recorded = lab.review_rules(
        market_id=args.market_id,
        rules_hash=args.rules_hash,
        cluster_id=args.cluster_id,
        trading_cutoff=args.trading_cutoff,
        reviewer=args.reviewer,
        expected_resolution=args.expected_resolution or settlement,
        expected_settlement_source=settlement,
        paper_model_version=args.authorize_model,
        notes=args.notes,
        authorize_model_version=args.authorize_model or None,
        rounding_mode=args.rounding_mode or None,
        rounding_increment=args.rounding_increment or None,
        rounding_unit=args.rounding_unit or None,
    )
    print(json.dumps(recorded, indent=2, sort_keys=True))


def _authorize(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    lab.authorize_model(args.model_version, args.notes)
    print(
        json.dumps(
            {
                "status": "authorized_for_paper_use_only",
                "model_version": args.model_version,
                "note": "This is not a statistical qualification or live promotion.",
            },
            indent=2,
            sort_keys=True,
        )
    )


def _paper_run(args: argparse.Namespace) -> None:
    from research_lab.paper_runner import PaperRunConfig, run_forward_paper

    lab = _prepare_lab(args.data_dir)
    result = run_forward_paper(
        lab,
        PaperRunConfig(
            import_paper=args.import_paper,
            auto_settle=False,
            max_cycles=args.max_cycles,
            interval_seconds=args.interval_seconds,
            cluster_id=args.cluster_id,
            as_of=args.as_of,
            discover_limit=args.limit,
            max_markets=args.max_markets,
        ),
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _replay(args: argparse.Namespace) -> None:
    lab = _prepare_lab(args.data_dir)
    decision = lab.replay_research_estimate(args.estimate_id)
    printable = dict(decision)
    printable.pop("diagnostics", None)
    print(json.dumps(printable, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
