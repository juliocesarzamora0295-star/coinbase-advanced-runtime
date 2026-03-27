"""
Contrato de dominio: OrderPlanner + OrderIntent

OrderPlanner combina SizingDecision y RiskDecision para producir el OrderIntent final.
Es la única entidad que decide final_qty.

Invariantes:
- final_qty = min(sizing.target_qty, risk.hard_max_qty, cap_por_notional)
- Si RiskDecision.allowed=False → no produce OrderIntent (lanza error o retorna None)
- Si final_qty < min_qty del símbolo → OrderIntent marcada inviable (viable=False), no se envía
- client_order_id es determinista dado signal_id + symbol (permite idempotencia)
- OrderIntent es inmutable (frozen=True)
- OrderPlanner no llama a RiskGate ni a PositionSizer ni a OMS directamente
"""
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional

from src.risk.position_sizer import SizingDecision, SymbolConstraints


PLANNER_VERSION = "1.0"


class OrderNotAllowedError(Exception):
    """Raised when RiskDecision.allowed=False. No OrderIntent debe crearse."""
    pass


@dataclass(frozen=True)
class RiskDecisionInput:
    """
    Subset de RiskDecision relevante para OrderPlanner.
    Evita acoplamiento directo a src.risk.gate para imports circulares.
    """
    allowed: bool
    hard_max_qty: Decimal
    hard_max_notional: Decimal
    reduce_only: bool
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    """
    Intención de orden ejecutable.

    Producida por OrderPlanner. Inmutable.
    No contiene el objeto Signal completo — solo referencias planas
    para mantener auditabilidad y serialización limpia.
    """

    client_order_id: str                        # determinista dado signal_id + symbol
    signal_id: str                              # referencia al Signal que lo originó
    strategy_id: str                            # referencia a la estrategia
    symbol: str
    side: Literal["BUY", "SELL"]
    final_qty: Decimal                          # min(target_qty, hard_max_qty, notional_cap)
    order_type: Literal["MARKET", "LIMIT"]
    price: Optional[Decimal]                    # None para órdenes MARKET
    reduce_only: bool
    viable: bool                                # False si final_qty < min_qty: no enviar
    planner_version: str


def _make_client_order_id(signal_id: str, symbol: str) -> str:
    """
    Genera client_order_id determinista a partir de signal_id + symbol.
    Mismo signal + symbol → mismo client_order_id → idempotencia garantizada.
    """
    raw = f"{signal_id}:{symbol}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class OrderPlanner:
    """
    Combina SizingDecision + RiskDecision para producir OrderIntent.

    No tiene acceso directo a RiskGate, PositionSizer, ni OMS.
    Recibe los resultados ya calculados y aplica la lógica de planificación.
    """

    def plan(
        self,
        *,
        signal_id: str,
        strategy_id: str,
        symbol: str,
        side: Literal["BUY", "SELL"],
        sizing: SizingDecision,
        risk: RiskDecisionInput,
        constraints: SymbolConstraints,
        order_type: Literal["MARKET", "LIMIT"] = "MARKET",
        price: Optional[Decimal] = None,
    ) -> OrderIntent:
        """
        Producir OrderIntent final.

        Args:
            signal_id: ID del Signal original.
            strategy_id: ID de la estrategia que generó el Signal.
            symbol: Símbolo a operar.
            side: Dirección ("BUY" o "SELL").
            sizing: Resultado de PositionSizer.
            risk: Resultado de RiskGate (como RiskDecisionInput).
            constraints: Constraints del símbolo.
            order_type: Tipo de orden.
            price: Precio límite (solo para LIMIT orders).

        Returns:
            OrderIntent (viable=False si final_qty < min_qty).

        Raises:
            OrderNotAllowedError: Si risk.allowed=False.
        """
        if not risk.allowed:
            raise OrderNotAllowedError(
                f"RiskDecision.allowed=False for {symbol} {side}: {risk.reason}"
            )

        # final_qty = min(target_qty, hard_max_qty)
        final_qty = min(sizing.target_qty, risk.hard_max_qty)

        # Cap adicional por notional: si final_qty * price > hard_max_notional
        if price is not None and price > Decimal("0"):
            qty_by_notional = risk.hard_max_notional / price
        elif sizing.target_qty > Decimal("0") and sizing.target_notional > Decimal("0"):
            # Estimar precio desde sizing
            implied_price = sizing.target_notional / sizing.target_qty
            qty_by_notional = risk.hard_max_notional / implied_price
        else:
            qty_by_notional = final_qty  # sin cap adicional por notional

        final_qty = min(final_qty, qty_by_notional)

        # Cuantización por step_size (no inventar qty)
        if constraints.step_size > Decimal("0") and final_qty > Decimal("0"):
            final_qty = (final_qty // constraints.step_size) * constraints.step_size

        # Determinar viabilidad
        viable = final_qty >= constraints.min_qty

        return OrderIntent(
            client_order_id=_make_client_order_id(signal_id, symbol),
            signal_id=signal_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            final_qty=final_qty,
            order_type=order_type,
            price=price,
            reduce_only=risk.reduce_only,
            viable=viable,
            planner_version=PLANNER_VERSION,
        )
