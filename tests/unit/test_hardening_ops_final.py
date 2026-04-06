"""
Tests for hardening operational final:
- Kill switch flatten convergence (ledger check)
- Reconcile external fills (not empty)
- Telemetry durable (already closed — verification test)
"""

import os
import tempfile
from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.execution.pending_store import PendingReport, PendingReportStore
from src.oms.reconcile import OMSReconcileService


def _make_intent(cid: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid, signal_id="test", strategy_id="test",
        symbol="BTC-USD", side="BUY", final_qty=Decimal("0.1"),
        order_type="LIMIT", price=Decimal("50000"),
        reduce_only=False, post_only=False, viable=True, planner_version="test",
    )


# ── Kill switch flatten convergence ──


class TestFlattenConvergence:

    def test_position_zero_after_flatten_fill(self, tmp_path):
        """Ledger position reaches 0 after flatten sell fill applied."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        # Buy position
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("0.5"), price=Decimal("50000"),
            cost=Decimal("25000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t-buy", order_id="o-buy",
        ))
        assert ledger.position_qty == Decimal("0.5")

        # Flatten fill arrives (simulates OMS reconcile)
        ledger.add_fill(Fill(
            side="sell", amount=Decimal("0.5"), price=Decimal("50000"),
            cost=Decimal("25000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=2000, trade_id="t-flatten", order_id="o-flatten",
        ))
        assert ledger.position_qty == Decimal("0")

    def test_convergence_check_logic(self, tmp_path):
        """position_qty <= threshold means converged."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        # No position → converged
        assert ledger.position_qty <= Decimal("0.00001")

    def test_non_convergence_with_remaining_position(self, tmp_path):
        """Position > threshold after flatten → not converged."""
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("1.0"), price=Decimal("50000"),
            cost=Decimal("50000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=1000, trade_id="t-buy2", order_id="o-buy2",
        ))
        # Partial flatten
        ledger.add_fill(Fill(
            side="sell", amount=Decimal("0.5"), price=Decimal("50000"),
            cost=Decimal("25000"), fee_cost=Decimal("0"), fee_currency="USD",
            ts_ms=2000, trade_id="t-partial", order_id="o-partial",
        ))
        assert ledger.position_qty > Decimal("0.00001")  # not converged


# ── Reconcile external with fills ──


class TestReconcileExternalFills:

    def test_unseen_fill_detected(self, tmp_path):
        """Exchange fill not in OMS → drift detected."""
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])

        exchange_fills = [{"trade_id": "unseen-fill-123"}]
        clean, drifts = oms.reconcile_against_exchange([], exchange_fills)
        assert not clean
        assert any("UNSEEN_FILL" in d for d in drifts)

    def test_seen_fill_no_drift(self, tmp_path):
        """Fill already in OMS → no drift."""
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])
        oms._seen_trade_ids.add("known-fill-456")

        exchange_fills = [{"trade_id": "known-fill-456"}]
        clean, drifts = oms.reconcile_against_exchange([], exchange_fills)
        assert clean

    def test_empty_fills_clean(self, tmp_path):
        """No exchange fills + no OMS open → clean."""
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])
        clean, drifts = oms.reconcile_against_exchange([], [])
        assert clean


# ── Telemetry durable (verification — already implemented) ──


class TestTelemetryDurableVerification:

    def test_pending_store_survives_restart(self, tmp_path):
        """PendingReportStore data persists across instances."""
        db = str(tmp_path / "pending.db")

        # Instance 1: save
        store1 = PendingReportStore(db_path=db)
        store1.save(PendingReport(
            client_order_id="durable-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("49500"),
            requested_qty=Decimal("0.1"),
            submit_latency_ms=150.0,
            submit_ts_ms=1_700_000_000_000,
        ))

        # Instance 2: load (simulates restart)
        store2 = PendingReportStore(db_path=db)
        loaded = store2.load("durable-1")
        assert loaded is not None
        assert loaded.expected_price == Decimal("49500")
        assert loaded.submit_latency_ms == 150.0

    def test_cleanup_stale_removes_old(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "p.db"))
        store.save(PendingReport(
            client_order_id="old-1", symbol="X", side="BUY",
            expected_price=Decimal("1"), requested_qty=Decimal("1"),
            submit_latency_ms=0, submit_ts_ms=1000,
        ))
        deleted = store.cleanup_stale(max_age_ms=1000)
        assert deleted == 1

    def test_estimated_flag_on_missing_pending(self):
        """When pending metadata missing, report marks slippage as estimated."""
        from src.execution.execution_report import build_execution_report
        report = build_execution_report(
            client_order_id="unknown-order",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),  # would be fill_price fallback
            fill_price=Decimal("50000"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=0.0, outcome="FILLED",
            estimated_slippage=True,
        )
        assert report.estimated_slippage
        assert report.slippage_bps == Decimal("0")
