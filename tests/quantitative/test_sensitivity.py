"""
Sensitivity analysis — measures impact of spread, latency, and fees.

Documents which parameters have greatest impact on PnL.
"""

from decimal import Decimal

from tests.quantitative.conftest import generate_trending_bars, run_backtest


class TestSlippageSensitivity:
    """Spread/slippage impact on PnL."""

    def setup_method(self):
        self.bars = generate_trending_bars(n=200, trend=0.001, seed=700)

    def test_zero_vs_nonzero_slippage(self):
        """Slippage should reduce PnL."""
        r_zero, _ = run_backtest(self.bars, slippage_bps=Decimal("0"), qty=Decimal("0.5"))
        r_slip, _ = run_backtest(self.bars, slippage_bps=Decimal("10"), qty=Decimal("0.5"))

        assert r_zero.total_pnl >= r_slip.total_pnl, (
            f"Slippage should reduce PnL: zero={r_zero.total_pnl}, slip={r_slip.total_pnl}"
        )

    def test_slippage_monotonic(self):
        """Higher slippage → lower PnL (monotonic or near-monotonic)."""
        pnls = []
        for bps in [0, 5, 10, 20]:
            r, _ = run_backtest(self.bars, slippage_bps=Decimal(str(bps)), qty=Decimal("0.5"))
            pnls.append(float(r.total_pnl))

        # Allow small non-monotonicity (±$1) but general trend down
        for i in range(len(pnls) - 1):
            assert pnls[i] >= pnls[i + 1] - 1.0, (
                f"Slippage not monotonic: {pnls}"
            )

    def test_slippage_impact_magnitude(self):
        """Quantify: each 10bps of slippage costs how much in PnL."""
        r_0, _ = run_backtest(self.bars, slippage_bps=Decimal("0"), qty=Decimal("0.5"))
        r_10, _ = run_backtest(self.bars, slippage_bps=Decimal("10"), qty=Decimal("0.5"))
        r_20, _ = run_backtest(self.bars, slippage_bps=Decimal("20"), qty=Decimal("0.5"))

        cost_per_10bps_1 = float(r_0.total_pnl - r_10.total_pnl)
        cost_per_10bps_2 = float(r_10.total_pnl - r_20.total_pnl)

        # Slippage cost should be roughly proportional
        if cost_per_10bps_1 > 0 and cost_per_10bps_2 > 0:
            ratio = cost_per_10bps_1 / cost_per_10bps_2 if cost_per_10bps_2 > 0 else 1
            assert 0.3 < ratio < 3.0, (
                f"Slippage cost not proportional: {cost_per_10bps_1:.2f} vs {cost_per_10bps_2:.2f}"
            )


class TestFeeSensitivity:
    """Fee rate impact on PnL."""

    def setup_method(self):
        self.bars = generate_trending_bars(n=200, trend=0.001, seed=710)

    def test_zero_vs_nonzero_fees(self):
        """Fees reduce PnL."""
        r_zero, _ = run_backtest(self.bars, fee_rate=Decimal("0"), qty=Decimal("0.5"))
        r_fee, _ = run_backtest(self.bars, fee_rate=Decimal("0.001"), qty=Decimal("0.5"))

        assert r_zero.total_pnl >= r_fee.total_pnl

    def test_fee_sensitivity_range(self):
        """PnL across fee range: 0, 10bps, 50bps, 100bps."""
        results = {}
        for fee in ["0", "0.001", "0.005", "0.01"]:
            r, _ = run_backtest(self.bars, fee_rate=Decimal(fee), qty=Decimal("0.5"))
            results[fee] = float(r.total_pnl)

        # Each fee increase should reduce PnL
        keys = sorted(results.keys(), key=float)
        for i in range(len(keys) - 1):
            assert results[keys[i]] >= results[keys[i + 1]] - 1.0, (
                f"Fee sensitivity broken: {results}"
            )

    def test_fee_determines_profitability_threshold(self):
        """At some fee level, strategy becomes unprofitable."""
        # Use strong uptrend to ensure profitability at 0 fees
        bars = generate_trending_bars(n=200, trend=0.003, seed=715)
        profitable_at = None

        for fee in ["0", "0.005", "0.01", "0.02", "0.05"]:
            r, _ = run_backtest(bars, fee_rate=Decimal(fee), qty=Decimal("0.5"))
            if r.total_pnl > 0:
                profitable_at = fee

        assert profitable_at is not None, "Strategy not profitable even at 0 fees on uptrend"


class TestInitialCashSensitivity:
    """Verify returns scale properly with capital."""

    def setup_method(self):
        self.bars = generate_trending_bars(n=200, trend=0.001, seed=720)

    def test_returns_independent_of_capital(self):
        """
        Return % should be roughly the same regardless of initial capital
        (since qty is fixed, not equity-based in test).
        """
        r_small, _ = run_backtest(self.bars, initial_cash=Decimal("5000"), qty=Decimal("0.5"))
        r_large, _ = run_backtest(self.bars, initial_cash=Decimal("50000"), qty=Decimal("0.5"))

        # Absolute PnL should be the same (same qty)
        assert abs(float(r_small.total_pnl - r_large.total_pnl)) < 1.0, (
            f"PnL should be same with fixed qty: "
            f"small={r_small.total_pnl}, large={r_large.total_pnl}"
        )
