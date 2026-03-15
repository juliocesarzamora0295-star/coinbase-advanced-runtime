"""
Estrategia SMA Crossover para Fortress v4.

Adaptada de GuardianBot con mejoras:
- Uso de Decimal para precisión
- Integración con Signal base
- Validación de datos de mercado
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Any

import pandas as pd

from src.strategy.base import Strategy, Signal

logger = logging.getLogger("SmaCrossoverStrategy")


class SmaCrossoverStrategy(Strategy):
    """
    Estrategia de cruce de medias móviles simples (SMA).
    
    Genera señales de compra cuando SMA rápida cruza por encima de SMA lenta.
    Genera señales de venta cuando SMA rápida cruza por debajo de SMA lenta.
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)
        
        # Períodos de SMA
        self.fast = int(self.config.get("sma_fast", 20))
        self.slow = int(self.config.get("sma_slow", 50))
        
        # Asegurar que fast < slow
        if self.fast >= self.slow:
            self.fast = max(2, self.slow // 2)
            logger.warning(f"Adjusted fast SMA to {self.fast} (must be < slow={self.slow})")
        
        # Estado interno
        self._last_signal_side: Optional[str] = None
        self.positions: Dict[str, Dict[str, Decimal]] = {}

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        """Actualizar datos de mercado."""
        if market_data is None or len(market_data) < (self.slow + 2):
            logger.warning(f"Insufficient data: {len(market_data) if market_data is not None else 0} < {self.slow + 2}")
            return
        
        if not hasattr(market_data, "iloc") or "close" not in market_data.columns:
            raise RuntimeError("market_data must be DataFrame with 'close' column")
        
        self._df = market_data

    def generate_signals(self, *, mid: Decimal) -> List[Signal]:
        """
        Generar señales basadas en cruce de SMAs.
        
        Args:
            mid: Precio medio actual
            
        Returns:
            Lista de señales (puede estar vacía)
        """
        if self._df is None or len(self._df) < (self.slow + 2):
            return []

        close = self._df["close"].astype(float)
        fast_sma = close.rolling(self.fast).mean()
        slow_sma = close.rolling(self.slow).mean()

        # Valores anteriores y actuales
        f_prev, f_now = float(fast_sma.iloc[-2]), float(fast_sma.iloc[-1])
        s_prev, s_now = float(slow_sma.iloc[-2]), float(slow_sma.iloc[-1])

        # Tamaño base de orden
        base_amount = Decimal(str(self.config.get("base_order_size", "0.001")))
        
        signals: List[Signal] = []

        # Cruce alcista: fast cruza por encima de slow
        if f_prev <= s_prev and f_now > s_now:
            if self._last_signal_side != "buy":
                signals.append(Signal(
                    symbol=self.symbol,
                    side="buy",
                    position_side="LONG",
                    order_type="limit",
                    amount=base_amount,
                    price=mid,
                    reduce_only=False,
                    reason=f"SMA crossover UP ({self.fast}/{self.slow})",
                ))
                self._last_signal_side = "buy"
                logger.info(f"BUY signal: SMA{self.fast} crossed above SMA{self.slow}")

        # Cruce bajista: fast cruza por debajo de slow
        if f_prev >= s_prev and f_now < s_now:
            if self._last_signal_side != "sell":
                signals.append(Signal(
                    symbol=self.symbol,
                    side="sell",
                    position_side="SHORT",
                    order_type="limit",
                    amount=base_amount,
                    price=mid,
                    reduce_only=False,
                    reason=f"SMA crossover DOWN ({self.fast}/{self.slow})",
                ))
                self._last_signal_side = "sell"
                logger.info(f"SELL signal: SMA{self.fast} crossed below SMA{self.slow}")

        return signals

    def update_positions(self, fill: Dict[str, Any]) -> None:
        """Actualizar estado de posiciones después de un fill."""
        sym = fill.get("symbol")
        ps = fill.get("position_side")
        amt = Decimal(str(fill.get("amount") or "0"))
        reduce_only = bool(fill.get("reduce_only", False))

        if not sym or ps not in ("LONG", "SHORT") or amt <= 0:
            return

        if sym not in self.positions:
            self.positions[sym] = {"LONG": Decimal("0"), "SHORT": Decimal("0")}

        if reduce_only:
            self.positions[sym][ps] = max(Decimal("0"), self.positions[sym][ps] - amt)
        else:
            self.positions[sym][ps] += amt

    def get_position(self, symbol: str, position_side: str) -> Decimal:
        """Obtener tamaño de posición."""
        return self.positions.get(symbol, {}).get(position_side, Decimal("0"))
