"""
Fixtures compartidas para tests cuantitativos.

Genera datos sintéticos reproducibles para walk-forward, robustness, sensitivity.
"""

import math
import random
from decimal import Decimal
from typing import List

import pytest

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor


def generate_trending_bars(
    n: int = 200,
    start_price: float = 100.0,
    trend: float = 0.0005,
    volatility: float = 0.02,
    seed: int = 42,
) -> List[Bar]:
    """
    Generate synthetic OHLCV bars with configurable trend and volatility.

    Args:
        n: number of bars
        start_price: initial price
        trend: drift per bar (positive = uptrend)
        volatility: standard deviation of returns
        seed: random seed for reproducibility
    """
    rng = random.Random(seed)
    bars = []
    price = start_price

    for i in range(n):
        ret = trend + volatility * rng.gauss(0, 1)
        close = price * (1 + ret)
        high = close * (1 + abs(rng.gauss(0, volatility * 0.5)))
        low = close * (1 - abs(rng.gauss(0, volatility * 0.5)))
        open_ = price
        volume = 100 + rng.random() * 900

        bars.append(Bar(
            timestamp_ms=1_000_000_000_000 + i * 3_600_000,
            open=Decimal(str(round(open_, 2))),
            high=Decimal(str(round(high, 2))),
            low=Decimal(str(round(low, 2))),
            close=Decimal(str(round(close, 2))),
            volume=Decimal(str(round(volume, 2))),
        ))
        price = close

    return bars


def sma_crossover_strategy(
    bar: Bar,
    history: list,
    fast: int = 5,
    slow: int = 20,
    qty: Decimal = Decimal("1"),
    _state: dict | None = None,
):
    """SMA crossover strategy for testing. State managed via _state dict."""
    if _state is None:
        _state = {}
    closes = [b.close for b in history] + [bar.close]
    if len(closes) < slow:
        return None

    fast_sma = sum(closes[-fast:]) / Decimal(str(fast))
    slow_sma = sum(closes[-slow:]) / Decimal(str(slow))

    prev = [b.close for b in history]
    if len(prev) < slow:
        return None
    prev_fast = sum(prev[-fast:]) / Decimal(str(fast))
    prev_slow = sum(prev[-slow:]) / Decimal(str(slow))

    in_position = _state.get("in_position", False)

    if prev_fast <= prev_slow and fast_sma > slow_sma and not in_position:
        _state["in_position"] = True
        return Signal(side="BUY", qty=qty)

    if prev_fast >= prev_slow and fast_sma < slow_sma and in_position:
        _state["in_position"] = False
        return Signal(side="SELL", qty=qty)

    return None


def run_backtest(
    bars: List[Bar],
    initial_cash: Decimal = Decimal("10000"),
    fee_rate: Decimal = Decimal("0.001"),
    slippage_bps: Decimal = Decimal("0"),
    fast: int = 5,
    slow: int = 20,
    qty: Decimal = Decimal("1"),
) -> tuple:
    """
    Run a backtest with SMA crossover strategy.

    Returns:
        (report, ledger)
    """
    feed = HistoricalDataFeed.from_bars(bars)
    ledger = BacktestLedger(initial_cash=initial_cash)
    executor = PaperExecutor(ledger, slippage_bps=slippage_bps, fee_rate=fee_rate)

    state = {}

    def strategy(bar, history):
        return sma_crossover_strategy(bar, history, fast=fast, slow=slow, qty=qty, _state=state)

    engine = BacktestEngine(feed, ledger, executor, strategy)
    report = engine.run()
    return report, ledger


@pytest.fixture
def uptrend_bars():
    """200 bars with slight uptrend."""
    return generate_trending_bars(n=200, trend=0.001, seed=42)


@pytest.fixture
def downtrend_bars():
    """200 bars with downtrend."""
    return generate_trending_bars(n=200, trend=-0.001, seed=42)


@pytest.fixture
def flat_bars():
    """200 bars with no trend."""
    return generate_trending_bars(n=200, trend=0.0, volatility=0.01, seed=42)
