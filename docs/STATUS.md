# Status vs átadás (2026-09-15)

Honesty rule: do not fill gaps with an optimistic narrative. No historical P&L
in this repo is evidence of a trading edge.

## What exists (Kapu A foundations)

- PAPER / DEMO only; separate SQLite files; CSV export refuses DEMO
- `Risk` dataclass (`risk-v1`) and hard gates: pause, daily loss, drawdown,
  unknown fee, expired forecast, missing rules review, hash mismatch, unauthorized
  model, incomplete mark-to-book
- Simulated FOK across book levels, fee rounded up, Decimal cash, SQLite
  transaction for fill + cash + position
- Market/book log: metadata, snapshot JSON, `rules_hash`, fee/meta
- Paper close (FOK sell) and manual settle (`payoff` 0 / 0.5 / 1 **on held token**)
- Forecast schema + immutability / idempotency
- Local FastAPI: ingest, forecast, worker path, close, settle, dashboard
- GET-only Gamma/CLOB adapters; default **fixtures**; network GET behind a flag
- Specialist placeholder (ABSTAIN) and Grok stub + cost-ceiling config
- `evaluation.py` stubs: net trade P&L, ops-cost-adjusted P&L, Brier/log loss,
  cluster bootstrap
- Offline `tests/test_lab.py`

## Gaps vs átadás component table

| Component | This repo | Gap |
|---|---|---|
| Public Gamma/CLOB GET | Fixture adapter + optional network GET | Real-schema soak / rate limits / production archive not done |
| Market and book log | SQLite meta + snapshot + hash | Event-time archive, retention policy |
| Rules review | Manual HTTP record (hash, cluster, cutoff) | Structured parser, exceptions, rule-change watcher |
| Specialist model | Interface + ABSTAIN placeholder | Baseline + calibration + timestamped source adapter |
| Grok API | Stub + ceiling env | Configurable model, strict JSON, spend enforcement in a real client |
| Grok → paper signal | JSON import + POST | Trusted source archive |
| Fair-value paper executor | Implemented (simulated FOK) | Latency stress vs fill model |
| Basket / MM executor | Absent | Out of scope this run |
| Simulated close/settle | Manual cited settle; book FOK close | Authenticated automatic settlement watcher |
| Live order / wallet / redeem | Absent | Separate audited adapter only after Kapu D |
| Dashboard / CSV | PAPER state | Must never display live as paper |
| Self-improvement | Protocol in docs + evaluator stubs | Challenger registry, walk-forward, human promotion |

## Kapu A / B / C / D

| Gate | Status | Notes |
|---|---|---|
| **A — Operation** | **Partial** | Offline tests + fixtures intended green. DEMO/PAPER isolation tested. Unknown fee / expired forecast / missing review refuse positions. **Not** full: live public API integration is optional/flagged, not claimed proven. |
| **B — Data** | **Not claimed** | Fixture hashes are reproducible; live source existence, vintages, stations, exceptional resolutions are not verified here. |
| **C — Economic hypothesis** | **Not claimed** | No locked forward sample. Correct output: **no proven edge**. |
| **D — Eligibility and live tech** | **Not claimed** | No live adapter. Do not start one from this PR. |

## Next recommended work

1. Specialist baseline (e.g. station/simple base rate) with **external** calibration
   vintage, timestamped inputs, and ABSTAIN when rules are incomplete.
2. Real rules-review workflow: store full text, reviewer checklist, cutoff timezone
   confirmation, exception list, hash re-check on every ingest.
3. Recorded network GET corpus (separate from CI fixtures) without enabling orders.
4. Only after B: pre-register H1–H3 tests in `docs/01_KUTATAS_ES_STRATEGIA.md`
   and keep the test set unread.

Grok wiring and any CLOB/wallet work stay behind those gates.
