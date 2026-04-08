"""
StrategySelector — picks optimal strategy based on market regime.

Combines RegimeDetector with a pool of strategies. On each candle close:
1. Detect current market regime
2. Select the strategy that matches the regime
3. Run that strategy's signal generation
4. Return signal (or None)

This replaces the fixed single-strategy approach.

Does NOT bypass RiskGate. Signals still flow through the full pipeline.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.strategy.base import Strategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.momentum_breakout import MomentumBreakoutStrategy
from src.strategy.regime_detector import MarketRegime, RegimeDetector, RegimeSnapshot
from src.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger("StrategySelector")

# Default strategy assignments per regime
_DEFAULT_REGIME_MAP: Dict[MarketRegime, str] = {
    MarketRegime.TRENDING_CALM: "sma_crossover",
    MarketRegime.TRENDING_VOLATILE: "momentum_breakout",
    MarketRegime.RANGING_CALM: "mean_reversion",
    MarketRegime.RANGING_VOLATILE: "mean_reversion",
    MarketRegime.UNKNOWN: "sma_crossover",
}

# Strategy class registry
_SELECTOR_REGISTRY: Dict[str, type] = {
    "sma_crossover": SmaCrossoverStrategy,
    "ma_crossover": SmaCrossoverStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
}


class StrategySelector:
    """
    Selects strategy based on market regime detection.

    Holds one instance of each strategy. On each bar:
    1. Accumulates candle data
    2. Runs regime detection
    3. Delegates to the strategy matching the current regime
    4. Returns that strategy's signal

    Regime detection uses a configurable re-evaluation interval
    (default: every bar) to avoid excessive switching.

    State:
    - Tracks which strategy is active
    - Tracks regime history for logging/analysis
    - Each strategy maintains its own internal state
    """

    def __init__(
        self,
        symbol: str,
        strategies: Dict[str, Strategy],
        detector: RegimeDetector,
        regime_map: Optional[Dict[MarketRegime, str]] = None,
        re_eval_interval: int = 1,  # re-evaluate regime every N bars
        min_regime_bars: int = 3,  # minimum bars before allowing regime switch
    ) -> None:
        if not strategies:
            raise ValueError(f"StrategySelector for {symbol}: strategies dict is empty")

        self.symbol = symbol
        self._strategies = strategies
        self._detector = detector
        self._regime_map = regime_map or _DEFAULT_REGIME_MAP
        self._re_eval_interval = max(1, re_eval_interval)
        self._min_regime_bars = max(1, min_regime_bars)

        self._candles: pd.DataFrame = pd.DataFrame()
        self._bar_count: int = 0
        self._current_regime: MarketRegime = MarketRegime.UNKNOWN
        self._current_strategy_name: str = self._regime_map[MarketRegime.UNKNOWN]
        self._bars_in_current_regime: int = 0
        self._regime_history: List[tuple[int, MarketRegime, str]] = []

    @classmethod
    def from_config(
        cls,
        symbol: str,
        config: Dict[str, Any],
    ) -> "StrategySelector":
        """
        Build StrategySelector from config dict.

        Config keys:
        - sma_fast, sma_slow: SMA crossover params
        - bb_period, bb_std, rsi_period, rsi_oversold, rsi_overbought: mean reversion
        - donchian_period, volume_ma_period, atr_period: momentum breakout
        - adx_period, adx_trend_threshold, adx_range_threshold: regime detector
        - vol_high_threshold, vol_low_threshold: volatility thresholds
        - re_eval_interval: bars between regime re-evaluation
        - min_regime_bars: minimum bars before regime switch
        """
        strategies: Dict[str, Strategy] = {}

        for name, strategy_cls in _SELECTOR_REGISTRY.items():
            try:
                strategies[name] = strategy_cls(symbol=symbol, config=config)
            except Exception as exc:
                logger.error("Failed to create strategy %s: %s", name, exc)

        if not strategies:
            raise ValueError(f"No strategies could be created for {symbol}")

        detector = RegimeDetector(
            adx_period=int(config.get("adx_period", 14)),
            atr_period=int(config.get("atr_period", 14)),
            adx_trend_threshold=float(config.get("adx_trend_threshold", 25.0)),
            adx_range_threshold=float(config.get("adx_range_threshold", 20.0)),
            vol_high_threshold=float(config.get("vol_high_threshold", 0.025)),
            vol_low_threshold=float(config.get("vol_low_threshold", 0.015)),
        )

        return cls(
            symbol=symbol,
            strategies=strategies,
            detector=detector,
            re_eval_interval=int(config.get("re_eval_interval", 1)),
            min_regime_bars=int(config.get("min_regime_bars", 3)),
        )

    def on_candle_closed(
        self,
        candle: pd.Series,
        mid: Optional[Decimal] = None,
        bar_timestamp: Optional[datetime] = None,
    ) -> Optional[object]:
        """
        Process a closed candle: detect regime, select strategy, generate signal.

        Args:
            candle: pd.Series with at least 'close' column.
            mid: Optional mid price.
            bar_timestamp: Optional bar timestamp.

        Returns:
            Signal or None.
        """
        # Accumulate candle
        row = candle.to_frame().T if isinstance(candle, pd.Series) else candle
        self._candles = (
            pd.concat([self._candles, row], ignore_index=True)
            if not self._candles.empty
            else row.reset_index(drop=True)
        )
        self._bar_count += 1
        self._bars_in_current_regime += 1

        # Skip first bar (startup)
        if self._bar_count == 1:
            return None

        # Regime detection
        if self._bar_count % self._re_eval_interval == 0:
            snapshot = self._detector.detect(self._candles)
            new_regime = snapshot.regime

            if new_regime != self._current_regime:
                if self._bars_in_current_regime >= self._min_regime_bars:
                    old = self._current_regime
                    self._current_regime = new_regime
                    self._current_strategy_name = self._regime_map.get(
                        new_regime, "sma_crossover"
                    )
                    self._bars_in_current_regime = 0
                    self._regime_history.append(
                        (self._bar_count, new_regime, self._current_strategy_name)
                    )
                    logger.info(
                        "Regime switch: %s → %s (strategy: %s, ADX=%.1f, ATR_norm=%.4f)",
                        old.value,
                        new_regime.value,
                        self._current_strategy_name,
                        snapshot.adx_value,
                        snapshot.atr_norm_value,
                    )

        # Select and run active strategy
        strategy = self._strategies.get(self._current_strategy_name)
        if strategy is None:
            logger.error(
                "Strategy '%s' not found in pool — falling back to first available",
                self._current_strategy_name,
            )
            strategy = next(iter(self._strategies.values()))

        price = mid if mid is not None else Decimal(str(candle.get("close", 0)))

        try:
            strategy.update_market_data(self._candles)
            signals = strategy.generate_signals(mid=price, bar_timestamp=bar_timestamp)
            return signals[0] if signals else None
        except Exception as exc:
            logger.error(
                "Strategy %s raised: %s — no signal",
                self._current_strategy_name,
                exc,
            )
            return None

    @property
    def current_regime(self) -> MarketRegime:
        return self._current_regime

    @property
    def current_strategy_name(self) -> str:
        return self._current_strategy_name

    @property
    def regime_history(self) -> List[tuple[int, MarketRegime, str]]:
        return list(self._regime_history)

    @property
    def bar_count(self) -> int:
        return self._bar_count
