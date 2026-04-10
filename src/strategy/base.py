"""
Clase base para estrategias de trading.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class Signal:
    """
    Señal legacy — mantenida para compatibilidad con tests de StrategyManager.
    Las estrategias de producción deben emitir src.strategy.signal.Signal.
    """

    symbol: str
    side: str  # "buy" | "sell"
    position_side: str  # "LONG" | "SHORT"
    order_type: str  # "market" | "limit"
    amount: Decimal
    price: Decimal
    reduce_only: bool = False
    reason: str = ""


class Strategy(ABC):
    """Clase base para estrategias de trading."""

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        self.symbol = symbol
        self.config = config or {}
        self._df: Optional[pd.DataFrame] = None

    @property
    def name(self) -> str:
        """Human-readable strategy name. Override in subclasses."""
        return type(self).__name__

    @property
    def warmup_bars(self) -> int:
        """Minimum number of bars needed before signal generation.

        Override in subclasses to declare warmup requirements.
        Default: 0 (no warmup needed).
        """
        return 0

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        """Actualizar datos de mercado."""
        self._df = market_data

    @abstractmethod
    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        """Generar señales de trading. Retorna List[src.strategy.signal.Signal]."""
        pass

    def on_candle(self, symbol: str, candle: pd.Series) -> Optional[Any]:
        """Process a closed candle and optionally return a Signal.

        This is a convenience method that combines update_market_data +
        generate_signals into a single call. Strategies can override this
        for a cleaner interface.

        Default implementation delegates to generate_signals() via
        StrategyManager's existing pipeline — this is a no-op here.
        """
        return None

    def on_tick(self, symbol: str, price: Decimal) -> None:
        """Process a real-time price tick.

        Override in subclasses that need tick-level data (e.g., for
        trailing stops or tick-based entries). Default: no-op.
        """
        pass

    def update_positions(self, fill: Dict[str, Any]) -> None:
        """Actualizar estado de posiciones después de un fill."""
        pass
