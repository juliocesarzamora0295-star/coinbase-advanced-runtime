"""Tests for vwap() indicator (Fase 3C)."""

import numpy as np
import pandas as pd

from src.quantitative.indicators import vwap


def test_vwap_flat_price_returns_same_value() -> None:
    closes = pd.Series([100.0] * 10)
    s = vwap(closes, closes, closes, pd.Series([1.0] * 10))
    assert (s == 100.0).all()


def test_vwap_handles_zero_volume_with_nan() -> None:
    closes = pd.Series([100.0, 101.0, 102.0])
    v = pd.Series([0.0, 0.0, 0.0])
    result = vwap(closes, closes, closes, v)
    assert result.isna().all()


def test_vwap_weighted_by_volume() -> None:
    """
    Two bars: typical price 100 with volume 1, then 200 with volume 3.
    Expected cumulative VWAP at bar 2 = (100*1 + 200*3) / (1+3) = 175.
    """
    closes = pd.Series([100.0, 200.0])
    v = pd.Series([1.0, 3.0])
    result = vwap(closes, closes, closes, v)
    assert result.iloc[0] == 100.0
    assert result.iloc[1] == 175.0


def test_vwap_uses_typical_price() -> None:
    """typical = (high+low+close)/3. High=110 Low=90 Close=100 → 100."""
    h = pd.Series([110.0])
    low = pd.Series([90.0])
    c = pd.Series([100.0])
    v = pd.Series([1.0])
    result = vwap(h, low, c, v)
    assert result.iloc[0] == 100.0
