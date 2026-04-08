"""
BacktestEngine — event loop secuencial para backtesting.

Procesa barras históricas una por una, llama al callback de estrategia,
ejecuta señales via PaperExecutor, y marca equity en el ledger.

No importa módulos live (WebSocket, Coinbase API, main.py).
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport, build_report

logger = logging.getLogger("BacktestEngine")


@dataclass(frozen=True)
class Signal:
    """Señal de trading del callback de estrategia."""

    side: str  # "BUY" or "SELL"
    qty: Decimal


# Type alias for strategy callback
# Recibe la barra actual y la lista de barras pasadas, retorna Signal o None
StrategyCallback = Callable[[Bar, list[Bar]], Optional[Signal]]


class BacktestEngine:
    """
    Event loop secuencial para backtest.

    Por cada barra:
    1. Llama al strategy callback
    2. Si hay señal, ejecuta via PaperExecutor
    3. Marca equity en el ledger

    Al final, genera BacktestReport.
    """

    def __init__(
        self,
        feed: HistoricalDataFeed,
        ledger: BacktestLedger,
        executor: PaperExecutor,
        strategy: StrategyCallback,
        risk_adapter: Optional[object] = None,
    ) -> None:
        self.feed = feed
        self.ledger = ledger
        self.executor = executor
        self.strategy = strategy
        self.risk_adapter = risk_adapter

    def run(self) -> BacktestReport:
        """
        Ejecutar backtest completo.

        If risk_adapter is set, each signal is evaluated through RiskGate
        before execution. Blocked signals are logged and skipped.

        Returns:
            BacktestReport con métricas finales.
        """
        history: list[Bar] = []
        total_bars = 0
        signals_generated = 0
        signals_blocked = 0
        last_bar: Optional[Bar] = None

        for bar in self.feed:
            total_bars += 1
            last_bar = bar

            # Strategy evaluation
            signal = self.strategy(bar, history)

            if signal is not None:
                signals_generated += 1
                execute = True

                # Risk check if adapter present
                if self.risk_adapter is not None:
                    verdict = self.risk_adapter.evaluate(
                        side=signal.side,
                        qty=signal.qty,
                        price=bar.close,
                    )
                    if not verdict.allowed:
                        execute = False
                        signals_blocked += 1

                if execute:
                    self.executor.execute(
                        side=signal.side,
                        qty=signal.qty,
                        price=bar.close,
                        ts_ms=bar.timestamp_ms,
                    )

            # Mark equity
            self.ledger.mark(bar.close, bar.timestamp_ms)

            # Append to history after evaluation
            history.append(bar)

        if signals_blocked > 0:
            logger.info(
                "Risk gate blocked %d/%d signals",
                signals_blocked,
                signals_generated,
            )

        # Final price for report
        final_price = last_bar.close if last_bar else Decimal("0")

        report = build_report(self.ledger, total_bars, final_price)
        logger.info("Backtest complete: %s", report)
        return report
