"""
Tests para PortfolioSnapshot.

Valida:
- Construcción correcta desde TradeLedger
- Cálculo exacto de equity = cash + inventory - fees + unrealized
- Invariante: equity == realized_pnl_neto + mark × qty
- Inmutabilidad (frozen dataclass)
- Casos borde: sin fills, posición cero, fees en base currency
"""

import os
import tempfile
from decimal import Decimal

import pytest

from src.accounting.ledger import Fill, TradeLedger
from src.accounting.portfolio_snapshot import PortfolioSnapshot


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path):
    return TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))


def make_fill(trade_id, side, amount, price, fee_cost, fee_currency="USD", ts_ms=1000):
    return Fill(
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        cost=Decimal(amount) * Decimal(price),
        fee_cost=Decimal(fee_cost),
        fee_currency=fee_currency,
        ts_ms=ts_ms,
        trade_id=trade_id,
        order_id=f"order-{trade_id}",
    )


# ──────────────────────────────────────────────
# Tests de construcción
# ──────────────────────────────────────────────


class TestPortfolioSnapshotConstruction:

    def test_from_ledger_empty(self, ledger):
        """Snapshot desde ledger vacío: todos los valores en cero."""
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"))

        assert snap.symbol == "BTC-USD"
        assert snap.position_qty == Decimal("0")
        assert snap.cash_balance == Decimal("0")
        assert snap.inventory_value == Decimal("0")
        assert snap.fees_accrued == Decimal("0")
        assert snap.realized_pnl == Decimal("0")
        assert snap.unrealized_pnl == Decimal("0")
        assert snap.equity == Decimal("0")

    def test_from_ledger_open_position(self, ledger):
        """Snapshot con posición abierta y fees en QUOTE."""
        ledger.add_fill(make_fill("t1", "buy", "1.0", "50000", "50", "USD"))
        # cost_basis = 50000 + 50 = 50050
        # fees_accrued = 50
        # realized = 0
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("55000"))

        assert snap.position_qty == Decimal("1.0")
        assert snap.inventory_value == Decimal("50050")   # cost_basis_quote
        assert snap.fees_accrued == Decimal("50")
        assert snap.realized_pnl == Decimal("0")

        # unrealized = (55000 - avg_entry) * qty
        assert snap.unrealized_pnl == ledger.get_unrealized_pnl(Decimal("55000"))

    def test_ts_ms_defaults_to_now(self, ledger):
        """ts_ms se asigna automáticamente si no se provee."""
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("1"))
        assert snap.ts_ms > 0

    def test_ts_ms_explicit(self, ledger):
        """ts_ms explícito se preserva en el snapshot."""
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("1"), ts_ms=9_999_999)
        assert snap.ts_ms == 9_999_999

    def test_reserved_balance_passed_through(self, ledger):
        """reserved_balance se incluye en el snapshot tal cual."""
        snap = PortfolioSnapshot.from_ledger(
            ledger, mark_price=Decimal("50000"), reserved_balance=Decimal("500")
        )
        assert snap.reserved_balance == Decimal("500")

    def test_negative_mark_price_raises(self, ledger):
        with pytest.raises(ValueError, match="mark_price"):
            PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("-1"))

    def test_negative_reserved_balance_raises(self, ledger):
        with pytest.raises(ValueError, match="reserved_balance"):
            PortfolioSnapshot.from_ledger(
                ledger, mark_price=Decimal("50000"), reserved_balance=Decimal("-1")
            )


# ──────────────────────────────────────────────
# Tests de cálculo de equity
# ──────────────────────────────────────────────


class TestEquityCalculation:

    def test_equity_formula_open_position(self, ledger):
        """
        equity = cash + inventory - fees + unrealized

        Con posición abierta y mark > avg_entry:
        equity debe ser > 0 (ganancia no realizada).
        """
        ledger.add_fill(make_fill("t1", "buy", "1.0", "50000", "50", "USD"))
        mark = Decimal("55000")
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=mark)

        expected = snap.cash_balance + snap.inventory_value - snap.fees_accrued + snap.unrealized_pnl
        assert snap.equity == expected

    def test_equity_invariant_vs_ledger(self, ledger):
        """
        Invariante: equity == realized_neto + mark × position_qty
        (idéntico a ledger.get_equity())
        """
        ledger.add_fill(make_fill("t1", "buy", "2.0", "48000", "100", "USD"))
        ledger.add_fill(make_fill("t2", "sell", "1.0", "52000", "60", "USD", ts_ms=2000))

        mark = Decimal("54000")
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=mark)

        assert snap.equity == ledger.get_equity(mark)

    def test_equity_zero_position(self, ledger):
        """Sin posición abierta: equity = realized_neto."""
        ledger.add_fill(make_fill("t1", "buy", "1.0", "50000", "50", "USD"))
        ledger.add_fill(make_fill("t2", "sell", "1.0", "52000", "60", "USD", ts_ms=2000))

        mark = Decimal("99999")  # no importa, posición = 0
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=mark)

        assert snap.position_qty == Decimal("0")
        assert snap.equity == snap.realized_pnl  # sin posición, equity = realized neto

    def test_equity_with_loss(self, ledger):
        """equity < 0 cuando hay pérdida mayor que capital."""
        ledger.add_fill(make_fill("t1", "buy", "1.0", "50000", "0", "USD"))
        ledger.add_fill(make_fill("t2", "sell", "1.0", "40000", "0", "USD", ts_ms=2000))

        mark = Decimal("35000")
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=mark)

        # realized = 40000 - 50000 = -10000
        assert snap.realized_pnl == Decimal("-10000")
        assert snap.equity == Decimal("-10000")  # sin posición abierta

    def test_equity_multiple_fills(self, ledger):
        """Secuencia buy/sell/buy verifica equity == ledger.get_equity()."""
        ledger.add_fill(make_fill("t1", "buy",  "2.0", "40000", "80",  "USD"))
        ledger.add_fill(make_fill("t2", "sell", "1.0", "45000", "45",  "USD", ts_ms=2000))
        ledger.add_fill(make_fill("t3", "buy",  "0.5", "42000", "21",  "USD", ts_ms=3000))

        mark = Decimal("46000")
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=mark)

        # Invariante principal
        assert snap.equity == ledger.get_equity(mark)
        # Fórmula explícita
        assert snap.equity == snap.cash_balance + snap.inventory_value - snap.fees_accrued + snap.unrealized_pnl


# ──────────────────────────────────────────────
# Tests de fees
# ──────────────────────────────────────────────


class TestFeesAccrued:

    def test_fees_quote_currency_accumulated(self, ledger):
        """Fees en USD acumulan en fees_accrued correctamente."""
        ledger.add_fill(make_fill("t1", "buy",  "1.0", "50000", "50",  "USD"))
        ledger.add_fill(make_fill("t2", "sell", "1.0", "52000", "60",  "USD", ts_ms=2000))

        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("52000"))
        assert snap.fees_accrued == Decimal("110")  # 50 + 60

    def test_fees_base_currency_converted_to_quote(self, ledger):
        """Fee en base currency se convierte a quote al precio del fill."""
        # fee_base = 0.001 BTC @ 50000 USD = 50 USD
        ledger.add_fill(make_fill("t1", "buy", "1.001", "50000", "0.001", "BTC"))

        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"))
        assert snap.fees_accrued == Decimal("0.001") * Decimal("50000")

    def test_no_fees(self, ledger):
        """Sin fees: fees_accrued == 0."""
        ledger.add_fill(make_fill("t1", "buy", "1.0", "50000", "0", "USD"))
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"))
        assert snap.fees_accrued == Decimal("0")


# ──────────────────────────────────────────────
# Tests de inmutabilidad
# ──────────────────────────────────────────────


class TestImmutability:

    def test_snapshot_is_frozen(self, ledger):
        """PortfolioSnapshot no permite mutación tras construcción."""
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"))
        with pytest.raises(Exception):  # FrozenInstanceError
            snap.position_qty = Decimal("999")  # type: ignore[misc]

    def test_two_snapshots_same_state_are_equal(self, ledger):
        """Dos snapshots del mismo estado con mismo ts_ms son iguales."""
        snap_a = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"), ts_ms=12345)
        snap_b = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"), ts_ms=12345)
        assert snap_a == snap_b

    def test_snapshots_different_mark_differ(self, ledger):
        """Dos snapshots con mark_price distinto no son iguales."""
        snap_a = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"), ts_ms=1)
        snap_b = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("51000"), ts_ms=1)
        assert snap_a != snap_b


# ──────────────────────────────────────────────
# Tests de inventory_mark_value
# ──────────────────────────────────────────────


class TestInventoryMarkValue:

    def test_inventory_mark_value(self, ledger):
        """inventory_mark_value = position_qty × mark_price."""
        ledger.add_fill(make_fill("t1", "buy", "2.0", "50000", "0", "USD"))
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("55000"))
        assert snap.inventory_mark_value == Decimal("2.0") * Decimal("55000")

    def test_inventory_mark_value_zero_position(self, ledger):
        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("55000"))
        assert snap.inventory_mark_value == Decimal("0")
