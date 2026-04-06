"""
BacktestReport — resumen de resultados con métricas estándar.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import List

from src.backtest.ledger import BacktestLedger, BacktestTrade

ZERO = Decimal("0")


@dataclass(frozen=True)
class BacktestReport:
    """Resumen inmutable de un backtest completo."""

    total_bars: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal  # 0-1
    total_pnl: Decimal
    max_drawdown: Decimal  # 0-1
    sharpe_ratio: float  # annualized
    avg_trade_pnl: Decimal
    avg_trade_duration_ms: int
    initial_equity: Decimal
    final_equity: Decimal
    return_pct: Decimal  # (final - initial) / initial
    fees_paid: Decimal

    def __str__(self) -> str:
        return (
            f"BacktestReport:\n"
            f"  Bars: {self.total_bars}\n"
            f"  Trades: {self.total_trades} (W:{self.winning_trades} L:{self.losing_trades})\n"
            f"  Win Rate: {self.win_rate:.1%}\n"
            f"  PnL: ${self.total_pnl:,.2f}\n"
            f"  Return: {self.return_pct:.2%}\n"
            f"  Max Drawdown: {self.max_drawdown:.2%}\n"
            f"  Sharpe: {self.sharpe_ratio:.2f}\n"
            f"  Avg PnL/Trade: ${self.avg_trade_pnl:,.2f}\n"
            f"  Avg Duration: {self.avg_trade_duration_ms / 1000:.0f}s\n"
            f"  Fees: ${self.fees_paid:,.2f}\n"
            f"  Equity: ${self.initial_equity:,.2f} → ${self.final_equity:,.2f}"
        )


def build_report(
    ledger: BacktestLedger,
    total_bars: int,
    final_price: Decimal,
) -> BacktestReport:
    """
    Construir reporte desde el estado final del ledger.

    Args:
        ledger: BacktestLedger con trades y equity curve
        total_bars: número total de barras procesadas
        final_price: precio de cierre final (para MTM)
    """
    trades = ledger.trades
    total_trades = len(trades)
    winning = sum(1 for t in trades if t.is_winner)
    losing = total_trades - winning

    win_rate = Decimal(str(winning)) / Decimal(str(total_trades)) if total_trades > 0 else ZERO

    total_pnl = ledger.get_total_pnl()
    avg_pnl = total_pnl / Decimal(str(total_trades)) if total_trades > 0 else ZERO

    avg_duration = 0
    if total_trades > 0:
        avg_duration = sum(t.duration_ms for t in trades) // total_trades

    final_equity = ledger.get_equity(final_price)
    return_pct = (
        (final_equity - ledger.initial_cash) / ledger.initial_cash
        if ledger.initial_cash > ZERO
        else ZERO
    )

    max_dd = _compute_max_drawdown(ledger.equity_curve)
    sharpe = _compute_sharpe(ledger.equity_curve)

    return BacktestReport(
        total_bars=total_bars,
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=win_rate,
        total_pnl=total_pnl,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        avg_trade_pnl=avg_pnl,
        avg_trade_duration_ms=avg_duration,
        initial_equity=ledger.initial_cash,
        final_equity=final_equity,
        return_pct=return_pct,
        fees_paid=ledger.fees_paid,
    )


def _compute_max_drawdown(equity_curve: List[tuple[int, Decimal]]) -> Decimal:
    """Max drawdown from equity curve."""
    if not equity_curve:
        return ZERO

    peak = ZERO
    max_dd = ZERO

    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > ZERO:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    return max_dd


def _compute_sharpe(
    equity_curve: List[tuple[int, Decimal]],
    periods_per_year: int = 252,
) -> float:
    """
    Sharpe ratio from equity curve (annualized).

    Uses simple returns between consecutive equity points.
    Risk-free rate assumed 0.
    """
    if len(equity_curve) < 2:
        return 0.0

    returns = []
    for i in range(1, len(equity_curve)):
        prev_eq = float(equity_curve[i - 1][1])
        curr_eq = float(equity_curve[i][1])
        if prev_eq > 0:
            returns.append((curr_eq - prev_eq) / prev_eq)

    if not returns:
        return 0.0

    mean_r = sum(returns) / len(returns)
    if len(returns) < 2:
        return 0.0

    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0

    if std_r == 0:
        return 0.0

    return (mean_r / std_r) * math.sqrt(periods_per_year)
