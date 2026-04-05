"""
Tests para SELL reductor en RiskGate — snapshot-based deterministic contract.

Valida:
- SELL desde posición al máximo → allowed (reduce_only=True)
- SELL qty capada por position_qty (spot-only)
- BUY bloqueado cuando posición al máximo
"""

from decimal import Decimal

from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot


class TestRiskGateSellReduction:
    """Tests para SELL reductor."""

    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),
            max_notional_per_symbol=Decimal("10000"),
            max_orders_per_minute=10,
            max_daily_loss_pct=Decimal("0.05"),
            max_drawdown_pct=Decimal("0.15"),
        )
        self.gate = RiskGate(self.limits)

    def test_sell_reduction_from_max_position_is_allowed(self):
        """
        P1 FIX: Un SELL reductor debe ser permitido aunque estés al máximo de posición.

        Caso:
        - equity = 1000, entry = 100
        - max_position_pct = 0.20 → max_qty = 2 BTC
        - position_qty = 2 (al máximo)
        - SELL debe reducir la exposición: allowed=True, reduce_only=True
        """
        snapshot = RiskSnapshot(
            equity=Decimal("1000"),
            position_qty=Decimal("2"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
            symbol="BTC-USD",
            side="SELL",
            target_qty=Decimal("0.5"),
            entry_ref=Decimal("100"),
        )
        decision = self.gate.evaluate(snapshot)
        assert decision.allowed, f"SELL reductor debe ser permitido: {decision.reason}"
        assert decision.hard_max_qty > Decimal(
            "0"
        ), f"hard_max_qty debe ser > 0, got {decision.hard_max_qty}"
        assert decision.reduce_only is True

    def test_sell_cannot_exceed_position(self):
        """
        P1 FIX: hard_max_qty no puede exceder position_qty (spot-only).

        No puedes vender más BTC del que tienes.
        """
        snapshot = RiskSnapshot(
            equity=Decimal("1000"),
            position_qty=Decimal("2"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
            symbol="BTC-USD",
            side="SELL",
            target_qty=Decimal("5"),
            entry_ref=Decimal("100"),
        )
        decision = self.gate.evaluate(snapshot)
        if decision.allowed:
            assert decision.hard_max_qty <= Decimal("2"), (
                f"SELL hard_max_qty ({decision.hard_max_qty}) "
                f"no debe exceder position_qty (2)"
            )

    def test_buy_blocked_when_at_max_position(self):
        """
        BUY debe ser bloqueado cuando ya estás al máximo de posición.

        equity=1000, entry=100, max_position_pct=0.20 → max_qty=2
        position_qty=2 → sin espacio para BUY.
        """
        snapshot = RiskSnapshot(
            equity=Decimal("1000"),
            position_qty=Decimal("2"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
            symbol="BTC-USD",
            side="BUY",
            target_qty=Decimal("0.1"),
            entry_ref=Decimal("100"),
        )
        decision = self.gate.evaluate(snapshot)
        assert not decision.allowed, "BUY debe ser bloqueado cuando estás al máximo"
        assert decision.hard_max_qty == Decimal("0")


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
