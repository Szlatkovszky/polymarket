"""CLI: research-lab serve | weather-forecast. No live/wallet flags."""

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

    args = parser.parse_args()
    if args.cmd == "serve":
        os.environ.setdefault("LAB_MODE", "PAPER")
        os.environ.setdefault("POLYMARKET_DATA_SOURCE", "fixtures")
        os.environ.setdefault("WEATHER_DATA_SOURCE", "fixtures")
        uvicorn.run("research_lab.app:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.cmd == "weather-forecast":
        _weather_forecast(args)


def _weather_forecast(args: argparse.Namespace) -> None:
    os.environ.setdefault("LAB_MODE", "PAPER")
    os.environ.setdefault("POLYMARKET_DATA_SOURCE", "fixtures")
    os.environ.setdefault("WEATHER_DATA_SOURCE", "fixtures")
    from research_lab.app import build_lab_from_env
    from research_lab.forecast import validate_forecast_dict

    # build_lab_from_env reads LAB_DATA_DIR
    os.environ["LAB_DATA_DIR"] = str(Path(args.data_dir))
    lab = build_lab_from_env()
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
    # Drop bulky diagnostics from stdout; they remain in SQLite research_estimates.
    printable = dict(decision)
    printable.pop("diagnostics", None)
    print(json.dumps(printable, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
