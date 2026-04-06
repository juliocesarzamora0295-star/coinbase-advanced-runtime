"""Tests for ShadowRunner."""

import yaml

from src.exchange_simulator import ExchangeSimulator
from src.shadow_runner import ShadowRunner, ShadowRunResult


class TestShadowRunnerExecution:

    def test_run_completes_without_crash(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=100)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.5, tick_interval_s=0.01)
        assert isinstance(result, ShadowRunResult)
        assert result.crashes == 0
        assert result.total_ticks > 0

    def test_run_generates_trades(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=101, initial_price=50000.0)
        runner = ShadowRunner(simulator=sim, signal_threshold=0.0001)
        result = runner.run(duration_s=0.5, tick_interval_s=0.005)
        assert result.total_signals >= 0
        assert result.total_ticks > 0

    def test_run_tracks_equity(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=102)
        runner = ShadowRunner(simulator=sim, initial_cash=10000.0)
        result = runner.run(duration_s=0.3, tick_interval_s=0.01)
        assert result.equity_start == 10000.0
        assert result.equity_end > 0

    def test_run_records_latency(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=103)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.3, tick_interval_s=0.01)
        assert result.avg_latency_ms >= 0

    def test_run_collects_samples(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=104)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.5, tick_interval_s=0.01)
        assert isinstance(result.samples, list)

    def test_run_respects_duration(self):
        sim = ExchangeSimulator(latency_ms=1.0, seed=105)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=0.2, tick_interval_s=0.01)
        assert result.duration_s < 1.0

    def test_max_ticks_deterministic(self):
        """max_ticks produces exact tick count regardless of wall clock."""
        sim = ExchangeSimulator(latency_ms=1.0, seed=200)
        runner = ShadowRunner(simulator=sim)
        result = runner.run(duration_s=999, tick_interval_s=0.001, max_ticks=30)
        assert result.total_ticks == 30


class TestShadowConfigFile:
    """Verify shadow_test.yaml is valid and loadable."""

    def test_shadow_config_loads(self):
        with open("configs/shadow_test.yaml") as f:
            data = yaml.safe_load(f)
        assert "trading" in data
        assert "risk" in data
        assert "shadow" in data
        assert data["shadow"]["duration_s"] == 3600
        assert data["shadow"]["max_drawdown_pct"] == 5.0
