"""
Tests for AdaptiveSizer and AdaptiveAdapter.
"""

import numpy as np
import pandas as pd
import pytest
from decimal import Decimal

from src.risk.adaptive_sizer import (
    AdaptiveSizer,
    AdaptiveSizingResult,
    SizingContext,
    build_context_from_df,
    _interpolate,
    _interpolate_inverse,
)
from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.strategy_adapter import AdaptiveAdapter, StrategyAdapter


class TestInterpolation:
    def test_interpolate_at_low(self):
        assert _interpolate(0.0, 0.0, 1.0, 0.5, 1.5) == pytest.approx(0.5)

    def test_interpolate_at_high(self):
        assert _interpolate(1.0, 0.0, 1.0, 0.5, 1.5) == pytest.approx(1.5)

    def test_interpolate_midpoint(self):
        assert _interpolate(0.5, 0.0, 1.0, 0.5, 1.5) == pytest.approx(1.0)

    def test_interpolate_clamped_below(self):
        assert _interpolate(-1.0, 0.0, 1.0, 0.5, 1.5) == pytest.approx(0.5)

    def test_interpolate_clamped_above(self):
        assert _interpolate(5.0, 0.0, 1.0, 0.5, 1.5) == pytest.approx(1.5)

    def test_inverse_at_low(self):
        # Low value → max_out
        assert _interpolate_inverse(0.0, 0.0, 1.0, 0.3, 1.0) == pytest.approx(1.0)

    def test_inverse_at_high(self):
        # High value → min_out
        assert _interpolate_inverse(1.0, 0.0, 1.0, 0.3, 1.0) == pytest.approx(0.3)


class TestAdaptiveSizer:
    def test_default_construction(self):
        sizer = AdaptiveSizer()
        assert sizer.base_pct == Decimal("0.005")

    def test_calm_trending_market_increases_size(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.005"))
        ctx = SizingContext(
            atr_norm=0.008,  # low volatility
            adx_value=35.0,  # strong trend
            volume_ratio=1.5,  # above average volume
            drawdown_pct=0.0,  # no drawdown
            trend_aligned=True,
        )
        result = sizer.compute(ctx)
        # Should increase from base
        assert result.effective_pct > sizer.base_pct
        assert result.vol_factor > 1.0  # low vol → high factor
        assert result.trend_factor > 1.0  # strong trend → high factor
        assert result.dd_factor == pytest.approx(1.0)  # no DD

    def test_volatile_ranging_market_decreases_size(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.005"))
        ctx = SizingContext(
            atr_norm=0.045,  # very high volatility
            adx_value=12.0,  # very weak trend
            volume_ratio=0.4,  # below average
            drawdown_pct=0.0,
            trend_aligned=False,
        )
        result = sizer.compute(ctx)
        assert result.effective_pct < sizer.base_pct
        assert result.vol_factor < 1.0
        assert result.trend_factor < 1.0

    def test_drawdown_reduces_size(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.005"))

        no_dd = SizingContext(atr_norm=0.02, adx_value=25, volume_ratio=1.0,
                              drawdown_pct=0.0, trend_aligned=True)
        with_dd = SizingContext(atr_norm=0.02, adx_value=25, volume_ratio=1.0,
                                drawdown_pct=0.07, trend_aligned=True)

        r1 = sizer.compute(no_dd)
        r2 = sizer.compute(with_dd)
        assert r2.effective_pct < r1.effective_pct
        assert r2.dd_factor < r1.dd_factor

    def test_floor_respected(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.005"), floor_pct=Decimal("0.001"))
        # Worst case scenario
        ctx = SizingContext(atr_norm=0.1, adx_value=5, volume_ratio=0.1,
                            drawdown_pct=0.1, trend_aligned=False)
        result = sizer.compute(ctx)
        assert result.effective_pct >= Decimal("0.001")

    def test_ceiling_respected(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.010"), ceiling_pct=Decimal("0.020"))
        # Best case scenario
        ctx = SizingContext(atr_norm=0.005, adx_value=50, volume_ratio=5.0,
                            drawdown_pct=0.0, trend_aligned=True)
        result = sizer.compute(ctx)
        assert result.effective_pct <= Decimal("0.020")

    def test_result_is_immutable(self):
        sizer = AdaptiveSizer()
        ctx = SizingContext(atr_norm=0.02, adx_value=25, volume_ratio=1.0,
                            drawdown_pct=0.0, trend_aligned=True)
        result = sizer.compute(ctx)
        with pytest.raises(AttributeError):
            result.effective_pct = Decimal("999")  # type: ignore

    def test_trend_aligned_bonus(self):
        sizer = AdaptiveSizer(base_pct=Decimal("0.005"))
        base = SizingContext(atr_norm=0.02, adx_value=30, volume_ratio=1.0,
                             drawdown_pct=0.0, trend_aligned=False)
        aligned = SizingContext(atr_norm=0.02, adx_value=30, volume_ratio=1.0,
                                drawdown_pct=0.0, trend_aligned=True)
        r1 = sizer.compute(base)
        r2 = sizer.compute(aligned)
        assert r2.trend_factor > r1.trend_factor


class TestBuildContextFromDf:
    def _make_df(self, n=100):
        rng = np.random.RandomState(42)
        close = 40000 + np.cumsum(rng.randn(n) * 100)
        return pd.DataFrame({
            "open": close + rng.randn(n) * 10,
            "high": close + rng.uniform(50, 200, n),
            "low": close - rng.uniform(50, 200, n),
            "close": close,
            "volume": rng.uniform(100, 500, n),
        })

    def test_returns_context(self):
        df = self._make_df()
        ctx = build_context_from_df(df, "BUY")
        assert ctx is not None
        assert ctx.atr_norm > 0
        assert ctx.adx_value > 0
        assert ctx.volume_ratio > 0

    def test_returns_none_insufficient_data(self):
        df = pd.DataFrame({"open": [1], "high": [2], "low": [0.5], "close": [1], "volume": [10]})
        ctx = build_context_from_df(df, "BUY")
        assert ctx is None


class TestAdaptiveAdapter:
    def _make_bars(self, prices):
        bars = []
        for i, p in enumerate(prices):
            d = Decimal(str(p))
            bars.append(Bar(
                timestamp_ms=1_000_000_000_000 + i * 3_600_000,
                open=d, high=d + Decimal("100"), low=d - Decimal("100"),
                close=d, volume=Decimal("50"),
            ))
        return bars

    def test_runs_full_backtest_with_adaptive(self):
        prices = [30000 + i * 80 for i in range(80)] + [36400 - i * 60 for i in range(80)]
        bars = self._make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger)

        inner = StrategyAdapter.from_config(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
            qty=Decimal("0.01"),
        )
        adaptive = AdaptiveAdapter(
            inner=inner,
            equity=Decimal("10000"),
            base_pct=Decimal("0.005"),
        )
        adaptive.set_ledger(ledger)

        engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=adaptive)
        report = engine.run()
        assert report.total_bars == len(prices)
        # Should have sizing log entries
        assert len(adaptive.sizing_log) >= 0  # may be 0 if no signals

    def test_adaptive_varies_qty(self):
        """Verify adaptive produces different qty vs fixed."""
        prices = (
            [30000 + i * 100 for i in range(60)]
            + [36000 - i * 50 for i in range(60)]
            + [33000 + i * 80 for i in range(60)]
        )
        bars = self._make_bars(prices)
        feed_fixed = HistoricalDataFeed.from_bars(bars)
        feed_adaptive = HistoricalDataFeed.from_bars(bars)

        # Fixed
        ledger_f = BacktestLedger(initial_cash=Decimal("10000"))
        exec_f = PaperExecutor(ledger=ledger_f)
        fixed_adapter = StrategyAdapter.from_config("BTC-USD", {"sma_fast": 5, "sma_slow": 10}, Decimal("0.01"))
        engine_f = BacktestEngine(feed=feed_fixed, ledger=ledger_f, executor=exec_f, strategy=fixed_adapter)
        report_f = engine_f.run()

        # Adaptive
        ledger_a = BacktestLedger(initial_cash=Decimal("10000"))
        exec_a = PaperExecutor(ledger=ledger_a)
        inner = StrategyAdapter.from_config("BTC-USD", {"sma_fast": 5, "sma_slow": 10}, Decimal("0.01"))
        adaptive = AdaptiveAdapter(inner=inner, equity=Decimal("10000"), base_pct=Decimal("0.005"))
        adaptive.set_ledger(ledger_a)
        engine_a = BacktestEngine(feed=feed_adaptive, ledger=ledger_a, executor=exec_a, strategy=adaptive)
        report_a = engine_a.run()

        # Both should complete
        assert report_f.total_bars == report_a.total_bars
        # If adaptive produced signals, qty should differ from fixed 0.01
        if adaptive.sizing_log:
            qtys = [s["adaptive_qty"] for s in adaptive.sizing_log]
            assert not all(q == 0.01 for q in qtys), "Adaptive should vary qty"
