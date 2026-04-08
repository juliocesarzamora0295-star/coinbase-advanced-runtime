"""
Momentum Breakout Strategy — Donchian Channel + Volume Confirmation.

Designed for TRENDING VOLATILE markets (strong moves with high ATR).

Logic:
- BUY when price breaks above Donchian upper channel AND volume > avg volume.
- SELL when price breaks below Donchian lower channel OR trailing stop hit.

Uses ATR-based trailing stop for exits.

This strategy generates WHIPSAW losses in ranging markets. Only use when
regime detector classifies market as TRENDING_VOLATILE.

Implements Strategy base class for integration with StrategyManager.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.quantitative.indicators import atr, donchian_channel, sma
from src.strategy.base import Strategy
from src.strategy.signal import make_signal

logger = logging.getLogger("MomentumBreakoutStrategy")


class MomentumBreakoutStrategy(Strategy):
    """
    Donchian breakout with volume filter and ATR trailing stop.

    Parameters (via config):
    - donchian_period: Channel lookback (default 20)
    - volume_ma_period: Volume moving average period (default 20)
    - volume_multiplier: Volume must exceed avg * multiplier (default 1.2)
    - atr_period: ATR period for trailing stop (default 14)
    - atr_stop_multiplier: Trailing stop = entry - ATR * multiplier (default 2.0)
    """

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

        self.donchian_period = int(self.config.get("donchian_period", 20))
        self.volume_ma_period = int(self.config.get("volume_ma_period", 20))
        self.volume_multiplier = float(self.config.get("volume_multiplier", 1.2))
        self.atr_period = int(self.config.get("atr_period", 14))
        self.atr_stop_multiplier = float(self.config.get("atr_stop_multiplier", 2.0))

        self._last_signal_side: Optional[str] = None
        self._entry_price: Optional[float] = None
        self._trailing_stop: Optional[float] = None
        self.positions: Dict[str, Dict[str, Decimal]] = {}

    @property
    def min_bars(self) -> int:
        return max(self.donchian_period, self.volume_ma_period, self.atr_period) + 5

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        """
        Generate momentum breakout signals.

        BUY when: close > Donchian upper (previous bar) AND volume > avg * multiplier
        SELL when: close < Donchian lower OR close < trailing stop
        """
        if self._df is None or len(self._df) < self.min_bars:
            return []

        high = self._df["high"].astype(float)
        low = self._df["low"].astype(float)
        close = self._df["close"].astype(float)
        volume = self._df["volume"].astype(float) if "volume" in self._df.columns else pd.Series(0, index=close.index)

        dc = donchian_channel(high, low, self.donchian_period)
        atr_vals = atr(high, low, close, self.atr_period)
        vol_sma = sma(volume, self.volume_ma_period)

        current_close = float(close.iloc[-1])
        current_volume = float(volume.iloc[-1])
        current_atr = float(atr_vals.iloc[-1])

        # Use previous bar's Donchian to avoid lookahead
        prev_upper = float(dc.upper.iloc[-2]) if len(dc.upper) >= 2 else None
        prev_lower = float(dc.lower.iloc[-2]) if len(dc.lower) >= 2 else None
        avg_volume = float(vol_sma.iloc[-1]) if not pd.isna(vol_sma.iloc[-1]) else 0

        if prev_upper is None or pd.isna(prev_upper) or pd.isna(current_atr):
            return []

        bt = bar_timestamp if bar_timestamp is not None else datetime.now(tz=timezone.utc)
        strategy_id = f"momentum_breakout_dc{self.donchian_period}"
        signals = []

        # Update trailing stop if in position
        if self._entry_price is not None and self._trailing_stop is not None:
            new_stop = current_close - current_atr * self.atr_stop_multiplier
            if new_stop > self._trailing_stop:
                self._trailing_stop = new_stop

        # ── BUY: Donchian breakout + volume confirmation ──
        volume_ok = avg_volume > 0 and current_volume > avg_volume * self.volume_multiplier

        if current_close > prev_upper and volume_ok:
            if self._last_signal_side != "buy":
                signals.append(
                    make_signal(
                        symbol=self.symbol,
                        direction="BUY",
                        strength=Decimal("1.0"),
                        strategy_id=strategy_id,
                        bar_timestamp=bt,
                        metadata={
                            "reason": f"Breakout BUY: close={current_close:.2f} > "
                            f"DC_upper={prev_upper:.2f}, vol={current_volume:.0f} > "
                            f"avg={avg_volume:.0f}",
                        },
                    )
                )
                self._last_signal_side = "buy"
                self._entry_price = current_close
                self._trailing_stop = current_close - current_atr * self.atr_stop_multiplier
                logger.info(
                    "BUY breakout: close=%.2f > DC=%.2f, stop=%.2f",
                    current_close,
                    prev_upper,
                    self._trailing_stop,
                )

        # ── SELL: Donchian breakdown OR trailing stop ──
        sell_reason = None

        if prev_lower is not None and not pd.isna(prev_lower) and current_close < prev_lower:
            sell_reason = f"Breakdown: close={current_close:.2f} < DC_lower={prev_lower:.2f}"

        elif self._trailing_stop is not None and current_close < self._trailing_stop:
            sell_reason = (
                f"Trailing stop: close={current_close:.2f} < stop={self._trailing_stop:.2f}"
            )

        if sell_reason and self._last_signal_side != "sell":
            signals.append(
                make_signal(
                    symbol=self.symbol,
                    direction="SELL",
                    strength=Decimal("1.0"),
                    strategy_id=strategy_id,
                    bar_timestamp=bt,
                    metadata={"reason": sell_reason},
                )
            )
            self._last_signal_side = "sell"
            self._entry_price = None
            self._trailing_stop = None
            logger.info("SELL: %s", sell_reason)

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
