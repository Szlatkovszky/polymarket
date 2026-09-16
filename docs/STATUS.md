# Status vs átadás (2026-09-15, Kapu B scaffolding)

Honesty rule: do not fill gaps with an optimistic narrative. No historical P&L
in this repo is evidence of a trading edge. The weather baseline and the Kapu B
runner are a **numeric measurement path**, not a proven edge.

Companion research: [`02_PIACKUTATAS.html`](02_PIACKUTATAS.html).
Locked strategy and `risk-v2` numbers: [`01_KUTATAS_ES_STRATEGIA.md`](01_KUTATAS_ES_STRATEGIA.md).

## What exists (Kapu A foundations + Strategy A slice + Kapu B scaffolding)

- PAPER / DEMO only; separate SQLite files; CSV export refuses DEMO
- **`risk-v2`** on 10k sim equity: trade 0.5%, market 1%, cluster 2%, open 10%,
  daily stop 1.5%, DD stop 5%; min edge 0.03; band 0.10–0.90; max spread 0.03;
  ≤20% depth; book ≤5s; forecast ≤6h; settlement ≤14d; ≤20 entries/day
- Taker fee `size×fee_rate×price×(1−price)` from **`feesEnabled` + `feeSchedule`**;
  unknown/unsupported exponent → no position. Reserves 0.002 ops + 0.002 slippage
  per share are **assumptions**
- YES+NO gap logged as **diagnostic only** (`tradeable: false`); never an auto-trade
- Simulated FOK, fee rounded up, Decimal cash, SQLite fill/cash/position transaction
- Market/book log: metadata, snapshot JSON, `rules_hash`, fee schedule meta
- **Append-only raw archive** of Gamma list/market and CLOB book payloads
  (`raw_archive`: source, payload_hash, rules_hash, captured_at)
- Paper close (FOK sell) and manual settle (`payoff` 0 / 0.5 / 1 **on held token**)
- Forecast schema + immutability / idempotency
- Local FastAPI: ingest, discover, forecast, worker path, close, settle, dashboard
- GET-only Gamma/CLOB adapters; default **fixtures**; network GET behind
  `POLYMARKET_DATA_SOURCE=network` **and** `POLYMARKET_ALLOW_NETWORK=1`
- **Weather-like discovery** (keyword / station / specialist-parse filter) then
  ingest of matching markets only; non-weather fixtures stay out of that path.
  Network mode uses Gamma `public-search` (not top `/markets`) so NOAA city
  daily-high contracts can be found. Fixtures remain the CI default.
- **Weather station/date baseline** (`WeatherStationBaseline`): target is the
  contract resolution quantity (station + local date + rounding), not city weather.
  Interval probs `F(b)-F(a)` under a Normal error model with rounding/boundary
  hooks. Always logs raw / identity-calibrated / historical base rate / market mid.
  ABSTAIN when station, rules, source, vintage, or forecast max is missing/ambiguous
- **NWS-shaped source adapter**: fixture archive is the CI default; optional live
  GET behind `NWS_ALLOW_NETWORK=1`. Observation lag (~20 min). Missing max/min stay
  null (never 0). Incomplete series is never promoted to official daily max.
  Contract naming a different provider (e.g. AccuWeather) → **ABSTAIN**, no NWS swap
- Research cost + call ceilings (`RESEARCH_COST_CEILING_USD`,
  `RESEARCH_MAX_CALLS_PER_CYCLE`, `RESEARCH_MIN_INTERVAL_SECONDS`); fixture reads
  cost 0 but still count against the call ceiling
- Specialist placeholder (ABSTAIN) kept for non-weather families
- Grok stub + cost-ceiling config; optional structured **critique** interface only
  (does not override `p_yes`, does not size trades)
- `evaluation.py` stubs: net trade P&L, ops-cost-adjusted P&L, Brier/log loss,
  cluster bootstrap
- **Human rules-review CLI + API**: exact `rules_hash`, semantic `cluster_id`,
  timezone-aware `trading_cutoff`, expected settlement source, optional PAPER
  `model_version` authorize. Missing or mismatched review → `missing_rules_review`
- **Forecast/decision archive**: every weather-forecast run (PROPOSE and ABSTAIN)
  stores timestamped variants plus **re-runnable `request_json` / `input_hash`**
- **Forward paper runner** (`paper-run`): dry default, loop-friendly positive
  `--max-cycles`, optional `--import-paper` through `risk-v2`. **Never auto-settles**
- Offline `tests/test_lab.py` + `tests/test_weather_specialist.py` +
  `tests/test_kapu_b.py` (no network)

## Locked strategy vs code

| Priority | Intent | Code |
|---|---|---|
| **A primary** | Specialist fair value; weather station/date first; econ prints alt; not LLM oracle | Paper FOK path + weather baseline envelope. Stake still **only** via `risk-v2` after `POST /api/forecast` or `--import-paper`. Not claimed better than mid. |
| **B secondary** | Formal basket/RV research | Diagnostic helper only; **no** basket executor |
| **C later** | Market making | Absent |

## Kapu B checklist — measurable vs still missing

| Item | Now measurable in-repo | Still missing |
|---|---|---|
| Weather-like market discovery | Fixture classify + ingest; network discover uses Gamma `public-search` behind explicit flags | Real-schema soak, tag coverage, rate-limit behavior in production |
| Raw GET archive | Append-only `raw_archive` + market `raw_json` + book snapshots + `rules_hash` | Retention policy; recorded live corpus separate from CI fixtures |
| Human rules review | CLI `show-rules` / `rules-review` + `GET /api/markets/{id}` + HTTP POST | Exception handling, rule-change watcher, cutoff-timezone UI |
| PAPER model authorize | `--authorize-model` / `POST /api/models/authorize`; per-review pin | Statistical qualification (explicitly not this flag) |
| No review → no position | `missing_rules_review` on `import_forecast` and `--import-paper` runner | — (wired) |
| Forecast + ABSTAIN log | `research_estimates` + `decisions` with timestamps | Live vintage completeness |
| Re-runnable inputs | `request_json` / `replay-estimate` | Bit-identical live NWS replay corpus |
| Forward paper cycle | `paper-run` dry/loop; optional import; call/cost ceilings | Locked calendar sample; official daily-max settlement watcher |
| Settlement | Manual `POST /api/settle` with `source_url` only | Authenticated automatic settlement (out of scope) |
| Edge | `edge_proven: false` everywhere | **No proven edge** — do not claim one from fixtures |

## Gaps vs átadás component table

| Component | This repo | Gap |
|---|---|---|
| Public Gamma/CLOB GET | Fixture adapter + optional network GET + weather-like discovery | Real-schema soak / rate limits / production archive not done |
| Market and book log | SQLite meta + snapshot + hash + append-only raw archive | Retention policy; live event-time corpus |
| Rules review | CLI + HTTP (hash, cluster, cutoff, settlement source, PAPER model pin) | Exceptions, rule-change watcher |
| Specialist model | Weather baseline + identity calibration stub + variant log | **Fitted** external calibration vintage; extremes/regime-shift model; econ-print family |
| NWS / contract source | Timestamped fixture archive; optional live GET (observations only) | Recorded live vintages; official daily-max settlement watcher; live forecast-grid mapping |
| Grok API | Stub + ceiling + critique interface (not wired) | Configurable model, strict JSON, spend enforcement |
| Grok → paper signal | JSON import + POST; specialist is the numeric path | Trusted source archive |
| Fair-value paper executor | Simulated FOK + `risk-v2` microstructure gates + dry paper-run | Latency stress vs fill model |
| Basket / MM executor | Explicitly **not** implemented (B diagnostic / C later) | Formal payoff matrix, inventory, queue |
| Simulated close/settle | Manual cited settle; book FOK close; runner **cannot** auto-settle | Authenticated automatic settlement watcher |
| Live order / wallet / redeem | Absent | Separate audited adapter only after Kapu D **and** legal clearance |
| Dashboard / CSV | PAPER state | Must never display live as paper |
| Self-improvement | Protocol in docs + evaluator stubs + research_estimates log | Challenger registry, walk-forward, human promotion |

## What this slice does **not** prove

- Normal errors fail at extremes and regime shifts; that is documented, not fixed
- Identity calibration is a stub (raw == calibrated) until an external vintage exists
- NWS forecast-period temperature is a **proxy** for predicted daily max, not the
  contractual official daily maximum
- Fixture climatology `p=0.42` is a prior table, not a climate claim
- Discovery heuristics are a collection filter, not a contract parser substitute
- No locked forward sample, no live vintages, **no proven edge**

## Kapu A / B / C / D

| Gate | Status | Notes |
|---|---|---|
| **A — Operation** | **Partial** | Offline tests + fixtures, including weather ABSTAIN/import. DEMO/PAPER isolation. Unknown fee / expired forecast / missing review refuse positions. `risk-v2` gates included. Live public API soak **not** claimed. |
| **B — Data** | **Partial (scaffolding only)** | Fixture discovery, raw archive, rules-review CLI, re-runnable forecast log, dry paper-run are in place. Live vintages, official daily max, and a locked forward sample are **not** claimed. `GET /api/kapu-b/status` → `claimed: false`. |
| **C — Economic hypothesis** | **Not claimed** | No locked forward sample. Correct output: **no proven edge**. |
| **D — Eligibility and live tech** | **Not claimed** | **HU:** SZTFH block uncertain → **stay PAPER**. No VPN. No live adapter. Money pilot needs legal clearance. |

## Next recommended work

1. Recorded live GET corpus (Gamma/CLOB + NWS vintages) **separate from CI fixtures**,
   still without orders.
2. Official daily-max settlement watcher with human-cited payoff only.
3. External calibration vintage (replace identity stub) scored against the
   resolution quantity, with a pre-registered holdout.
4. Keep B (basket) as research notes + diagnostics until a formal basket state table exists.
5. Do not start C or live tech while Kapu C/D are red.

Grok wiring and any CLOB/wallet work stay behind those gates.
Hungary: **stay PAPER**. No proven edge.
