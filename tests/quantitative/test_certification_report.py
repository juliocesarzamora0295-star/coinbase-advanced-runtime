"""
Certification report — consolidated PASS/FAIL with quantitative thresholds.

Generates a certification report and verifies minimum quality thresholds.
"""

from decimal import Decimal
from typing import List, Tuple

from src.quantitative.metrics import (
    PerformanceMetrics,
    TradeRecord,
    compute_metrics,
)
from tests.quantitative.conftest import generate_trending_bars, run_backtest


def _trades_and_curve_from_backtest(bars, **kwargs):
    """Run backtest and extract trades + equity curve for metrics module."""
    report, ledger = run_backtest(bars, **kwargs)
    trades = [
        TradeRecord(pnl=t.pnl, duration_ms=t.duration_ms)
        for t in ledger.trades
    ]
    return trades, ledger.equity_curve, ledger.initial_cash


class TestPerformanceMetrics:
    """Metrics module computes correctly."""

    def test_metrics_from_winning_trades(self):
        trades = [TradeRecord(pnl=Decimal("100")), TradeRecord(pnl=Decimal("50"))]
        curve = [(0, Decimal("10000")), (1, Decimal("10150"))]
        m = compute_metrics(trades, curve, Decimal("10000"))

        assert m.total_trades == 2
        assert m.winning_trades == 2
        assert m.losing_trades == 0
        assert m.win_rate == 1.0
        assert m.total_pnl == Decimal("150")
        assert m.profit_factor == float("inf")

    def test_metrics_from_mixed_trades(self):
        trades = [
            TradeRecord(pnl=Decimal("100")),
            TradeRecord(pnl=Decimal("-30")),
            TradeRecord(pnl=Decimal("50")),
            TradeRecord(pnl=Decimal("-20")),
        ]
        curve = [(i, Decimal("10000") + Decimal(str(i * 10))) for i in range(10)]
        m = compute_metrics(trades, curve, Decimal("10000"))

        assert m.total_trades == 4
        assert m.winning_trades == 2
        assert m.losing_trades == 2
        assert m.win_rate == 0.5
        assert m.total_pnl == Decimal("100")
        assert m.profit_factor > 1.0

    def test_metrics_drawdown(self):
        curve = [
            (0, Decimal("10000")),
            (1, Decimal("10500")),  # peak
            (2, Decimal("9500")),   # drawdown
            (3, Decimal("10200")),
        ]
        m = compute_metrics([], curve, Decimal("10000"))
        # DD = (10500 - 9500) / 10500 ≈ 9.5%
        assert 0.09 < m.max_drawdown < 0.10

    def test_metrics_empty_trades(self):
        m = compute_metrics([], [], Decimal("10000"))
        assert m.total_trades == 0
        assert m.win_rate == 0.0
        assert m.sharpe_ratio == 0.0

    def test_return_pct(self):
        curve = [(0, Decimal("10000")), (1, Decimal("11000"))]
        m = compute_metrics([], curve, Decimal("10000"))
        assert abs(m.return_pct - 0.10) < 0.001


class TestCertificationPassFail:
    """Certification thresholds."""

    def test_good_metrics_pass(self):
        m = PerformanceMetrics(
            total_trades=20,
            winning_trades=12,
            losing_trades=8,
            win_rate=0.6,
            total_pnl=Decimal("500"),
            profit_factor=2.0,
            max_drawdown=0.10,
            sharpe_ratio=1.5,
            avg_trade_pnl=Decimal("25"),
            avg_win=Decimal("80"),
            avg_loss=Decimal("-40"),
            avg_trade_duration_ms=3600000,
            return_pct=0.05,
        )
        passed, failures = m.passes_certification()
        assert passed, f"Should pass: {failures}"

    def test_low_sharpe_fails(self):
        m = PerformanceMetrics(
            total_trades=20, winning_trades=8, losing_trades=12,
            win_rate=0.4, total_pnl=Decimal("-100"),
            profit_factor=0.3, max_drawdown=0.20,
            sharpe_ratio=-0.5, avg_trade_pnl=Decimal("-5"),
            avg_win=Decimal("20"), avg_loss=Decimal("-15"),
            avg_trade_duration_ms=3600000, return_pct=-0.01,
        )
        passed, failures = m.passes_certification(min_sharpe=0.0)
        assert not passed
        assert any("sharpe" in f for f in failures)

    def test_high_drawdown_fails(self):
        m = PerformanceMetrics(
            total_trades=20, winning_trades=10, losing_trades=10,
            win_rate=0.5, total_pnl=Decimal("100"),
            profit_factor=1.2, max_drawdown=0.60,
            sharpe_ratio=0.5, avg_trade_pnl=Decimal("5"),
            avg_win=Decimal("30"), avg_loss=Decimal("-20"),
            avg_trade_duration_ms=3600000, return_pct=0.01,
        )
        passed, failures = m.passes_certification(max_drawdown=0.50)
        assert not passed
        assert any("max_dd" in f for f in failures)

    def test_too_few_trades_fails(self):
        m = PerformanceMetrics(
            total_trades=2, winning_trades=2, losing_trades=0,
            win_rate=1.0, total_pnl=Decimal("100"),
            profit_factor=float("inf"), max_drawdown=0.0,
            sharpe_ratio=5.0, avg_trade_pnl=Decimal("50"),
            avg_win=Decimal("50"), avg_loss=Decimal("0"),
            avg_trade_duration_ms=3600000, return_pct=0.01,
        )
        passed, failures = m.passes_certification(min_trades=5)
        assert not passed
        assert any("trades" in f for f in failures)


class TestEndToEndCertification:
    """Run full backtest and verify certification."""

    def test_uptrend_certification(self):
        """Strategy on uptrend data should pass basic certification."""
        bars = generate_trending_bars(n=300, trend=0.001, seed=800)
        trades, curve, initial = _trades_and_curve_from_backtest(
            bars, qty=Decimal("0.5"), fee_rate=Decimal("0.001")
        )
        m = compute_metrics(trades, curve, initial)

        # Basic viability check
        assert m.total_trades >= 3, f"Too few trades: {m.total_trades}"
        assert m.max_drawdown < 0.50, f"Drawdown too high: {m.max_drawdown:.2%}"

    def test_certification_report_format(self):
        """Certification report has all required fields."""
        bars = generate_trending_bars(n=200, seed=810)
        trades, curve, initial = _trades_and_curve_from_backtest(
            bars, qty=Decimal("0.5")
        )
        m = compute_metrics(trades, curve, initial)

        # All fields present
        assert hasattr(m, "total_trades")
        assert hasattr(m, "sharpe_ratio")
        assert hasattr(m, "max_drawdown")
        assert hasattr(m, "profit_factor")
        assert hasattr(m, "win_rate")
        assert hasattr(m, "return_pct")

        # passes_certification returns tuple
        passed, failures = m.passes_certification()
        assert isinstance(passed, bool)
        assert isinstance(failures, list)
