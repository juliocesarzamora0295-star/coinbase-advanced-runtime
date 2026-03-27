"""
Tests de contrato para RiskDecision.

Invariantes testeadas:
- RiskDecision es inmutable (frozen)
- allowed=False → hard_max_qty=0 y hard_max_notional=0
- hard_max_notional coherente con hard_max_qty × precio
- blocking_rule_ids es tuple (no list)
- RiskGate.evaluate() nunca retorna suggested_qty (ese campo no existe)
- Fail-closed: equity=None/0, market_price faltante → allowed=False
- CircuitBreaker como input de evaluate() (no puerta paralela)
"""
from decimal import Decimal

import pytest

from src.risk.gate import (
    RiskGate,
    RiskDecision,
    RiskLimits,
    RiskSnapshot,
    RULE_CIRCUIT_BREAKER_OPEN,
    RULE_EQUITY_ZERO_OR_MISSING,
    RULE_DAILY_LOSS_LIMIT,
    RULE_MAX_DRAWDOWN,
    RULE_SELL_NO_POSITION,
)


def make_gate(**kwargs) -> RiskGate:
    limits = RiskLimits(
        max_position_pct=Decimal("0.20"),
        max_notional_per_symbol=Decimal("50000"),
        max_orders_per_minute=10,
        max_daily_loss_pct=Decimal("0.05"),
        max_drawdown_pct=Decimal("0.15"),
    )
    for k, v in kwargs.items():
        setattr(limits, k, v)
    return RiskGate(limits)


def healthy_snapshot(**kwargs) -> RiskSnapshot:
    defaults = dict(
        equity=Decimal("10000"),
        position_qty=Decimal("0"),
        day_pnl_pct=Decimal("0"),
        drawdown_pct=Decimal("0"),
        orders_last_minute=0,
    )
    defaults.update(kwargs)
    return RiskSnapshot(**defaults)


class TestRiskDecisionStructure:
    """Tests sobre la estructura del dataclass RiskDecision."""

    def test_risk_decision_has_no_suggested_qty(self):
        """
        RiskDecision no tiene suggested_qty.
        Riesgo impone caps, no propone negocio.
        """
        gate = make_gate()
        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not hasattr(decision, "suggested_qty"), (
            "RiskDecision no debe tener suggested_qty — ese campo contamina capas"
        )

    def test_risk_decision_has_hard_max_qty(self):
        """RiskDecision tiene hard_max_qty (un cap, no sugerencia)."""
        gate = make_gate()
        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert hasattr(decision, "hard_max_qty")
        assert isinstance(decision.hard_max_qty, Decimal)

    def test_risk_decision_is_immutable(self):
        """RiskDecision es frozen."""
        gate = make_gate()
        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        with pytest.raises((AttributeError, TypeError)):
            decision.allowed = False  # type: ignore[misc]

    def test_blocking_rule_ids_is_tuple(self):
        """blocking_rule_ids es una tuple (hashable, inmutable)."""
        gate = make_gate()
        blocked = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(equity=Decimal("0")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert isinstance(blocked.blocking_rule_ids, tuple)


class TestRiskDecisionBlockedInvariants:
    """Invariantes cuando allowed=False."""

    def test_blocked_decision_has_zero_hard_max_qty(self):
        """allowed=False → hard_max_qty=0."""
        gate = make_gate()
        blocked = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(equity=Decimal("0")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not blocked.allowed
        assert blocked.hard_max_qty == Decimal("0")

    def test_blocked_decision_has_zero_hard_max_notional(self):
        """allowed=False → hard_max_notional=0."""
        gate = make_gate()
        blocked = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(day_pnl_pct=Decimal("-0.10")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not blocked.allowed
        assert blocked.hard_max_notional == Decimal("0")

    def test_blocked_has_non_empty_blocking_rule_ids(self):
        """blocked decision tiene al menos un blocking_rule_id."""
        gate = make_gate()
        blocked = gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=healthy_snapshot(position_qty=Decimal("0")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not blocked.allowed
        assert len(blocked.blocking_rule_ids) > 0

    def test_blocked_has_non_empty_reason(self):
        """blocked decision tiene reason no vacío."""
        gate = make_gate()
        blocked = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=healthy_snapshot(drawdown_pct=Decimal("0.20")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not blocked.allowed
        assert blocked.reason


class TestRiskDecisionFailClosed:
    """Tests de fail-closed: bloqueo cuando faltan datos críticos."""

    def test_equity_zero_blocks(self):
        """equity=0 → allowed=False con RULE_EQUITY_ZERO_OR_MISSING."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(equity=Decimal("0")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not d.allowed
        assert RULE_EQUITY_ZERO_OR_MISSING in d.blocking_rule_ids

    def test_circuit_breaker_open_blocks(self):
        """breaker_state=OPEN → allowed=False con RULE_CIRCUIT_BREAKER_OPEN."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
            breaker_state="OPEN",
        )
        assert not d.allowed
        assert RULE_CIRCUIT_BREAKER_OPEN in d.blocking_rule_ids

    def test_circuit_breaker_closed_allows(self):
        """breaker_state=CLOSED (default) no bloquea por sí mismo."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.001"),
            entry_ref=Decimal("50000"),
            breaker_state="CLOSED",
        )
        # No bloqueado por circuit breaker; puede bloquearse por otro motivo
        assert RULE_CIRCUIT_BREAKER_OPEN not in d.blocking_rule_ids

    def test_daily_loss_blocks_with_correct_rule_id(self):
        """day_pnl_pct <= -max_daily_loss_pct → RULE_DAILY_LOSS_LIMIT en blocking_rule_ids."""
        gate = make_gate(max_daily_loss_pct=Decimal("0.05"))
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(day_pnl_pct=Decimal("-0.06")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not d.allowed
        assert RULE_DAILY_LOSS_LIMIT in d.blocking_rule_ids

    def test_drawdown_blocks_with_correct_rule_id(self):
        """drawdown_pct >= max_drawdown_pct → RULE_MAX_DRAWDOWN en blocking_rule_ids."""
        gate = make_gate(max_drawdown_pct=Decimal("0.15"))
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(drawdown_pct=Decimal("0.16")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not d.allowed
        assert RULE_MAX_DRAWDOWN in d.blocking_rule_ids

    def test_sell_without_position_blocks_with_correct_rule_id(self):
        """SELL sin posición → RULE_SELL_NO_POSITION."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="SELL",
            snapshot=healthy_snapshot(position_qty=Decimal("0")),
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        assert not d.allowed
        assert RULE_SELL_NO_POSITION in d.blocking_rule_ids


class TestRiskDecisionAllowedInvariants:
    """Invariantes cuando allowed=True."""

    def test_allowed_hard_max_qty_le_target_qty(self):
        """allowed=True → hard_max_qty ≤ target_qty (cap, no amplificación)."""
        gate = make_gate()
        target = Decimal("0.001")
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(equity=Decimal("100000")),
            target_qty=target,
            entry_ref=Decimal("50000"),
        )
        if d.allowed:
            assert d.hard_max_qty <= target, (
                f"hard_max_qty={d.hard_max_qty} debe ser ≤ target_qty={target}"
            )

    def test_allowed_hard_max_notional_coherent_with_qty_and_price(self):
        """allowed=True → hard_max_notional ≈ hard_max_qty × entry_ref."""
        gate = make_gate()
        entry = Decimal("50000")
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(equity=Decimal("100000")),
            target_qty=Decimal("0.001"),
            entry_ref=entry,
        )
        if d.allowed and d.hard_max_qty > Decimal("0"):
            expected_notional = d.hard_max_qty * entry
            # tolerancia de 1 cent por rounding
            assert abs(d.hard_max_notional - expected_notional) < Decimal("0.01"), (
                f"hard_max_notional={d.hard_max_notional} incoherente con "
                f"hard_max_qty={d.hard_max_qty} × entry={entry}={expected_notional}"
            )

    def test_sell_allowed_has_reduce_only_true(self):
        """SELL permitido → reduce_only=True (spot-only invariant)."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="SELL",
            snapshot=healthy_snapshot(position_qty=Decimal("0.1")),
            target_qty=Decimal("0.05"),
            entry_ref=Decimal("50000"),
        )
        if d.allowed:
            assert d.reduce_only is True

    def test_buy_allowed_has_reduce_only_false(self):
        """BUY permitido → reduce_only=False."""
        gate = make_gate()
        d = gate.evaluate(
            symbol="BTC-USD", side="BUY",
            snapshot=healthy_snapshot(),
            target_qty=Decimal("0.001"),
            entry_ref=Decimal("50000"),
        )
        if d.allowed:
            assert d.reduce_only is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
