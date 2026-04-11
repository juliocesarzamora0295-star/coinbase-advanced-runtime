"""
MACD Histogram Strategy — trend-following with divergence boost (Fase 3A).

BUY when:
  - MACD histogram flips from negative to positive (cross up).
  - macd_line > signal_line (confirmation).
  - close > sma(trend_period) (trend filter — no counter-trend BUYs).

SELL when:
  - Histogram flips positive to negative, OR
  - Trailing ATR stop hit.

Bullish divergence (price lower-low + histogram higher-low) boosts BUY
strength to 1.0; a plain crossover ships with strength 0.7.

Fail-closed on invalid ATR (invariant I6): BUY suppressed if ATR is None,
NaN, zero, or negative — no position opens without a stop.
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.quantitative.indicators import atr, macd, sma
from src.strategy.base import Strategy
from src.strategy.signal import make_signal
from src.strategy.trailing_stop import TrailingStop

logger = logging.getLogger("MacdStrategy")


class MacdStrategy(Strategy):
    """
    MACD histogram crossover + trend filter + trailing stop.

    Config:
    - macd_fast (int, default 12)
    - macd_slow (int, default 26)
    - macd_signal (int, default 9)
    - trend_sma_period (int, default 50)
    - stop_loss_atr_mult (float, default 2.0)
    - atr_period (int, default 14)
    - divergence_lookback (int, default 20)
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

        self.macd_fast = int(self.config.get("macd_fast", 12))
        self.macd_slow = int(self.config.get("macd_slow", 26))
        self.macd_signal = int(self.config.get("macd_signal", 9))
        self.trend_sma_period = int(self.config.get("trend_sma_period", 50))
        self.divergence_lookback = int(self.config.get("divergence_lookback", 20))

        self._atr_period = int(self.config.get("atr_period", 14))
        self._trailing = TrailingStop(
            atr_mult=float(self.config.get("stop_loss_atr_mult", 2.0))
        )

        self._last_signal_side: Optional[str] = None

    @property
    def name(self) -> str:
        return f"macd_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}"

    @property
    def warmup_bars(self) -> int:
        return max(self.macd_slow, self.trend_sma_period) + 5

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

    def _detect_bullish_divergence(
        self,
        close_series: Any,
        hist_series: Any,
    ) -> bool:
        """
        Bullish regular divergence: price makes a lower low, histogram makes
        a higher low, within the lookback window. Simple two-pivot check.
        """
        n = len(close_series)
        lb = min(self.divergence_lookback, n - 1)
        if lb < 5:
            return False
        window_close = close_series.iloc[-lb:]
        window_hist = hist_series.iloc[-lb:]

        # Find the two lowest close bars in the window.
        close_idx_sorted = window_close.sort_values().index.tolist()
        if len(close_idx_sorted) < 2:
            return False
        low1_idx = close_idx_sorted[0]
        # Pick the second lowest that is at least 2 bars away from the first.
        low2_idx = None
        for idx in close_idx_sorted[1:]:
            if abs(int(idx) - int(low1_idx)) >= 2:
                low2_idx = idx
                break
        if low2_idx is None:
            return False
        # Order chronologically: earlier, later.
        earlier, later = sorted([int(low1_idx), int(low2_idx)])
        price_lower_low = float(close_series.iloc[later]) < float(close_series.iloc[earlier])
        hist_earlier = float(hist_series.iloc[earlier])
        hist_later = float(hist_series.iloc[later])
        if math.isnan(hist_earlier) or math.isnan(hist_later):
            return False
        hist_higher_low = hist_later > hist_earlier
        return price_lower_low and hist_higher_low

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        if self._df is None or len(self._df) < self.warmup_bars:
            return []

        close = self._df["close"].astype(float)
        macd_res = macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
        trend_sma = sma(close, self.trend_sma_period)

        hist_prev = float(macd_res.histogram.iloc[-2])
        hist_now = float(macd_res.histogram.iloc[-1])
        macd_line_now = float(macd_res.macd_line.iloc[-1])
        signal_line_now = float(macd_res.signal_line.iloc[-1])
        current_close = float(close.iloc[-1])
        trend_now = float(trend_sma.iloc[-1])

        if any(
            math.isnan(v)
            for v in (hist_prev, hist_now, macd_line_now, signal_line_now, trend_now)
        ):
            return []

        current_atr = self._current_atr()
        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = self.name
        signals: List = []

        bullish_cross = hist_prev <= 0.0 and hist_now > 0.0
        bearish_cross = hist_prev >= 0.0 and hist_now < 0.0

        # Trailing stop check first.
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
                    "SELL: MACD trailing stop hit close=%.2f stop=%.2f",
                    current_close,
                    signals[-1].metadata.get("stop_price") or 0.0,
                )
                return signals

        if bullish_cross and self._last_signal_side != "buy":
            trend_ok = current_close > trend_now
            confirmation = macd_line_now > signal_line_now
            if trend_ok and confirmation:
                if not self._trailing.activate(
                    entry_price=current_close,
                    current_atr=current_atr,
                    direction="LONG",
                ):
                    logger.warning(
                        "BUY suppressed: invalid ATR (%s) — fail-closed", current_atr
                    )
                else:
                    has_div = self._detect_bullish_divergence(close, macd_res.histogram)
                    strength = Decimal("1.0") if has_div else Decimal("0.7")
                    signals.append(
                        make_signal(
                            symbol=self.symbol,
                            direction="BUY",
                            strength=strength,
                            strategy_id=strategy_id,
                            bar_timestamp=bt,
                            metadata={
                                "reason": "MACD histogram cross up"
                                + (" + bullish divergence" if has_div else ""),
                                "stop_price": self._trailing.stop_price,
                                "divergence": has_div,
                            },
                        )
                    )
                    self._last_signal_side = "buy"
                    logger.info(
                        "BUY: MACD cross up close=%.2f trend_sma=%.2f div=%s stop=%.2f",
                        current_close,
                        trend_now,
                        has_div,
                        self._trailing.stop_price or 0.0,
                    )

        if bearish_cross and self._last_signal_side != "sell":
            signals.append(
                make_signal(
                    symbol=self.symbol,
                    direction="SELL",
                    strength=Decimal("1.0"),
                    strategy_id=strategy_id,
                    bar_timestamp=bt,
                    metadata={"reason": "MACD histogram cross down"},
                )
            )
            self._last_signal_side = "sell"
            self._trailing.reset()
            logger.info("SELL: MACD histogram cross down close=%.2f", current_close)

        return signals
