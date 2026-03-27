"""
Tests para RiskGate.evaluate() con el nuevo contrato v3.

Invariantes testadas:
- allowed=False bloquea incondicionalmente
- hard_max_qty es un cap (nunca excede target_qty ni límites de posición/notional)
- CircuitBreaker OPEN → allowed=False vía evaluate(breaker_state="OPEN")
- Fail-closed: equity=0 → blocked
- Daily loss y drawdown → blocked con blocking_rule_ids correctos
- Orders/minute: usa max(snapshot, interno)
- SELL sin posición → blocked (spot-only)
"""
from decimal import Decimal

from src.risk.gate import (
    RiskGate,
    RiskLimits,
    RiskSnapshot,
    RiskDecision,
    RULE_CIRCUIT_BREAKER_OPEN,
    RULE_DAILY_LOSS_LIMIT,
    RULE_MAX_DRAWDOWN,
    RULE_MAX_ORDERS_PER_MINUTE,
    RULE_SELL_NO_POSITION,
    RULE_EQUITY_ZERO_OR_MISSING,
)


def make_limits(**kwargs) -> RiskLimits:
    defaults = dict(
        max_position_pct=Decimal("0.20"),
        max_notional_per_symbol=Decimal("10000"),
        max_orders_per_minute=10,
        max_daily_loss_pct=Decimal("0.05"),
        max_drawdown_pct=Decimal("0.15"),
    )
    defaults.update(kwargs)
    return RiskLimits(**defaults)


def make_snapshot(**kwargs) -> RiskSnapshot:
    defaults = dict(
        equity=Decimal("10000"),
        position_qty=Decimal("0"),
        day_pnl_pct=Decimal("0"),
        drawdown_pct=Decimal("0"),
        orders_last_minute=0,
    )
    defaults.update(kwargs)
    return RiskSnapshot(**defaults)


class TestRiskGateBuy:
    """Tests para órdenes BUY."""

    def setup_method(self):
        self.gate = RiskGate(make_limits())

    def test_buy_allowed_with_room_for_position(self):
        """BUY con posición < max_position_pct debe ser permitido."""
        snapshot = make_snapshot(equity=Decimal("10000"), position_qty=Decimal("0"))
        # target_qty = 0.01 BTC a 50k = $500 = 5% equity (dentro del 20% limit)
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert decision.allowed, f"BUY debería ser permitido: {decision.reason}"
        assert decision.hard_max_qty > Decimal("0")
        assert decision.hard_max_qty <= Decimal("0.01")  # nunca excede target

    def test_buy_hard_max_qty_never_exceeds_target(self):
        """hard_max_qty nunca excede target_qty."""
        snapshot = make_snapshot(equity=Decimal("100000"), position_qty=Decimal("0"))
        target = Decimal("0.05")
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=target,
            entry_ref=Decimal("50000"),
        )
        if decision.allowed:
            assert decision.hard_max_qty <= target

    def test_buy_blocked_when_position_at_max(self):
        """BUY bloqueado cuando posición ya está al máximo permitido."""
        # equity=10000, max_position_pct=0.20, entry=50000 → max_qty = 0.04 BTC
        # position_qty ya en 0.04 → sin espacio disponible
        snapshot = make_snapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.04"),  # exactamente al límite
        )
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed, "BUY debe ser bloqueado en posición máxima"

    def test_buy_circuit_breaker_open_blocks(self):
        """Circuit breaker OPEN debe bloquear BUY."""
        snapshot = make_snapshot()
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
            breaker_state="OPEN",
        )
        assert not decision.allowed
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids

    def test_buy_equity_zero_blocks(self):
        """equity=0 → blocked (fail-closed)."""
        snapshot = make_snapshot(equity=Decimal("0"))
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed
        assert RULE_EQUITY_ZERO_OR_MISSING in decision.blocking_rule_ids


class TestRiskGateDailyLoss:
    """Tests para daily loss limit."""

    def setup_method(self):
        self.gate = RiskGate(make_limits(max_daily_loss_pct=Decimal("0.05")))

    def test_daily_loss_exceeded_blocks(self):
        """day_pnl_pct <= -5% debe bloquear."""
        snapshot = make_snapshot(day_pnl_pct=Decimal("-0.06"))
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed
        assert RULE_DAILY_LOSS_LIMIT in decision.blocking_rule_ids
        assert "Daily loss limit" in decision.reason

    def test_daily_loss_within_limit_allows(self):
        """day_pnl_pct = -3% (dentro del 5% límite) → permitido."""
        snapshot = make_snapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("-0.03"),
        )
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert decision.allowed, f"Orden debería ser permitida: {decision.reason}"


class TestRiskGateDrawdown:
    """Tests para drawdown limit."""

    def setup_method(self):
        self.gate = RiskGate(make_limits(max_drawdown_pct=Decimal("0.15")))

    def test_drawdown_exceeded_blocks(self):
        """drawdown_pct >= 15% → blocked."""
        snapshot = make_snapshot(drawdown_pct=Decimal("0.16"))
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed
        assert RULE_MAX_DRAWDOWN in decision.blocking_rule_ids
        assert "drawdown" in decision.reason.lower()


class TestRiskGateOrdersPerMinute:
    """Tests para max orders per minute."""

    def setup_method(self):
        self.gate = RiskGate(make_limits(max_orders_per_minute=3))

    def test_snapshot_orders_at_limit_blocks(self):
        """
        Si snapshot.orders_last_minute ya está al límite → blocked.
        Verifica que el gate usa max(snapshot, interno), no solo el interno.
        """
        snapshot = make_snapshot(orders_last_minute=3)
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed
        assert RULE_MAX_ORDERS_PER_MINUTE in decision.blocking_rule_ids

    def test_internal_counter_increments_on_approval(self):
        """El contador interno incrementa con cada orden aprobada."""
        snapshot = make_snapshot(orders_last_minute=0)
        entry = Decimal("50000")
        tq = Decimal("0.001")

        # 3 órdenes aprobadas
        for i in range(3):
            d = self.gate.evaluate(
                symbol="BTC-USD", side="BUY",
                snapshot=snapshot, target_qty=tq, entry_ref=entry,
            )
            assert d.allowed, f"Orden {i+1} debería aprobarse: {d.reason}"

        # 4ta debe ser bloqueada
        d4 = self.gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=snapshot, target_qty=tq, entry_ref=entry,
        )
        assert not d4.allowed
        assert RULE_MAX_ORDERS_PER_MINUTE in d4.blocking_rule_ids


class TestRiskGateSell:
    """Tests para órdenes SELL."""

    def setup_method(self):
        self.gate = RiskGate(make_limits())

    def test_sell_without_position_blocked(self):
        """SELL sin posición → blocked (spot-only invariant)."""
        snapshot = make_snapshot(position_qty=Decimal("0"))
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not decision.allowed
        assert RULE_SELL_NO_POSITION in decision.blocking_rule_ids

    def test_sell_reduction_allowed(self):
        """SELL con posición larga → allowed (reduce_only=True)."""
        snapshot = make_snapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.1"),
        )
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            target_qty=Decimal("0.05"),
            entry_ref=Decimal("50000"),
        )
        assert decision.allowed, f"SELL reductor debería ser permitido: {decision.reason}"
        assert decision.reduce_only is True
        assert decision.hard_max_qty > Decimal("0")

    def test_sell_hard_max_qty_capped_by_position(self):
        """hard_max_qty no puede exceder position_qty (spot-only)."""
        snapshot = make_snapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.05"),
        )
        # target_qty > position_qty
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            target_qty=Decimal("1.0"),  # mucho más que la posición
            entry_ref=Decimal("50000"),
        )
        if decision.allowed:
            assert decision.hard_max_qty <= Decimal("0.05"), (
                f"hard_max_qty={decision.hard_max_qty} no debe exceder position_qty=0.05"
            )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
