"""
Tests para open-order exposure.

Valida:
- OpenOrderEntry.notional = qty × price
- OpenOrderExposureReport.from_entries: desglose BUY/SELL, total correcto
- OpenOrderExposureReport.empty: cero en todo
- Validaciones de entrada (qty<0, price<0)
- PortfolioSnapshot.open_order_exposure fluye a gross_exposure
- gross_exposure = inventory_value + open_order_exposure  [determinista]
- Integración: exposure calculado desde IdempotencyStore records
"""

from decimal import Decimal

import pytest

from src.risk.exposure import OpenOrderEntry, OpenOrderExposureReport
from src.accounting.portfolio_snapshot import PortfolioSnapshot


# ──────────────────────────────────────────────
# Tests de OpenOrderEntry
# ──────────────────────────────────────────────


class TestOpenOrderEntry:

    def test_notional_buy(self):
        e = OpenOrderEntry("c1", "BUY", Decimal("0.5"), Decimal("50000"), "OPEN_RESTING")
        assert e.notional == Decimal("25000")

    def test_notional_sell(self):
        e = OpenOrderEntry("c2", "SELL", Decimal("1.0"), Decimal("48000"), "OPEN_PENDING")
        assert e.notional == Decimal("48000")

    def test_notional_zero_qty(self):
        e = OpenOrderEntry("c3", "BUY", Decimal("0"), Decimal("50000"), "OPEN_RESTING")
        assert e.notional == Decimal("0")

    def test_negative_qty_raises(self):
        with pytest.raises(ValueError, match="qty"):
            OpenOrderEntry("c4", "BUY", Decimal("-1"), Decimal("50000"), "OPEN_RESTING")

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            OpenOrderEntry("c5", "BUY", Decimal("1"), Decimal("-1"), "OPEN_RESTING")

    def test_immutable(self):
        e = OpenOrderEntry("c6", "BUY", Decimal("1"), Decimal("50000"), "OPEN_RESTING")
        with pytest.raises(Exception):
            e.qty = Decimal("999")  # type: ignore[misc]


# ──────────────────────────────────────────────
# Tests de OpenOrderExposureReport
# ──────────────────────────────────────────────


class TestOpenOrderExposureReport:

    def test_empty_report(self):
        r = OpenOrderExposureReport.empty()
        assert r.total == Decimal("0")
        assert r.buy_exposure == Decimal("0")
        assert r.sell_exposure == Decimal("0")
        assert r.entries == ()

    def test_from_entries_empty_list(self):
        r = OpenOrderExposureReport.from_entries([])
        assert r.total == Decimal("0")
        assert r.buy_exposure == Decimal("0")
        assert r.sell_exposure == Decimal("0")

    def test_single_buy_order(self):
        e = OpenOrderEntry("c1", "BUY", Decimal("2.0"), Decimal("50000"), "OPEN_RESTING")
        r = OpenOrderExposureReport.from_entries([e])
        assert r.buy_exposure == Decimal("100000")
        assert r.sell_exposure == Decimal("0")
        assert r.total == Decimal("100000")

    def test_single_sell_order(self):
        e = OpenOrderEntry("c1", "SELL", Decimal("1.0"), Decimal("52000"), "OPEN_PENDING")
        r = OpenOrderExposureReport.from_entries([e])
        assert r.buy_exposure == Decimal("0")
        assert r.sell_exposure == Decimal("52000")
        assert r.total == Decimal("52000")

    def test_mixed_buy_sell_orders(self):
        entries = [
            OpenOrderEntry("c1", "BUY",  Decimal("1.0"), Decimal("50000"), "OPEN_RESTING"),
            OpenOrderEntry("c2", "BUY",  Decimal("0.5"), Decimal("49000"), "OPEN_PENDING"),
            OpenOrderEntry("c3", "SELL", Decimal("0.8"), Decimal("55000"), "OPEN_RESTING"),
        ]
        r = OpenOrderExposureReport.from_entries(entries)
        assert r.buy_exposure == Decimal("50000") + Decimal("24500")
        assert r.sell_exposure == Decimal("44000")
        assert r.total == r.buy_exposure + r.sell_exposure

    def test_cancel_queued_included(self):
        """CANCEL_QUEUED sigue contando como exposure hasta confirmación."""
        e = OpenOrderEntry("c1", "BUY", Decimal("1.0"), Decimal("50000"), "CANCEL_QUEUED")
        r = OpenOrderExposureReport.from_entries([e])
        assert r.total == Decimal("50000")

    def test_multiple_orders_total_is_sum(self):
        entries = [
            OpenOrderEntry(f"c{i}", "BUY", Decimal("1"), Decimal(str(i * 1000)), "OPEN_RESTING")
            for i in range(1, 6)
        ]
        r = OpenOrderExposureReport.from_entries(entries)
        expected = sum(i * 1000 for i in range(1, 6))
        assert r.total == Decimal(str(expected))

    def test_report_is_immutable(self):
        r = OpenOrderExposureReport.empty()
        with pytest.raises(Exception):
            r.total = Decimal("999")  # type: ignore[misc]

    def test_entries_are_tuple(self):
        e = OpenOrderEntry("c1", "BUY", Decimal("1"), Decimal("50000"), "OPEN_RESTING")
        r = OpenOrderExposureReport.from_entries([e])
        assert isinstance(r.entries, tuple)

    def test_side_case_insensitive(self):
        """side en minúsculas debe funcionar igual."""
        entries = [
            OpenOrderEntry("c1", "buy",  Decimal("1.0"), Decimal("50000"), "OPEN_RESTING"),
            OpenOrderEntry("c2", "sell", Decimal("1.0"), Decimal("50000"), "OPEN_RESTING"),
        ]
        r = OpenOrderExposureReport.from_entries(entries)
        assert r.buy_exposure == Decimal("50000")
        assert r.sell_exposure == Decimal("50000")


# ──────────────────────────────────────────────
# Tests de PortfolioSnapshot.gross_exposure
# ──────────────────────────────────────────────


class TestPortfolioSnapshotGrossExposure:

    def _snap(self, inventory_value, open_order_exposure):
        return PortfolioSnapshot(
            symbol="BTC-USD",
            ts_ms=1_000_000,
            mark_price=Decimal("50000"),
            cash_balance=Decimal("0"),
            reserved_balance=Decimal("0"),
            inventory_value=Decimal(str(inventory_value)),
            position_qty=Decimal("0"),
            avg_entry=Decimal("0"),
            fees_accrued=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            open_order_exposure=Decimal(str(open_order_exposure)),
        )

    def test_gross_exposure_formula(self):
        """gross_exposure = inventory_value + open_order_exposure."""
        snap = self._snap(50000, 25000)
        assert snap.gross_exposure == Decimal("75000")

    def test_gross_exposure_no_open_orders(self):
        """Sin órdenes abiertas: gross_exposure == inventory_value."""
        snap = self._snap(80000, 0)
        assert snap.gross_exposure == Decimal("80000")

    def test_gross_exposure_no_inventory(self):
        """Sin posición: gross_exposure == open_order_exposure."""
        snap = self._snap(0, 30000)
        assert snap.gross_exposure == Decimal("30000")

    def test_gross_exposure_both_zero(self):
        snap = self._snap(0, 0)
        assert snap.gross_exposure == Decimal("0")

    def test_gross_exposure_deterministic(self):
        """Dos snapshots idénticos producen el mismo gross_exposure."""
        s1 = self._snap(50000, 25000)
        s2 = self._snap(50000, 25000)
        assert s1.gross_exposure == s2.gross_exposure


# ──────────────────────────────────────────────
# Tests de PortfolioSnapshot.from_ledger con exposure
# ──────────────────────────────────────────────


class TestPortfolioSnapshotFromLedgerExposure:

    def test_from_ledger_default_exposure_zero(self, tmp_path):
        """Sin open_order_exposure: gross_exposure == inventory_value."""
        from src.accounting.ledger import Fill, TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("1.0"), price=Decimal("50000"),
            cost=Decimal("50000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t1", order_id="o1",
        ))

        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("50000"))
        assert snap.open_order_exposure == Decimal("0")
        assert snap.gross_exposure == snap.inventory_value

    def test_from_ledger_with_exposure(self, tmp_path):
        """open_order_exposure se propaga a gross_exposure correctamente."""
        from src.accounting.ledger import Fill, TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("1.0"), price=Decimal("50000"),
            cost=Decimal("50000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t1", order_id="o1",
        ))
        # Hay además 2 órdenes BUY abiertas con notional 10000 c/u
        snap = PortfolioSnapshot.from_ledger(
            ledger,
            mark_price=Decimal("50000"),
            open_order_exposure=Decimal("20000"),
        )
        assert snap.open_order_exposure == Decimal("20000")
        # inventory_value = cost_basis = 50000
        assert snap.gross_exposure == Decimal("50000") + Decimal("20000")

    def test_negative_open_order_exposure_raises(self, tmp_path):
        from src.accounting.ledger import TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        with pytest.raises(ValueError, match="open_order_exposure"):
            PortfolioSnapshot.from_ledger(
                ledger,
                mark_price=Decimal("50000"),
                open_order_exposure=Decimal("-1"),
            )


# ──────────────────────────────────────────────
# Tests de integración completa
# ──────────────────────────────────────────────


class TestFullExposureIntegration:

    def test_exposure_from_entries_to_snapshot(self, tmp_path):
        """
        Flujo completo: IdempotencyStore records → OpenOrderExposureReport →
        PortfolioSnapshot.gross_exposure determinista.
        """
        from src.accounting.ledger import Fill, TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("2.0"), price=Decimal("40000"),
            cost=Decimal("80000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t1", order_id="o1",
        ))

        # Simular 3 órdenes BUY abiertas (como vendrían de get_pending_or_open)
        entries = [
            OpenOrderEntry("c1", "BUY", Decimal("0.5"), Decimal("39000"), "OPEN_RESTING"),
            OpenOrderEntry("c2", "BUY", Decimal("0.5"), Decimal("38000"), "OPEN_PENDING"),
            OpenOrderEntry("c3", "BUY", Decimal("1.0"), Decimal("37000"), "CANCEL_QUEUED"),
        ]
        report = OpenOrderExposureReport.from_entries(entries)
        # 0.5*39000 + 0.5*38000 + 1.0*37000 = 19500 + 19000 + 37000 = 75500
        assert report.total == Decimal("75500")

        snap = PortfolioSnapshot.from_ledger(
            ledger,
            mark_price=Decimal("42000"),
            open_order_exposure=report.total,
        )
        # inventory_value = cost_basis = 80000 (2 BTC @ 40000)
        assert snap.gross_exposure == Decimal("80000") + Decimal("75500")

    def test_gross_exposure_increases_with_new_open_order(self, tmp_path):
        """Añadir una orden abierta incrementa el gross_exposure."""
        from src.accounting.ledger import TradeLedger

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))

        snap_before = PortfolioSnapshot.from_ledger(
            ledger, mark_price=Decimal("50000"), open_order_exposure=Decimal("0")
        )
        snap_after = PortfolioSnapshot.from_ledger(
            ledger, mark_price=Decimal("50000"), open_order_exposure=Decimal("10000")
        )
        assert snap_after.gross_exposure > snap_before.gross_exposure
        assert snap_after.gross_exposure - snap_before.gross_exposure == Decimal("10000")
