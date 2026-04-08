"""
Technical indicators — pure functions on pandas Series/DataFrames.

All functions return pandas Series. No side effects, no I/O.
NaN values at the start are expected (warmup period).

Indicators:
- ATR (Average True Range)
- RSI (Relative Strength Index)
- MACD (Moving Average Convergence Divergence)
- Bollinger Bands
- ADX (Average Directional Index)
- Donchian Channel
- EMA / SMA helpers
"""

from dataclasses import dataclass
from typing import Tuple

import numpy as np
import pandas as pd


# ── Helpers ────────────────────────────────────────────────────────────────


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


# ── ATR ────────────────────────────────────────────────────────────────────


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """
    True Range = max(H-L, |H-prev_C|, |L-prev_C|).

    First row is H-L (no previous close available).
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    Average True Range (Wilder's smoothing).

    Returns:
        Series of ATR values. First `period` values are NaN.
    """
    tr = true_range(high, low, close)
    # Wilder's smoothing = EMA with alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def atr_normalized(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """ATR as percentage of close price. Useful for cross-asset comparison."""
    atr_val = atr(high, low, close, period)
    return atr_val / close


# ── RSI ────────────────────────────────────────────────────────────────────


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's smoothing).

    Returns:
        Series of RSI values (0–100). First `period` values are NaN.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


# ── MACD ───────────────────────────────────────────────────────────────────


@dataclass
class MACDResult:
    """MACD computation result."""

    macd_line: pd.Series  # EMA(fast) - EMA(slow)
    signal_line: pd.Series  # EMA of macd_line
    histogram: pd.Series  # macd_line - signal_line


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """
    MACD (Moving Average Convergence Divergence).

    Args:
        close: Close price series.
        fast: Fast EMA period (default 12).
        slow: Slow EMA period (default 26).
        signal: Signal line EMA period (default 9).

    Returns:
        MACDResult with macd_line, signal_line, histogram.
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return MACDResult(
        macd_line=macd_line,
        signal_line=signal_line,
        histogram=histogram,
    )


# ── Bollinger Bands ────────────────────────────────────────────────────────


@dataclass
class BollingerResult:
    """Bollinger Bands computation result."""

    upper: pd.Series
    middle: pd.Series  # SMA
    lower: pd.Series
    bandwidth: pd.Series  # (upper - lower) / middle
    pct_b: pd.Series  # (close - lower) / (upper - lower)


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult:
    """
    Bollinger Bands.

    Args:
        close: Close price series.
        period: SMA period (default 20).
        num_std: Number of standard deviations (default 2.0).

    Returns:
        BollingerResult with upper, middle, lower, bandwidth, pct_b.
    """
    middle = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std

    band_width = upper - lower
    bandwidth = band_width / middle
    pct_b = (close - lower) / band_width.replace(0, np.nan)

    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=bandwidth,
        pct_b=pct_b,
    )


# ── ADX ────────────────────────────────────────────────────────────────────


@dataclass
class ADXResult:
    """ADX computation result."""

    adx: pd.Series  # ADX line (0–100)
    plus_di: pd.Series  # +DI line
    minus_di: pd.Series  # -DI line


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> ADXResult:
    """
    Average Directional Index.

    ADX measures trend strength (not direction):
    - ADX > 25: trending market
    - ADX < 20: ranging/sideways market
    - ADX 20-25: transition zone

    +DI > -DI: bullish trend
    -DI > +DI: bearish trend

    Returns:
        ADXResult with adx, plus_di, minus_di.
    """
    # Directional Movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    # Smoothed with Wilder's method
    atr_val = atr(high, low, close, period)

    alpha = 1.0 / period
    smooth_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    smooth_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    # Directional Indicators
    plus_di = 100.0 * smooth_plus_dm / atr_val.replace(0, np.nan)
    minus_di = 100.0 * smooth_minus_dm / atr_val.replace(0, np.nan)

    # DX and ADX
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = 100.0 * di_diff / di_sum.replace(0, np.nan)

    adx_line = dx.ewm(alpha=alpha, adjust=False).mean()

    return ADXResult(adx=adx_line, plus_di=plus_di, minus_di=minus_di)


# ── Donchian Channel ───────────────────────────────────────────────────────


@dataclass
class DonchianResult:
    """Donchian Channel result."""

    upper: pd.Series  # highest high over period
    lower: pd.Series  # lowest low over period
    middle: pd.Series  # (upper + lower) / 2


def donchian_channel(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> DonchianResult:
    """
    Donchian Channel — breakout indicator.

    Upper = highest high of last N bars.
    Lower = lowest low of last N bars.
    Middle = midpoint.

    Breakout above upper → strong bullish signal.
    Breakdown below lower → strong bearish signal.
    """
    upper = high.rolling(window=period, min_periods=period).max()
    lower = low.rolling(window=period, min_periods=period).min()
    middle = (upper + lower) / 2.0
    return DonchianResult(upper=upper, lower=lower, middle=middle)
