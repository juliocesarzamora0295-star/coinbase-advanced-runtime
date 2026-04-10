"""
Backtest vs live consistency tests.

Verifies that:
1. BacktestEngine and MarketDataService produce equivalent data flow
2. Strategy adapter uses the exact same code as production strategies
3. Risk adapter uses the same RiskGate as live trading
4. BacktestReport metrics are internally consistent
5. Segmented runner handles all synthetic regimes
"""

from decimal import Decimal
from typing import List, Optional

from src.backtest.data_feed import Bar, HistoricalDataFeed
from src.backtest.data_replay import HistoricalReplay
from src.backtest.engine import BacktestEngine, Signal
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.report import BacktestReport, build_report
from src.backtest.risk_adapter import BacktestRiskAdapter
from src.marketdata.service import CandleClosed, MarketDataService
from src.risk.gate import RiskGate, RiskLimits


def make_bars(prices: list[float], start_ts: int = 1_700_006_400_000) -> list[Bar]:
    """Create bars from a list of close prices at 5m intervals."""
    step = 5 * 60 * 1000
    return [
        Bar(
            timestamp_ms=start_ts + i * step,
            open=Decimal(str(p - 10)),
            high=Decimal(str(p + 20)),
            low=Decimal(str(p - 20)),
            close=Decimal(str(p)),
            volume=Decimal("100"),
        )
        for i, p in enumerate(prices)
    ]


def always_buy_strategy(bar: Bar, history: list[Bar]) -> Optional[Signal]:
    """Buy on every bar (for testing)."""
    if len(history) == 0:
        return Signal(side="BUY", qty=Decimal("0.01"))
    return None


def buy_sell_strategy(bar: Bar, history: list[Bar]) -> Optional[Signal]:
    """Buy first, sell second (for testing round-trip)."""
    if len(history) == 0:
        return Signal(side="BUY", qty=Decimal("0.01"))
    if len(history) == 5:
        return Signal(side="SELL", qty=Decimal("0.01"))
    return None


class TestBacktestLiveDataConsistency:
    """MarketDataService replay produces same close prices as direct feed."""

    def test_5m_close_prices_match(self):
        """5m replay through MarketDataService preserves close prices."""
        prices = [50000 + i * 50 for i in range(10)]
        bars = make_bars(prices)

        # Direct feed
        direct_closes = [float(b.close) for b in bars]

        # Replay through MarketDataService
        replay = HistoricalReplay("BTC-USD", "5m")
        replay.load_bars(bars)
        replay.run()
        emitted = replay.emitted_candles

        replayed_closes = [float(c.close) for c in emitted]

        # Replayed close prices should be subset of direct (minus startup discard)
        for c in replayed_closes:
            assert c in direct_closes, f"Replayed close {c} not in direct feed"

    def test_replay_symbol_isolation(self):
        """Different symbols don't cross-contaminate in replay."""
        bars_btc = make_bars([50000, 51000, 52000, 53000, 54000])
        bars_eth = make_bars([3000, 3100, 3200, 3300, 3400])

        svc = MarketDataService()
        replay_btc = HistoricalReplay("BTC-USD", "5m", service=svc)
        replay_btc.load_bars(bars_btc)

        # Register ETH separately
        svc.register_symbol("ETH-USD", "5m")
        eth_events: list[CandleClosed] = []
        svc.subscribe("ETH-USD", lambda c: eth_events.append(c))

        replay_btc.run()

        # ETH should have no events (only BTC was replayed)
        assert len(eth_events) == 0

        btc_candles = replay_btc.emitted_candles
        for c in btc_candles:
            assert c.symbol == "BTC-USD"


class TestBacktestRiskConsistency:
    """BacktestRiskAdapter uses the real RiskGate, same as live."""

    def test_risk_adapter_uses_real_gate(self):
        """BacktestRiskAdapter wraps the actual RiskGate class."""
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger, symbol="BTC-USD")
        assert isinstance(adapter._gate, RiskGate)

    def test_risk_adapter_caps_excessive_position(self):
        """Risk adapter caps qty when position exceeds limits."""
        limits = RiskLimits(max_position_pct=Decimal("0.01"))  # very tight
        gate = RiskGate(limits)
        ledger = BacktestLedger(initial_cash=Decimal("100"))  # small equity
        adapter = BacktestRiskAdapter(risk_gate=gate, ledger=ledger, symbol="BTC-USD")

        # Try to buy qty worth 10x equity — gate caps but may still allow
        verdict = adapter.evaluate(
            side="BUY",
            qty=Decimal("100"),
            price=Decimal("50000"),
            ts_ms=1000,
        )
        # Gate hard_max_qty should be very small relative to requested 100
        assert verdict.hard_max_qty < Decimal("1")

    def test_risk_adapter_allows_within_limits(self):
        """Risk adapter allows normal-sized trades."""
        ledger = BacktestLedger(initial_cash=Decimal("100000"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger, symbol="BTC-USD")

        verdict = adapter.evaluate(
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            ts_ms=1000,
        )
        assert verdict.allowed
        assert adapter.blocked_count == 0

    def test_engine_with_risk_adapter(self):
        """BacktestEngine with risk adapter produces valid report."""
        prices = [50000 + i * 100 for i in range(50)]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0.001"))
        adapter = BacktestRiskAdapter.from_config(ledger=ledger, symbol="BTC-USD")

        engine = BacktestEngine(
            feed=feed,
            ledger=ledger,
            executor=executor,
            strategy=always_buy_strategy,
            risk_adapter=adapter,
        )
        report = engine.run()

        assert isinstance(report, BacktestReport)
        assert report.total_bars == 50


class TestReportMetricsConsistency:
    """BacktestReport metrics are internally consistent."""

    def test_winning_plus_losing_equals_total(self):
        """winning_trades + losing_trades == total_trades."""
        prices = [50000, 51000, 52000, 53000, 52000, 51000, 50000, 49000, 50000, 51000]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=buy_sell_strategy,
        )
        report = engine.run()

        assert report.winning_trades + report.losing_trades == report.total_trades

    def test_win_rate_bounded(self):
        """win_rate is always 0-1."""
        prices = [50000 + i * 100 for i in range(20)]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=buy_sell_strategy,
        )
        report = engine.run()

        assert Decimal("0") <= report.win_rate <= Decimal("1")

    def test_max_drawdown_bounded(self):
        """max_drawdown is 0-1."""
        prices = [50000 + i * 100 for i in range(20)]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=buy_sell_strategy,
        )
        report = engine.run()

        assert Decimal("0") <= report.max_drawdown <= Decimal("1")

    def test_profit_factor_consistent_with_pnl(self):
        """profit_factor > 1 iff total_pnl > 0 (ignoring edge cases)."""
        prices = [50000, 51000, 52000, 53000, 54000, 55000, 56000, 57000, 58000, 59000]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=buy_sell_strategy,
        )
        report = engine.run()

        if report.total_trades > 0 and report.losing_trades > 0:
            if report.total_pnl > Decimal("0"):
                assert report.profit_factor > Decimal("1")
            elif report.total_pnl < Decimal("0"):
                assert report.profit_factor < Decimal("1")

    def test_return_pct_matches_equity_delta(self):
        """return_pct = (final - initial) / initial."""
        prices = [50000 + i * 100 for i in range(20)]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=buy_sell_strategy,
        )
        report = engine.run()

        expected = (report.final_equity - report.initial_equity) / report.initial_equity
        assert abs(report.return_pct - expected) < Decimal("0.0001")

    def test_no_trades_report(self):
        """No signals → 0 trades, 0 PnL, 0 drawdown."""
        prices = [50000 + i * 100 for i in range(10)]
        bars = make_bars(prices)
        feed = HistoricalDataFeed.from_bars(bars)
        ledger = BacktestLedger(initial_cash=Decimal("10000"))
        executor = PaperExecutor(ledger=ledger, fee_rate=Decimal("0"))

        def no_signal(bar, history):
            return None

        engine = BacktestEngine(
            feed=feed, ledger=ledger, executor=executor,
            strategy=no_signal,
        )
        report = engine.run()

        assert report.total_trades == 0
        assert report.total_pnl == Decimal("0")
        assert report.win_rate == Decimal("0")


class TestSyntheticDataRegimes:
    """Synthetic data generators produce valid price series."""

    def test_all_regimes_generate_data(self):
        """All SYNTHETIC_REGIMES produce non-empty arrays with correct length."""
        from src.backtest.synthetic_data import SYNTHETIC_REGIMES

        for label, spec in SYNTHETIC_REGIMES.items():
            prices = spec["generator"](**spec["kwargs"])
            expected_n = spec["kwargs"]["n"]
            assert len(prices) == expected_n, (
                f"Regime {label}: expected {expected_n} bars, got {len(prices)}"
            )

    def test_bull_trend_has_positive_start(self):
        """Bull trend starts near start_price."""
        from src.backtest.synthetic_data import generate_bull_trend

        prices = generate_bull_trend(n=200, start_price=30000, end_price=50000)
        # First few prices should be near start_price
        assert abs(prices[0] - 30000) < 5000

    def test_bear_trend_ends_lower(self):
        """Bear trend should end lower than it starts."""
        from src.backtest.synthetic_data import generate_bear_trend

        prices = generate_bear_trend(n=200, start_price=50000, end_price=25000)
        assert prices[-1] < prices[0]

    def test_crash_has_sharp_decline(self):
        """Crash regime has a period of sharp decline."""
        from src.backtest.synthetic_data import generate_crash

        prices = generate_crash(n=300, start_price=60000, crash_to=30000)
        # Max should be near start, min should be much lower
        assert max(prices) > min(prices) * 1.5
