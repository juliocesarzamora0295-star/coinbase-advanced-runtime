"""
MarketDataService - Emite eventos CandleClosed con resampling correcto.

Ingiere velas 5m desde Coinbase y emite CandleClosed solo cuando
el bucket del timeframe operativo está completo, alineado a fronteras UTC.

P0 FIXES:
- Upsert por timestamp 5m (no duplicar updates del mismo candle)
- Descartar primer bucket parcial (no emitir si faltan velas)
- Emitir solo buckets completos
"""
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger("MarketDataService")


@dataclass
class CandleClosed:
    """Evento de vela cerrada."""
    symbol: str
    timeframe: str  # Timeframe real (ej: "1h", no "5m")
    timestamp_ms: int  # Timestamp de CIERRE de la vela (no inicio)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class SymbolBarBuilder:
    """
    Builder de barras para un símbolo/timeframe específico.
    
    Acumula velas 5m y emite CandleClosed cuando el bucket calendario está completo.
    Alinea buckets a fronteras UTC reales (ej: 10:00, 11:00 para 1h).
    
    P0 FIXES:
    - Upsert por timestamp 5m (evita duplicados de updates frecuentes)
    - Descarta primer bucket parcial (arranque a mitad de bucket)
    - Solo emite buckets con todas las velas 5m requeridas
    """
    
    # Mapeo de timeframe a cantidad de velas 5m requeridas
    REQUIRED_CANDLES = {
        "5m": 1,
        "15m": 3,
        "30m": 6,
        "1h": 12,
        "2h": 24,
        "4h": 48,
        "6h": 72,
        "1d": 288,
    }
    
    # Mapeo de timeframe a duración en milisegundos
    TIMEFRAME_MS = {
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "2h": 2 * 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "6h": 6 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    
    def __init__(self, symbol: str, target_timeframe: str):
        self.symbol = symbol
        self.target_timeframe = target_timeframe
        self.required_5m = self.REQUIRED_CANDLES.get(target_timeframe, 12)
        self.bucket_ms = self.TIMEFRAME_MS.get(target_timeframe, 60 * 60 * 1000)
        
        self._current_bucket_id: Optional[int] = None
        self._current_bucket_candles: Dict[int, Dict] = {}  # P0 FIX: dict por timestamp
        self._first_bucket_emitted: bool = False  # P0 FIX: track primer bucket
        self._last_emitted_close_ts: int = 0
        self._lock = threading.Lock()
    
    def _get_bucket_id(self, start_ts_ms: int) -> int:
        """
        Calcular el ID del bucket calendario para un timestamp.
        
        El bucket ID es el timestamp de inicio del bucket alineado a frontera UTC.
        Ej: para 1h y start=10:05:00, bucket_id=10:00:00
        """
        return (start_ts_ms // self.bucket_ms) * self.bucket_ms
    
    def _get_bucket_close_ts(self, bucket_id: int) -> int:
        """
        Calcular el timestamp de CIERRE de un bucket.
        
        El cierre es: bucket_id + bucket_duration
        Ej: para bucket 10:00:00 de 1h, close=11:00:00
        """
        return bucket_id + self.bucket_ms
    
    def ingest_5m_candle_with_accumulation(self, candle_5m: Dict) -> List[CandleClosed]:
        """
        Ingerir una vela 5m y retornar lista de CandleClosed emitidos.
        
        P0 FIXES:
        - Upsert por timestamp 5m (evita duplicados de updates frecuentes de Coinbase)
        - Descarta primer bucket parcial (si arranca a mitad de bucket)
        - Solo emite buckets con todas las velas 5m requeridas
        
        Args:
            candle_5m: Dict con timestamp_ms (start), open, high, low, close, volume
            
        Returns:
            Lista de CandleClosed emitidos (vacía si se acumula o bucket incompleto)
        """
        start_ts_ms = candle_5m["timestamp_ms"]  # Coinbase 'start' es inicio de la vela 5m
        bucket_id = self._get_bucket_id(start_ts_ms)
        
        events = []
        
        with self._lock:
            # Ignorar velas viejas (antes del último cierre emitido)
            bucket_close_ts = self._get_bucket_close_ts(bucket_id)
            if bucket_close_ts <= self._last_emitted_close_ts:
                logger.debug(
                    f"[{self.symbol}] Ignoring old candle: bucket_close={bucket_close_ts} "
                    f"<= last_emitted={self._last_emitted_close_ts}"
                )
                return []
            
            # Detectar cambio de bucket
            if self._current_bucket_id is not None and bucket_id != self._current_bucket_id:
                # P0 FIX: Verificar si el bucket anterior está COMPLETO antes de emitir
                if self._current_bucket_candles:
                    if len(self._current_bucket_candles) == self.required_5m:
                        # Bucket completo - emitir
                        event = self._build_candle_closed(self._current_bucket_id, self._current_bucket_candles)
                        if event:
                            events.append(event)
                            self._last_emitted_close_ts = event.timestamp_ms
                            self._first_bucket_emitted = True
                    else:
                        # P0 FIX: Bucket incompleto - descartar (primer arranque)
                        if self._first_bucket_emitted:
                            # Solo loggear warning si no es el primer bucket
                            logger.warning(
                                f"[{self.symbol}] Discarding partial {self.target_timeframe} bucket "
                                f"{self._current_bucket_id} ({len(self._current_bucket_candles)}/{self.required_5m} candles)"
                            )
                        else:
                            logger.info(
                                f"[{self.symbol}] Discarding first partial {self.target_timeframe} bucket "
                                f"({len(self._current_bucket_candles)}/{self.required_5m} candles) - startup"
                            )
                
                # Iniciar nuevo bucket
                self._current_bucket_id = bucket_id
                self._current_bucket_candles = {start_ts_ms: candle_5m}  # P0 FIX: dict
            elif self._current_bucket_id is None:
                # Primer candle
                self._current_bucket_id = bucket_id
                self._current_bucket_candles = {start_ts_ms: candle_5m}  # P0 FIX: dict
            else:
                # Mismo bucket - P0 FIX: UPSERT por timestamp (no append)
                self._current_bucket_candles[start_ts_ms] = candle_5m
        
        return events
    
    def _build_candle_closed(self, bucket_id: int, candles_by_ts: Dict[int, Dict]) -> Optional[CandleClosed]:
        """Construir CandleClosed desde un bucket completo."""
        if not candles_by_ts:
            return None
        
        # Ordenar candles por timestamp
        candles = [candles_by_ts[k] for k in sorted(candles_by_ts)]
        
        # Calcular OHLCV del bucket
        open_p = candles[0]["open"]
        high_p = max(c["high"] for c in candles)
        low_p = min(c["low"] for c in candles)
        close_p = candles[-1]["close"]
        volume = sum(c["volume"] for c in candles)
        
        # Timestamp de CIERRE del bucket (no inicio de última vela)
        close_ts = self._get_bucket_close_ts(bucket_id)
        
        event = CandleClosed(
            symbol=self.symbol,
            timeframe=self.target_timeframe,
            timestamp_ms=close_ts,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=volume,
        )
        
        logger.info(
            f"[{self.symbol}] CandleClosed emitted: {self.target_timeframe} "
            f"bucket={bucket_id} close={close_ts} "
            f"O={open_p} H={high_p} L={low_p} C={close_p} V={volume} "
            f"(from {len(candles)} candles)"
        )
        
        return event


class MarketDataService:
    """
    Servicio de datos de mercado con resampling 5m -> timeframe operativo.
    
    Responsabilidades:
    - Ingerir velas 5m crudas desde Coinbase (con updates frecuentes)
    - Acumular en buckets calendario del timeframe operativo
    - Emitir CandleClosed SOLO cuando el bucket calendario está completo
    - Alinear buckets a fronteras UTC (ej: 10:00, 11:00 para 1h)
    - P0 FIX: Upsert por timestamp (no duplicar updates del mismo candle)
    - P0 FIX: Descartar primer bucket parcial (arranque a mitad de bucket)
    """
    
    def __init__(self):
        # Un builder por (symbol, timeframe)
        self._builders: Dict[str, SymbolBarBuilder] = {}
        self._callbacks: Dict[str, List[Callable[[CandleClosed], None]]] = {}
        self._lock = threading.Lock()
    
    def register_symbol(self, symbol: str, target_timeframe: str) -> None:
        """Registrar un símbolo con su timeframe operativo."""
        key = f"{symbol}:{target_timeframe}"
        with self._lock:
            if key not in self._builders:
                self._builders[key] = SymbolBarBuilder(symbol, target_timeframe)
                logger.info(f"Registered {symbol} with timeframe {target_timeframe}")
    
    def subscribe(self, symbol: str, callback: Callable[[CandleClosed], None]) -> None:
        """Suscribirse a eventos CandleClosed para un símbolo."""
        with self._lock:
            if symbol not in self._callbacks:
                self._callbacks[symbol] = []
            self._callbacks[symbol].append(callback)
        logger.info(f"Subscribed to CandleClosed for {symbol}")
    
    def ingest_5m_candle(
        self,
        symbol: str,
        target_timeframe: str,
        timestamp_ms: int,
        open_p: Decimal,
        high_p: Decimal,
        low_p: Decimal,
        close_p: Decimal,
        volume: Decimal,
    ) -> List[CandleClosed]:
        """
        Ingerir vela 5m desde WebSocket y emitir CandleClosed si aplica.
        
        P0 FIX: Soporta updates frecuentes del mismo candle (upsert por timestamp).
        
        Args:
            symbol: Símbolo (ej: "BTC-USD")
            target_timeframe: Timeframe operativo (ej: "1h", "4h")
            timestamp_ms: Timestamp START de la vela 5m (de Coinbase 'start')
            open_p, high_p, low_p, close_p, volume: OHLCV de la vela 5m
            
        Returns:
            Lista de CandleClosed emitidos (vacía si se acumula o bucket incompleto)
        """
        key = f"{symbol}:{target_timeframe}"
        
        with self._lock:
            builder = self._builders.get(key)
            if not builder:
                logger.warning(f"Symbol {symbol} with timeframe {target_timeframe} not registered")
                return []
        
        candle_5m = {
            "timestamp_ms": timestamp_ms,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": volume,
        }
        
        # Ingerir la vela y obtener eventos emitidos
        events = builder.ingest_5m_candle_with_accumulation(candle_5m)
        
        # Llamar callbacks para cada evento
        for event in events:
            callbacks = self._callbacks.get(symbol, [])
            for cb in callbacks:
                try:
                    cb(event)
                except Exception as e:
                    logger.error(f"Error in CandleClosed callback: {e}")
        
        return events


class SignalEngine:
    """
    Motor de señales aislado por símbolo.
    
    Cada SignalEngine maneja estrategias para UN símbolo específico.
    No mezcla históricos entre símbolos.
    """
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._strategies: List[Callable[[CandleClosed], Optional[Dict]]] = []
    
    def add_strategy(self, strategy: Callable[[CandleClosed], Optional[Dict]]) -> None:
        """Agregar una estrategia al motor."""
        self._strategies.append(strategy)
    
    def on_candle_closed(self, candle: CandleClosed) -> List[Dict]:
        """
        Procesar vela cerrada y generar señales.
        
        Args:
            candle: CandleClosed recibido
            
        Returns:
            Lista de señales generadas (puede estar vacía)
        """
        # Sanity check: solo procesar velas del símbolo correcto
        if candle.symbol != self.symbol:
            logger.warning(
                f"[{self.symbol}] Received candle for wrong symbol: {candle.symbol}. Ignoring."
            )
            return []
        
        signals = []
        
        for strategy in self._strategies:
            try:
                signal = strategy(candle)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"[{self.symbol}] Error in strategy: {e}")
        
        if signals:
            logger.info(f"[{self.symbol}] Generated {len(signals)} signals")
        
        return signals


def create_naive_ma_strategy(symbol: str, fast_period: int = 10, slow_period: int = 30) -> Callable:
    """
    Crear estrategia naive de cruce de medias móviles para UN símbolo.
    
    P1 FIX: Requiere slow_period + 1 velas para calcular prev_* correctamente.
    
    Args:
        symbol: Símbolo que esta estrategia maneja
        fast_period: Período de MA rápida
        slow_period: Período de MA lenta
        
    NOTA: Esta es una estrategia de ejemplo/demostración.
    No está optimizada ni validada para trading real.
    """
    candles_history: List[CandleClosed] = []
    
    def strategy(candle: CandleClosed) -> Optional[Dict]:
        # Sanity check: solo procesar velas del símbolo correcto
        if candle.symbol != symbol:
            return None
        
        candles_history.append(candle)
        
        # P1 FIX: Requerir slow_period + 1 velas para calcular prev_* correctamente
        required = slow_period + 1
        if len(candles_history) < required:
            return None
        
        # P1 FIX: Calcular current y previous con ventanas completas
        current_closes = [float(c.close) for c in candles_history[-slow_period:]]
        previous_closes = [float(c.close) for c in candles_history[-slow_period-1:-1]]
        
        fast_ma = sum(current_closes[-fast_period:]) / fast_period
        slow_ma = sum(current_closes) / slow_period
        
        prev_fast = sum(previous_closes[-fast_period:]) / fast_period
        prev_slow = sum(previous_closes) / slow_period
        
        signal = None
        if prev_fast <= prev_slow and fast_ma > slow_ma:
            signal = {
                "symbol": candle.symbol,
                "side": "BUY",
                "reason": "MA crossover",
                "fast_ma": fast_ma,
                "slow_ma": slow_ma,
            }
        elif prev_fast >= prev_slow and fast_ma < slow_ma:
            signal = {
                "symbol": candle.symbol,
                "side": "SELL",
                "reason": "MA crossunder",
                "fast_ma": fast_ma,
                "slow_ma": slow_ma,
            }
        
        return signal
    
    return strategy
