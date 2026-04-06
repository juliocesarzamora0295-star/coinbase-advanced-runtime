"""
BacktestLedger — tracking de cash, posiciones, equity y PnL por trade.

Versión simplificada para backtesting. Sin SQLite, sin persistencia.
Modelo contable: cash + inventory * mark_price = equity.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List


@dataclass
class BacktestTrade:
    """Registro de un trade completado (entry + exit)."""

    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    side: str  # "BUY" (long) — entry=buy, exit=sell
    pnl: Decimal
    entry_ts_ms: int
    exit_ts_ms: int

    @property
    def duration_ms(self) -> int:
        return self.exit_ts_ms - self.entry_ts_ms

    @property
    def is_winner(self) -> bool:
        return self.pnl > Decimal("0")


class BacktestLedger:
    """
    Ledger simplificado para backtest.

    Modelo: cash + position_qty * mark_price = equity
    Trackea equity curve y trades completados.
    """

    def __init__(self, initial_cash: Decimal = Decimal("10000")) -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.position_qty = Decimal("0")
        self.avg_entry = Decimal("0")
        self.cost_basis = Decimal("0")
        self.fees_paid = Decimal("0")

        # Tracking
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[tuple[int, Decimal]] = []  # (ts_ms, equity)
        self.equity_peak = initial_cash

        # Current position entry timestamp
        self._entry_ts_ms: int = 0

    def buy(
        self,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
        ts_ms: int = 0,
    ) -> None:
        """Comprar qty a price. Reduce cash, aumenta posición."""
        cost = qty * price + fee
        self.cash -= cost
        self.fees_paid += fee

        if self.position_qty == Decimal("0"):
            self._entry_ts_ms = ts_ms

        # Update average entry
        total_cost = self.cost_basis + qty * price
        self.position_qty += qty
        self.cost_basis = total_cost
        self.avg_entry = total_cost / self.position_qty if self.position_qty > 0 else Decimal("0")

    def sell(
        self,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
        ts_ms: int = 0,
    ) -> None:
        """Vender qty a price. Aumenta cash, reduce posición."""
        if qty > self.position_qty:
            qty = self.position_qty  # cap at position

        if qty <= Decimal("0"):
            return

        proceeds = qty * price - fee
        self.cash += proceeds
        self.fees_paid += fee

        # PnL for this portion
        pnl = (price - self.avg_entry) * qty - fee

        # Record trade if fully closing or partial close
        self.trades.append(
            BacktestTrade(
                entry_price=self.avg_entry,
                exit_price=price,
                qty=qty,
                side="BUY",
                pnl=pnl,
                entry_ts_ms=self._entry_ts_ms,
                exit_ts_ms=ts_ms,
            )
        )

        # Reduce position
        self.position_qty -= qty
        if self.position_qty <= Decimal("1e-12"):
            self.position_qty = Decimal("0")
            self.cost_basis = Decimal("0")
            self.avg_entry = Decimal("0")
        else:
            self.cost_basis = self.avg_entry * self.position_qty

    def mark(self, price: Decimal, ts_ms: int = 0) -> Decimal:
        """
        Mark to market. Registra equity en la curve.

        Returns:
            Equity actual.
        """
        equity = self.get_equity(price)
        self.equity_curve.append((ts_ms, equity))
        if equity > self.equity_peak:
            self.equity_peak = equity
        return equity

    def get_equity(self, price: Decimal) -> Decimal:
        """equity = cash + position * price"""
        return self.cash + self.position_qty * price

    def get_drawdown(self, price: Decimal) -> Decimal:
        """Current drawdown from peak."""
        equity = self.get_equity(price)
        if self.equity_peak <= Decimal("0"):
            return Decimal("0")
        if equity >= self.equity_peak:
            return Decimal("0")
        return (self.equity_peak - equity) / self.equity_peak

    def get_total_pnl(self) -> Decimal:
        """Sum of realized PnL from closed trades."""
        return sum((t.pnl for t in self.trades), Decimal("0"))
