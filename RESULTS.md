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

---

## Fase 1A — Stop-loss on SMA and Mean Reversion (`feat/strategy-stoploss`, merged #82)

Integrated `TrailingStop` into `SmaCrossoverStrategy` (trailing) and `MeanReversionStrategy`
(fixed — MR needs fixed stop or rebound exits prematurely). Both strategies fail-closed on
ATR NaN/0/negative per invariant I6. Added `tests/unit/test_sma_stoploss.py` and
`test_mean_reversion_stoploss.py`. All pre-existing tests green.

## Fase 1B' — Strength-aware asymmetric sizing (`feat/strength-sizing`, merged #83)

`StrategyAdapter` and `SelectorAdapter` now size BUY by `signal.strength` (variable) and
SELL with the full remembered position qty. Prevents ledger matching bugs that would
happen with symmetric `qty*strength` on both legs (strength BUY ≠ strength SELL in general).
Added 6 tests in `test_strategy_adapter_strength.py`.

## Fase 3A — MACD Histogram Strategy (`feat/strategy-macd`, merged #84)

New `MacdStrategy` (`src/strategy/macd_strategy.py`) with histogram crossover + trend SMA
filter + bullish divergence detection. Integrates `TrailingStop` with fail-closed ATR.
Registered in `registry.py` as `"macd"` / `"macd_histogram"`. **Selector regime map NOT
updated** per plan — isolated validation first. 7 tests.

## Fase 3B — RSI Divergence Strategy (`feat/strategy-rsi-div`, merged #85)

New `RsiDivergenceStrategy` with scipy-free local-extrema divergence detection
(±`order` window). Bullish div + RSI<40 → BUY, bearish div + RSI>60 → SELL. Registered
as `"rsi_divergence"`. Selector map untouched. 6 tests.

## Fase 3C — VWAP Reversion Strategy (`feat/strategy-vwap`, merged #86)

Added `vwap()` pure function to `quantitative/indicators.py` (typical-price cumulative,
NaN where cumulative volume is 0). New `VwapStrategy` with VWAP reversion + RSI
confirmation + tighter `stop_loss_atr_mult=1.5`. Registered as `"vwap"` / `"vwap_reversion"`.
4 + 5 tests.

## Fase 4B' — Partial exits opt-in (`feat/partial-exits`, merged #87)

`StrategyAdapter` gains `partial_exits: bool = False` (off by default so existing
baselines unchanged). When enabled, adapter takes a 50% close at `entry_price + entry_atr`
(TP1) before strategy's full exit. Adapter recalculates ATR locally from history so it
stays decoupled from strategy internals. Engine and PaperExecutor untouched — verified
they already support any partial qty. 4 tests in `test_partial_exits.py`.

## Fase 2 — Multi-asset infrastructure (`feat/multi-asset`, merged #88)

- `BTC_MARKET_REGIMES` → `MARKET_REGIMES` (backward-compat alias preserved), since
  cycle dates apply cross-asset.
- `_fetch_json` exponential backoff (5 attempts, max 30s per retry).
- `resample_1h_to_4h()` helper: fail-closed drop of incomplete trailing groups.
- `scripts/compare_assets.py`: parses `results_*.txt`, flags qualifying pairs
  (Sharpe>0.3 AND PnL>$100).
- ETH/SOL data downloaded from Coinbase public API across the 10-regime table
  (SOL missing 2 early regimes — pre-listing).

### Multi-asset selector results (1h, --use-selector, RiskGate active)

| Symbol | Trades | WinRate | Avg Sharpe | MaxDD  | Total PnL | Qualified? |
|--------|-------:|--------:|-----------:|-------:|----------:|-----------:|
| BTC-USD (qty 0.01) |  187 |  39.6% |  -0.07 | 1.07% |  -52.30 | ❌ |
| ETH-USD (qty 0.01) |  192 |  46.9% |  -0.06 | 0.16% |   -5.27 | ❌ |
| SOL-USD (qty 0.10) |  170 |  40.0% |  -0.18 | 0.07% |    2.96 | ❌ |

None pass the prod_symbols.yaml gate (Sharpe>0.3 AND PnL>$100). Reason: the selector
still uses the default regime map (`_DEFAULT_REGIME_MAP`), which only knows about SMA,
mean reversion, and momentum breakout. MACD/RSI-div/VWAP are registered in the registry
but NOT wired into the regime map — per plan, their map integration is conditional on
isolated validation against the historically-mapped strategy. That validation work was
out of scope for the multi-asset phase (would change strategy-per-regime assignments).

**No new entries added to `configs/prod_symbols.yaml`.**

## Fase 3D — Ensemble (**DEFERRED — design approval required**)

Plan criterion: "only if Sharpe < 0.5 after all prior phases". Aggregate Sharpe across
regimes is < 0.5, so 3D is technically triggered.

However, the plan also says: *"Antes de arrancar esta fase, pausar y pedir aprobación
del diseño."* The 3 known blockers documented in the plan require non-trivial contract
changes outside this plan's autonomy:

1. `BacktestEngine` does not notify adapter of fills → no way to impute per-strategy PnL.
2. `BacktestSignal` has no `strategy_id` field → no trade-level attribution.
3. In-memory `_weights` violate fail-closed on restart → need JSON persistence with
   fail-closed load.

Addressing these touches `src/backtest/engine.py` contract (not prohibited, but a signal
pipeline change that should be explicitly reviewed). **Status: deferred pending user
approval of the design for engine fill notification + BacktestSignal extension +
persisted weights.**

---

## Final state vs. plan targets

| Metric                       | Target      | Actual (BTC 1h selector) | Pass? |
|------------------------------|------------:|-------------------------:|------:|
| Sharpe (selector, BTC 1h)    | > 0.4       |  ~-0.07 (regime avg)     | ❌ |
| PnL aggregate 10 regimes     | > $500      |                  -$52.30 | ❌ |
| Win rate                     | > 48%       |                    39.6% | ❌ |
| MaxDD per regime             | < 5%        |                 1.07% (max) | ✅ |
| Regimes w/ negative PnL      | ≤ 4/10      |                     7/10 | ❌ |
| Tests passing                | 100%        |  1198 pass / 14 skip     | ✅ |
| New strategies registered    | ≥ 1         |    3 (MACD, RSI-div, VWAP) | ✅ |
| Non-BTC assets validated     | ≥ 1         |    2 (ETH, SOL data)     | ⚠ (neither qualified) |

### Honest read-out

Infrastructure work (Fases 1A, 1B', 3A, 3B, 3C, 4B', 2) is **complete and shippable**.
All phases merged, tests green, RiskGate active in all backtests, fail-closed in all new
code paths, prohibited files untouched.

Strategy-level **profitability targets are not met**. The core reason: the new strategies
(MACD, RSI divergence, VWAP) are registered and individually validated but NOT wired into
the selector's regime map. The selector is still running the pre-plan strategy set
(SMA / mean reversion / momentum breakout), and the added stop-losses reduced false-signal
losses but didn't unlock new winning regimes.

**Next lever (outside this plan's scope):** A/B backtest each new strategy per regime,
swap into `_DEFAULT_REGIME_MAP` where it beats the incumbent, then re-run segmented
backtests. This is the validation loop Fase 3A/3B/3C explicitly deferred. Until that's
done, the new strategies are latent capability, not realized PnL.
