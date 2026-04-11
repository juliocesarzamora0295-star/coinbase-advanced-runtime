"""
Tests for RsiDivergenceStrategy (Fase 3B).
"""

from decimal import Decimal
from typing import List

import pandas as pd

from src.strategy.rsi_divergence import (
    RsiDivergenceStrategy,
    _local_maxima,
    _local_minima,
)


def _ohlc_df(closes: List[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
        }
    )


def _make_strategy(
    rsi_period: int = 5,
    order: int = 2,
    lookback: int = 30,
    atr_mult: float = 2.0,
    oversold: float = 40.0,
    overbought: float = 60.0,
) -> RsiDivergenceStrategy:
    return RsiDivergenceStrategy(
        symbol="BTC-USD",
        config={
            "rsi_period": rsi_period,
            "rsi_oversold": oversold,
            "rsi_overbought": overbought,
            "divergence_order": order,
            "divergence_lookback": lookback,
            "stop_loss_atr_mult": atr_mult,
            "atr_period": 5,
        },
    )


def _feed(strategy: RsiDivergenceStrategy, df: pd.DataFrame) -> List:
    strategy.update_market_data(df)
    return strategy.generate_signals(mid=Decimal("100"))


def test_local_minima_detects_symmetric_low() -> None:
    s = pd.Series([10, 8, 6, 5, 6, 8, 10, 9, 7, 6, 5, 6, 7, 8], dtype=float)
    mins = _local_minima(s, order=2)
    # Valleys at index 3 (val=5) and index 10 (val=5).
    assert 3 in mins
    assert 10 in mins


def test_local_maxima_detects_symmetric_high() -> None:
    s = pd.Series([5, 7, 9, 10, 9, 7, 5, 6, 8, 10, 11, 10, 8, 6], dtype=float)
    maxs = _local_maxima(s, order=2)
    assert 3 in maxs
    assert 10 in maxs


def test_warmup_returns_no_signal() -> None:
    strategy = _make_strategy()
    signals = _feed(strategy, _ohlc_df([100.0] * 10))
    assert signals == []


def test_bullish_divergence_emits_buy() -> None:
    """
    Price lower-low + RSI higher-low + current RSI oversold → BUY.
    """
    pre = [105, 104, 103, 102, 101, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    seg1 = [100, 98, 94, 90, 88, 87, 86, 87, 89, 92, 95]  # first low at local idx 6
    seg2 = [94, 92, 89, 86, 84, 83, 82.5, 82, 81.5, 82, 83]  # deeper low at local idx 8
    closes = [float(x) for x in pre + seg1 + seg2]

    strat = _make_strategy(rsi_period=5, order=2, lookback=30, oversold=40.0)
    buy_fired = False
    for i in range(strat.warmup_bars, len(closes) + 1):
        sigs = _feed(strat, _ohlc_df(closes[:i]))
        if any(s.direction == "BUY" for s in sigs):
            buy_fired = True
            buy = [s for s in sigs if s.direction == "BUY"][0]
            assert buy.strength == Decimal("0.9")
            assert "bullish RSI divergence" in (buy.metadata.get("reason") or "")
            assert strat._trailing.is_active is True
            break
    assert buy_fired, "Expected bullish RSI divergence BUY"


def test_buy_suppressed_when_atr_nan() -> None:
    import numpy as np

    closes = [100, 98, 94, 90, 88, 87, 86, 87, 89, 92, 95,
              94, 92, 89, 86, 84, 83, 82.5, 83.5, 85, 88]
    strategy = _make_strategy(rsi_period=5, order=2, lookback=40, oversold=45.0)
    df = _ohlc_df([float(x) for x in closes])
    df.loc[df.index[-1], "high"] = np.nan
    df.loc[df.index[-1], "low"] = np.nan
    df.loc[df.index[-1], "close"] = np.nan
    strategy.update_market_data(df)
    signals = strategy.generate_signals(mid=Decimal("100"))
    assert [s for s in signals if s.direction == "BUY"] == []
    assert strategy._trailing.is_active is False


def test_registry_exposes_rsi_divergence() -> None:
    from src.strategy.registry import get_strategy_class

    assert get_strategy_class("rsi_divergence") is RsiDivergenceStrategy
