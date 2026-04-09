"""
Comparative backtest: SMA Fixed vs Regime-Adaptive Selector.

Generates synthetic data for each market regime, runs both strategies,
and produces a side-by-side comparison report.

Usage: python -m src.backtest.compare_strategies
"""

import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from src.backtest.data_feed import HistoricalDataFeed
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport
from src.backtest.strategy_adapter import FullAdaptiveAdapter, StrategyAdapter, SelectorAdapter
from src.backtest.synthetic_data import SYNTHETIC_REGIMES, generate_all_synthetic

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("CompareStrategies")


def _run_backtest(
    csv_path: Path,
    strategy_callback,
    cash: Decimal = Decimal("10000"),
    fee: Decimal = Decimal("0.001"),
    slippage: Decimal = Decimal("5"),
) -> Optional[BacktestReport]:
    """Run a single backtest and return report."""
    if not csv_path.exists():
        return None

    feed = HistoricalDataFeed.from_csv(csv_path)
    if len(feed) < 60:
        return None

    ledger = BacktestLedger(initial_cash=cash)
    executor = PaperExecutor(ledger=ledger, slippage_bps=slippage, fee_rate=fee)
    engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=strategy_callback)
    return engine.run()


def _run_backtest_adaptive(
    csv_path: Path,
    symbol: str,
    config: Dict,
    cash: Decimal = Decimal("10000"),
    fee: Decimal = Decimal("0.001"),
    slippage: Decimal = Decimal("5"),
) -> Optional[BacktestReport]:
    """Run backtest with FullAdaptiveAdapter (needs ledger access)."""
    if not csv_path.exists():
        return None

    feed = HistoricalDataFeed.from_csv(csv_path)
    if len(feed) < 60:
        return None

    ledger = BacktestLedger(initial_cash=cash)
    executor = PaperExecutor(ledger=ledger, slippage_bps=slippage, fee_rate=fee)
    adapter = FullAdaptiveAdapter(symbol=symbol, config=config, equity=cash, ledger=ledger)
    engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=adapter)
    return engine.run()


def run_comparison(data_dir: str = "data/synthetic") -> str:
    """Run full comparison and return formatted report."""
    # Generate synthetic data
    print("\n📊 Generating synthetic market data...")
    paths = generate_all_synthetic(data_dir)

    config_sma = {"sma_fast": 20, "sma_slow": 50}
    config_selector = {
        "sma_fast": 20, "sma_slow": 50,
        "bb_period": 20, "rsi_period": 14,
        "donchian_period": 20, "volume_ma_period": 20,
        "adx_period": 14,
        "min_regime_bars": 3,
    }

    results_sma: Dict[str, BacktestReport] = {}
    results_selector: Dict[str, BacktestReport] = {}
    results_full: Dict[str, BacktestReport] = {}

    cash = Decimal("10000")

    print("\nRunning backtests...\n")

    for label, csv_path in paths.items():
        desc = SYNTHETIC_REGIMES[label]["description"]
        print(f"  [{label}] {desc}")

        # SMA Fixed
        sma_adapter = StrategyAdapter.from_config(
            symbol="BTC-USD", config=config_sma, qty=Decimal("0.01"),
        )
        r_sma = _run_backtest(csv_path, sma_adapter, cash=cash)
        if r_sma:
            results_sma[label] = r_sma

        # Selector Adaptive
        sel_adapter = SelectorAdapter(
            symbol="BTC-USD", config=config_selector, qty=Decimal("0.01"),
        )
        r_sel = _run_backtest(csv_path, sel_adapter, cash=cash)
        if r_sel:
            results_selector[label] = r_sel

        # Full Adaptive (selector + adaptive sizing)
        r_full = _run_backtest_adaptive(
            csv_path, symbol="BTC-USD", config=config_selector, cash=cash,
        )
        if r_full:
            results_full[label] = r_full

        if r_sma and r_sel and r_full:
            sma_ret = float(r_sma.return_pct) * 100
            sel_ret = float(r_sel.return_pct) * 100
            full_ret = float(r_full.return_pct) * 100
            best = max(sma_ret, sel_ret, full_ret)
            winner = "SMA" if best == sma_ret else "SELECTOR" if best == sel_ret else "FULL"
            print(f"    SMA: {sma_ret:+.2f}%  |  SEL: {sel_ret:+.2f}%  |  FULL: {full_ret:+.2f}%  -> {winner}")

    # Build comparative report
    lines = []
    lines.append("")
    lines.append("=" * 140)
    lines.append("COMPARATIVE BACKTEST: SMA Fixed vs Selector vs Full Adaptive (Selector + Sizing)")
    lines.append("=" * 140)
    lines.append(
        f"{'Regime':<22} {'SMA Ret%':>9} {'SMA Trds':>8} {'SMA Shp':>7}  "
        f"{'SEL Ret%':>9} {'SEL Trds':>8} {'SEL Shp':>7}  "
        f"{'FULL Ret%':>9} {'FULL Trds':>9} {'FULL Shp':>8} {'Winner':>10}"
    )
    lines.append("-" * 140)

    sma_total_pnl = Decimal("0")
    sel_total_pnl = Decimal("0")
    full_total_pnl = Decimal("0")
    sma_wins = 0
    sel_wins = 0
    full_wins = 0

    for label in SYNTHETIC_REGIMES:
        r_sma = results_sma.get(label)
        r_sel = results_selector.get(label)
        r_full = results_full.get(label)

        if not r_sma or not r_sel or not r_full:
            lines.append(f"{label:<22} {'SKIP':>9}")
            continue

        sma_ret = float(r_sma.return_pct) * 100
        sel_ret = float(r_sel.return_pct) * 100
        full_ret = float(r_full.return_pct) * 100
        sma_total_pnl += r_sma.total_pnl
        sel_total_pnl += r_sel.total_pnl
        full_total_pnl += r_full.total_pnl

        best = max(sma_ret, sel_ret, full_ret)
        if best == full_ret and full_ret > sel_ret and full_ret > sma_ret:
            winner = "FULL"
            full_wins += 1
        elif best == sel_ret and sel_ret > sma_ret:
            winner = "SELECTOR"
            sel_wins += 1
        elif best == sma_ret and sma_ret > sel_ret:
            winner = "SMA"
            sma_wins += 1
        else:
            winner = "TIE"

        lines.append(
            f"{label:<22} {sma_ret:>+8.2f}% {r_sma.total_trades:>8} {r_sma.sharpe_ratio:>7.2f}  "
            f"{sel_ret:>+8.2f}% {r_sel.total_trades:>8} {r_sel.sharpe_ratio:>7.2f}  "
            f"{full_ret:>+8.2f}% {r_full.total_trades:>9} {r_full.sharpe_ratio:>8.2f} {winner:>10}"
        )

    lines.append("-" * 140)
    lines.append(
        f"{'TOTAL PnL':<22} ${float(sma_total_pnl):>+7.2f} {'':>8} {'':>7}  "
        f"${float(sel_total_pnl):>+7.2f} {'':>8} {'':>7}  "
        f"${float(full_total_pnl):>+7.2f} {'':>9} {'':>8} "
        f"{'SMA' if sma_wins >= sel_wins and sma_wins >= full_wins else 'SELECTOR' if sel_wins >= full_wins else 'FULL':>10}"
    )
    lines.append(f"{'Regime Wins':<22} {sma_wins:>8} {'':>8} {'':>7}  {sel_wins:>8} {'':>8} {'':>7}  {full_wins:>8}")
    lines.append("=" * 140)

    report = "\n".join(lines)
    print(report)
    return report


if __name__ == "__main__":
    run_comparison()
