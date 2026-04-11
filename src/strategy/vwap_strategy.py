"""
VWAP Reversion Strategy — mean-revert to volume-weighted price (Fase 3C).

Intended for RANGING_CALM regimes: shallow oscillations where price
deviation from VWAP + an RSI extreme flags a reversion entry.

BUY when:
  - close < vwap * (1 - threshold)  (price stretched below VWAP)
  - RSI < rsi_oversold              (momentum confirms exhaustion)

SELL when:
  - close > vwap * (1 + threshold)
  - RSI > rsi_overbought
  - OR trailing stop hit

TrailingStop with fail-closed ATR (invariant I6). Tighter default
atr_mult=1.5 — reversions to VWAP are short moves, not swing trades.
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.quantitative.indicators import atr, rsi, vwap
from src.strategy.base import Strategy
from src.strategy.signal import make_signal
from src.strategy.trailing_stop import TrailingStop

logger = logging.getLogger("VwapStrategy")


class VwapStrategy(Strategy):
    """
    VWAP reversion with RSI confirmation.

    Config:
    - vwap_threshold (float, default 0.005)
    - rsi_period (int, default 14)
    - rsi_oversold (float, default 40)
    - rsi_overbought (float, default 60)
    - stop_loss_atr_mult (float, default 1.5)
    - atr_period (int, default 14)
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

        self.vwap_threshold = float(self.config.get("vwap_threshold", 0.005))
        self.rsi_period = int(self.config.get("rsi_period", 14))
        self.rsi_oversold = float(self.config.get("rsi_oversold", 40))
        self.rsi_overbought = float(self.config.get("rsi_overbought", 60))

        self._atr_period = int(self.config.get("atr_period", 14))
        self._trailing = TrailingStop(
            atr_mult=float(self.config.get("stop_loss_atr_mult", 1.5))
        )

        self._last_signal_side: Optional[str] = None

    @property
    def name(self) -> str:
        return f"vwap_reversion_{self.rsi_period}"

    @property
    def warmup_bars(self) -> int:
        return self.rsi_period + 5

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

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        if self._df is None or len(self._df) < self.warmup_bars:
            return []

        close = self._df["close"].astype(float)
        # VWAP needs high/low/volume. Missing columns → fallback to close-only
        # (degenerate typical = close, still a valid mean price proxy).
        if {"high", "low", "volume"}.issubset(self._df.columns):
            high = self._df["high"].astype(float)
            low = self._df["low"].astype(float)
            volume = self._df["volume"].astype(float)
        else:
            high = close
            low = close
            volume = close * 0.0 + 1.0

        vwap_s = vwap(high, low, close, volume)
        rsi_s = rsi(close, self.rsi_period)

        current_close = float(close.iloc[-1])
        current_vwap = float(vwap_s.iloc[-1])
        current_rsi = float(rsi_s.iloc[-1])
        if math.isnan(current_vwap) or math.isnan(current_rsi):
            return []

        current_atr = self._current_atr()
        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = self.name
        signals: List = []

        lower_band = current_vwap * (1.0 - self.vwap_threshold)
        upper_band = current_vwap * (1.0 + self.vwap_threshold)

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

        if (
            self._last_signal_side != "buy"
            and current_close < lower_band
            and current_rsi < self.rsi_oversold
        ):
            if not self._trailing.activate(
                entry_price=current_close,
                current_atr=current_atr,
                direction="LONG",
            ):
                logger.warning(
                    "BUY suppressed: invalid ATR (%s) — fail-closed", current_atr
                )
            else:
                deviation = (current_vwap - current_close) / current_vwap
                strength = min(Decimal("1.0"), Decimal(str(deviation / self.vwap_threshold)))
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="BUY",
                        strength=max(Decimal("0.3"), strength),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": "VWAP reversion BUY",
                            "vwap": current_vwap,
                            "stop_price": self._trailing.stop_price,
                            "rsi": current_rsi,
                        },
                    )
                )
                self._last_signal_side = "buy"
                logger.info(
                    "BUY: close=%.2f < vwap_lo=%.2f rsi=%.1f stop=%.2f",
                    current_close,
                    lower_band,
                    current_rsi,
                    self._trailing.stop_price or 0.0,
                )

        if (
            self._last_signal_side != "sell"
            and current_close > upper_band
            and current_rsi > self.rsi_overbought
        ):
            signals.append(
                make_signal(
                    symbol=self.symbol,
                    direction="SELL",
                    strength=Decimal("0.8"),
                    strategy_id=strategy_id,
                    bar_timestamp=bt,
                    metadata={
                        "reason": "VWAP reversion SELL",
                        "vwap": current_vwap,
                        "rsi": current_rsi,
                    },
                )
            )
            self._last_signal_side = "sell"
            self._trailing.reset()

        return signals
