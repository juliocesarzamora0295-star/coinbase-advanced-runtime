"""
Tests for quantitative indicators.
"""

import numpy as np
import pandas as pd
import pytest

from src.quantitative.indicators import (
    atr,
    atr_normalized,
    bollinger_bands,
    adx,
    donchian_channel,
    ema,
    macd,
    rsi,
    sma,
    true_range,
)


def _random_ohlcv(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """Generate random OHLCV data for testing."""
    rng = np.random.RandomState(seed)
    close = 50000.0 + np.cumsum(rng.randn(n) * 100)
    high = close + rng.uniform(50, 200, n)
    low = close - rng.uniform(50, 200, n)
    opn = close + rng.uniform(-100, 100, n)
    volume = rng.uniform(10, 1000, n)
    return pd.DataFrame({
        "open": opn, "high": high, "low": low, "close": close, "volume": volume,
    })


class TestSMA:
    def test_sma_length(self):
        s = pd.Series(range(100), dtype=float)
        result = sma(s, 10)
        assert len(result) == 100
        assert pd.isna(result.iloc[8])
        assert not pd.isna(result.iloc[9])

    def test_sma_value(self):
        s = pd.Series([1, 2, 3, 4, 5], dtype=float)
        result = sma(s, 3)
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[4] == pytest.approx(4.0)


class TestEMA:
    def test_ema_no_nan(self):
        s = pd.Series(range(50), dtype=float)
        result = ema(s, 10)
        assert not result.isna().any()


class TestATR:
    def test_atr_positive(self):
        df = _random_ohlcv()
        result = atr(df["high"], df["low"], df["close"], 14)
        valid = result.dropna()
        assert (valid > 0).all()

    def test_atr_normalized_range(self):
        df = _random_ohlcv()
        result = atr_normalized(df["high"], df["low"], df["close"], 14)
        valid = result.dropna()
        assert (valid > 0).all()
        assert (valid < 1).all()  # ATR should be < 100% of price


class TestRSI:
    def test_rsi_range(self):
        df = _random_ohlcv()
        result = rsi(df["close"], 14)
        valid = result.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_rsi_uptrend_high(self):
        # Strong uptrend with pullbacks should have high RSI
        rng = np.random.RandomState(42)
        # Mostly up with occasional small downs
        moves = rng.normal(1.0, 0.5, 100)
        s = pd.Series(100 + np.cumsum(moves))
        result = rsi(s, 14)
        # Last value should be defined and elevated
        assert not pd.isna(result.iloc[-1])
        assert result.iloc[-1] > 50


class TestMACD:
    def test_macd_components(self):
        df = _random_ohlcv()
        result = macd(df["close"])
        assert len(result.macd_line) == len(df)
        assert len(result.signal_line) == len(df)
        assert len(result.histogram) == len(df)

    def test_histogram_is_difference(self):
        df = _random_ohlcv()
        result = macd(df["close"])
        diff = result.macd_line - result.signal_line
        np.testing.assert_array_almost_equal(result.histogram, diff)


class TestBollinger:
    def test_bands_ordering(self):
        df = _random_ohlcv()
        result = bollinger_bands(df["close"], 20, 2.0)
        valid_idx = result.upper.dropna().index
        assert (result.upper[valid_idx] >= result.middle[valid_idx]).all()
        assert (result.middle[valid_idx] >= result.lower[valid_idx]).all()

    def test_pct_b_range(self):
        df = _random_ohlcv()
        result = bollinger_bands(df["close"], 20, 2.0)
        valid = result.pct_b.dropna()
        # Most values should be between -0.5 and 1.5
        assert valid.median() > 0
        assert valid.median() < 1


class TestADX:
    def test_adx_range(self):
        df = _random_ohlcv()
        result = adx(df["high"], df["low"], df["close"], 14)
        valid = result.adx.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_di_positive(self):
        df = _random_ohlcv()
        result = adx(df["high"], df["low"], df["close"], 14)
        valid_plus = result.plus_di.dropna()
        valid_minus = result.minus_di.dropna()
        assert (valid_plus >= 0).all()
        assert (valid_minus >= 0).all()


class TestDonchian:
    def test_upper_gte_lower(self):
        df = _random_ohlcv()
        result = donchian_channel(df["high"], df["low"], 20)
        valid_idx = result.upper.dropna().index
        assert (result.upper[valid_idx] >= result.lower[valid_idx]).all()

    def test_middle_is_midpoint(self):
        df = _random_ohlcv()
        result = donchian_channel(df["high"], df["low"], 20)
        valid_idx = result.upper.dropna().index
        expected = (result.upper[valid_idx] + result.lower[valid_idx]) / 2
        np.testing.assert_array_almost_equal(result.middle[valid_idx], expected)
