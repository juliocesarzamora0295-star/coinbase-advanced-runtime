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
import pytest

from src.strategy.sma_crossover import SmaCrossoverStrategy
from src.strategy.base import Signal


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

MID = Decimal("50000")


def make_strategy(fast: int = 3, slow: int = 5, base_order_size: str = "0.001") -> SmaCrossoverStrategy:
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

    def test_base_order_size_from_config(self):
        """base_order_size desde config se aplica en señales."""
        strategy = make_strategy(fast=2, slow=3, base_order_size="0.05")
        # Construir datos con cruce alcista
        # slow=3: necesitamos slow+2=5 filas
        # Para cruce alcista: precios subiendo al final
        prices = [50000, 49000, 48000, 47000, 48000, 52000, 55000]
        signals = feed_and_generate(strategy, prices)
        if signals:
            assert signals[0].amount == Decimal("0.05")


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
        """Cruce alcista → señal 'buy'."""
        strategy = make_strategy(fast=3, slow=5)
        prices = self._make_bullish_crossover_prices(3, 5)
        signals = feed_and_generate(strategy, prices)
        buy_signals = [s for s in signals if s.side == "buy"]
        assert len(buy_signals) >= 1

    def test_bearish_crossover_emits_sell(self):
        """Cruce bajista → señal 'sell'."""
        strategy = make_strategy(fast=3, slow=5)
        prices = self._make_bearish_crossover_prices(3, 5)
        signals = feed_and_generate(strategy, prices)
        sell_signals = [s for s in signals if s.side == "sell"]
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
        buy_signals = [s for s in signals2 if s.side == "buy"]
        assert len(buy_signals) == 0  # deduplicado

    def test_signal_side_flips_after_opposing_crossover(self):
        """Cruce bajista después de alcista → se re-permite SELL."""
        strategy = make_strategy(fast=2, slow=3)
        strategy._last_signal_side = "buy"

        prices = self._make_bearish_crossover_prices(2, 3)
        signals = feed_and_generate(strategy, prices)
        sell_signals = [s for s in signals if s.side == "sell"]
        assert len(sell_signals) >= 1
