"""
Tests para descarte de buckets parciales (P0 FIX).

Valida que el primer bucket parcial (arranque a mitad de hora) se descarte.
"""

import sys
from decimal import Decimal

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.marketdata.service import CandleClosed, MarketDataService


class TestPartialBucketDiscard:
    """Tests para descarte de buckets parciales."""

    def test_partial_startup_bucket_is_discarded(self):
        """
        P0 FIX: Arrancar a 10:05 no debe emitir bucket 10:00-11:00 parcial.

        Si arrancamos a mitad de bucket, el primer bucket debe descartarse
        porque no tenemos todas las velas 5m.
        """
        service = MarketDataService()
        events = []

        def handler(candle: CandleClosed):
            events.append(candle)

        service.register_symbol("BTC-USD", "1h")
        service.subscribe("BTC-USD", handler)

        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        # Ingerir velas desde 10:05 (falta la vela 10:00)
        for i in range(1, 12):  # 10:05, 10:10, ..., 10:55 (11 velas)
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )

        # Trigger con vela del siguiente bucket (11:00)
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="1h",
            timestamp_ms=base_ts + 12 * 5 * 60 * 1000,  # 11:00
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )

        # El bucket 10:00-11:00 está incompleto (falta 10:00), debe descartarse
        assert len(events) == 0, f"Expected 0 events (partial bucket discarded), got {len(events)}"

    def test_complete_bucket_after_partial_is_emitted(self):
        """
        P0 FIX: Después de descartar el primer bucket parcial,
        los buckets completos subsiguientes deben emitirse.
        """
        service = MarketDataService()
        events = []

        def handler(candle: CandleClosed):
            events.append(candle)

        service.register_symbol("BTC-USD", "1h")
        service.subscribe("BTC-USD", handler)

        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC

        # Ingerir velas desde 10:05 (bucket 10:00-11:00 incompleto)
        for i in range(1, 12):
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )

        # Trigger bucket 10:00-11:00 (descartado por incompleto)
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="1h",
            timestamp_ms=base_ts + 12 * 5 * 60 * 1000,  # 11:00
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )

        assert len(events) == 0  # Primer bucket descartado

        # Ahora ingerir todas las velas del bucket 11:00-12:00 (completo)
        for i in range(12, 24):  # 11:00, 11:05, ..., 11:55 (12 velas)
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="1h",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50100"),
                high_p=Decimal("50200"),
                low_p=Decimal("50000"),
                close_p=Decimal("50150"),
                volume=Decimal("2.0"),
            )

        # Trigger bucket 11:00-12:00
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="1h",
            timestamp_ms=base_ts + 24 * 5 * 60 * 1000,  # 12:00
            open_p=Decimal("50150"),
            high_p=Decimal("50200"),
            low_p=Decimal("50100"),
            close_p=Decimal("50150"),
            volume=Decimal("2.0"),
        )

        # El bucket 11:00-12:00 está completo, debe emitirse
        assert len(events) == 1, f"Expected 1 event, got {len(events)}"
        assert events[0].timestamp_ms == base_ts + 2 * 3600 * 1000  # 12:00


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
