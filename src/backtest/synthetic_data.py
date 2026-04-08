"""
Synthetic OHLCV generator for backtesting without network access.

Generates realistic price series that mimic specific market regimes.
Each regime has calibrated parameters matching historical BTC behavior.

NOT for production use — for development/validation only.
"""

import csv
import math
from decimal import Decimal
from pathlib import Path
from typing import List, Tuple

import numpy as np


def _generate_ohlcv(
    close_series: np.ndarray,
    start_ts_ms: int,
    step_ms: int,
    volatility_factor: float = 0.005,
    seed: int = 42,
) -> List[Tuple[int, float, float, float, float, float]]:
    """Generate OHLCV rows from a close price series."""
    rng = np.random.RandomState(seed)
    rows = []
    for i, c in enumerate(close_series):
        spread = abs(c) * volatility_factor
        h = c + rng.uniform(0.3, 1.0) * spread
        l = c - rng.uniform(0.3, 1.0) * spread
        o = c + rng.uniform(-0.5, 0.5) * spread
        vol = rng.uniform(50, 500)
        rows.append((
            start_ts_ms + i * step_ms,
            round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(vol, 2),
        ))
    return rows


def _write_csv(path: Path, rows: List[Tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for row in rows:
            w.writerow(row)


def generate_bull_trend(
    n: int = 500,
    start_price: float = 30000.0,
    end_price: float = 60000.0,
    noise_pct: float = 0.01,
    seed: int = 1,
) -> np.ndarray:
    """Strong uptrend with pullbacks."""
    rng = np.random.RandomState(seed)
    trend = np.linspace(start_price, end_price, n)
    noise = rng.normal(0, start_price * noise_pct, n)
    # Add occasional 3-5% pullbacks
    pullbacks = np.zeros(n)
    for i in range(n // 50, n, n // 10):
        length = min(15, n - i)
        pullbacks[i:i+length] = -trend[i] * rng.uniform(0.02, 0.04)
    return trend + noise + np.cumsum(pullbacks * 0.3)


def generate_bear_trend(
    n: int = 500,
    start_price: float = 50000.0,
    end_price: float = 20000.0,
    noise_pct: float = 0.012,
    seed: int = 2,
) -> np.ndarray:
    """Strong downtrend with dead cat bounces."""
    rng = np.random.RandomState(seed)
    trend = np.linspace(start_price, end_price, n)
    noise = rng.normal(0, start_price * noise_pct, n)
    return trend + noise


def generate_sideways(
    n: int = 500,
    center: float = 40000.0,
    amplitude: float = 3000.0,
    noise_pct: float = 0.005,
    seed: int = 3,
) -> np.ndarray:
    """Ranging market oscillating around a center."""
    rng = np.random.RandomState(seed)
    t = np.linspace(0, 6 * math.pi, n)
    oscillation = amplitude * np.sin(t)
    noise = rng.normal(0, center * noise_pct, n)
    return center + oscillation + noise


def generate_crash(
    n: int = 300,
    start_price: float = 60000.0,
    crash_to: float = 30000.0,
    crash_start_pct: float = 0.3,
    seed: int = 4,
) -> np.ndarray:
    """Sharp crash followed by high-volatility consolidation."""
    rng = np.random.RandomState(seed)
    crash_bar = int(n * crash_start_pct)

    # Pre-crash: slight uptrend
    pre = np.linspace(start_price, start_price * 1.05, crash_bar)
    pre += rng.normal(0, start_price * 0.005, crash_bar)

    # Crash: rapid decline
    crash_len = n // 5
    crash = np.linspace(start_price * 1.05, crash_to, crash_len)
    crash += rng.normal(0, start_price * 0.015, crash_len)

    # Post-crash: volatile consolidation
    post_len = n - crash_bar - crash_len
    post = crash_to + rng.normal(0, crash_to * 0.02, post_len).cumsum()
    post = np.clip(post, crash_to * 0.85, crash_to * 1.2)

    return np.concatenate([pre, crash, post])


def generate_volatile_trend(
    n: int = 500,
    start_price: float = 25000.0,
    end_price: float = 70000.0,
    noise_pct: float = 0.025,
    seed: int = 5,
) -> np.ndarray:
    """Strong trend with very high volatility (large swings)."""
    rng = np.random.RandomState(seed)
    trend = np.linspace(start_price, end_price, n)
    noise = rng.normal(0, start_price * noise_pct, n)
    # Large random spikes
    spikes = np.zeros(n)
    for _ in range(n // 20):
        idx = rng.randint(10, n)
        spikes[idx] = rng.choice([-1, 1]) * trend[idx] * rng.uniform(0.03, 0.06)
    return trend + noise + spikes


# ── Regime definitions with generators ─────────────────────────────────────

SYNTHETIC_REGIMES = {
    "bull_calm": {
        "generator": generate_bull_trend,
        "kwargs": {"n": 500, "start_price": 30000, "end_price": 50000, "noise_pct": 0.008, "seed": 10},
        "description": "Clean uptrend, low volatility — SMA crossover should excel",
        "vol_factor": 0.004,
    },
    "bull_volatile": {
        "generator": generate_volatile_trend,
        "kwargs": {"n": 500, "start_price": 25000, "end_price": 65000, "noise_pct": 0.025, "seed": 20},
        "description": "Strong uptrend with large swings — momentum breakout territory",
        "vol_factor": 0.012,
    },
    "bear_calm": {
        "generator": generate_bear_trend,
        "kwargs": {"n": 500, "start_price": 50000, "end_price": 25000, "noise_pct": 0.008, "seed": 30},
        "description": "Steady decline — trend following should cut losses",
        "vol_factor": 0.005,
    },
    "sideways_calm": {
        "generator": generate_sideways,
        "kwargs": {"n": 500, "center": 40000, "amplitude": 3000, "noise_pct": 0.004, "seed": 40},
        "description": "Range-bound, low vol — mean reversion should dominate",
        "vol_factor": 0.003,
    },
    "sideways_volatile": {
        "generator": generate_sideways,
        "kwargs": {"n": 500, "center": 40000, "amplitude": 6000, "noise_pct": 0.012, "seed": 50},
        "description": "Range-bound, high vol — mean reversion with wider bands",
        "vol_factor": 0.008,
    },
    "crash": {
        "generator": generate_crash,
        "kwargs": {"n": 400, "start_price": 60000, "crash_to": 30000, "seed": 60},
        "description": "Sharp crash then consolidation — risk management test",
        "vol_factor": 0.015,
    },
}


def generate_all_synthetic(
    output_dir: str = "data/synthetic",
    step_ms: int = 3_600_000,
) -> dict[str, Path]:
    """
    Generate all synthetic regime CSVs.

    Returns:
        Dict mapping regime label to CSV path.
    """
    base = Path(output_dir)
    paths = {}

    start_ts = 1_609_459_200_000  # 2021-01-01 00:00:00 UTC

    for label, spec in SYNTHETIC_REGIMES.items():
        close = spec["generator"](**spec["kwargs"])
        rows = _generate_ohlcv(
            close, start_ts, step_ms,
            volatility_factor=spec["vol_factor"],
            seed=hash(label) % 10000,
        )
        path = base / f"{label}.csv"
        _write_csv(path, rows)
        paths[label] = path
        print(f"  Generated {label}: {len(rows)} bars → {path}")

    return paths


if __name__ == "__main__":
    generate_all_synthetic()
