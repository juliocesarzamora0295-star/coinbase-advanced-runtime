"""
Tests para CircuitBreaker.

Invariantes testeadas:
- Estado inicial: CLOSED
- CLOSED → OPEN por daily loss
- CLOSED → OPEN por drawdown
- CLOSED → OPEN por consecutive losses
- CLOSED → OPEN por latency p95
- CLOSED → OPEN por reject rate
- CLOSED → OPEN por ws_gap
- OPEN permanece OPEN durante cooldown
- OPEN → HALF_OPEN tras cooldown
- HALF_OPEN → CLOSED tras N trades exitosos
- HALF_OPEN → OPEN si trade falla durante recuperación
- Trip registra trip_reason no vacío
- get_status() coherente con estado interno
"""

from datetime import datetime, timedelta
from decimal import Decimal

from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker


def make_breaker(**overrides) -> CircuitBreaker:
    """Factory con defaults de prueba agresivos para activar fácilmente."""
    defaults = dict(
        max_daily_loss=0.05,
        max_drawdown=0.15,
        max_consecutive_losses=3,
        latency_p95_threshold_ms=500.0,
        reject_rate_threshold=0.03,
        slippage_drift_threshold_bps=10.0,
        rate_limit_threshold=5,
        ws_gap_threshold=1,
        recovery_cooldown_minutes=30,
        recovery_test_trades=2,
    )
    defaults.update(overrides)
    return CircuitBreaker(BreakerConfig(**defaults))


class TestInitialState:

    def test_initial_state_is_closed(self):
        breaker = make_breaker()
        assert breaker.state == BreakerState.CLOSED

    def test_check_before_trade_allowed_when_closed_no_conditions(self):
        """check_before_trade → (True, None) cuando CLOSED sin condiciones de trip."""
        breaker = make_breaker()
        breaker.reset_day(Decimal("10000"))

        allowed, reason = breaker.check_before_trade()
        assert allowed is True
        assert reason is None


class TestTripByDailyLoss:

    def test_trips_when_daily_loss_exceeds_threshold(self):
        """CLOSED → OPEN cuando daily_loss > max_daily_loss."""
        breaker = make_breaker(max_daily_loss=0.05)
        breaker.reset_day(Decimal("10000"))

        # Pérdida del 6% (> 5% threshold)
        breaker.update_equity(Decimal("9400"))

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_no_trip_when_daily_loss_below_threshold(self):
        """No se dispara cuando daily_loss < max_daily_loss."""
        breaker = make_breaker(max_daily_loss=0.05)
        breaker.reset_day(Decimal("10000"))

        # Pérdida del 3% (< 5%)
        breaker.update_equity(Decimal("9700"))

        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestTripByDrawdown:

    def test_trips_when_drawdown_exceeds_threshold(self):
        """CLOSED → OPEN cuando drawdown >= max_drawdown."""
        breaker = make_breaker(max_drawdown=0.15)
        breaker.reset_day(Decimal("10000"))

        # Drawdown 20%: peak=10000, now=8000
        breaker.update_equity(Decimal("8000"))

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_no_trip_when_drawdown_below_threshold(self):
        """No se dispara cuando drawdown < max_drawdown.

        Se usa max_daily_loss=1.0 para aislar la condición de drawdown
        y evitar que el daily_loss check dispare en el mismo escenario.
        """
        breaker = make_breaker(max_drawdown=0.15, max_daily_loss=1.0)
        breaker.reset_day(Decimal("10000"))

        # Drawdown 10%: 10000 → 9000 (< 15% threshold)
        breaker.update_equity(Decimal("9000"))

        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestTripByConsecutiveLosses:

    def test_trips_after_consecutive_losses(self):
        """CLOSED → OPEN cuando consecutive_losses >= threshold."""
        breaker = make_breaker(max_consecutive_losses=3)
        breaker.reset_day(Decimal("10000"))

        for _ in range(3):
            breaker.record_trade_pnl(Decimal("-100"))

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_win_resets_consecutive_loss_counter(self):
        """Una ganancia resetea el contador de pérdidas consecutivas."""
        breaker = make_breaker(max_consecutive_losses=3)
        breaker.reset_day(Decimal("10000"))

        breaker.record_trade_pnl(Decimal("-100"))
        breaker.record_trade_pnl(Decimal("-100"))
        breaker.record_trade_pnl(Decimal("+500"))  # reset
        breaker.record_trade_pnl(Decimal("-100"))

        # Contador = 1 (< 3) → no dispara
        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestTripByLatency:

    def test_trips_on_high_p95_latency(self):
        """CLOSED → OPEN cuando latencia p95 > threshold."""
        breaker = make_breaker(latency_p95_threshold_ms=100.0)
        breaker.reset_day(Decimal("10000"))

        # Agregar muestras de alta latencia (> 100ms)
        for _ in range(20):
            breaker.record_latency(200.0)

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_no_trip_when_latency_below_threshold(self):
        """No se dispara cuando latencia p95 < threshold."""
        breaker = make_breaker(latency_p95_threshold_ms=500.0)
        breaker.reset_day(Decimal("10000"))

        for _ in range(20):
            breaker.record_latency(100.0)

        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestTripByRejectRate:

    def test_trips_on_high_reject_rate(self):
        """CLOSED → OPEN cuando reject_rate > threshold."""
        breaker = make_breaker(reject_rate_threshold=0.03)
        breaker.reset_day(Decimal("10000"))

        # 5/100 = 5% > 3%
        for _ in range(95):
            breaker.record_execution_result(True)
        for _ in range(5):
            breaker.record_execution_result(False)

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_no_trip_with_zero_rejections(self):
        """0% rejection rate no dispara."""
        breaker = make_breaker(reject_rate_threshold=0.03)
        breaker.reset_day(Decimal("10000"))

        for _ in range(20):
            breaker.record_execution_result(True)

        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestTripByWsGap:

    def test_trips_on_ws_gap(self):
        """CLOSED → OPEN cuando ws_gaps >= ws_gap_threshold."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))

        breaker.record_ws_gap()

        allowed, _ = breaker.check_before_trade()
        assert not allowed
        assert breaker.state == BreakerState.OPEN

    def test_no_trip_without_ws_gaps(self):
        """0 ws_gaps no dispara."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))

        allowed, _ = breaker.check_before_trade()
        assert allowed is True


class TestOpenStateInvariants:

    def test_open_blocks_all_subsequent_checks(self):
        """Una vez OPEN, check_before_trade retorna False."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))
        breaker.record_ws_gap()
        breaker.check_before_trade()

        assert breaker.state == BreakerState.OPEN

        allowed, reason = breaker.check_before_trade()
        assert not allowed
        assert reason is not None

    def test_trip_sets_reason(self):
        """trip_reason está seteado tras OPEN."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))
        breaker.record_ws_gap()
        breaker.check_before_trade()

        assert breaker.trip_reason is not None
        assert len(breaker.trip_reason) > 0

    def test_trip_sets_trip_time(self):
        """trip_time está seteado tras OPEN."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))
        breaker.record_ws_gap()
        breaker.check_before_trade()

        assert breaker.trip_time is not None


class TestRecoveryFlow:
    """Tests de recuperación OPEN → HALF_OPEN → CLOSED."""

    def test_transitions_to_half_open_after_cooldown(self):
        """OPEN → HALF_OPEN después del recovery_cooldown_minutes."""
        breaker = make_breaker(recovery_cooldown_minutes=30, recovery_test_trades=2)
        breaker.reset_day(Decimal("10000"))

        # Abrir
        breaker.record_ws_gap()
        breaker.check_before_trade()
        assert breaker.state == BreakerState.OPEN

        # Simular que pasó el tiempo de cooldown
        breaker.trip_time = datetime.now() - timedelta(minutes=31)

        # Siguiente check debe transitar a HALF_OPEN
        breaker.check_before_trade()
        assert breaker.state == BreakerState.HALF_OPEN

    def test_transitions_to_closed_after_successful_recovery(self):
        """HALF_OPEN → CLOSED después de recovery_test_trades exitosos."""
        breaker = make_breaker(recovery_cooldown_minutes=1, recovery_test_trades=2)
        breaker.reset_day(Decimal("10000"))

        # Abrir y pasar cooldown
        breaker.record_ws_gap()
        breaker.check_before_trade()
        breaker.trip_time = datetime.now() - timedelta(minutes=2)

        # HALF_OPEN
        breaker.check_before_trade()
        assert breaker.state == BreakerState.HALF_OPEN

        # Simular N trades exitosos (inyectar recovery_success_count directamente)
        breaker.recovery_success_count = 2

        # Siguiente check debe cerrar
        allowed, _ = breaker.check_before_trade()
        assert breaker.state == BreakerState.CLOSED
        assert breaker.trip_reason is None

    def test_half_open_trips_back_to_open_on_loss(self):
        """HALF_OPEN → OPEN si un trade de recuperación falla."""
        breaker = make_breaker(recovery_cooldown_minutes=1, recovery_test_trades=3)
        breaker.reset_day(Decimal("10000"))

        # Abrir y pasar cooldown
        breaker.record_ws_gap()
        breaker.check_before_trade()
        breaker.trip_time = datetime.now() - timedelta(minutes=2)

        # HALF_OPEN
        breaker.check_before_trade()
        assert breaker.state == BreakerState.HALF_OPEN

        # Trade fallido durante recuperación
        breaker.record_trade_pnl(Decimal("-500"))

        assert breaker.state == BreakerState.OPEN


class TestGetStatus:

    def test_get_status_includes_state(self):
        """get_status() incluye el estado actual."""
        breaker = make_breaker()
        status = breaker.get_status()
        assert "state" in status
        assert status["state"] == BreakerState.CLOSED.value

    def test_get_status_after_trip_reflects_open(self):
        """get_status() refleja OPEN tras trip."""
        breaker = make_breaker(ws_gap_threshold=1)
        breaker.reset_day(Decimal("10000"))
        breaker.record_ws_gap()
        breaker.check_before_trade()

        status = breaker.get_status()
        assert status["state"] == BreakerState.OPEN.value
        assert status["trip_reason"] is not None
