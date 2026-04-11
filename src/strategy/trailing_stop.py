"""
TrailingStop — shared component for ATR-based stop-loss across strategies.

Fail-closed semantics (invariant I6):
- Refuses to activate when ATR is None, NaN, zero, or negative.
- Caller MUST check the return value of activate() and NOT enter the position
  on False — the stop-loss is the risk floor; without it, there is no floor.

Usage pattern (long):
    ts = TrailingStop(atr_mult=2.0)
    if not ts.activate(entry_price=close, current_atr=atr, direction="LONG"):
        # ATR invalid — fail-closed: DO NOT enter.
        return
    # Each bar thereafter:
    if ts.update(current_price=close, current_atr=atr):
        # Stop hit — close position.
        ts.reset()

The component is indicator-agnostic: it does not compute ATR itself.
Strategies compute ATR once per bar and pass the scalar in.
"""

from __future__ import annotations

import math
from typing import Optional


def _is_valid_atr(value: Optional[float]) -> bool:
    """ATR valid iff numeric, finite, strictly positive."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v) or math.isinf(v):
        return False
    return v > 0.0


class TrailingStop:
    """
    ATR-multiple trailing stop.

    State machine:
      inactive -> activate() -> active -> update() -> (active | hit) -> reset() -> inactive

    `stop_price` only moves in the favorable direction:
      LONG:  stop can only increase (trail up)
      SHORT: stop can only decrease (trail down)

    If ATR becomes invalid (NaN, 0, etc.) mid-position, `update()` does NOT move
    the stop but still checks whether current price hit the existing level. The
    position is already open, so we cannot refuse it — we protect what we have.
    """

    def __init__(self, atr_mult: float) -> None:
        if atr_mult <= 0:
            raise ValueError(f"atr_mult must be > 0, got {atr_mult}")
        self._atr_mult = float(atr_mult)
        self._active = False
        self._direction: Optional[str] = None  # "LONG" | "SHORT"
        self._stop_price: Optional[float] = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def stop_price(self) -> Optional[float]:
        return self._stop_price

    @property
    def direction(self) -> Optional[str]:
        return self._direction

    def can_activate(self, current_atr: Optional[float]) -> bool:
        """True iff ATR is valid for activation. Fail-closed on invalid input."""
        return _is_valid_atr(current_atr)

    def activate(
        self,
        entry_price: float,
        current_atr: Optional[float],
        direction: str,
    ) -> bool:
        """
        Initialize the trailing stop at entry.

        Returns:
            True if activated. False if ATR invalid — caller MUST NOT enter.
        """
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")
        if not _is_valid_atr(current_atr):
            # Fail-closed: do not activate, do not mutate state.
            return False

        entry = float(entry_price)
        atr = float(current_atr)  # type: ignore[arg-type]
        offset = self._atr_mult * atr

        if direction == "LONG":
            self._stop_price = entry - offset
        else:  # SHORT
            self._stop_price = entry + offset

        self._active = True
        self._direction = direction
        return True

    def update(
        self,
        current_price: float,
        current_atr: Optional[float],
    ) -> bool:
        """
        Advance the trailing stop and check for a hit.

        Returns:
            True if current_price has hit or breached the stop (position should close).
            False otherwise.

        Semantics:
        - If ATR is invalid, the stop level is NOT updated (frozen at last known good level)
          but we still check whether price has hit it. Position is open; we cannot refuse.
        - If ATR is valid, the stop trails in the favorable direction only.
        """
        if not self._active or self._stop_price is None or self._direction is None:
            return False

        price = float(current_price)

        # Advance stop only when ATR is valid.
        if _is_valid_atr(current_atr):
            atr = float(current_atr)  # type: ignore[arg-type]
            offset = self._atr_mult * atr
            if self._direction == "LONG":
                candidate = price - offset
                if candidate > self._stop_price:
                    self._stop_price = candidate
            else:  # SHORT
                candidate = price + offset
                if candidate < self._stop_price:
                    self._stop_price = candidate

        # Check hit — use >= / <= so exact touches trigger.
        if self._direction == "LONG":
            return price <= self._stop_price
        else:
            return price >= self._stop_price

    def reset(self) -> None:
        """Clear state so a fresh entry can use a new stop."""
        self._active = False
        self._direction = None
        self._stop_price = None
