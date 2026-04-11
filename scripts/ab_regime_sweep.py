"""
A/B sweep: compare incumbent vs candidate strategies standalone per regime.

Runs each strategy as a fixed single-strategy adapter (NOT through the
selector) across the 10 BTC regimes, collects Sharpe/PnL/trades, and prints
a comparative table plus swap recommendations grouped by regime family.

Usage:
    python -m scripts.ab_regime_sweep [--data-dir data/regimes] [--qty 0.01]
"""

import argparse
import csv
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

from src.backtest.data_downloader import MARKET_REGIMES, parse_regime_dates
from src.backtest.data_feed import HistoricalDataFeed
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.risk_adapter import BacktestRiskAdapter
from src.backtest.strategy_adapter import StrategyAdapter
from src.strategy.registry import get_strategy_class

# Regime family groupings (informed by historical price action).
REGIME_FAMILY: Dict[str, str] = {
    "bull_2020_q4": "trending",
    "bull_2021_q1": "trending",
    "crash_may_2021": "trending",  # strong down-trend
    "recovery_2021_q3": "trending",
    "bear_2022_h1": "trending",  # sustained down-trend
    "sideways_2022_h2": "ranging",
    "recovery_2023": "trending",
    "pre_etf_2023_h2": "trending",
    "etf_bull_2024_q1": "trending",
    "consolidation_2024_q2q3": "ranging",
}

# Candidate matchups: regime_family → (incumbent, candidate)
MATCHUPS = {
    "TRENDING_CALM (SMA vs MACD)": ("sma_crossover", "macd"),
    "TRENDING_VOLATILE (momentum vs MACD)": ("momentum_breakout", "macd"),
    "RANGING_CALM (MR vs VWAP)": ("mean_reversion", "vwap"),
    "RANGING_VOLATILE (MR vs RSI-div)": ("mean_reversion", "rsi_divergence"),
}

STRATEGY_CONFIGS: Dict[str, Dict] = {
    "sma_crossover": {"sma_fast": 20, "sma_slow": 50, "stop_loss_atr_mult": 2.0},
    "mean_reversion": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                       "rsi_oversold": 30, "rsi_overbought": 70,
                       "stop_loss_atr_mult": 2.5},
    "momentum_breakout": {"donchian_period": 20, "volume_ma_period": 20,
                          "atr_period": 14, "stop_loss_atr_mult": 2.0},
    "macd": {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
             "trend_sma_period": 50, "stop_loss_atr_mult": 2.0, "atr_period": 14},
    "rsi_divergence": {"rsi_period": 14, "rsi_oversold": 40, "rsi_overbought": 60,
                       "divergence_order": 5, "divergence_lookback": 30,
                       "stop_loss_atr_mult": 2.0, "atr_period": 14},
    "vwap": {"vwap_threshold": 0.005, "rsi_period": 14, "rsi_oversold": 40,
             "rsi_overbought": 60, "stop_loss_atr_mult": 1.5, "atr_period": 14},
}


def run_strategy_on_regime(
    strategy_name: str,
    csv_path: Path,
    symbol: str,
    qty: Decimal,
    cash: Decimal,
) -> Tuple[float, float, int]:
    """Return (sharpe, pnl, trades) for one strategy on one regime CSV."""
    cls = get_strategy_class(strategy_name)
    if cls is None:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    strategy = cls(symbol=symbol, config=STRATEGY_CONFIGS[strategy_name])
    feed = HistoricalDataFeed.from_csv(csv_path)
    if len(feed) < 60:
        return (0.0, 0.0, 0)

    ledger = BacktestLedger(initial_cash=cash)
    executor = PaperExecutor(ledger=ledger, slippage_bps=Decimal("5"),
                             fee_rate=Decimal("0.001"))
    adapter = StrategyAdapter(strategy=strategy, qty=qty)
    risk_adapter = BacktestRiskAdapter.from_config(ledger=ledger, symbol=symbol)

    engine = BacktestEngine(
        feed=feed, ledger=ledger, executor=executor,
        strategy=adapter, risk_adapter=risk_adapter,
    )
    report = engine.run()
    return (report.sharpe_ratio, float(report.total_pnl), report.total_trades)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/regimes")
    ap.add_argument("--symbol", default="BTC-USD")
    ap.add_argument("--qty", type=float, default=0.01)
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--csv-out", default="ab_regime_sweep.csv")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    qty = Decimal(str(args.qty))
    cash = Decimal(str(args.cash))

    # Pre-compute unique strategies across matchups
    unique_strats = sorted({s for pair in MATCHUPS.values() for s in pair})

    # results[strategy][regime_label] = (sharpe, pnl, trades)
    results: Dict[str, Dict[str, Tuple[float, float, int]]] = {
        s: {} for s in unique_strats
    }

    for regime in MARKET_REGIMES:
        label, _, _, _ = parse_regime_dates(regime)
        csv_path = data_dir / f"{label}.csv"
        if not csv_path.exists():
            print(f"  SKIP: {label} (no CSV)")
            continue
        for strat in unique_strats:
            try:
                sharpe, pnl, trades = run_strategy_on_regime(
                    strat, csv_path, args.symbol, qty, cash,
                )
                results[strat][label] = (sharpe, pnl, trades)
                print(f"  {strat:<20s} {label:<28s} sharpe={sharpe:+.2f} pnl={pnl:+8.2f} trades={trades}")
            except Exception as exc:
                print(f"  ERROR {strat} on {label}: {exc}")
                results[strat][label] = (0.0, 0.0, 0)

    # Write CSV
    with open(args.csv_out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "regime", "sharpe", "pnl", "trades"])
        for strat, per_regime in results.items():
            for label, (sh, pnl, tr) in per_regime.items():
                w.writerow([strat, label, f"{sh:.4f}", f"{pnl:.2f}", tr])
    print(f"\nWrote {args.csv_out}")

    # A/B matchups by regime family
    print()
    print("=" * 92)
    print("A/B MATCHUPS BY REGIME FAMILY")
    print("=" * 92)

    decisions: Dict[str, str] = {}
    for matchup_label, (incumbent, candidate) in MATCHUPS.items():
        family_needed = "trending" if "TRENDING" in matchup_label else "ranging"
        print(f"\n{matchup_label}  (family: {family_needed})")
        print(f"  {'regime':<28s} {incumbent:>16s}  {candidate:>16s}  winner")
        print("  " + "-" * 80)

        inc_wins = 0
        cand_wins = 0
        for label, family in REGIME_FAMILY.items():
            if family != family_needed:
                continue
            inc = results.get(incumbent, {}).get(label)
            cand = results.get(candidate, {}).get(label)
            if inc is None or cand is None:
                continue
            inc_sharpe = inc[0]
            cand_sharpe = cand[0]
            winner = candidate if cand_sharpe > inc_sharpe else incumbent
            if cand_sharpe > inc_sharpe:
                cand_wins += 1
            else:
                inc_wins += 1
            marker = "◄" if winner == candidate else " "
            print(f"  {label:<28s} {inc_sharpe:+16.2f}  {cand_sharpe:+16.2f}  {winner} {marker}")

        total = inc_wins + cand_wins
        verdict = candidate if cand_wins > inc_wins else incumbent
        decisions[matchup_label] = verdict
        print(f"  TALLY: {incumbent}={inc_wins}  {candidate}={cand_wins}  →  USE: {verdict}")

    print()
    print("=" * 92)
    print("SWAP DECISIONS")
    print("=" * 92)
    for m, v in decisions.items():
        print(f"  {m}: {v}")


if __name__ == "__main__":
    main()
