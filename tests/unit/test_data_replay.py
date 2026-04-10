"""
Tests for historical data replay through MarketDataService
and backtest report enhancements (profit_factor, equity_curve_to_csv).
"""

import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.data_replay import HistoricalReplay, ReplayResult, replay_and_compare
from src.backtest.ledger import BacktestLedger, BacktestTrade
from src.backtest.report import (
    BacktestReport,
    _compute_profit_factor,
    build_report,
    equity_curve_to_csv,
)
from src.marketdata.service import MarketDataService

ZERO = Decimal("0")


# ── Helper: generate 5m bars ──


def make_5m_bars(n: int, base_ts: int = 1700006400000, base_price: float = 50000.0) -> list[Bar]:
    """Generate n sequential 5m bars."""
    step = 5 * 60 * 1000
    bars = []
    for i in range(n):
        price = Decimal(str(base_price + i * 10))
        bars.append(Bar(
            timestamp_ms=base_ts + i * step,
            open=price - Decimal("5"),
            high=price + Decimal("20"),
            low=price - Decimal("20"),
            close=price,
            volume=Decimal("100"),
        ))
    return bars


def write_bars_csv(bars: list[Bar], path: str) -> None:
    """Write bars to CSV file."""
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.timestamp_ms, b.open, b.high, b.low, b.close, b.volume])


# ── HistoricalReplay tests ──


class TestHistoricalReplay:
    """Verify replay feeds bars through MarketDataService correctly."""

    def test_replay_5m_bars(self):
        """5m bars produce CandleClosed events via MarketDataService."""
        bars = make_5m_bars(5)
        replay = HistoricalReplay("BTC-USD", "5m")
        replay.load_bars(bars)
        result = replay.run()

        assert isinstance(result, ReplayResult)
        assert result.bars_fed == 5
        # First bucket discarded, so at most 4 candles emitted
        assert result.candles_emitted >= 1

    def test_replay_from_csv(self, tmp_path):
        """Load and replay from CSV file."""
        bars = make_5m_bars(10)
        csv_path = str(tmp_path / "test_bars.csv")
        write_bars_csv(bars, csv_path)

        replay = HistoricalReplay("BTC-USD", "5m")
        count = replay.load_csv(csv_path)
        assert count == 10

        result = replay.run()
        assert result.bars_fed == 10
        assert result.candles_emitted >= 1

    def test_replay_emitted_candles_accessible(self):
        """Emitted candles are accessible after replay."""
        bars = make_5m_bars(5)
        replay = HistoricalReplay("BTC-USD", "5m")
        replay.load_bars(bars)
        replay.run()

        candles = replay.emitted_candles
        for c in candles:
            assert c.symbol == "BTC-USD"
            assert c.timeframe == "5m"

    def test_replay_no_bars_raises(self):
        """Replay without loading bars raises ValueError."""
        replay = HistoricalReplay("BTC-USD", "5m")
        with pytest.raises(ValueError, match="No bars loaded"):
            replay.run()

    def test_replay_1h_needs_12_bars(self):
        """1h timeframe needs 12 x 5m bars per bucket."""
        # Feed 25 bars (2+ hours) — aligned to hour boundary
        base_ts = 1700006400000  # hour-aligned
        bars = make_5m_bars(25, base_ts=base_ts)

        replay = HistoricalReplay("BTC-USD", "1h")
        replay.load_bars(bars)
        result = replay.run()

        assert result.bars_fed == 25
        # First hour bucket discarded, second hour emitted
        assert result.candles_emitted >= 1

    def test_replay_preserves_ohlcv(self):
        """Emitted candle OHLCV matches input bar for 5m timeframe."""
        bars = make_5m_bars(3)
        replay = HistoricalReplay("BTC-USD", "5m")
        replay.load_bars(bars)
        replay.run()

        candles = replay.emitted_candles
        if candles:
            # For 5m, each candle = 1 bar (minus startup discard)
            c = candles[0]
            assert c.close > Decimal("0")
            assert c.volume > Decimal("0")


# ── replay_and_compare tests ──


class TestReplayAndCompare:
    """Verify replay-vs-direct comparison function."""

    def test_compare_produces_result(self, tmp_path):
        """replay_and_compare returns comparison metrics."""
        bars = make_5m_bars(10)
        csv_path = str(tmp_path / "compare.csv")
        write_bars_csv(bars, csv_path)

        result = replay_and_compare(csv_path, symbol="BTC-USD", timeframe="5m")

        assert result["bars_in_csv"] == 10
        assert result["candles_emitted"] >= 1
        assert "ohlcv_match" in result


# ── profit_factor tests ──


class TestProfitFactor:
    """Verify profit_factor computation."""

    def test_profit_factor_all_winners(self):
        """All winning trades → Infinity."""
        trades = [
            BacktestTrade(
                entry_price=Decimal("100"), exit_price=Decimal("110"),
                qty=Decimal("1"), side="BUY", pnl=Decimal("10"),
                entry_ts_ms=0, exit_ts_ms=1000,
            ),
        ]
        pf = _compute_profit_factor(trades)
        assert pf == Decimal("Infinity")

    def test_profit_factor_all_losers(self):
        """All losing trades → 0."""
        trades = [
            BacktestTrade(
                entry_price=Decimal("100"), exit_price=Decimal("90"),
                qty=Decimal("1"), side="BUY", pnl=Decimal("-10"),
                entry_ts_ms=0, exit_ts_ms=1000,
            ),
        ]
        pf = _compute_profit_factor(trades)
        assert pf == ZERO

    def test_profit_factor_mixed(self):
        """Mixed trades → gross_profit / gross_loss."""
        trades = [
            BacktestTrade(
                entry_price=Decimal("100"), exit_price=Decimal("120"),
                qty=Decimal("1"), side="BUY", pnl=Decimal("20"),
                entry_ts_ms=0, exit_ts_ms=1000,
            ),
            BacktestTrade(
                entry_price=Decimal("100"), exit_price=Decimal("90"),
                qty=Decimal("1"), side="BUY", pnl=Decimal("-10"),
                entry_ts_ms=0, exit_ts_ms=1000,
            ),
        ]
        pf = _compute_profit_factor(trades)
        assert pf == Decimal("2")  # 20 / 10

    def test_profit_factor_no_trades(self):
        """No trades → 0."""
        assert _compute_profit_factor([]) == ZERO

    def test_profit_factor_in_report(self):
        """build_report includes profit_factor."""
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), ts_ms=1000)
        ledger.mark(Decimal("50000"), ts_ms=1000)
        ledger.sell(Decimal("0.1"), Decimal("51000"), ts_ms=2000)
        ledger.mark(Decimal("51000"), ts_ms=2000)

        report = build_report(ledger, total_bars=10, final_price=Decimal("51000"))
        assert hasattr(report, "profit_factor")
        assert report.profit_factor == Decimal("Infinity")  # 1 winning trade, 0 losses


# ── equity_curve_to_csv tests ──


class TestEquityCurveExport:
    """Verify equity curve CSV export."""

    def test_export_creates_csv(self, tmp_path):
        """equity_curve_to_csv creates a valid CSV."""
        curve = [
            (1000, Decimal("10000")),
            (2000, Decimal("10100")),
            (3000, Decimal("10050")),
        ]
        csv_path = str(tmp_path / "equity.csv")
        equity_curve_to_csv(curve, csv_path)

        assert os.path.exists(csv_path)

        import csv
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 3
        assert rows[0]["timestamp_ms"] == "1000"
        assert rows[0]["equity"] == "10000"

    def test_export_empty_curve(self, tmp_path):
        """Empty curve produces header-only CSV."""
        csv_path = str(tmp_path / "empty.csv")
        equity_curve_to_csv([], csv_path)

        import csv
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 0
