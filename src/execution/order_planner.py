"""
Contrato de dominio: OrderPlanner + OrderIntent

OrderPlanner combina SizingDecision y RiskVerdict para producir el OrderIntent final.
Es la única entidad que decide final_qty.

Invariantes:
- final_qty = min(sizing.target_qty, risk.hard_max_qty, cap_por_notional)
- Si RiskVerdict.allowed=False → no produce OrderIntent (lanza error)
- Si final_qty < min_qty del símbolo → OrderIntent marcada inviable (viable=False), no se envía
- client_order_id es determinista dado signal_id + symbol (permite idempotencia)
- OrderIntent es inmutable (frozen=True)
- OrderPlanner no llama a RiskGate ni a PositionSizer ni a OMS directamente
- OrderIntent es el contrato canónico para execution + persistence
"""

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Literal, Optional

from src.risk.position_sizer import SizingDecision, SymbolConstraints

logger = logging.getLogger("OrderPlanner")

PLANNER_VERSION = "1.0"


class OrderNotAllowedError(Exception):
    """Raised when RiskVerdict.allowed=False. No OrderIntent debe crearse."""

    pass


@dataclass(frozen=True)
class RiskDecisionInput:
    """
    Subset de RiskVerdict relevante para OrderPlanner.
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
    Intención de orden ejecutable — contrato canónico.

    Producida por OrderPlanner. Inmutable.
    Única fuente de verdad para execution y persistence.

    Trazabilidad: signal_id → client_order_id → exchange_order_id
    """

    # Identidad (determinista)
    client_order_id: str  # sha256(signal_id:symbol)[:32]
    signal_id: str  # referencia al Signal que lo originó
    strategy_id: str  # referencia a la estrategia

    # Orden
    symbol: str
    side: Literal["BUY", "SELL"]
    final_qty: Decimal  # min(target_qty, hard_max_qty, notional_cap)
    order_type: Literal["MARKET", "LIMIT"]
    price: Optional[Decimal]  # None para MARKET
    reduce_only: bool
    post_only: bool  # solo relevante para LIMIT

    # Metadata
    viable: bool  # False si final_qty < min_qty: no enviar
    planner_version: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialización para persistencia."""
        return {
            "client_order_id": self.client_order_id,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "final_qty": str(self.final_qty),
            "order_type": self.order_type,
            "price": str(self.price) if self.price else None,
            "reduce_only": self.reduce_only,
            "post_only": self.post_only,
            "viable": self.viable,
            "planner_version": self.planner_version,
        }


def _make_client_order_id(signal_id: str, symbol: str) -> str:
    """
    Genera client_order_id determinista a partir de signal_id + symbol.
    Mismo signal + symbol → mismo client_order_id → idempotencia garantizada.
    """
    raw = f"{signal_id}:{symbol}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class OrderPlanner:
    """
    Combina SizingDecision + RiskVerdict para producir OrderIntent.

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
        post_only: bool = False,
    ) -> OrderIntent:
        """
        Producir OrderIntent final.

        Returns:
            OrderIntent (viable=False si final_qty < min_qty).

        Raises:
            OrderNotAllowedError: Si risk.allowed=False.
        """
        if not risk.allowed:
            logger.info(
                "[%s] OrderPlanner blocked: %s %s — %s",
                symbol, side, signal_id, risk.reason,
            )
            raise OrderNotAllowedError(
                f"RiskVerdict.allowed=False for {symbol} {side}: {risk.reason}"
            )

        # final_qty = min(target_qty, hard_max_qty)
        final_qty = min(sizing.target_qty, risk.hard_max_qty)

        # Cap adicional por notional: si final_qty * price > hard_max_notional
        if price is not None and price > Decimal("0"):
            qty_by_notional = risk.hard_max_notional / price
        elif sizing.target_qty > Decimal("0") and sizing.target_notional > Decimal("0"):
            implied_price = sizing.target_notional / sizing.target_qty
            qty_by_notional = risk.hard_max_notional / implied_price
        else:
            qty_by_notional = final_qty

        final_qty = min(final_qty, qty_by_notional)

        # Cuantización por step_size
        if constraints.step_size > Decimal("0") and final_qty > Decimal("0"):
            final_qty = (final_qty // constraints.step_size) * constraints.step_size

        # Determinar viabilidad
        viable = final_qty >= constraints.min_qty

        if not viable:
            logger.info(
                "[%s] OrderIntent not viable: final_qty=%s < min_qty=%s",
                symbol, final_qty, constraints.min_qty,
            )
        else:
            logger.debug(
                "[%s] OrderIntent: %s %s qty=%s signal=%s",
                symbol, side, order_type, final_qty, signal_id,
            )

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
            post_only=post_only,
            viable=viable,
            planner_version=PLANNER_VERSION,
        )
