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

    def test_calmar_dimensionally_correct(self):
        """Calmar = CAGR / max_drawdown_fraction. Both dimensionless."""
        trades = [TradeRecord(pnl=Decimal("10")) for _ in range(5)]
        curve = [
            (0, Decimal("10000")),
            (1, Decimal("10500")),
            (2, Decimal("10200")),  # drawdown from peak
            (3, Decimal("10800")),
        ]
        m = compute_metrics(trades, curve, Decimal("10000"))
        assert isinstance(m.calmar_ratio, float)
        # Calmar should be CAGR/dd, not rate/dollars
        # With positive CAGR and small drawdown, Calmar should be > 0
        if m.cagr > 0 and m.max_drawdown > 0:
            expected = m.cagr / m.max_drawdown
            assert abs(m.calmar_ratio - expected) < 0.001, (
                f"Calmar mismatch: got {m.calmar_ratio}, expected CAGR/dd={expected}"
            )

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
            max_consecutive_losses=50,
        )
        assert passed, f"Should pass lenient certification: {failures}"

    def test_certification_fails_on_consecutive_losses(self):
        """Too many consecutive losses → fails certification."""
        from src.quantitative.metrics import PerformanceMetrics
        m = PerformanceMetrics(
            total_trades=20, winning_trades=5, losing_trades=15,
            win_rate=0.25, total_pnl=Decimal("-500"),
            profit_factor=0.3, max_drawdown=0.20,
            sharpe_ratio=-0.5, avg_trade_pnl=Decimal("-25"),
            avg_win=Decimal("50"), avg_loss=Decimal("-50"),
            avg_trade_duration_ms=3600000, return_pct=-0.05,
            max_consecutive_losses=12,
        )
        passed, failures = m.passes_certification(
            max_consecutive_losses=10,
        )
        assert not passed
        assert any("consec_losses" in f for f in failures)
