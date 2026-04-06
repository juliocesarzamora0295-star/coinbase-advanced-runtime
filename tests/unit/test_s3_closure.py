"""
Tests for S3 risk closure: R10-R16.
"""

from decimal import Decimal

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.execution.twap import TWAPExecutor, TWAPResult
from src.quantitative.metrics import TradeRecord, compute_metrics
from src.risk.gate import (
    RULE_TOTAL_EXPOSURE,
    RiskGate,
    RiskLimits,
)
from src.risk.position_sizer import PositionSizer, SizingMode, SymbolConstraints
from src.strategy.examples.momentum import momentum_strategy

CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("Infinity"),
    min_notional=Decimal("1"),
)


# ── R10: Cross-symbol risk aggregation ──


class TestR10CrossSymbolExposure:

    def test_within_limit_allowed(self):
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.80")))
        v = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("3000"), "ETH-USD": Decimal("2000")},
            new_symbol="SOL-USD",
            new_notional=Decimal("1000"),
        )
        assert v.allowed  # 6000/10000 = 60% < 80%

    def test_exceeds_limit_blocked(self):
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.80")))
        v = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("5000"), "ETH-USD": Decimal("3000")},
            new_symbol="SOL-USD",
            new_notional=Decimal("1000"),
        )
        assert not v.allowed  # 9000/10000 = 90% > 80%
        assert RULE_TOTAL_EXPOSURE in v.blocking_rule_ids

    def test_zero_equity_blocked(self):
        gate = RiskGate(RiskLimits())
        v = gate.check_total_exposure(
            equity=Decimal("0"),
            exposures={},
            new_symbol="BTC-USD",
            new_notional=Decimal("100"),
        )
        assert not v.allowed


# ── R11: stop_price in pipeline ──


class TestR11StopPricePipeline:

    def test_stop_price_activates_risk_based(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("49000"),
            preferred_mode=SizingMode.RISK_BASED,
        )
        assert d.sizing_mode == SizingMode.RISK_BASED

    def test_no_stop_falls_back(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=None,
            preferred_mode=SizingMode.RISK_BASED,
        )
        assert d.sizing_mode == SizingMode.NOTIONAL


# ── R12: TWAP executor ──


class TestR12TWAP:

    def test_twap_simulated_splits_evenly(self):
        twap = TWAPExecutor(num_slices=4, interval_seconds=1.0)
        result = twap.execute("BTC-USD", "BUY", Decimal("1.0"), Decimal("50000"), simulate=True)
        assert result.completed_slices == 4
        assert result.total_qty == Decimal("1.0")
        assert len(result.slices) == 4
        for s in result.slices:
            assert s.qty == Decimal("0.25")

    def test_twap_avg_price(self):
        twap = TWAPExecutor(num_slices=2)
        result = twap.execute("BTC-USD", "SELL", Decimal("2.0"), Decimal("50000"), simulate=True)
        assert result.avg_price == Decimal("50000")

    def test_twap_fill_ratio(self):
        twap = TWAPExecutor(num_slices=5)
        result = twap.execute("ETH-USD", "BUY", Decimal("10"), Decimal("3000"), simulate=True)
        assert result.fill_ratio == Decimal("1")

    def test_twap_with_failing_executor(self):
        """Non-simulated with failing execute_fn → partial fill."""
        twap = TWAPExecutor(
            num_slices=3,
            interval_seconds=0.0,
            execute_fn=lambda sym, side, qty, price: False,
        )
        result = twap.execute("BTC-USD", "BUY", Decimal("1"), Decimal("50000"), simulate=False)
        assert result.completed_slices == 0
        assert result.total_qty == Decimal("0")


# ── R13: Momentum strategy ──


class TestR13Momentum:

    def test_momentum_produces_signals(self):
        from tests.quantitative.conftest import generate_trending_bars
        bars = generate_trending_bars(n=100, trend=0.005, seed=900)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))

        state = {}
        def strategy(bar, history):
            return momentum_strategy(bar, history, lookback=10, qty=Decimal("0.5"), _state=state)

        engine = BacktestEngine(feed, ledger, executor, strategy)
        report = engine.run()
        assert report.total_bars == 100
        assert report.total_trades >= 1  # at least one trade in strong trend

    def test_momentum_no_signal_without_enough_history(self):
        bar = Bar(
            timestamp_ms=1000, open=Decimal("100"), high=Decimal("101"),
            low=Decimal("99"), close=Decimal("100"), volume=Decimal("100"),
        )
        result = momentum_strategy(bar, [], lookback=20)
        assert result is None


# ── R14: Walk-forward with grid search ──


class TestR14WalkForwardOptimization:

    def test_grid_search_on_train_window(self):
        """Grid search finds best params on train, applies to test."""
        from tests.quantitative.conftest import generate_trending_bars, run_backtest

        all_bars = generate_trending_bars(n=200, trend=0.001, seed=1400)
        train_bars = all_bars[:100]
        test_bars = all_bars[100:]

        # Grid search on train
        best_pnl = None
        best_params = None
        for fast in [3, 5, 7]:
            for slow in [15, 20, 25]:
                if fast >= slow:
                    continue
                report, _ = run_backtest(train_bars, fast=fast, slow=slow, qty=Decimal("0.5"))
                pnl = float(report.total_pnl)
                if best_pnl is None or pnl > best_pnl:
                    best_pnl = pnl
                    best_params = (fast, slow)

        assert best_params is not None

        # Apply best params to test (no lookahead)
        test_report, _ = run_backtest(
            test_bars, fast=best_params[0], slow=best_params[1], qty=Decimal("0.5")
        )
        assert test_report.total_bars == 100

    def test_optimized_params_differ_from_default(self):
        """Grid search should find params different from default in some cases."""
        from tests.quantitative.conftest import generate_trending_bars, run_backtest

        bars = generate_trending_bars(n=150, trend=0.002, seed=1401)
        results = {}
        for fast in [3, 5, 10]:
            for slow in [15, 20, 30]:
                if fast >= slow:
                    continue
                report, _ = run_backtest(bars, fast=fast, slow=slow, qty=Decimal("0.5"))
                results[(fast, slow)] = float(report.total_pnl)

        # At least 2 different PnL values exist (params matter)
        unique_pnls = set(round(v, 2) for v in results.values())
        assert len(unique_pnls) >= 2


# ── R15: Sharpe configurable ──


class TestR15SharpeConfigurable:

    def test_sharpe_with_default_252(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(10)]
        curve = [(i * 1000, Decimal("10000") + Decimal(str(i * 10))) for i in range(20)]
        m = compute_metrics(trades, curve, Decimal("10000"), periods_per_year=252)
        assert m.sharpe_ratio != 0.0

    def test_sharpe_with_crypto_8760(self):
        """Hourly crypto: 365 * 24 = 8760 periods per year."""
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(10)]
        curve = [(i * 1000, Decimal("10000") + Decimal(str(i * 10))) for i in range(20)]
        m_252 = compute_metrics(trades, curve, Decimal("10000"), periods_per_year=252)
        m_8760 = compute_metrics(trades, curve, Decimal("10000"), periods_per_year=8760)
        # Higher periods_per_year → higher annualized Sharpe (same returns, more periods)
        assert abs(m_8760.sharpe_ratio) > abs(m_252.sharpe_ratio)

    def test_sharpe_zero_for_flat_equity(self):
        curve = [(i, Decimal("10000")) for i in range(10)]
        m = compute_metrics([], curve, Decimal("10000"), periods_per_year=252)
        assert m.sharpe_ratio == 0.0


# ── R16: Deprecated alias removed from config ──


class TestR16DeprecatedRemoved:

    def test_trading_config_no_risk_per_trade_pct(self):
        """TradingConfig no longer has risk_per_trade_pct field."""
        from src.config import TradingConfig
        tc = TradingConfig()
        assert not hasattr(tc, "risk_per_trade_pct")
        assert hasattr(tc, "notional_pct")
        assert tc.notional_pct == 0.01

    def test_position_sizer_still_accepts_legacy_param(self):
        """PositionSizer.compute() still accepts risk_per_trade_pct as fallback."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            risk_per_trade_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.target_qty > Decimal("0")
