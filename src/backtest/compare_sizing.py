"""
Comparative backtest: Fixed Sizing vs Adaptive Sizing.

Same strategy (SMA crossover), same data, only difference is
whether qty is fixed or dynamically adjusted by market conditions.

Usage: python -m src.backtest.compare_sizing
"""

import logging
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from src.backtest.data_feed import HistoricalDataFeed
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport
from src.backtest.strategy_adapter import AdaptiveAdapter, StrategyAdapter
from src.backtest.synthetic_data import SYNTHETIC_REGIMES, generate_all_synthetic

logging.basicConfig(level=logging.WARNING)


def _run(csv_path: Path, strategy_cb, cash=Decimal("10000")) -> Optional[BacktestReport]:
    if not csv_path.exists():
        return None
    feed = HistoricalDataFeed.from_csv(csv_path)
    if len(feed) < 60:
        return None
    ledger = BacktestLedger(initial_cash=cash)
    executor = PaperExecutor(ledger=ledger, slippage_bps=Decimal("5"), fee_rate=Decimal("0.001"))

    # If adapter has set_ledger, attach it
    if hasattr(strategy_cb, "set_ledger"):
        strategy_cb.set_ledger(ledger)

    engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=strategy_cb)
    return engine.run()


def main():
    data_dir = Path("data/synthetic")
    print("\n📊 Generating synthetic data...")
    paths = generate_all_synthetic(str(data_dir))

    config = {"sma_fast": 10, "sma_slow": 25}
    fixed_qty = Decimal("0.05")  # bigger qty to see real differences
    base_pct = Decimal("0.01")  # 1% base for adaptive

    results_fixed: Dict[str, BacktestReport] = {}
    results_adaptive: Dict[str, BacktestReport] = {}
    adaptive_logs: Dict[str, list] = {}

    print("\n🔄 Running backtests...\n")

    for label, csv_path in paths.items():
        desc = SYNTHETIC_REGIMES[label]["description"]
        print(f"  [{label}] {desc}")

        # Fixed sizing
        fixed = StrategyAdapter.from_config("BTC-USD", config, qty=fixed_qty)
        r_fixed = _run(csv_path, fixed)
        if r_fixed:
            results_fixed[label] = r_fixed

        # Adaptive sizing
        inner = StrategyAdapter.from_config("BTC-USD", config, qty=fixed_qty)
        adaptive = AdaptiveAdapter(
            inner=inner,
            equity=Decimal("10000"),
            base_pct=base_pct,
        )
        r_adaptive = _run(csv_path, adaptive)
        if r_adaptive:
            results_adaptive[label] = r_adaptive
            adaptive_logs[label] = adaptive.sizing_log

        if r_fixed and r_adaptive:
            f_ret = float(r_fixed.return_pct) * 100
            a_ret = float(r_adaptive.return_pct) * 100
            diff = a_ret - f_ret
            winner = "ADAPTIVE" if diff > 0 else "FIXED" if diff < 0 else "TIE"
            print(f"    Fixed: {f_ret:+.2f}%  |  Adaptive: {a_ret:+.2f}%  |  Δ: {diff:+.2f}%  → {winner}")

    # ── Detailed Report ──
    print()
    print("=" * 120)
    print("COMPARATIVE BACKTEST: Fixed Sizing vs Adaptive Sizing (SMA Crossover)")
    print("=" * 120)
    print(
        f"{'Regime':<22} "
        f"{'F.Ret%':>8} {'F.Trades':>8} {'F.Sharpe':>8} {'F.MaxDD':>8} {'F.PnL':>10} "
        f"{'A.Ret%':>8} {'A.Trades':>8} {'A.Sharpe':>8} {'A.MaxDD':>8} {'A.PnL':>10} "
        f"{'Winner':>10}"
    )
    print("-" * 120)

    f_total = Decimal("0")
    a_total = Decimal("0")
    f_wins = 0
    a_wins = 0

    for label in SYNTHETIC_REGIMES:
        rf = results_fixed.get(label)
        ra = results_adaptive.get(label)
        if not rf or not ra:
            print(f"{label:<22} SKIP")
            continue

        f_ret = float(rf.return_pct) * 100
        a_ret = float(ra.return_pct) * 100
        f_total += rf.total_pnl
        a_total += ra.total_pnl

        if a_ret > f_ret:
            winner = "ADAPTIVE"
            a_wins += 1
        elif f_ret > a_ret:
            winner = "FIXED"
            f_wins += 1
        else:
            winner = "TIE"

        print(
            f"{label:<22} "
            f"{f_ret:>+7.2f}% {rf.total_trades:>8} {rf.sharpe_ratio:>8.2f} {float(rf.max_drawdown)*100:>7.2f}% {float(rf.total_pnl):>+10.2f} "
            f"{a_ret:>+7.2f}% {ra.total_trades:>8} {ra.sharpe_ratio:>8.2f} {float(ra.max_drawdown)*100:>7.2f}% {float(ra.total_pnl):>+10.2f} "
            f"{winner:>10}"
        )

    print("-" * 120)
    print(
        f"{'TOTAL':>22} {'':>8} {'':>8} {'':>8} {'':>8} {float(f_total):>+10.2f} "
        f"{'':>8} {'':>8} {'':>8} {'':>8} {float(a_total):>+10.2f} "
        f"{'ADAPTIVE' if a_wins > f_wins else 'FIXED':>10}"
    )
    print(f"{'Regime Wins':>22} {f_wins:>8} {'':>8} {'':>8} {'':>8} {'':>10} {a_wins:>8}")
    print("=" * 120)

    # ── Sizing Factor Analysis ──
    print()
    print("=" * 80)
    print("ADAPTIVE SIZING FACTOR BREAKDOWN (per regime)")
    print("=" * 80)
    for label, log in adaptive_logs.items():
        if not log:
            continue
        avg_mult = sum(e["multiplier"] for e in log) / len(log)
        avg_vol = sum(e["vol_f"] for e in log) / len(log)
        avg_trend = sum(e["trend_f"] for e in log) / len(log)
        avg_volume = sum(e["volume_f"] for e in log) / len(log)
        avg_dd = sum(e["dd_f"] for e in log) / len(log)
        avg_pct = sum(e["effective_pct"] for e in log) / len(log)
        base_qty_avg = sum(e["base_qty"] for e in log) / len(log)
        adap_qty_avg = sum(e["adaptive_qty"] for e in log) / len(log)

        print(f"\n  {label} ({len(log)} signals):")
        print(f"    Avg multiplier: {avg_mult:.3f}")
        print(f"    Vol factor:     {avg_vol:.3f}  (>1 = low vol → bigger size)")
        print(f"    Trend factor:   {avg_trend:.3f}  (>1 = strong trend → bigger size)")
        print(f"    Volume factor:  {avg_volume:.3f}  (>1 = high volume → bigger size)")
        print(f"    DD factor:      {avg_dd:.3f}  (<1 = in drawdown → smaller size)")
        print(f"    Avg eff. pct:   {avg_pct:.5f}  (base: {float(base_pct):.3f})")
        print(f"    Avg qty:        {adap_qty_avg:.6f}  (fixed was: {base_qty_avg:.6f})")
    print()


if __name__ == "__main__":
    main()
