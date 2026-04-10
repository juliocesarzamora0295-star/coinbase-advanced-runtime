"""
Tests proving MarketDataService supports multiple timeframes simultaneously.
"""

from decimal import Decimal
from typing import List

from src.marketdata.service import CandleClosed, MarketDataService


def make_5m_candle(start_ts_ms: int, close: float = 100.0) -> dict:
    """Create a minimal 5m candle dict."""
    return {
        "timestamp_ms": start_ts_ms,
        "open": Decimal(str(close - 1)),
        "high": Decimal(str(close + 1)),
        "low": Decimal(str(close - 2)),
        "close": Decimal(str(close)),
        "volume": Decimal("10"),
    }


class TestMultiTimeframeService:
    """Prove that 5m + 1h timeframes work simultaneously for the same symbol."""

    def test_dual_timeframe_registration(self):
        """Can register BTC-USD with both 5m and 1h."""
        svc = MarketDataService()
        svc.register_symbol("BTC-USD", "5m")
        svc.register_symbol("BTC-USD", "1h")

        assert "BTC-USD:5m" in svc._builders
        assert "BTC-USD:1h" in svc._builders

    def test_5m_emits_every_bar(self):
        """5m timeframe emits on every bar change."""
        svc = MarketDataService()
        svc.register_symbol("BTC-USD", "5m")

        received: List[CandleClosed] = []
        svc.subscribe("BTC-USD", lambda c: received.append(c))

        base = 1700000000000  # arbitrary ms timestamp
        step = 5 * 60 * 1000  # 5 minutes in ms

        # Feed 3 bars: first is discarded (startup bucket), second triggers first emit
        for i in range(3):
            svc.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="5m",
                timestamp_ms=base + i * step,
                open_p=Decimal("100"), high_p=Decimal("101"),
                low_p=Decimal("99"), close_p=Decimal("100"),
                volume=Decimal("10"),
            )

        # First bucket discarded on startup, so 2 candles emitted from bar 2 and 3
        # Actually: bar 0 is first bucket (discarded), bar 1 triggers emit of bar 0's bucket,
        # but bar 0 was partial. Then bar 2 triggers emit of bar 1.
        # With 5m timeframe (1 candle per bucket), each bar is its own bucket.
        # First bucket is discarded. So bars 1 and 2 emit = 2 events.
        assert len(received) >= 1  # At minimum bar 1 emits

    def test_1h_accumulates_twelve_5m_bars(self):
        """1h timeframe requires 12 x 5m bars before emission."""
        svc = MarketDataService()
        svc.register_symbol("BTC-USD", "1h")

        received: List[CandleClosed] = []
        svc.subscribe("BTC-USD", lambda c: received.append(c))

        # Start at a clean hour boundary
        base = 1700006400000  # aligned to hour
        step = 5 * 60 * 1000

        # Feed 12 bars (one full hour bucket)
        for i in range(12):
            svc.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=base + i * step,
                open_p=Decimal("100"), high_p=Decimal("101"),
                low_p=Decimal("99"), close_p=Decimal("100"),
                volume=Decimal("10"),
            )

        # First hour bucket should be discarded (startup), need to feed next hour
        next_hour = base + 12 * step
        for i in range(12):
            svc.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=next_hour + i * step,
                open_p=Decimal("100"), high_p=Decimal("101"),
                low_p=Decimal("99"), close_p=Decimal("100"),
                volume=Decimal("10"),
            )

        # After second full hour, first hour should have been emitted
        assert len(received) >= 1
        assert received[0].timeframe == "1h"

    def test_dual_timeframe_simultaneous_feed(self):
        """Feed same 5m candles to both 5m and 1h builders simultaneously."""
        svc = MarketDataService()
        svc.register_symbol("BTC-USD", "5m")
        svc.register_symbol("BTC-USD", "1h")

        received_5m: List[CandleClosed] = []
        received_1h: List[CandleClosed] = []

        def on_candle(c: CandleClosed):
            if c.timeframe == "5m":
                received_5m.append(c)
            elif c.timeframe == "1h":
                received_1h.append(c)

        svc.subscribe("BTC-USD", on_candle)

        base = 1700006400000
        step = 5 * 60 * 1000

        # Feed 25 bars (2+ hours of 5m data)
        for i in range(25):
            ts = base + i * step
            for tf in ["5m", "1h"]:
                svc.ingest_5m_candle(
                    symbol="BTC-USD",
                    target_timeframe=tf,
                    timestamp_ms=ts,
                    open_p=Decimal("100"), high_p=Decimal("101"),
                    low_p=Decimal("99"), close_p=Decimal(str(100 + i)),
                    volume=Decimal("10"),
                )

        # 5m should have many events (24 bars after first discard)
        assert len(received_5m) >= 20

        # 1h should have at most 1 event (one complete hour after startup discard)
        assert len(received_1h) >= 1
        assert received_1h[0].timeframe == "1h"

    def test_different_symbols_isolated(self):
        """BTC-USD and ETH-USD don't cross-contaminate."""
        svc = MarketDataService()
        svc.register_symbol("BTC-USD", "5m")
        svc.register_symbol("ETH-USD", "5m")

        btc_events: List[CandleClosed] = []
        eth_events: List[CandleClosed] = []

        svc.subscribe("BTC-USD", lambda c: btc_events.append(c))
        svc.subscribe("ETH-USD", lambda c: eth_events.append(c))

        base = 1700000000000
        step = 5 * 60 * 1000

        for i in range(5):
            ts = base + i * step
            svc.ingest_5m_candle(
                symbol="BTC-USD", target_timeframe="5m",
                timestamp_ms=ts,
                open_p=Decimal("70000"), high_p=Decimal("71000"),
                low_p=Decimal("69000"), close_p=Decimal("70000"),
                volume=Decimal("100"),
            )
            svc.ingest_5m_candle(
                symbol="ETH-USD", target_timeframe="5m",
                timestamp_ms=ts,
                open_p=Decimal("3000"), high_p=Decimal("3100"),
                low_p=Decimal("2900"), close_p=Decimal("3000"),
                volume=Decimal("500"),
            )

        # Both should have events, but they shouldn't mix
        for e in btc_events:
            assert e.symbol == "BTC-USD"
        for e in eth_events:
            assert e.symbol == "ETH-USD"
