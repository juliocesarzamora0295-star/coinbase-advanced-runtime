"""
Tests for backtest integration: strategy adapter, risk adapter, segmented runner.
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.strategy_adapter import StrategyAdapter
from src.backtest.risk_adapter import BacktestRiskAdapter
from src.backtest.data_downloader import BTC_MARKET_REGIMES, parse_regime_dates
from src.risk.gate import RiskGate, RiskLimits


def _make_bars(prices: list[float], start_ms: int = 1_000_000_000_000, step_ms: int = 3_600_000) -> list[Bar]:
    """Helper: create bars from close prices."""
    bars = []
    for i, p in enumerate(prices):
        d = Decimal(str(p))
        bars.append(Bar(
            timestamp_ms=start_ms + i * step_ms,
            open=d, high=d + Decimal("100"), low=d - Decimal("100"),
            close=d, volume=Decimal("10"),
        ))
    return bars


class TestStrategyAdapter:
    """Tests for production strategy adapter bridge."""

    def test_adapter_creates_from_config(self):
        adapter = StrategyAdapter.from_config(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
            qty=Decimal("0.01"),
        )
        assert adapter is not None

    def test_adapter_returns_none_with_insufficient_data(self):
        adapter = StrategyAdapter.from_config(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
        )
        bar = _make_bars([50000.0])[0]
        result = adapter(bar, [])
        assert result is None

    def test_adapter_runs_full_backtest(self):
        """Run a full backtest with the adapter to verify no crashes."""
        # Generate enough bars for SMA crossover to trigger
        # Trend up then down to force crossovers
        prices = (
            [30000 + i * 100 for i in range(30)]  # uptrend
            + [33000 - i * 100 for i in range(30)]  # downtrend
            + [30000 + i * 150 for i in range(30)]  # uptrend again
        )
        bars = _make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger)

        adapter = StrategyAdapter.from_config(
            symbol="BTC-USD",
            config={"sma_fast": 5, "sma_slow": 10},
            qty=Decimal("0.01"),
        )

        engine = BacktestEngine(feed=feed, ledger=ledger, executor=executor, strategy=adapter)
        report = engine.run()
        assert report.total_bars == len(prices)


class TestRiskAdapter:
    """Tests for RiskGate integration in backtest."""

    def test_risk_adapter_blocks_on_zero_equity(self):
        ledger = BacktestLedger(initial_cash=Decimal("0"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger)
        verdict = adapter.evaluate("BUY", Decimal("0.01"), Decimal("50000"))
        assert not verdict.allowed

    def test_risk_adapter_allows_normal_trade(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger)
        verdict = adapter.evaluate("BUY", Decimal("0.01"), Decimal("50000"))
        assert verdict.allowed

    def test_risk_adapter_blocks_sell_without_position(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger)
        verdict = adapter.evaluate("SELL", Decimal("0.01"), Decimal("50000"))
        assert not verdict.allowed

    def test_engine_with_risk_adapter(self):
        """BacktestEngine respects risk adapter blocking."""
        prices = [50000 + i * 10 for i in range(20)]
        bars = _make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger)

        risk_adapter = BacktestRiskAdapter.from_config(ledger=ledger)

        # Strategy that always tries to sell (should be blocked — no position)
        def always_sell(bar, history):
            return Signal(side="SELL", qty=Decimal("0.01"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=always_sell, risk_adapter=risk_adapter,
        )
        report = engine.run()
        # All sells should be blocked (no position)
        assert report.total_trades == 0
        assert risk_adapter.blocked_count > 0


class TestDataDownloader:
    """Tests for market regime definitions."""

    def test_all_regimes_parse(self):
        for regime in BTC_MARKET_REGIMES:
            label, start, end, desc = parse_regime_dates(regime)
            assert isinstance(start, datetime)
            assert isinstance(end, datetime)
            assert start < end
            assert start.tzinfo is not None

    def test_regimes_are_chronological(self):
        dates = [parse_regime_dates(r)[1] for r in BTC_MARKET_REGIMES]
        for i in range(1, len(dates)):
            assert dates[i] >= dates[i - 1], f"Regime {i} not chronological"

    def test_regime_count(self):
        assert len(BTC_MARKET_REGIMES) >= 8
