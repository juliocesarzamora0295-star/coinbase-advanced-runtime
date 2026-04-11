# Strategy Layer — Backtest Results Log

Running baseline for strategy optimization work. See plan at
`C:\Users\julio\.claude\plans\effervescent-launching-bentley.md`.

All results use `segmented_runner` over 10 BTC regimes 2020-2024, 1h candles,
$10k initial cash per regime, fee=0.1%, slippage=5 bps, qty=0.01 BTC.

**Invariant:** all backtests below run with **RiskGate active** (default after Fase 0).
Use `--no-risk` explicitly to disable for debug comparisons.

---

## Fase 0 — fix/backtest-prereqs (baselines, IMMUTABLE)

Fixes applied:
- **BUG-1:** `PaperExecutor.execute` now caps SELL qty to `ledger.position_qty` *before*
  computing notional/fee. Returns `None` if no position. Preexisting fee overcharge
  on orphan SELLs eliminated (backtest-only bug, no live impact).
- **BUG-2:** `segmented_runner --use-risk` flag replaced by `--no-risk` opt-out.
  RiskGate is now active by default in all baselines.

Tests: 4 new (`tests/unit/test_paper_executor_fee_cap.py`), full suite 1140 passed.

### Baseline SMA (fast=20, slow=50, qty=0.01)

| Regime                    | Return  | Trades | WinRate | Sharpe | MaxDD  |      PnL |    Fees |
|---------------------------|---------|--------|---------|--------|--------|----------|---------|
| bull_2020_q4              |   1.21% |     21 |   47.6% |   0.79 |  0.31% |   114.51 |    7.18 |
| bull_2021_q1              |   1.49% |     25 |   48.0% |   0.29 |  1.01% |   131.96 |   23.67 |
| crash_may_2021            |  -2.25% |     26 |   26.9% |  -0.64 |  2.59% |  -213.68 |   22.13 |
| recovery_2021_q3          |   1.33% |     27 |   51.9% |   0.31 |  1.11% |   146.88 |   26.94 |
| bear_2022_h1              |  -1.94% |     49 |   22.4% |  -0.43 |  2.16% |  -175.84 |   36.68 |
| sideways_2022_h2          |  -0.10% |     53 |   24.5% |  -0.04 |  0.70% |     1.14 |   20.59 |
| recovery_2023             |   0.66% |     50 |   34.0% |   0.23 |  0.72% |    80.43 |   26.38 |
| pre_etf_2023_h2           |   0.06% |     61 |   26.2% |   0.02 |  0.85% |    25.66 |   38.76 |
| etf_bull_2024_q1          |   1.10% |     29 |   41.4% |   0.32 |  1.08% |   113.47 |   31.26 |
| consolidation_2024_q2q3   |  -1.60% |     51 |   37.3% |  -0.26 |  2.59% |  -127.28 |   64.44 |
| **AGGREGATE**             |         |    392 |   33.4% |        |        | **97.25** |        |

### Baseline Selector (regime-aware, qty=0.01)

| Regime                    | Return  | Trades | WinRate | Sharpe | MaxDD  |       PnL |   Fees |
|---------------------------|---------|--------|---------|--------|--------|-----------|--------|
| bull_2020_q4              |   0.96% |      6 |  100.0% |   0.89 |  0.16% |     97.01 |   2.10 |
| bull_2021_q1              |   1.00% |      8 |   37.5% |   0.23 |  1.10% |     51.40 |   7.73 |
| crash_may_2021            |  -0.39% |     12 |   50.0% |  -0.12 |  1.38% |     -4.80 |  10.62 |
| recovery_2021_q3          |   1.51% |     10 |   60.0% |   0.37 |  0.88% |    155.78 |  10.11 |
| bear_2022_h1              |  -0.29% |     15 |   46.7% |  -0.05 |  1.53% |    -22.94 |  11.51 |
| sideways_2022_h2          |  -0.13% |     20 |   60.0% |  -0.06 |  0.82% |     -9.30 |   7.76 |
| recovery_2023             |   0.02% |     17 |   52.9% |   0.01 |  0.60% |      6.12 |   8.74 |
| pre_etf_2023_h2           |  -0.44% |     18 |   38.9% |  -0.18 |  0.73% |    -38.45 |  11.42 |
| etf_bull_2024_q1          |   1.00% |      5 |   40.0% |   0.31 |  1.60% |    102.43 |   5.57 |
| consolidation_2024_q2q3   |  -0.14% |     21 |   47.6% |  -0.02 |  0.84% |     14.96 |  27.32 |
| **AGGREGATE**             |         |    132 |   51.5% |        |        | **352.21** |      |

### Baseline FullAdaptive (regime + adaptive sizing)

| Regime                    | Return  | Trades | WinRate | Sharpe | MaxDD  |      PnL |   Fees |
|---------------------------|---------|--------|---------|--------|--------|----------|--------|
| bull_2020_q4              |   0.24% |      6 |  100.0% |   0.83 |  0.06% |    24.06 |   0.60 |
| bull_2021_q1              |   0.68% |      8 |   75.0% |   0.36 |  0.43% |    30.09 |   1.09 |
| crash_may_2021            |  -0.10% |     12 |   50.0% |  -0.18 |  0.25% |    -5.80 |   1.34 |
| recovery_2021_q3          |   0.33% |     10 |   80.0% |   0.44 |  0.16% |    27.68 |   1.27 |
| bear_2022_h1              |  -0.26% |     15 |   40.0% |  -0.21 |  0.47% |   -21.85 |   1.85 |
| sideways_2022_h2          |  -0.31% |     20 |   45.0% |  -0.29 |  0.53% |   -30.00 |   2.35 |
| recovery_2023             |   0.16% |     17 |   35.3% |   0.10 |  0.39% |    -1.65 |   2.05 |
| pre_etf_2023_h2           |   0.10% |     18 |   50.0% |   0.10 |  0.22% |    12.47 |   2.45 |
| etf_bull_2024_q1          |   0.20% |      5 |   40.0% |   0.51 |  0.16% |    19.81 |   0.54 |
| consolidation_2024_q2q3   |  -0.05% |     21 |   38.1% |  -0.04 |  0.34% |   -11.14 |   2.66 |
| **AGGREGATE**             |         |    132 |   50.0% |        |        | **43.66** |       |

### Notes on baselines

- Selector baseline matches v1 plan numbers ($352 PnL, 132 trades) — confirming that
  at `qty=0.01`, RiskGate was **not binding** (notional ~$400-600 << $5000 cap).
  The v1 baselines were not inflated at this qty; the gate becomes relevant only
  when sweeping larger qty values in Fase 1C'.
- SMA has highest trade count but worst win rate (33%). Selector has best win rate
  (51.5%) but half the PnL of SMA gross. FullAdaptive is a PnL loser vs Selector —
  adaptive sizing currently reduces trades too much to be profitable.
- MaxDD per regime is already < 5% across the board (max 2.59% on crash_may_2021
  for SMA). The optimization target (< 5% per regime) is already satisfied on
  baselines — so the bar is to **preserve** MaxDD while lifting Sharpe/PnL.

---

## Fase 1C' — feat/sizing-sweep (config-only phase, NO code changes)

Sweep over `--qty ∈ {0.005, 0.01, 0.015, 0.02, 0.03}`, `--use-selector`, gate active default.

| qty    | Agg PnL   | Max regime DD | Trades | WinRate | Per-regime Sharpe |
|--------|-----------|---------------|--------|---------|-------------------|
| 0.005  |  $176.10  |    0.81%      |   132  |  51.5%  | identical         |
| 0.01   |  $352.21  |    1.60%      |   132  |  51.5%  | identical (baseline) |
| 0.015  |  $528.31  |    2.37%      |   132  |  51.5%  | identical         |
| 0.02   |  $704.41  |    3.12%      |   132  |  51.5%  | identical         |
| 0.03   | $1056.62  |    4.59%      |   132  |  51.5%  | identical         |

### Critical findings (non-obvious, falsify plan premise)

1. **Gate never binds across the full sweep.** At qty=0.03 and BTC ~$60k, notional is ~$1800 —
   well under the $5000 `max_notional_per_symbol` cap. Trade count, win rate, and per-regime
   Sharpe are **identical** across all qty values. The RiskGate is not a bottleneck at these
   sizing levels.
2. **Sharpe is scale-invariant.** Multiplying qty by k multiplies all per-trade returns by k,
   leaving Sharpe (ratio of mean to stdev) unchanged. PnL and MaxDD scale linearly with qty;
   Sharpe does not. There is no qty that "maximizes Sharpe" — the Fase 1C' objective as
   originally written is malformed.
3. **The only binding constraint is MaxDD<5% per regime.** Since MaxDD scales linearly,
   the decision reduces to picking the largest qty still under the ceiling. qty=0.03 at 4.59%
   is technically compliant but has zero headroom. qty=0.02 at 3.12% is the last safe step.

### Decision: NO config change this phase

- The sweep used `--qty` (fixed BTC), but `config/dry_run.yaml` and `configs/prod_symbols.yaml`
  use `notional_pct` (cash-relative). These sizing models are not interchangeable: backtest
  used constant BTC regardless of equity; live uses constant % of cash. Any mapping would be
  approximate and would shift meaning as equity drifts.
- The plan's escape hatch applies: "Si 0.01 sigue siendo mejor, no cambiar y documentar."
  Reframing: since Sharpe is invariant, "better" is only PnL/DD scaling — a risk-tolerance
  decision, not a strategy-level improvement. Should not be bundled into a strategy-optimization
  plan. Any `notional_pct` change must go through a separate risk-budget review with the user.
- Keeping current live sizing untouched: `prod_symbols.yaml notional_pct=0.005`,
  `dry_run.yaml notional_pct=0.01`.

### Output of this phase

Empirical confirmation that sizing is not a strategy-level lever at the current scale,
and falsification of the original plan premise that a qty sweep would reveal a clean winner.
Fases 4A/1A (stop-loss) and 1B' (strength-aware sizing) remain the right levers. Files
`results_qty_*.txt` committed for audit trail. No code or config modified.

---

## Technical debt documented (not fixed in this plan)

- **Intra-bar stop accuracy:** stops evaluated on `close`, not `low`/`high`. A bar
  that pierces the stop intra-bar but closes above it doesn't trigger. Preexisting
  in `momentum_breakout`. Fix requires extending `BacktestSignal` with `limit_price`.
- **Fee bug fix is backtest-only:** live fills use real exchange fees, no equivalent
  bug in execution path.
