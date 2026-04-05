"""
Contrato de dominio: PositionSizer + SizingDecision

PositionSizer calcula el target_qty para una señal dada equity y constraints.
Esta qty es una propuesta; RiskGate aplica caps adicionales.
OrderPlanner toma la decisión final: final_qty = min(target_qty, hard_max_qty).

Modos de sizing:
- NOTIONAL: target_qty = (equity * notional_pct) / entry_price
  Invierte un porcentaje del equity como notional. Sin stop_price, el riesgo
  real de la posición no está acotado — la pérdida puede ser mayor que notional_pct.
  Este es el modo por defecto cuando no hay stop_price.

- STOP_BASED: target_qty = (equity * risk_pct) / stop_distance
  Calcula qty tal que si el precio llega al stop, la pérdida = equity * risk_pct.
  Riesgo real acotado. Requiere stop_price explícito.

Invariantes:
- Si equity es None → FailClosedError. No se inventan defaults.
- target_qty nunca excede max_notional ni max_qty del símbolo.
- target_qty respeta step_size (cuantización hacia abajo).
- SizingDecision es inmutable (frozen=True).
- PositionSizer no tiene acceso a RiskGate ni a OMS.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class FailClosedError(Exception):
    """Raised when a required input is missing. Fail-closed invariant."""

    pass


class SizingMode(Enum):
    """Modo de sizing utilizado."""

    NOTIONAL = "NOTIONAL"  # % del equity como notional (sin stop)
    STOP_BASED = "STOP_BASED"  # % del equity como riesgo real (con stop)


@dataclass(frozen=True)
class SymbolConstraints:
    """Constraints del símbolo tal como los reporta el exchange."""

    step_size: Decimal  # mínimo incremento de qty
    min_qty: Decimal  # qty mínima aceptada
    max_qty: Decimal  # qty máxima aceptada (usar Decimal("Infinity") si no hay límite)
    min_notional: Decimal  # notional mínimo aceptado


@dataclass(frozen=True)
class SizingDecision:
    """
    Resultado del cálculo de sizing. Inmutable.

    NOTA: notional_budget_used es la fracción del equity comprometida como notional,
    NO el riesgo real. El riesgo real solo está acotado en modo STOP_BASED.
    """

    target_qty: Decimal  # cantidad propuesta (puede ser 0)
    target_notional: Decimal  # notional de target_qty al precio de entrada
    notional_budget_used: Decimal  # fracción del equity como notional (0.0–1.0)
    sizing_mode: SizingMode  # modo utilizado
    rationale: str  # descripción legible del cálculo

    # Compat alias — consumers existentes que lean risk_budget_used
    @property
    def risk_budget_used(self) -> Decimal:
        return self.notional_budget_used


class PositionSizer:
    """
    Calcula target_qty a partir de equity, sizing parameters y constraints.

    Dos modos:
    - Sin stop_price: notional sizing (notional_pct del equity)
    - Con stop_price: risk sizing (riesgo real acotado por stop distance)

    No tiene acceso a RiskGate. Solo calcula sizing.
    RiskGate aplica sus propios caps sobre este resultado.
    """

    def compute(
        self,
        *,
        symbol: str,
        equity: Optional[Decimal],
        entry_price: Decimal,
        notional_pct: Optional[Decimal] = None,
        risk_per_trade_pct: Optional[Decimal] = None,
        constraints: SymbolConstraints,
        max_notional: Decimal,
        stop_price: Optional[Decimal] = None,
    ) -> SizingDecision:
        """
        Calcular SizingDecision.

        Args:
            symbol: Símbolo a operar.
            equity: Equity disponible. None → FailClosedError.
            entry_price: Precio de entrada de referencia.
            notional_pct: Fracción del equity como notional (ej. 0.01 = 1%).
                Nuevo nombre canónico. Alias de risk_per_trade_pct cuando no hay stop.
            risk_per_trade_pct: DEPRECATED alias de notional_pct. Si ambos se pasan,
                notional_pct tiene prioridad.
            constraints: Constraints del símbolo del exchange.
            max_notional: Notional máximo permitido por config.
            stop_price: Precio de stop. Si presente, sizing es risk-based
                (qty = risk_amount / stop_distance). Si None, sizing es notional-based.

        Returns:
            SizingDecision con target_qty >= 0.

        Raises:
            FailClosedError: Si equity es None.
        """
        # Resolve pct: notional_pct takes priority, fall back to risk_per_trade_pct
        pct = notional_pct if notional_pct is not None else risk_per_trade_pct

        if equity is None:
            raise FailClosedError(
                f"equity is None for {symbol}: cannot size position (fail-closed)"
            )

        if equity <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                notional_budget_used=Decimal("0"),
                sizing_mode=SizingMode.NOTIONAL,
                rationale=f"equity={equity} <= 0",
            )

        if entry_price <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                notional_budget_used=Decimal("0"),
                sizing_mode=SizingMode.NOTIONAL,
                rationale=f"entry_price={entry_price} <= 0",
            )

        if pct is None or pct <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                notional_budget_used=Decimal("0"),
                sizing_mode=SizingMode.NOTIONAL,
                rationale=f"notional_pct={pct} is None or <= 0",
            )

        budget = equity * pct

        # Determine sizing mode and calculate qty
        if stop_price is not None and stop_price > Decimal("0"):
            stop_distance = abs(entry_price - stop_price)
            if stop_distance > Decimal("0"):
                qty = budget / stop_distance
                mode = SizingMode.STOP_BASED
            else:
                qty = budget / entry_price
                mode = SizingMode.NOTIONAL
        else:
            qty = budget / entry_price
            mode = SizingMode.NOTIONAL

        # Cap por max_notional de config
        notional = qty * entry_price
        if notional > max_notional:
            qty = max_notional / entry_price

        # Cap por max_qty del símbolo
        if qty > constraints.max_qty:
            qty = constraints.max_qty

        # Cuantización por step_size (floor: nunca exceder caps)
        if constraints.step_size > Decimal("0"):
            qty = (qty // constraints.step_size) * constraints.step_size

        # Si no alcanza min_qty, la orden no es viable
        if qty < constraints.min_qty:
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                notional_budget_used=Decimal("0"),
                sizing_mode=mode,
                rationale=(
                    f"qty={qty} < min_qty={constraints.min_qty} for {symbol}: "
                    f"sizing inviable con notional_pct={pct}"
                ),
            )

        final_notional = qty * entry_price
        notional_budget = final_notional / equity

        return SizingDecision(
            target_qty=qty,
            target_notional=final_notional,
            notional_budget_used=notional_budget,
            sizing_mode=mode,
            rationale=(
                f"symbol={symbol} mode={mode.value} notional_pct={pct} "
                f"equity={equity} entry={entry_price}"
                + (f" stop={stop_price}" if stop_price else "")
            ),
        )
