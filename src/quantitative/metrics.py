"""
Trading performance metrics — Sharpe, drawdown, win rate, profit factor.

Pure functions operating on equity curves and trade lists.
No side effects, no I/O.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Tuple

ZERO = Decimal("0")


@dataclass(frozen=True)
class TradeRecord:
    """Minimal trade record for metric computation."""

    pnl: Decimal
    duration_ms: int = 0


@dataclass(frozen=True)
class PerformanceMetrics:
    """Complete set of trading performance metrics."""

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float  # 0-1
    total_pnl: Decimal
    profit_factor: float  # gross_profit / gross_loss (inf if no losses)
    max_drawdown: float  # 0-1 fraction
    sharpe_ratio: float  # annualized
    avg_trade_pnl: Decimal
    avg_win: Decimal
    avg_loss: Decimal
    avg_trade_duration_ms: int
    return_pct: float  # total return as fraction

    # Extended metrics (institutional)
    cagr: float = 0.0  # compound annual growth rate
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_consecutive_losses: int = 0
    recovery_factor: float = 0.0  # total_pnl / max_drawdown_abs
    time_in_market_pct: float = 0.0
    turnover_rate: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0

    def passes_certification(
        self,
        min_sharpe: float = 0.0,
        max_drawdown: float = 0.50,
        min_trades: int = 5,
        min_profit_factor: float = 0.5,
    ) -> Tuple[bool, List[str]]:
        """
        Check if metrics pass certification thresholds.

        Returns:
            (pass, list_of_failures)
        """
        failures = []
        if self.total_trades < min_trades:
            failures.append(f"trades={self.total_trades} < {min_trades}")
        if self.sharpe_ratio < min_sharpe:
            failures.append(f"sharpe={self.sharpe_ratio:.2f} < {min_sharpe}")
        if self.max_drawdown > max_drawdown:
            failures.append(f"max_dd={self.max_drawdown:.2%} > {max_drawdown:.2%}")
        if self.profit_factor < min_profit_factor:
            failures.append(f"profit_factor={self.profit_factor:.2f} < {min_profit_factor}")
        return len(failures) == 0, failures


def compute_metrics(
    trades: List[TradeRecord],
    equity_curve: List[Tuple[int, Decimal]],
    initial_equity: Decimal,
    periods_per_year: int = 252,
) -> PerformanceMetrics:
    """
    Compute full performance metrics from trades and equity curve.

    Args:
        trades: list of TradeRecord
        equity_curve: list of (timestamp_ms, equity) tuples
        initial_equity: starting equity
    """
    total = len(trades)
    winners = [t for t in trades if t.pnl > ZERO]
    losers = [t for t in trades if t.pnl <= ZERO]

    total_pnl = sum((t.pnl for t in trades), ZERO)
    gross_profit = sum((t.pnl for t in winners), ZERO)
    gross_loss = abs(sum((t.pnl for t in losers), ZERO))

    win_rate = len(winners) / total if total > 0 else 0.0
    profit_factor = (
        float(gross_profit / gross_loss) if gross_loss > ZERO else float("inf")
    )
    avg_pnl = total_pnl / Decimal(str(total)) if total > 0 else ZERO
    avg_win = gross_profit / Decimal(str(len(winners))) if winners else ZERO
    avg_loss = (
        -gross_loss / Decimal(str(len(losers))) if losers else ZERO
    )
    avg_duration = (
        sum(t.duration_ms for t in trades) // total if total > 0 else 0
    )

    final_equity = equity_curve[-1][1] if equity_curve else initial_equity
    return_pct = (
        float((final_equity - initial_equity) / initial_equity)
        if initial_equity > ZERO
        else 0.0
    )

    max_dd = _max_drawdown(equity_curve)
    sharpe = _sharpe_ratio(equity_curve, periods_per_year=periods_per_year)

    # Extended metrics (compute from returns series)
    returns = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1][1])
        curr = float(equity_curve[i][1])
        if prev > 0:
            returns.append((curr - prev) / prev)

    trade_pnls = [float(t.pnl) for t in trades]
    n_bars = len(equity_curve)

    # CAGR
    cagr = 0.0
    if return_pct > -1.0 and n_bars > 1:
        years = n_bars / periods_per_year if periods_per_year > 0 else 1.0
        if years > 0:
            cagr = (1 + return_pct) ** (1 / years) - 1

    # Sortino (downside deviation)
    sortino = 0.0
    if returns:
        mean_r = sum(returns) / len(returns)
        downside = [min(0.0, r - mean_r) ** 2 for r in returns]
        dd_dev = math.sqrt(sum(downside) / max(len(returns) - 1, 1))
        sortino = (mean_r / dd_dev * math.sqrt(periods_per_year)) if dd_dev > 0 else 0.0

    # Calmar
    max_dd_abs = max_dd * float(initial_equity) if initial_equity > ZERO else 0.0
    calmar = 0.0
    if max_dd_abs > 0 and returns:
        ann_ret = (sum(returns) / len(returns)) * periods_per_year
        calmar = ann_ret / max_dd_abs if max_dd_abs > 0 else 0.0

    # Max consecutive losses
    max_consec = 0
    consec = 0
    for pnl in trade_pnls:
        if pnl < 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    # Recovery factor
    recovery = float(total_pnl) / max_dd_abs if max_dd_abs > 0 else 0.0

    # Skewness/kurtosis
    from src.quantitative.advanced import _skewness, _kurtosis
    skew = _skewness(returns) if len(returns) >= 3 else 0.0
    kurt = _kurtosis(returns) if len(returns) >= 4 else 0.0

    # Time in market / turnover (estimated from trades vs bars)
    time_pct = min(1.0, total * 2 / n_bars) if n_bars > 0 else 0.0  # rough: each trade covers ~2 bars
    turnover = total / n_bars if n_bars > 0 else 0.0

    return PerformanceMetrics(
        total_trades=total,
        winning_trades=len(winners),
        losing_trades=len(losers),
        win_rate=win_rate,
        total_pnl=total_pnl,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        avg_trade_pnl=avg_pnl,
        avg_win=avg_win,
        avg_loss=avg_loss,
        avg_trade_duration_ms=avg_duration,
        return_pct=return_pct,
        cagr=cagr,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_consecutive_losses=max_consec,
        recovery_factor=recovery,
        time_in_market_pct=time_pct,
        turnover_rate=turnover,
        skewness=skew,
        kurtosis=kurt,
    )


def _max_drawdown(equity_curve: List[Tuple[int, Decimal]]) -> float:
    if not equity_curve:
        return 0.0
    peak = ZERO
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > ZERO:
            dd = float((peak - eq) / peak)
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _sharpe_ratio(
    equity_curve: List[Tuple[int, Decimal]],
    periods_per_year: int = 252,
) -> float:
    if len(equity_curve) < 2:
        return 0.0
    returns = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1][1])
        curr = float(equity_curve[i][1])
        if prev > 0:
            returns.append((curr - prev) / prev)
    if not returns or len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean_r / std) * math.sqrt(periods_per_year)
