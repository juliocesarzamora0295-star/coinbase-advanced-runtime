"""
Multi-Timeframe Filter — confirms signals against higher timeframe trends.

Resamples 1h candles to 4h and 1d, computes SMA on each, and returns
a confidence score (0.0–1.0) based on trend alignment.

Does NOT generate signals. Only filters/scores existing ones.
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("MTFFilter")


class MultiTimeframeFilter:
    """
    Confirms trade direction against 4h and 1d trend (SMA-based).

    Confidence scores:
        BUY + 4h up + 1d up  = 1.0  (full alignment)
        BUY + one aligned     = 0.5  (mixed)
        BUY + both against    = 0.2  (counter-trend)
        SELL mirrors BUY (4h down + 1d down = 1.0 for SELL)

    Returns 0.5 (neutral) if insufficient data for resampling.
    """

    MIN_BARS_1H = 1200  # 50 days × 24h = 1200 bars for 1d SMA(50)

    def __init__(self, sma_period: int = 50) -> None:
        self._sma_period = sma_period

    def get_confidence(self, df_1h: pd.DataFrame, direction: str) -> float:
        """
        Compute trend-alignment confidence.

        Args:
            df_1h: DataFrame with 'close' column and DatetimeIndex (or 'timestamp' col).
            direction: "BUY" or "SELL"

        Returns:
            Confidence score 0.0–1.0.
        """
        if len(df_1h) < self.MIN_BARS_1H:
            return 0.5

        df = self._ensure_datetime_index(df_1h)
        if df is None:
            return 0.5

        uptrend_4h = self._is_uptrend(df, "4h")
        uptrend_1d = self._is_uptrend(df, "1D")

        if uptrend_4h is None or uptrend_1d is None:
            return 0.5

        return self._score(direction.upper(), uptrend_4h, uptrend_1d)

    def _ensure_datetime_index(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """Convert to DatetimeIndex if needed."""
        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            if "timestamp" in df.columns:
                ts = df["timestamp"]
                if ts.dtype in ("int64", "float64") and ts.iloc[0] > 1e12:
                    df.index = pd.to_datetime(ts, unit="ms", utc=True)
                elif ts.dtype in ("int64", "float64"):
                    df.index = pd.to_datetime(ts, unit="s", utc=True)
                else:
                    df.index = pd.to_datetime(ts, utc=True)
            else:
                return None
        return df

    def _is_uptrend(self, df: pd.DataFrame, freq: str) -> Optional[bool]:
        """Resample to freq, compute SMA, return True if close > SMA."""
        try:
            resampled = df["close"].resample(freq).last().dropna()
            if len(resampled) < self._sma_period:
                return None
            sma = resampled.rolling(self._sma_period).mean()
            last_close = resampled.iloc[-1]
            last_sma = sma.iloc[-1]
            if pd.isna(last_sma):
                return None
            return float(last_close) > float(last_sma)
        except Exception as exc:
            logger.debug("Resample to %s failed: %s", freq, exc)
            return None

    @staticmethod
    def _score(direction: str, uptrend_4h: bool, uptrend_1d: bool) -> float:
        """Map direction + higher-TF trends to confidence score."""
        if direction == "BUY":
            aligned_4h = uptrend_4h
            aligned_1d = uptrend_1d
        else:  # SELL
            aligned_4h = not uptrend_4h
            aligned_1d = not uptrend_1d

        if aligned_4h and aligned_1d:
            return 1.0
        if aligned_4h or aligned_1d:
            return 0.5
        return 0.2
