"""
Tests para MarketDataService con resampling correcto (alineado a calendario UTC).

Valida:
- Velas 5m se acumulan en buckets calendario
- Buckets alineados a fronteras UTC (ej: 10:00, 11:00 para 1h)
- Timestamp de CIERRE del bucket, no inicio de última vela
- No se emiten velas parciales
"""

import sys
from decimal import Decimal

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.marketdata.service import CandleClosed, MarketDataService, SymbolBarBuilder


class TestSymbolBarBuilderCalendar:
    """Tests para SymbolBarBuilder con alineación calendario."""

    def test_builder_aligns_to_calendar_boundary(self):
        """
        Builder debe alinear buckets a fronteras calendario.

        P0 FIX: El primer bucket parcial se descarta.
        Ej: para 1h, si arrancas a 10:05, el bucket 10:00-11:00 se descarta.
        """
        builder = SymbolBarBuilder("BTC-USD", "1h")

        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        # Ingerir velas 5m desde 10:05 hasta 10:55 (11 velas, falta 10:00)
        for i in range(1, 12):  # 10:05, 10:10, ..., 10:55
            candle_ts = base_ts + i * 5 * 60 * 1000
            candle = {
                "timestamp_ms": candle_ts,
                "open": Decimal("50000"),
                "high": Decimal("50100"),
                "low": Decimal("49900"),
                "close": Decimal("50050"),
                "volume": Decimal("1.0"),
            }
            result = builder.ingest_5m_candle_with_accumulation(candle)
            # Ninguna debe emitir todavía (bucket 10:00 incompleto)
            assert result == [], f"Should not emit at {i}th candle"

        # Ingerir velas desde 11:00 (inicio de nuevo bucket)
        # Esto trigger el bucket 10:00, pero se descarta porque está incompleto
        candle_ts = base_ts + 12 * 5 * 60 * 1000  # 11:00:00
        candle = {
            "timestamp_ms": candle_ts,
            "open": Decimal("50050"),
            "high": Decimal("50200"),
            "low": Decimal("50000"),
            "close": Decimal("50100"),
            "volume": Decimal("1.5"),
        }
        result = builder.ingest_5m_candle_with_accumulation(candle)

        # P0 FIX: El bucket 10:00 está incompleto (falta 10:00), se descarta
        assert len(result) == 0, f"Expected 0 events (partial bucket discarded), got {len(result)}"

    def test_builder_emits_correct_ohlcv(self):
        """El CandleClosed emitido debe tener OHLCV correcto del bucket."""
        builder = SymbolBarBuilder("BTC-USD", "15m")

        # Bucket 10:00-10:15 (15m)
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        candles_5m = [
            {"ts": base_ts, "o": 100, "h": 110, "l": 95, "c": 105, "v": 1},
            {"ts": base_ts + 300000, "o": 105, "h": 115, "l": 100, "c": 110, "v": 2},
            {"ts": base_ts + 600000, "o": 110, "h": 120, "l": 105, "c": 115, "v": 3},
        ]

        for c in candles_5m:
            candle = {
                "timestamp_ms": c["ts"],
                "open": Decimal(str(c["o"])),
                "high": Decimal(str(c["h"])),
                "low": Decimal(str(c["l"])),
                "close": Decimal(str(c["c"])),
                "volume": Decimal(str(c["v"])),
            }
            result = builder.ingest_5m_candle_with_accumulation(candle)

        # La última vela (10:10) no debería emitir todavía
        # Necesitamos una vela del siguiente bucket para trigger
        next_bucket_candle = {
            "timestamp_ms": base_ts + 900000,  # 10:15:00 - inicio siguiente bucket
            "open": Decimal("115"),
            "high": Decimal("116"),
            "low": Decimal("114"),
            "close": Decimal("115"),
            "volume": Decimal("1"),
        }
        result = builder.ingest_5m_candle_with_accumulation(next_bucket_candle)

        assert len(result) == 1, f"Expected 1 event, got {len(result)}"
        emitted = result[0]

        # OHLCV del bucket 10:00-10:15:
        # open = primera vela.open = 100
        # high = max de todas = 120
        # low = min de todas = 95
        # close = última vela.close = 115
        # volume = sum = 6
        assert emitted.open == Decimal("100")
        assert emitted.high == Decimal("120")
        assert emitted.low == Decimal("95")
        assert emitted.close == Decimal("115")
        assert emitted.volume == Decimal("6")

        # Timestamp de cierre debe ser 10:15:00
        assert emitted.timestamp_ms == base_ts + 15 * 60 * 1000

    def test_builder_ignores_old_candles(self):
        """Builder debe ignorar velas viejas (antes del último cierre emitido)."""
        builder = SymbolBarBuilder("BTC-USD", "1h")

        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        # Completar primer bucket 10:00-11:00
        for i in range(12):
            candle = {
                "timestamp_ms": base_ts + i * 5 * 60 * 1000,
                "open": Decimal("50000"),
                "high": Decimal("50100"),
                "low": Decimal("49900"),
                "close": Decimal("50050"),
                "volume": Decimal("1.0"),
            }
            builder.ingest_5m_candle_with_accumulation(candle)

        # Trigger con vela del siguiente bucket
        next_candle = {
            "timestamp_ms": base_ts + 12 * 5 * 60 * 1000,  # 11:00:00
            "open": Decimal("50050"),
            "high": Decimal("50100"),
            "low": Decimal("49900"),
            "close": Decimal("50050"),
            "volume": Decimal("1.0"),
        }
        builder.ingest_5m_candle_with_accumulation(next_candle)

        # Intentar ingerir vela vieja del bucket 10:00
        old_candle = {
            "timestamp_ms": base_ts + 2 * 5 * 60 * 1000,  # 10:10:00 - ya pasó
            "open": Decimal("49000"),
            "high": Decimal("49100"),
            "low": Decimal("48900"),
            "close": Decimal("49050"),
            "volume": Decimal("1.0"),
        }
        result = builder.ingest_5m_candle_with_accumulation(old_candle)
        assert result == [], "Should ignore old candle from already-closed bucket"

    def test_builder_handles_gaps(self):
        """Builder debe manejar gaps en datos (velas faltantes)."""
        builder = SymbolBarBuilder("BTC-USD", "15m")

        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        # Vela 1: 10:00
        builder.ingest_5m_candle_with_accumulation(
            {
                "timestamp_ms": base_ts,
                "open": Decimal("50000"),
                "high": Decimal("50100"),
                "low": Decimal("49900"),
                "close": Decimal("50050"),
                "volume": Decimal("1.0"),
            }
        )

        # Gap: falta 10:05
        # Vela 2: 10:10 (con gap de 5 min)
        builder.ingest_5m_candle_with_accumulation(
            {
                "timestamp_ms": base_ts + 2 * 5 * 60 * 1000,  # 10:10
                "open": Decimal("50050"),
                "high": Decimal("50100"),
                "low": Decimal("49900"),
                "close": Decimal("50050"),
                "volume": Decimal("1.0"),
            }
        )

        # Vela 3: 10:15 (trigger de bucket incompleto)
        result = builder.ingest_5m_candle_with_accumulation(
            {
                "timestamp_ms": base_ts + 3 * 5 * 60 * 1000,  # 10:15
                "open": Decimal("50050"),
                "high": Decimal("50100"),
                "low": Decimal("49900"),
                "close": Decimal("50050"),
                "volume": Decimal("1.0"),
            }
        )

        # P0 FIX: El bucket está incompleto (solo 2 de 3 velas), se descarta
        assert (
            len(result) == 0
        ), f"Expected 0 events (incomplete bucket with gap), got {len(result)}"


class TestMarketDataService:
    """Tests para MarketDataService (integración)."""

    def setup_method(self):
        self.service = MarketDataService()
        self.emitted_events = []

    def callback(self, event: CandleClosed):
        self.emitted_events.append(event)

    def test_service_emits_1h_after_bucket_complete(self):
        """Service debe emitir CandleClosed 1h cuando el bucket calendario está completo."""
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        self.service.register_symbol("BTC-USD", "1h")
        self.service.subscribe("BTC-USD", self.callback)

        # Ingerir velas 5m desde 10:00 hasta 10:55 (12 velas)
        for i in range(12):
            self.service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )

        # Nada emitido todavía (bucket 10:00-11:00 incompleto sin trigger)
        assert len(self.emitted_events) == 0

        # Trigger con vela del siguiente bucket (11:00)
        self.service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="1h",
            timestamp_ms=base_ts + 12 * 5 * 60 * 1000,  # 11:00:00
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )

        # Ahora debe haber emitido el bucket 10:00-11:00
        assert len(self.emitted_events) == 1, f"Expected 1 event, got {len(self.emitted_events)}"
        assert self.emitted_events[0].timeframe == "1h"
        assert self.emitted_events[0].symbol == "BTC-USD"
        # Cierre debe ser 11:00:00
        assert self.emitted_events[0].timestamp_ms == base_ts + 3600 * 1000

    def test_service_different_timeframes(self):
        """Service debe manejar diferentes timeframes correctamente."""
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        self.service.register_symbol("BTC-USD", "15m")  # 3 velas 5m por bucket
        self.service.register_symbol("ETH-USD", "30m")  # 6 velas 5m por bucket

        btc_events = []
        eth_events = []

        self.service.subscribe("BTC-USD", lambda e: btc_events.append(e))
        self.service.subscribe("ETH-USD", lambda e: eth_events.append(e))

        # Ingerir 6 velas para ambos
        # BTC: velas 0,1,2 → bucket 10:00-10:15; velas 3,4,5 → bucket 10:15-10:30
        # ETH: velas 0-5 → bucket 10:00-10:30
        for i in range(6):
            self.service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="15m",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )
            self.service.ingest_5m_candle(
                symbol="ETH-USD",
                target_timeframe="30m",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("3000"),
                high_p=Decimal("3010"),
                low_p=Decimal("2990"),
                close_p=Decimal("3005"),
                volume=Decimal("10.0"),
            )

        # BTC: vela 3 (10:15) trigger bucket 10:00-10:15 → 1 evento emitido
        # ETH: todavía en bucket 10:00-10:30 → 0 eventos
        assert len(btc_events) == 1, f"Expected 1 BTC event after 6 candles, got {len(btc_events)}"
        assert len(eth_events) == 0, f"Expected 0 ETH events after 6 candles, got {len(eth_events)}"

        # Trigger con velas del siguiente bucket (10:30)
        self.service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 6 * 5 * 60 * 1000,  # 10:30
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )
        self.service.ingest_5m_candle(
            symbol="ETH-USD",
            target_timeframe="30m",
            timestamp_ms=base_ts + 6 * 5 * 60 * 1000,  # 10:30
            open_p=Decimal("3005"),
            high_p=Decimal("3010"),
            low_p=Decimal("2990"),
            close_p=Decimal("3005"),
            volume=Decimal("10.0"),
        )

        # BTC: ahora tiene 2 eventos (10:00-10:15 y 10:15-10:30)
        assert len(btc_events) == 2, f"Expected 2 BTC events, got {len(btc_events)}"
        assert all(e.timeframe == "15m" for e in btc_events)

        # ETH: ahora tiene 1 evento (10:00-10:30)
        assert len(eth_events) == 1, f"Expected 1 ETH event, got {len(eth_events)}"
        assert eth_events[0].timeframe == "30m"

    def test_service_isolates_symbols(self):
        """Service debe aislar símbolos (no mezclar históricos)."""
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        self.service.register_symbol("BTC-USD", "15m")
        self.service.register_symbol("ETH-USD", "15m")

        btc_events = []
        eth_events = []

        self.service.subscribe("BTC-USD", lambda e: btc_events.append(e))
        self.service.subscribe("ETH-USD", lambda e: eth_events.append(e))

        # Ingerir solo velas BTC
        for i in range(3):
            self.service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="15m",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )

        # Nada emitido todavía
        assert len(btc_events) == 0
        assert len(eth_events) == 0

        # Trigger con vela del siguiente bucket
        self.service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 3 * 5 * 60 * 1000,  # 10:15
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )

        # Solo BTC debe haber emitido
        assert len(btc_events) == 1, f"Expected 1 BTC event, got {len(btc_events)}"
        assert len(eth_events) == 0, f"Expected 0 ETH events, got {len(eth_events)}"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
