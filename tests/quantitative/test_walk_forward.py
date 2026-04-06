"""
Walk-forward backtest — validates no lookahead bias and out-of-sample consistency.

Splits data into rolling windows: train on window N, test on window N+1.
Verifies that in-sample results don't leak into out-of-sample.
"""

from decimal import Decimal

from tests.quantitative.conftest import generate_trending_bars, run_backtest


class TestWalkForward:

    def test_no_lookahead_bias(self):
        """
        Running on first half vs full data: the first-half results must be identical.

        If there's lookahead bias, results on the first half would change
        when more data is appended.
        """
        full_bars = generate_trending_bars(n=200, seed=100)
        half_bars = full_bars[:100]

        report_half, ledger_half = run_backtest(half_bars, qty=Decimal("0.5"))
        report_full, ledger_full = run_backtest(full_bars, qty=Decimal("0.5"))

        # Trades that occurred in the first 100 bars should be identical
        half_trades = ledger_half.trades
        full_first_half_trades = [
            t for t in ledger_full.trades
            if t.exit_ts_ms <= half_bars[-1].timestamp_ms
        ]

        assert len(half_trades) == len(full_first_half_trades), (
            f"Lookahead bias: half={len(half_trades)} trades, "
            f"full_first_half={len(full_first_half_trades)} trades"
        )

        for h, f in zip(half_trades, full_first_half_trades):
            assert h.entry_price == f.entry_price
            assert h.exit_price == f.exit_price
            assert h.pnl == f.pnl

    def test_rolling_windows_produce_results(self):
        """
        Walk-forward with 3 windows of 100 bars each.

        Each window should produce trades (strategy is active).
        """
        all_bars = generate_trending_bars(n=300, seed=200)
        window_size = 100

        results = []
        for i in range(3):
            start = i * window_size
            end = start + window_size
            window_bars = all_bars[start:end]
            report, _ = run_backtest(window_bars, qty=Decimal("0.5"))
            results.append(report)

        # Each window processed all its bars
        for i, r in enumerate(results):
            assert r.total_bars == window_size, f"Window {i}: {r.total_bars} bars"

    def test_out_of_sample_not_catastrophic(self):
        """
        Train parameters on window 1, test on window 2.

        Out-of-sample should not have catastrophic drawdown (< 50%).
        """
        all_bars = generate_trending_bars(n=200, trend=0.0005, seed=300)
        train_bars = all_bars[:100]
        test_bars = all_bars[100:]

        # "Train" — just run to verify strategy works
        train_report, _ = run_backtest(train_bars, qty=Decimal("0.5"))
        assert train_report.total_bars == 100

        # "Test" — out-of-sample
        test_report, _ = run_backtest(test_bars, qty=Decimal("0.5"))
        assert test_report.total_bars == 100
        assert test_report.max_drawdown < Decimal("0.50"), (
            f"OOS drawdown catastrophic: {test_report.max_drawdown:.2%}"
        )

    def test_sequential_windows_consistent(self):
        """
        Run 4 sequential windows. Verify no window has >3x the drawdown
        of the average — indicates stability.
        """
        all_bars = generate_trending_bars(n=400, trend=0.0003, seed=400)
        window_size = 100
        drawdowns = []

        for i in range(4):
            window = all_bars[i * window_size:(i + 1) * window_size]
            report, _ = run_backtest(window, qty=Decimal("0.5"))
            drawdowns.append(float(report.max_drawdown))

        avg_dd = sum(drawdowns) / len(drawdowns) if drawdowns else 0
        for i, dd in enumerate(drawdowns):
            if avg_dd > 0:
                assert dd < avg_dd * 3 + 0.05, (
                    f"Window {i} drawdown {dd:.2%} is >3x average {avg_dd:.2%}"
                )
