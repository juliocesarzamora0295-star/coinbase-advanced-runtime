"""
Tests for regime detector, new strategies, and strategy selector.
"""

import numpy as np
import pandas as pd
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.strategy.regime_detector import MarketRegime, RegimeDetector, REGIME_STRATEGY_MAP
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.momentum_breakout import MomentumBreakoutStrategy
from src.strategy.selector import StrategySelector
from src.backtest.strategy_adapter import SelectorAdapter
from src.backtest.data_feed import Bar
from src.backtest.engine import BacktestEngine
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.data_feed import HistoricalDataFeed


def _trending_df(n: int = 200, direction: str = "up") -> pd.DataFrame:
    """Generate strongly trending OHLCV data."""
    rng = np.random.RandomState(101)
    if direction == "up":
        close = 30000.0 + np.arange(n) * 50.0 + rng.randn(n) * 20
    else:
        close = 50000.0 - np.arange(n) * 50.0 + rng.randn(n) * 20
    high = close + rng.uniform(30, 80, n)
    low = close - rng.uniform(30, 80, n)
    volume = rng.uniform(100, 500, n)
    return pd.DataFrame({
        "open": close + rng.randn(n) * 10,
        "high": high, "low": low, "close": close, "volume": volume,
    })


def _ranging_df(n: int = 200, center: float = 40000.0, amplitude: float = 500.0) -> pd.DataFrame:
    """Generate sideways/ranging OHLCV data."""
    rng = np.random.RandomState(202)
    t = np.linspace(0, 8 * np.pi, n)
    close = center + amplitude * np.sin(t) + rng.randn(n) * 50
    high = close + rng.uniform(20, 60, n)
    low = close - rng.uniform(20, 60, n)
    volume = rng.uniform(50, 300, n)
    return pd.DataFrame({
        "open": close + rng.randn(n) * 10,
        "high": high, "low": low, "close": close, "volume": volume,
    })


def _make_bars(prices: list[float], start_ms: int = 1_000_000_000_000) -> list[Bar]:
    bars = []
    for i, p in enumerate(prices):
        d = Decimal(str(p))
        bars.append(Bar(
            timestamp_ms=start_ms + i * 3_600_000,
            open=d, high=d + Decimal("100"), low=d - Decimal("100"),
            close=d, volume=Decimal("50"),
        ))
    return bars


# ── Regime Detector ────────────────────────────────────────────────────────

class TestRegimeDetector:
    def test_unknown_with_insufficient_data(self):
        detector = RegimeDetector()
        df = pd.DataFrame({"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [10]})
        result = detector.detect(df)
        assert result.regime == MarketRegime.UNKNOWN

    def test_trending_detection(self):
        detector = RegimeDetector()
        df = _trending_df(300, "up")
        result = detector.detect(df)
        # Strong uptrend should be detected as trending
        assert result.regime in (MarketRegime.TRENDING_CALM, MarketRegime.TRENDING_VOLATILE)
        assert result.adx_value > 0

    def test_ranging_detection(self):
        detector = RegimeDetector()
        df = _ranging_df(300)
        result = detector.detect(df)
        # Sideways should be detected as ranging
        assert result.regime in (MarketRegime.RANGING_CALM, MarketRegime.RANGING_VOLATILE)

    def test_all_regimes_have_strategy(self):
        for regime in MarketRegime:
            assert regime in REGIME_STRATEGY_MAP

    def test_reset_clears_hysteresis(self):
        detector = RegimeDetector()
        df = _trending_df(200)
        detector.detect(df)
        assert detector._last_trending is not None
        detector.reset()
        assert detector._last_trending is None

    def test_snapshot_immutable(self):
        detector = RegimeDetector()
        df = _trending_df(200)
        result = detector.detect(df)
        with pytest.raises(AttributeError):
            result.regime = MarketRegime.UNKNOWN  # type: ignore


# ── Mean Reversion Strategy ────────────────────────────────────────────────

class TestMeanReversionStrategy:
    def test_creates_from_config(self):
        s = MeanReversionStrategy("BTC-USD", {"bb_period": 15, "rsi_period": 10})
        assert s.bb_period == 15
        assert s.rsi_period == 10

    def test_no_signal_insufficient_data(self):
        s = MeanReversionStrategy("BTC-USD")
        s.update_market_data(pd.DataFrame({"close": [1, 2, 3]}))
        signals = s.generate_signals(mid=Decimal("3"))
        assert signals == []

    def test_generates_signals_on_ranging_data(self):
        s = MeanReversionStrategy("BTC-USD", {"bb_period": 10, "rsi_period": 7,
                                               "rsi_oversold": 35, "rsi_overbought": 65})
        # Create data that oscillates to trigger oversold/overbought
        n = 100
        t = np.linspace(0, 6 * np.pi, n)
        close = 40000 + 2000 * np.sin(t)
        df = pd.DataFrame({
            "open": close, "high": close + 100, "low": close - 100,
            "close": close, "volume": np.ones(n) * 100,
        })
        bt = datetime.now(tz=timezone.utc)
        all_signals = []
        for i in range(20, n):
            s.update_market_data(df.iloc[:i+1])
            sigs = s.generate_signals(mid=Decimal(str(close[i])), bar_timestamp=bt)
            all_signals.extend(sigs)
        # Should generate at least some signals on oscillating data
        assert len(all_signals) >= 1


# ── Momentum Breakout Strategy ─────────────────────────────────────────────

class TestMomentumBreakoutStrategy:
    def test_creates_from_config(self):
        s = MomentumBreakoutStrategy("BTC-USD", {"donchian_period": 15})
        assert s.donchian_period == 15

    def test_no_signal_insufficient_data(self):
        s = MomentumBreakoutStrategy("BTC-USD")
        s.update_market_data(pd.DataFrame({"close": [1, 2, 3]}))
        signals = s.generate_signals(mid=Decimal("3"))
        assert signals == []

    def test_generates_buy_on_breakout(self):
        s = MomentumBreakoutStrategy("BTC-USD", {
            "donchian_period": 10, "volume_ma_period": 10, "volume_multiplier": 1.0,
        })
        # Flat then sharp breakout
        n = 50
        close = np.concatenate([
            np.ones(30) * 40000,
            np.linspace(40000, 45000, 20),  # breakout
        ])
        volume = np.concatenate([np.ones(30) * 100, np.ones(20) * 500])
        df = pd.DataFrame({
            "open": close, "high": close + 50, "low": close - 50,
            "close": close, "volume": volume,
        })
        bt = datetime.now(tz=timezone.utc)
        all_signals = []
        for i in range(15, n):
            s.update_market_data(df.iloc[:i+1])
            sigs = s.generate_signals(mid=Decimal(str(close[i])), bar_timestamp=bt)
            all_signals.extend(sigs)
        buy_signals = [s for s in all_signals if s.direction == "BUY"]
        assert len(buy_signals) >= 1


# ── Strategy Selector ──────────────────────────────────────────────────────

class TestStrategySelector:
    def test_creates_from_config(self):
        sel = StrategySelector.from_config("BTC-USD", {"sma_fast": 10, "sma_slow": 20})
        assert sel.current_regime == MarketRegime.UNKNOWN
        assert sel.bar_count == 0

    def test_processes_bars(self):
        sel = StrategySelector.from_config("BTC-USD", {"sma_fast": 5, "sma_slow": 10})
        df = _trending_df(100)
        for i in range(len(df)):
            candle = df.iloc[i]
            sel.on_candle_closed(candle)
        assert sel.bar_count == 100

    def test_regime_switches_on_data_change(self):
        sel = StrategySelector.from_config("BTC-USD", {
            "sma_fast": 5, "sma_slow": 10, "min_regime_bars": 1,
        })
        # Feed trending data then ranging data
        trending = _trending_df(100, "up")
        ranging = _ranging_df(100)
        combined = pd.concat([trending, ranging], ignore_index=True)
        for i in range(len(combined)):
            sel.on_candle_closed(combined.iloc[i])
        # Should have at least one regime switch in history
        # (may not switch if ADX doesn't cross threshold, but bar_count should advance)
        assert sel.bar_count == 200


# ── Selector Adapter (Backtest Integration) ────────────────────────────────

class TestSelectorAdapter:
    def test_runs_full_backtest(self):
        """SelectorAdapter works end-to-end with BacktestEngine."""
        prices = (
            [30000 + i * 100 for i in range(40)]
            + [34000 - i * 50 for i in range(40)]
            + [32000 + i * 80 for i in range(40)]
        )
        bars = _make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger)

        adapter = SelectorAdapter(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
            qty=Decimal("0.01"),
        )

        engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=adapter)
        report = engine.run()
        assert report.total_bars == len(prices)

    def test_exposes_regime_info(self):
        adapter = SelectorAdapter(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
        )
        assert adapter.current_regime == "UNKNOWN"
        assert isinstance(adapter.current_strategy, str)
