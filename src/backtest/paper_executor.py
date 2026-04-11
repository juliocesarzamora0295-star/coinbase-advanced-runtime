"""
PaperExecutor para backtest — simula fills instantáneos.

Sin red, sin persistencia. Slippage configurable.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.backtest.ledger import BacktestLedger


@dataclass(frozen=True)
class BacktestFill:
    """Fill simulado en backtest."""

    side: str  # "BUY" or "SELL"
    qty: Decimal
    price: Decimal  # precio de ejecución (con slippage aplicado)
    fee: Decimal
    ts_ms: int


class PaperExecutor:
    """
    Ejecutor simulado para backtest.

    Fills instantáneos al precio dado + slippage configurable.
    Fee como fracción del notional.
    """

    def __init__(
        self,
        ledger: BacktestLedger,
        slippage_bps: Decimal = Decimal("0"),
        fee_rate: Decimal = Decimal("0.001"),  # 0.1% default
    ) -> None:
        self.ledger = ledger
        self.slippage_bps = slippage_bps
        self.fee_rate = fee_rate

    def execute(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
        ts_ms: int = 0,
    ) -> Optional[BacktestFill]:
        """
        Ejecutar orden simulada.

        Slippage:
        - BUY: price * (1 + slippage_bps / 10000) → desfavorable
        - SELL: price * (1 - slippage_bps / 10000) → desfavorable

        SELL qty se capea a ledger.position_qty ANTES de calcular notional/fee.
        Si no hay posición para vender, retorna None (fail-closed).

        Returns:
            BacktestFill con precio ajustado, o None si no se ejecutó.
        """
        side_up = side.upper()

        if side_up == "SELL":
            # Cap qty to available position BEFORE computing fee/notional.
            if qty > self.ledger.position_qty:
                qty = self.ledger.position_qty
            if qty <= Decimal("0"):
                return None

        if side_up == "BUY":
            exec_price = price * (Decimal("1") + self.slippage_bps / Decimal("10000"))
        else:
            exec_price = price * (Decimal("1") - self.slippage_bps / Decimal("10000"))

        notional = qty * exec_price
        fee = notional * self.fee_rate

        fill = BacktestFill(
            side=side_up,
            qty=qty,
            price=exec_price,
            fee=fee,
            ts_ms=ts_ms,
        )

        # Apply to ledger
        if side_up == "BUY":
            self.ledger.buy(qty, exec_price, fee, ts_ms)
        else:
            self.ledger.sell(qty, exec_price, fee, ts_ms)

        return fill
