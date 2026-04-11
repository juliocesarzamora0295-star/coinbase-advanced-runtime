"""Tests for VwapStrategy (Fase 3C)."""

from decimal import Decimal
from typing import List

import numpy as np
import pandas as pd

from src.strategy.vwap_strategy import VwapStrategy


def _ohlc_df(closes: List[float], volume: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [volume] * len(closes),
        }
    )


def _make_strategy(
    threshold: float = 0.01,
    rsi_period: int = 5,
    oversold: float = 40.0,
    atr_mult: float = 1.5,
) -> VwapStrategy:
    return VwapStrategy(
        symbol="BTC-USD",
        config={
            "vwap_threshold": threshold,
            "rsi_period": rsi_period,
            "rsi_oversold": oversold,
            "rsi_overbought": 60.0,
            "stop_loss_atr_mult": atr_mult,
            "atr_period": 5,
        },
    )


def _feed(strategy: VwapStrategy, df: pd.DataFrame) -> List:
    strategy.update_market_data(df)
    return strategy.generate_signals(mid=Decimal("100"))


def test_warmup_returns_no_signal() -> None:
    strategy = _make_strategy()
    assert _feed(strategy, _ohlc_df([100.0] * 3)) == []


def test_buy_on_stretched_below_vwap_with_oversold_rsi() -> None:
    strategy = _make_strategy(threshold=0.005, oversold=50.0)
    # Long flat period anchors VWAP near 100; then a sharp drop pushes close
    # below vwap*(1-threshold) and RSI into the oversold zone.
    closes = [100.0] * 15 + [98.0, 96.0, 94.0, 92.0, 90.0]
    signals = _feed(strategy, _ohlc_df(closes))
    buys = [s for s in signals if s.direction == "BUY"]
    assert len(buys) == 1
    assert strategy._trailing.is_active is True
    assert buys[0].metadata.get("stop_price") is not None
    assert "VWAP" in (buys[0].metadata.get("reason") or "")


def test_no_buy_when_close_above_vwap() -> None:
    strategy = _make_strategy(threshold=0.005, oversold=50.0)
    closes = [100.0] * 15 + [101.0, 102.0, 103.0, 104.0, 105.0]
    signals = _feed(strategy, _ohlc_df(closes))
    buys = [s for s in signals if s.direction == "BUY"]
    assert buys == []


def test_buy_suppressed_when_atr_nan() -> None:
    strategy = _make_strategy(threshold=0.005, oversold=50.0)
    closes = [100.0] * 15 + [98.0, 96.0, 94.0, 92.0, 90.0]
    df = _ohlc_df(closes)
    df.loc[df.index[-1], "high"] = np.nan
    df.loc[df.index[-1], "low"] = np.nan
    df.loc[df.index[-1], "close"] = np.nan
    strategy.update_market_data(df)
    signals = strategy.generate_signals(mid=Decimal("100"))
    assert [s for s in signals if s.direction == "BUY"] == []
    assert strategy._trailing.is_active is False


def test_registry_exposes_vwap() -> None:
    from src.strategy.registry import get_strategy_class

    assert get_strategy_class("vwap") is VwapStrategy
    assert get_strategy_class("vwap_reversion") is VwapStrategy
