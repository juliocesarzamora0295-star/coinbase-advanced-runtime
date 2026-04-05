"""
Tests para CircuitBreaker.force_open() y telemetría operacional.

Invariantes testeadas:
- force_open() trips the breaker to OPEN state
- force_open() sets trip_reason
- check_before_trade() returns False after force_open()
- Telemetry: record_execution_result affects reject_rate check
- Telemetry: record_latency affects latency check
- Telemetry: record_ws_gap trips breaker
"""

from decimal import Decimal

from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker


def make_breaker(**kwargs) -> CircuitBreaker:
    cfg = BreakerConfig(**kwargs)
    breaker = CircuitBreaker(cfg)
    breaker.reset_day(Decimal("10000"))
    return breaker


class TestForceOpen:
    """force_open() como API pública."""

    def test_force_open_trips_breaker(self):
        breaker = make_breaker()
        assert breaker.state == BreakerState.CLOSED
        breaker.force_open("OMS degraded")
        assert breaker.state == BreakerState.OPEN
        assert breaker.trip_reason == "OMS degraded"

    def test_check_returns_false_after_force_open(self):
        breaker = make_breaker()
        breaker.force_open("manual stop")
        ok, reason = breaker.check_before_trade()
        assert ok is False
        assert "manual stop" in reason

    def test_force_open_idempotent(self):
        breaker = make_breaker()
        breaker.force_open("first reason")
        breaker.force_open("second reason")
        # First reason preserved (already OPEN)
        assert breaker.trip_reason == "first reason"


class TestTelemetryTrips:
    """Telemetry signals trigger breaker trips."""

    def test_ws_gap_trips_breaker(self):
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.execution.record_ws_gap()
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert breaker.state == BreakerState.OPEN

    def test_high_reject_rate_trips(self):
        breaker = make_breaker(reject_rate_threshold=0.03)
        # 4 failures out of 100 = 4% > 3%
        for _ in range(96):
            breaker.execution.record_request(True)
        for _ in range(4):
            breaker.execution.record_request(False)
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert "Reject rate" in breaker.trip_reason

    def test_high_latency_trips(self):
        breaker = make_breaker(latency_p95_threshold_ms=100.0)
        for _ in range(20):
            breaker.record_latency(200.0)  # all above threshold
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert "Latency" in breaker.trip_reason

    def test_consecutive_losses_trip(self):
        breaker = make_breaker(max_consecutive_losses=3)
        for _ in range(3):
            breaker.record_trade_pnl(Decimal("-100"))
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert "Consecutive" in breaker.trip_reason

    def test_daily_loss_trips(self):
        breaker = make_breaker(max_daily_loss=0.05)
        breaker.reset_day(Decimal("10000"))
        breaker.update_equity(Decimal("9400"))  # -6% > -5% threshold
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert "Daily loss" in breaker.trip_reason

    def test_drawdown_trips(self):
        breaker = make_breaker(max_drawdown=0.10, max_daily_loss=1.0)
        breaker.reset_day(Decimal("10000"))
        breaker.update_equity(Decimal("8900"))  # 11% dd > 10%
        ok, _ = breaker.check_before_trade()
        assert ok is False
        assert "drawdown" in breaker.trip_reason.lower()

    def test_healthy_metrics_pass(self):
        breaker = make_breaker()
        breaker.reset_day(Decimal("10000"))
        breaker.update_equity(Decimal("10000"))
        for _ in range(10):
            breaker.record_latency(50.0)
            breaker.execution.record_request(True)
        ok, reason = breaker.check_before_trade()
        assert ok is True
        assert reason is None
