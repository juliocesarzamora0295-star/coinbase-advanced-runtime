"""
PortfolioSnapshot — modelo institucional de portfolio inmutable.

Separa explícitamente: cash, inventory, fees, reserved balances,
realized/unrealized PnL y equity.

Fórmula de equity:
    equity = cash_balance + inventory_value - fees_accrued + unrealized_pnl

Donde:
    cash_balance   = realized_pnl_quote + fees_accrued_quote
                     (PnL bruto antes de descuento explícito de fees)
    inventory_value = cost_basis_quote  (capital bloqueado en posición abierta)
    fees_accrued   = total de fees pagados en equivalente QUOTE
    unrealized_pnl = (mark_price - avg_entry) × position_qty

Esto equivale a: realized_pnl_neto + mark_price × position_qty
que es la identidad que satisface el ledger actual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.accounting.ledger import TradeLedger


@dataclass(frozen=True)
class PortfolioSnapshot:
    """
    Snapshot inmutable del portfolio en un instante.

    Todos los valores en QUOTE currency (ej: USD para BTC-USD).
    `frozen=True` garantiza reproducibilidad: un snapshot no muta tras crearse.
    """

    symbol: str
    ts_ms: int
    mark_price: Decimal

    # Saldos
    cash_balance: Decimal     # PnL bruto realizado (antes de descuento explícito de fees)
    reserved_balance: Decimal  # QUOTE reservado para órdenes abiertas aún no ejecutadas

    # Inventario
    inventory_value: Decimal  # cost_basis_quote: capital bloqueado en posición abierta
    position_qty: Decimal     # Cantidad BASE en posición
    avg_entry: Decimal        # Precio promedio de entrada

    # Fees
    fees_accrued: Decimal     # Total fees acumulados en QUOTE equivalente

    # PnL
    realized_pnl: Decimal     # PnL neto realizado (neto de fees, del ledger)
    unrealized_pnl: Decimal   # (mark_price - avg_entry) × position_qty

    @property
    def equity(self) -> Decimal:
        """
        Equity institucional.

        equity = cash_balance + inventory_value - fees_accrued + unrealized_pnl

        Invariante: equity == realized_pnl_neto + mark_price × position_qty
        """
        return self.cash_balance + self.inventory_value - self.fees_accrued + self.unrealized_pnl

    @property
    def inventory_mark_value(self) -> Decimal:
        """Valor de mercado de la posición al mark_price."""
        return self.position_qty * self.mark_price

    @classmethod
    def from_ledger(
        cls,
        ledger: "TradeLedger",
        mark_price: Decimal,
        reserved_balance: Decimal = Decimal("0"),
        ts_ms: Optional[int] = None,
    ) -> "PortfolioSnapshot":
        """
        Construir snapshot desde el estado actual del ledger.

        Args:
            ledger: TradeLedger con estado actualizado.
            mark_price: Precio de referencia para valorizar posición abierta.
            reserved_balance: QUOTE reservado para órdenes abiertas (externo al ledger).
            ts_ms: Timestamp en ms. Si None, usa UTC ahora.

        Returns:
            PortfolioSnapshot inmutable.
        """
        if mark_price < Decimal("0"):
            raise ValueError(f"mark_price no puede ser negativo: {mark_price}")
        if reserved_balance < Decimal("0"):
            raise ValueError(f"reserved_balance no puede ser negativo: {reserved_balance}")

        unrealized = ledger.get_unrealized_pnl(mark_price)

        # cash_balance = PnL bruto = realized_neto + fees
        # Así la ecuación equity = cash + inventory - fees + unrealized reduce a:
        # equity = realized_neto + inventory - fees + fees + unrealized  ← CORRECTO
        # equity = realized_neto + cost_basis + unrealized
        # equity = realized_neto + mark × qty   (ya que unrealized = mark×qty - cost_basis)
        cash_balance = ledger.realized_pnl_quote + ledger.fees_accrued_quote

        snapshot_ts = ts_ms if ts_ms is not None else int(
            datetime.now(tz=timezone.utc).timestamp() * 1000
        )

        return cls(
            symbol=ledger.symbol,
            ts_ms=snapshot_ts,
            mark_price=mark_price,
            cash_balance=cash_balance,
            reserved_balance=reserved_balance,
            inventory_value=ledger.cost_basis_quote,
            position_qty=ledger.position_qty,
            avg_entry=ledger.avg_entry,
            fees_accrued=ledger.fees_accrued_quote,
            realized_pnl=ledger.realized_pnl_quote,
            unrealized_pnl=unrealized,
        )
