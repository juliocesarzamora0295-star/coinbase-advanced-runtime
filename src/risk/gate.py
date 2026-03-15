"""
Risk Gate - Pre-trade risk checks.

Valida órdenes antes de enviarlas al exchange.
"""
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("RiskGate")


@dataclass
class RiskDecision:
    """Decisión del risk gate."""
    allow: bool
    qty: Decimal
    reason: str


@dataclass
class RiskLimits:
    """Límites de riesgo."""
    max_position_pct: Decimal = Decimal("0.20")  # 20% del equity
    max_notional_per_symbol: Decimal = Decimal("10000")  # $10k
    max_orders_per_minute: int = 10
    max_daily_loss_pct: Decimal = Decimal("0.05")  # 5%
    max_drawdown_pct: Decimal = Decimal("0.15")  # 15%


@dataclass
class RiskSnapshot:
    """Snapshot de riesgo actual."""
    equity: Decimal
    position_qty: Decimal
    day_pnl_pct: Decimal  # PnL diario como porcentaje del equity
    drawdown_pct: Decimal  # Drawdown actual como porcentaje
    orders_last_minute: int = 0


class RiskGate:
    """
    Pre-trade risk gate.
    
    Valida:
    - Sizing por equity/stop/costos
    - Max position
    - Max notional por símbolo
    - Max orders/minute
    - Daily loss
    - Drawdown
    """
    
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self._orders_this_minute = 0
        self._last_order_time = 0.0
    
    def evaluate(
        self,
        symbol: str,
        side: str,
        snapshot: RiskSnapshot,
        entry_ref: Decimal,
        stop_ref: Optional[Decimal],
        cost_estimate: Decimal,  # fees + slippage estimate
    ) -> RiskDecision:
        """
        Evaluar si una orden puede ser ejecutada.
        
        Args:
            symbol: Símbolo a operar
            side: BUY o SELL
            snapshot: Snapshot de riesgo actual
            entry_ref: Precio de entrada de referencia
            stop_ref: Precio de stop (para sizing)
            cost_estimate: Costo estimado (fees + slippage)
        
        Returns:
            RiskDecision con allow/reject y razón
        """
        equity = snapshot.equity
        position_qty = snapshot.position_qty
        
        # Check 1: Equity suficiente
        if equity <= 0:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason="Insufficient equity",
            )
        
        # Check 2: Daily loss limit
        if snapshot.day_pnl_pct <= -self.limits.max_daily_loss_pct:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason=f"Daily loss limit reached: {snapshot.day_pnl_pct:.2%}",
            )
        
        # Check 3: Max drawdown
        if snapshot.drawdown_pct >= self.limits.max_drawdown_pct:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason=f"Max drawdown reached: {snapshot.drawdown_pct:.2%}",
            )
        
        # Check 4: Max orders per minute
        # CORREGIDO: Usar snapshot.orders_last_minute + contador interno
        now = time.time()
        if now - self._last_order_time > 60:
            self._orders_this_minute = 0
        
        observed_orders = max(self._orders_this_minute, snapshot.orders_last_minute)
        if observed_orders >= self.limits.max_orders_per_minute:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason=f"Max orders per minute exceeded: {self.limits.max_orders_per_minute}",
            )
        
        # P1 FIX: Calcular sizing óptimo con side-aware position limits
        qty = self._calculate_qty(
            equity=equity,
            position_qty=position_qty,
            side=side,
            entry=entry_ref,
            stop=stop_ref,
            cost_estimate=cost_estimate,
        )
        
        if qty <= 0:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason="Calculated qty is zero or negative",
            )
        
        # Check 5: Max position (ya verificado en _calculate_qty, pero doble check)
        max_position = equity * self.limits.max_position_pct / entry_ref
        signed_qty = qty if side.upper() == "BUY" else -qty
        new_position = position_qty + signed_qty
        
        if abs(new_position) > max_position:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason=f"Max position exceeded: {new_position} > ±{max_position}",
            )
        
        # Check 6: Max notional
        notional = qty * entry_ref
        if notional > self.limits.max_notional_per_symbol:
            return RiskDecision(
                allow=False,
                qty=Decimal("0"),
                reason=f"Max notional exceeded: {notional} > {self.limits.max_notional_per_symbol}",
            )
        
        # Aprobar orden
        self._orders_this_minute += 1
        self._last_order_time = now
        
        return RiskDecision(
            allow=True,
            qty=qty,
            reason="Risk checks passed",
        )
    
    def _calculate_qty(
        self,
        equity: Decimal,
        position_qty: Decimal,
        side: str,
        entry: Decimal,
        stop: Optional[Decimal],
        cost_estimate: Decimal,
    ) -> Decimal:
        """
        Calcular cantidad óptima basada en equity, stop y costos.
        
        P1 FIX: Side-aware position limits.
        - BUY: limitado por max_position - position_qty (si long)
        - SELL: limitado por position_qty (spot-only, solo reduce)
        
        Args:
            equity: Equity disponible
            position_qty: Posición actual
            side: BUY o SELL
            entry: Precio de entrada
            stop: Precio de stop (None para market orders sin stop)
            cost_estimate: Costo estimado (fees + slippage)
        
        Returns:
            Cantidad óptima
        """
        if stop is None or stop <= 0:
            # Sin stop, usar porcentaje fijo del equity
            risk_pct = Decimal("0.01")  # 1% risk
            qty = (equity * risk_pct) / entry
        else:
            # Con stop: sizing basado en riesgo
            risk_pct = Decimal("0.01")  # 1% risk por trade
            risk_amount = equity * risk_pct
            
            stop_distance = abs(entry - stop)
            if stop_distance <= 0:
                return Decimal("0")
            
            qty = risk_amount / stop_distance
        
        # Ajustar por cost_estimate (reducir qty para compensar fees/slippage)
        if cost_estimate > 0:
            # Reducir qty proporcionalmente al costo
            cost_adjustment = max(Decimal("0.5"), Decimal("1") - (cost_estimate / (qty * entry)))
            qty = qty * cost_adjustment
        
        # P1 FIX: Side-aware position limits
        max_qty = (equity * self.limits.max_position_pct) / entry
        
        if side.upper() == "BUY":
            # BUY: limitado por espacio disponible hasta max_position
            # Si ya estamos long, solo podemos comprar hasta max_position - position_qty
            available = max(Decimal("0"), max_qty - max(position_qty, Decimal("0")))
            qty = min(qty, available)
        else:  # SELL
            # P1 FIX: SELL spot-only - solo puedes vender lo que tienes
            # No hay restricción de max_position para SELL (estás reduciendo)
            available = max(Decimal("0"), position_qty)  # Solo puedes vender posición larga existente
            qty = min(qty, available)
        
        return max(qty, Decimal("0"))
    
    def pre_order_check(
        self,
        *,
        equity: Decimal,
        price: Decimal,
        amount: Decimal,
        order_type: str,
        side: str,
        position_side: str = "LONG",
        reduce_only: bool = False,
    ) -> tuple[bool, str]:
        """
        Validación pre-orden detallada.
        
        Adaptado de GuardianBot RiskManager.
        
        Args:
            equity: Equity actual
            price: Precio de la orden
            amount: Cantidad
            order_type: "market" o "limit"
            side: "buy" o "sell"
            position_side: "LONG" o "SHORT"
            reduce_only: Si es orden de reducción
            
        Returns:
            (ok, reason) - reason es "ok" si pasa, o el motivo del rechazo
        """
        # Validaciones básicas
        if equity <= 0:
            return False, "equity<=0"
        if amount <= 0:
            return False, "amount<=0"
        if price <= 0:
            return False, "price<=0"
        if order_type not in ("market", "limit"):
            return False, "bad_order_type"
        if side not in ("buy", "sell"):
            return False, "bad_side"
        if position_side not in ("LONG", "SHORT"):
            return False, "bad_position_side"
        
        # Validación de reduce_only
        if reduce_only:
            if position_side == "LONG" and side != "sell":
                return False, "reduce_only_LONG_requires_sell"
            if position_side == "SHORT" and side != "buy":
                return False, "reduce_only_SHORT_requires_buy"
        else:
            if position_side == "LONG" and side != "buy":
                return False, "open_LONG_requires_buy"
            if position_side == "SHORT" and side != "sell":
                return False, "open_SHORT_requires_sell"
        
        # Validación de notional
        notional = amount * price
        if notional > self.limits.max_notional_per_symbol:
            return False, f"max_notional_exceeded:{notional}>{self.limits.max_notional_per_symbol}"
        
        return True, "ok"
