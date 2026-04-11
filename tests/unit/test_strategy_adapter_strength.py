"""
Tests for asymmetric strength-based sizing in StrategyAdapter / SelectorAdapter
(Fase 1B').

BUY qty scales with signal.strength. SELL qty closes the full recorded position
(symmetric on strength would break ledger matching because strength at BUY time
is not the same as at SELL time).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

import pandas as pd

from src.backtest.data_feed import Bar
from src.backtest.strategy_adapter import (
    FullAdaptiveAdapter,
    SelectorAdapter,
    StrategyAdapter,
)
from src.strategy.base import Strategy
from src.strategy.signal import Signal, make_signal


class _ScriptedStrategy(Strategy):
    """Emits a pre-scripted sequence of signals, one per call."""

    def __init__(self, symbol: str, script: List[Optional[Signal]]) -> None:
        super().__init__(symbol=symbol, config={})
        self._script = list(script)
        self._i = 0

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        self._df = market_data

    def generate_signals(self, *, mid: Decimal, bar_timestamp=None) -> List[Signal]:
        if self._i >= len(self._script):
            return []
        s = self._script[self._i]
        self._i += 1
        return [s] if s is not None else []

    def update_positions(self, fill) -> None:
        return None


def _bar(ts: int = 1_700_000_000_000, close: float = 100.0) -> Bar:
    return Bar(
        timestamp_ms=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
    )


def _sig(direction: str, strength: Decimal) -> Signal:
    return make_signal(
        symbol="BTC-USD",
        direction=direction,  # type: ignore[arg-type]
        strength=strength,
        strategy_id="scripted",
        bar_timestamp=datetime.now(tz=timezone.utc),
    )


def test_buy_scales_by_strength_and_sell_matches_buy_qty() -> None:
    """BUY strength=0.7 qty=0.01 → 0.007. SELL must close exactly 0.007."""
    buy = _sig("BUY", Decimal("0.7"))
    sell = _sig("SELL", Decimal("0.3"))  # different strength on purpose
    strategy = _ScriptedStrategy("BTC-USD", [buy, sell])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.01"))

    out_buy = adapter(_bar(), [])
    assert out_buy is not None
    assert out_buy.side == "BUY"
    assert out_buy.qty == Decimal("0.01") * Decimal("0.7")

    out_sell = adapter(_bar(ts=1_700_000_060_000), [_bar()])
    assert out_sell is not None
    assert out_sell.side == "SELL"
    # SELL qty must match BUY qty, NOT be rescaled by SELL's strength.
    assert out_sell.qty == Decimal("0.01") * Decimal("0.7")


def test_strength_one_uses_full_qty() -> None:
    buy = _sig("BUY", Decimal("1.0"))
    strategy = _ScriptedStrategy("BTC-USD", [buy])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.01"))

    out = adapter(_bar(), [])
    assert out is not None
    assert out.qty == Decimal("0.01")


def test_tiny_strength_rejected_no_entry() -> None:
    """qty * strength < 0.0001 → None, no position opened."""
    buy = _sig("BUY", Decimal("0.001"))  # 0.01 * 0.001 = 0.00001 < 0.0001
    strategy = _ScriptedStrategy("BTC-USD", [buy])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.01"))

    out = adapter(_bar(), [])
    assert out is None
    assert adapter._in_position is False
    assert adapter._position_qty == Decimal("0")


def test_no_double_entry_when_already_in_position() -> None:
    buy1 = _sig("BUY", Decimal("0.7"))
    buy2 = _sig("BUY", Decimal("0.9"))
    strategy = _ScriptedStrategy("BTC-USD", [buy1, buy2])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.01"))

    out1 = adapter(_bar(), [])
    out2 = adapter(_bar(ts=1_700_000_060_000), [_bar()])
    assert out1 is not None
    assert out2 is None
    # Position qty unchanged from first BUY.
    assert adapter._position_qty == Decimal("0.01") * Decimal("0.7")


def test_sell_without_position_is_ignored() -> None:
    sell = _sig("SELL", Decimal("1.0"))
    strategy = _ScriptedStrategy("BTC-USD", [sell])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.01"))

    out = adapter(_bar(), [])
    assert out is None
    assert adapter._position_qty == Decimal("0")


def test_full_adaptive_adapter_unchanged_contract() -> None:
    """
    FullAdaptiveAdapter wraps SelectorAdapter and applies its own sizing.
    It should still produce Optional[BacktestSignal] and leave the new
    _position_qty field accessible on the inner selector.
    """
    adapter = FullAdaptiveAdapter(
        symbol="BTC-USD",
        config={"sma_fast": 5, "sma_slow": 10},
        equity=Decimal("10000"),
    )
    # With empty history the selector should return None (no signal yet).
    out = adapter(_bar(), [])
    assert out is None
    # Inner selector carries the new field.
    assert hasattr(adapter._selector, "_position_qty")
    assert adapter._selector._position_qty == Decimal("0")
