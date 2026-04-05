"""
Tests dirigidos: cobertura de paths no cubiertos en gate.py.

Cubre:
- pre_order_check(): todos los paths de validación
- edge case: hard_max_qty = 0 después de cap por max_notional_per_symbol
"""

from decimal import Decimal

from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot


def make_gate(max_notional: str = "10000") -> RiskGate:
    limits = RiskLimits(
        max_position_pct=Decimal("0.50"),
        max_notional_per_symbol=Decimal(max_notional),
        max_orders_per_minute=100,
        max_daily_loss_pct=Decimal("0.10"),
        max_drawdown_pct=Decimal("0.20"),
    )
    return RiskGate(limits=limits)


# ──────────────────────────────────────────────
# pre_order_check — paths de rechazo
# ──────────────────────────────────────────────


class TestPreOrderCheck:

    def test_equity_zero_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("0"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="buy",
        )
        assert ok is False
        assert "equity" in reason

    def test_amount_zero_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0"),
            order_type="market",
            side="buy",
        )
        assert ok is False
        assert "amount" in reason

    def test_price_zero_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("0"),
            amount=Decimal("0.1"),
            order_type="market",
            side="buy",
        )
        assert ok is False
        assert "price" in reason

    def test_bad_order_type_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="stop",
            side="buy",
        )
        assert ok is False
        assert "order_type" in reason

    def test_bad_side_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="long",
        )
        assert ok is False
        assert "side" in reason

    def test_bad_position_side_rejected(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="buy",
            position_side="FLAT",
        )
        assert ok is False
        assert "position_side" in reason

    def test_reduce_only_long_requires_sell(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="buy",
            position_side="LONG",
            reduce_only=True,
        )
        assert ok is False
        assert "reduce_only" in reason

    def test_reduce_only_short_requires_buy(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="sell",
            position_side="SHORT",
            reduce_only=True,
        )
        assert ok is False
        assert "reduce_only" in reason

    def test_open_long_requires_buy(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="sell",
            position_side="LONG",
            reduce_only=False,
        )
        assert ok is False
        assert "LONG" in reason

    def test_open_short_requires_sell(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            order_type="market",
            side="buy",
            position_side="SHORT",
            reduce_only=False,
        )
        assert ok is False
        assert "SHORT" in reason

    def test_notional_exceeded_rejected(self):
        gate = make_gate(max_notional="100")
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("1.0"),
            order_type="market",
            side="buy",
            position_side="LONG",
        )
        assert ok is False
        assert "notional" in reason.lower() or "max_notional" in reason

    def test_valid_market_buy_approved(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            order_type="market",
            side="buy",
            position_side="LONG",
        )
        assert ok is True
        assert reason == "ok"

    def test_valid_limit_sell_approved(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            order_type="limit",
            side="sell",
            position_side="LONG",
            reduce_only=True,
        )
        assert ok is True
        assert reason == "ok"

    def test_valid_reduce_only_long_sell(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            order_type="market",
            side="sell",
            position_side="LONG",
            reduce_only=True,
        )
        assert ok is True

    def test_valid_reduce_only_short_buy(self):
        gate = make_gate()
        ok, reason = gate.pre_order_check(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            order_type="market",
            side="buy",
            position_side="SHORT",
            reduce_only=True,
        )
        assert ok is True


# ──────────────────────────────────────────────
# edge case: hard_max_qty = 0 después de notional cap
# ──────────────────────────────────────────────


class TestHardMaxQtyZeroBlocked:

    def test_position_at_max_buy_more_blocked(self):
        """
        Posición ya en max_position_pct → BUY adicional tiene available_qty=0
        → bloqueado con RULE_TARGET_QTY_ZERO.
        """
        from src.risk.gate import RULE_TARGET_QTY_ZERO

        limits = RiskLimits(
            max_position_pct=Decimal("0.10"),
            max_notional_per_symbol=Decimal("100000"),
            max_orders_per_minute=100,
            max_daily_loss_pct=Decimal("0.99"),
            max_drawdown_pct=Decimal("0.99"),
        )
        gate = RiskGate(limits=limits)
        snap = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.02"),  # ya en el máximo
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol="BTC-USD",
            side="BUY",
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        decision = gate.evaluate(snap)
        assert decision.allowed is False
        assert RULE_TARGET_QTY_ZERO in decision.blocking_rule_ids
