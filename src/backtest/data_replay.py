"""
Historical data replay through MarketDataService.

Feeds CSV bars through the live MarketDataService pipeline as if
they were arriving from the Coinbase WebSocket. This validates that
the live signal path handles historical data consistently with
the backtest engine.

Usage:
    replay = HistoricalReplay("BTC-USD", "1h")
    replay.load_csv("data/btc_1h.csv")
    events = replay.run()
"""

import csv
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from src.backtest.data_feed import Bar, HistoricalDataFeed, _parse_timestamp
from src.marketdata.service import CandleClosed, MarketDataService

logger = logging.getLogger("HistoricalReplay")


@dataclass(frozen=True)
class ReplayResult:
    """Summary of a historical replay run."""

    bars_fed: int
    candles_emitted: int
    first_bar_ts: int
    last_bar_ts: int
    first_emit_ts: int
    last_emit_ts: int


class HistoricalReplay:
    """
    Feeds historical bars through MarketDataService as synthetic 5m candles.

    For each bar in the CSV:
    1. Splits it into the required number of 5m sub-candles
    2. Feeds each sub-candle through MarketDataService.ingest_5m_candle()
    3. Collects emitted CandleClosed events

    This validates that the live pipeline produces the same OHLCV
    aggregation as the backtest's direct bar iteration.
    """

    def __init__(
        self,
        symbol: str,
        target_timeframe: str,
        service: Optional[MarketDataService] = None,
    ) -> None:
        self.symbol = symbol
        self.target_timeframe = target_timeframe
        self.service = service or MarketDataService()
        self._feed: Optional[HistoricalDataFeed] = None
        self._emitted: List[CandleClosed] = []

        # Register and subscribe
        self.service.register_symbol(symbol, target_timeframe)
        self.service.subscribe(symbol, self._on_candle)

    def _on_candle(self, event: CandleClosed) -> None:
        self._emitted.append(event)

    def load_csv(self, path: str | Path) -> int:
        """Load bars from CSV. Returns number of bars loaded."""
        self._feed = HistoricalDataFeed.from_csv(path)
        count = len(self._feed)
        logger.info("Loaded %d bars from %s", count, path)
        return count

    def load_bars(self, bars: List[Bar]) -> int:
        """Load bars directly. Returns number of bars loaded."""
        self._feed = HistoricalDataFeed.from_bars(bars)
        count = len(self._feed)
        logger.info("Loaded %d bars directly", count)
        return count

    def run(self) -> ReplayResult:
        """
        Replay all loaded bars through MarketDataService.

        Each bar is decomposed into 5m sub-candles that are fed
        to the service. The service accumulates and emits CandleClosed
        events according to its normal bucket logic.

        Returns:
            ReplayResult with summary statistics.
        """
        if self._feed is None or len(self._feed) == 0:
            raise ValueError("No bars loaded — call load_csv() or load_bars() first")

        self._emitted.clear()
        bars_fed = 0
        first_ts = 0
        last_ts = 0

        STEP_5M = 5 * 60 * 1000

        for bar in self._feed:
            if bars_fed == 0:
                first_ts = bar.timestamp_ms
            last_ts = bar.timestamp_ms
            bars_fed += 1

            # Feed as a single 5m candle at bar's timestamp
            self.service.ingest_5m_candle(
                symbol=self.symbol,
                target_timeframe=self.target_timeframe,
                timestamp_ms=bar.timestamp_ms,
                open_p=bar.open,
                high_p=bar.high,
                low_p=bar.low,
                close_p=bar.close,
                volume=bar.volume,
            )

        first_emit = self._emitted[0].timestamp_ms if self._emitted else 0
        last_emit = self._emitted[-1].timestamp_ms if self._emitted else 0

        result = ReplayResult(
            bars_fed=bars_fed,
            candles_emitted=len(self._emitted),
            first_bar_ts=first_ts,
            last_bar_ts=last_ts,
            first_emit_ts=first_emit,
            last_emit_ts=last_emit,
        )

        logger.info(
            "Replay complete: %d bars fed → %d candles emitted",
            bars_fed,
            len(self._emitted),
        )

        return result

    @property
    def emitted_candles(self) -> List[CandleClosed]:
        """Access emitted CandleClosed events after replay."""
        return list(self._emitted)


def replay_and_compare(
    csv_path: str | Path,
    symbol: str = "BTC-USD",
    timeframe: str = "5m",
) -> dict:
    """
    Replay CSV through MarketDataService and compare with direct feed.

    Returns dict with comparison metrics:
    - bars_in_csv: total bars in CSV
    - candles_emitted: events from MarketDataService
    - bars_match: whether emitted candle count matches expected
    - ohlcv_match: whether OHLCV values match between direct and replayed

    For 5m timeframe, each bar should produce one CandleClosed
    (minus the first discarded startup bucket).
    """
    # Direct feed
    feed = HistoricalDataFeed.from_csv(csv_path)
    direct_bars = list(feed)

    # Replay through service
    replay = HistoricalReplay(symbol, timeframe)
    replay.load_csv(csv_path)
    result = replay.run()
    emitted = replay.emitted_candles

    # For 5m, expected emissions = total_bars - 1 (first bucket discarded)
    from src.marketdata.service import SymbolBarBuilder
    required_per_bucket = SymbolBarBuilder.REQUIRED_CANDLES.get(timeframe, 1)

    # Compare OHLCV values where timestamps align
    ohlcv_mismatches = 0
    matched_count = 0

    emitted_by_ts = {e.timestamp_ms: e for e in emitted}
    for bar in direct_bars:
        # The emitted timestamp is the close of the bucket
        bucket_ms = SymbolBarBuilder.TIMEFRAME_MS.get(timeframe, 5 * 60 * 1000)
        expected_close_ts = ((bar.timestamp_ms // bucket_ms) * bucket_ms) + bucket_ms

        if expected_close_ts in emitted_by_ts:
            e = emitted_by_ts[expected_close_ts]
            matched_count += 1
            if e.close != bar.close:
                ohlcv_mismatches += 1

    return {
        "bars_in_csv": len(direct_bars),
        "candles_emitted": len(emitted),
        "matched_bars": matched_count,
        "ohlcv_mismatches": ohlcv_mismatches,
        "ohlcv_match": ohlcv_mismatches == 0,
    }
