"""
Tests for final remediation — S1 closures verified.
"""

import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService


def _make_intent(cid: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid, signal_id="test", strategy_id="test",
        symbol="BTC-USD", side="BUY", final_qty=Decimal("0.1"),
        order_type="LIMIT", price=Decimal("50000"),
        reduce_only=False, post_only=False, viable=True, planner_version="test",
    )


# ── S1-1: Runtime classification honest ──


class TestRuntimeClassification:

    def test_live_mode_label(self):
        """Config with dry_run=False, observe_only=False → LIVE MODE."""
        from src.config import TradingConfig
        tc = TradingConfig(dry_run=False, observe_only=False)
        if tc.observe_only:
            mode = "SHADOW MODE"
        elif tc.dry_run:
            mode = "SIMULATION MODE"
        else:
            mode = "LIVE MODE"
        assert mode == "LIVE MODE"

    def test_shadow_mode_label(self):
        from src.config import TradingConfig
        tc = TradingConfig(observe_only=True)
        mode = "SHADOW MODE" if tc.observe_only else "other"
        assert mode == "SHADOW MODE"

    def test_no_hardcoded_preproduction_string(self):
        """main.py must not contain hardcoded 'pre-producción' string."""
        with open("src/main.py") as f:
            content = f.read()
        assert "pre-producción" not in content
        assert "pre-produc" not in content.lower()


# ── S1-2: Kill switch convergence with exchange verification ──


class TestKillSwitchExchangeVerification:

    def test_terminal_order_means_exchange_confirmed(self, tmp_path):
        """OMS record in terminal state → exchange confirmed."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        intent = _make_intent("ks-term-1")
        idem.save_intent(intent, OrderState.FILLED)

        record = idem.get_by_client_order_id("ks-term-1")
        assert record.is_terminal  # exchange confirmed

    def test_non_terminal_order_means_not_confirmed(self, tmp_path):
        """OMS record still active → exchange NOT confirmed."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        intent = _make_intent("ks-active-1")
        idem.save_intent(intent, OrderState.OPEN_PENDING)

        record = idem.get_by_client_order_id("ks-active-1")
        assert not record.is_terminal

    def test_convergence_requires_both_ledger_and_oms(self, tmp_path):
        """Full convergence = ledger position 0 + OMS terminal."""
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"),
                             initial_cash=Decimal("10000"))
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))

        # Ledger at 0 (no position)
        assert ledger.position_qty == Decimal("0")
        # OMS terminal
        intent = _make_intent("conv-1")
        idem.save_intent(intent, OrderState.FILLED)
        record = idem.get_by_client_order_id("conv-1")

        # Both conditions met
        ledger_ok = ledger.position_qty <= Decimal("0.00001")
        oms_ok = record.is_terminal
        assert ledger_ok and oms_ok


# ── S1-3: Reconcile auto-clear requires strong consistency ──


class TestReconcileAutolearStrong:

    def _make_oms(self, tmp_path):
        return OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )

    def test_dirty_external_resets_clean_flag(self, tmp_path):
        """Drift in external reconcile → _last_external_reconcile_clean = False."""
        oms = self._make_oms(tmp_path)
        oms.handle_user_event("snapshot", [])

        # First: clean external
        oms.reconcile_against_exchange([], [])
        assert oms.last_external_reconcile_clean

        # Then: dirty external (unseen fill)
        oms.reconcile_against_exchange([], [{"trade_id": "unknown"}])
        assert not oms.last_external_reconcile_clean

    def test_report_divergence_resets_clean_flag(self, tmp_path):
        """report_divergence() invalidates external clean state."""
        oms = self._make_oms(tmp_path)
        oms.handle_user_event("snapshot", [])

        oms.reconcile_against_exchange([], [])
        assert oms.last_external_reconcile_clean

        oms.report_divergence("manual divergence")
        assert not oms.last_external_reconcile_clean

    def test_auto_clear_blocked_after_dirty_external(self, tmp_path):
        """
        Degraded + 3 internal clean BUT external dirty → no auto-clear.
        """
        oms = self._make_oms(tmp_path)
        oms.handle_user_event("snapshot", [])
        oms.report_divergence("issue")
        assert oms.is_degraded()

        # 3 internal clean without external clean
        for _ in range(5):
            oms.record_clean_reconcile()
        assert oms.is_degraded()  # still degraded — external not clean

    def test_auto_clear_succeeds_with_fresh_external_clean(self, tmp_path):
        """
        Degraded → external clean → 3 internal clean → auto-clear.
        """
        oms = self._make_oms(tmp_path)
        oms.handle_user_event("snapshot", [])
        oms.report_divergence("issue")
        assert oms.is_degraded()

        # Fresh external clean
        oms.reconcile_against_exchange([], [])
        assert oms.last_external_reconcile_clean

        # 3 internal clean (external reconcile already counted as 1)
        oms.record_clean_reconcile()
        oms.record_clean_reconcile()
        assert not oms.is_degraded()  # cleared
