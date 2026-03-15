"""
Tests para upsert de velas 5m (P0 FIX).

Valida que updates frecuentes del mismo candle no dupliquen datos.
"""
import sys
from decimal import Decimal

sys.path.insert(0, '/mnt/okcomputer/output/fortress_v4')

from src.marketdata.service import MarketDataService, CandleClosed


class TestMarketDataUpsert:
    """Tests para upsert de velas 5m."""
    
    def test_duplicate_5m_updates_do_not_inflate_volume(self):
        """
        P0 FIX: Tres updates del mismo candle 5m no deben triplicar el volumen.
        
        Coinbase manda actualizaciones cada segundo sobre el mismo bucket 5m.
        El builder debe hacer upsert por timestamp, no append.
        """
        service = MarketDataService()
        events = []
        
        def handler(candle: CandleClosed):
            events.append(candle)
        
        service.register_symbol("BTC-USD", "15m")
        service.subscribe("BTC-USD", handler)
        
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC
        
        # Misma vela 5m (10:00) recibida 3 veces con diferentes valores
        for _ in range(3):
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="15m",
                timestamp_ms=base_ts,  # Mismo timestamp
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),  # Mismo volumen
            )
        
        # Completar bucket con otras 2 velas
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 5 * 60 * 1000,  # 10:05
            open_p=Decimal("50050"),
            high_p=Decimal("50200"),
            low_p=Decimal("50000"),
            close_p=Decimal("50100"),
            volume=Decimal("2.0"),
        )
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 10 * 60 * 1000,  # 10:10
            open_p=Decimal("50100"),
            high_p=Decimal("50300"),
            low_p=Decimal("50050"),
            close_p=Decimal("50200"),
            volume=Decimal("3.0"),
        )
        
        # Trigger con vela del siguiente bucket
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 15 * 60 * 1000,  # 10:15
            open_p=Decimal("50200"),
            high_p=Decimal("50400"),
            low_p=Decimal("50100"),
            close_p=Decimal("50300"),
            volume=Decimal("4.0"),
        )
        
        # Debe haber emitido exactamente 1 evento
        assert len(events) == 1, f"Expected 1 event, got {len(events)}"
        
        # El volumen debe ser 1+2+3=6, no 1+1+1+2+3=8
        assert events[0].volume == Decimal("6"), f"Expected volume=6, got {events[0].volume}"
    
    def test_5m_update_overwrites_previous(self):
        """
        P0 FIX: Un update posterior de la misma vela 5m debe sobrescribir la anterior.
        """
        service = MarketDataService()
        events = []
        
        def handler(candle: CandleClosed):
            events.append(candle)
        
        service.register_symbol("BTC-USD", "15m")
        service.subscribe("BTC-USD", handler)
        
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC
        
        # Primera versión de la vela 10:00
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts,
            open_p=Decimal("50000"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )
        
        # Update de la misma vela con valores diferentes
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts,
            open_p=Decimal("50000"),
            high_p=Decimal("50200"),  # Nuevo high
            low_p=Decimal("49800"),   # Nuevo low
            close_p=Decimal("50100"), # Nuevo close
            volume=Decimal("2.0"),    # Nuevo volumen
        )
        
        # Completar bucket
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 5 * 60 * 1000,
            open_p=Decimal("50100"),
            high_p=Decimal("50200"),
            low_p=Decimal("50000"),
            close_p=Decimal("50150"),
            volume=Decimal("1.0"),
        )
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 10 * 60 * 1000,
            open_p=Decimal("50150"),
            high_p=Decimal("50300"),
            low_p=Decimal("50100"),
            close_p=Decimal("50200"),
            volume=Decimal("1.0"),
        )
        
        # Trigger
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 15 * 60 * 1000,
            open_p=Decimal("50200"),
            high_p=Decimal("50400"),
            low_p=Decimal("50100"),
            close_p=Decimal("50300"),
            volume=Decimal("1.0"),
        )
        
        assert len(events) == 1
        
        # El high debe ser 50300 (de la vela 10:10), que es mayor que 50200 del update
        assert events[0].high == Decimal("50300"), f"Expected high=50300, got {events[0].high}"
        # El low debe ser 49800 (del update), no 49900 (original)
        assert events[0].low == Decimal("49800"), f"Expected low=49800, got {events[0].low}"
        # El volumen debe ser 2+1+1=4, no 1+1+1=3
        assert events[0].volume == Decimal("4"), f"Expected volume=4, got {events[0].volume}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
