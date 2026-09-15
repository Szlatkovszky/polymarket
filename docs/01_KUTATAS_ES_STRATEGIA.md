# Kutatás és stratégia / Research hypotheses

These are **hypotheses and kill conditions**, not claims of alpha.
Paper and DEMO fills do not prove a live edge. Hit rate and tip count are not
success metrics. The target is contractual payout after **all relevant costs**,
inside coded risk limits, with bindable depth.

Companion market research (2026-09-15): [`02_PIACKUTATAS.html`](02_PIACKUTATAS.html).
Risk numbers below are locked in `research_lab/core.py` as **`risk-v2`**.

A kutatási réteg soha nem választ tétméretet, nem emel limiteket, és nem kapcsolja ki a kill-eket.

## Locked strategy priority

| Rank | Branch | Status in this repo |
|---|---|---|
| **A (primary)** | Specialist **fair value** | Paper executor path + **weather station/date baseline** (Normal error, identity calibration stub). First study set: station-and-date weather. Alternative: strictly specified, pre-scheduled **economic releases** (not built). **Not** a general “Grok predicts everything” bot. **Not** a proven edge. |
| **B (secondary)** | Formal basket / relative value | Research only. A YES+NO apparent gap is **diagnostic** and **never auto-traded**. No basket executor. |
| **C (later)** | Selective market making | Out of scope. Needs quote/queue/latency/inventory modules first. |

Do not fund an LLM-latency arb bot against sub-second competition. Grok’s role is
structuring rules and evidence; its self-reported confidence is **not** a
calibrated probability. It does not get wallet keys or risk-limit tools.

## Business hypotheses (to be tested, not assumed)

1. **H1 — Calibrated specialist vs mid.** On a pre-registered weather (or econ-print)
   cluster, a timestamped specialist is closer to contractual payoff than book mid
   and a simple base rate.
   *Falsify if:* out-of-sample Brier/log loss is not better after multiple-testing
   accounting.

2. **H2 — Conservative fair-value entry.** Buy YES iff `edge = p_L − c ≥ 0.03`;
   buy NO iff `edge_NO = (1 − p_H) − c_NO ≥ 0.03`; pick the better passing side,
   never both on conflicting narratives. `c` is FOK VWAP + taker fee + **research
   reserves** (0.002 ops + 0.002 slippage **per share** — assumptions, not observed
   own costs).
   *Falsify if:* cost-adjusted P&L ≤ 0 on a locked forward window, a single cluster
   drives the sign, or worse fill/delay stress wipes it.

3. **H3 — LLM as structure, not oracle.** Any Grok/LLM increment must beat the
   specialist-only series **after** its own API cost, with no post-cutoff evidence.
   *Falsify if:* ops-cost-adjusted P&L of the LLM series ≤ specialist, or look-ahead.

4. **H4 — Capacity vs costs.** Depth at the 0.5% trade cap and 20% book-participation
   limit can cover measured monthly ops/LLM/data cost without raising risk limits.
   *Falsify if:* it cannot. Then stop the branch; do not raise limits.

None of H1–H4 are confirmed. Kapu C is **not** claimed.

Worked **illustration only** (not a quote): `p=0.64`, `p_L=0.59`, ask `0.54`,
fee rate `0.05` → fee/share `0.01242`; plus 0.002 + 0.002 → `c=0.55642`;
conservative edge `0.03358` **if** `p_L` is justified and the book is still
bindable. `p_L` in the prototype is a conservative scenario, not a 95% CI.

## Fee model (unknown → NO TRADE)

Documented taker fee: `size × fee_rate × price × (1 − price)`.
The bot reads **`feesEnabled` + `feeSchedule`** from market metadata (not a baked
category table). Missing fields or an unsupported exponent → **NO TRADE**.
`feesEnabled: false` is a known zero rate. Maker fee is documented as zero; that
is not zero risk. Displayed mid/probability is not a bid.

## Risk defaults (`risk-v2`, 10k sim equity)

10k is the simulated starting book, **not** a suggested deposit. Stops block the
**next entry**; they are not a guaranteed floor.

| Limit | Code field | Starting value |
|---|---|---|
| One trade all-in entry cost | `max_trade_cost` | 0.5% = 50 |
| One market aggregate | `max_market_cost` | 1% = 100 (prototype also refuses a second ticket on an open market) |
| One semantic event cluster | `max_cluster_notional` | 2% = 200 |
| All open entry cost (gross) | `max_open_entry_cost` | 10% = 1000 |
| Daily loss stop | `max_daily_loss` | 1.5% = 150 |
| Peak-to-trough drawdown stop | `max_drawdown` | 5% = 500 |
| Min conservative edge | `min_conservative_edge` | 0.03 after fee + reserves |
| Price band | `price_band_low/high` | 0.10–0.90 |
| Max spread | `max_spread` | 0.03 absolute |
| Depth participation | `max_depth_fraction` | ≤ 20% of ask depth |
| Fresh book | `max_book_age_seconds` | ≤ 5 s (server and receive time) |
| Forecast age | `max_forecast_age_hours` | ≤ 6 h |
| Settlement horizon | `max_settlement_days` | ≤ 14 d (human review records the fine cutoff) |
| Daily entry cap | `max_entries_per_day` | 20 (not a production target) |
| Research reserves | `ops_reserve_per_share` + `slippage_reserve_per_share` | 0.002 + 0.002 per share |

10% open exposure plus a common event shock can exceed the 5% stop. Illiquidity,
gap, settlement delay, or bugs can exceed any coded stop.

Quarter-Kelly `f=(p−c)/(1−c)/4` is a comment only; the 0.5% trade cap is the
binding coded size rule. Stake is **never** taken from the forecast envelope.

## Exclusion / kill conditions

Stop proposing entries when:

- Mode would be anything other than PAPER or DEMO.
- Fee schedule unknown / unsupported exponent.
- Forecast expired, `as_of` in the future, or older than 6 hours.
- Settlement/cutoff more than 14 days out.
- Book older than 5 seconds; spread > 0.03; price outside 0.10–0.90; size would
  take more than 20% of depth; FOK cannot fill.
- `rules_hash` mismatch or no human rules review.
- Evidence `available_at` > `as_of`.
- Paused; daily loss; drawdown; incomplete valuation; daily entry cap; cluster or
  open-exposure caps.
- Ambiguous contract (cutoff, timezone, source, rounding, revision, cancel) → **ABSTAIN**.
- Action would raise limits, disable kills, size from the LLM, auto-trade a YES+NO
  gap, or promote to live.
- Legal/platform eligibility unclear → remain PAPER; **do not use a VPN** to bypass
  access control.

## Hungary / legal (not legal advice)

January 2026 Hungarian press reported an **SZTFH** block of Polymarket. This
research did **not** independently confirm that the block was lifted. A country
missing from Polymarket’s geoblock list is **not** automatic lawful access.
**Stay PAPER** while that is uncertain. Geoblock endpoints describe the calling
server, not the owner’s eligibility. VPN/proxy/foreign-server workarounds are
**not** part of this plan. A money (live) pilot needs **separate legal clearance**
(jurisdiction, ToS, tax). ESMA’s Sep 2026 TRV discussion of prediction markets
is a risk note, not a license.

## What we will measure (when sample exists)

- Net trade P&L and ops-cost-adjusted P&L (do not double-count entry reserves)
- Brier / log loss vs YES-mapped payoff
- Slippage vs book, refusal reasons, occupancy, cluster concentration, drawdown
- Cluster bootstrap on the **closed** sample only — exploratory, not walk-forward
- Rewards/promos tracked separately; evaluate “clean” P&L without them

A single optimized score must not hide losses. One-shot test set. Human-only
promotion. Champion vs challenger: the LLM does not edit the risk engine.

## Strategy A first slice (landed, unproven)

Code: `research_lab/specialist.py` (`WeatherStationBaseline`),
`research_lab/weather_source.py` (NWS-shaped fixture adapter),
`research_lab/weather_math.py` (interval `F(b)-F(a)` + rounding hooks).

- Target is station + local date + contract rounding, parsed from verified
  `rules_text` / `rules_hash`. Vague city weather is rejected.
- Starting likelihood is Normal around the forecasted daily max with a
  horizon/station sigma table when present, else an explicit unmeasured prior.
  **Limitations:** thin tails vs extremes; no regime-shift model; forecast-grid
  temperature is not the official daily max.
- Calibration hook exists (`identity-stub-v0`); raw and calibrated tracks are
  both logged even when they are equal.
- Historical base rate and contemporaneous YES mid are logged when available;
  missing tracks stay unavailable (not 0).
- Non-NWS resolution source → **ABSTAIN** (no silent NWS substitution).
- Missing official max/min fields stay null; incomplete series is never treated
  as official daily max.
- Grok remains unwired; optional critique must not override `p_yes`.
- `risk-v2` remains the only stake/decision authority. YES+NO gap stays diagnostic.

Still-unproven: any out-of-sample Brier/log-loss or cost-adjusted P&L advantage
vs mid or vs the base rate. **Kapu B scaffolding** (discovery, raw archive,
rules-review CLI, re-runnable forecast log, dry paper-run) is a measurement path
on a pre-registered cluster. It is **not** a locked forward sample and **not**
an edge. Stay PAPER.

## Explicit non-goals for this phase

- Live order, wallet, redeem adapters
- Basket executor or market-maker executor
- Wired Grok API (interface + cost ceiling + critique stub only)
- Fitted specialist (baseline exists; calibration is still an identity stub)
- Any statement that the strategy is profitable
- Martingale, wallet-copying, “98% lock” autos, treating many LLM personas as
  independent evidence
