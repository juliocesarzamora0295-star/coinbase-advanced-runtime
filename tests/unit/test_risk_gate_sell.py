"""
Tests para SELL reductor en RiskGate (P1 FIX).

Valida que un SELL reductor no se bloquee cuando estás al máximo de posición.
"""
import sys
from decimal import Decimal

sys.path.insert(0, '/mnt/okcomputer/output/fortress_v4')

from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskDecision


class TestRiskGateSellReduction:
    """Tests para SELL reductor."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),  # 20%
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
        - equity = 1000
        - entry = 100
        - max_position_pct = 0.20 -> max_qty = 2
        - posición actual = +2 (al máximo)
        - quieres vender para reducir
        
        El gate debe permitir la salida, no bloquearla.
        """
        equity = Decimal("1000")
        position_qty = Decimal("2")  # Al máximo (2 BTC a $100 = $200 = 20% de $1000)
        
        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Intentar vender 0.5 BTC para reducir
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            entry_ref=Decimal("100"),
            stop_ref=None,  # Market order
            cost_estimate=Decimal("1"),
        )
        
        # El SELL debe ser permitido (estamos reduciendo, no aumentando)
        assert decision.allow, f"SELL reductor debe ser permitido: {decision.reason}"
        assert decision.qty > 0, f"qty debe ser > 0, got {decision.qty}"
    
    def test_sell_cannot_exceed_position(self):
        """
        P1 FIX: Un SELL no puede vender más de lo que tienes en posición larga.
        
        Spot-only: no puedes vender 3 BTC si solo tienes 2.
        """
        equity = Decimal("1000")
        position_qty = Decimal("2")  # Tienes 2 BTC
        
        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Intentar vender 5 BTC (más de lo que tienes)
        # El sizing calculado será limitado por position_qty
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            entry_ref=Decimal("100"),
            stop_ref=None,
            cost_estimate=Decimal("1"),
        )
        
        # Puede permitirse, pero qty no debe exceder position_qty
        if decision.allow:
            assert decision.qty <= position_qty, \
                f"SELL qty ({decision.qty}) no debe exceder position_qty ({position_qty})"
    
    def test_buy_blocked_when_at_max_position(self):
        """
        Un BUY debe ser bloqueado cuando ya estás al máximo de posición.
        """
        equity = Decimal("1000")
        position_qty = Decimal("2")  # Al máximo
        
        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Intentar comprar más
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("100"),
            stop_ref=Decimal("99"),
            cost_estimate=Decimal("1"),
        )
        
        # El BUY debe ser bloqueado (ya estamos al máximo)
        assert not decision.allow, "BUY debe ser bloqueado cuando estás al máximo"
        assert decision.qty == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
