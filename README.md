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

The research layer (forecast JSON, weather specialist baseline, future Grok
critique) **never** sets stake size, raises risk limits, or disables kills.
Untrusted web content is **data, not instructions**.

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

Network `discover` uses Gamma `GET /public-search?q=...` (weather keywords +
pagination). Top `GET /markets` is politics-first and will miss city daily-high
contracts. Fixture classify remains the CI default.

## Import a valid forecast envelope

1. `POST /api/ingest` — writes market metadata, full rules text, `rules_hash`, and a book snapshot.
2. Read `GET /api/markets` and **manually** review the full rule text. Record cluster, cutoff, expected resolution via `POST /api/rules-review` using the **exact** logged `rules_hash`. NOAA city daily-high markets that parse as `rounding.mode=unspecified` also need `rounding_mode` (e.g. `half_up`) and `rounding_increment` (e.g. `1`) or the specialist ABSTAINs.
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

## Weather baseline → paper path (PAPER only)

The specialist emits a schema-valid forecast envelope. It does **not** trade.
Default weather source is fixtures; pytest does not use the network.

```bash
export LAB_MODE=PAPER
export POLYMARKET_DATA_SOURCE=fixtures
export WEATHER_DATA_SOURCE=fixtures
export NWS_ALLOW_NETWORK=0
python -m research_lab serve --host 127.0.0.1 --port 8000
```

Then:

1. `POST /api/ingest`
2. Human `POST /api/rules-review` on market `900004` (KMIA daily-max fixture) using
   the logged `rules_hash`.
3. `POST /api/models/authorize` with
   `weather-station-baseline-v1-calibration-identity-stub-v0`
   (paper-use flag, not a statistical qualification).
4. `POST /api/specialist/weather` with
   `{"market_id": "900004", "as_of": "2026-09-15T12:00:00+00:00"}`.
   - `ABSTAIN` if station/rules/source/vintage are ambiguous (e.g. market `900005`
     names AccuWeather — NWS is **not** substituted).
   - `PROPOSE` returns `forecast` plus logged variants (raw, calibrated identity
     stub, historical base rate, market mid). `imported` is false.
5. `POST /api/forecast` with that `forecast` object (or
   `{ "action": "PROPOSE", "forecast": { ... } }`).
   `risk-v2` is the only stake authority and may still `NO_TRADE`.

CLI equivalent (prints the envelope; add `--import-paper` only after review +
authorize on that data dir):

```bash
python -m research_lab weather-forecast --market-id 900004 \
  --as-of 2026-09-15T12:00:00+00:00
```

Optional live NWS GET (`WEATHER_DATA_SOURCE=network` and `NWS_ALLOW_NETWORK=1`)
still cannot place orders. If the contract names a different resolution provider,
the specialist ABSTAINs instead of swapping in NWS.

This path does **not** claim profitability. Identity calibration and Normal tails
are known limitations; Kapu B (forward paper data) is a **measurement path**,
not a proven edge.

## Kapu B — forward paper collection (still PAPER)

Measurement only. Fixture Gamma/CLOB is the CI default. Live public GET is
off unless both flags are set:

```bash
export POLYMARKET_DATA_SOURCE=network
export POLYMARKET_ALLOW_NETWORK=1
```

That combination still cannot place orders. Default tests patch adapters closed.

```bash
export LAB_MODE=PAPER
export POLYMARKET_DATA_SOURCE=fixtures
export POLYMARKET_ALLOW_NETWORK=0
export WEATHER_DATA_SOURCE=fixtures
export NWS_ALLOW_NETWORK=0

# 1. Discover weather-like markets; persist raw archive + meta + books + rules_hash
python -m research_lab discover --data-dir data

# 2. Human rules review (exact logged hash, cluster, cutoff, settlement source)
python -m research_lab show-rules --market-id 900004 --data-dir data
python -m research_lab rules-review --market-id 900004 \
  --rules-hash <exact-logged-hash> \
  --cluster-id kmia-station-date \
  --trading-cutoff 2026-09-16T22:00:00+00:00 \
  --expected-settlement-source https://api.weather.gov/stations/KMIA \
  --reviewer you \
  --authorize-model weather-station-baseline-v1-calibration-identity-stub-v0 \
  --data-dir data

# NOAA city daily-high (Tokyo fixture 900006): parser leaves rounding unspecified.
# Record half_up/1 only after reading the full rules — do not invent it.
# python -m research_lab rules-review --market-id 900006 \
#   --rules-hash <exact-logged-hash> \
#   --cluster-id tokyo-rjtt-daily-high \
#   --trading-cutoff 2026-09-16T15:00:00+00:00 \
#   --expected-settlement-source 'https://www.weather.gov/wrh/timeseries?site=rjtt' \
#   --rounding-mode half_up --rounding-increment 1 --rounding-unit C \
#   --reviewer you --data-dir data

# 3. Dry forward cycle (logs PROPOSE/ABSTAIN; does not import; never auto-settles)
python -m research_lab paper-run --as-of 2026-09-15T12:00:00+00:00 --data-dir data

# Optional: import PROPOSE envelopes through risk-v2 after review + authorize
python -m research_lab paper-run --import-paper --as-of 2026-09-15T12:00:00+00:00 \
  --cluster-id kmia-station-date --data-dir data
```

Without a matching rules review, risk-v2 still refuses positions
(`missing_rules_review`). Settlement stays `POST /api/settle` with a human
`source_url`. `paper-run` will reject any auto-settle flag.

Research call/cost ceilings: `RESEARCH_MAX_CALLS_PER_CYCLE`,
`RESEARCH_COST_CEILING_USD`, `RESEARCH_MIN_INTERVAL_SECONDS` (network spacing).

Replay a logged specialist run from archived raw inputs:

```bash
python -m research_lab replay-estimate --estimate-id 1 --data-dir data
```

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Mode-labeled dashboard |
| GET | `/health` | `live_trading: false` |
| GET | `/api/state` | Current mode snapshot |
| GET | `/api/export.csv` | PAPER only |
| GET | `/api/evaluation` | Metric stubs; `edge_proven: false` |
| POST | `/api/ingest` | Fixture or GET-only network |
| POST | `/api/discover` | Weather-like classify + optional ingest; archives raw GET |
| GET | `/api/markets` | Includes `rules_hash` + review status |
| GET | `/api/markets/{id}` | Full rules text for human review |
| POST | `/api/rules-review` | Human gate (hash, cluster, cutoff, settlement source, optional PAPER model, optional rounding) |
| POST | `/api/models/authorize` | Paper-use only |
| POST | `/api/forecast` | Import + decision path |
| POST | `/api/paper-run` | Dry/loop collection; never auto-settles |
| GET | `/api/paper-runs` | Forward-run log |
| GET | `/api/archive` | Raw Gamma/CLOB payload archive |
| GET | `/api/kapu-b/status` | Measurement vs missing; `edge_proven: false` |
| POST | `/api/worker/run` | Re-run a stored forecast id |
| POST | `/api/close` | Simulated FOK sell |
| POST | `/api/settle` | Manual cited settlement |
| POST | `/api/pause` `/api/resume` | Human kill switch |
| GET | `/api/grok/status` | Stub; not wired; critique-only role |
| POST | `/api/grok/critique` | Optional structured critique; never overrides `p_yes` |
| GET | `/api/research/budget` | Research + Grok ceilings; not a stake |
| GET | `/api/specialist/placeholder` | Always ABSTAIN |
| POST | `/api/specialist/weather` | Weather baseline envelope; does not trade unless `import_paper` |
| GET | `/api/specialist/weather/log` | Logged comparison tracks + re-runnable request |
| POST | `/api/specialist/weather/replay/{id}` | Re-run archived raw inputs; does not trade |

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
