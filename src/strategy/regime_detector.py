"""
RegimeDetector — classifies current market state.

Uses ADX for trend strength and normalized ATR for volatility.
Classification is a 2x2 matrix:

                    Low Volatility        High Volatility
    Trending        TRENDING_CALM         TRENDING_VOLATILE
    Ranging         RANGING_CALM          RANGING_VOLATILE

Each regime maps to an optimal strategy type.

RegimeDetector is stateless per evaluation. Same DataFrame → same regime.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from src.quantitative.indicators import adx, atr_normalized, sma

logger = logging.getLogger("RegimeDetector")


class MarketRegime(Enum):
    """Market regime classification."""

    TRENDING_CALM = "TRENDING_CALM"
    TRENDING_VOLATILE = "TRENDING_VOLATILE"
    RANGING_CALM = "RANGING_CALM"
    RANGING_VOLATILE = "RANGING_VOLATILE"
    UNKNOWN = "UNKNOWN"  # insufficient data


# Strategy type recommendations per regime
REGIME_STRATEGY_MAP = {
    MarketRegime.TRENDING_CALM: "sma_crossover",
    MarketRegime.TRENDING_VOLATILE: "momentum_breakout",
    MarketRegime.RANGING_CALM: "mean_reversion",
    MarketRegime.RANGING_VOLATILE: "mean_reversion",
    MarketRegime.UNKNOWN: "sma_crossover",  # conservative default
}


@dataclass(frozen=True)
class RegimeSnapshot:
    """Immutable snapshot of regime classification."""

    regime: MarketRegime
    adx_value: float
    atr_norm_value: float  # ATR as % of price
    trend_direction: str  # "BULL", "BEAR", or "NEUTRAL"
    confidence: float  # 0.0–1.0: how decisive the classification is
    recommended_strategy: str


class RegimeDetector:
    """
    Classifies market regime from OHLCV DataFrame.

    Thresholds are configurable. Defaults tuned for BTC 1h candles.

    ADX thresholds:
    - adx > adx_trend_threshold → trending
    - adx < adx_range_threshold → ranging
    - between → use hysteresis from last regime

    ATR normalized thresholds:
    - atr_norm > vol_high_threshold → high volatility
    - atr_norm < vol_low_threshold → low volatility
    """

    def __init__(
        self,
        adx_period: int = 14,
        atr_period: int = 14,
        adx_trend_threshold: float = 25.0,
        adx_range_threshold: float = 20.0,
        vol_high_threshold: float = 0.025,  # 2.5% ATR/price
        vol_low_threshold: float = 0.015,  # 1.5% ATR/price
        sma_trend_period: int = 50,
    ) -> None:
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.adx_trend_threshold = adx_trend_threshold
        self.adx_range_threshold = adx_range_threshold
        self.vol_high_threshold = vol_high_threshold
        self.vol_low_threshold = vol_low_threshold
        self.sma_trend_period = sma_trend_period

        # Hysteresis: remember last regime for ambiguous zones
        self._last_trending: Optional[bool] = None
        self._last_volatile: Optional[bool] = None

    @property
    def min_bars_required(self) -> int:
        """Minimum bars needed for regime detection."""
        return max(self.adx_period, self.atr_period, self.sma_trend_period) + 10

    def detect(self, df: pd.DataFrame) -> RegimeSnapshot:
        """
        Classify current market regime from OHLCV DataFrame.

        Args:
            df: DataFrame with columns: open, high, low, close, volume.
                Must have at least `min_bars_required` rows.

        Returns:
            RegimeSnapshot with classification and metrics.
        """
        if df is None or len(df) < self.min_bars_required:
            return RegimeSnapshot(
                regime=MarketRegime.UNKNOWN,
                adx_value=0.0,
                atr_norm_value=0.0,
                trend_direction="NEUTRAL",
                confidence=0.0,
                recommended_strategy=REGIME_STRATEGY_MAP[MarketRegime.UNKNOWN],
            )

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        # Compute indicators
        adx_result = adx(high, low, close, self.adx_period)
        atr_norm = atr_normalized(high, low, close, self.atr_period)
        sma_line = sma(close, self.sma_trend_period)

        # Current values (last row)
        current_adx = float(adx_result.adx.iloc[-1])
        current_atr_norm = float(atr_norm.iloc[-1])
        current_plus_di = float(adx_result.plus_di.iloc[-1])
        current_minus_di = float(adx_result.minus_di.iloc[-1])
        current_price = float(close.iloc[-1])
        current_sma = float(sma_line.iloc[-1]) if not pd.isna(sma_line.iloc[-1]) else current_price

        # ── Trend classification with hysteresis ──
        if current_adx > self.adx_trend_threshold:
            is_trending = True
        elif current_adx < self.adx_range_threshold:
            is_trending = False
        else:
            # Ambiguous zone — use last known state
            is_trending = self._last_trending if self._last_trending is not None else False

        self._last_trending = is_trending

        # ── Volatility classification with hysteresis ──
        if current_atr_norm > self.vol_high_threshold:
            is_volatile = True
        elif current_atr_norm < self.vol_low_threshold:
            is_volatile = False
        else:
            is_volatile = self._last_volatile if self._last_volatile is not None else False

        self._last_volatile = is_volatile

        # ── Trend direction ──
        if current_plus_di > current_minus_di and current_price > current_sma:
            trend_direction = "BULL"
        elif current_minus_di > current_plus_di and current_price < current_sma:
            trend_direction = "BEAR"
        else:
            trend_direction = "NEUTRAL"

        # ── Regime classification ──
        if is_trending and is_volatile:
            regime = MarketRegime.TRENDING_VOLATILE
        elif is_trending and not is_volatile:
            regime = MarketRegime.TRENDING_CALM
        elif not is_trending and is_volatile:
            regime = MarketRegime.RANGING_VOLATILE
        else:
            regime = MarketRegime.RANGING_CALM

        # ── Confidence ──
        # Higher ADX = more confident about trend/no-trend
        # Farther from thresholds = more confident
        adx_dist = abs(current_adx - (self.adx_trend_threshold + self.adx_range_threshold) / 2)
        confidence = min(1.0, adx_dist / 15.0)  # normalize to 0–1

        recommended = REGIME_STRATEGY_MAP[regime]

        logger.debug(
            "Regime: %s (ADX=%.1f, ATR_norm=%.4f, direction=%s, confidence=%.2f) → %s",
            regime.value,
            current_adx,
            current_atr_norm,
            trend_direction,
            confidence,
            recommended,
        )

        return RegimeSnapshot(
            regime=regime,
            adx_value=current_adx,
            atr_norm_value=current_atr_norm,
            trend_direction=trend_direction,
            confidence=confidence,
            recommended_strategy=recommended,
        )

    def reset(self) -> None:
        """Reset hysteresis state."""
        self._last_trending = None
        self._last_volatile = None
