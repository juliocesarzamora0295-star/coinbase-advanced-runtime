"""
Estrategia SMA Crossover para Fortress v4.

Adaptada de GuardianBot con mejoras:
- Uso de Decimal para precisión
- Integración con Signal base
- Validación de datos de mercado
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.quantitative.indicators import atr
from src.strategy.base import Strategy
from src.strategy.signal import make_signal
from src.strategy.trailing_stop import TrailingStop

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

        # Trailing stop (Fase 1A — fail-closed on invalid ATR, invariant I6)
        self._atr_period = int(self.config.get("atr_period", 14))
        self._trailing = TrailingStop(
            atr_mult=float(self.config.get("stop_loss_atr_mult", 2.0))
        )

        # Estado interno
        self._last_signal_side: Optional[str] = None
        self.positions: Dict[str, Dict[str, Decimal]] = {}

    @property
    def name(self) -> str:
        return f"sma_crossover_{self.fast}_{self.slow}"

    @property
    def warmup_bars(self) -> int:
        return self.slow + 2

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        """Actualizar datos de mercado."""
        if market_data is None or len(market_data) < (self.slow + 2):
            logger.debug(
                f"Insufficient data: {len(market_data) if market_data is not None else 0} < {self.slow + 2}"
            )
            return

        if not hasattr(market_data, "iloc") or "close" not in market_data.columns:
            raise RuntimeError("market_data must be DataFrame with 'close' column")

        self._df = market_data

    def _current_atr(self) -> Optional[float]:
        """
        Compute current ATR from the stored DataFrame.

        - Falls back to close-only pseudo-ATR when high/low columns are not
          present (e.g., close-only test fixtures).
        - When the DataFrame is shorter than the configured atr_period, the
          effective period is clamped to `len(df) - 1` (minimum 2), so small
          fixtures still yield a valid ATR.
        - Returns None when ATR is not computable so the caller can fail-closed.
        """
        if self._df is None or len(self._df) < 3:
            return None

        effective_period = max(2, min(self._atr_period, len(self._df) - 1))

        close_s = self._df["close"].astype(float)
        if "high" in self._df.columns and "low" in self._df.columns:
            high_s = self._df["high"].astype(float)
            low_s = self._df["low"].astype(float)
        else:
            # Close-only fallback: degenerate OHLC → TR = |Δclose|.
            high_s = close_s
            low_s = close_s

        atr_series = atr(high_s, low_s, close_s, period=effective_period)
        value = float(atr_series.iloc[-1])
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        """
        Generar señales basadas en cruce de SMAs + trailing stop.

        Emisión asimétrica:
        - BUY en cruce alcista, condicionado a activación exitosa del trailing stop
          (fail-closed: si ATR es inválido, NO se emite BUY — invariante I6).
        - SELL en cruce bajista O cuando el trailing stop es golpeado.

        Returns:
            Lista de src.strategy.signal.Signal (puede estar vacía).
        """
        if self._df is None or len(self._df) < (self.slow + 2):
            return []

        close = self._df["close"].astype(float)
        fast_sma = close.rolling(self.fast).mean()
        slow_sma = close.rolling(self.slow).mean()

        f_prev, f_now = float(fast_sma.iloc[-2]), float(fast_sma.iloc[-1])
        s_prev, s_now = float(slow_sma.iloc[-2]), float(slow_sma.iloc[-1])
        current_close = float(close.iloc[-1])
        current_atr = self._current_atr()

        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = f"sma_crossover_{self.fast}_{self.slow}"
        signals: List = []

        bullish_cross = f_prev <= s_prev and f_now > s_now
        bearish_cross = f_prev >= s_prev and f_now < s_now

        # Trailing-stop check on open LONG, BEFORE considering new entries.
        # If a cross-down happens on the same bar the stop fires, we emit
        # only one SELL (whichever triggers first) to avoid double-signals.
        if self._last_signal_side == "buy" and self._trailing.is_active:
            if self._trailing.update(current_price=current_close, current_atr=current_atr):
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="SELL",
                        strength=Decimal("1.0"),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": "trailing_stop_hit",
                            "stop_price": self._trailing.stop_price,
                        },
                    )
                )
                self._last_signal_side = "sell"
                self._trailing.reset()
                logger.info(
                    "SELL signal: SMA trailing stop hit at close=%.2f (stop was %.2f)",
                    current_close,
                    signals[-1].metadata.get("stop_price") or 0.0,
                )
                return signals

        # Cruce alcista: fast cruza por encima de slow.
        if bullish_cross and self._last_signal_side != "buy":
            if not self._trailing.activate(
                entry_price=current_close,
                current_atr=current_atr,
                direction="LONG",
            ):
                logger.warning(
                    "BUY suppressed: invalid ATR (%s) — fail-closed, no entry",
                    current_atr,
                )
            else:
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="BUY",
                        strength=Decimal("1.0"),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": f"SMA crossover UP ({self.fast}/{self.slow})",
                            "stop_price": self._trailing.stop_price,
                        },
                    )
                )
                self._last_signal_side = "buy"
                logger.info(
                    "BUY signal: SMA%d crossed above SMA%d (stop=%.2f)",
                    self.fast,
                    self.slow,
                    self._trailing.stop_price or 0.0,
                )

        # Cruce bajista: fast cruza por debajo de slow.
        if bearish_cross and self._last_signal_side != "sell":
            signals.append(
                make_signal(
                    symbol=self.symbol,
                    direction="SELL",
                    strength=Decimal("1.0"),
                    strategy_id=strategy_id,
                    bar_timestamp=bt,
                    metadata={"reason": f"SMA crossover DOWN ({self.fast}/{self.slow})"},
                )
            )
            self._last_signal_side = "sell"
            self._trailing.reset()
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
