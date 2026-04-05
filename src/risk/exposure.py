"""
Open-order exposure — notional en riesgo por órdenes activas.

Separar inventory_value (posición ya ejecutada) de open_order_exposure
(órdenes vivas que aún pueden ejecutarse) es crítico para evitar
subestimar el riesgo real del portfolio.

gross_exposure = inventory_value + open_order_exposure

Uso típico:
    entries = [
        OpenOrderEntry(
            client_order_id=rec.client_order_id,
            side=rec.intent.side,
            qty=rec.intent.qty,
            price=rec.intent.price or mark_price,
            state=rec.state.name,
        )
        for rec in idempotency_store.get_pending_or_open()
    ]
    report = OpenOrderExposureReport.from_entries(entries)
    snapshot = PortfolioSnapshot.from_ledger(
        ledger, mark_price, open_order_exposure=report.total
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


@dataclass(frozen=True)
class OpenOrderEntry:
    """
    Entrada de orden abierta para el cálculo de exposure.

    qty y price son los valores en el momento de calcular:
    - qty: cantidad restante (no ejecutada)
    - price: precio límite para órdenes límite; mark_price para market orders
    """

    client_order_id: str
    side: str       # "BUY" | "SELL"
    qty: Decimal    # Cantidad BASE pendiente de ejecución
    price: Decimal  # Precio de referencia en QUOTE
    state: str      # "OPEN_RESTING" | "OPEN_PENDING" | "CANCEL_QUEUED"

    def __post_init__(self) -> None:
        if self.qty < Decimal("0"):
            raise ValueError(
                f"OpenOrderEntry qty no puede ser negativo: {self.qty!r} "
                f"(client_order_id={self.client_order_id!r})"
            )
        if self.price < Decimal("0"):
            raise ValueError(
                f"OpenOrderEntry price no puede ser negativo: {self.price!r} "
                f"(client_order_id={self.client_order_id!r})"
            )

    @property
    def notional(self) -> Decimal:
        """Notional de esta orden: qty × price."""
        return self.qty * self.price


@dataclass(frozen=True)
class OpenOrderExposureReport:
    """
    Snapshot inmutable del exposure total por órdenes abiertas.

    Desglosa el exposure por side para auditoría institucional.
    """

    entries: tuple[OpenOrderEntry, ...]
    buy_exposure: Decimal   # Suma de notional de órdenes BUY
    sell_exposure: Decimal  # Suma de notional de órdenes SELL
    total: Decimal          # buy_exposure + sell_exposure

    @classmethod
    def from_entries(cls, entries: Sequence[OpenOrderEntry]) -> "OpenOrderExposureReport":
        """
        Construir reporte desde una secuencia de entradas.

        Cero entradas → todas las métricas en Decimal("0").
        """
        buy_exp = Decimal("0")
        sell_exp = Decimal("0")
        for e in entries:
            if e.side.upper() == "BUY":
                buy_exp += e.notional
            else:
                sell_exp += e.notional
        return cls(
            entries=tuple(entries),
            buy_exposure=buy_exp,
            sell_exposure=sell_exp,
            total=buy_exp + sell_exp,
        )

    @classmethod
    def empty(cls) -> "OpenOrderExposureReport":
        """Reporte vacío (sin órdenes abiertas)."""
        return cls(
            entries=(),
            buy_exposure=Decimal("0"),
            sell_exposure=Decimal("0"),
            total=Decimal("0"),
        )
