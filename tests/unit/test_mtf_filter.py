"""Tests for MultiTimeframeFilter — trend confirmation across 4h and 1d."""

import numpy as np
import pandas as pd
import pytest

from src.strategy.mtf_filter import MultiTimeframeFilter


def _make_trending_df(n_bars: int, start_price: float, trend: float, seed: int = 42) -> pd.DataFrame:
    """Generate 1h candle data with a clear trend direction."""
    rng = np.random.RandomState(seed)
    prices = [start_price]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] * (1 + trend + rng.normal(0, abs(trend) * 0.3)))

    dates = pd.date_range("2023-01-01", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": [int(d.timestamp() * 1000) for d in dates],
        "close": prices,
        "open": prices,
        "high": [p * 1.001 for p in prices],
        "low": [p * 0.999 for p in prices],
        "volume": [100.0] * n_bars,
    }, index=dates)


class TestMultiTimeframeFilter:

    def test_uptrend_all_timeframes_buy(self):
        """Strong uptrend on all TFs + BUY → high confidence."""
        df = _make_trending_df(1500, start_price=30000, trend=0.0005)
        mtf = MultiTimeframeFilter()
        confidence = mtf.get_confidence(df, "BUY")
        assert confidence >= 0.8

    def test_downtrend_all_timeframes_buy(self):
        """Strong downtrend on all TFs + BUY → low confidence (counter-trend)."""
        df = _make_trending_df(1500, start_price=30000, trend=-0.0005)
        mtf = MultiTimeframeFilter()
        confidence = mtf.get_confidence(df, "BUY")
        assert confidence <= 0.3

    def test_downtrend_all_timeframes_sell(self):
        """Strong downtrend + SELL → high confidence (aligned)."""
        df = _make_trending_df(1500, start_price=30000, trend=-0.0005)
        mtf = MultiTimeframeFilter()
        confidence = mtf.get_confidence(df, "SELL")
        assert confidence >= 0.8

    def test_insufficient_data(self):
        """< 1200 bars → neutral 0.5."""
        df = _make_trending_df(100, start_price=30000, trend=0.001)
        mtf = MultiTimeframeFilter()
        confidence = mtf.get_confidence(df, "BUY")
        assert confidence == 0.5

    def test_resampling_produces_valid_data(self):
        """Verify internal resampling works: 1h→4h has ~1/4 rows."""
        df = _make_trending_df(1500, start_price=30000, trend=0.0003)
        mtf = MultiTimeframeFilter()
        df_idx = mtf._ensure_datetime_index(df)
        assert df_idx is not None

        resampled_4h = df_idx["close"].resample("4h").last().dropna()
        resampled_1d = df_idx["close"].resample("1D").last().dropna()

        # 1500 1h bars → ~375 4h bars, ~62 1d bars
        assert 350 < len(resampled_4h) < 400
        assert 55 < len(resampled_1d) < 70

    def test_confidence_is_bounded(self):
        """Confidence always between 0.0 and 1.0."""
        df = _make_trending_df(1500, start_price=30000, trend=0.0001, seed=99)
        mtf = MultiTimeframeFilter()
        for direction in ("BUY", "SELL"):
            c = mtf.get_confidence(df, direction)
            assert 0.0 <= c <= 1.0
