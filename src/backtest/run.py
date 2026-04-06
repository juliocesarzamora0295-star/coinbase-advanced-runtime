"""
CLI entry point para backtest.

Uso: python -m src.backtest.run --data data.csv [--cash 10000] [--fee 0.001] [--slippage 5]

Estrategia por defecto: SMA crossover (fast=10, slow=30).
"""

import argparse
import logging
import sys
from decimal import Decimal
from typing import List, Optional

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def sma(values: List[Decimal], period: int) -> Optional[Decimal]:
    """Simple moving average. None if not enough data."""
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / Decimal(str(period))


def sma_crossover_strategy(
    bar: Bar,
    history: list[Bar],
    fast_period: int = 10,
    slow_period: int = 30,
    qty: Decimal = Decimal("0.01"),
    position: list = [],  # mutable default as state container
) -> Optional[Signal]:
    """
    SMA crossover strategy.

    BUY when fast SMA crosses above slow SMA (and not already long).
    SELL when fast SMA crosses below slow SMA (and is long).
    """
    closes = [b.close for b in history] + [bar.close]

    fast = sma(closes, fast_period)
    slow = sma(closes, slow_period)

    if fast is None or slow is None:
        return None

    prev_closes = [b.close for b in history]
    prev_fast = sma(prev_closes, fast_period)
    prev_slow = sma(prev_closes, slow_period)

    if prev_fast is None or prev_slow is None:
        return None

    # Cross above: fast was below slow, now above
    if prev_fast <= prev_slow and fast > slow and not position:
        position.append(True)
        return Signal(side="BUY", qty=qty)

    # Cross below: fast was above slow, now below
    if prev_fast >= prev_slow and fast < slow and position:
        position.clear()
        return Signal(side="SELL", qty=qty)

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--data", required=True, help="Path to OHLCV CSV file")
    parser.add_argument("--cash", type=float, default=10000.0, help="Initial cash")
    parser.add_argument("--fee", type=float, default=0.001, help="Fee rate (0.001 = 0.1%%)")
    parser.add_argument("--slippage", type=float, default=0.0, help="Slippage in bps")
    args = parser.parse_args()

    feed = HistoricalDataFeed.from_csv(args.data)
    ledger = BacktestLedger(initial_cash=Decimal(str(args.cash)))
    executor = PaperExecutor(
        ledger=ledger,
        slippage_bps=Decimal(str(args.slippage)),
        fee_rate=Decimal(str(args.fee)),
    )

    # Reset mutable state for strategy
    position_state: list = []

    def strategy(bar: Bar, history: list[Bar]) -> Optional[Signal]:
        return sma_crossover_strategy(bar, history, position=position_state)

    engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=strategy)
    report = engine.run()

    print("\n" + str(report))


if __name__ == "__main__":
    main()
