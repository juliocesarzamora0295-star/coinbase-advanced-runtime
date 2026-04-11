"""Tests for 1h→4h resample helper (Fase 2)."""

from pathlib import Path

import pandas as pd

from src.backtest.data_downloader import MARKET_REGIMES, resample_1h_to_4h


def _write_1h(path: Path, n: int) -> None:
    rows = []
    for i in range(n):
        rows.append(
            {
                "timestamp": 1_000_000 + i * 3600_000,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 10.0,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def test_resample_aggregates_four_bars(tmp_path: Path) -> None:
    src = tmp_path / "in.csv"
    dst = tmp_path / "out.csv"
    _write_1h(src, 8)
    resample_1h_to_4h(src, dst)
    out = pd.read_csv(dst)
    assert len(out) == 2
    assert out.iloc[0]["open"] == 100.0
    assert out.iloc[0]["high"] == 104.0  # max(101,102,103,104)
    assert out.iloc[0]["low"] == 99.0
    assert out.iloc[0]["close"] == 103.5
    assert out.iloc[0]["volume"] == 40.0


def test_resample_drops_incomplete_trailing(tmp_path: Path) -> None:
    src = tmp_path / "in.csv"
    dst = tmp_path / "out.csv"
    _write_1h(src, 6)  # 1 full 4h bar + 2 orphans
    resample_1h_to_4h(src, dst)
    out = pd.read_csv(dst)
    assert len(out) == 1


def test_market_regimes_alias_preserved() -> None:
    from src.backtest.data_downloader import BTC_MARKET_REGIMES

    assert BTC_MARKET_REGIMES is MARKET_REGIMES
    assert len(MARKET_REGIMES) == 10
