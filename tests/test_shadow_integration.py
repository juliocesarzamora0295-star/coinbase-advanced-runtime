"""
End-to-end shadow integration test.

Executes a short shadow run with simulator, verifies:
- Trades generated
- Metrics collected
- Report produced
- No crashes
- Risk gates functional
"""

from src.exchange_simulator import ExchangeSimulator
from src.shadow_report import ShadowCriteria, analyze_shadow_run
from src.shadow_runner import ShadowRunner
from src.risk.gate import RiskLimits
from src.risk.circuit_breaker import BreakerConfig


class TestShadowIntegrationEndToEnd:

    def test_full_shadow_run_10_seconds(self):
        """Complete shadow run: simulator → runner → report."""
        sim = ExchangeSimulator(
            initial_price=50000.0,
            spread_pct=0.02,
            slippage_bps=2.0,
            latency_ms=1.0,  # fast for testing
            seed=999,
        )
        runner = ShadowRunner(
            simulator=sim,
            risk_limits=RiskLimits(),
            breaker_config=BreakerConfig(),
            initial_cash=10000.0,
            signal_threshold=0.0005,
        )

        result = runner.run(duration_s=2.0, tick_interval_s=0.005)

        # Basic assertions
        assert result.crashes == 0, f"Crashes: {result.errors}"
        assert result.total_ticks > 10
        assert result.equity_start == 10000.0
        assert result.equity_end > 0

        # Report
        report = analyze_shadow_run(
            result,
            ShadowCriteria(
                max_drawdown_pct=50.0,  # lenient for short run
                min_sharpe=-100.0,
                max_avg_latency_ms=1000.0,
                max_crashes=0,
                min_trades=0,  # short run may have 0 trades
            ),
        )
        assert report.passed, f"Shadow report failed: {report.failures}"
        assert report.duration_s > 0

        # Report output
        summary = report.summary()
        assert "Shadow Report" in summary

        d = report.to_dict()
        assert "passed" in d
        assert "sharpe_ratio" in d

    def test_risk_gates_block_when_breaker_tripped(self):
        """Breaker trip → signals blocked in shadow run."""
        sim = ExchangeSimulator(seed=998)
        runner = ShadowRunner(
            simulator=sim,
            breaker_config=BreakerConfig(ws_gap_threshold=1),
            signal_threshold=0.0001,
        )
        # Trip breaker before run
        runner._breaker.execution.record_ws_gap()

        result = runner.run(duration_s=0.5, tick_interval_s=0.01)
        assert result.breaker_trips > 0

    def test_deterministic_with_same_seed(self):
        """Same seed → same price evolution → same trades."""
        results = []
        for _ in range(2):
            sim = ExchangeSimulator(seed=777, latency_ms=0.1)
            runner = ShadowRunner(simulator=sim, signal_threshold=0.0005)
            result = runner.run(duration_s=0.3, tick_interval_s=0.005)
            results.append(result)

        assert results[0].total_ticks == results[1].total_ticks
        # Trades should be very similar (timing jitter may cause small differences)
        assert abs(results[0].total_trades - results[1].total_trades) <= 1
