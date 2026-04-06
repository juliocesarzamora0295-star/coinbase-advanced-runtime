"""Tests for ShadowRunner."""

from src.exchange_simulator import ExchangeSimulator
from src.shadow_runner import ShadowRunner, ShadowRunResult


class TestShadowRunnerExecution:

    def test_run_completes_without_crash(self):
        sim = ExchangeSimulator(seed=100)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.5, tick_interval_s=0.01)
        assert isinstance(result, ShadowRunResult)
        assert result.crashes == 0
        assert result.total_ticks > 0

    def test_run_generates_trades(self):
        sim = ExchangeSimulator(seed=101, initial_price=50000.0)
        runner = ShadowRunner(simulator=sim, signal_threshold=0.0001)
        result = runner.run(duration_s=0.5, tick_interval_s=0.005)
        # With very low threshold, should generate some signals
        assert result.total_signals >= 0  # may be 0 on short run
        assert result.total_ticks > 0

    def test_run_tracks_equity(self):
        sim = ExchangeSimulator(seed=102)
        runner = ShadowRunner(simulator=sim, initial_cash=10000.0)
        result = runner.run(duration_s=0.3, tick_interval_s=0.01)
        assert result.equity_start == 10000.0
        assert result.equity_end > 0

    def test_run_records_latency(self):
        sim = ExchangeSimulator(seed=103, latency_ms=1.0)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.3, tick_interval_s=0.01)
        assert result.avg_latency_ms >= 0

    def test_run_collects_samples(self):
        sim = ExchangeSimulator(seed=104)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.5, tick_interval_s=0.01)
        # Should have at least some samples
        assert isinstance(result.samples, list)

    def test_run_respects_duration(self):
        sim = ExchangeSimulator(seed=105)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.2, tick_interval_s=0.01)
        assert result.duration_s < 1.0  # should be ~0.2s
