# Status vs átadás (2026-09-15, weather slice)

Honesty rule: do not fill gaps with an optimistic narrative. No historical P&L
in this repo is evidence of a trading edge. The weather baseline is a **numeric
research path**, not a proven edge.

Companion research: [`02_PIACKUTATAS.html`](02_PIACKUTATAS.html).
Locked strategy and `risk-v2` numbers: [`01_KUTATAS_ES_STRATEGIA.md`](01_KUTATAS_ES_STRATEGIA.md).

## What exists (Kapu A foundations + Strategy A first slice)

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
- Paper close (FOK sell) and manual settle (`payoff` 0 / 0.5 / 1 **on held token**)
- Forecast schema + immutability / idempotency
- Local FastAPI: ingest, forecast, worker path, close, settle, dashboard
- GET-only Gamma/CLOB adapters; default **fixtures**; network GET behind a flag
- **Weather station/date baseline** (`WeatherStationBaseline`): target is the
  contract resolution quantity (station + local date + rounding), not city weather.
  Interval probs `F(b)-F(a)` under a Normal error model with rounding/boundary
  hooks. Always logs raw / identity-calibrated / historical base rate / market mid.
  ABSTAIN when station, rules, source, vintage, or forecast max is missing/ambiguous
- **NWS-shaped source adapter**: fixture archive is the CI default; optional live
  GET behind `NWS_ALLOW_NETWORK=1`. Observation lag (~20 min). Missing max/min stay
  null (never 0). Incomplete series is never promoted to official daily max.
  Contract naming a different provider (e.g. AccuWeather) → **ABSTAIN**, no NWS swap
- Research cost ceiling (`RESEARCH_COST_CEILING_USD`); fixture reads cost 0
- Specialist placeholder (ABSTAIN) kept for non-weather families
- Grok stub + cost-ceiling config; optional structured **critique** interface only
  (does not override `p_yes`, does not size trades)
- `evaluation.py` stubs: net trade P&L, ops-cost-adjusted P&L, Brier/log loss,
  cluster bootstrap
- Offline `tests/test_lab.py` + `tests/test_weather_specialist.py` (no network)

## Locked strategy vs code

| Priority | Intent | Code |
|---|---|---|
| **A primary** | Specialist fair value; weather station/date first; econ prints alt; not LLM oracle | Paper FOK path + weather baseline envelope. Stake still **only** via `risk-v2` after `POST /api/forecast`. Not claimed better than mid. |
| **B secondary** | Formal basket/RV research | Diagnostic helper only; **no** basket executor |
| **C later** | Market making | Absent |

## Gaps vs átadás component table

| Component | This repo | Gap |
|---|---|---|
| Public Gamma/CLOB GET | Fixture adapter + optional network GET | Real-schema soak / rate limits / production archive not done |
| Market and book log | SQLite meta + snapshot + hash | Event-time archive, retention policy |
| Rules review | Manual HTTP record (hash, cluster, cutoff) + conservative weather-rules parser | Exceptions, rule-change watcher, full cutoff-timezone workflow |
| Specialist model | Weather baseline + identity calibration stub + variant log | **Fitted** external calibration vintage; extremes/regime-shift model; econ-print family |
| NWS / contract source | Timestamped fixture archive; optional live GET (observations only) | Recorded live vintages; official daily-max settlement watcher; live forecast-grid mapping |
| Grok API | Stub + ceiling + critique interface (not wired) | Configurable model, strict JSON, spend enforcement |
| Grok → paper signal | JSON import + POST; specialist is the numeric path | Trusted source archive |
| Fair-value paper executor | Simulated FOK + `risk-v2` microstructure gates | Latency stress vs fill model |
| Basket / MM executor | Explicitly **not** implemented (B diagnostic / C later) | Formal payoff matrix, inventory, queue |
| Simulated close/settle | Manual cited settle; book FOK close | Authenticated automatic settlement watcher |
| Live order / wallet / redeem | Absent | Separate audited adapter only after Kapu D **and** legal clearance |
| Dashboard / CSV | PAPER state | Must never display live as paper |
| Self-improvement | Protocol in docs + evaluator stubs + research_estimates log | Challenger registry, walk-forward, human promotion |

## What this slice does **not** prove

- Normal errors fail at extremes and regime shifts; that is documented, not fixed
- Identity calibration is a stub (raw == calibrated) until an external vintage exists
- NWS forecast-period temperature is a **proxy** for predicted daily max, not the
  contractual official daily maximum
- Fixture climatology `p=0.42` is a prior table, not a climate claim
- No forward paper sample, no live vintages, **no proven edge**

## Kapu A / B / C / D

| Gate | Status | Notes |
|---|---|---|
| **A — Operation** | **Partial** | Offline tests + fixtures, including weather ABSTAIN/import. DEMO/PAPER isolation. Unknown fee / expired forecast / missing review refuse positions. `risk-v2` gates included. Live public API soak **not** claimed. |
| **B — Data** | **Not claimed** | Fixture hashes are reproducible; live vintages, stations, exceptional resolutions are not verified here. Next gate: collect timestamped forward paper data (forecast vintages + official daily max + book mids) without promoting to live. |
| **C — Economic hypothesis** | **Not claimed** | No locked forward sample. Correct output: **no proven edge**. |
| **D — Eligibility and live tech** | **Not claimed** | **HU:** SZTFH block uncertain → **stay PAPER**. No VPN. No live adapter. Money pilot needs legal clearance. |

## Next recommended work

1. **Kapu B / forward paper data collection** for the KMIA-style station/date cluster:
   archive issue time, model run, official daily-max field (or explicit missing),
   contemporaneous mid, and all four comparison tracks. Do not claim edge from
   fixtures.
2. External calibration vintage (replace identity stub) scored against the
   resolution quantity, with a pre-registered holdout.
3. Real rules-review workflow: full text, cutoff timezone, exceptions, hash re-check.
4. Recorded network GET corpus (separate from CI fixtures) without enabling orders.
5. Keep B (basket) as research notes + diagnostics until a formal basket state table exists.
6. Do not start C or live tech while Kapu B/C/D are red.

Grok wiring and any CLOB/wallet work stay behind those gates.
