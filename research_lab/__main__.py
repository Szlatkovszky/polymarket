"""CLI: research-lab serve | does not accept live/wallet flags."""

from __future__ import annotations

import argparse
import os

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
    args = parser.parse_args()
    if args.cmd == "serve":
        os.environ.setdefault("LAB_MODE", "PAPER")
        os.environ.setdefault("POLYMARKET_DATA_SOURCE", "fixtures")
        uvicorn.run("research_lab.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
