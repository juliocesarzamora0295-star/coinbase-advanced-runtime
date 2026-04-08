"""
AdaptiveSizer — dynamic position sizing based on market conditions.

Replaces fixed notional_pct with a dynamic multiplier computed from:
1. Volatility (ATR normalized) — high vol → smaller size
2. Trend strength (ADX) — strong trend → larger size
3. Volume confirmation — high volume → larger size
4. Drawdown penalty — deeper drawdown → smaller size

Formula:
    effective_pct = base_pct × vol_factor × trend_factor × volume_factor × dd_factor

Each factor ranges [min_factor, max_factor] (default [0.3, 1.5]).
The product is clamped to [floor_pct, ceiling_pct] to prevent extremes.

AdaptiveSizer is stateless per call. Same inputs → same output.
Does not replace RiskGate — RiskGate still applies hard caps after sizing.
"""

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import pandas as pd

from src.quantitative.indicators import adx, atr_normalized, sma

logger = logging.getLogger("AdaptiveSizer")


@dataclass(frozen=True)
class SizingContext:
    """
    Market context for adaptive sizing. Immutable.

    All values are current (latest bar).
    """

    atr_norm: float  # ATR / price (0.0–1.0 typically 0.005–0.05)
    adx_value: float  # ADX (0–100)
    volume_ratio: float  # current_volume / avg_volume (0.0+)
    drawdown_pct: float  # current drawdown from peak (0.0–1.0)
    trend_aligned: bool  # True if signal direction matches trend direction


@dataclass(frozen=True)
class AdaptiveSizingResult:
    """Result of adaptive sizing computation. Immutable."""

    effective_pct: Decimal  # final notional_pct to use
    base_pct: Decimal  # original base_pct
    vol_factor: float
    trend_factor: float
    volume_factor: float
    dd_factor: float
    combined_multiplier: float
    rationale: str


class AdaptiveSizer:
    """
    Computes dynamic notional_pct based on market conditions.

    Each factor maps a metric to a multiplier via linear interpolation
    between defined breakpoints. Factors are multiplied together,
    then clamped to [floor_pct, ceiling_pct].

    Configuration (all via constructor):
    - base_pct: starting notional_pct (from config, e.g. 0.005)
    - Volatility: high ATR → reduce size (inverse relationship)
    - Trend: high ADX + aligned → increase size
    - Volume: high relative volume → increase size
    - Drawdown: deeper DD → reduce size (protective)
    """

    def __init__(
        self,
        base_pct: Decimal = Decimal("0.005"),
        # Volatility factor: ATR_norm breakpoints → factor
        vol_low: float = 0.010,  # below this → max factor
        vol_high: float = 0.040,  # above this → min factor
        vol_factor_range: tuple[float, float] = (0.4, 1.3),
        # Trend factor: ADX breakpoints → factor
        adx_weak: float = 15.0,  # below this → min factor
        adx_strong: float = 35.0,  # above this → max factor
        trend_factor_range: tuple[float, float] = (0.5, 1.5),
        trend_aligned_bonus: float = 0.2,  # extra when signal aligns with trend
        # Volume factor
        vol_ratio_low: float = 0.5,  # below this → min factor
        vol_ratio_high: float = 2.0,  # above this → max factor
        volume_factor_range: tuple[float, float] = (0.6, 1.3),
        # Drawdown penalty
        dd_start: float = 0.02,  # start penalizing at 2% DD
        dd_severe: float = 0.08,  # severe penalty at 8% DD
        dd_factor_range: tuple[float, float] = (0.2, 1.0),
        # Clamps
        floor_pct: Decimal = Decimal("0.001"),  # never below 0.1%
        ceiling_pct: Decimal = Decimal("0.020"),  # never above 2.0%
    ) -> None:
        self.base_pct = base_pct
        self.vol_low = vol_low
        self.vol_high = vol_high
        self.vol_factor_range = vol_factor_range
        self.adx_weak = adx_weak
        self.adx_strong = adx_strong
        self.trend_factor_range = trend_factor_range
        self.trend_aligned_bonus = trend_aligned_bonus
        self.vol_ratio_low = vol_ratio_low
        self.vol_ratio_high = vol_ratio_high
        self.volume_factor_range = volume_factor_range
        self.dd_start = dd_start
        self.dd_severe = dd_severe
        self.dd_factor_range = dd_factor_range
        self.floor_pct = floor_pct
        self.ceiling_pct = ceiling_pct

    def compute(self, ctx: SizingContext) -> AdaptiveSizingResult:
        """
        Compute adaptive notional_pct from market context.

        Args:
            ctx: SizingContext with current market metrics.

        Returns:
            AdaptiveSizingResult with effective_pct and breakdown.
        """
        # 1. Volatility factor (INVERSE — high vol → lower factor)
        vf = _interpolate_inverse(
            ctx.atr_norm,
            self.vol_low,
            self.vol_high,
            self.vol_factor_range[0],
            self.vol_factor_range[1],
        )

        # 2. Trend factor (DIRECT — high ADX → higher factor)
        tf = _interpolate(
            ctx.adx_value,
            self.adx_weak,
            self.adx_strong,
            self.trend_factor_range[0],
            self.trend_factor_range[1],
        )
        # Bonus when signal direction matches trend
        if ctx.trend_aligned:
            tf = min(tf + self.trend_aligned_bonus, self.trend_factor_range[1])

        # 3. Volume factor (DIRECT — high volume → higher factor)
        vlf = _interpolate(
            ctx.volume_ratio,
            self.vol_ratio_low,
            self.vol_ratio_high,
            self.volume_factor_range[0],
            self.volume_factor_range[1],
        )

        # 4. Drawdown factor (INVERSE — high DD → lower factor)
        ddf = _interpolate_inverse(
            ctx.drawdown_pct,
            self.dd_start,
            self.dd_severe,
            self.dd_factor_range[0],
            self.dd_factor_range[1],
        )

        # Combine
        combined = vf * tf * vlf * ddf
        raw_pct = float(self.base_pct) * combined
        effective = Decimal(str(round(raw_pct, 6)))

        # Clamp
        effective = max(self.floor_pct, min(self.ceiling_pct, effective))

        rationale = (
            f"base={self.base_pct} × vol={vf:.2f} × trend={tf:.2f} "
            f"× volume={vlf:.2f} × dd={ddf:.2f} = {combined:.3f} "
            f"→ pct={effective}"
        )

        logger.debug("AdaptiveSizer: %s", rationale)

        return AdaptiveSizingResult(
            effective_pct=effective,
            base_pct=self.base_pct,
            vol_factor=vf,
            trend_factor=tf,
            volume_factor=vlf,
            dd_factor=ddf,
            combined_multiplier=combined,
            rationale=rationale,
        )


def build_context_from_df(
    df: pd.DataFrame,
    signal_direction: str,
    drawdown_pct: float = 0.0,
    adx_period: int = 14,
    atr_period: int = 14,
    volume_ma_period: int = 20,
) -> Optional[SizingContext]:
    """
    Build SizingContext from OHLCV DataFrame.

    Convenience function for backtest and live integration.

    Args:
        df: DataFrame with open, high, low, close, volume columns.
        signal_direction: "BUY" or "SELL"
        drawdown_pct: current drawdown from equity peak (0.0–1.0)

    Returns:
        SizingContext or None if insufficient data.
    """
    min_bars = max(adx_period, atr_period, volume_ma_period) + 5
    if df is None or len(df) < min_bars:
        return None

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0, index=close.index)

    # Compute indicators
    atr_norm_series = atr_normalized(high, low, close, atr_period)
    adx_result = adx(high, low, close, adx_period)
    vol_ma = sma(volume, volume_ma_period)

    current_atr_norm = float(atr_norm_series.iloc[-1])
    current_adx = float(adx_result.adx.iloc[-1])
    current_plus_di = float(adx_result.plus_di.iloc[-1])
    current_minus_di = float(adx_result.minus_di.iloc[-1])
    current_volume = float(volume.iloc[-1])
    avg_volume = float(vol_ma.iloc[-1]) if not pd.isna(vol_ma.iloc[-1]) else 1.0

    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

    # Trend alignment
    if signal_direction.upper() == "BUY":
        trend_aligned = current_plus_di > current_minus_di
    else:
        trend_aligned = current_minus_di > current_plus_di

    if pd.isna(current_atr_norm) or pd.isna(current_adx):
        return None

    return SizingContext(
        atr_norm=current_atr_norm,
        adx_value=current_adx,
        volume_ratio=volume_ratio,
        drawdown_pct=drawdown_pct,
        trend_aligned=trend_aligned,
    )


def _interpolate(value: float, low: float, high: float, min_out: float, max_out: float) -> float:
    """Linear interpolation: low→min_out, high→max_out, clamped."""
    if high <= low:
        return (min_out + max_out) / 2
    t = (value - low) / (high - low)
    t = max(0.0, min(1.0, t))
    return min_out + t * (max_out - min_out)


def _interpolate_inverse(value: float, low: float, high: float, min_out: float, max_out: float) -> float:
    """Inverse interpolation: low→max_out, high→min_out, clamped."""
    return _interpolate(value, low, high, max_out, min_out)
