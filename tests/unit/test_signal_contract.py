"""
Tests de contrato para Signal.

Invariantes testeadas:
- direction inválida → ValueError
- strength fuera de [0, 1] → ValueError
- signal_id vacío → ValueError
- bar_timestamp sin timezone → ValueError
- Signal es inmutable (frozen)
- make_signal() genera signal_id y emitted_at automáticamente
- metadata es opcional
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.strategy.signal import Signal, make_signal


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def confirmed_bar_ts() -> datetime:
    """Simula un timestamp de bucket confirmado."""
    return datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


class TestSignalInvariants:
    """Invariantes del dataclass Signal."""

    def test_valid_buy_signal(self):
        """Señal BUY válida se construye sin errores."""
        s = Signal(
            signal_id="test-id-001",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.8"),
            strategy_id="sma_crossover",
            bar_timestamp=confirmed_bar_ts(),
            emitted_at=utc_now(),
        )
        assert s.direction == "BUY"
        assert s.symbol == "BTC-USD"

    def test_valid_sell_signal(self):
        """Señal SELL válida se construye sin errores."""
        s = Signal(
            signal_id="test-id-002",
            symbol="ETH-USD",
            direction="SELL",
            strength=Decimal("0.5"),
            strategy_id="sma_crossover",
            bar_timestamp=confirmed_bar_ts(),
            emitted_at=utc_now(),
        )
        assert s.direction == "SELL"

    def test_invalid_direction_raises(self):
        """direction != 'BUY'|'SELL' → ValueError."""
        with pytest.raises(ValueError, match="Invalid direction"):
            Signal(
                signal_id="test-id",
                symbol="BTC-USD",
                direction="LONG",  # inválido
                strength=Decimal("0.5"),
                strategy_id="sma",
                bar_timestamp=confirmed_bar_ts(),
                emitted_at=utc_now(),
            )

    def test_strength_below_zero_raises(self):
        """strength < 0 → ValueError."""
        with pytest.raises(ValueError, match="strength must be in"):
            Signal(
                signal_id="test-id",
                symbol="BTC-USD",
                direction="BUY",
                strength=Decimal("-0.1"),
                strategy_id="sma",
                bar_timestamp=confirmed_bar_ts(),
                emitted_at=utc_now(),
            )

    def test_strength_above_one_raises(self):
        """strength > 1 → ValueError."""
        with pytest.raises(ValueError, match="strength must be in"):
            Signal(
                signal_id="test-id",
                symbol="BTC-USD",
                direction="BUY",
                strength=Decimal("1.1"),
                strategy_id="sma",
                bar_timestamp=confirmed_bar_ts(),
                emitted_at=utc_now(),
            )

    def test_strength_boundary_zero_ok(self):
        """strength = 0 es válido."""
        s = Signal(
            signal_id="test-id",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
            emitted_at=utc_now(),
        )
        assert s.strength == Decimal("0")

    def test_strength_boundary_one_ok(self):
        """strength = 1 es válido."""
        s = Signal(
            signal_id="test-id",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("1"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
            emitted_at=utc_now(),
        )
        assert s.strength == Decimal("1")

    def test_empty_signal_id_raises(self):
        """signal_id vacío → ValueError."""
        with pytest.raises(ValueError, match="signal_id"):
            Signal(
                signal_id="",
                symbol="BTC-USD",
                direction="BUY",
                strength=Decimal("0.5"),
                strategy_id="sma",
                bar_timestamp=confirmed_bar_ts(),
                emitted_at=utc_now(),
            )

    def test_bar_timestamp_without_timezone_raises(self):
        """bar_timestamp sin timezone → ValueError."""
        naive_ts = datetime(2024, 1, 15, 10, 0, 0)  # naive, sin tz
        with pytest.raises(ValueError, match="bar_timestamp must be timezone-aware"):
            Signal(
                signal_id="test-id",
                symbol="BTC-USD",
                direction="BUY",
                strength=Decimal("0.5"),
                strategy_id="sma",
                bar_timestamp=naive_ts,
                emitted_at=utc_now(),
            )

    def test_emitted_at_without_timezone_raises(self):
        """emitted_at sin timezone → ValueError."""
        naive_ts = datetime(2024, 1, 15, 10, 0, 0)  # naive
        with pytest.raises(ValueError, match="emitted_at must be timezone-aware"):
            Signal(
                signal_id="test-id",
                symbol="BTC-USD",
                direction="BUY",
                strength=Decimal("0.5"),
                strategy_id="sma",
                bar_timestamp=confirmed_bar_ts(),
                emitted_at=naive_ts,
            )

    def test_signal_is_immutable(self):
        """Signal es frozen: no se puede modificar post-construcción."""
        s = Signal(
            signal_id="test-id",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.5"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
            emitted_at=utc_now(),
        )
        with pytest.raises((AttributeError, TypeError)):
            s.direction = "SELL"  # type: ignore[misc]

    def test_bar_timestamp_can_be_in_past(self):
        """
        bar_timestamp puede ser del pasado (replay legítimo).
        No se valida contra el reloj local.
        """
        past_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        s = Signal(
            signal_id="test-id",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.5"),
            strategy_id="sma",
            bar_timestamp=past_ts,  # pasado: OK
            emitted_at=utc_now(),
        )
        assert s.bar_timestamp == past_ts

    def test_bar_timestamp_future_is_accepted(self):
        """
        bar_timestamp futuro NO lanza error — clock skew y latency son reales.
        La validación es responsabilidad del stream de market data, no del contrato Signal.
        """
        future_ts = utc_now() + timedelta(hours=1)
        s = Signal(
            signal_id="test-id",
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.5"),
            strategy_id="sma",
            bar_timestamp=future_ts,
            emitted_at=utc_now(),
        )
        assert s.bar_timestamp == future_ts

    def test_signal_has_no_qty_field(self):
        """Signal no tiene campo qty — ese contrato pertenece a PositionSizer."""
        s = make_signal(
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.5"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        assert not hasattr(s, "qty")
        assert not hasattr(s, "amount")
        assert not hasattr(s, "price")


class TestMakeSignalFactory:
    """Tests para la función factory make_signal()."""

    def test_make_signal_generates_signal_id(self):
        """make_signal() genera signal_id automáticamente."""
        s = make_signal(
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("0.7"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        assert s.signal_id
        assert len(s.signal_id) > 0

    def test_make_signal_generates_emitted_at_utc(self):
        """emitted_at generado por factory es UTC-aware."""
        s = make_signal(
            symbol="BTC-USD",
            direction="SELL",
            strength=Decimal("0.3"),
            strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        assert s.emitted_at.tzinfo is not None

    def test_two_signals_have_different_ids(self):
        """Dos llamadas a make_signal() producen signal_ids distintos."""
        s1 = make_signal(
            symbol="BTC-USD", direction="BUY",
            strength=Decimal("0.5"), strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        s2 = make_signal(
            symbol="BTC-USD", direction="BUY",
            strength=Decimal("0.5"), strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        assert s1.signal_id != s2.signal_id

    def test_metadata_defaults_to_empty(self):
        """metadata es vacío por defecto."""
        s = make_signal(
            symbol="BTC-USD", direction="BUY",
            strength=Decimal("0.5"), strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
        )
        assert s.metadata == {}

    def test_metadata_passed_through(self):
        """metadata pasado explícitamente se conserva."""
        s = make_signal(
            symbol="BTC-USD", direction="BUY",
            strength=Decimal("0.5"), strategy_id="sma",
            bar_timestamp=confirmed_bar_ts(),
            metadata={"source": "backtest", "version": 2},
        )
        assert s.metadata["source"] == "backtest"


class TestStrategyManagerEmitsNewSignal:
    """
    Contract test: StrategyManager.on_candle_closed() debe retornar
    src.strategy.signal.Signal (no el Signal legacy de base.py).
    """

    def test_sma_crossover_emits_new_signal_type(self):
        """SmaCrossoverStrategy emite Signal del contrato nuevo (con signal_id, direction)."""
        from decimal import Decimal
        import pandas as pd
        from src.strategy.manager import StrategyManager

        manager = StrategyManager.load_from_config(
            symbol="BTC-USD",
            symbol_config={"strategies": ["sma_crossover"], "sma_fast": 2, "sma_slow": 3},
        )

        # Warmup: primera barra es startup bucket
        manager.on_candle_closed(pd.Series({"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1.0}))

        # Alimentar datos que producen cruce alcista
        prices = [100.0] * 4 + [90.0] + [130.0]
        result = None
        for p in prices:
            row = pd.Series({"close": p, "open": p * 0.99, "high": p * 1.01, "low": p * 0.98, "volume": 1.0})
            r = manager.on_candle_closed(row)
            if r is not None:
                result = r

        if result is not None:
            # Debe tener campos del contrato nuevo
            assert hasattr(result, "signal_id"), "Signal nuevo debe tener signal_id"
            assert hasattr(result, "direction"), "Signal nuevo debe tener direction (no side)"
            assert hasattr(result, "strategy_id"), "Signal nuevo debe tener strategy_id"
            assert result.direction in ("BUY", "SELL")
            assert len(result.signal_id) > 0
            # No debe tener campos del Signal viejo
            assert not hasattr(result, "amount"), "Signal nuevo NO debe tener amount"
            assert not hasattr(result, "reduce_only"), "Signal nuevo NO debe tener reduce_only"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
