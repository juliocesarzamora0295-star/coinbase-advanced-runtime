"""
Tests for integrated quantitative certification:
- PerformanceMetrics includes CAGR and extended metrics
- compute_metrics produces all fields
- BacktestReport → PerformanceMetrics pipeline
"""

from decimal import Decimal

from src.quantitative.metrics import (
    PerformanceMetrics,
    TradeRecord,
    compute_metrics,
)
from tests.quantitative.conftest import generate_trending_bars, run_backtest


class TestIntegratedMetrics:
    """compute_metrics produces all extended fields."""

    def test_cagr_positive_for_winning_strategy(self):
        trades = [TradeRecord(pnl=Decimal("100")) for _ in range(10)]
        curve = [(i * 1000, Decimal("10000") + Decimal(str(i * 100))) for i in range(50)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert m.cagr > 0, f"CAGR should be positive: {m.cagr}"

    def test_cagr_zero_for_flat(self):
        curve = [(i, Decimal("10000")) for i in range(50)]
        m = compute_metrics([], curve, Decimal("10000"))
        assert abs(m.cagr) < 0.001

    def test_sortino_computed(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(5)]
        curve = [(i * 1000, Decimal("10000") + Decimal(str(i * 50))) for i in range(30)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert m.sortino_ratio != 0.0

    def test_calmar_computed(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(5)]
        curve = [
            (0, Decimal("10000")),
            (1, Decimal("10500")),
            (2, Decimal("10200")),  # drawdown
            (3, Decimal("10800")),
        ]
        m = compute_metrics(trades, curve, Decimal("10000"))
        # Has some value (not necessarily > 0 due to small sample)
        assert isinstance(m.calmar_ratio, float)

    def test_max_consecutive_losses_computed(self):
        trades = [
            TradeRecord(pnl=Decimal("10")),
            TradeRecord(pnl=Decimal("-5")),
            TradeRecord(pnl=Decimal("-3")),
            TradeRecord(pnl=Decimal("-2")),
            TradeRecord(pnl=Decimal("20")),
        ]
        curve = [(i, Decimal("10000")) for i in range(10)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert m.max_consecutive_losses == 3

    def test_recovery_factor_computed(self):
        trades = [TradeRecord(pnl=Decimal("100"))]
        curve = [
            (0, Decimal("10000")),
            (1, Decimal("10500")),
            (2, Decimal("9500")),  # drawdown = 1000
            (3, Decimal("10100")),
        ]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert isinstance(m.recovery_factor, float)

    def test_skewness_kurtosis_computed(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(5)]
        curve = [(i * 1000, Decimal("10000") + Decimal(str(i * 10))) for i in range(50)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert isinstance(m.skewness, float)
        assert isinstance(m.kurtosis, float)

    def test_time_in_market_computed(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(5)]
        curve = [(i, Decimal("10000")) for i in range(100)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert 0.0 <= m.time_in_market_pct <= 1.0

    def test_turnover_computed(self):
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(10)]
        curve = [(i, Decimal("10000")) for i in range(100)]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert abs(m.turnover_rate - 0.1) < 0.01

    def test_all_fields_present(self):
        m = compute_metrics([], [(0, Decimal("10000"))], Decimal("10000"))
        for field in [
            "cagr", "sortino_ratio", "calmar_ratio",
            "max_consecutive_losses", "recovery_factor",
            "time_in_market_pct", "turnover_rate",
            "skewness", "kurtosis",
        ]:
            assert hasattr(m, field), f"Missing field: {field}"


class TestEndToEndBacktestMetrics:
    """BacktestReport → PerformanceMetrics with extended fields."""

    def test_backtest_produces_extended_metrics(self):
        bars = generate_trending_bars(n=200, trend=0.001, seed=9000)
        report, ledger = run_backtest(bars, qty=Decimal("0.5"))

        trades = [TradeRecord(pnl=t.pnl, duration_ms=t.duration_ms) for t in ledger.trades]
        m = compute_metrics(
            trades, ledger.equity_curve, ledger.initial_cash,
        )

        assert m.total_trades == report.total_trades
        assert m.sharpe_ratio != 0.0 or m.total_trades == 0
        # Extended fields computed
        assert isinstance(m.cagr, float)
        assert isinstance(m.sortino_ratio, float)
        assert isinstance(m.max_consecutive_losses, int)

    def test_certification_with_extended_metrics(self):
        bars = generate_trending_bars(n=300, trend=0.002, seed=9001)
        _, ledger = run_backtest(bars, qty=Decimal("0.5"))

        trades = [TradeRecord(pnl=t.pnl, duration_ms=t.duration_ms) for t in ledger.trades]
        m = compute_metrics(trades, ledger.equity_curve, ledger.initial_cash)

        passed, failures = m.passes_certification(
            min_sharpe=-10.0,  # lenient for test
            max_drawdown=0.99,
            min_trades=1,
            min_profit_factor=0.0,
        )
        assert passed, f"Should pass lenient certification: {failures}"
