"""
PortfolioSnapshot — valor de equity del portfolio en un instante.

Este módulo expone el contrato mínimo necesario para alimentar DailyRiskTracker:
    tracker.update(snapshot.equity)

La versión completa con cash, inventory, fees y unrealized PnL
se implementa en Sprint 3 PR-3.1.
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

    equity es el único campo requerido por DailyRiskTracker.
    Campos adicionales se añaden en PR-3.1.
    """

    symbol: str
    ts_ms: int
    equity: Decimal  # Valor total del portfolio en QUOTE (realized + mark_value)

    @classmethod
    def from_ledger(
        cls,
        ledger: "TradeLedger",
        mark_price: Decimal,
        ts_ms: Optional[int] = None,
    ) -> "PortfolioSnapshot":
        """
        Construir snapshot desde el ledger.

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
