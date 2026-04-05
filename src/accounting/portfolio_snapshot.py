"""
PortfolioSnapshot — modelo institucional de portfolio inmutable.

Separa explícitamente:
    cash_balance        — PnL realizado (bruto, antes de fees como línea separada)
    inventory_value     — cost_basis_quote: capital en posición ejecutada
    fees_accrued        — total fees pagados en QUOTE equivalente
    reserved_balance    — QUOTE reservado para órdenes BUY abiertas
    open_order_exposure — notional total de órdenes activas (resting + pending)
    unrealized_pnl      — (mark_price - avg_entry) × position_qty

Fórmulas:
    equity         = cash_balance + inventory_value - fees_accrued + unrealized_pnl
    gross_exposure = inventory_value + open_order_exposure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.accounting.ledger import TradeLedger


@dataclass(frozen=True)
class PortfolioSnapshot:
    """
    Snapshot inmutable del portfolio en un instante.

    Todos los valores monetarios en QUOTE currency (ej: USD para BTC-USD).
    `frozen=True` garantiza reproducibilidad: el snapshot no muta tras crearse.
    """

    symbol: str
    ts_ms: int
    mark_price: Decimal

    # ── Saldos ────────────────────────────────────────────────────────
    cash_balance: Decimal      # PnL bruto realizado (realized_net + fees)
    reserved_balance: Decimal  # QUOTE reservado para órdenes BUY abiertas

    # ── Inventario ────────────────────────────────────────────────────
    inventory_value: Decimal   # cost_basis_quote: capital en posición ejecutada
    position_qty: Decimal      # Cantidad BASE en posición abierta
    avg_entry: Decimal         # Precio promedio de entrada

    # ── Fees ─────────────────────────────────────────────────────────
    fees_accrued: Decimal      # Total fees pagados en QUOTE equivalente

    # ── P&L ──────────────────────────────────────────────────────────
    realized_pnl: Decimal      # PnL neto realizado (neto de fees)
    unrealized_pnl: Decimal    # (mark_price - avg_entry) × position_qty

    # ── Exposure por órdenes abiertas ────────────────────────────────
    open_order_exposure: Decimal  # Notional total de órdenes activas (resting+pending)

    # ── Propiedades derivadas ─────────────────────────────────────────

    @property
    def equity(self) -> Decimal:
        """
        Equity institucional.

        equity = cash_balance + inventory_value - fees_accrued + unrealized_pnl

        Invariante: equity == realized_pnl_neto + mark_price × position_qty
        """
        return (
            self.cash_balance
            + self.inventory_value
            - self.fees_accrued
            + self.unrealized_pnl
        )

    @property
    def gross_exposure(self) -> Decimal:
        """
        Exposure bruto del portfolio.

        gross_exposure = inventory_value + open_order_exposure

        Incluye tanto la posición ejecutada (inventory) como las órdenes
        activas que aún pueden ejecutarse. Usar en checks de concentration
        y leverage.
        """
        return self.inventory_value + self.open_order_exposure

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
        open_order_exposure: Decimal = Decimal("0"),
        ts_ms: Optional[int] = None,
    ) -> "PortfolioSnapshot":
        """
        Construir snapshot desde el estado actual del ledger.

        Args:
            ledger: TradeLedger con estado actualizado.
            mark_price: Precio de referencia para valorizar posición abierta.
            reserved_balance: QUOTE reservado para órdenes BUY abiertas.
            open_order_exposure: Notional total de órdenes activas.
                Usar OpenOrderExposureReport.from_entries(entries).total
                para calcularlo desde IdempotencyStore.
            ts_ms: Timestamp en ms. Si None, usa UTC ahora.

        Returns:
            PortfolioSnapshot inmutable.

        Raises:
            ValueError: Si mark_price, reserved_balance u open_order_exposure
                son negativos.
        """
        if mark_price < Decimal("0"):
            raise ValueError(f"mark_price no puede ser negativo: {mark_price}")
        if reserved_balance < Decimal("0"):
            raise ValueError(f"reserved_balance no puede ser negativo: {reserved_balance}")
        if open_order_exposure < Decimal("0"):
            raise ValueError(
                f"open_order_exposure no puede ser negativo: {open_order_exposure}"
            )

        unrealized = ledger.get_unrealized_pnl(mark_price)

        # Intentar obtener fees_accrued_quote si el ledger lo tiene (PR-3.1+)
        fees_accrued = getattr(ledger, "fees_accrued_quote", Decimal("0"))

        # cash_balance = PnL bruto = realized_neto + fees
        cash_balance = ledger.realized_pnl_quote + fees_accrued

        snapshot_ts = (
            ts_ms
            if ts_ms is not None
            else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
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
            fees_accrued=fees_accrued,
            realized_pnl=ledger.realized_pnl_quote,
            unrealized_pnl=unrealized,
            open_order_exposure=open_order_exposure,
        )
