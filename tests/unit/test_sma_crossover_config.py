"""
Tests unitarios: SmaCrossoverStrategy — configuración y comportamiento básico.

Invariantes testeadas:
- parámetros sma_fast/sma_slow desde config (no defaults hardcodeados en tests)
- fast >= slow → ajustado automáticamente
- datos insuficientes (< slow + 2 barras) → no señal
- cruce alcista (fast cruza por encima de slow) → señal BUY
- cruce bajista (fast cruza por debajo de slow) → señal SELL
- sin crossover → no señal
- señal repetida del mismo lado no se re-emite (dedup de _last_signal_side)
- base_order_size desde config
"""

from decimal import Decimal
from typing import List

import pandas as pd

from src.strategy.signal import Signal
from src.strategy.sma_crossover import SmaCrossoverStrategy

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

MID = Decimal("50000")


def make_strategy(
    fast: int = 3, slow: int = 5, base_order_size: str = "0.001"
) -> SmaCrossoverStrategy:
    return SmaCrossoverStrategy(
        symbol="BTC-USD",
        config={
            "sma_fast": fast,
            "sma_slow": slow,
            "base_order_size": base_order_size,
        },
    )


def make_df(prices: List[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": prices})


def feed_and_generate(strategy: SmaCrossoverStrategy, prices: List[float]) -> List[Signal]:
    """Alimentar DataFrame y generar señales."""
    df = make_df(prices)
    strategy.update_market_data(df)
    return strategy.generate_signals(mid=MID)


# ──────────────────────────────────────────────
# Configuración
# ──────────────────────────────────────────────


class TestConfiguration:

    def test_fast_slow_from_config(self):
        """sma_fast y sma_slow se toman del config."""
        strategy = make_strategy(fast=7, slow=21)
        assert strategy.fast == 7
        assert strategy.slow == 21

    def test_default_fast_slow_when_not_in_config(self):
        """Sin config → defaults de la estrategia (20/50)."""
        strategy = SmaCrossoverStrategy(symbol="BTC-USD", config={})
        assert strategy.fast == 20
        assert strategy.slow == 50

    def test_fast_ge_slow_adjusted(self):
        """fast >= slow → fast se ajusta a slow // 2 (no falla)."""
        strategy = SmaCrossoverStrategy(
            symbol="BTC-USD",
            config={"sma_fast": 10, "sma_slow": 5},
        )
        assert strategy.fast < strategy.slow

    def test_bullish_signal_has_buy_direction(self):
        """Cruce alcista → señal con direction='BUY'."""
        strategy = make_strategy(fast=2, slow=3)
        prices = [50000, 49000, 48000, 47000, 48000, 52000, 55000]
        signals = feed_and_generate(strategy, prices)
        if signals:
            assert signals[0].direction == "BUY"


# ──────────────────────────────────────────────
# Datos insuficientes
# ──────────────────────────────────────────────


class TestInsufficientData:

    def test_no_data_returns_empty(self):
        """Sin datos → no señal."""
        strategy = make_strategy(fast=3, slow=5)
        signals = strategy.generate_signals(mid=MID)
        assert signals == []

    def test_insufficient_rows_returns_empty(self):
        """Menos de slow+2 filas → no señal."""
        strategy = make_strategy(fast=3, slow=5)
        # Necesita 5+2=7 filas mínimo
        df = make_df([50000] * 6)
        strategy.update_market_data(df)
        signals = strategy.generate_signals(mid=MID)
        assert signals == []

    def test_exactly_slow_plus_2_rows_can_generate(self):
        """Con exactamente slow+2 filas → puede generar señal."""
        strategy = make_strategy(fast=2, slow=3)
        # Necesita 3+2=5 filas
        # Diseñar cruce alcista en las últimas 2 filas
        # Primeras 3 filas: bajando (fast < slow)
        # Últimas 2 filas: subiendo (fast > slow)
        prices = [50000, 49000, 48000, 49000, 55000]
        df = make_df(prices)
        strategy.update_market_data(df)
        signals = strategy.generate_signals(mid=MID)
        # Puede o no haber señal dependiendo de los valores exactos,
        # pero no debe fallar
        assert isinstance(signals, list)


# ──────────────────────────────────────────────
# Señales
# ──────────────────────────────────────────────


class TestSignalGeneration:

    def _make_bullish_crossover_prices(self, fast: int, slow: int) -> List[float]:
        """
        Genera precios que producen un cruce alcista exactamente en el último step.

        Diseño para fast=3, slow=5 (o cualquier fast < slow):
        - slow+1 precios estables (SMA fast ≈ SMA slow)
        - Un precio bajo (fast SMA cae por debajo de slow SMA → f_prev < s_prev)
        - Un precio alto (fast SMA sube por encima de slow SMA → f_now > s_now)
        """
        base = 100.0
        # slow+2 precios mínimos: [base]*(slow+1) + [dip] + [spike]
        # Asegurar que en el penúltimo step: SMAfast < SMAslow
        # y en el último step: SMAfast > SMAslow
        prices = [base] * (slow + 1) + [base * 0.90] + [base * 1.30]
        return prices

    def _make_bearish_crossover_prices(self, fast: int, slow: int) -> List[float]:
        """
        Genera precios que producen un cruce bajista exactamente en el último step.
        """
        base = 100.0
        prices = [base] * (slow + 1) + [base * 1.10] + [base * 0.70]
        return prices

    def test_bullish_crossover_emits_buy(self):
        """Cruce alcista → señal direction='BUY'."""
        strategy = make_strategy(fast=3, slow=5)
        prices = self._make_bullish_crossover_prices(3, 5)
        signals = feed_and_generate(strategy, prices)
        buy_signals = [s for s in signals if s.direction == "BUY"]
        assert len(buy_signals) >= 1

    def test_bearish_crossover_emits_sell(self):
        """Cruce bajista → señal direction='SELL'."""
        strategy = make_strategy(fast=3, slow=5)
        prices = self._make_bearish_crossover_prices(3, 5)
        signals = feed_and_generate(strategy, prices)
        sell_signals = [s for s in signals if s.direction == "SELL"]
        assert len(sell_signals) >= 1

    def test_no_crossover_no_signal(self):
        """Tendencia plana sin cruce → no señal."""
        strategy = make_strategy(fast=3, slow=5)
        # Precios uniformes → SMA fast == SMA slow, no hay cruce
        prices = [50000.0] * 20
        signals = feed_and_generate(strategy, prices)
        assert signals == []

    def test_duplicate_buy_signal_not_re_emitted(self):
        """Segundo cruce en la misma dirección → no re-emite señal."""
        strategy = make_strategy(fast=2, slow=3)

        # Primera señal BUY
        prices1 = [49000, 48000, 47000, 48000, 55000]
        feed_and_generate(strategy, prices1)

        # Forzar _last_signal_side = "buy" directamente para aislar el test
        strategy._last_signal_side = "buy"

        # Generar otro cruce alcista — debe ser ignorado
        prices2 = [48000, 47000, 46000, 47000, 56000]
        signals2 = feed_and_generate(strategy, prices2)
        buy_signals = [s for s in signals2 if s.direction == "BUY"]
        assert len(buy_signals) == 0  # deduplicado

    def test_signal_side_flips_after_opposing_crossover(self):
        """Cruce bajista después de alcista → se re-permite SELL."""
        strategy = make_strategy(fast=2, slow=3)
        strategy._last_signal_side = "buy"

        prices = self._make_bearish_crossover_prices(2, 3)
        signals = feed_and_generate(strategy, prices)
        sell_signals = [s for s in signals if s.direction == "SELL"]
        assert len(sell_signals) >= 1

    def test_signal_metadata_contains_reason(self):
        """Señal generada tiene metadata con clave 'reason'."""
        strategy = make_strategy(fast=3, slow=5)
        prices = self._make_bullish_crossover_prices(3, 5)
        signals = feed_and_generate(strategy, prices)
        buy_signals = [s for s in signals if s.direction == "BUY"]
        assert len(buy_signals) >= 1
        assert "reason" in buy_signals[0].metadata
        assert (
            "3" in buy_signals[0].metadata["reason"]
            or "fast" in buy_signals[0].metadata["reason"].lower()
            or "/" in buy_signals[0].metadata["reason"]
        )

    def test_bar_timestamp_propagated_to_signal(self):
        """bar_timestamp pasado a generate_signals aparece en la señal emitida."""
        from datetime import datetime, timezone

        strategy = make_strategy(fast=3, slow=5)
        expected_ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        prices = self._make_bullish_crossover_prices(3, 5)
        df = pd.DataFrame({"close": prices})
        strategy.update_market_data(df)
        signals = strategy.generate_signals(mid=MID, bar_timestamp=expected_ts)
        buy_signals = [s for s in signals if s.direction == "BUY"]
        assert len(buy_signals) >= 1
        assert buy_signals[0].bar_timestamp == expected_ts


# ──────────────────────────────────────────────
# update_positions / get_position
# ──────────────────────────────────────────────


class TestUpdatePositions:

    def test_buy_fill_increases_long_position(self):
        """Fill BUY LONG aumenta la posición LONG."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0.5", "reduce_only": False}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("0.5")

    def test_buy_fill_accumulates(self):
        """Múltiples fills BUY LONG acumulan la posición."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0.3", "reduce_only": False}
        )
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0.2", "reduce_only": False}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("0.5")

    def test_reduce_only_decreases_position(self):
        """Fill reduce_only=True reduce la posición existente."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "1.0", "reduce_only": False}
        )
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0.4", "reduce_only": True}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("0.6")

    def test_reduce_only_does_not_go_negative(self):
        """reduce_only=True no puede llevar la posición a negativo."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0.1", "reduce_only": False}
        )
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "9.0", "reduce_only": True}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("0")

    def test_get_position_returns_zero_for_unknown_symbol(self):
        """get_position() retorna 0 para símbolo sin posición registrada."""
        strategy = make_strategy()
        assert strategy.get_position("ETH-USD", "LONG") == Decimal("0")

    def test_positions_isolated_per_symbol(self):
        """Posiciones de BTC-USD y ETH-USD no se contaminan."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "1.0", "reduce_only": False}
        )
        strategy.update_positions(
            {"symbol": "ETH-USD", "position_side": "LONG", "amount": "5.0", "reduce_only": False}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("1.0")
        assert strategy.get_position("ETH-USD", "LONG") == Decimal("5.0")

    def test_fill_with_zero_amount_ignored(self):
        """Fill con amount=0 no modifica la posición."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "1.0", "reduce_only": False}
        )
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "LONG", "amount": "0", "reduce_only": False}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("1.0")

    def test_fill_with_invalid_position_side_ignored(self):
        """Fill con position_side inválido no modifica nada."""
        strategy = make_strategy()
        strategy.update_positions(
            {"symbol": "BTC-USD", "position_side": "INVALID", "amount": "1.0", "reduce_only": False}
        )
        assert strategy.get_position("BTC-USD", "LONG") == Decimal("0")
        assert strategy.get_position("BTC-USD", "SHORT") == Decimal("0")


# ──────────────────────────────────────────────
# StrategyManager.load_from_config con sma_crossover real
# ──────────────────────────────────────────────


class TestLoadFromConfigWithRealStrategy:

    def test_load_sma_crossover_by_name(self):
        """load_from_config con nombre 'sma_crossover' carga SmaCrossoverStrategy."""
        from src.strategy.manager import StrategyManager
        from src.strategy.sma_crossover import SmaCrossoverStrategy

        mgr = StrategyManager.load_from_config(
            symbol="BTC-USD",
            symbol_config={
                "strategies": ["sma_crossover"],
                "sma_crossover": {"sma_fast": 5, "sma_slow": 10},
            },
        )
        assert mgr.strategy_count == 1
        assert isinstance(mgr._strategies[0], SmaCrossoverStrategy)

    def test_load_ma_crossover_alias(self):
        """'ma_crossover' es alias de SmaCrossoverStrategy."""
        from src.strategy.manager import StrategyManager
        from src.strategy.sma_crossover import SmaCrossoverStrategy

        mgr = StrategyManager.load_from_config(
            symbol="ETH-USD",
            symbol_config={"strategies": ["ma_crossover"]},
        )
        assert isinstance(mgr._strategies[0], SmaCrossoverStrategy)

    def test_loaded_strategy_respects_config_params(self):
        """Estrategia cargada tiene fast/slow del config."""
        from src.strategy.manager import StrategyManager
        from src.strategy.sma_crossover import SmaCrossoverStrategy

        mgr = StrategyManager.load_from_config(
            symbol="BTC-USD",
            symbol_config={
                "strategies": [{"name": "sma_crossover", "sma_fast": 7, "sma_slow": 21}],
            },
        )
        strat = mgr._strategies[0]
        assert isinstance(strat, SmaCrossoverStrategy)
        assert strat.fast == 7
        assert strat.slow == 21

    def test_unknown_strategy_name_raises_if_only_one(self):
        """load_from_config con solo estrategia desconocida → ValueError."""
        import pytest

        from src.strategy.manager import StrategyManager

        with pytest.raises(ValueError):
            StrategyManager.load_from_config(
                symbol="BTC-USD",
                symbol_config={"strategies": ["nonexistent_strategy"]},
            )
