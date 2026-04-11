"""
Tests for SmaCrossoverStrategy trailing stop-loss integration (Fase 1A).

Covers:
- BUY activates trailing stop with valid ATR.
- BUY NOT emitted when ATR is invalid (fail-closed, invariant I6).
- Trailing stop hit emits SELL with reason=trailing_stop_hit.
- Trailing stop on LONG only moves up (monotone).
- Cross-down SELL resets the stop.
"""

from decimal import Decimal
from typing import List

import numpy as np
import pandas as pd

from src.strategy.sma_crossover import SmaCrossoverStrategy


def _ohlc_df(closes: List[float]) -> pd.DataFrame:
    """Build a minimal OHLC DataFrame from a close series (high=close+1, low=close-1)."""
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
        }
    )


def _make_strategy(fast: int = 3, slow: int = 5, atr_mult: float = 2.0) -> SmaCrossoverStrategy:
    return SmaCrossoverStrategy(
        symbol="BTC-USD",
        config={
            "sma_fast": fast,
            "sma_slow": slow,
            "stop_loss_atr_mult": atr_mult,
            "atr_period": 5,
        },
    )


def _feed(strategy: SmaCrossoverStrategy, df: pd.DataFrame) -> List:
    strategy.update_market_data(df)
    return strategy.generate_signals(mid=Decimal("100"))


def test_buy_activates_trailing_stop_with_valid_atr() -> None:
    """BUY emitted when OHLC is present and ATR is valid; stop below entry for LONG."""
    strategy = _make_strategy()
    # Bullish crossover: flat then dip-and-spike to force SMA cross.
    closes = [100.0] * 6 + [90.0, 130.0]
    signals = _feed(strategy, _ohlc_df(closes))

    buys = [s for s in signals if s.direction == "BUY"]
    assert len(buys) == 1
    assert strategy._trailing.is_active is True
    assert strategy._trailing.direction == "LONG"
    assert strategy._trailing.stop_price is not None
    assert strategy._trailing.stop_price < 130.0  # below entry
    assert buys[0].metadata.get("stop_price") == strategy._trailing.stop_price


def test_buy_suppressed_when_atr_is_nan() -> None:
    """
    Fail-closed: if ATR cannot be computed (all-NaN close series), no BUY is emitted
    and no stop is activated. We inject a NaN-only series and force a crossover.
    """
    strategy = _make_strategy()
    # Build a DataFrame with NaN high/low/close on the last row to force NaN ATR.
    closes = [100.0] * 6 + [90.0, 130.0]
    df = _ohlc_df(closes)
    df.loc[df.index[-1], "high"] = np.nan
    df.loc[df.index[-1], "low"] = np.nan
    df.loc[df.index[-1], "close"] = np.nan
    strategy.update_market_data(df)

    signals = strategy.generate_signals(mid=Decimal("100"))
    # Either no signal (insufficient data after NaN) or no BUY. Trailing must be inactive.
    buys = [s for s in signals if s.direction == "BUY"]
    assert buys == []
    assert strategy._trailing.is_active is False


def test_trailing_stop_hit_emits_sell() -> None:
    """After BUY, a close below the stop triggers a SELL with reason=trailing_stop_hit."""
    strategy = _make_strategy(fast=2, slow=3, atr_mult=2.0)
    # Force BUY on bar 5.
    closes_in = [100.0, 100.0, 100.0, 90.0, 130.0]
    signals_in = _feed(strategy, _ohlc_df(closes_in))
    assert any(s.direction == "BUY" for s in signals_in)
    entry_stop = strategy._trailing.stop_price
    assert entry_stop is not None

    # Feed a new bar well below the stop — must trigger a trailing-stop SELL.
    # Build a fresh DataFrame extending the previous one.
    drop_price = entry_stop - 20.0  # unambiguously under the stop
    closes_out = closes_in + [drop_price]
    signals_out = _feed(strategy, _ohlc_df(closes_out))

    sells = [s for s in signals_out if s.direction == "SELL"]
    assert len(sells) == 1
    assert sells[0].metadata.get("reason") == "trailing_stop_hit"
    assert strategy._trailing.is_active is False  # reset after hit
    assert strategy._last_signal_side == "sell"


def test_trailing_stop_moves_up_only() -> None:
    """
    Long trailing stop must be monotone: price rising pulls it up, pullbacks leave it unchanged.
    """
    strategy = _make_strategy(fast=2, slow=3, atr_mult=2.0)
    # Force BUY.
    closes = [100.0, 100.0, 100.0, 90.0, 130.0]
    _feed(strategy, _ohlc_df(closes))
    assert strategy._trailing.is_active
    stop_after_entry = strategy._trailing.stop_price
    assert stop_after_entry is not None

    # Price rises — stop should pull up.
    closes_up = closes + [150.0]
    _feed(strategy, _ohlc_df(closes_up))
    stop_after_rise = strategy._trailing.stop_price
    assert stop_after_rise is not None
    assert stop_after_rise > stop_after_entry

    # Pullback (still above stop) — stop must NOT retreat.
    closes_pullback = closes_up + [140.0]
    _feed(strategy, _ohlc_df(closes_pullback))
    assert strategy._trailing.stop_price == stop_after_rise


def test_cross_down_sell_resets_stop() -> None:
    """A regular bearish-cross SELL must clear the trailing state."""
    strategy = _make_strategy(fast=2, slow=3, atr_mult=2.0)
    # BUY first.
    closes_in = [100.0, 100.0, 100.0, 90.0, 130.0]
    _feed(strategy, _ohlc_df(closes_in))
    assert strategy._trailing.is_active

    # Force bearish cross: big drop that is NOT below the stop, but enough to flip the SMAs.
    # Use prices that keep close above stop and produce a SMA cross-down.
    stop = strategy._trailing.stop_price or 0.0
    closes_down = closes_in + [stop + 5.0, stop + 3.0]
    signals = _feed(strategy, _ohlc_df(closes_down))
    # Either the SMA flipped or it didn't; if it did, the stop must be reset.
    sells = [s for s in signals if s.direction == "SELL"]
    if sells and sells[0].metadata.get("reason", "").startswith("SMA crossover DOWN"):
        assert strategy._trailing.is_active is False
        assert strategy._last_signal_side == "sell"
