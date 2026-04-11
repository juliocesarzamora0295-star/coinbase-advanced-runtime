"""
Mean Reversion Strategy — Bollinger Bands + RSI.

Designed for RANGING markets (sideways, low-to-medium volatility).

Logic:
- BUY when price touches lower Bollinger Band AND RSI < oversold threshold.
- SELL when price touches upper Bollinger Band AND RSI > overbought threshold.

This strategy LOSES money in trending markets. Only use when regime
detector classifies market as RANGING.

Implements Strategy base class for integration with StrategyManager.
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.quantitative.indicators import atr, bollinger_bands, rsi
from src.strategy.base import Strategy
from src.strategy.signal import make_signal
from src.strategy.trailing_stop import TrailingStop

logger = logging.getLogger("MeanReversionStrategy")


class MeanReversionStrategy(Strategy):
    """
    Bollinger Band + RSI mean reversion.

    Parameters (via config):
    - bb_period: Bollinger Band period (default 20)
    - bb_std: Number of std deviations (default 2.0)
    - rsi_period: RSI period (default 14)
    - rsi_oversold: RSI buy threshold (default 30)
    - rsi_overbought: RSI sell threshold (default 70)
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

        self.bb_period = int(self.config.get("bb_period", 20))
        self.bb_std = float(self.config.get("bb_std", 2.0))
        self.rsi_period = int(self.config.get("rsi_period", 14))
        self.rsi_oversold = float(self.config.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.config.get("rsi_overbought", 70))

        # Fixed stop-loss (NOT trailing): mean reversion waits for the rebound,
        # so moving the stop in favor would close winners prematurely. We reuse
        # TrailingStop only to hold the immutable level and to benefit from the
        # fail-closed activation contract. Default multiplier laxer than SMA:
        # it's tail-protection, not an operative exit.
        self._atr_period = int(self.config.get("atr_period", 14))
        self._stop = TrailingStop(
            atr_mult=float(self.config.get("stop_loss_atr_mult", 2.5))
        )

        self._last_signal_side: Optional[str] = None
        self.positions: Dict[str, Dict[str, Decimal]] = {}

    @property
    def min_bars(self) -> int:
        return max(self.bb_period, self.rsi_period) + 5

    def _current_atr(self) -> Optional[float]:
        """
        Compute current ATR from the stored DataFrame, with graceful fallback.

        See SmaCrossoverStrategy._current_atr for the same pattern:
        close-only fallback when high/low columns are missing, and period
        clamping when the DataFrame is shorter than the configured period.
        """
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
        """
        Generate mean reversion signals with a FIXED (non-trailing) stop-loss.

        BUY when: close < lower Bollinger AND RSI < oversold — gated by successful
        stop activation (fail-closed on invalid ATR, invariant I6).
        SELL when: close > upper Bollinger AND RSI > overbought — OR stop hit.

        The stop is fixed at entry, not trailed: mean reversion trades expect a
        rebound, and trailing in favor would close winners prematurely.
        """
        if self._df is None or len(self._df) < self.min_bars:
            return []

        close = self._df["close"].astype(float)
        bb = bollinger_bands(close, self.bb_period, self.bb_std)
        rsi_vals = rsi(close, self.rsi_period)

        current_close = float(close.iloc[-1])
        current_rsi = float(rsi_vals.iloc[-1])
        current_lower = float(bb.lower.iloc[-1])
        current_upper = float(bb.upper.iloc[-1])

        if pd.isna(current_rsi) or pd.isna(current_lower):
            return []

        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = f"mean_reversion_bb{self.bb_period}_rsi{self.rsi_period}"
        signals: List = []
        current_atr = self._current_atr()

        # Fixed stop-loss check on open LONG, BEFORE new entries.
        # NOTE: we pass current_atr=None on update() so the stop NEVER moves.
        # The TrailingStop component preserves the level on invalid ATR by
        # design (see trailing_stop.py). Hit check still runs.
        if self._last_signal_side == "buy" and self._stop.is_active:
            if self._stop.update(current_price=current_close, current_atr=None):
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="SELL",
                        strength=Decimal("1.0"),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": "stop_loss_hit",
                            "stop_price": self._stop.stop_price,
                        },
                    )
                )
                self._last_signal_side = "sell"
                self._stop.reset()
                logger.info(
                    "SELL: mean-reversion stop-loss hit close=%.2f stop=%.2f",
                    current_close,
                    signals[-1].metadata.get("stop_price") or 0.0,
                )
                return signals

        # BUY signal: price at/below lower band + RSI oversold
        if current_close <= current_lower and current_rsi < self.rsi_oversold:
            if self._last_signal_side != "buy":
                if not self._stop.activate(
                    entry_price=current_close,
                    current_atr=current_atr,
                    direction="LONG",
                ):
                    logger.warning(
                        "BUY suppressed: invalid ATR (%s) — fail-closed, no entry",
                        current_atr,
                    )
                else:
                    # Strength proportional to how oversold
                    strength = min(Decimal("1.0"), Decimal(str(
                        (self.rsi_oversold - current_rsi) / self.rsi_oversold
                    )))
                    signals.append(
                        make_signal(
                            symbol=self.symbol,
                            direction="BUY",
                            strength=max(Decimal("0.1"), strength),
                            strategy_id=strategy_id,
                            bar_timestamp=bt,
                            metadata={
                                "reason": f"Mean reversion BUY: close={current_close:.2f} "
                                f"<= BB_lower={current_lower:.2f}, RSI={current_rsi:.1f}",
                                "stop_price": self._stop.stop_price,
                            },
                        )
                    )
                    self._last_signal_side = "buy"
                    logger.info(
                        "BUY: close=%.2f <= BB_lower=%.2f, RSI=%.1f, stop=%.2f",
                        current_close,
                        current_lower,
                        current_rsi,
                        self._stop.stop_price or 0.0,
                    )

        # SELL signal: price at/above upper band + RSI overbought
        if current_close >= current_upper and current_rsi > self.rsi_overbought:
            if self._last_signal_side != "sell":
                strength = min(Decimal("1.0"), Decimal(str(
                    (current_rsi - self.rsi_overbought) / (100 - self.rsi_overbought)
                )))
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="SELL",
                        strength=max(Decimal("0.1"), strength),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": f"Mean reversion SELL: close={current_close:.2f} "
                            f">= BB_upper={current_upper:.2f}, RSI={current_rsi:.1f}",
                        },
                    )
                )
                self._last_signal_side = "sell"
                self._stop.reset()
                logger.info(
                    "SELL: close=%.2f >= BB_upper=%.2f, RSI=%.1f",
                    current_close,
                    current_upper,
                    current_rsi,
                )

        return signals

    def update_positions(self, fill: Dict[str, Any]) -> None:
        """Update position state after fill."""
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
