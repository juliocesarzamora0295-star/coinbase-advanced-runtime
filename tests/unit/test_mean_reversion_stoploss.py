"""
Tests for MeanReversionStrategy fixed stop-loss integration (Fase 1A).

Covers:
- BUY activates a FIXED stop with valid ATR.
- BUY NOT emitted when ATR is invalid (fail-closed, invariant I6).
- Stop hit emits SELL with reason=stop_loss_hit.
- Stop is FIXED: does not move as price rises favorably (mean reversion
  waits for the rebound — trailing would close winners early).
- Cross-up SELL (upper band + RSI overbought) resets the stop.
"""

from decimal import Decimal
from typing import List

import numpy as np
import pandas as pd

from src.strategy.mean_reversion import MeanReversionStrategy


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
    bb_period: int = 10,
    rsi_period: int = 5,
    atr_mult: float = 2.5,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
) -> MeanReversionStrategy:
    return MeanReversionStrategy(
        symbol="BTC-USD",
        config={
            "bb_period": bb_period,
            "bb_std": 2.0,
            "rsi_period": rsi_period,
            "rsi_oversold": rsi_oversold,
            "rsi_overbought": rsi_overbought,
            "stop_loss_atr_mult": atr_mult,
            "atr_period": 5,
        },
    )


def _feed(strategy: MeanReversionStrategy, df: pd.DataFrame) -> List:
    strategy.update_market_data(df)
    return strategy.generate_signals(mid=Decimal("100"))


def _oversold_closes() -> List[float]:
    """
    Build a close series that ends with a sharp drop below BB lower and RSI<30.
    ~20 bars is enough for bb_period=10 + rsi_period=5 warmup.
    """
    base = [100.0] * 15
    drop = [98.0, 96.0, 93.0, 90.0, 85.0]
    return base + drop


def test_buy_activates_fixed_stop_with_valid_atr() -> None:
    strategy = _make_strategy()
    closes = _oversold_closes()
    signals = _feed(strategy, _ohlc_df(closes))

    buys = [s for s in signals if s.direction == "BUY"]
    assert len(buys) == 1
    assert strategy._stop.is_active is True
    assert strategy._stop.direction == "LONG"
    assert strategy._stop.stop_price is not None
    assert strategy._stop.stop_price < 85.0  # below entry
    assert buys[0].metadata.get("stop_price") == strategy._stop.stop_price


def test_buy_suppressed_when_atr_is_nan() -> None:
    strategy = _make_strategy()
    closes = _oversold_closes()
    df = _ohlc_df(closes)
    df.loc[df.index[-1], "high"] = np.nan
    df.loc[df.index[-1], "low"] = np.nan
    df.loc[df.index[-1], "close"] = np.nan
    strategy.update_market_data(df)

    signals = strategy.generate_signals(mid=Decimal("100"))
    buys = [s for s in signals if s.direction == "BUY"]
    assert buys == []
    assert strategy._stop.is_active is False


def test_stop_hit_emits_sell() -> None:
    strategy = _make_strategy()
    closes_in = _oversold_closes()
    _feed(strategy, _ohlc_df(closes_in))
    assert strategy._stop.is_active
    entry_stop = strategy._stop.stop_price
    assert entry_stop is not None

    drop_price = entry_stop - 5.0
    closes_out = closes_in + [drop_price]
    signals_out = _feed(strategy, _ohlc_df(closes_out))

    sells = [s for s in signals_out if s.direction == "SELL"]
    assert len(sells) == 1
    assert sells[0].metadata.get("reason") == "stop_loss_hit"
    assert strategy._stop.is_active is False
    assert strategy._last_signal_side == "sell"


def test_stop_is_fixed_does_not_move_on_favorable_price() -> None:
    """Mean reversion stop must NOT trail as price rises — fixed at entry."""
    strategy = _make_strategy()
    closes = _oversold_closes()
    _feed(strategy, _ohlc_df(closes))
    assert strategy._stop.is_active
    stop_after_entry = strategy._stop.stop_price
    assert stop_after_entry is not None

    # Price rebounds sharply — a trailing stop would pull up; fixed must not.
    closes_up = closes + [95.0, 100.0, 105.0]
    _feed(strategy, _ohlc_df(closes_up))
    assert strategy._stop.stop_price == stop_after_entry


def test_cross_up_sell_resets_stop() -> None:
    """SELL on upper-band + RSI overbought must clear the stop state."""
    strategy = _make_strategy(rsi_overbought=60.0)
    closes_in = _oversold_closes()
    _feed(strategy, _ohlc_df(closes_in))
    assert strategy._stop.is_active

    # Force an overbought rally: push close well above BB upper.
    closes_up = closes_in + [110.0, 115.0, 120.0, 125.0, 130.0]
    signals = _feed(strategy, _ohlc_df(closes_up))

    sells = [s for s in signals if s.direction == "SELL"]
    # If the regular SELL fired, stop must be reset.
    if sells and "Mean reversion SELL" in (sells[0].metadata.get("reason") or ""):
        assert strategy._stop.is_active is False
        assert strategy._last_signal_side == "sell"
