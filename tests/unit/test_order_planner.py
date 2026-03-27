"""
Tests unitarios: OrderPlanner.

Invariantes testeadas:
- risk.allowed=False → OrderNotAllowedError (no OrderIntent creado)
- final_qty = min(sizing.target_qty, risk.hard_max_qty)
- cap por notional: final_qty * price <= hard_max_notional
- final_qty < min_qty → viable=False
- final_qty >= min_qty → viable=True
- client_order_id es determinista dado signal_id + symbol
- mismo signal_id + symbol → mismo client_order_id (idempotencia)
- signal_id distinto → client_order_id distinto
- symbol distinto → client_order_id distinto
- OrderIntent es inmutable (frozen)
- reduce_only proviene de risk.reduce_only
- planner_version es constante y no vacío
- step_size aplicado en final_qty
"""
import hashlib
from decimal import Decimal

import pytest

from src.execution.order_planner import (
    OrderIntent,
    OrderNotAllowedError,
    OrderPlanner,
    RiskDecisionInput,
    _make_client_order_id,
)
from src.risk.position_sizer import SizingDecision, SymbolConstraints


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("10000"),
    min_notional=Decimal("1"),
)


def make_sizing(
    target_qty: str = "0.1",
    entry_price: str = "50000",
) -> SizingDecision:
    qty = Decimal(target_qty)
    price = Decimal(entry_price)
    return SizingDecision(
        target_qty=qty,
        target_notional=qty * price,
        risk_budget_used=Decimal("0.01"),
        rationale="test sizing",
    )


def make_risk(
    allowed: bool = True,
    hard_max_qty: str = "1.0",
    hard_max_notional: str = "100000",
    reduce_only: bool = False,
    reason: str = "Risk checks passed",
) -> RiskDecisionInput:
    return RiskDecisionInput(
        allowed=allowed,
        hard_max_qty=Decimal(hard_max_qty),
        hard_max_notional=Decimal(hard_max_notional),
        reduce_only=reduce_only,
        reason=reason,
    )


def plan(
    signal_id: str = "sig-001",
    strategy_id: str = "sma-001",
    symbol: str = "BTC-USD",
    side: str = "BUY",
    sizing: SizingDecision | None = None,
    risk: RiskDecisionInput | None = None,
    constraints: SymbolConstraints = BTC_CONSTRAINTS,
    order_type: str = "MARKET",
    price: str | None = None,
) -> OrderIntent:
    planner = OrderPlanner()
    return planner.plan(
        signal_id=signal_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        sizing=sizing or make_sizing(),
        risk=risk or make_risk(),
        constraints=constraints,
        order_type=order_type,
        price=Decimal(price) if price else None,
    )


# ──────────────────────────────────────────────
# Bloqueo cuando risk.allowed=False
# ──────────────────────────────────────────────

class TestRiskAllowedFalse:

    def test_blocked_risk_raises_order_not_allowed(self):
        """risk.allowed=False → OrderNotAllowedError."""
        with pytest.raises(OrderNotAllowedError):
            plan(risk=make_risk(allowed=False, reason="equity=0"))

    def test_blocked_risk_error_message_contains_reason(self):
        """Error message contiene la razón del bloqueo."""
        with pytest.raises(OrderNotAllowedError, match="equity=0"):
            plan(risk=make_risk(allowed=False, reason="equity=0"))

    def test_no_order_intent_when_blocked(self):
        """Ningún OrderIntent producido cuando risk bloquea."""
        result = None
        with pytest.raises(OrderNotAllowedError):
            result = plan(risk=make_risk(allowed=False))
        assert result is None


# ──────────────────────────────────────────────
# final_qty = min(target_qty, hard_max_qty)
# ──────────────────────────────────────────────

class TestFinalQtyCalculation:

    def test_final_qty_min_of_target_and_hard_max(self):
        """final_qty = min(sizing.target_qty, risk.hard_max_qty)."""
        intent = plan(
            sizing=make_sizing(target_qty="0.5"),
            risk=make_risk(hard_max_qty="0.3"),
        )
        assert intent.final_qty == Decimal("0.3")

    def test_target_qty_smaller_than_hard_max(self):
        """Cuando target < hard_max, final_qty = target_qty."""
        intent = plan(
            sizing=make_sizing(target_qty="0.1"),
            risk=make_risk(hard_max_qty="1.0"),
        )
        assert intent.final_qty == Decimal("0.1")

    def test_final_qty_never_exceeds_hard_max_qty(self):
        """final_qty nunca excede hard_max_qty."""
        intent = plan(
            sizing=make_sizing(target_qty="10.0"),
            risk=make_risk(hard_max_qty="0.5"),
        )
        assert intent.final_qty <= Decimal("0.5")

    def test_final_qty_capped_by_notional_with_limit_price(self):
        """final_qty * price <= hard_max_notional cuando hay price."""
        intent = plan(
            sizing=make_sizing(target_qty="1.0", entry_price="50000"),
            risk=make_risk(hard_max_qty="1.0", hard_max_notional="1000"),
            order_type="LIMIT",
            price="50000",
        )
        actual_notional = intent.final_qty * Decimal("50000")
        assert actual_notional <= Decimal("1000")

    def test_step_size_applied_to_final_qty(self):
        """final_qty es múltiplo de step_size."""
        constraints = SymbolConstraints(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
            min_notional=Decimal("1"),
        )
        # target=0.1234 → min(0.1234, 1.0) = 0.1234 → floor(0.1234/0.001)*0.001 = 0.123
        intent = plan(
            sizing=make_sizing(target_qty="0.1234"),
            risk=make_risk(hard_max_qty="1.0"),
            constraints=constraints,
        )
        assert intent.final_qty % Decimal("0.001") == Decimal("0")


# ──────────────────────────────────────────────
# Viabilidad
# ──────────────────────────────────────────────

class TestViability:

    def test_viable_true_when_final_qty_ge_min_qty(self):
        """viable=True cuando final_qty >= min_qty."""
        intent = plan(
            sizing=make_sizing(target_qty="0.1"),
            risk=make_risk(hard_max_qty="1.0"),
        )
        assert intent.viable is True

    def test_viable_false_when_final_qty_lt_min_qty(self):
        """viable=False cuando final_qty < min_qty."""
        high_min = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("10"),  # mínimo muy alto
            max_qty=Decimal("10000"),
            min_notional=Decimal("1"),
        )
        intent = plan(
            sizing=make_sizing(target_qty="0.001"),   # muy pequeño
            risk=make_risk(hard_max_qty="0.001"),
            constraints=high_min,
        )
        assert intent.viable is False

    def test_viable_false_does_not_raise(self):
        """viable=False no lanza excepción — caller decide si enviar."""
        high_min = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("100"),
            max_qty=Decimal("10000"),
            min_notional=Decimal("1"),
        )
        # No debe lanzar — retorna intent con viable=False
        intent = plan(
            sizing=make_sizing(target_qty="0.001"),
            risk=make_risk(hard_max_qty="0.001"),
            constraints=high_min,
        )
        assert isinstance(intent, OrderIntent)


# ──────────────────────────────────────────────
# client_order_id determinismo
# ──────────────────────────────────────────────

class TestClientOrderIdDeterminism:

    def test_same_signal_symbol_same_client_order_id(self):
        """Mismo signal_id + symbol → mismo client_order_id."""
        i1 = plan(signal_id="sig-abc", symbol="BTC-USD")
        i2 = plan(signal_id="sig-abc", symbol="BTC-USD")
        assert i1.client_order_id == i2.client_order_id

    def test_different_signal_different_client_order_id(self):
        """signal_id distinto → client_order_id distinto."""
        i1 = plan(signal_id="sig-001")
        i2 = plan(signal_id="sig-002")
        assert i1.client_order_id != i2.client_order_id

    def test_different_symbol_different_client_order_id(self):
        """symbol distinto → client_order_id distinto (aunque misma señal)."""
        i1 = plan(signal_id="sig-001", symbol="BTC-USD")
        i2 = plan(signal_id="sig-001", symbol="ETH-USD")
        assert i1.client_order_id != i2.client_order_id

    def test_client_order_id_is_32_chars(self):
        """client_order_id tiene 32 caracteres (sha256[:32])."""
        intent = plan()
        assert len(intent.client_order_id) == 32

    def test_make_client_order_id_matches_sha256(self):
        """_make_client_order_id produce sha256 del raw string."""
        raw = "sig-test:BTC-USD"
        expected = hashlib.sha256(raw.encode()).hexdigest()[:32]
        assert _make_client_order_id("sig-test", "BTC-USD") == expected


# ──────────────────────────────────────────────
# Campos del OrderIntent
# ──────────────────────────────────────────────

class TestOrderIntentFields:

    def test_reduce_only_from_risk(self):
        """reduce_only proviene de risk.reduce_only."""
        intent_buy = plan(risk=make_risk(reduce_only=False))
        intent_sell = plan(risk=make_risk(reduce_only=True))
        assert intent_buy.reduce_only is False
        assert intent_sell.reduce_only is True

    def test_planner_version_not_empty(self):
        """planner_version es no vacío."""
        intent = plan()
        assert intent.planner_version
        assert len(intent.planner_version) > 0

    def test_signal_id_and_strategy_id_preserved(self):
        """signal_id y strategy_id se preservan en el intent."""
        intent = plan(signal_id="my-signal", strategy_id="my-strategy")
        assert intent.signal_id == "my-signal"
        assert intent.strategy_id == "my-strategy"

    def test_side_preserved(self):
        """side se preserva en el intent."""
        buy_intent = plan(side="BUY")
        sell_intent = plan(
            side="SELL",
            sizing=make_sizing(target_qty="0.1"),
            risk=make_risk(hard_max_qty="0.5", reduce_only=True),
        )
        assert buy_intent.side == "BUY"
        assert sell_intent.side == "SELL"

    def test_limit_order_preserves_price(self):
        """LIMIT order → price preservado en intent."""
        intent = plan(order_type="LIMIT", price="49000")
        assert intent.price == Decimal("49000")
        assert intent.order_type == "LIMIT"

    def test_market_order_price_none(self):
        """MARKET order → price=None."""
        intent = plan(order_type="MARKET", price=None)
        assert intent.price is None
        assert intent.order_type == "MARKET"

    def test_order_intent_is_immutable(self):
        """OrderIntent es frozen — no se puede modificar."""
        intent = plan()
        with pytest.raises((AttributeError, TypeError)):
            intent.final_qty = Decimal("999")  # type: ignore[misc]
