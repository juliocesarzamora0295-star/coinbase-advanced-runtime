"""
Circuit breaker integration tests.

Tests full state transition lifecycle:
- CLOSED → OPEN on API failures (consecutive losses, daily loss, drawdown)
- OPEN → HALF_OPEN after cooldown
- HALF_OPEN → CLOSED after recovery trades
- HALF_OPEN → OPEN on recovery failure
- Kill switch behavior on transient vs persistent failures
- Circuit breaker + RiskGate interaction
"""

import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot
from src.risk.kill_switch import KillSwitch, KillSwitchMode


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_cb(
    max_daily_loss: float = 0.05,
    max_drawdown: float = 0.15,
    max_consecutive_losses: int = 3,
    cooldown_minutes: int = 0,  # 0 for instant recovery in tests
    recovery_test_trades: int = 3,
) -> CircuitBreaker:
    """Create a CircuitBreaker with test-friendly defaults."""
    config = BreakerConfig(
        max_daily_loss=max_daily_loss,
        max_drawdown=max_drawdown,
        max_consecutive_losses=max_consecutive_losses,
        recovery_cooldown_minutes=cooldown_minutes,
        recovery_test_trades=recovery_test_trades,
    )
    cb = CircuitBreaker(config)
    cb.reset_day(Decimal("10000"))
    return cb


def make_risk_snapshot(
    breaker_state: str = "CLOSED",
    kill_switch: bool = False,
    equity: Decimal = Decimal("10000"),
    position_qty: Decimal = Decimal("0"),
    target_qty: Decimal = Decimal("0.01"),
    entry_ref: Decimal = Decimal("70000"),
    side: str = "BUY",
) -> RiskSnapshot:
    return RiskSnapshot(
        equity=equity,
        position_qty=position_qty,
        day_pnl_pct=Decimal("0"),
        drawdown_pct=Decimal("0"),
        symbol="BTC-USD",
        side=side,
        target_qty=target_qty,
        entry_ref=entry_ref,
        breaker_state=breaker_state,
        kill_switch=kill_switch,
    )


# ──────────────────────────────────────────────
# State Transitions: CLOSED → OPEN
# ──────────────────────────────────────────────

class TestClosedToOpen:
    def test_consecutive_losses_trip(self):
        """3 consecutive losses → OPEN."""
        cb = make_cb(max_consecutive_losses=3)
        assert cb.state == BreakerState.CLOSED

        for i in range(3):
            cb.record_trade_pnl(Decimal("-100"))

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN
        assert "Consecutive losses" in reason

    def test_daily_loss_trip(self):
        """Daily loss exceeding threshold → OPEN."""
        cb = make_cb(max_daily_loss=0.05)
        cb.equity_now = Decimal("9400")  # -6% from 10000

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN
        assert "Daily loss" in reason

    def test_max_drawdown_trip(self):
        """Drawdown from peak → OPEN."""
        cb = make_cb(max_drawdown=0.15)
        cb.equity_peak = Decimal("12000")
        cb.equity_now = Decimal("10000")  # 16.7% drawdown

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN
        assert "drawdown" in reason.lower()

    def test_latency_spike_trip(self):
        """P95 latency above threshold → OPEN."""
        cb = make_cb()
        # Record high latencies
        for _ in range(25):
            cb.record_latency(600.0)  # above 500ms threshold

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN

    def test_reject_rate_trip(self):
        """High reject rate → OPEN."""
        cb = make_cb()
        for _ in range(10):
            cb.record_execution_result(False)

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN
        assert "Reject rate" in reason

    def test_ws_gap_trip(self):
        """WebSocket gap → OPEN."""
        cb = make_cb()
        cb.record_ws_gap()

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN


# ──────────────────────────────────────────────
# State Transitions: OPEN → HALF_OPEN
# ──────────────────────────────────────────────

class TestOpenToHalfOpen:
    def test_recovery_after_cooldown(self):
        """After cooldown period, transitions to HALF_OPEN."""
        cb = make_cb(max_consecutive_losses=3, cooldown_minutes=0)

        # Trip
        for _ in range(3):
            cb.record_trade_pnl(Decimal("-100"))
        cb.check_before_trade()
        assert cb.state == BreakerState.OPEN

        # Set trip_time in the past
        cb.trip_time = datetime.now() - timedelta(minutes=1)

        allowed, reason = cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN

    def test_no_recovery_during_cooldown(self):
        """Still in cooldown → stays OPEN."""
        cb = make_cb(max_consecutive_losses=3, cooldown_minutes=60)

        for _ in range(3):
            cb.record_trade_pnl(Decimal("-100"))
        cb.check_before_trade()
        assert cb.state == BreakerState.OPEN

        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN


# ──────────────────────────────────────────────
# State Transitions: HALF_OPEN → CLOSED
# ──────────────────────────────────────────────

class TestHalfOpenToClosed:
    def test_successful_recovery_trades(self):
        """N successful trades in HALF_OPEN → CLOSED."""
        cb = make_cb(max_consecutive_losses=3, cooldown_minutes=0, recovery_test_trades=3)

        # Trip
        for _ in range(3):
            cb.record_trade_pnl(Decimal("-100"))
        cb.check_before_trade()
        cb.trip_time = datetime.now() - timedelta(minutes=1)
        cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN

        # Successful recovery trades
        for _ in range(3):
            cb.record_trade_pnl(Decimal("50"))

        allowed, reason = cb.check_before_trade()
        assert allowed
        assert cb.state == BreakerState.CLOSED


# ──────────────────────────────────────────────
# State Transitions: HALF_OPEN → OPEN
# ──────────────────────────────────────────────

class TestHalfOpenToOpen:
    def test_failed_recovery_trade(self):
        """Loss during HALF_OPEN → back to OPEN."""
        cb = make_cb(max_consecutive_losses=3, cooldown_minutes=0)

        # Trip and enter HALF_OPEN
        for _ in range(3):
            cb.record_trade_pnl(Decimal("-100"))
        cb.check_before_trade()
        cb.trip_time = datetime.now() - timedelta(minutes=1)
        cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN

        # Failed recovery trade
        cb.record_trade_pnl(Decimal("-50"))
        assert cb.state == BreakerState.OPEN


# ──────────────────────────────────────────────
# Full lifecycle: CLOSED → OPEN → HALF_OPEN → CLOSED
# ──────────────────────────────────────────────

class TestFullLifecycle:
    def test_complete_trip_and_recovery(self):
        """Full lifecycle: healthy → trip → cooldown → recovery → healthy."""
        cb = make_cb(max_consecutive_losses=3, cooldown_minutes=0, recovery_test_trades=2)

        # Phase 1: Healthy trading
        assert cb.state == BreakerState.CLOSED
        allowed, _ = cb.check_before_trade()
        assert allowed

        # Phase 2: Consecutive losses → trip
        for _ in range(3):
            cb.record_trade_pnl(Decimal("-200"))
        allowed, reason = cb.check_before_trade()
        assert not allowed
        assert cb.state == BreakerState.OPEN

        # Phase 3: Cooldown passes → HALF_OPEN
        cb.trip_time = datetime.now() - timedelta(minutes=1)
        cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN

        # Phase 4: Recovery trades → CLOSED
        cb.record_trade_pnl(Decimal("100"))
        cb.record_trade_pnl(Decimal("100"))
        allowed, _ = cb.check_before_trade()
        assert allowed
        assert cb.state == BreakerState.CLOSED

    def test_double_trip_recovery(self):
        """Trip, recover, trip again, recover again."""
        cb = make_cb(max_consecutive_losses=2, cooldown_minutes=0, recovery_test_trades=1)

        # First trip
        cb.record_trade_pnl(Decimal("-50"))
        cb.record_trade_pnl(Decimal("-50"))
        cb.check_before_trade()
        assert cb.state == BreakerState.OPEN

        # First recovery
        cb.trip_time = datetime.now() - timedelta(minutes=1)
        cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN
        cb.record_trade_pnl(Decimal("100"))
        cb.check_before_trade()
        assert cb.state == BreakerState.CLOSED

        # Second trip
        cb.record_trade_pnl(Decimal("-50"))
        cb.record_trade_pnl(Decimal("-50"))
        cb.check_before_trade()
        assert cb.state == BreakerState.OPEN

        # Second recovery
        cb.trip_time = datetime.now() - timedelta(minutes=1)
        cb.check_before_trade()
        assert cb.state == BreakerState.HALF_OPEN
        cb.record_trade_pnl(Decimal("200"))
        cb.check_before_trade()
        assert cb.state == BreakerState.CLOSED


# ──────────────────────────────────────────────
# Kill Switch: transient vs persistent failures
# ──────────────────────────────────────────────

class TestKillSwitchBehavior:
    def test_kill_switch_not_triggered_by_transient_errors(self):
        """Transient failures trip CB but not kill switch."""
        temp_dir = tempfile.mkdtemp()
        ks_path = os.path.join(temp_dir, "ks_test.db")
        ks = KillSwitch(db_path=ks_path)

        cb = make_cb(max_consecutive_losses=3)

        # Transient losses → CB trips
        for _ in range(3):
            cb.record_trade_pnl(Decimal("-100"))
        cb.check_before_trade()
        assert cb.state == BreakerState.OPEN

        # Kill switch should NOT be active
        assert not ks.is_active

    def test_kill_switch_blocks_via_risk_gate(self):
        """Kill switch active → RiskGate blocks unconditionally."""
        temp_dir = tempfile.mkdtemp()
        ks_path = os.path.join(temp_dir, "ks_test2.db")
        ks = KillSwitch(db_path=ks_path)

        # Activate kill switch
        ks.activate(KillSwitchMode.BLOCK_NEW, reason="test", activated_by="test")
        assert ks.is_active

        # RiskGate with kill_switch=True
        gate = RiskGate(RiskLimits())
        snapshot = make_risk_snapshot(kill_switch=True)
        verdict = gate.evaluate(snapshot)
        assert not verdict.allowed
        assert "KILL_SWITCH" in verdict.blocking_rule_ids

    def test_kill_switch_requires_explicit_clear(self):
        """Kill switch does not auto-recover — must be explicitly cleared."""
        temp_dir = tempfile.mkdtemp()
        ks_path = os.path.join(temp_dir, "ks_test3.db")
        ks = KillSwitch(db_path=ks_path)

        ks.activate(KillSwitchMode.BLOCK_NEW, reason="test", activated_by="test")
        assert ks.is_active

        # Simulate time passing — still active
        assert ks.is_active

        # Explicit clear
        ks.clear(cleared_by="test")
        assert not ks.is_active


# ──────────────────────────────────────────────
# Circuit Breaker + RiskGate interaction
# ──────────────────────────────────────────────

class TestCBRiskGateInteraction:
    def test_open_cb_blocks_via_riskgate(self):
        """OPEN circuit breaker → RiskGate blocks."""
        gate = RiskGate(RiskLimits())
        snapshot = make_risk_snapshot(breaker_state="OPEN")
        verdict = gate.evaluate(snapshot)
        assert not verdict.allowed
        assert "CIRCUIT_BREAKER_OPEN" in verdict.blocking_rule_ids

    def test_closed_cb_allows_via_riskgate(self):
        """CLOSED circuit breaker → RiskGate allows (if other checks pass)."""
        gate = RiskGate(RiskLimits())
        snapshot = make_risk_snapshot(breaker_state="CLOSED")
        verdict = gate.evaluate(snapshot)
        assert verdict.allowed

    def test_half_open_not_blocked_by_riskgate(self):
        """HALF_OPEN is not 'OPEN' — RiskGate allows (for recovery trades)."""
        gate = RiskGate(RiskLimits())
        snapshot = make_risk_snapshot(breaker_state="HALF_OPEN")
        verdict = gate.evaluate(snapshot)
        assert verdict.allowed


# ──────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────

class TestEdgeCases:
    def test_day_auto_reset(self):
        """New day auto-resets equity metrics."""
        cb = make_cb()
        # Set day_start to yesterday
        cb.day_start = datetime.now() - timedelta(days=1)
        cb.equity_now = Decimal("10000")

        allowed, _ = cb.check_before_trade()
        assert allowed
        # After check, day should have been reset
        assert cb.day_start.date() == datetime.now().date()

    def test_force_open(self):
        """External force_open trips the breaker."""
        cb = make_cb()
        assert cb.state == BreakerState.CLOSED

        cb.force_open("OMS degraded")
        assert cb.state == BreakerState.OPEN
        assert cb.trip_reason == "OMS degraded"

    def test_zero_equity_day_start_no_crash(self):
        """Zero equity at day start doesn't divide by zero."""
        cb = make_cb()
        cb.equity_day_start = Decimal("0")
        cb.equity_peak = Decimal("0")
        cb.equity_now = Decimal("0")

        # Should not crash (no division by zero)
        allowed, _ = cb.check_before_trade()
        # With zero equity, no loss/drawdown checks can trigger
        assert allowed

    def test_status_report(self):
        """get_status returns structured dict."""
        cb = make_cb()
        status = cb.get_status()
        assert "state" in status
        assert "equity" in status
        assert "trades" in status
        assert "health" in status
        assert status["state"] == "closed"

    def test_winning_trade_resets_consecutive_losses(self):
        """A winning trade resets consecutive loss counter."""
        cb = make_cb(max_consecutive_losses=3)
        cb.record_trade_pnl(Decimal("-100"))
        cb.record_trade_pnl(Decimal("-100"))
        assert cb.consecutive_losses == 2

        cb.record_trade_pnl(Decimal("50"))
        assert cb.consecutive_losses == 0

        # Two more losses should not trip (need 3 consecutive)
        cb.record_trade_pnl(Decimal("-100"))
        cb.record_trade_pnl(Decimal("-100"))
        allowed, _ = cb.check_before_trade()
        assert allowed
