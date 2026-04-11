"""
Strategy adapter — bridges production strategies to BacktestEngine.

Wraps SmaCrossoverStrategy (or any Strategy subclass) so it can be used
as a BacktestEngine StrategyCallback.

This ensures backtests use the EXACT same logic as live trading,
not a simplified copy.
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.backtest.data_feed import Bar
from src.backtest.engine import Signal as BacktestSignal
from src.quantitative.indicators import atr as atr_fn
from src.strategy.base import Strategy
from src.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger("StrategyAdapter")


def _compute_entry_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Compute ATR from a DataFrame for the adapter's internal partial-exit logic.
    Falls back to close-only pseudo-ATR when high/low are missing, clamps the
    period to available bars, and returns None on NaN/inf.
    """
    if df is None or len(df) < 3:
        return None
    effective = max(2, min(period, len(df) - 1))
    close_s = df["close"].astype(float)
    if "high" in df.columns and "low" in df.columns:
        high_s = df["high"].astype(float)
        low_s = df["low"].astype(float)
    else:
        high_s = close_s
        low_s = close_s
    series = atr_fn(high_s, low_s, close_s, period=effective)
    value = float(series.iloc[-1])
    if math.isnan(value) or math.isinf(value):
        return None
    return value


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
        partial_exits: bool = False,
        atr_period: int = 14,
    ) -> None:
        self._strategy = strategy
        self._qty = qty
        self._in_position = False
        self._position_qty: Decimal = Decimal("0")
        self._df = pd.DataFrame()

        # Partial exits (Fase 4B'): off by default. When enabled, the adapter
        # takes a 50% profit at `entry_price + entry_atr` (TP1) before the
        # strategy's full exit signal. Stays off in the default backtest path
        # so existing baselines are unchanged.
        self._partial_exits = partial_exits
        self._atr_period = atr_period
        self._tp1_hit: bool = False
        self._remaining_qty: Decimal = Decimal("0")
        self._entry_price: Optional[float] = None
        self._entry_atr: Optional[float] = None

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

        # Partial exit (TP1) check — opt-in. Fires BEFORE consulting the
        # strategy so the entry bar isn't double-signaled. Strategy state
        # is still advanced below so trailing stops stay coherent on live
        # re-use.
        if (
            self._partial_exits
            and self._in_position
            and not self._tp1_hit
            and self._entry_price is not None
            and self._entry_atr is not None
        ):
            if float(bar.close) >= self._entry_price + self._entry_atr:
                half = self._remaining_qty / Decimal("2")
                if half >= Decimal("0.0001"):
                    self._tp1_hit = True
                    self._remaining_qty -= half
                    self._position_qty = self._remaining_qty
                    # Feed strategy so it stays in sync with the bar timeline.
                    self._strategy.update_market_data(df)
                    return BacktestSignal(side="SELL", qty=half)

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

        # Asymmetric sizing (Fase 1B'):
        # - BUY qty = base_qty * signal.strength (variable, by confidence).
        # - SELL qty = self._position_qty (full close).
        # Symmetric strength on both legs breaks ledger matching because
        # strength(BUY) != strength(SELL) in general.
        if direction == "BUY" and not self._in_position:
            strength = getattr(sig, "strength", Decimal("1"))
            if not isinstance(strength, Decimal):
                strength = Decimal(str(strength))
            effective_qty = self._qty * strength
            if effective_qty < Decimal("0.0001"):
                return None
            self._in_position = True
            self._position_qty = effective_qty
            self._remaining_qty = effective_qty
            self._tp1_hit = False
            self._entry_price = float(bar.close) if self._partial_exits else None
            self._entry_atr = (
                _compute_entry_atr(df, period=self._atr_period)
                if self._partial_exits
                else None
            )
            return BacktestSignal(side="BUY", qty=effective_qty)

        if direction == "SELL" and self._in_position:
            sell_qty = self._remaining_qty
            self._in_position = False
            self._position_qty = Decimal("0")
            self._remaining_qty = Decimal("0")
            self._tp1_hit = False
            self._entry_price = None
            self._entry_atr = None
            return BacktestSignal(side="SELL", qty=sell_qty)

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
        self._position_qty: Decimal = Decimal("0")

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

        # Asymmetric sizing (Fase 1B'): BUY by strength, SELL full close.
        if direction == "BUY" and not self._in_position:
            strength = getattr(signal, "strength", Decimal("1"))
            if not isinstance(strength, Decimal):
                strength = Decimal(str(strength))
            effective_qty = self._qty * strength
            if effective_qty < Decimal("0.0001"):
                return None
            self._in_position = True
            self._position_qty = effective_qty
            return BacktestSignal(side="BUY", qty=effective_qty)

        if direction == "SELL" and self._in_position:
            sell_qty = self._position_qty
            self._in_position = False
            self._position_qty = Decimal("0")
            return BacktestSignal(side="SELL", qty=sell_qty)

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
        mtf_filter: Optional[Any] = None,
    ) -> None:
        self._selector = SelectorAdapter(symbol=symbol, config=config, qty=Decimal("0.01"))
        self._adaptive = AdaptiveAdapter(
            inner=self._selector,
            equity=equity,
            base_pct=base_pct,
            adaptive_config=adaptive_config,
        )
        self._mtf_filter = mtf_filter
        if ledger is not None:
            self._adaptive.set_ledger(ledger)

    def set_ledger(self, ledger: Any) -> None:
        self._adaptive.set_ledger(ledger)

    def __call__(
        self,
        bar: Bar,
        history: List[Bar],
    ) -> Optional[BacktestSignal]:
        signal = self._adaptive(bar, history)

        if signal is None or self._mtf_filter is None:
            return signal

        # Build DataFrame with DatetimeIndex for MTF resampling
        all_bars = history + [bar]
        if len(all_bars) < self._mtf_filter.MIN_BARS_1H:
            return signal

        rows = [
            {
                "timestamp": b.timestamp_ms,
                "close": float(b.close),
            }
            for b in all_bars
        ]
        df = pd.DataFrame(rows)
        confidence = self._mtf_filter.get_confidence(df, signal.side)

        # Log MTF confidence
        if self._adaptive.sizing_log:
            self._adaptive.sizing_log[-1]["mtf_confidence"] = confidence

        if confidence < 0.3:
            return None

        # Scale qty by confidence
        adjusted_qty = signal.qty * Decimal(str(confidence))
        return BacktestSignal(side=signal.side, qty=adjusted_qty)

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
