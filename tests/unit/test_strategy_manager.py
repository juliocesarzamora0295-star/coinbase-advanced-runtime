"""
Tests unitarios: StrategyManager.

Invariantes testeadas:
- load_from_config con 1 estrategia → StrategyManager instanciado
- load_from_config con estrategia desconocida → ignorada con log
- load_from_config sin estrategias válidas → ValueError
- primera barra (startup bucket) → None
- sin warmup suficiente (datos insuficientes) → None
- estrategia levanta excepción → no propaga, retorna None (solo esa estrategia falla)
- compose_mode=first: primera señal gana
- compose_mode=majority: señal con más votos gana
- compose_mode=majority con empate → None
- StrategyManager por símbolo es independiente (no cross-contamination)
- bar_count y strategy_count son correctos
"""
from decimal import Decimal
from typing import List, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.strategy.base import Signal, Strategy
from src.strategy.manager import StrategyManager


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_candle(close: float = 50000.0) -> pd.Series:
    return pd.Series({"open": close * 0.99, "high": close * 1.01, "low": close * 0.98, "close": close, "volume": 1.0})


def make_signal(side: str = "buy") -> Signal:
    return Signal(
        symbol="BTC-USD",
        side=side,
        position_side="LONG" if side == "buy" else "SHORT",
        order_type="limit",
        amount=Decimal("0.001"),
        price=Decimal("50000"),
        reason="test",
    )


class FixedStrategy(Strategy):
    """Estrategia de prueba que emite una señal fija después de N barras."""

    def __init__(self, symbol: str, config=None, emit_after: int = 0, signal_side: str = "buy"):
        super().__init__(symbol, config)
        self._emit_after = emit_after
        self._call_count = 0
        self._signal_side = signal_side

    def generate_signals(self, *, mid: Decimal) -> List[Signal]:
        self._call_count += 1
        if self._call_count > self._emit_after:
            return [make_signal(self._signal_side)]
        return []


class ExplodingStrategy(Strategy):
    """Estrategia que levanta excepción en generate_signals."""

    def generate_signals(self, *, mid: Decimal) -> List[Signal]:
        raise RuntimeError("strategy exploded")


class EmptyStrategy(Strategy):
    """Estrategia que nunca emite señal."""

    def generate_signals(self, *, mid: Decimal) -> List[Signal]:
        return []


# ──────────────────────────────────────────────
# load_from_config
# ──────────────────────────────────────────────

class TestLoadFromConfig:

    def test_load_known_strategy(self):
        """Estrategia conocida → cargada correctamente."""
        config = {"strategies": ["ma_crossover"]}
        manager = StrategyManager.load_from_config("BTC-USD", config)
        assert manager.strategy_count == 1

    def test_load_unknown_strategy_ignored(self):
        """Estrategia desconocida → ignorada, no raise."""
        config = {"strategies": ["ma_crossover", "nonexistent_strategy"]}
        manager = StrategyManager.load_from_config("BTC-USD", config)
        assert manager.strategy_count == 1  # solo ma_crossover se cargó

    def test_no_valid_strategies_raises(self):
        """Ninguna estrategia válida → ValueError."""
        config = {"strategies": ["totally_unknown"]}
        with pytest.raises(ValueError, match="No valid strategies"):
            StrategyManager.load_from_config("BTC-USD", config)

    def test_empty_strategies_list_raises(self):
        """Lista vacía de estrategias → ValueError."""
        config = {"strategies": []}
        with pytest.raises(ValueError):
            StrategyManager.load_from_config("BTC-USD", config)

    def test_compose_mode_from_config(self):
        """compose_mode se toma del config."""
        config = {"strategies": ["ma_crossover"], "compose_mode": "majority"}
        manager = StrategyManager.load_from_config("BTC-USD", config)
        assert manager._compose_mode == "majority"

    def test_default_compose_mode_is_first(self):
        """compose_mode por defecto es 'first'."""
        config = {"strategies": ["ma_crossover"]}
        manager = StrategyManager.load_from_config("BTC-USD", config)
        assert manager._compose_mode == "first"


# ──────────────────────────────────────────────
# Startup bucket (primera barra)
# ──────────────────────────────────────────────

class TestStartupBucket:

    def test_first_candle_returns_none(self):
        """Primera barra → None (startup bucket, datos posiblemente parciales)."""
        strategy = FixedStrategy("BTC-USD", emit_after=0)
        manager = StrategyManager("BTC-USD", [strategy])

        result = manager.on_candle_closed(make_candle())
        assert result is None

    def test_second_candle_can_return_signal(self):
        """Segunda barra en adelante puede retornar señal."""
        strategy = FixedStrategy("BTC-USD", emit_after=0)
        manager = StrategyManager("BTC-USD", [strategy])

        manager.on_candle_closed(make_candle())  # primera: startup, None
        result = manager.on_candle_closed(make_candle())  # segunda
        assert result is not None

    def test_bar_count_increments(self):
        """bar_count se incrementa con cada candle."""
        strategy = EmptyStrategy("BTC-USD")
        manager = StrategyManager("BTC-USD", [strategy])

        for i in range(3):
            manager.on_candle_closed(make_candle())

        assert manager.bar_count == 3


# ──────────────────────────────────────────────
# Sin warmup suficiente
# ──────────────────────────────────────────────

class TestInsufficientWarmup:

    def test_strategy_with_no_data_returns_none(self):
        """Estrategia con datos insuficientes → None."""
        strategy = FixedStrategy("BTC-USD", emit_after=5)
        manager = StrategyManager("BTC-USD", [strategy])

        # 3 barras: primera es startup, luego 2 con emit_after=5 → no emite
        for _ in range(3):
            manager.on_candle_closed(make_candle())

        # A la 4ta barra emit_after=5 aún no alcanzado
        result = manager.on_candle_closed(make_candle())
        assert result is None

    def test_after_warmup_emits_signal(self):
        """Después del warmup, la estrategia emite señal."""
        strategy = FixedStrategy("BTC-USD", emit_after=2)
        manager = StrategyManager("BTC-USD", [strategy])

        results = []
        for _ in range(5):
            results.append(manager.on_candle_closed(make_candle()))

        # Debe haber al menos una señal después del warmup
        assert any(r is not None for r in results)


# ──────────────────────────────────────────────
# Excepciones no propagan
# ──────────────────────────────────────────────

class TestExceptionIsolation:

    def test_exploding_strategy_does_not_propagate(self):
        """Excepción en estrategia → log, no propaga, retorna None."""
        strategy = ExplodingStrategy("BTC-USD")
        manager = StrategyManager("BTC-USD", [strategy])

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())  # segunda

        # No propagó la excepción
        assert result is None

    def test_exploding_strategy_does_not_affect_sibling(self):
        """Excepción en estrategia A no silencia señal de estrategia B."""
        exploding = ExplodingStrategy("BTC-USD")
        good = FixedStrategy("BTC-USD", emit_after=0)
        manager = StrategyManager("BTC-USD", [exploding, good], compose_mode="first")

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())

        # La estrategia buena emitió señal, debe aparecer
        assert result is not None


# ──────────────────────────────────────────────
# Composición
# ──────────────────────────────────────────────

class TestCompose:

    def test_compose_first_wins(self):
        """compose_mode=first → primera señal de la lista gana."""
        s1 = FixedStrategy("BTC-USD", emit_after=0, signal_side="buy")
        s2 = FixedStrategy("BTC-USD", emit_after=0, signal_side="sell")
        manager = StrategyManager("BTC-USD", [s1, s2], compose_mode="first")

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())

        assert result is not None
        assert result.side == "buy"  # s1 es primera

    def test_compose_majority_buy_wins(self):
        """compose_mode=majority → mayoría buy → retorna buy."""
        buy1 = FixedStrategy("BTC-USD", emit_after=0, signal_side="buy")
        buy2 = FixedStrategy("BTC-USD", emit_after=0, signal_side="buy")
        sell1 = FixedStrategy("BTC-USD", emit_after=0, signal_side="sell")
        manager = StrategyManager("BTC-USD", [buy1, buy2, sell1], compose_mode="majority")

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())

        assert result is not None
        assert result.side == "buy"

    def test_compose_majority_sell_wins(self):
        """compose_mode=majority → mayoría sell → retorna sell."""
        buy1 = FixedStrategy("BTC-USD", emit_after=0, signal_side="buy")
        sell1 = FixedStrategy("BTC-USD", emit_after=0, signal_side="sell")
        sell2 = FixedStrategy("BTC-USD", emit_after=0, signal_side="sell")
        manager = StrategyManager("BTC-USD", [buy1, sell1, sell2], compose_mode="majority")

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())

        assert result is not None
        assert result.side == "sell"

    def test_compose_majority_tie_returns_none(self):
        """compose_mode=majority con empate → None."""
        buy = FixedStrategy("BTC-USD", emit_after=0, signal_side="buy")
        sell = FixedStrategy("BTC-USD", emit_after=0, signal_side="sell")
        manager = StrategyManager("BTC-USD", [buy, sell], compose_mode="majority")

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())

        assert result is None

    def test_no_signals_returns_none(self):
        """Sin señales → None."""
        strategy = EmptyStrategy("BTC-USD")
        manager = StrategyManager("BTC-USD", [strategy])

        manager.on_candle_closed(make_candle())  # startup
        result = manager.on_candle_closed(make_candle())
        assert result is None


# ──────────────────────────────────────────────
# Independencia por símbolo
# ──────────────────────────────────────────────

class TestSymbolIndependence:

    def test_managers_do_not_share_state(self):
        """Dos StrategyManagers para distintos símbolos no comparten estado."""
        s1 = FixedStrategy("BTC-USD", emit_after=0)
        s2 = FixedStrategy("ETH-USD", emit_after=0)
        m1 = StrategyManager("BTC-USD", [s1])
        m2 = StrategyManager("ETH-USD", [s2])

        # Alimentar solo m1
        m1.on_candle_closed(make_candle())
        m1.on_candle_closed(make_candle())

        # m2 debe tener bar_count=0, m1 bar_count=2
        assert m1.bar_count == 2
        assert m2.bar_count == 0

    def test_candles_not_shared_between_managers(self):
        """Los candles acumulados de m1 no afectan a m2."""
        s1 = FixedStrategy("BTC-USD", emit_after=0)
        s2 = FixedStrategy("ETH-USD", emit_after=0)
        m1 = StrategyManager("BTC-USD", [s1])
        m2 = StrategyManager("ETH-USD", [s2])

        for _ in range(5):
            m1.on_candle_closed(make_candle())

        assert m2.bar_count == 0
        assert m2._candles.empty
