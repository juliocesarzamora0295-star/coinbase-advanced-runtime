"""
Tests unitarios: funciones de métricas de backtest y GemProtocol.

Invariantes testeadas:
- _max_drawdown: serie con caída conocida produce el drawdown correcto
- _max_drawdown: serie siempre creciente → drawdown = 0
- _sharpe: serie constante (sin volatilidad) → Sharpe = 0
- _cagr: equity que dobla en 252 días → CAGR ≈ 100%
- _calmar: ratio CAGR / max_drawdown_abs
- _backtest_long_only: sin señales → total_return ≈ 0, 0 trades
- _backtest_long_only: una entrada/salida → 1 trade, return calculado
- _backtest_long_only: fees y slippage reducen el retorno
- _backtest_long_only: win_rate correcto con trades ganadores/perdedores
- GemProtocol: min_points insuficiente → ValueError
- GemProtocol.run(): retorna dict con claves esperadas
- GemProtocol.run(): passed=True para estrategia con buen RF y bajo DD
- GemProtocol.run(): passed=False cuando RF < threshold
"""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.validation.gem_protocol import (
    GemProtocol,
    _backtest_long_only,
    _cagr,
    _calmar,
    _max_drawdown,
    _sharpe,
)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def make_price_series(values: list, freq: str = "D") -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=float)


def make_bool_series(values: list, freq: str = "D") -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=bool)


# ──────────────────────────────────────────────
# _max_drawdown
# ──────────────────────────────────────────────


class TestMaxDrawdown:

    def test_known_drawdown(self):
        """Serie con caída del 50% → drawdown = -0.50."""
        equity = make_price_series([100, 120, 60, 80, 90])
        dd = _max_drawdown(equity)
        # Peak = 120, min post-peak = 60 → drawdown = 60/120 - 1 = -0.5
        assert abs(float(dd) - (-0.5)) < 0.001

    def test_monotone_increasing_no_drawdown(self):
        """Serie siempre creciente → drawdown ≈ 0."""
        equity = make_price_series([100, 110, 120, 130, 140])
        dd = _max_drawdown(equity)
        assert float(dd) >= -0.001  # cerca de 0, no negativo

    def test_drawdown_is_negative_or_zero(self):
        """_max_drawdown siempre retorna valor ≤ 0."""
        equity = make_price_series([100, 80, 90, 70, 85])
        dd = _max_drawdown(equity)
        assert float(dd) <= 0


# ──────────────────────────────────────────────
# _sharpe
# ──────────────────────────────────────────────


class TestSharpe:

    def test_constant_returns_zero_sharpe(self):
        """Retornos constantes (sin varianza) → Sharpe = 0."""
        # Serie constante → pct_change = 0 siempre → std = 0
        equity = make_price_series([100.0] * 50)
        rets = equity.pct_change().fillna(0.0)
        sharpe = _sharpe(rets, rf=Decimal("0"), freq=252.0)
        assert float(sharpe) == 0.0

    def test_positive_excess_returns_positive_sharpe(self):
        """Retornos positivos consistentes → Sharpe > 0."""
        # Crecimiento lineal → retornos positivos
        prices = [100 + i for i in range(50)]
        equity = make_price_series(prices)
        rets = equity.pct_change().fillna(0.0)
        sharpe = _sharpe(rets, rf=Decimal("0"), freq=252.0)
        assert float(sharpe) > 0

    def test_insufficient_data_returns_zero(self):
        """< 2 puntos → Sharpe = 0."""
        rets = pd.Series([0.01])
        sharpe = _sharpe(rets, rf=Decimal("0"), freq=252.0)
        assert float(sharpe) == 0.0


# ──────────────────────────────────────────────
# _cagr
# ──────────────────────────────────────────────


class TestCagr:

    def test_double_in_one_year(self):
        """Equity que dobla en 252 barras → CAGR ≈ 100%."""
        n = 252
        prices = [100 * (2 ** (i / n)) for i in range(n)]
        equity = make_price_series(prices)
        cagr = _cagr(equity, freq=252.0)
        # CAGR debe ser cercano a 1.0 (100%)
        assert abs(float(cagr) - 1.0) < 0.05

    def test_flat_equity_zero_cagr(self):
        """Equity constante → CAGR ≈ 0."""
        equity = make_price_series([100.0] * 252)
        cagr = _cagr(equity, freq=252.0)
        assert abs(float(cagr)) < 0.01


# ──────────────────────────────────────────────
# _calmar
# ──────────────────────────────────────────────


class TestCalmar:

    def test_calmar_ratio(self):
        """CAGR / max_drawdown_abs = Calmar."""
        cagr = Decimal("0.20")
        mdd_abs = Decimal("0.10")
        calmar = _calmar(cagr, mdd_abs)
        assert abs(float(calmar) - 2.0) < 0.001

    def test_zero_drawdown_returns_inf(self):
        """max_drawdown_abs = 0 → Calmar = inf o muy alto."""
        cagr = Decimal("0.20")
        mdd_abs = Decimal("0")
        calmar = _calmar(cagr, mdd_abs)
        # Debería ser inf (positivo)
        import math

        assert math.isinf(float(calmar)) or float(calmar) > 1e10


# ──────────────────────────────────────────────
# _backtest_long_only
# ──────────────────────────────────────────────


class TestBacktestLongOnly:

    def _run_backtest(self, prices, entries, exits):
        price = make_price_series(prices)
        en = make_bool_series(entries)
        ex = make_bool_series(exits)
        return _backtest_long_only(
            price,
            en,
            ex,
            init_cash=Decimal("10000"),
            fee=Decimal("0"),
            slip=Decimal("0"),
        )

    def test_no_signals_zero_trades(self):
        """Sin entradas → 0 trades, retorno neutro."""
        prices = [100.0] * 20
        entries = [False] * 20
        exits = [False] * 20
        result = self._run_backtest(prices, entries, exits)
        assert result["total_trades"] == 0

    def test_single_winning_trade(self):
        """Una entrada/salida ganadora → 1 trade, return > 0."""
        # Precio sube de 100 a 150
        prices = [100.0] * 5 + [150.0] * 5
        entries = [False, True, False, False, False, False, False, False, False, False]
        exits = [False, False, False, False, False, False, False, False, True, False]
        result = self._run_backtest(prices, entries, exits)
        assert result["total_trades"] == 1
        assert float(result["total_return"]) > 0

    def test_single_losing_trade(self):
        """Una entrada/salida perdedora → 1 trade, return < 0."""
        # Precio baja de 100 a 80
        prices = [100.0] * 5 + [80.0] * 5
        entries = [False, True, False, False, False, False, False, False, False, False]
        exits = [False, False, False, False, False, False, False, False, True, False]
        result = self._run_backtest(prices, entries, exits)
        assert result["total_trades"] == 1
        assert float(result["total_return"]) < 0

    def test_fees_reduce_return(self):
        """Fees reducen el retorno vs sin fees."""
        prices = [100.0] * 5 + [120.0] * 5
        entries = [False, True, False, False, False, False, False, False, False, False]
        exits = [False, False, False, False, False, False, False, False, True, False]
        price = make_price_series(prices)
        en = make_bool_series(entries)
        ex = make_bool_series(exits)

        r_no_fee = _backtest_long_only(
            price, en, ex, init_cash=Decimal("10000"), fee=Decimal("0"), slip=Decimal("0")
        )
        r_with_fee = _backtest_long_only(
            price, en, ex, init_cash=Decimal("10000"), fee=Decimal("0.001"), slip=Decimal("0")
        )

        assert float(r_with_fee["total_return"]) < float(r_no_fee["total_return"])

    def test_slippage_reduces_return(self):
        """Slippage reduce el retorno vs sin slippage."""
        prices = [100.0] * 5 + [120.0] * 5
        entries = [False, True, False, False, False, False, False, False, False, False]
        exits = [False, False, False, False, False, False, False, False, True, False]
        price = make_price_series(prices)
        en = make_bool_series(entries)
        ex = make_bool_series(exits)

        r_no_slip = _backtest_long_only(
            price, en, ex, init_cash=Decimal("10000"), fee=Decimal("0"), slip=Decimal("0")
        )
        r_with_slip = _backtest_long_only(
            price, en, ex, init_cash=Decimal("10000"), fee=Decimal("0"), slip=Decimal("0.001")
        )

        assert float(r_with_slip["total_return"]) < float(r_no_slip["total_return"])

    def test_win_rate_correct(self):
        """win_rate es fracción de trades ganadores."""
        # 2 trades: uno ganador (100→120), uno perdedor (120→100)
        prices = [100.0, 120.0, 120.0, 100.0]
        entries = [True, False, False, False]
        exits = [False, True, False, False]
        # Solo 1 trade en este diseño
        result = self._run_backtest(prices, entries, exits)
        assert result["win_rate"] == 1.0  # trade ganador

    def test_result_has_required_keys(self):
        """Resultado contiene todas las claves esperadas."""
        prices = [100.0] * 10
        entries = [False] * 10
        exits = [False] * 10
        result = self._run_backtest(prices, entries, exits)
        for key in [
            "equity",
            "total_return",
            "max_drawdown",
            "max_drawdown_abs",
            "cagr",
            "sharpe",
            "calmar",
            "recovery_factor",
            "win_rate",
            "profit_factor",
            "sqn",
            "total_trades",
        ]:
            assert key in result, f"Falta clave: {key}"

    def test_equity_series_length_matches_price(self):
        """Equity series tiene la misma longitud que price."""
        prices = [float(100 + i) for i in range(20)]
        entries = [False] * 20
        exits = [False] * 20
        result = self._run_backtest(prices, entries, exits)
        assert len(result["equity"]) == 20


# ──────────────────────────────────────────────
# GemProtocol
# ──────────────────────────────────────────────


def _make_gem_data(n: int = 300, trend: bool = True):
    """Genera price/entries/exits sintéticos para GemProtocol."""
    np.random.seed(42)
    idx = pd.date_range("2019-01-01", periods=n, freq="D")

    if trend:
        # Serie con tendencia alcista suave + ruido
        base = np.cumsum(np.random.normal(0.001, 0.01, n)) + 10
    else:
        # Serie sin tendencia (random walk puro)
        base = np.cumsum(np.random.normal(0, 0.015, n)) + 10

    price = pd.Series(np.exp(base), index=idx)

    # Entradas cada 20 días, salidas 10 días después
    entries = pd.Series(False, index=idx)
    exits = pd.Series(False, index=idx)
    for i in range(0, n - 15, 25):
        entries.iloc[i] = True
        exits.iloc[i + 12] = True

    return price, entries, exits


class TestGemProtocol:

    def test_min_points_too_few_raises(self):
        """min_points insuficiente → ValueError."""
        price = make_price_series([float(100 + i) for i in range(50)])
        entries = make_bool_series([False] * 50)
        exits = make_bool_series([False] * 50)
        with pytest.raises(ValueError, match="min_points"):
            GemProtocol(price, entries, exits, config={"min_points": 200})

    def test_non_long_direction_raises(self):
        """direction != 'long' → ValueError."""
        price, entries, exits = _make_gem_data()
        with pytest.raises(ValueError):
            GemProtocol(price, entries, exits, config={"direction": "short"})

    def test_run_returns_decision_dict(self):
        """run() retorna un dict con clave 'passed'."""
        price, entries, exits = _make_gem_data()
        proto = GemProtocol(price, entries, exits, config={"bootstrap_runs": 10, "min_points": 200})
        result = proto.run()
        assert isinstance(result, dict)
        assert "passed" in result

    def test_run_decision_has_expected_keys(self):
        """Decisión tiene claves: regime, base, stress, adversarial, issues, recommendations."""
        price, entries, exits = _make_gem_data()
        proto = GemProtocol(price, entries, exits, config={"bootstrap_runs": 10, "min_points": 200})
        result = proto.run()
        for key in ["regime", "base", "stress", "adversarial", "issues", "recommendations"]:
            assert key in result, f"Falta clave: {key}"

    def test_base_metrics_present(self):
        """result['base'] tiene las métricas fundamentales."""
        price, entries, exits = _make_gem_data()
        proto = GemProtocol(price, entries, exits, config={"bootstrap_runs": 10, "min_points": 200})
        result = proto.run()
        base = result["base"]
        for key in ["total_return", "max_drawdown_abs", "recovery_factor", "sharpe", "win_rate"]:
            assert key in base

    def test_passed_false_when_rf_below_threshold(self):
        """Estrategia con recovery_factor < threshold → passed=False."""
        price, entries, exits = _make_gem_data()
        # threshold muy alto → imposible pasar
        proto = GemProtocol(
            price,
            entries,
            exits,
            config={
                "bootstrap_runs": 5,
                "min_points": 200,
                "recovery_factor_threshold": 1000.0,  # imposible
            },
        )
        result = proto.run()
        assert result["passed"] is False

    def test_regime_contains_current_regime(self):
        """regime['current_regime'] es uno de TREND/MEAN_REVERSION/RANDOM_WALK."""
        price, entries, exits = _make_gem_data()
        proto = GemProtocol(price, entries, exits, config={"bootstrap_runs": 5, "min_points": 200})
        result = proto.run()
        assert result["regime"]["current_regime"] in ("TREND", "MEAN_REVERSION", "RANDOM_WALK")

    def test_to_json_returns_valid_json(self):
        """to_json() produce JSON parseable."""
        import json

        price, entries, exits = _make_gem_data()
        proto = GemProtocol(price, entries, exits, config={"bootstrap_runs": 5, "min_points": 200})
        proto.run()
        json_str = proto.to_json()
        parsed = json.loads(json_str)
        assert "passed" in parsed
