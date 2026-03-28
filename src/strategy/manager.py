"""
StrategyManager — Orquestador de estrategias por símbolo.

Carga estrategias desde config, ejecuta on_candle_closed, compone señales.

Responsabilidades:
- Cargar estrategias por símbolo desde symbol_config dict.
- Ejecutar estrategias con candle data acumulado.
- Componer señales (first-wins o majority-vote, configurable).
- Retornar Signal | None.

Lo que NO hace:
- No ejecuta órdenes.
- No decide qty final.
- No bypassea RiskGate.
- No lee estado OMS.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from src.strategy.base import Strategy
from src.strategy.sma_crossover import SmaCrossoverStrategy

logger = logging.getLogger("StrategyManager")

# Registro de estrategias disponibles por nombre de config.
# Extender aquí para agregar nuevas estrategias.
_STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_crossover": SmaCrossoverStrategy,
    "sma_crossover": SmaCrossoverStrategy,
}


class StrategyManager:
    """
    Orquestador de estrategias para un único símbolo.

    Cada símbolo tiene su propio StrategyManager — no hay cross-contamination.
    """

    def __init__(
        self,
        symbol: str,
        strategies: List[Strategy],
        compose_mode: str = "first",
    ) -> None:
        if not strategies:
            raise ValueError(f"StrategyManager for {symbol}: strategies list is empty")
        self.symbol = symbol
        self._strategies = strategies
        self._compose_mode = compose_mode  # "first" | "majority"
        self._candles: pd.DataFrame = pd.DataFrame()
        self._bar_count: int = 0

    @classmethod
    def load_from_config(
        cls,
        symbol: str,
        symbol_config: Dict[str, Any],
    ) -> "StrategyManager":
        """
        Construir StrategyManager desde configuración de símbolo.

        Args:
            symbol: Identificador del símbolo (ej. "BTC-USD").
            symbol_config: Dict con claves 'strategies' (list de dicts o str),
                           'compose_mode' (opcional), y config de cada estrategia.

        Returns:
            StrategyManager instanciado con estrategias cargadas.

        Raises:
            ValueError: Si no se puede cargar ninguna estrategia válida.
        """
        strategy_configs = symbol_config.get("strategies", [])
        compose_mode = symbol_config.get("compose_mode", "first")

        loaded: List[Strategy] = []
        for entry in strategy_configs:
            if isinstance(entry, str):
                name = entry
                cfg: Dict[str, Any] = symbol_config.get(name, {})
            else:
                name = entry.get("name", "")
                cfg = entry

            strategy_cls = _STRATEGY_REGISTRY.get(name)
            if strategy_cls is None:
                logger.warning("Unknown strategy '%s' for %s — skipping", name, symbol)
                continue

            try:
                strategy = strategy_cls(symbol=symbol, config=cfg)
                loaded.append(strategy)
                logger.info("Loaded strategy '%s' for %s", name, symbol)
            except Exception as exc:
                logger.error("Failed to load strategy '%s' for %s: %s", name, symbol, exc)

        if not loaded:
            raise ValueError(
                f"No valid strategies loaded for {symbol} from config: {strategy_configs}"
            )

        return cls(symbol=symbol, strategies=loaded, compose_mode=compose_mode)

    def on_candle_closed(
        self,
        candle: pd.Series,
        mid: Optional[Decimal] = None,
        bar_timestamp: Optional[datetime] = None,
    ) -> Optional[object]:
        """
        Procesar una vela cerrada y obtener señal compuesta.

        Args:
            candle: pd.Series con al menos columna 'close'. Puede tener
                    'open', 'high', 'low', 'volume'.
            mid: Precio mid opcional. Si None, usa candle['close'].

        Returns:
            Signal compuesta o None si no hay señal o si es la primera barra
            (startup bucket — datos parciales).
        """
        # Acumular candle en DataFrame interno
        row = candle.to_frame().T if isinstance(candle, pd.Series) else candle
        self._candles = (
            pd.concat([self._candles, row], ignore_index=True)
            if not self._candles.empty
            else row.reset_index(drop=True)
        )
        self._bar_count += 1

        # Primera barra: startup bucket — puede ser parcial, no emitir señal
        if self._bar_count == 1:
            logger.debug(
                "StrategyManager(%s): primera barra — startup bucket, sin señal", self.symbol
            )
            return None

        price = mid if mid is not None else Decimal(str(candle.get("close", 0)))

        all_signals: List = []
        for strategy in self._strategies:
            try:
                strategy.update_market_data(self._candles)
                signals = strategy.generate_signals(mid=price, bar_timestamp=bar_timestamp)
                all_signals.extend(signals)
            except Exception as exc:
                logger.error(
                    "Strategy %s raised exception on %s: %s — skipping",
                    type(strategy).__name__,
                    self.symbol,
                    exc,
                )
                # No propagar — log y continuar

        return self._compose(all_signals)

    def _compose(self, signals: List) -> Optional[object]:
        """
        Componer lista de señales en una sola según compose_mode.

        "first": primera señal válida gana.
        "majority": mayoría de señales en la misma dirección.

        Compatible con src.strategy.signal.Signal (direction="BUY"/"SELL")
        y con src.strategy.base.Signal (side="buy"/"sell").

        Returns:
            Signal o None.
        """
        if not signals:
            return None

        if self._compose_mode == "first":
            return signals[0]  # type: ignore[no-any-return]

        if self._compose_mode == "majority":

            def _direction(s: Any) -> str:
                # New Signal: direction="BUY"/"SELL"; old Signal: side="buy"/"sell"
                d = getattr(s, "direction", None)
                if d is not None:
                    return str(d).upper()
                return str(getattr(s, "side", "")).upper()

            buys = [s for s in signals if _direction(s) == "BUY"]
            sells = [s for s in signals if _direction(s) == "SELL"]
            if len(buys) > len(sells):
                return buys[0]  # type: ignore[no-any-return]
            if len(sells) > len(buys):
                return sells[0]  # type: ignore[no-any-return]
            return None  # empate → sin señal

        # Fallback a first
        return signals[0]  # type: ignore[no-any-return]

    @property
    def bar_count(self) -> int:
        """Número de barras cerradas procesadas."""
        return self._bar_count

    @property
    def strategy_count(self) -> int:
        """Número de estrategias cargadas."""
        return len(self._strategies)
