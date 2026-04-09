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


class AdaptiveAdapter:
    """
    Wraps any strategy adapter and applies adaptive position sizing.

    Instead of fixed qty, computes qty dynamically based on market
    conditions (volatility, trend, volume, drawdown).

    Works with StrategyAdapter, SelectorAdapter, or any callable
    that returns BacktestSignal.
    """

    def __init__(
        self,
        inner: Any,
        equity: Decimal,
        base_pct: Decimal = Decimal("0.005"),
        adaptive_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        from src.risk.adaptive_sizer import AdaptiveSizer

        self._inner = inner
        self._equity = equity
        self._base_pct = base_pct
        cfg = adaptive_config or {}
        self._sizer = AdaptiveSizer(
            base_pct=base_pct,
            **{k: v for k, v in cfg.items() if k in AdaptiveSizer.__init__.__code__.co_varnames},
        )
        self._ledger = None  # set externally for drawdown tracking
        self._df_cache: pd.DataFrame = pd.DataFrame()
        self.sizing_log: List[Dict[str, Any]] = []

    def set_ledger(self, ledger: Any) -> None:
        """Attach ledger for drawdown tracking."""
        self._ledger = ledger

    def __call__(
        self,
        bar: Bar,
        history: List[Bar],
    ) -> Optional[BacktestSignal]:
        """
        Delegates to inner adapter, then adjusts qty with AdaptiveSizer.
        """
        from src.risk.adaptive_sizer import build_context_from_df

        signal = self._inner(bar, history)
        if signal is None:
            return None

        # Build DataFrame for context
        all_bars = history + [bar]
        if len(all_bars) < 30:
            return signal  # not enough data for adaptive — use default

        rows = [
            {
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume),
            }
            for b in all_bars
        ]
        df = pd.DataFrame(rows)

        # Drawdown from ledger
        dd_pct = 0.0
        if self._ledger is not None:
            dd_pct = float(self._ledger.get_drawdown(bar.close))

        ctx = build_context_from_df(
            df=df,
            signal_direction=signal.side,
            drawdown_pct=dd_pct,
        )

        if ctx is None:
            return signal

        result = self._sizer.compute(ctx)

        # Convert pct to qty: qty = (equity × pct) / price
        price = bar.close
        if price <= Decimal("0"):
            return signal

        current_equity = self._equity
        if self._ledger is not None:
            current_equity = self._ledger.get_equity(price)

        adaptive_qty = (current_equity * result.effective_pct) / price

        # Log for analysis
        self.sizing_log.append({
            "bar_ts": bar.timestamp_ms,
            "side": signal.side,
            "base_qty": float(signal.qty),
            "adaptive_qty": float(adaptive_qty),
            "effective_pct": float(result.effective_pct),
            "multiplier": result.combined_multiplier,
            "vol_f": result.vol_factor,
            "trend_f": result.trend_factor,
            "volume_f": result.volume_factor,
            "dd_f": result.dd_factor,
        })

        return BacktestSignal(side=signal.side, qty=adaptive_qty)


class FullAdaptiveAdapter:
    """
    Combines SelectorAdapter (regime-aware strategy) with AdaptiveAdapter
    (dynamic position sizing) into a single backtest callback.
    """

    def __init__(
        self,
        symbol: str,
        config: Dict[str, Any],
        equity: Decimal = Decimal("10000"),
        base_pct: Decimal = Decimal("0.005"),
        adaptive_config: Optional[Dict[str, Any]] = None,
        ledger: Any = None,
    ) -> None:
        self._selector = SelectorAdapter(symbol=symbol, config=config, qty=Decimal("0.01"))
        self._adaptive = AdaptiveAdapter(
            inner=self._selector,
            equity=equity,
            base_pct=base_pct,
            adaptive_config=adaptive_config,
        )
        if ledger is not None:
            self._adaptive.set_ledger(ledger)

    def set_ledger(self, ledger: Any) -> None:
        self._adaptive.set_ledger(ledger)

    def __call__(
        self,
        bar: Bar,
        history: List[Bar],
    ) -> Optional[BacktestSignal]:
        return self._adaptive(bar, history)

    @property
    def regime_history(self) -> list:
        return self._selector.regime_history

    @property
    def sizing_log(self) -> List[Dict[str, Any]]:
        return self._adaptive.sizing_log

    @property
    def current_regime(self) -> str:
        return self._selector.current_regime

    @property
    def current_strategy(self) -> str:
        return self._selector.current_strategy
