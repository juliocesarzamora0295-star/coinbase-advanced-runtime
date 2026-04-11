"""
RSI Divergence Strategy — counter-trend mean-reversion entries (Fase 3B).

Designed for VOLATILE RANGING regimes where momentum exhaustion shows up as
a divergence between price extremes and RSI extremes.

Bullish regular divergence:
  - Price prints a lower low inside the lookback window.
  - RSI prints a higher low on the matching bars.
  - Current RSI < 40 (still oversold).
  → BUY, strength 0.9.

Bearish regular divergence:
  - Price prints a higher high.
  - RSI prints a lower high.
  - Current RSI > 60.
  → SELL, strength 0.9.

Local extrema are detected via a symmetric ±order window with no scipy
dependency. A point qualifies as a local minimum if it is strictly less
than every neighbor within `order` bars on both sides; maximum is
symmetric.

TrailingStop provides the exit floor with fail-closed ATR (invariant I6).
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.quantitative.indicators import atr, rsi
from src.strategy.base import Strategy
from src.strategy.signal import make_signal
from src.strategy.trailing_stop import TrailingStop

logger = logging.getLogger("RsiDivergenceStrategy")


def _local_minima(series: pd.Series, order: int) -> List[int]:
    """Return indices where series[i] is strictly less than its ±order neighbors."""
    n = len(series)
    out: List[int] = []
    for i in range(order, n - order):
        val = series.iloc[i]
        if math.isnan(val):
            continue
        window = series.iloc[i - order : i + order + 1]
        if val == window.min() and (window == val).sum() == 1:
            out.append(i)
    return out


def _local_maxima(series: pd.Series, order: int) -> List[int]:
    n = len(series)
    out: List[int] = []
    for i in range(order, n - order):
        val = series.iloc[i]
        if math.isnan(val):
            continue
        window = series.iloc[i - order : i + order + 1]
        if val == window.max() and (window == val).sum() == 1:
            out.append(i)
    return out


class RsiDivergenceStrategy(Strategy):
    """
    RSI regular divergence detector.

    Config:
    - rsi_period (int, default 14)
    - rsi_oversold (float, default 40)  # upper bound for bullish div BUY
    - rsi_overbought (float, default 60) # lower bound for bearish div SELL
    - divergence_order (int, default 5)  # ± window for local extrema
    - divergence_lookback (int, default 30)
    - stop_loss_atr_mult (float, default 2.0)
    - atr_period (int, default 14)
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

        self.rsi_period = int(self.config.get("rsi_period", 14))
        self.rsi_oversold = float(self.config.get("rsi_oversold", 40))
        self.rsi_overbought = float(self.config.get("rsi_overbought", 60))
        self.divergence_order = int(self.config.get("divergence_order", 5))
        self.divergence_lookback = int(self.config.get("divergence_lookback", 30))

        self._atr_period = int(self.config.get("atr_period", 14))
        self._trailing = TrailingStop(
            atr_mult=float(self.config.get("stop_loss_atr_mult", 2.0))
        )

        self._last_signal_side: Optional[str] = None

    @property
    def name(self) -> str:
        return f"rsi_divergence_{self.rsi_period}"

    @property
    def warmup_bars(self) -> int:
        return max(self.rsi_period, self.divergence_lookback) + self.divergence_order + 2

    def _current_atr(self) -> Optional[float]:
        if self._df is None or len(self._df) < 3:
            return None
        effective_period = max(2, min(self._atr_period, len(self._df) - 1))
        close_s = self._df["close"].astype(float)
        if "high" in self._df.columns and "low" in self._df.columns:
            high_s = self._df["high"].astype(float)
            low_s = self._df["low"].astype(float)
        else:
            high_s = close_s
            low_s = close_s
        atr_series = atr(high_s, low_s, close_s, period=effective_period)
        value = float(atr_series.iloc[-1])
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def _last_two_pivots(
        self,
        price: pd.Series,
        rsi_s: pd.Series,
        kind: str,
    ) -> Optional[Tuple[int, int]]:
        """
        Return (earlier_idx, later_idx) of the two most recent price pivots
        of the requested `kind` ("min" or "max") inside the lookback window.
        """
        n = len(price)
        lb = min(self.divergence_lookback, n)
        start = n - lb
        price_window = price.iloc[start:]
        pivots = (
            _local_minima(price_window, self.divergence_order)
            if kind == "min"
            else _local_maxima(price_window, self.divergence_order)
        )
        if len(pivots) < 2:
            return None
        earlier, later = pivots[-2] + start, pivots[-1] + start
        return earlier, later

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        if self._df is None or len(self._df) < self.warmup_bars:
            return []

        close = self._df["close"].astype(float)
        rsi_s = rsi(close, self.rsi_period)

        current_close = float(close.iloc[-1])
        current_rsi = float(rsi_s.iloc[-1])
        if math.isnan(current_rsi):
            return []

        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = self.name
        signals: List = []
        current_atr = self._current_atr()

        # Trailing stop check on open LONG.
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
                return signals

        # Bullish divergence → BUY.
        if self._last_signal_side != "buy" and current_rsi < self.rsi_oversold:
            pivots = self._last_two_pivots(close, rsi_s, "min")
            if pivots is not None:
                earlier, later = pivots
                price_lower_low = float(close.iloc[later]) < float(close.iloc[earlier])
                rsi_e = float(rsi_s.iloc[earlier])
                rsi_l = float(rsi_s.iloc[later])
                if not (math.isnan(rsi_e) or math.isnan(rsi_l)):
                    rsi_higher_low = rsi_l > rsi_e
                    if price_lower_low and rsi_higher_low:
                        if not self._trailing.activate(
                            entry_price=current_close,
                            current_atr=current_atr,
                            direction="LONG",
                        ):
                            logger.warning(
                                "BUY suppressed: invalid ATR (%s) — fail-closed",
                                current_atr,
                            )
                        else:
                            signals.append(
                                make_signal(
                                    symbol=self.symbol,
                                    direction="BUY",
                                    strength=Decimal("0.9"),
                                    strategy_id=strategy_id,
                                    bar_timestamp=bt,
                                    metadata={
                                        "reason": "bullish RSI divergence",
                                        "stop_price": self._trailing.stop_price,
                                        "pivot_earlier": earlier,
                                        "pivot_later": later,
                                    },
                                )
                            )
                            self._last_signal_side = "buy"
                            logger.info(
                                "BUY: bullish RSI div close=%.2f rsi=%.1f stop=%.2f",
                                current_close,
                                current_rsi,
                                self._trailing.stop_price or 0.0,
                            )

        # Bearish divergence → SELL (exit LONG).
        if self._last_signal_side != "sell" and current_rsi > self.rsi_overbought:
            pivots = self._last_two_pivots(close, rsi_s, "max")
            if pivots is not None:
                earlier, later = pivots
                price_higher_high = float(close.iloc[later]) > float(close.iloc[earlier])
                rsi_e = float(rsi_s.iloc[earlier])
                rsi_l = float(rsi_s.iloc[later])
                if not (math.isnan(rsi_e) or math.isnan(rsi_l)):
                    rsi_lower_high = rsi_l < rsi_e
                    if price_higher_high and rsi_lower_high:
                        signals.append(
                            make_signal(
                                symbol=self.symbol,
                                direction="SELL",
                                strength=Decimal("0.9"),
                                strategy_id=strategy_id,
                                bar_timestamp=bt,
                                metadata={
                                    "reason": "bearish RSI divergence",
                                    "pivot_earlier": earlier,
                                    "pivot_later": later,
                                },
                            )
                        )
                        self._last_signal_side = "sell"
                        self._trailing.reset()

        return signals
