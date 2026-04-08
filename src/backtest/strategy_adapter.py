"""
Strategy adapter — bridges production strategies to BacktestEngine.

Wraps SmaCrossoverStrategy (or any Strategy subclass) so it can be used
as a BacktestEngine StrategyCallback.

This ensures backtests use the EXACT same logic as live trading,
not a simplified copy.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.backtest.data_feed import Bar
from src.backtest.engine import Signal as BacktestSignal
from src.strategy.base import Strategy
from src.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger("StrategyAdapter")


class StrategyAdapter:
    """
    Adapts a production Strategy to a BacktestEngine StrategyCallback.

    Accumulates bars into a DataFrame, feeds them to the strategy,
    and translates production Signals into backtest Signals.

    Tracks position state to avoid double-entry.
    """

    def __init__(
        self,
        strategy: Strategy,
        qty: Decimal = Decimal("0.01"),
    ) -> None:
        self._strategy = strategy
        self._qty = qty
        self._in_position = False
        self._df = pd.DataFrame()

    @classmethod
    def from_config(
        cls,
        symbol: str,
        config: Dict[str, Any],
        qty: Decimal = Decimal("0.01"),
    ) -> "StrategyAdapter":
        """
        Build adapter from symbol config dict.

        Config keys used:
        - sma_fast (int, default 20)
        - sma_slow (int, default 50)
        """
        strategy = SmaCrossoverStrategy(symbol=symbol, config=config)
        return cls(strategy=strategy, qty=qty)

    def __call__(
        self,
        bar: Bar,
        history: List[Bar],
    ) -> Optional[BacktestSignal]:
        """
        BacktestEngine StrategyCallback interface.

        Converts bar history to DataFrame, runs production strategy,
        translates output.
        """
        # Build DataFrame from history + current bar
        all_bars = history + [bar]
        rows = [
            {
                "timestamp": b.timestamp_ms,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
            for b in all_bars
        ]
        df = pd.DataFrame(rows)

        # Feed to strategy
        self._strategy.update_market_data(df)

        bar_ts = datetime.fromtimestamp(bar.timestamp_ms / 1000, tz=timezone.utc)

        signals = self._strategy.generate_signals(
            mid=bar.close,
            bar_timestamp=bar_ts,
        )

        if not signals:
            return None

        # Take first signal (production uses first-wins compose mode)
        sig = signals[0]
        direction = sig.direction  # "BUY" or "SELL"

        # Position tracking to avoid double-entry
        if direction == "BUY" and not self._in_position:
            self._in_position = True
            return BacktestSignal(side="BUY", qty=self._qty)

        if direction == "SELL" and self._in_position:
            self._in_position = False
            return BacktestSignal(side="SELL", qty=self._qty)

        return None


class SelectorAdapter:
    """
    Adapts StrategySelector to a BacktestEngine StrategyCallback.

    Uses regime-aware strategy switching. Each bar:
    1. Builds DataFrame from history
    2. Feeds to StrategySelector (which detects regime + picks strategy)
    3. Translates signal to BacktestSignal

    Exposes regime_history for post-backtest analysis.
    """

    def __init__(
        self,
        symbol: str,
        config: Dict[str, Any],
        qty: Decimal = Decimal("0.01"),
    ) -> None:
        from src.strategy.selector import StrategySelector

        self._selector = StrategySelector.from_config(symbol=symbol, config=config)
        self._qty = qty
        self._in_position = False

    def __call__(
        self,
        bar: Bar,
        history: List[Bar],
    ) -> Optional[BacktestSignal]:
        """BacktestEngine StrategyCallback interface."""
        candle = pd.Series({
            "timestamp": bar.timestamp_ms,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        })

        bar_ts = datetime.fromtimestamp(bar.timestamp_ms / 1000, tz=timezone.utc)

        signal = self._selector.on_candle_closed(
            candle=candle,
            mid=bar.close,
            bar_timestamp=bar_ts,
        )

        if signal is None:
            return None

        direction = signal.direction  # "BUY" or "SELL"

        if direction == "BUY" and not self._in_position:
            self._in_position = True
            return BacktestSignal(side="BUY", qty=self._qty)

        if direction == "SELL" and self._in_position:
            self._in_position = False
            return BacktestSignal(side="SELL", qty=self._qty)

        return None

    @property
    def current_regime(self) -> str:
        return self._selector.current_regime.value

    @property
    def current_strategy(self) -> str:
        return self._selector.current_strategy_name

    @property
    def regime_history(self) -> list:
        return self._selector.regime_history
