"""
Test para verificar que CandleClosed se dispara exactamente una vez.

Valida que no haya doble dispatch de eventos.
"""
import sys
from decimal import Decimal

sys.path.insert(0, '/mnt/okcomputer/output/fortress_v4')

from src.marketdata.service import MarketDataService, CandleClosed


class TestSingleDispatch:
    """Test que CandleClosed se dispara exactamente una vez por bucket."""
    
    def test_candleclosed_dispatched_once(self):
        """
        Un bucket completo debe generar exactamente una llamada al handler.
        
        Este test verifica que no haya doble dispatch de eventos.
        """
        service = MarketDataService()
        dispatch_count = 0
        received_candles = []
        
        def handler(candle: CandleClosed):
            nonlocal dispatch_count
            dispatch_count += 1
            received_candles.append(candle)
        
        service.register_symbol("BTC-USD", "15m")
        service.subscribe("BTC-USD", handler)
        
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC
        
        # Ingerir 3 velas 5m para completar bucket 15m
        for i in range(3):
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="15m",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )
        
        # Nada emitido todavía (sin trigger)
        assert dispatch_count == 0, f"Expected 0 dispatches, got {dispatch_count}"
        
        # Trigger con vela del siguiente bucket
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 3 * 5 * 60 * 1000,  # 10:15:00
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )
        
        # Debe haber exactamente 1 dispatch
        assert dispatch_count == 1, f"Expected 1 dispatch, got {dispatch_count}"
        assert len(received_candles) == 1
        assert received_candles[0].symbol == "BTC-USD"
        assert received_candles[0].timeframe == "15m"
    
    def test_multiple_handlers_same_candle(self):
        """
        Múltiples handlers suscritos al mismo símbolo deben recibir el mismo candle.
        
        Esto es comportamiento esperado - cada handler recibe el evento.
        """
        service = MarketDataService()
        handler1_count = 0
        handler2_count = 0
        
        def handler1(candle: CandleClosed):
            nonlocal handler1_count
            handler1_count += 1
        
        def handler2(candle: CandleClosed):
            nonlocal handler2_count
            handler2_count += 1
        
        service.register_symbol("BTC-USD", "15m")
        service.subscribe("BTC-USD", handler1)
        service.subscribe("BTC-USD", handler2)
        
        base_ts = 10 * 3600 * 1000  # 10:00:00 UTC
        
        # Completar bucket y trigger
        for i in range(3):
            service.ingest_5m_candle(
                symbol="BTC-USD",
                target_timeframe="15m",
                timestamp_ms=base_ts + i * 5 * 60 * 1000,
                open_p=Decimal("50000"),
                high_p=Decimal("50100"),
                low_p=Decimal("49900"),
                close_p=Decimal("50050"),
                volume=Decimal("1.0"),
            )
        
        service.ingest_5m_candle(
            symbol="BTC-USD",
            target_timeframe="15m",
            timestamp_ms=base_ts + 3 * 5 * 60 * 1000,
            open_p=Decimal("50050"),
            high_p=Decimal("50100"),
            low_p=Decimal("49900"),
            close_p=Decimal("50050"),
            volume=Decimal("1.0"),
        )
        
        # Cada handler debe haber recibido exactamente 1 evento
        assert handler1_count == 1, f"Handler1: expected 1, got {handler1_count}"
        assert handler2_count == 1, f"Handler2: expected 1, got {handler2_count}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
