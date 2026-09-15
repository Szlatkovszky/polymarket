# Status vs átadás (2026-09-15)

Honesty rule: do not fill gaps with an optimistic narrative. No historical P&L
in this repo is evidence of a trading edge.

Companion research: [`02_PIACKUTATAS.html`](02_PIACKUTATAS.html).
Locked strategy and `risk-v2` numbers: [`01_KUTATAS_ES_STRATEGIA.md`](01_KUTATAS_ES_STRATEGIA.md).

## What exists (Kapu A foundations)

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
- Specialist placeholder (ABSTAIN) and Grok stub + cost-ceiling config
- `evaluation.py` stubs: net trade P&L, ops-cost-adjusted P&L, Brier/log loss,
  cluster bootstrap
- Offline `tests/test_lab.py`

## Locked strategy vs code

| Priority | Intent | Code |
|---|---|---|
| **A primary** | Specialist fair value; weather station/date first; econ prints alt; not LLM oracle | Paper FOK path + specialist stub (ABSTAIN until a real baseline exists) |
| **B secondary** | Formal basket/RV research | Diagnostic helper only; **no** basket executor |
| **C later** | Market making | Absent |

## Gaps vs átadás component table

| Component | This repo | Gap |
|---|---|---|
| Public Gamma/CLOB GET | Fixture adapter + optional network GET | Real-schema soak / rate limits / production archive not done |
| Market and book log | SQLite meta + snapshot + hash | Event-time archive, retention policy |
| Rules review | Manual HTTP record (hash, cluster, cutoff) | Structured parser, exceptions, rule-change watcher |
| Specialist model | Interface + ABSTAIN placeholder | Weather-station baseline + calibration + timestamped NWS (or contract source) adapter |
| Grok API | Stub + ceiling env | Configurable model, strict JSON, spend enforcement |
| Grok → paper signal | JSON import + POST | Trusted source archive |
| Fair-value paper executor | Simulated FOK + `risk-v2` microstructure gates | Latency stress vs fill model |
| Basket / MM executor | Explicitly **not** implemented (B diagnostic / C later) | Formal payoff matrix, inventory, queue |
| Simulated close/settle | Manual cited settle; book FOK close | Authenticated automatic settlement watcher |
| Live order / wallet / redeem | Absent | Separate audited adapter only after Kapu D **and** legal clearance |
| Dashboard / CSV | PAPER state | Must never display live as paper |
| Self-improvement | Protocol in docs + evaluator stubs | Challenger registry, walk-forward, human promotion |

## Kapu A / B / C / D

| Gate | Status | Notes |
|---|---|---|
| **A — Operation** | **Partial** | Offline tests + fixtures. DEMO/PAPER isolation. Unknown fee / expired forecast / missing review refuse positions. `risk-v2` gates included. Live public API soak **not** claimed. |
| **B — Data** | **Not claimed** | Fixture hashes are reproducible; live vintages, stations, exceptional resolutions are not verified here. |
| **C — Economic hypothesis** | **Not claimed** | No locked forward sample. Correct output: **no proven edge**. |
| **D — Eligibility and live tech** | **Not claimed** | **HU:** SZTFH block uncertain → **stay PAPER**. No VPN. No live adapter. Money pilot needs legal clearance. |

## Next recommended work

1. Specialist baseline for **station/date weather** (contract source ≠ silently swapped NWS),
   with external calibration vintage, missing-obs ABSTAIN, and timestamped inputs.
2. Real rules-review workflow: full text, cutoff timezone, exceptions, hash re-check.
3. Recorded network GET corpus (separate from CI fixtures) without enabling orders.
4. Keep B as research notes + diagnostics until a formal basket state table exists.
5. Do not start C or live tech while Kapu B/C/D are red.

Grok wiring and any CLOB/wallet work stay behind those gates.
