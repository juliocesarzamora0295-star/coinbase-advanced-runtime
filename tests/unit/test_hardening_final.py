"""
Tests for hardening final: kill switch convergence, external reconcile,
bootstrap from exchange.
"""

import os
import tempfile
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService


def _make_intent(client_order_id: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id="test", strategy_id="test",
        symbol="BTC-USD", side="BUY",
        final_qty=Decimal("0.1"), order_type="LIMIT",
        price=Decimal("50000"), reduce_only=False,
        post_only=False, viable=True, planner_version="test",
    )


# ── A) Kill switch convergence ──


class TestKillSwitchConvergence:

    def test_cancel_converges_when_no_open_orders(self, tmp_path):
        """No open orders → convergence immediate."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        # No orders → get_pending_or_open returns []
        assert len(idem.get_pending_or_open()) == 0

    def test_convergence_detects_remaining_orders(self, tmp_path):
        """Open orders after cancel attempts → convergence fails."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        intent = _make_intent("ks-conv-1")
        idem.save_intent(intent, OrderState.OPEN_RESTING)
        idem.update_state(
            client_order_id="ks-conv-1",
            state=OrderState.OPEN_RESTING,
            exchange_order_id="ex-1",
        )
        # Still open
        remaining = idem.get_pending_or_open()
        assert len(remaining) == 1


# ── B) External reconcile ──


class TestExternalReconcile:

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        idem_path = os.path.join(self.temp_dir, "idem.db")
        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.idem = IdempotencyStore(db_path=idem_path)
        self.oms = OMSReconcileService(
            idempotency=self.idem,
            ledger=TradeLedger("BTC-USD", db_path=ledger_path),
        )
        self.oms.handle_user_event("snapshot", [])

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_reconcile_no_drift(self):
        """No OMS orders, no exchange orders → clean."""
        clean, drifts = self.oms.reconcile_against_exchange([], [])
        assert clean
        assert drifts == []

    def test_oms_open_not_on_exchange_drifts(self):
        """OMS has open order but exchange doesn't → drift."""
        intent = _make_intent("ext-rec-1")
        self.idem.save_intent(intent, OrderState.OPEN_RESTING)

        clean, drifts = self.oms.reconcile_against_exchange([], [])
        assert not clean
        assert any("OMS_OPEN_NOT_ON_EXCHANGE" in d for d in drifts)

    def test_exchange_open_not_in_oms_drifts(self):
        """Exchange has order OMS doesn't know → drift."""
        exchange_orders = [
            {"client_order_id": "unknown-cid", "order_id": "ex-99", "status": "OPEN"}
        ]
        clean, drifts = self.oms.reconcile_against_exchange(exchange_orders, [])
        assert not clean
        assert any("EXCHANGE_OPEN_NOT_IN_OMS" in d for d in drifts)

    def test_unseen_fill_drifts(self):
        """Exchange has fill OMS hasn't seen → drift."""
        fills = [{"trade_id": "unseen-fill-1"}]
        clean, drifts = self.oms.reconcile_against_exchange([], fills)
        assert not clean
        assert any("UNSEEN_FILL" in d for d in drifts)

    def test_matching_state_clean(self):
        """OMS and exchange agree → clean."""
        intent = _make_intent("matched-1")
        self.idem.save_intent(intent, OrderState.OPEN_RESTING)

        exchange = [{"client_order_id": "matched-1", "order_id": "ex-m", "status": "OPEN"}]
        clean, drifts = self.oms.reconcile_against_exchange(exchange, [])
        assert clean

    def test_drift_marks_oms_degraded(self):
        """Drift from external reconcile → OMS degraded."""
        assert self.oms.is_ready()
        self.oms.reconcile_against_exchange(
            [{"client_order_id": "orphan", "order_id": "ex-o", "status": "OPEN"}], []
        )
        assert self.oms.is_degraded()

    def test_auto_clear_requires_external_clean(self):
        """Auto-clear blocked unless external reconcile was also clean."""
        self.oms.report_divergence("test issue")
        assert self.oms.is_degraded()

        # Internal clean reconciles alone shouldn't clear (external not clean)
        for _ in range(5):
            self.oms.record_clean_reconcile()
        assert self.oms.is_degraded()  # still degraded

        # External clean reconcile
        self.oms.reconcile_against_exchange([], [])
        # Now internal should clear
        for _ in range(3):
            self.oms.record_clean_reconcile()
        assert not self.oms.is_degraded()


# ── C) Bootstrap from exchange ──


class TestBootstrapFromExchange:

    def test_bootstrap_sets_cash_and_position(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.bootstrap_from_exchange(
            quote_balance=Decimal("5000"),
            base_balance=Decimal("0.1"),
            current_price=Decimal("50000"),
        )
        assert ledger.cash == Decimal("5000")
        assert ledger.position_qty == Decimal("0.1")
        # equity = 5000 + 0.1 * 50000 = 10000
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")

    def test_bootstrap_sets_equity_day_start(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.bootstrap_from_exchange(
            quote_balance=Decimal("10000"),
            base_balance=Decimal("0"),
            current_price=Decimal("50000"),
        )
        assert ledger.equity_day_start == Decimal("10000")

    def test_bootstrap_updates_peak(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.bootstrap_from_exchange(
            quote_balance=Decimal("8000"),
            base_balance=Decimal("0.05"),
            current_price=Decimal("50000"),
        )
        # equity = 8000 + 2500 = 10500
        assert ledger.equity_peak >= Decimal("10500")

    def test_bootstrap_zero_base(self, tmp_path):
        """No position → cash only."""
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"))
        ledger.bootstrap_from_exchange(
            quote_balance=Decimal("15000"),
            base_balance=Decimal("0"),
            current_price=Decimal("50000"),
        )
        assert ledger.cash == Decimal("15000")
        assert ledger.position_qty == Decimal("0")
        assert ledger.get_equity(Decimal("50000")) == Decimal("15000")

    def test_config_fallback_preserved(self, tmp_path):
        """When no exchange, initial_cash from config is used."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        # No bootstrap_from_exchange called
        assert ledger.cash == Decimal("10000")
        assert ledger.initial_cash == Decimal("10000")
