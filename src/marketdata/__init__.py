"""
MarketData module - Servicios de datos de mercado.

Incluye:
- MarketDataService: Emite eventos CandleClosed
- SignalEngine: Genera señales desde velas cerradas
"""

from src.marketdata.service import (
    CandleClosed,
    MarketDataService,
    SignalEngine,
    create_naive_ma_strategy,
)

__all__ = [
    "CandleClosed",
    "MarketDataService",
    "SignalEngine",
    "create_naive_ma_strategy",
]
