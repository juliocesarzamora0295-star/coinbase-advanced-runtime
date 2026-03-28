"""
Clase base para estrategias de trading.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Any

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

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        """Actualizar datos de mercado."""
        self._df = market_data

    @abstractmethod
    def generate_signals(self, *, mid: Decimal, bar_timestamp=None) -> List:
        """Generar señales de trading. Retorna List[src.strategy.signal.Signal]."""
        pass

    def update_positions(self, fill: Dict[str, Any]) -> None:
        """Actualizar estado de posiciones después de un fill."""
        pass
