"""
Tests para PositionSizer — modos semánticos y compute_from_snapshot.

Valida:
- Modo ALLOCATION: qty = (equity × target_notional_pct) / entry_price
- Modo RISK_BASED con stop: qty = (equity × risk_pct) / stop_distance
- Modo RISK_BASED sin stop: fallback a (equity × risk_pct) / entry_price
- Modo BOTH: min(qty_allocation, qty_risk) — conservador
- compute_from_snapshot: usa snapshot.equity real, no hardcoded
- Fail-closed: snapshot.equity=None → FailClosedError
- Determinismo: mismos inputs → misma decisión
- Separación semántica: rationale identifica el modo activo
"""

from decimal import Decimal
from dataclasses import dataclass

import pytest

from src.risk.position_sizer import (
    FailClosedError,
    PositionSizer,
    SizingDecision,
    SymbolConstraints,
)
from src.accounting.portfolio_snapshot import PortfolioSnapshot

# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

BTC = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("10000"),
    min_notional=Decimal("1"),
)
MAX_NOTIONAL = Decimal("500_000")

SIZER = PositionSizer()

EQUITY = Decimal("100_000")   # $100k portfolio
ENTRY  = Decimal("50_000")    # BTC @ $50k


def alloc(target_notional_pct: str, **kw) -> SizingDecision:
    return SIZER.compute(
        symbol="BTC-USD",
        equity=EQUITY,
        entry_price=ENTRY,
        constraints=BTC,
        max_notional=MAX_NOTIONAL,
        target_notional_pct=Decimal(target_notional_pct),
        **kw,
    )


def risk(risk_per_trade_pct: str, stop: str | None = None, **kw) -> SizingDecision:
    return SIZER.compute(
        symbol="BTC-USD",
        equity=EQUITY,
        entry_price=ENTRY,
        constraints=BTC,
        max_notional=MAX_NOTIONAL,
        risk_per_trade_pct=Decimal(risk_per_trade_pct),
        stop_price=Decimal(stop) if stop else None,
        **kw,
    )


# ──────────────────────────────────────────────
# Modo ALLOCATION
# ──────────────────────────────────────────────


class TestAllocationMode:

    def test_allocation_10pct(self):
        """10% de $100k a $50k BTC = 0.2 BTC."""
        d = alloc("0.10")
        assert d.target_qty == Decimal("0.2")
        assert d.target_notional == Decimal("0.2") * ENTRY

    def test_allocation_50pct(self):
        """50% de $100k a $50k = 1 BTC."""
        d = alloc("0.50")
        assert d.target_qty == Decimal("1.0")

    def test_allocation_scales_with_equity(self):
        """Mayor equity → mayor qty para mismo pct."""
        d1 = SIZER.compute(
            symbol="BTC-USD", equity=Decimal("50_000"), entry_price=ENTRY,
            constraints=BTC, max_notional=MAX_NOTIONAL, target_notional_pct=Decimal("0.10"),
        )
        d2 = SIZER.compute(
            symbol="BTC-USD", equity=Decimal("100_000"), entry_price=ENTRY,
            constraints=BTC, max_notional=MAX_NOTIONAL, target_notional_pct=Decimal("0.10"),
        )
        assert d2.target_qty == 2 * d1.target_qty

    def test_allocation_rationale_contains_ALLOCATION(self):
        d = alloc("0.10")
        assert "ALLOCATION" in d.rationale

    def test_allocation_no_stop_needed(self):
        """ALLOCATION no necesita stop_price — funciona sin él."""
        d = alloc("0.05")
        assert d.target_qty > Decimal("0")

    def test_allocation_risk_budget_correct(self):
        """risk_budget_used = target_notional / equity."""
        d = alloc("0.10")
        expected = d.target_notional / EQUITY
        assert d.risk_budget_used == expected


# ──────────────────────────────────────────────
# Modo RISK_BASED
# ──────────────────────────────────────────────


class TestRiskBasedMode:

    def test_risk_based_with_stop(self):
        """qty = (equity × risk_pct) / stop_distance."""
        # risk_amount = 100_000 × 0.01 = 1000
        # stop_distance = |50000 - 49000| = 1000
        # qty = 1000 / 1000 = 1.0 BTC
        d = risk("0.01", stop="49000")
        assert d.target_qty == Decimal("1.0")

    def test_risk_based_no_stop_fallback(self):
        """Sin stop: qty = (equity × risk_pct) / entry_price."""
        # risk_amount = 100_000 × 0.01 = 1000
        # qty = 1000 / 50000 = 0.02 BTC
        d = risk("0.01")
        assert d.target_qty == Decimal("0.02")

    def test_risk_based_tight_stop_larger_qty(self):
        """Stop más cercano → menor distancia → mayor qty."""
        d_wide = risk("0.01", stop="45000")   # distance = 5000
        d_tight = risk("0.01", stop="49500")  # distance = 500
        assert d_tight.target_qty > d_wide.target_qty

    def test_risk_based_rationale_contains_RISK_BASED(self):
        d = risk("0.01", stop="49000")
        assert "RISK_BASED" in d.rationale

    def test_risk_pct_zero_returns_qty_zero(self):
        """Sin ningún modo activo → qty=0."""
        d = SIZER.compute(
            symbol="BTC-USD", equity=EQUITY, entry_price=ENTRY,
            constraints=BTC, max_notional=MAX_NOTIONAL,
            risk_per_trade_pct=Decimal("0"),
        )
        assert d.target_qty == Decimal("0")


# ──────────────────────────────────────────────
# Modo BOTH — conservador
# ──────────────────────────────────────────────


class TestBothModes:

    def test_both_takes_minimum(self):
        """Con ambos modos, target_qty = min(qty_alloc, qty_risk)."""
        # ALLOCATION: 2% de $100k a $50k = $2000 → 0.04 BTC
        # RISK_BASED: 1% de $100k / ($50k-$49k) = $1000/1000 = 1.0 BTC
        # min = 0.04 BTC (ALLOCATION es más conservador)
        d = SIZER.compute(
            symbol="BTC-USD",
            equity=EQUITY,
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.02"),   # → 0.04 BTC
            risk_per_trade_pct=Decimal("0.01"),     # → 1.0 BTC (con stop)
            stop_price=Decimal("49000"),
        )
        assert d.target_qty == Decimal("0.04")

    def test_both_risk_more_restrictive(self):
        """Si RISK es más restrictivo, domina."""
        # ALLOCATION: 50% de $100k a $50k = $50k → 1.0 BTC
        # RISK_BASED: 0.1% de $100k / 1000 distancia = $100/1000 = 0.1 BTC
        # min = 0.1 BTC
        d = SIZER.compute(
            symbol="BTC-USD",
            equity=EQUITY,
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.50"),  # → 1.0 BTC
            risk_per_trade_pct=Decimal("0.001"),  # → 0.1 BTC (stop)
            stop_price=Decimal("49000"),
        )
        assert d.target_qty == Decimal("0.1")

    def test_both_equal_modes_same_result(self):
        """Si ambos modos producen la misma qty, result es igual a cualquiera."""
        # Construimos el caso: ALLOC = RISK → ambos dan 0.02 BTC
        # ALLOC: equity*0.01 / entry = 100_000*0.01/50_000 = 0.02
        # RISK no-stop: equity*0.01 / entry = 100_000*0.01/50_000 = 0.02
        d_both = SIZER.compute(
            symbol="BTC-USD",
            equity=EQUITY,
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.01"),
            risk_per_trade_pct=Decimal("0.01"),
        )
        d_alloc_only = alloc("0.01")
        assert d_both.target_qty == d_alloc_only.target_qty


# ──────────────────────────────────────────────
# compute_from_snapshot
# ──────────────────────────────────────────────


class TestComputeFromSnapshot:

    def _snap(self, equity: str) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            symbol="BTC-USD",
            ts_ms=1_000_000,
            equity=Decimal(equity),
        )

    def test_from_snapshot_allocation(self):
        """compute_from_snapshot usa snapshot.equity para ALLOCATION."""
        snap = self._snap("100000")
        d = SIZER.compute_from_snapshot(
            snapshot=snap,
            symbol="BTC-USD",
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.10"),
        )
        # 10% de $100k a $50k = 0.2 BTC
        assert d.target_qty == Decimal("0.2")

    def test_from_snapshot_risk_based(self):
        """compute_from_snapshot usa snapshot.equity para RISK_BASED."""
        snap = self._snap("100000")
        d = SIZER.compute_from_snapshot(
            snapshot=snap,
            symbol="BTC-USD",
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            risk_per_trade_pct=Decimal("0.01"),
            stop_price=Decimal("49000"),
        )
        assert d.target_qty == Decimal("1.0")

    def test_from_snapshot_equity_none_raises(self):
        """snapshot.equity=None → FailClosedError."""
        @dataclass
        class BadSnap:
            equity = None

        with pytest.raises(FailClosedError):
            SIZER.compute_from_snapshot(
                snapshot=BadSnap(),
                symbol="BTC-USD",
                entry_price=ENTRY,
                constraints=BTC,
                max_notional=MAX_NOTIONAL,
                target_notional_pct=Decimal("0.10"),
            )

    def test_from_snapshot_no_equity_attr_raises(self):
        """Objeto sin atributo equity → FailClosedError."""
        with pytest.raises(FailClosedError):
            SIZER.compute_from_snapshot(
                snapshot=object(),
                symbol="BTC-USD",
                entry_price=ENTRY,
                constraints=BTC,
                max_notional=MAX_NOTIONAL,
                target_notional_pct=Decimal("0.10"),
            )

    def test_from_snapshot_with_ledger(self, tmp_path):
        """compute_from_snapshot con PortfolioSnapshot.from_ledger()."""
        from src.accounting.ledger import Fill, TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("1.0"), price=Decimal("50000"),
            cost=Decimal("50000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t1", order_id="o1",
        ))
        # realized=0, posición 1 BTC a $50k
        # mark=55000 → equity = 0 + 1.0*55000 = 55000
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("55000"))
        assert snap.equity == Decimal("55000")

        d = SIZER.compute_from_snapshot(
            snapshot=snap,
            symbol="BTC-USD",
            entry_price=Decimal("55000"),
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.10"),
        )
        # 10% de $55k a $55k = 0.1 BTC
        assert d.target_qty == Decimal("0.1")

    def test_from_snapshot_deterministic(self):
        """Mismos inputs → misma decisión."""
        snap = self._snap("80000")
        kw = dict(
            snapshot=snap,
            symbol="BTC-USD",
            entry_price=Decimal("48000"),
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.05"),
        )
        d1 = SIZER.compute_from_snapshot(**kw)
        d2 = SIZER.compute_from_snapshot(**kw)
        assert d1.target_qty == d2.target_qty
        assert d1.target_notional == d2.target_notional


# ──────────────────────────────────────────────
# Separación semántica en rationale
# ──────────────────────────────────────────────


class TestRationale:

    def test_allocation_only_rationale(self):
        d = alloc("0.05")
        assert "ALLOCATION" in d.rationale
        assert "RISK_BASED" not in d.rationale

    def test_risk_only_rationale(self):
        d = risk("0.01", stop="49000")
        assert "RISK_BASED" in d.rationale
        assert "ALLOCATION" not in d.rationale

    def test_both_modes_rationale_has_both(self):
        d = SIZER.compute(
            symbol="BTC-USD",
            equity=EQUITY,
            entry_price=ENTRY,
            constraints=BTC,
            max_notional=MAX_NOTIONAL,
            target_notional_pct=Decimal("0.02"),
            risk_per_trade_pct=Decimal("0.01"),
        )
        assert "ALLOCATION" in d.rationale
        assert "RISK_BASED" in d.rationale
