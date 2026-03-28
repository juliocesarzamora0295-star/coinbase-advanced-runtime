"""
Tests unitarios: PositionSizer.

Invariantes testeadas:
- equity=None → FailClosedError (fail-closed)
- equity=0 → target_qty=0, no error
- risk_pct=0 → target_qty=0, no error
- entry_price=0 → target_qty=0, no error
- qty correcta dado equity y risk_pct (sin stop)
- qty correcta dado equity, risk_pct y stop_price
- qty respeta step_size (floor, nunca exceder)
- qty no excede max_notional
- qty no excede max_qty del símbolo
- qty < min_qty → target_qty=0 (inviable)
- target_notional = target_qty * entry_price
- risk_budget_used = target_notional / equity
- SizingDecision es inmutable (frozen)
- mismos inputs → mismo output (determinismo)
"""

from decimal import Decimal

import pytest

from src.risk.position_sizer import (
    FailClosedError,
    PositionSizer,
    SizingDecision,
    SymbolConstraints,
)

# ──────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("10000"),
    min_notional=Decimal("1"),
)

LARGE_MAX_NOTIONAL = Decimal("1_000_000")


def compute(
    equity: str = "10000",
    entry_price: str = "50000",
    risk_pct: str = "0.01",
    constraints: SymbolConstraints = BTC_CONSTRAINTS,
    max_notional: str = "100000",
    stop_price: str | None = None,
) -> SizingDecision:
    sizer = PositionSizer()
    return sizer.compute(
        symbol="BTC-USD",
        equity=Decimal(equity) if equity is not None else None,
        entry_price=Decimal(entry_price),
        risk_per_trade_pct=Decimal(risk_pct),
        constraints=constraints,
        max_notional=Decimal(max_notional),
        stop_price=Decimal(stop_price) if stop_price else None,
    )


# ──────────────────────────────────────────────
# Fail-closed
# ──────────────────────────────────────────────


class TestFailClosed:

    def test_equity_none_raises_fail_closed_error(self):
        """equity=None → FailClosedError. No defaults, no trading."""
        sizer = PositionSizer()
        with pytest.raises(FailClosedError):
            sizer.compute(
                symbol="BTC-USD",
                equity=None,
                entry_price=Decimal("50000"),
                risk_per_trade_pct=Decimal("0.01"),
                constraints=BTC_CONSTRAINTS,
                max_notional=Decimal("100000"),
            )

    def test_fail_closed_error_message_contains_symbol(self):
        """FailClosedError contiene el símbolo en el mensaje."""
        sizer = PositionSizer()
        with pytest.raises(FailClosedError, match="BTC-USD"):
            sizer.compute(
                symbol="BTC-USD",
                equity=None,
                entry_price=Decimal("50000"),
                risk_per_trade_pct=Decimal("0.01"),
                constraints=BTC_CONSTRAINTS,
                max_notional=Decimal("100000"),
            )


# ──────────────────────────────────────────────
# Casos degenerados (no error, qty=0)
# ──────────────────────────────────────────────


class TestDegenerateCases:

    def test_equity_zero_returns_qty_zero(self):
        """equity=0 → target_qty=0, sin FailClosedError."""
        result = compute(equity="0")
        assert result.target_qty == Decimal("0")
        assert result.target_notional == Decimal("0")

    def test_equity_negative_returns_qty_zero(self):
        """equity negativo → target_qty=0."""
        result = compute(equity="-500")
        assert result.target_qty == Decimal("0")

    def test_risk_pct_zero_returns_qty_zero(self):
        """risk_pct=0 → target_qty=0, sin error."""
        result = compute(risk_pct="0")
        assert result.target_qty == Decimal("0")

    def test_risk_pct_negative_returns_qty_zero(self):
        """risk_pct negativo → target_qty=0."""
        result = compute(risk_pct="-0.01")
        assert result.target_qty == Decimal("0")

    def test_entry_price_zero_returns_qty_zero(self):
        """entry_price=0 → target_qty=0, sin división por cero."""
        result = compute(entry_price="0")
        assert result.target_qty == Decimal("0")

    def test_entry_price_negative_returns_qty_zero(self):
        """entry_price negativo → target_qty=0."""
        result = compute(entry_price="-1")
        assert result.target_qty == Decimal("0")


# ──────────────────────────────────────────────
# Cálculo correcto (sin stop)
# ──────────────────────────────────────────────


class TestBasicSizing:

    def test_qty_correct_percentage_sizing(self):
        """qty = (equity * risk_pct) / entry_price cuantizado a step_size."""
        # risk_amount = 10000 * 0.01 = 100
        # qty_raw = 100 / 50000 = 0.002
        result = compute(equity="10000", entry_price="50000", risk_pct="0.01")
        assert result.target_qty == Decimal("0.002")

    def test_target_notional_equals_qty_times_price(self):
        """target_notional = target_qty * entry_price."""
        result = compute(equity="10000", entry_price="50000", risk_pct="0.01")
        expected_notional = result.target_qty * Decimal("50000")
        assert result.target_notional == expected_notional

    def test_risk_budget_used_equals_notional_over_equity(self):
        """risk_budget_used = target_notional / equity."""
        result = compute(equity="10000", entry_price="50000", risk_pct="0.01")
        expected_budget = result.target_notional / Decimal("10000")
        assert result.risk_budget_used == expected_budget

    def test_larger_equity_produces_larger_qty(self):
        """A igual risk_pct, mayor equity → mayor qty."""
        r1 = compute(equity="10000")
        r2 = compute(equity="20000")
        assert r2.target_qty > r1.target_qty

    def test_higher_risk_pct_produces_larger_qty(self):
        """A igual equity, mayor risk_pct → mayor qty."""
        r1 = compute(risk_pct="0.01")
        r2 = compute(risk_pct="0.05")
        assert r2.target_qty > r1.target_qty


# ──────────────────────────────────────────────
# Sizing con stop_price
# ──────────────────────────────────────────────


class TestStopBasedSizing:

    def test_stop_based_sizing_correct(self):
        """qty = risk_amount / stop_distance cuando stop_price está disponible."""
        # risk_amount = 10000 * 0.01 = 100
        # stop_distance = |50000 - 49000| = 1000
        # qty_raw = 100 / 1000 = 0.1
        result = compute(
            equity="10000",
            entry_price="50000",
            risk_pct="0.01",
            stop_price="49000",
        )
        assert result.target_qty == Decimal("0.1")

    def test_tight_stop_produces_larger_qty(self):
        """Stop más cercano → menor stop_distance → mayor qty."""
        wide_stop = compute(entry_price="50000", stop_price="45000")  # distance=5000
        tight_stop = compute(entry_price="50000", stop_price="49500")  # distance=500
        assert tight_stop.target_qty > wide_stop.target_qty

    def test_stop_zero_falls_back_to_pct_sizing(self):
        """stop_price=0 → fallback a sizing por porcentaje."""
        result_no_stop = compute(stop_price=None)
        result_zero_stop = compute(stop_price="0")
        # Ambos deben producir la misma qty (fallback idéntico)
        assert result_zero_stop.target_qty == result_no_stop.target_qty


# ──────────────────────────────────────────────
# Step size (cuantización)
# ──────────────────────────────────────────────


class TestStepSizeQuantization:

    def test_qty_quantized_to_step_size(self):
        """qty se trunca a múltiplo de step_size (floor, nunca exceder)."""
        constraints = SymbolConstraints(
            step_size=Decimal("0.1"),
            min_qty=Decimal("0.1"),
            max_qty=Decimal("1000"),
            min_notional=Decimal("1"),
        )
        # risk_amount = 10000 * 0.015 = 150
        # qty_raw = 150 / 50000 = 0.003 → floor a step_size=0.1 → 0.0
        # qty_raw = 150 → price = 100 → 1.5, floor to 0.1 → 1.5 ok
        sizer = PositionSizer()
        result = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("100"),
            risk_per_trade_pct=Decimal("0.015"),
            constraints=constraints,
            max_notional=Decimal("100000"),
        )
        # qty_raw = 150 / 100 = 1.5, step_size=0.1 → 1.5
        assert result.target_qty % Decimal("0.1") == Decimal("0")

    def test_step_size_floor_not_round(self):
        """Cuantización es FLOOR, no round."""
        constraints = SymbolConstraints(
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            max_qty=Decimal("1000"),
            min_notional=Decimal("1"),
        )
        sizer = PositionSizer()
        # qty_raw = (10000 * 0.001) / 50000 = 10 / 50000 = 0.0002
        # floor to step_size=0.01 → 0.00 (below min_qty → qty=0)
        result = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            risk_per_trade_pct=Decimal("0.001"),
            constraints=constraints,
            max_notional=Decimal("100000"),
        )
        # 0.0002 < 0.01 → inviable → qty=0
        assert result.target_qty == Decimal("0")

    def test_qty_is_exact_multiple_of_step_size(self):
        """target_qty es siempre múltiplo exacto de step_size."""
        result = compute(equity="10000", entry_price="50000", risk_pct="0.0123")
        if BTC_CONSTRAINTS.step_size > Decimal("0"):
            remainder = result.target_qty % BTC_CONSTRAINTS.step_size
            assert remainder == Decimal(
                "0"
            ), f"qty={result.target_qty} no múltiplo de {BTC_CONSTRAINTS.step_size}"


# ──────────────────────────────────────────────
# Caps
# ──────────────────────────────────────────────


class TestCaps:

    def test_qty_capped_by_max_notional(self):
        """qty nunca produce notional > max_notional."""
        result = compute(
            equity="1000000",
            entry_price="50000",
            risk_pct="0.10",
            max_notional="1000",  # cap agresivo
        )
        actual_notional = result.target_qty * Decimal("50000")
        assert actual_notional <= Decimal("1000")

    def test_qty_capped_by_max_qty(self):
        """qty nunca excede max_qty del símbolo."""
        tight_constraints = SymbolConstraints(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("0.01"),  # cap muy bajo
            min_notional=Decimal("1"),
        )
        sizer = PositionSizer()
        result = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("1_000_000"),
            entry_price=Decimal("50000"),
            risk_per_trade_pct=Decimal("0.10"),
            constraints=tight_constraints,
            max_notional=Decimal("1_000_000"),
        )
        assert result.target_qty <= Decimal("0.01")

    def test_qty_zero_when_below_min_qty(self):
        """qty < min_qty → target_qty=0 (inviable, no se envía)."""
        high_min = SymbolConstraints(
            step_size=Decimal("1"),
            min_qty=Decimal("100"),  # mínimo muy alto
            max_qty=Decimal("10000"),
            min_notional=Decimal("1"),
        )
        sizer = PositionSizer()
        # risk_amount = 10000 * 0.0001 = 1
        # qty = 1 / 50000 = 0.00002 → mucho menor que min_qty=100
        result = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            risk_per_trade_pct=Decimal("0.0001"),
            constraints=high_min,
            max_notional=Decimal("100000"),
        )
        assert result.target_qty == Decimal("0")


# ──────────────────────────────────────────────
# Determinismo e inmutabilidad
# ──────────────────────────────────────────────


class TestDeterminismAndImmutability:

    def test_same_inputs_same_output(self):
        """Mismos inputs → mismo output (determinismo)."""
        r1 = compute(equity="10000", entry_price="50000", risk_pct="0.02")
        r2 = compute(equity="10000", entry_price="50000", risk_pct="0.02")
        assert r1.target_qty == r2.target_qty
        assert r1.target_notional == r2.target_notional
        assert r1.risk_budget_used == r2.risk_budget_used

    def test_sizing_decision_is_immutable(self):
        """SizingDecision es frozen — no se puede modificar."""
        result = compute()
        with pytest.raises((AttributeError, TypeError)):
            result.target_qty = Decimal("999")  # type: ignore[misc]
