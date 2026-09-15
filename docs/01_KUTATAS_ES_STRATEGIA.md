# Kutatás és stratégia / Research hypotheses

These are **hypotheses and kill conditions**, not claims of alpha.
Paper and DEMO fills do not prove a live edge. Hit rate and tip count are not
success metrics. The target is contractual payout after costs and slippage,
inside the coded risk limits.

A kutatási réteg soha nem választ tétméretet, nem emel limiteket, és nem kapcsolja ki a kill-eket.

## Business hypotheses (to be tested, not assumed)

1. **H1 — Calibrated specialist vs mid.** A timestamped specialist probability,
   after published calibration, is closer to the contractual YES/NO payoff than
   the contemporaneous book mid, on a pre-registered event-cluster split.
   *Falsify if:* out-of-sample Brier/log loss is not better than mid and a simple
   base rate after multiple-testing accounting.

2. **H2 — Conservative fair-value entry.** Buying the cheap side only when
   `p_low` (YES) or `1 - p_high` (NO) still exceeds all-in FOK price + fee +
   allocated ops reserve by the versioned `min_conservative_edge` can leave
   positive **net trade P&L** after fees on a locked forward window.
   *Falsify if:* cost-adjusted P&L ≤ 0 on that window, or the result is a single
   cluster, or live-fill stress (worse price, extra delay) wipes the sign.

3. **H3 — LLM as structure, not oracle.** A Grok (or other LLM) layer that only
   structures evidence and may shrink toward the specialist does not, by itself,
   create edge. Any incremental P&L must beat the specialist-only baseline after
   the LLM’s own API cost.
   *Falsify if:* ops-cost-adjusted P&L of the LLM-corrected series ≤ specialist
   series, or the LLM silently uses post-cutoff evidence.

4. **H4 — Capacity vs costs.** Average available depth at the tested size can
   cover measured monthly data/LLM/ops cost at the pre-registered loss cap.
   *Falsify if:* capacity at `unit_shares` / `max_position_notional` cannot cover
   costs without raising risk limits (which this layer must not do).

None of H1–H4 are marked confirmed. Kapu C is **not** claimed.

## Exclusion / kill conditions

Stop proposing entries (code already refuses many of these) when:

- Mode would be anything other than PAPER or DEMO.
- Fee rate is missing/unknown.
- Forecast `expires_at` ≤ now, or `as_of` is in the future.
- `rules_hash` ≠ hash of the logged full rule text, or no human rules review.
- Evidence `available_at` > `as_of` (look-ahead).
- Account paused; daily loss or drawdown gate hit; valuation incomplete.
- Book depth cannot FOK the risk-engine size (partial fill is a fail).
- Model version is not paper-authorized (authorization ≠ statistical pass).
- Correlated cluster notional would exceed `max_cluster_notional`.
- Resolution rules are ambiguous (cutoff, timezone, source, rounding, revision,
  cancellation) → research output **ABSTAIN**.
- Sources missing, contradictory, or invented.
- Proposed action would raise limits, disable kills, size the stake from the
  LLM, or promote to live.
- Legal/platform eligibility for the operator is unclear → remain in research
  mode; do not bypass access control.

Daily/max-loss stops **block the next entry**. They are not a guaranteed max
loss and do not create live exit liquidity.

## What we will measure (when sample exists)

- Net trade P&L (paper FOK accounting)
- Ops-cost-adjusted P&L (ledgered invoices; do not double-count the per-trade
  reserve used only at entry)
- Brier and log loss vs payoff mapped to YES
- Calibration plots (not in Kapu A)
- Slippage vs quoted book, fill/refusal reasons, capital occupancy, cluster
  concentration, drawdown
- Cluster bootstrap on the **closed** sample only (`evaluation.py`) — not
  walk-forward, not live-fill confirmed

A single optimized score must not hide losses. The test set is one-shot; tuning
after seeing it requires a new test. Challenger promotion is human-only.

## Explicit non-goals for this phase

- Live order, wallet, redeem adapters
- Basket / market-maker executor
- Wired Grok API (interface + cost ceiling only)
- Fitted specialist model (placeholder ABSTAIN + calibration hooks)
- Any statement that the strategy is profitable
