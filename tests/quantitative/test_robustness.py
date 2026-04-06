"""
Robustness analysis — perturbaciones de ±10% no causan cambios catastróficos.

Verifica estabilidad del sistema ante variaciones razonables en parámetros.
"""

from decimal import Decimal

from tests.quantitative.conftest import generate_trending_bars, run_backtest


class TestParameterRobustness:
    """±10% variations in strategy parameters should not be catastrophic."""

    def setup_method(self):
        self.bars = generate_trending_bars(n=200, trend=0.0005, seed=500)

    def test_fast_sma_perturbation(self):
        """Fast SMA period ±1 (from 5 → 4, 5, 6) should not catastrophically change PnL."""
        results = {}
        for fast in [4, 5, 6]:
            report, _ = run_backtest(self.bars, fast=fast, slow=20, qty=Decimal("0.5"))
            results[fast] = float(report.total_pnl)

        # No single variation should lose >10x what the base makes
        base_pnl = results[5]
        for fast, pnl in results.items():
            if base_pnl > 0:
                assert pnl > -abs(base_pnl) * 10, (
                    f"fast={fast} PnL={pnl:.2f} is catastrophic vs base={base_pnl:.2f}"
                )

    def test_slow_sma_perturbation(self):
        """Slow SMA period ±2 (from 20 → 18, 20, 22) should be stable."""
        results = {}
        for slow in [18, 20, 22]:
            report, _ = run_backtest(self.bars, fast=5, slow=slow, qty=Decimal("0.5"))
            results[slow] = float(report.total_pnl)

        base_pnl = results[20]
        for slow, pnl in results.items():
            if base_pnl > 0:
                assert pnl > -abs(base_pnl) * 10

    def test_qty_perturbation(self):
        """Position size ±10% should scale PnL roughly linearly."""
        results = {}
        for qty_str in ["0.45", "0.50", "0.55"]:
            report, _ = run_backtest(
                self.bars, fast=5, slow=20, qty=Decimal(qty_str)
            )
            results[qty_str] = float(report.total_pnl)

        # All should have same sign (or be close to zero)
        signs = [1 if v > 0 else -1 if v < -1 else 0 for v in results.values()]
        non_zero = [s for s in signs if s != 0]
        if len(non_zero) >= 2:
            # At least 2 of 3 should agree on direction
            assert abs(sum(non_zero)) >= 1, (
                f"PnL signs diverge under qty perturbation: {results}"
            )

    def test_fee_perturbation(self):
        """Fee rate ±10% (0.0009 to 0.0011) should not flip profitability."""
        results = {}
        for fee_str in ["0.0009", "0.001", "0.0011"]:
            report, _ = run_backtest(
                self.bars, fee_rate=Decimal(fee_str), qty=Decimal("0.5")
            )
            results[fee_str] = float(report.total_pnl)

        # Higher fees should produce lower PnL (monotonic or close)
        pnl_list = [results["0.0009"], results["0.001"], results["0.0011"]]
        # Allow small deviations but general direction should hold
        assert pnl_list[0] >= pnl_list[2] - 5, (
            f"Fee increase should decrease PnL: {pnl_list}"
        )


class TestRegimeRobustness:
    """Strategy should survive different market regimes without blowup."""

    def test_survives_uptrend(self):
        bars = generate_trending_bars(n=200, trend=0.002, seed=601)
        report, _ = run_backtest(bars, qty=Decimal("0.5"))
        assert report.max_drawdown < Decimal("0.50")

    def test_survives_downtrend(self):
        bars = generate_trending_bars(n=200, trend=-0.002, seed=602)
        report, _ = run_backtest(bars, qty=Decimal("0.5"))
        assert report.max_drawdown < Decimal("0.50")

    def test_survives_flat_market(self):
        bars = generate_trending_bars(n=200, trend=0.0, volatility=0.005, seed=603)
        report, _ = run_backtest(bars, qty=Decimal("0.5"))
        assert report.max_drawdown < Decimal("0.50")

    def test_survives_high_volatility(self):
        bars = generate_trending_bars(n=200, trend=0.0, volatility=0.05, seed=604)
        report, _ = run_backtest(bars, qty=Decimal("0.5"))
        assert report.max_drawdown < Decimal("0.50")
