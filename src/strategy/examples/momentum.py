"""
Momentum strategy — buy when recent returns are positive, sell when negative.

Uses rate of change (ROC) over a lookback period.
BUY when ROC > threshold, SELL when ROC < -threshold.

Not a production strategy — for backtest framework validation.
"""

from decimal import Decimal
from typing import Optional

from src.backtest.engine import Signal
from src.backtest.data_feed import Bar


def momentum_strategy(
    bar: Bar,
    history: list,
    lookback: int = 20,
    threshold: Decimal = Decimal("0.02"),
    qty: Decimal = Decimal("1"),
    _state: dict | None = None,
) -> Optional[Signal]:
    """
    Momentum strategy based on rate of change.

    BUY when price increased > threshold over lookback bars.
    SELL when price decreased > threshold over lookback bars.
    """
    if _state is None:
        _state = {}

    closes = [b.close for b in history] + [bar.close]
    if len(closes) < lookback + 1:
        return None

    current = closes[-1]
    past = closes[-(lookback + 1)]

    if past <= Decimal("0"):
        return None

    roc = (current - past) / past
    in_position = _state.get("in_position", False)

    if roc > threshold and not in_position:
        _state["in_position"] = True
        return Signal(side="BUY", qty=qty)

    if roc < -threshold and in_position:
        _state["in_position"] = False
        return Signal(side="SELL", qty=qty)

    return None
