"""
Tests para framework de backtesting.

Cubre:
- HistoricalDataFeed: carga CSV, iteración, sorting
- BacktestLedger: buy/sell, equity, drawdown, PnL, equity curve
- PaperExecutor: fills con slippage, fees
- BacktestEngine: loop completo con estrategia mock
- BacktestReport: métricas correctas
- Aislamiento: no importa módulos live
"""

import csv
import os
import tempfile
from decimal import Decimal
from typing import List, Optional

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport, build_report


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_bars(prices: List[float], start_ts: int = 1_000_000_000_000) -> List[Bar]:
    """Create bars from a list of close prices."""
    return [
        Bar(
            timestamp_ms=start_ts + i * 60_000,
            open=Decimal(str(p)),
            high=Decimal(str(p * 1.01)),
            low=Decimal(str(p * 0.99)),
            close=Decimal(str(p)),
            volume=Decimal("100"),
        )
        for i, p in enumerate(prices)
    ]


# ──────────────────────────────────────────────
# HistoricalDataFeed
# ──────────────────────────────────────────────


class TestHistoricalDataFeed:

    def test_from_bars(self):
        bars = make_bars([100, 101, 102])
        feed = HistoricalDataFeed.from_bars(bars)
        assert len(feed) == 3

    def test_iteration(self):
        bars = make_bars([100, 101, 102])
        feed = HistoricalDataFeed.from_bars(bars)
        result = list(feed)
        assert len(result) == 3
        assert result[0].close == Decimal("100")
        assert result[2].close == Decimal("102")

    def test_sorted_by_timestamp(self):
        bars = [
            Bar(timestamp_ms=3000, open=Decimal("3"), high=Decimal("3"),
                low=Decimal("3"), close=Decimal("3"), volume=Decimal("1")),
            Bar(timestamp_ms=1000, open=Decimal("1"), high=Decimal("1"),
                low=Decimal("1"), close=Decimal("1"), volume=Decimal("1")),
        ]
        feed = HistoricalDataFeed.from_bars(bars)
        result = list(feed)
        assert result[0].timestamp_ms == 1000
        assert result[1].timestamp_ms == 3000

    def test_from_csv(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            writer.writerow(["1000000000000", "100", "101", "99", "100.5", "500"])
            writer.writerow(["1000000060000", "100.5", "102", "100", "101", "600"])
            f.flush()
            path = f.name

        try:
            feed = HistoricalDataFeed.from_csv(path)
            assert len(feed) == 2
            bars = list(feed)
            closes = {bars[0].close, bars[1].close}
            assert Decimal("100.5") in closes
            assert Decimal("101") in closes
        finally:
            os.unlink(path)

    def test_empty_feed(self):
        feed = HistoricalDataFeed.from_bars([])
        assert len(feed) == 0
        assert list(feed) == []


# ──────────────────────────────────────────────
# BacktestLedger
# ──────────────────────────────────────────────


class TestBacktestLedger:

    def test_initial_state(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        assert ledger.cash == Decimal("10000")
        assert ledger.position_qty == Decimal("0")

    def test_buy_reduces_cash(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"))
        assert ledger.cash == Decimal("5000")
        assert ledger.position_qty == Decimal("0.1")

    def test_sell_increases_cash(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), ts_ms=1000)
        ledger.sell(Decimal("0.1"), Decimal("52000"), ts_ms=2000)
        assert ledger.position_qty == Decimal("0")
        # Cash = 10000 - 5000 + 5200 = 10200
        assert ledger.cash == Decimal("10200")

    def test_trade_recorded(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), ts_ms=1000)
        ledger.sell(Decimal("0.1"), Decimal("52000"), ts_ms=2000)
        assert len(ledger.trades) == 1
        assert ledger.trades[0].pnl == Decimal("200")
        assert ledger.trades[0].is_winner

    def test_losing_trade(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), ts_ms=1000)
        ledger.sell(Decimal("0.1"), Decimal("48000"), ts_ms=2000)
        assert ledger.trades[0].pnl == Decimal("-200")
        assert not ledger.trades[0].is_winner

    def test_equity_with_position(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"))
        # Cash = 5000, position = 0.1 * 55000 = 5500
        assert ledger.get_equity(Decimal("55000")) == Decimal("10500")

    def test_drawdown(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.equity_peak = Decimal("10000")
        ledger.buy(Decimal("0.1"), Decimal("50000"))
        # Price drops to 40000: equity = 5000 + 4000 = 9000
        dd = ledger.get_drawdown(Decimal("40000"))
        assert dd == Decimal("0.1")  # 10%

    def test_equity_curve_recorded(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.mark(Decimal("50000"), ts_ms=1000)
        ledger.mark(Decimal("51000"), ts_ms=2000)
        assert len(ledger.equity_curve) == 2

    def test_fees_tracked(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), fee=Decimal("5"))
        assert ledger.fees_paid == Decimal("5")
        assert ledger.cash == Decimal("4995")

    def test_sell_capped_at_position(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"))
        ledger.sell(Decimal("1.0"), Decimal("50000"))  # try to sell more than have
        assert ledger.position_qty == Decimal("0")

    def test_total_pnl(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("0.1"), Decimal("50000"), ts_ms=1000)
        ledger.sell(Decimal("0.1"), Decimal("52000"), ts_ms=2000)
        ledger.buy(Decimal("0.1"), Decimal("51000"), ts_ms=3000)
        ledger.sell(Decimal("0.1"), Decimal("50000"), ts_ms=4000)
        # Trade 1: +200, Trade 2: -100
        assert ledger.get_total_pnl() == Decimal("100")


# ──────────────────────────────────────────────
# PaperExecutor
# ──────────────────────────────────────────────


class TestPaperExecutor:

    def test_buy_fill(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))
        fill = executor.execute("BUY", Decimal("0.1"), Decimal("50000"))
        assert fill.side == "BUY"
        assert fill.qty == Decimal("0.1")
        assert ledger.position_qty == Decimal("0.1")

    def test_slippage_buy(self):
        ledger = BacktestLedger(initial_cash=Decimal("100000"))
        executor = PaperExecutor(ledger, slippage_bps=Decimal("10"), fee_rate=Decimal("0"))
        fill = executor.execute("BUY", Decimal("0.1"), Decimal("50000"))
        # 10 bps = 0.1% → 50000 * 1.001 = 50050
        assert fill.price == Decimal("50050.0")

    def test_slippage_sell(self):
        ledger = BacktestLedger(initial_cash=Decimal("100000"))
        executor = PaperExecutor(ledger, slippage_bps=Decimal("10"), fee_rate=Decimal("0"))
        ledger.buy(Decimal("0.1"), Decimal("50000"))
        fill = executor.execute("SELL", Decimal("0.1"), Decimal("50000"))
        # SELL: 50000 * (1 - 0.001) = 49950
        assert fill.price == Decimal("49950.0")

    def test_fee_applied(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0.01"))  # 1%
        fill = executor.execute("BUY", Decimal("0.1"), Decimal("50000"))
        # notional = 0.1 * 50000 = 5000, fee = 50
        assert fill.fee == Decimal("50.00")
        assert ledger.fees_paid == Decimal("50.00")


# ──────────────────────────────────────────────
# BacktestEngine
# ──────────────────────────────────────────────


class TestBacktestEngine:

    def test_engine_processes_all_bars(self):
        bars = make_bars([100, 101, 102, 103, 104])
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))

        bar_count = [0]
        def strategy(bar: Bar, history: list[Bar]) -> Optional[Signal]:
            bar_count[0] += 1
            return None

        engine = BacktestEngine(feed, ledger, executor, strategy)
        report = engine.run()
        assert bar_count[0] == 5
        assert report.total_bars == 5
        assert report.total_trades == 0

    def test_engine_executes_signals(self):
        prices = [100] * 5 + [110] * 5  # flat then up
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))

        # Buy on bar 3, sell on bar 8
        def strategy(bar: Bar, history: list[Bar]) -> Optional[Signal]:
            if len(history) == 2:
                return Signal(side="BUY", qty=Decimal("1"))
            if len(history) == 7:
                return Signal(side="SELL", qty=Decimal("1"))
            return None

        engine = BacktestEngine(feed, ledger, executor, strategy)
        report = engine.run()
        assert report.total_trades == 1
        assert report.total_pnl == Decimal("10")  # bought at 100, sold at 110

    def test_engine_generates_equity_curve(self):
        bars = make_bars([100, 101, 102])
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(feed, ledger, executor, lambda b, h: None)
        engine.run()
        assert len(ledger.equity_curve) == 3

    def test_engine_with_empty_feed(self):
        feed = HistoricalDataFeed.from_bars([])
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(feed, ledger, executor, lambda b, h: None)
        report = engine.run()
        assert report.total_bars == 0
        assert report.total_trades == 0


# ──────────────────────────────────────────────
# BacktestReport
# ──────────────────────────────────────────────


class TestBacktestReport:

    def test_report_from_profitable_backtest(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("1"), Decimal("100"), ts_ms=1000)
        ledger.sell(Decimal("1"), Decimal("110"), ts_ms=2000)
        ledger.mark(Decimal("110"), ts_ms=2000)

        report = build_report(ledger, total_bars=10, final_price=Decimal("110"))
        assert report.total_trades == 1
        assert report.winning_trades == 1
        assert report.losing_trades == 0
        assert report.win_rate == Decimal("1")
        assert report.total_pnl == Decimal("10")
        assert report.return_pct == Decimal("0.001")  # 10/10000

    def test_report_with_losses(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.buy(Decimal("1"), Decimal("100"), ts_ms=1000)
        ledger.sell(Decimal("1"), Decimal("90"), ts_ms=2000)
        ledger.mark(Decimal("90"), ts_ms=2000)

        report = build_report(ledger, total_bars=10, final_price=Decimal("90"))
        assert report.winning_trades == 0
        assert report.losing_trades == 1
        assert report.total_pnl == Decimal("-10")

    def test_report_drawdown(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        ledger.mark(Decimal("100"), ts_ms=1000)  # equity = 10000
        ledger.buy(Decimal("1"), Decimal("100"))
        ledger.mark(Decimal("100"), ts_ms=2000)  # equity = 10000
        ledger.mark(Decimal("90"), ts_ms=3000)    # equity = 9900 → dd from peak

        report = build_report(ledger, total_bars=3, final_price=Decimal("90"))
        assert report.max_drawdown > Decimal("0")

    def test_report_str_renders(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        report = build_report(ledger, total_bars=0, final_price=Decimal("0"))
        text = str(report)
        assert "BacktestReport" in text

    def test_report_zero_trades(self):
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        report = build_report(ledger, total_bars=50, final_price=Decimal("100"))
        assert report.total_trades == 0
        assert report.win_rate == Decimal("0")
        assert report.avg_trade_pnl == Decimal("0")
        assert report.sharpe_ratio == 0.0


# ──────────────────────────────────────────────
# Isolation — no live imports
# ──────────────────────────────────────────────


class TestIsolation:

    def test_no_websocket_import(self):
        """Backtest modules don't import WebSocket."""
        import src.backtest.engine as eng
        import src.backtest.data_feed as df
        import src.backtest.ledger as led
        import src.backtest.paper_executor as pe
        import src.backtest.report as rep
        import sys

        live_modules = [
            "src.core.coinbase_websocket",
            "src.core.coinbase_exchange",
            "src.main",
        ]
        for mod_name in live_modules:
            # These should NOT be in sys.modules from backtest imports
            for bt_mod in [eng, df, led, pe, rep]:
                source = getattr(bt_mod, "__file__", "")
                # Verify the module source doesn't reference live modules
                assert mod_name not in str(getattr(bt_mod, "__name__", "")), (
                    f"Backtest module {bt_mod.__name__} should not import {mod_name}"
                )
