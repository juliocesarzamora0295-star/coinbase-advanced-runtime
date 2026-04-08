"""
Risk adapter for backtest — wraps RiskGate for use inside BacktestEngine.

Builds RiskSnapshots from ledger state and evaluates each signal through
the real RiskGate before allowing execution.

This ensures backtests respect the same risk rules as live trading.
"""

import logging
from decimal import Decimal

from src.backtest.data_feed import Bar
from src.backtest.ledger import BacktestLedger
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskVerdict

logger = logging.getLogger("BacktestRiskAdapter")


class BacktestRiskAdapter:
    """
    Evaluates backtest signals through the real RiskGate.

    Builds a RiskSnapshot from the current ledger state and bar price,
    then calls RiskGate.evaluate(). If blocked, the signal is dropped
    and the reason is logged.

    Tracks orders_last_minute approximately (resets every 60 bars as proxy).
    """

    def __init__(
        self,
        risk_gate: RiskGate,
        ledger: BacktestLedger,
        symbol: str = "BTC-USD",
    ) -> None:
        self._gate = risk_gate
        self._ledger = ledger
        self._symbol = symbol
        self._orders_this_window: int = 0
        self._window_start_bar: int = 0
        self._bar_count: int = 0
        self.blocked_count: int = 0
        self.blocked_reasons: list[str] = []

    def evaluate(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> RiskVerdict:
        """
        Evaluate a proposed trade through RiskGate.

        Args:
            side: "BUY" or "SELL"
            qty: proposed quantity
            price: current bar close price

        Returns:
            RiskVerdict from the real RiskGate.
        """
        self._bar_count += 1

        # Approximate orders-per-minute window (reset every 60 bars)
        if self._bar_count - self._window_start_bar >= 60:
            self._orders_this_window = 0
            self._window_start_bar = self._bar_count

        equity = self._ledger.get_equity(price)
        position_qty = self._ledger.position_qty

        # Day PnL as fraction of initial cash
        day_pnl_pct = Decimal("0")
        if self._ledger.initial_cash > Decimal("0"):
            day_pnl_pct = (equity - self._ledger.initial_cash) / self._ledger.initial_cash

        drawdown_pct = self._ledger.get_drawdown(price)

        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=day_pnl_pct,
            drawdown_pct=drawdown_pct,
            orders_last_minute=self._orders_this_window,
            symbol=self._symbol,
            side=side.upper(),
            target_qty=qty,
            entry_ref=price,
            breaker_state="CLOSED",
            kill_switch=False,
        )

        verdict = self._gate.evaluate(snapshot)

        if not verdict.allowed:
            self.blocked_count += 1
            self.blocked_reasons.append(verdict.reason)
            logger.info(
                "RiskGate BLOCKED: side=%s qty=%s reason=%s",
                side,
                qty,
                verdict.reason,
            )
        else:
            self._orders_this_window += 1

        return verdict

    @classmethod
    def from_config(
        cls,
        ledger: BacktestLedger,
        symbol: str = "BTC-USD",
        max_position_pct: Decimal = Decimal("0.10"),
        max_notional: Decimal = Decimal("5000"),
        max_daily_loss_pct: Decimal = Decimal("0.03"),
        max_drawdown_pct: Decimal = Decimal("0.10"),
        max_orders_per_minute: int = 5,
    ) -> "BacktestRiskAdapter":
        """Build from config values matching prod_symbols.yaml."""
        limits = RiskLimits(
            max_position_pct=max_position_pct,
            max_notional_per_symbol=max_notional,
            max_orders_per_minute=max_orders_per_minute,
            max_daily_loss_pct=max_daily_loss_pct,
            max_drawdown_pct=max_drawdown_pct,
        )
        gate = RiskGate(limits)
        return cls(risk_gate=gate, ledger=ledger, symbol=symbol)
