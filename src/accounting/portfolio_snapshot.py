"""
PortfolioSnapshot — contrato mínimo de equity para PositionSizer.

La versión completa con cash, inventory, fees, unrealized PnL y
open_order_exposure se implementa en Sprint 3 PR-3.1 / PR-3.3.
Este módulo expone el contrato mínimo que PositionSizer necesita:
    snapshot.equity: Decimal

Flujo:
    snapshot = PortfolioSnapshot.from_ledger(ledger, mark_price)
    decision = sizer.compute_from_snapshot(snapshot, ...)
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
    Snapshot inmutable del portfolio.

    equity = realized_pnl + mark_price × position_qty
    Alimentar PositionSizer.compute_from_snapshot() con este objeto.
    """

    symbol: str
    ts_ms: int
    equity: Decimal  # Valor total del portfolio en QUOTE

    @classmethod
    def from_ledger(
        cls,
        ledger: "TradeLedger",
        mark_price: Decimal,
        ts_ms: Optional[int] = None,
    ) -> "PortfolioSnapshot":
        """
        Construir snapshot desde ledger.

        equity = ledger.get_equity(mark_price)
        """
        if mark_price < Decimal("0"):
            raise ValueError(f"mark_price no puede ser negativo: {mark_price}")
        snapshot_ts = (
            ts_ms
            if ts_ms is not None
            else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        )
        return cls(
            symbol=ledger.symbol,
            ts_ms=snapshot_ts,
            equity=ledger.get_equity(mark_price),
        )
