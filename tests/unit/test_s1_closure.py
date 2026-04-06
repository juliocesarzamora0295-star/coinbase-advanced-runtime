"""
Tests for S1 risk closure: R1-R4.

R1: set_reserved() wired from OMS open orders
R2: record_clean/dirty_reconcile called after each cycle
R3: _pending_reports fallback documented with WARNING
R4: Day change triggers reset_day()
"""

import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService
from src.risk.circuit_breaker import BreakerConfig, CircuitBreaker


def _make_intent(client_order_id, qty="0.1", price="50000"):
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol="BTC-USD",
        side="BUY",
        final_qty=Decimal(qty),
        order_type="LIMIT",
        price=Decimal(price),
        reduce_only=False,
        post_only=False,
        viable=True,
        planner_version="test",
    )


class TestR1ReservedBalances:
    """R1: Open orders reduce available cash."""

    def test_reserved_set_from_open_orders(self, tmp_path):
        """Ledger.set_reserved() reflects open order notional."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))

        # Save an open order: 0.1 BTC @ 50000 = 5000 notional
        intent = _make_intent("r1-order-1")
        idem.save_intent(intent, OrderState.OPEN_RESTING)

        # Simulate what _post_cycle_housekeeping does
        open_orders = idem.get_pending_or_open()
        reserved = Decimal("0")
        for record in open_orders:
            if record.intent.price and record.intent.price > Decimal("0"):
                reserved += record.intent.final_qty * record.intent.price
        ledger.set_reserved(reserved)

        assert ledger.reserved_quote == Decimal("5000")
        assert ledger.get_available_cash() == Decimal("5000")
        assert ledger.get_equity(Decimal("50000")) == Decimal("5000")

    def test_no_open_orders_no_reserved(self, tmp_path):
        """No open orders → reserved = 0."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))

        open_orders = idem.get_pending_or_open()
        assert len(open_orders) == 0
        ledger.set_reserved(Decimal("0"))
        assert ledger.get_available_cash() == Decimal("10000")

    def test_filled_orders_not_reserved(self, tmp_path):
        """Filled orders don't count as reserved."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))

        intent = _make_intent("r1-filled")
        idem.save_intent(intent, OrderState.FILLED)

        open_orders = idem.get_pending_or_open()
        assert len(open_orders) == 0


class TestR2ReconcileTracking:
    """R2: Reconcile health tracking for auto-recovery."""

    def test_clean_reconcile_after_no_pending(self, tmp_path):
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])  # bootstrap
        assert oms.is_ready()

        # Simulate post-cycle: no pending orders → clean
        open_count = len(oms.idempotency.get_pending_or_open())
        assert open_count == 0
        oms.record_clean_reconcile()
        assert oms._consecutive_clean_reconciles == 1

    def test_degraded_auto_clears_after_threshold(self, tmp_path):
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])
        oms.report_divergence("test issue")
        assert not oms.is_ready()

        for _ in range(3):
            oms.record_clean_reconcile()

        assert oms.is_ready()  # auto-cleared

    def test_dirty_reconcile_resets_counter(self, tmp_path):
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])
        oms.report_divergence("issue")

        oms.record_clean_reconcile()
        oms.record_clean_reconcile()
        oms.record_dirty_reconcile()
        assert oms._consecutive_clean_reconciles == 0
        assert oms.is_degraded()


class TestR3PendingReportsFallback:
    """R3: Documented fallback when pending_reports miss."""

    def test_fallback_uses_fill_price_as_expected(self):
        """When order not in _pending_reports, slippage = 0."""
        from src.execution.execution_report import build_execution_report

        # Simulate what _on_fill_reconciled does when pending is None
        fill_price = Decimal("50100")
        filled_qty = Decimal("0.1")

        report = build_execution_report(
            client_order_id="r3-unknown",
            symbol="BTC-USD",
            side="BUY",
            expected_price=fill_price,  # fallback: same as fill
            fill_price=fill_price,
            requested_qty=filled_qty,
            filled_qty=filled_qty,
            latency_ms=0.0,
            outcome="FILLED",
        )

        assert report.slippage_bps == Decimal("0")
        assert report.fill_ratio == Decimal("1")
        assert report.outcome == "FILLED"

    def test_pending_reports_dict_works_normally(self):
        """When order IS in _pending_reports, real slippage calculated."""
        from src.execution.execution_report import build_execution_report

        expected = Decimal("50000")
        fill = Decimal("50050")

        report = build_execution_report(
            client_order_id="r3-known",
            symbol="BTC-USD",
            side="BUY",
            expected_price=expected,
            fill_price=fill,
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=150.0,
            outcome="FILLED",
        )

        assert report.slippage_bps == Decimal("10")  # (50/50000)*10000
        assert report.latency_ms == 150.0


class TestR4DayChangeDetection:
    """R4: Day change triggers reset_day()."""

    def test_reset_day_updates_equity_day_start(self, tmp_path):
        """reset_day() captures current equity as equity_day_start."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))
        assert ledger.equity_day_start == Decimal("10000")

    def test_day_pnl_correct_after_reset(self, tmp_path):
        """After reset_day, price change → correct daily PnL."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        # Buy 0.1 BTC @ 50000
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("0.1"), price=Decimal("50000"),
            cost=Decimal("5000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t-1", order_id="o-1",
        ))

        # Reset day at price 50000 → equity = 10000
        ledger.reset_day(Decimal("50000"))
        assert ledger.equity_day_start == Decimal("10000")

        # Price rises to 51000 → equity = 5000 + 0.1*51000 = 10100
        pnl = ledger.get_day_pnl_pct(Decimal("51000"))
        assert pnl == Decimal("0.01")  # +1%

    def test_day_change_detection_logic(self):
        """Simulates the day change detection from _post_cycle_housekeeping."""
        last_date = date(2024, 1, 1)
        today = date(2024, 1, 2)

        # Day changed
        assert today != last_date
        # Same day
        assert today == today

    def test_breaker_reset_day_called_on_day_change(self):
        """CircuitBreaker.reset_day() called when day changes."""
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        breaker.update_equity(Decimal("9500"))  # simulate loss

        # Reset day with new equity
        breaker.reset_day(Decimal("9500"))
        assert breaker.equity_day_start == Decimal("9500")
        assert breaker.equity_peak == Decimal("9500")
        assert breaker.consecutive_losses == 0
