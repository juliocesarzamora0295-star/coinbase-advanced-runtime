"""
Tests para PositionSizer modos de sizing y nomenclatura correcta.

Invariantes testeadas:
- notional_pct sin stop → SizingMode.NOTIONAL
- notional_pct con stop → SizingMode.STOP_BASED
- risk_per_trade_pct funciona como compat alias de notional_pct
- notional_pct tiene prioridad sobre risk_per_trade_pct si ambos se pasan
- notional_budget_used refleja fracción del equity como notional
- risk_budget_used es alias de notional_budget_used
- SizingDecision.rationale refleja modo correcto
"""

from decimal import Decimal

from src.risk.position_sizer import (
    FailClosedError,
    PositionSizer,
    SizingDecision,
    SizingMode,
    SymbolConstraints,
)

CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("Infinity"),
    min_notional=Decimal("1"),
)


class TestSizingModeNotional:
    """Sin stop_price → SizingMode.NOTIONAL."""

    def test_no_stop_gives_notional_mode(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.sizing_mode == SizingMode.NOTIONAL
        assert "NOTIONAL" in d.rationale

    def test_notional_qty_correct(self):
        """qty = (equity * pct) / price = (10000 * 0.01) / 50000 = 0.002"""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        # 10000 * 0.01 / 50000 = 0.002
        assert d.target_qty == Decimal("0.00200")

    def test_notional_budget_used_matches(self):
        """notional_budget_used = notional / equity."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        expected = d.target_notional / Decimal("10000")
        assert d.notional_budget_used == expected


class TestSizingModeStopBased:
    """Con stop_price → SizingMode.STOP_BASED."""

    def test_with_stop_gives_stop_mode(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("49000"),
        )
        assert d.sizing_mode == SizingMode.STOP_BASED
        assert "STOP_BASED" in d.rationale
        assert "stop=" in d.rationale

    def test_stop_based_qty_correct(self):
        """qty = risk_amount / stop_distance = 100 / 1000 = 0.1"""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),  # risk_amount = 100
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("49000"),  # stop_distance = 1000
        )
        assert d.target_qty == Decimal("0.10000")

    def test_stop_distance_zero_falls_back_to_notional(self):
        """stop == entry → stop_distance = 0 → fallback a notional mode."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("50000"),  # same as entry
        )
        assert d.sizing_mode == SizingMode.NOTIONAL


class TestCompatAlias:
    """Backward compat: risk_per_trade_pct still accepted at sizer level."""

    def test_legacy_param_still_accepted(self):
        """risk_per_trade_pct still works as fallback in PositionSizer."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            risk_per_trade_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.target_qty > Decimal("0")

    def test_notional_pct_takes_priority(self):
        """notional_pct has priority over risk_per_trade_pct."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.02"),
            risk_per_trade_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.target_qty == Decimal("0.00400")

    def test_risk_budget_used_alias(self):
        """risk_budget_used is an alias for notional_budget_used."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.risk_budget_used == d.notional_budget_used


class TestNoneInputs:
    """Both pct params None → zero sizing."""

    def test_no_pct_returns_zero(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert d.target_qty == Decimal("0")
        assert "None" in d.rationale
