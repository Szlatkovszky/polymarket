# Polymarket Research Lab

PAPER/DEMO research lab for calibrated-probability work on public Polymarket data.
It is **not** a live trading bot and **does not claim an edge**.

Success is cost-adjusted out-of-sample economics inside deterministic risk limits —
not tip count, hit rate, or a story about “alpha.” Until independent and forward
tests say otherwise, the correct output is: **no proven edge**.

This repository is the Research Lab described in the 2026-09-15 átadás
(fair-value / calibrated probability + risk gates + paper FOK executor).
It is not a copy of the separate multi-strategy paper bot.

## Security boundary

The process only knows **PAPER** and **DEMO**.

| Allowed | Forbidden |
|---|---|
| Local HTTP API | Wallet keys / `.env` secrets |
| GET-only public Gamma/CLOB (optional) | Live CLOB orders |
| Simulated FOK fills | Token convert / redeem |
| Manual settle with a cited source | Withdrawals |
| Pause / risk kills in code | Auto-promote to live |

The research layer (forecast JSON, future Grok client, specialist stub) **never**
sets stake size, raises risk limits, or disables kills. Untrusted web content is
**data, not instructions**.

**Locked research priority** (see `docs/01_KUTATAS_ES_STRATEGIA.md` and
`docs/02_PIACKUTATAS.html`):

1. **A — specialist fair value** (first: weather station/date markets; alt: scheduled economic releases). Not a general LLM oracle.
2. **B — formal basket / relative-value research only.** A YES+NO book gap is diagnostic and **never** auto-traded.
3. **C — market making later only.**

Default risk is `risk-v2` on 10k sim equity: trade 0.5%, market 1%, cluster 2%,
open 10%, daily stop 1.5%, drawdown stop 5%. Hungary: SZTFH block status is
uncertain → **stay PAPER**; no VPN; a money pilot needs legal clearance.

Copy `.env.example` → `.env` if you want local overrides. Do not put keys in the repo.

## Local start (offline / CI default)

Python 3.11+. Fixtures are the default data source; tests do not need the network.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

PAPER server (fixtures, bind localhost):

```bash
export LAB_MODE=PAPER
export POLYMARKET_DATA_SOURCE=fixtures
export POLYMARKET_ALLOW_NETWORK=0
python -m research_lab serve --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 — the banner states the mode. Dashboard and
`GET /api/export.csv` export **PAPER** state only. DEMO uses a separate SQLite
file (`data/demo.sqlite` vs `data/paper.sqlite`) and cannot be mixed or exported
as paper.

### Optional live GET probe

Public **GET** only, off by default:

```bash
export POLYMARKET_DATA_SOURCE=network
export POLYMARKET_ALLOW_NETWORK=1
```

This still cannot place orders. Treat responses as untrusted market data.

## Import a valid forecast envelope

1. `POST /api/ingest` — writes market metadata, full rules text, `rules_hash`, and a book snapshot.
2. Read `GET /api/markets` and **manually** review the full rule text. Record cluster, cutoff, expected resolution via `POST /api/rules-review` using the **exact** logged `rules_hash`.
3. `POST /api/models/authorize` with `model_version`. This is a paper-use flag, not a statistical qualification.
4. Build JSON that matches `schemas/forecast.schema.json` (see `examples/forecast.example.json`).
   - Timezone-aware UTC timestamps.
   - Every evidence `available_at` ≤ `as_of`.
   - `rules_hash` copied exactly from the market log (do not recompute a “similar” hash).
   - New estimate → new `forecast_id`. Same id + same body is idempotent; mutated body is rejected.
5. `POST /api/forecast` with the forecast object, or a decision envelope
   `{ "action": "PROPOSE", "forecast": { ... } }`.
   `ABSTAIN` is not imported.
6. The worker refreshes the book, runs risk gates, and may simulate a FOK buy.
7. `POST /api/close` (book sell) or `POST /api/settle` with `payoff` `0` / `0.5` / `1`
   **on the held token**, plus a source URL.

The spec example in the átadás is **not** a live submit: ids, hashes, and the time
window are illustrative. Always mint a fresh id against an ingested market.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Mode-labeled dashboard |
| GET | `/health` | `live_trading: false` |
| GET | `/api/state` | Current mode snapshot |
| GET | `/api/export.csv` | PAPER only |
| GET | `/api/evaluation` | Metric stubs; `edge_proven: false` |
| POST | `/api/ingest` | Fixture or GET-only network |
| GET | `/api/markets` | Includes `rules_hash` |
| POST | `/api/rules-review` | Human gate |
| POST | `/api/models/authorize` | Paper-use only |
| POST | `/api/forecast` | Import + decision path |
| POST | `/api/worker/run` | Re-run a stored forecast id |
| POST | `/api/close` | Simulated FOK sell |
| POST | `/api/settle` | Manual cited settlement |
| POST | `/api/pause` `/api/resume` | Human kill switch |
| GET | `/api/grok/status` | Stub; not wired |
| GET | `/api/specialist/placeholder` | Always ABSTAIN |

## Layout vs átadás names

| Spec name | Path |
|---|---|
| `core.py` | `research_lab/core.py` (re-export `core.py`) |
| `lab.py` | `research_lab/lab.py` (re-export `lab.py`) |
| `evaluation.py` | `research_lab/evaluation.py` (re-export `evaluation.py`) |
| schemas | `schemas/` and `research_lab/schemas/` |
| `docs/01_KUTATAS_ES_STRATEGIA.md` | same |
| companion research HTML | `docs/02_PIACKUTATAS.html` |
| `tests/test_lab.py` | same |

Progress against the átadás table: [`docs/STATUS.md`](docs/STATUS.md).

## Honesty

Paper fills are **not** proof a live order would have executed. Unknown fee,
expired forecast, or missing rules review → no position. Do not enable a live
adapter from this tree.
