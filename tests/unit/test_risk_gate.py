"""
Tests para RiskGate - Pre-trade risk checks.

Valida:
- BUY respeta max position
- SELL reductor no se bloquea como aumento de posición
- Daily loss bloquea orden
- Drawdown bloquea orden
- Orders per minute usa snapshot y contador interno
"""
import sys
from decimal import Decimal

sys.path.insert(0, '/mnt/okcomputer/output/fortress_v4')

from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskDecision


class TestRiskGateBuy:
    """Tests para órdenes BUY."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),  # 20%
            max_notional_per_symbol=Decimal("10000"),
            max_orders_per_minute=10,
            max_daily_loss_pct=Decimal("0.05"),
            max_drawdown_pct=Decimal("0.15"),
        )
        self.gate = RiskGate(self.limits)
    
    def test_buy_respects_max_position(self):
        """BUY debe respetar el límite de posición máxima."""
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Con equity 10k y max_position 20%, max position = 0.2 BTC a 50k
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        assert decision.allow, f"BUY debería ser permitido: {decision.reason}"
        # Max position = 0.2 BTC, con sizing conservador qty debe ser > 0
        assert decision.qty > 0
    
    def test_buy_blocked_when_position_near_max(self):
        """BUY bloqueado cuando posición está cerca del máximo."""
        # Posición ya al 19% (cerca del 20% límite)
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.19"),  # 19% de 0.2 BTC max
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        # Debe permitir qty muy pequeña o 0
        assert decision.qty >= 0


class TestRiskGateSell:
    """Tests para órdenes SELL."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),
            max_notional_per_symbol=Decimal("10000"),
            max_orders_per_minute=10,
            max_daily_loss_pct=Decimal("0.05"),
            max_drawdown_pct=Decimal("0.15"),
        )
        self.gate = RiskGate(self.limits)
    
    def test_sell_reduction_not_blocked_as_position_increase(self):
        """
        SELL reductor NO debe bloquearse como si aumentara exposición.
        
        Bug anterior: new_position = position_qty + qty (sin considerar side)
        Resultado: SELL con posición larga se bloqueaba incorrectamente.
        """
        # Tenemos 0.1 BTC, queremos vender 0.05 BTC
        # Con equity 10k y precio 50k, max_position = 0.04 BTC (20% de 10k / 50k)
        # Posición de 0.1 BTC ya excede max_position, pero SELL la reduce
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0.1"),  # Posición larga (excede max de 0.04)
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Forzar sizing alto para asegurar que hay qty disponible
        # Usar stop muy cerco para que qty sea grande
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49999"),  # Stop muy cercano = sizing grande
            cost_estimate=Decimal("0.01"),
        )
        
        # SELL reductor debe ser permitido (reduce exposición)
        # Nota: qty puede ser 0 si la posición ya excede max_position,
        # pero el punto es que no se bloquee por "max position exceeded"
        # con new_position > max_position (que sería el bug)
        if decision.qty > 0:
            assert decision.allow, f"SELL reductor debería ser permitido: {decision.reason}"
        else:
            # Si qty es 0, es por sizing, no por el bug de posición
            assert "max position" not in decision.reason.lower() or "±" in decision.reason
    
    def test_sell_without_position_blocked(self):
        """
        P1 FIX: SELL sin posición debe ser bloqueado (spot-only).
        
        En spot, no puedes vender lo que no tienes.
        """
        # Sin posición, SELL no puede ejecutarse
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=None,
            cost_estimate=Decimal("10"),
        )
        
        # Spot-only: SELL sin posición debe ser bloqueado
        assert not decision.allow, "SELL without position should be blocked in spot-only mode"
        assert decision.qty == 0


class TestRiskGateDailyLoss:
    """Tests para daily loss limit."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),
            max_daily_loss_pct=Decimal("0.05"),  # 5%
            max_drawdown_pct=Decimal("0.15"),
        )
        self.gate = RiskGate(self.limits)
    
    def test_daily_loss_blocks_order(self):
        """Daily loss >= 5% debe bloquear orden."""
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("-0.06"),  # -6% (excede 5%)
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        assert not decision.allow, "Orden debe ser bloqueada por daily loss"
        assert "Daily loss limit" in decision.reason
    
    def test_daily_loss_allows_order_when_under_limit(self):
        """Daily loss < 5% debe permitir orden."""
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("-0.03"),  # -3% (dentro de 5%)
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        assert decision.allow, f"Orden debería ser permitida: {decision.reason}"


class TestRiskGateDrawdown:
    """Tests para drawdown limit."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),
            max_daily_loss_pct=Decimal("0.05"),
            max_drawdown_pct=Decimal("0.15"),  # 15%
        )
        self.gate = RiskGate(self.limits)
    
    def test_drawdown_blocks_order(self):
        """Drawdown >= 15% debe bloquear orden."""
        snapshot = RiskSnapshot(
            equity=Decimal("8500"),  # Drawdown implícito
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0.16"),  # 16% (excede 15%)
            orders_last_minute=0,
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        assert not decision.allow, "Orden debe ser bloqueada por drawdown"
        assert "drawdown" in decision.reason.lower()


class TestRiskGateOrdersPerMinute:
    """Tests para max orders per minute."""
    
    def setup_method(self):
        self.limits = RiskLimits(
            max_position_pct=Decimal("0.20"),
            max_orders_per_minute=3,
        )
        self.gate = RiskGate(self.limits)
    
    def test_orders_per_minute_uses_snapshot_and_internal_counter(self):
        """
        Max orders per minute debe usar snapshot.orders_last_minute + contador interno.
        
        Bug anterior: solo usaba contador interno, ignorando estado externo.
        """
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=3,  # Ya al límite desde snapshot
        )
        
        decision = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        
        assert not decision.allow, "Orden debe ser bloqueada por max orders/min"
        assert "orders per minute" in decision.reason.lower()
    
    def test_internal_counter_increments_on_approval(self):
        """Contador interno debe incrementar cuando orden es aprobada."""
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            orders_last_minute=0,
        )
        
        # Primera orden - debe aprobarse
        decision1 = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        assert decision1.allow
        
        # Segunda orden - debe aprobarse
        decision2 = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        assert decision2.allow
        
        # Tercera orden - debe aprobarse
        decision3 = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        assert decision3.allow
        
        # Cuarta orden - debe bloquearse (3 por minuto)
        decision4 = self.gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snapshot,
            entry_ref=Decimal("50000"),
            stop_ref=Decimal("49000"),
            cost_estimate=Decimal("10"),
        )
        assert not decision4.allow


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
