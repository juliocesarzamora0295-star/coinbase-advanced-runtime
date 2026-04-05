"""
Contrato de dominio: PositionSizer + SizingDecision

PositionSizer calcula el target_qty para una señal dada equity y constraints.
Esta qty es una propuesta; RiskGate aplica caps adicionales.
OrderPlanner toma la decisión final: final_qty = min(target_qty, hard_max_qty).

Modos de sizing (semánticamente distintos):

    ALLOCATION  — target_notional_pct
        qty = (equity × target_notional_pct) / entry_price
        Responde: "¿Qué tamaño de posición representa X% del portfolio?"
        No requiere stop_price.

    RISK_BASED  — risk_per_trade_pct + stop_price
        qty = (equity × risk_per_trade_pct) / stop_distance
        Responde: "¿Qué qty arriesga exactamente X% del equity si el stop se activa?"
        stop_price requerido para precisión; sin stop usa entry_price como fallback.

    BOTH        — min(qty_allocation, qty_risk)
        Conservador: toma el menor de ambos. Útil cuando la señal tiene stop
        Y el portfolio tiene un target de allocación máximo.

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

    target_qty: Decimal       # cantidad propuesta (puede ser 0)
    target_notional: Decimal  # notional de target_qty al precio de entrada
    risk_budget_used: Decimal  # fracción del equity comprometida (0.0–1.0)
    rationale: str            # descripción legible del cálculo


def _zero_decision(reason: str) -> SizingDecision:
    return SizingDecision(
        target_qty=Decimal("0"),
        target_notional=Decimal("0"),
        risk_budget_used=Decimal("0"),
        rationale=reason,
    )


class PositionSizer:
    """
    Calcula target_qty a partir de equity, parámetros de sizing y constraints.

    No tiene acceso a RiskGate. Solo calcula sizing.
    RiskGate aplica sus propios caps sobre este resultado.
    """

    def compute(
        self,
        *,
        symbol: str,
        equity: Optional[Decimal],
        entry_price: Decimal,
        constraints: SymbolConstraints,
        max_notional: Decimal,
        # ── Modo ALLOCATION ──────────────────────────────────────────
        target_notional_pct: Optional[Decimal] = None,
        # ── Modo RISK_BASED ──────────────────────────────────────────
        risk_per_trade_pct: Decimal = Decimal("0"),
        stop_price: Optional[Decimal] = None,
    ) -> SizingDecision:
        """
        Calcular SizingDecision.

        Exactamente uno de target_notional_pct o risk_per_trade_pct debe ser > 0,
        o ambos (resultado conservador = mínimo de los dos).

        Args:
            symbol: Símbolo (para rationale/logging).
            equity: Equity del portfolio en QUOTE. None → FailClosedError.
            entry_price: Precio de entrada de referencia.
            constraints: Constraints del símbolo del exchange.
            max_notional: Cap de notional por config.
            target_notional_pct: Fracción del equity a asignar (ej. Decimal("0.10")).
                Modo ALLOCATION. No requiere stop_price.
            risk_per_trade_pct: Fracción del equity a arriesgar por trade (ej. Decimal("0.01")).
                Modo RISK_BASED. Con stop_price usa stop_distance; sin stop usa entry_price.
            stop_price: Precio de stop para modo RISK_BASED.

        Returns:
            SizingDecision con target_qty >= 0.

        Raises:
            FailClosedError: Si equity es None.
        """
        # ── Fail-closed: equity obligatorio ──────────────────────────
        if equity is None:
            raise FailClosedError(
                f"equity is None for {symbol}: cannot size position (fail-closed)"
            )

        # ── Casos degenerados: retornan qty=0 sin error ───────────────
        if equity <= Decimal("0"):
            return _zero_decision(f"equity={equity} <= 0")

        if entry_price <= Decimal("0"):
            return _zero_decision(f"entry_price={entry_price} <= 0")

        has_allocation = target_notional_pct is not None and target_notional_pct > Decimal("0")
        has_risk = risk_per_trade_pct > Decimal("0")

        if not has_allocation and not has_risk:
            return _zero_decision(
                f"target_notional_pct={target_notional_pct} y "
                f"risk_per_trade_pct={risk_per_trade_pct}: ningún modo activo"
            )

        qty_candidates: list[Decimal] = []
        mode_parts: list[str] = []

        # ── Modo ALLOCATION ──────────────────────────────────────────
        if has_allocation:
            assert target_notional_pct is not None  # mypy
            qty_alloc = (equity * target_notional_pct) / entry_price
            qty_candidates.append(qty_alloc)
            mode_parts.append(
                f"ALLOCATION(target_notional_pct={target_notional_pct} "
                f"qty_alloc={qty_alloc:.8f})"
            )

        # ── Modo RISK_BASED ──────────────────────────────────────────
        if has_risk:
            risk_amount = equity * risk_per_trade_pct
            if stop_price is not None and stop_price > Decimal("0"):
                stop_distance = abs(entry_price - stop_price)
                if stop_distance > Decimal("0"):
                    qty_risk = risk_amount / stop_distance
                    mode_parts.append(
                        f"RISK_BASED(risk_per_trade_pct={risk_per_trade_pct} "
                        f"stop_distance={stop_distance} qty_risk={qty_risk:.8f})"
                    )
                else:
                    # stop coincide con entry: fallback a pct-of-price
                    qty_risk = risk_amount / entry_price
                    mode_parts.append(
                        f"RISK_BASED_FALLBACK(stop_distance=0 "
                        f"qty_risk={qty_risk:.8f})"
                    )
            else:
                qty_risk = risk_amount / entry_price
                mode_parts.append(
                    f"RISK_BASED(risk_per_trade_pct={risk_per_trade_pct} "
                    f"no_stop qty_risk={qty_risk:.8f})"
                )
            qty_candidates.append(qty_risk)

        # ── Conservador: mínimo de los modos activos ─────────────────
        qty = min(qty_candidates)

        # ── Caps ─────────────────────────────────────────────────────
        notional = qty * entry_price
        if notional > max_notional:
            qty = max_notional / entry_price

        if qty > constraints.max_qty:
            qty = constraints.max_qty

        # ── Cuantización por step_size (floor) ───────────────────────
        if constraints.step_size > Decimal("0"):
            qty = (qty // constraints.step_size) * constraints.step_size

        # ── Viabilidad: min_qty ───────────────────────────────────────
        if qty < constraints.min_qty:
            return _zero_decision(
                f"qty={qty} < min_qty={constraints.min_qty} for {symbol}: "
                f"sizing inviable con {' + '.join(mode_parts)}"
            )

        final_notional = qty * entry_price
        risk_budget = final_notional / equity

        return SizingDecision(
            target_qty=qty,
            target_notional=final_notional,
            risk_budget_used=risk_budget,
            rationale=(
                f"symbol={symbol} equity={equity} entry={entry_price} "
                + " | ".join(mode_parts)
                + (f" | stop={stop_price}" if stop_price else "")
            ),
        )

    def compute_from_snapshot(
        self,
        *,
        snapshot: object,  # PortfolioSnapshot or any object with .equity: Decimal
        symbol: str,
        entry_price: Decimal,
        constraints: SymbolConstraints,
        max_notional: Decimal,
        target_notional_pct: Optional[Decimal] = None,
        risk_per_trade_pct: Decimal = Decimal("0"),
        stop_price: Optional[Decimal] = None,
    ) -> SizingDecision:
        """
        Sizing usando equity real del PortfolioSnapshot.

        Extrae `snapshot.equity` y delega a compute(). Si snapshot no tiene
        `equity` o es None → FailClosedError.

        Args:
            snapshot: PortfolioSnapshot (o cualquier objeto con .equity: Decimal).
            Resto: idéntico a compute().
        """
        equity: Optional[Decimal] = getattr(snapshot, "equity", None)
        if equity is None:
            raise FailClosedError(
                f"snapshot.equity is None for {symbol}: cannot size position (fail-closed)"
            )
        return self.compute(
            symbol=symbol,
            equity=equity,
            entry_price=entry_price,
            constraints=constraints,
            max_notional=max_notional,
            target_notional_pct=target_notional_pct,
            risk_per_trade_pct=risk_per_trade_pct,
            stop_price=stop_price,
        )
