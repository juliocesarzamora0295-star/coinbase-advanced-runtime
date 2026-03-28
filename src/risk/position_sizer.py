"""
Contrato de dominio: PositionSizer + SizingDecision

PositionSizer calcula el target_qty para una señal dada equity y constraints.
Esta qty es una propuesta; RiskGate aplica caps adicionales.
OrderPlanner toma la decisión final: final_qty = min(target_qty, hard_max_qty).

Invariantes:
- Si equity es None → FailClosedError. No se inventan defaults.
- target_qty nunca excede max_notional ni max_qty del símbolo.
- target_qty respeta step_size (cuantización hacia abajo).
- SizingDecision es inmutable (frozen=True).
- PositionSizer no tiene acceso a RiskGate ni a OMS.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


class FailClosedError(Exception):
    """Raised when a required input is missing. Fail-closed invariant."""

    pass


@dataclass(frozen=True)
class SymbolConstraints:
    """Constraints del símbolo tal como los reporta el exchange."""

    step_size: Decimal  # mínimo incremento de qty
    min_qty: Decimal  # qty mínima aceptada
    max_qty: Decimal  # qty máxima aceptada (usar Decimal("Infinity") si no hay límite)
    min_notional: Decimal  # notional mínimo aceptado


@dataclass(frozen=True)
class SizingDecision:
    """Resultado del cálculo de sizing. Inmutable."""

    target_qty: Decimal  # cantidad propuesta (puede ser 0)
    target_notional: Decimal  # notional de target_qty al precio de entrada
    risk_budget_used: Decimal  # fracción del equity comprometida (0.0–1.0)
    rationale: str  # descripción legible del cálculo


class PositionSizer:
    """
    Calcula target_qty a partir de equity, risk_pct y constraints del símbolo.

    No tiene acceso a RiskGate. Solo calcula sizing.
    RiskGate aplica sus propios caps sobre este resultado.
    """

    def compute(
        self,
        *,
        symbol: str,
        equity: Optional[Decimal],
        entry_price: Decimal,
        risk_per_trade_pct: Decimal,
        constraints: SymbolConstraints,
        max_notional: Decimal,
        stop_price: Optional[Decimal] = None,
    ) -> SizingDecision:
        """
        Calcular SizingDecision.

        Args:
            symbol: Símbolo a operar (usado solo para logging/rationale).
            equity: Equity disponible. None → FailClosedError (no defaults).
            entry_price: Precio de entrada de referencia.
            risk_per_trade_pct: Fracción del equity a arriesgar por trade (ej. Decimal("0.01")).
            constraints: Constraints del símbolo del exchange.
            max_notional: Notional máximo permitido por config.
            stop_price: Precio de stop (None para sizing por porcentaje fijo).

        Returns:
            SizingDecision con target_qty >= 0.

        Raises:
            FailClosedError: Si equity es None.
        """
        if equity is None:
            raise FailClosedError(
                f"equity is None for {symbol}: cannot size position (fail-closed)"
            )

        if equity <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                risk_budget_used=Decimal("0"),
                rationale=f"equity={equity} <= 0",
            )

        if entry_price <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                risk_budget_used=Decimal("0"),
                rationale=f"entry_price={entry_price} <= 0",
            )

        if risk_per_trade_pct <= Decimal("0"):
            return SizingDecision(
                target_qty=Decimal("0"),
                target_notional=Decimal("0"),
                risk_budget_used=Decimal("0"),
                rationale=f"risk_per_trade_pct={risk_per_trade_pct} <= 0",
            )

        risk_amount = equity * risk_per_trade_pct

        # Sizing por stop si está disponible, sino por porcentaje directo
        if stop_price is not None and stop_price > Decimal("0"):
            stop_distance = abs(entry_price - stop_price)
            if stop_distance > Decimal("0"):
                qty = risk_amount / stop_distance
            else:
                qty = risk_amount / entry_price
        else:
            qty = risk_amount / entry_price

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
                risk_budget_used=Decimal("0"),
                rationale=(
                    f"qty={qty} < min_qty={constraints.min_qty} for {symbol}: "
                    f"sizing inviable con risk_pct={risk_per_trade_pct}"
                ),
            )

        final_notional = qty * entry_price
        risk_budget = final_notional / equity

        return SizingDecision(
            target_qty=qty,
            target_notional=final_notional,
            risk_budget_used=risk_budget,
            rationale=(
                f"symbol={symbol} risk_pct={risk_per_trade_pct} "
                f"equity={equity} entry={entry_price}"
                + (f" stop={stop_price}" if stop_price else "")
            ),
        )
