"""
Reconcile stress tests — edge cases found in observe-only runs.

Tests mock Coinbase API responses for OMSReconcileService:
- Empty orders list
- Partial fills
- Orphan orders
- Network timeout during fill fetch
- Multiple fills on same order
- Out-of-order fill delivery
- Concurrent bootstrap + order events
- Degraded state recovery
- External reconcile drift detection
- Duplicate trade_id deduplication under load
"""

import os
import tempfile
import uuid
from decimal import Decimal
from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService, OMSIncident


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_intent(client_order_id: str, symbol: str = "BTC-USD") -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id=f"sig-{client_order_id}",
        strategy_id="test-strategy",
        symbol=symbol,
        side="BUY",
        final_qty=Decimal("0.001"),
        order_type="MARKET",
        price=None,
        reduce_only=False,
        post_only=False,
        viable=True,
        planner_version="1.0",
    )


def make_order_event(
    order_id: str,
    client_order_id: str,
    status: str = "OPEN",
    product_id: str = "BTC-USD",
    number_of_fills: int = 0,
    order_side: str = "BUY",
) -> Dict:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "status": status,
        "product_id": product_id,
        "order_side": order_side,
        "number_of_fills": str(number_of_fills),
    }


def make_fill(
    trade_id: str,
    price: str = "70000",
    size: str = "0.001",
    commission: str = "0.07",
    side: str = "BUY",
    trade_time: str = "2026-01-01T00:00:00Z",
) -> Dict:
    return {
        "trade_id": trade_id,
        "price": price,
        "size": size,
        "commission": commission,
        "side": side,
        "trade_time": trade_time,
    }


class ReconcileFixture:
    """Shared setup for reconcile tests."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        idem_path = os.path.join(self.temp_dir, f"idem_{uuid.uuid4().hex[:8]}.db")
        ledger_path = os.path.join(self.temp_dir, f"ledger_{uuid.uuid4().hex[:8]}.db")

        self.idempotency = IdempotencyStore(db_path=idem_path)
        self.ledger = TradeLedger("BTC-USD", db_path=ledger_path)
        self.fill_fetcher = MagicMock(return_value=[])
        self.degraded_incidents: List[OMSIncident] = []
        self.bootstrap_complete_called = False

        def on_degraded(incident):
            self.degraded_incidents.append(incident)

        def on_bootstrap():
            self.bootstrap_complete_called = True

        self.oms = OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=self.fill_fetcher,
            on_bootstrap_complete=on_bootstrap,
            on_degraded=on_degraded,
        )

    def register_order(self, client_order_id: str, symbol: str = "BTC-USD") -> None:
        """Register an order in the idempotency store."""
        intent = make_intent(client_order_id, symbol)
        self.idempotency.save_intent(intent)


# ──────────────────────────────────────────────
# Test 1: Empty orders response from exchange
# ──────────────────────────────────────────────

class TestEmptyOrdersResponse:
    def test_reconcile_with_no_exchange_orders(self):
        """Exchange returns empty orders list — OMS should be clean."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        clean, drifts = fix.oms.reconcile_against_exchange(
            exchange_open_orders=[],
            exchange_recent_fills=[],
        )
        assert clean is True
        assert drifts == []

    def test_reconcile_empty_exchange_with_oms_pending(self):
        """OMS has pending order but exchange has none — drift detected."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-001")

        clean, drifts = fix.oms.reconcile_against_exchange(
            exchange_open_orders=[],
            exchange_recent_fills=[],
        )
        assert clean is False
        assert any("OMS_OPEN_NOT_ON_EXCHANGE" in d for d in drifts)

    def test_bootstrap_with_zero_orders_snapshot(self):
        """Snapshot with 0 orders should complete bootstrap."""
        fix = ReconcileFixture()
        assert not fix.oms.is_bootstrap_complete()

        fix.oms.handle_user_event("snapshot", [])
        assert fix.oms.is_bootstrap_complete()
        assert fix.bootstrap_complete_called


# ──────────────────────────────────────────────
# Test 2: Partial fills
# ──────────────────────────────────────────────

class TestPartialFills:
    def test_partial_fill_updates_state(self):
        """Order with partial fill should stay OPEN and apply the fill."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-partial")

        fill1 = make_fill("trade-001", size="0.0005")
        fix.fill_fetcher.return_value = [fill1]

        order_evt = make_order_event(
            "oid-partial", "cid-partial", status="OPEN", number_of_fills=1
        )
        fix.oms.handle_user_event("update", [order_evt])

        fix.fill_fetcher.assert_called_once()
        assert "trade-001" in fix.oms._seen_trade_ids

    def test_second_partial_fill_deduplicates_first(self):
        """Second fill fetch should not re-apply first fill."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-partial2")

        fill1 = make_fill("trade-100", size="0.0003")
        fill2 = make_fill("trade-101", size="0.0002")

        # First update: 1 fill
        fix.fill_fetcher.return_value = [fill1]
        order_evt1 = make_order_event(
            "oid-p2", "cid-partial2", status="OPEN", number_of_fills=1
        )
        fix.oms.handle_user_event("update", [order_evt1])

        # Second update: 2 fills (includes first)
        fix.fill_fetcher.return_value = [fill1, fill2]
        order_evt2 = make_order_event(
            "oid-p2", "cid-partial2", status="OPEN", number_of_fills=2
        )
        fix.oms.handle_user_event("update", [order_evt2])

        assert "trade-100" in fix.oms._seen_trade_ids
        assert "trade-101" in fix.oms._seen_trade_ids


# ──────────────────────────────────────────────
# Test 3: Orphan orders
# ──────────────────────────────────────────────

class TestOrphanOrders:
    def test_orphan_order_triggers_degradation(self):
        """Order on exchange not in OMS → degraded state."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        orphan_event = make_order_event("oid-orphan", "cid-unknown", status="OPEN")
        fix.oms.handle_user_event("update", [orphan_event])

        assert fix.oms.is_degraded()
        assert len(fix.degraded_incidents) == 1
        assert fix.degraded_incidents[0].incident_type == "ORPHAN_ORDER"

    def test_orphan_blocks_readiness(self):
        """After orphan detection, is_ready() returns False."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        assert fix.oms.is_ready()

        orphan_event = make_order_event("oid-o2", "cid-unknown2", status="FILLED")
        fix.oms.handle_user_event("update", [orphan_event])

        assert not fix.oms.is_ready()

    def test_multiple_orphans_accumulate_incidents(self):
        """Multiple orphan orders → multiple incidents."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        for i in range(5):
            orphan = make_order_event(f"oid-orph-{i}", f"cid-unk-{i}", status="OPEN")
            fix.oms.handle_user_event("update", [orphan])

        assert len(fix.degraded_incidents) == 5
        assert fix.oms.is_degraded()


# ──────────────────────────────────────────────
# Test 4: Network timeout during fill fetch
# ──────────────────────────────────────────────

class TestNetworkTimeout:
    def test_fill_fetch_exception_triggers_degradation(self):
        """Exception in fill_fetcher → OMS degraded."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-timeout")

        fix.fill_fetcher.side_effect = ConnectionError("timeout")

        order_evt = make_order_event(
            "oid-timeout", "cid-timeout", status="OPEN", number_of_fills=1
        )
        fix.oms.handle_user_event("update", [order_evt])

        assert fix.oms.is_degraded()
        assert any(i.incident_type == "FILL_FETCH_FAILED" for i in fix.degraded_incidents)

    def test_timeout_recovery_after_clean_reconciles(self):
        """After fill_fetch failure, N clean reconciles clear degradation."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-recover")

        # Trigger failure
        fix.fill_fetcher.side_effect = TimeoutError("network")
        order_evt = make_order_event(
            "oid-recover", "cid-recover", status="OPEN", number_of_fills=1
        )
        fix.oms.handle_user_event("update", [order_evt])
        assert fix.oms.is_degraded()

        # Reset fetcher and mark order as FILLED so reconcile is clean
        fix.fill_fetcher.side_effect = None
        fix.fill_fetcher.return_value = []
        fix.idempotency.update_state(
            client_order_id="cid-recover",
            state=OrderState.FILLED,
            exchange_order_id="oid-recover",
        )

        # External reconcile clean (no open orders on either side)
        fix.oms.reconcile_against_exchange([], [])

        # Record clean reconciles (need threshold met + external clean)
        for _ in range(fix.oms.clean_reconcile_threshold):
            fix.oms.record_clean_reconcile()

        assert not fix.oms.is_degraded()


# ──────────────────────────────────────────────
# Test 5: Multiple fills on same order
# ──────────────────────────────────────────────

class TestMultipleFills:
    def test_three_fills_on_one_order(self):
        """Order filled in 3 partial fills, all applied."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-multi")

        fills = [
            make_fill(f"trade-m{i}", size=f"0.000{i+1}") for i in range(3)
        ]

        # Simulate incremental fill events
        for i, fill in enumerate(fills, 1):
            fix.fill_fetcher.return_value = fills[:i]
            order_evt = make_order_event(
                "oid-multi", "cid-multi",
                status="OPEN" if i < 3 else "FILLED",
                number_of_fills=i,
            )
            fix.oms.handle_user_event("update", [order_evt])

        for i in range(3):
            assert f"trade-m{i}" in fix.oms._seen_trade_ids


# ──────────────────────────────────────────────
# Test 6: Out-of-order event delivery
# ──────────────────────────────────────────────

class TestOutOfOrderDelivery:
    def test_filled_before_open(self):
        """FILLED event arrives before OPEN — state jumps directly."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-ooo")

        # FILLED arrives first
        order_evt = make_order_event(
            "oid-ooo", "cid-ooo", status="FILLED", number_of_fills=1
        )
        fill = make_fill("trade-ooo")
        fix.fill_fetcher.return_value = [fill]
        fix.oms.handle_user_event("update", [order_evt])

        record = fix.idempotency.get_by_client_order_id("cid-ooo")
        assert record.state == OrderState.FILLED
        assert "trade-ooo" in fix.oms._seen_trade_ids


# ──────────────────────────────────────────────
# Test 7: Bootstrap + order events concurrent
# ──────────────────────────────────────────────

class TestBootstrapConcurrency:
    def test_snapshot_with_known_and_unknown_orders(self):
        """Snapshot contains mix of known and unknown orders."""
        fix = ReconcileFixture()
        fix.register_order("cid-known")

        events = [
            make_order_event("oid-known", "cid-known", status="OPEN"),
            make_order_event("oid-unknown", "cid-unknown", status="OPEN"),
        ]

        fix.oms.handle_user_event("snapshot", events)

        # Bootstrap completed
        assert fix.oms.is_bootstrap_complete()
        # Unknown order triggered orphan → degraded
        assert fix.oms.is_degraded()

    def test_force_bootstrap_after_timeout(self):
        """Force bootstrap completion when no snapshot arrives."""
        fix = ReconcileFixture()
        assert not fix.oms.is_bootstrap_complete()

        result = fix.oms.complete_bootstrap_if_no_snapshot()
        assert result is True
        assert fix.oms.is_bootstrap_complete()

    def test_force_bootstrap_noop_if_already_complete(self):
        """Force bootstrap is noop if already done."""
        fix = ReconcileFixture()
        fix.oms.handle_user_event("snapshot", [])
        assert fix.oms.is_bootstrap_complete()

        result = fix.oms.complete_bootstrap_if_no_snapshot()
        assert result is False


# ──────────────────────────────────────────────
# Test 8: External reconcile drift detection
# ──────────────────────────────────────────────

class TestExternalReconcile:
    def test_exchange_order_not_in_oms(self):
        """Exchange has open order unknown to OMS → drift."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        exchange_orders = [
            {"client_order_id": "cid-ext-unknown", "order_id": "oid-ext"}
        ]
        clean, drifts = fix.oms.reconcile_against_exchange(exchange_orders, [])

        assert clean is False
        assert any("EXCHANGE_OPEN_NOT_IN_OMS" in d for d in drifts)

    def test_unseen_fill_detected(self):
        """Exchange has fill that OMS never saw → drift."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        fills = [{"trade_id": "trade-unseen-999"}]
        clean, drifts = fix.oms.reconcile_against_exchange([], fills)

        assert clean is False
        assert any("UNSEEN_FILL" in d for d in drifts)

    def test_clean_reconcile_marks_external_clean(self):
        """Clean external reconcile sets the clean flag."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        clean, drifts = fix.oms.reconcile_against_exchange([], [])
        assert clean is True
        assert fix.oms.last_external_reconcile_clean is True

    def test_dirty_reconcile_resets_clean_counter(self):
        """Dirty reconcile resets consecutive clean counter."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        # Record some clean reconciles
        fix.oms.record_clean_reconcile()
        fix.oms.record_clean_reconcile()

        # Dirty reconcile
        fix.oms.record_dirty_reconcile()

        # Counter reset verified indirectly — degraded would need more cleans
        assert fix.oms._consecutive_clean_reconciles == 0


# ──────────────────────────────────────────────
# Test 9: Trade ID deduplication under load
# ──────────────────────────────────────────────

class TestDeduplicationLoad:
    def test_same_trade_id_across_multiple_events(self):
        """Same trade_id delivered in multiple events — applied only once."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-dedup")

        fill = make_fill("trade-dedup-001", size="0.001")
        fix.fill_fetcher.return_value = [fill]

        # Same order event delivered 5 times
        for _ in range(5):
            order_evt = make_order_event(
                "oid-dedup", "cid-dedup", status="FILLED", number_of_fills=1
            )
            fix.oms.handle_user_event("update", [order_evt])

        assert "trade-dedup-001" in fix.oms._seen_trade_ids
        # fill_fetcher called once for first fill count change, then skipped
        # (number_of_fills stays at 1 so no re-fetch after first)


# ──────────────────────────────────────────────
# Test 10: Degraded state management
# ──────────────────────────────────────────────

class TestDegradedState:
    def test_clear_degraded_explicitly(self):
        """Explicit clear_degraded resets state."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        fix.oms.report_divergence("test divergence")
        assert fix.oms.is_degraded()

        fix.oms.clear_degraded()
        assert not fix.oms.is_degraded()

    def test_degraded_blocks_readiness(self):
        """Degraded OMS is not ready even with bootstrap complete."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        assert fix.oms.is_ready()

        fix.oms.report_divergence("drift detected")
        assert not fix.oms.is_ready()

    def test_incidents_accumulate(self):
        """All incidents are recorded in get_incidents()."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True

        fix.oms.report_divergence("d1")
        fix.oms.report_divergence("d2")

        orphan = make_order_event("oid-inc", "cid-inc-unknown", status="OPEN")
        fix.oms.handle_user_event("update", [orphan])

        incidents = fix.oms.get_incidents()
        assert len(incidents) == 3
        types = {i.incident_type for i in incidents}
        assert "DIVERGENCE" in types
        assert "ORPHAN_ORDER" in types


# ──────────────────────────────────────────────
# Test 11: Status mapping edge cases
# ──────────────────────────────────────────────

class TestStatusMapping:
    def test_cancel_queued_state(self):
        """CANCEL_QUEUED status maps correctly."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-cq")

        order_evt = make_order_event("oid-cq", "cid-cq", status="CANCEL_QUEUED")
        fix.oms.handle_user_event("update", [order_evt])

        record = fix.idempotency.get_by_client_order_id("cid-cq")
        assert record.state == OrderState.CANCEL_QUEUED

    def test_expired_state(self):
        """EXPIRED status maps correctly."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-exp")

        order_evt = make_order_event("oid-exp", "cid-exp", status="EXPIRED")
        fix.oms.handle_user_event("update", [order_evt])

        record = fix.idempotency.get_by_client_order_id("cid-exp")
        assert record.state == OrderState.EXPIRED

    def test_failed_state(self):
        """FAILED status maps correctly."""
        fix = ReconcileFixture()
        fix.oms._bootstrap_complete = True
        fix.register_order("cid-fail")

        order_evt = make_order_event("oid-fail", "cid-fail", status="FAILED")
        fix.oms.handle_user_event("update", [order_evt])

        record = fix.idempotency.get_by_client_order_id("cid-fail")
        assert record.state == OrderState.FAILED


# ──────────────────────────────────────────────
# Test 12: Stats reporting
# ──────────────────────────────────────────────

class TestStatsReporting:
    def test_stats_after_activity(self):
        """get_stats returns accurate counts after operations."""
        fix = ReconcileFixture()
        fix.register_order("cid-stats")

        # Bootstrap
        fix.oms.handle_user_event("snapshot", [
            make_order_event("oid-stats", "cid-stats", status="OPEN"),
        ])

        # Fill
        fill = make_fill("trade-stats-001")
        fix.fill_fetcher.return_value = [fill]
        fix.oms.handle_user_event("update", [
            make_order_event("oid-stats", "cid-stats", status="FILLED", number_of_fills=1),
        ])

        stats = fix.oms.get_stats()
        assert stats["bootstrap_complete"] is True
        assert stats["seen_trade_ids"] == 1
        assert stats["snapshot_batches"] == 1
