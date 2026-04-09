"""
Segmented backtest runner — runs the same strategy across multiple
market regimes and produces a comparative report.

Usage:
    python -m src.backtest.segmented_runner \\
        --symbol BTC-USD --granularity 1h --cash 10000 \\
        [--use-risk] [--download]

With --download: fetches data from Coinbase for each regime.
Without --download: expects CSV files in data/ directory named {label}.csv.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from src.backtest.data_feed import HistoricalDataFeed
from src.backtest.data_downloader import BTC_MARKET_REGIMES, parse_regime_dates
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport
from src.backtest.risk_adapter import BacktestRiskAdapter
from src.backtest.strategy_adapter import FullAdaptiveAdapter, SelectorAdapter, StrategyAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("SegmentedRunner")


def run_single_regime(
    csv_path: Path,
    symbol: str,
    strategy_config: Dict,
    cash: Decimal,
    fee_rate: Decimal,
    slippage_bps: Decimal,
    qty: Decimal,
    use_risk: bool = False,
    use_selector: bool = False,
    use_full_adaptive: bool = False,
) -> Optional[BacktestReport]:
    """
    Run backtest on a single CSV file (one market regime).

    Returns BacktestReport or None if data insufficient.
    """
    if not csv_path.exists():
        logger.warning("CSV not found: %s — skipping", csv_path)
        return None

    feed = HistoricalDataFeed.from_csv(csv_path)
    if len(feed) < 60:
        logger.warning("Only %d bars in %s — skipping (need ≥60)", len(feed), csv_path)
        return None

    ledger = BacktestLedger(initial_cash=cash)
    executor = PaperExecutor(
        ledger=ledger,
        slippage_bps=slippage_bps,
        fee_rate=fee_rate,
    )

    if use_full_adaptive:
        adapter = FullAdaptiveAdapter(
            symbol=symbol, config=strategy_config, equity=cash, ledger=ledger,
        )
    elif use_selector:
        adapter = SelectorAdapter(symbol=symbol, config=strategy_config, qty=qty)
    else:
        adapter = StrategyAdapter.from_config(
            symbol=symbol,
            config=strategy_config,
            qty=qty,
        )

    risk_adapter = None
    if use_risk:
        risk_adapter = BacktestRiskAdapter.from_config(
            ledger=ledger,
            symbol=symbol,
        )

    engine = BacktestEngine(
        feed=feed,
        ledger=ledger,
        executor=executor,
        strategy=adapter,
        risk_adapter=risk_adapter,
    )

    return engine.run()


def run_all_regimes(
    data_dir: Path,
    symbol: str,
    strategy_config: Dict,
    cash: Decimal,
    fee_rate: Decimal,
    slippage_bps: Decimal,
    qty: Decimal,
    use_risk: bool = False,
    use_selector: bool = False,
    use_full_adaptive: bool = False,
    regimes: Optional[List] = None,
) -> Dict[str, BacktestReport]:
    """
    Run backtest across all market regimes.

    Args:
        data_dir: Directory containing CSV files named {label}.csv
        regimes: List of regime tuples. Defaults to BTC_MARKET_REGIMES.

    Returns:
        Dict mapping regime label to BacktestReport.
    """
    if regimes is None:
        regimes = BTC_MARKET_REGIMES

    results: Dict[str, BacktestReport] = {}

    for regime in regimes:
        label, start_dt, end_dt, desc = parse_regime_dates(regime)
        csv_path = data_dir / f"{label}.csv"

        logger.info("═" * 60)
        logger.info("Regime: %s — %s", label, desc)
        logger.info("Period: %s to %s", start_dt.date(), end_dt.date())

        report = run_single_regime(
            csv_path=csv_path,
            symbol=symbol,
            strategy_config=strategy_config,
            cash=cash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            qty=qty,
            use_risk=use_risk,
            use_selector=use_selector,
            use_full_adaptive=use_full_adaptive,
        )

        if report is not None:
            results[label] = report
            logger.info("  Return: %.2f%% | Trades: %d | Sharpe: %.2f | MaxDD: %.2f%%",
                float(report.return_pct) * 100,
                report.total_trades,
                report.sharpe_ratio,
                float(report.max_drawdown) * 100,
            )
        else:
            logger.info("  SKIPPED — no data or insufficient bars")

    return results


def print_comparative_report(results: Dict[str, BacktestReport]) -> str:
    """
    Print formatted comparative report across regimes.

    Returns the report as string.
    """
    if not results:
        return "No results to report."

    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append("SEGMENTED BACKTEST REPORT — Comparative Results")
    lines.append("=" * 90)
    lines.append(
        f"{'Regime':<28} {'Return':>8} {'Trades':>7} {'WinRate':>8} "
        f"{'Sharpe':>7} {'MaxDD':>7} {'PnL':>10} {'Fees':>8}"
    )
    lines.append("-" * 90)

    total_pnl = Decimal("0")
    total_trades = 0
    total_wins = 0

    for label, r in results.items():
        total_pnl += r.total_pnl
        total_trades += r.total_trades
        total_wins += r.winning_trades

        lines.append(
            f"{label:<28} {float(r.return_pct):>7.2%} {r.total_trades:>7} "
            f"{float(r.win_rate):>7.1%} {r.sharpe_ratio:>7.2f} "
            f"{float(r.max_drawdown):>6.2%} {float(r.total_pnl):>10.2f} "
            f"{float(r.fees_paid):>8.2f}"
        )

    lines.append("-" * 90)
    overall_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    lines.append(
        f"{'AGGREGATE':<28} {'':>8} {total_trades:>7} {overall_wr:>7.1f}% "
        f"{'':>7} {'':>7} {float(total_pnl):>10.2f}"
    )
    lines.append("=" * 90)

    report_str = "\n".join(lines)
    print(report_str)
    return report_str


def main() -> None:
    parser = argparse.ArgumentParser(description="Segmented backtest runner")
    parser.add_argument("--symbol", default="BTC-USD", help="Trading pair")
    parser.add_argument("--granularity", default="1h", help="Candle timeframe")
    parser.add_argument("--cash", type=float, default=10000.0, help="Initial cash per regime")
    parser.add_argument("--fee", type=float, default=0.001, help="Fee rate")
    parser.add_argument("--slippage", type=float, default=5.0, help="Slippage bps")
    parser.add_argument("--qty", type=float, default=0.01, help="Trade qty in BTC")
    parser.add_argument("--sma-fast", type=int, default=20, help="Fast SMA period")
    parser.add_argument("--sma-slow", type=int, default=50, help="Slow SMA period")
    parser.add_argument("--use-risk", action="store_true", help="Enable RiskGate in backtest")
    parser.add_argument("--use-selector", action="store_true", help="Use regime-aware SelectorAdapter instead of SMA crossover")
    parser.add_argument("--use-full-adaptive", action="store_true", help="Use regime selector + adaptive position sizing")
    parser.add_argument("--download", action="store_true", help="Download data from Coinbase")
    parser.add_argument("--data-dir", default="data/regimes", help="Directory for CSV files")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    strategy_config = {
        "sma_fast": args.sma_fast,
        "sma_slow": args.sma_slow,
    }

    if args.download:
        from src.backtest.data_downloader import download_coinbase, parse_regime_dates

        for regime in BTC_MARKET_REGIMES:
            label, start_dt, end_dt, desc = parse_regime_dates(regime)
            csv_path = data_dir / f"{label}.csv"
            if csv_path.exists():
                logger.info("Already exists: %s — skipping download", csv_path)
                continue
            try:
                download_coinbase(
                    product_id=args.symbol,
                    granularity=args.granularity,
                    start=start_dt,
                    end=end_dt,
                    output_path=csv_path,
                )
            except Exception as exc:
                logger.error("Failed to download %s: %s", label, exc)

    results = run_all_regimes(
        data_dir=data_dir,
        symbol=args.symbol,
        strategy_config=strategy_config,
        cash=Decimal(str(args.cash)),
        fee_rate=Decimal(str(args.fee)),
        slippage_bps=Decimal(str(args.slippage)),
        qty=Decimal(str(args.qty)),
        use_risk=args.use_risk,
        use_selector=args.use_selector,
        use_full_adaptive=args.use_full_adaptive,
    )

    print_comparative_report(results)


if __name__ == "__main__":
    main()
