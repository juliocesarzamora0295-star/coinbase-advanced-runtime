"""
Tests de contrato para OrderIntent y OrderPlanner.

Invariantes testeadas:
- OrderIntent es inmutable (frozen)
- client_order_id es determinista dado signal_id + symbol
- final_qty = min(target_qty, hard_max_qty)
- final_qty respeta step_size (no excede caps silenciosamente)
- Si final_qty < min_qty → OrderIntent.viable=False (no enviar)
- risk.allowed=False → OrderNotAllowedError (no se produce OrderIntent)
- OrderIntent no contiene objeto Signal completo (solo signal_id, strategy_id)
- reduce_only se propaga desde RiskDecision
"""
from decimal import Decimal

import pytest

from src.execution.order_planner import (
    OrderPlanner,
    OrderIntent,
    OrderNotAllowedError,
    RiskDecisionInput,
    _make_client_order_id,
)
from src.risk.position_sizer import SizingDecision, SymbolConstraints


def make_sizing(
    target_qty: Decimal = Decimal("0.05"),
    target_notional: Decimal = Decimal("2500"),
) -> SizingDecision:
    return SizingDecision(
        target_qty=target_qty,
        target_notional=target_notional,
        risk_budget_used=Decimal("0.01"),
        rationale="test",
    )


def make_risk(
    allowed: bool = True,
    hard_max_qty: Decimal = Decimal("0.10"),
    hard_max_notional: Decimal = Decimal("5000"),
    reduce_only: bool = False,
) -> RiskDecisionInput:
    return RiskDecisionInput(
        allowed=allowed,
        hard_max_qty=hard_max_qty,
        hard_max_notional=hard_max_notional,
        reduce_only=reduce_only,
        reason="ok" if allowed else "blocked",
    )


def make_constraints(
    step_size: Decimal = Decimal("0.00001"),
    min_qty: Decimal = Decimal("0.00001"),
    max_qty: Decimal = Decimal("9999999"),
    min_notional: Decimal = Decimal("1"),
) -> SymbolConstraints:
    return SymbolConstraints(
        step_size=step_size,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=min_notional,
    )


class TestOrderIntentStructure:
    """Tests sobre la estructura de OrderIntent."""

    def test_order_intent_has_no_signal_ref_object(self):
        """
        OrderIntent no contiene un objeto Signal completo.
        Solo referencia plana: signal_id, strategy_id.
        """
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001",
            strategy_id="sma_crossover",
            symbol="BTC-USD",
            side="BUY",
            sizing=make_sizing(),
            risk=make_risk(),
            constraints=make_constraints(),
        )
        assert not hasattr(intent, "signal_ref"), (
            "OrderIntent no debe tener signal_ref: acoplamiento frágil"
        )
        assert intent.signal_id == "sig-001"
        assert intent.strategy_id == "sma_crossover"

    def test_order_intent_is_immutable(self):
        """OrderIntent es frozen."""
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001",
            strategy_id="sma",
            symbol="BTC-USD",
            side="BUY",
            sizing=make_sizing(),
            risk=make_risk(),
            constraints=make_constraints(),
        )
        with pytest.raises((AttributeError, TypeError)):
            intent.final_qty = Decimal("999")  # type: ignore[misc]

    def test_order_intent_has_planner_version(self):
        """OrderIntent registra planner_version para auditabilidad."""
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001",
            strategy_id="sma",
            symbol="BTC-USD",
            side="BUY",
            sizing=make_sizing(),
            risk=make_risk(),
            constraints=make_constraints(),
        )
        assert intent.planner_version


class TestOrderPlannerFinalQty:
    """Tests sobre final_qty en OrderPlanner."""

    def test_final_qty_is_min_of_target_and_hard_max(self):
        """final_qty = min(target_qty, hard_max_qty)."""
        planner = OrderPlanner()

        # Caso: target_qty < hard_max_qty → final_qty = target_qty
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(target_qty=Decimal("0.03")),
            risk=make_risk(hard_max_qty=Decimal("0.10")),
            constraints=make_constraints(),
        )
        assert intent.final_qty <= Decimal("0.03")

    def test_final_qty_capped_by_hard_max_qty(self):
        """Si target_qty > hard_max_qty → final_qty ≤ hard_max_qty."""
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(target_qty=Decimal("1.0")),   # grande
            risk=make_risk(hard_max_qty=Decimal("0.05")),    # cap pequeño
            constraints=make_constraints(),
        )
        assert intent.final_qty <= Decimal("0.05"), (
            f"final_qty={intent.final_qty} debe ser ≤ hard_max_qty=0.05"
        )

    def test_final_qty_respects_step_size(self):
        """final_qty está cuantizado por step_size (no inventa qty)."""
        planner = OrderPlanner()
        constraints = make_constraints(step_size=Decimal("0.01"))
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(target_qty=Decimal("0.0567")),
            risk=make_risk(hard_max_qty=Decimal("0.10")),
            constraints=constraints,
        )
        if intent.final_qty > Decimal("0"):
            remainder = intent.final_qty % Decimal("0.01")
            assert remainder == Decimal("0"), (
                f"final_qty={intent.final_qty} no está cuantizado por step_size=0.01"
            )

    def test_viable_false_when_final_qty_below_min_qty(self):
        """Si final_qty < min_qty → OrderIntent.viable=False (no se envía)."""
        planner = OrderPlanner()
        constraints = make_constraints(
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.1"),   # min muy alto
        )
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(target_qty=Decimal("0.001")),  # menor que min_qty
            risk=make_risk(hard_max_qty=Decimal("0.001")),
            constraints=constraints,
        )
        assert intent.viable is False, (
            f"final_qty={intent.final_qty} < min_qty=0.1 → viable debe ser False"
        )

    def test_viable_true_when_final_qty_meets_min_qty(self):
        """Si final_qty >= min_qty → OrderIntent.viable=True."""
        planner = OrderPlanner()
        constraints = make_constraints(min_qty=Decimal("0.001"))
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(target_qty=Decimal("0.05")),
            risk=make_risk(hard_max_qty=Decimal("0.10")),
            constraints=constraints,
        )
        assert intent.viable is True


class TestOrderPlannerBlocking:
    """Tests de bloqueo cuando risk.allowed=False."""

    def test_risk_not_allowed_raises_order_not_allowed_error(self):
        """risk.allowed=False → OrderNotAllowedError. No se produce OrderIntent."""
        planner = OrderPlanner()
        with pytest.raises(OrderNotAllowedError):
            planner.plan(
                signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
                sizing=make_sizing(),
                risk=make_risk(allowed=False, hard_max_qty=Decimal("0")),
                constraints=make_constraints(),
            )

    def test_no_order_intent_created_when_risk_blocked(self):
        """Verificación explícita: si risk bloquea, el plan() no retorna nada."""
        planner = OrderPlanner()
        result = None
        try:
            result = planner.plan(
                signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
                sizing=make_sizing(),
                risk=make_risk(allowed=False, hard_max_qty=Decimal("0")),
                constraints=make_constraints(),
            )
        except OrderNotAllowedError:
            pass
        assert result is None, "No debe haber OrderIntent si risk.allowed=False"


class TestClientOrderIdDeterminism:
    """Tests para determinismo de client_order_id."""

    def test_same_signal_id_and_symbol_produce_same_client_order_id(self):
        """
        Mismo signal_id + symbol → mismo client_order_id.
        Garantiza idempotencia de ejecución.
        """
        coid1 = _make_client_order_id("sig-abc", "BTC-USD")
        coid2 = _make_client_order_id("sig-abc", "BTC-USD")
        assert coid1 == coid2

    def test_different_signal_ids_produce_different_client_order_ids(self):
        """Distintos signal_ids → distintos client_order_ids."""
        coid1 = _make_client_order_id("sig-001", "BTC-USD")
        coid2 = _make_client_order_id("sig-002", "BTC-USD")
        assert coid1 != coid2

    def test_different_symbols_produce_different_client_order_ids(self):
        """Mismo signal_id pero distinto symbol → distinto client_order_id."""
        coid1 = _make_client_order_id("sig-001", "BTC-USD")
        coid2 = _make_client_order_id("sig-001", "ETH-USD")
        assert coid1 != coid2

    def test_client_order_id_is_string_of_reasonable_length(self):
        """client_order_id es string de longitud razonable para el exchange."""
        coid = _make_client_order_id("sig-001", "BTC-USD")
        assert isinstance(coid, str)
        assert 8 <= len(coid) <= 64  # exchange IDs suelen tener 8-64 chars


class TestReduceOnlyPropagation:
    """Tests de propagación de reduce_only desde RiskDecision."""

    def test_reduce_only_true_propagates_to_intent(self):
        """reduce_only=True del RiskDecision se propaga a OrderIntent."""
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="SELL",
            sizing=make_sizing(),
            risk=make_risk(reduce_only=True),
            constraints=make_constraints(),
        )
        assert intent.reduce_only is True

    def test_reduce_only_false_propagates_to_intent(self):
        """reduce_only=False del RiskDecision se propaga a OrderIntent."""
        planner = OrderPlanner()
        intent = planner.plan(
            signal_id="sig-001", strategy_id="sma", symbol="BTC-USD", side="BUY",
            sizing=make_sizing(),
            risk=make_risk(reduce_only=False),
            constraints=make_constraints(),
        )
        assert intent.reduce_only is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
