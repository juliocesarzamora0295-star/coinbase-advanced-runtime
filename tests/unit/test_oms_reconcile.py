"""
Tests para OMSReconcileService.

Valida:
- Bootstrap completo en lotes de 50 + 50 + n
- Transición OPEN_PENDING -> FILLED/CANCELLED/EXPIRED
- Deduplicación de fills vía trade_id
"""

import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService


class TestOMSBootstrap:
    """Tests para bootstrap del canal user."""

    def setup_method(self):
        # Crear stores temporales
        self.temp_dir = tempfile.mkdtemp()

        idem_path = os.path.join(self.temp_dir, "idempotency.db")
        self.idempotency = IdempotencyStore(db_path=idem_path)

        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.ledger = TradeLedger("BTC-USD", db_path=ledger_path)

        self.fill_fetcher = MagicMock(return_value=[])
        self.oms = OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=self.fill_fetcher,
        )

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bootstrap_complete_on_first_batch_lt_50(self):
        """
        Bootstrap completo cuando primer batch tiene < 50 órdenes.

        Coinbase documenta que user channel devuelve órdenes en lotes de 50,
        terminando el bootstrap en el primer mensaje con < 50 órdenes.
        """
        # Simular snapshot con 9 órdenes (< 50)
        orders = [
            {
                "order_id": f"o-{i}",
                "client_order_id": f"c-{i}",
                "product_id": "BTC-USD",
                "status": "OPEN",
                "number_of_fills": "0",
            }
            for i in range(9)
        ]

        # Pre-populate idempotency store
        for i, order in enumerate(orders):
            intent = OrderIntent(
                client_order_id=order["client_order_id"],
                signal_id="test-signal",
                strategy_id="test-strategy",
                symbol="BTC-USD",
                side="BUY",
                final_qty=Decimal("0.01"),
                order_type="LIMIT",
                price=Decimal("50000"),
                reduce_only=False,
                post_only=True,
                viable=True,
                planner_version="test",
            )
            self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)

        self.oms.handle_user_event("snapshot", orders)

        # Bootstrap debe estar completo (9 < 50)
        assert self.oms.is_bootstrap_complete()
        stats = self.oms.get_stats()
        assert stats["snapshot_batches"] == 1
        assert stats["orders_in_snapshot"] == 9

    def test_bootstrap_50_plus_50_plus_n(self):
        """
        Bootstrap 50 + 50 + n lotes.

        Caso: primer batch 50, segundo batch 50, tercer batch 9.
        Bootstrap completo en tercer batch (< 50).
        """
        # Pre-populate idempotency store con 109 órdenes
        for batch_idx, batch_size in enumerate([50, 50, 9]):
            orders = []
            for i in range(batch_size):
                idx = batch_idx * 50 + i
                client_id = f"c-{batch_idx}-{i}"
                order_id = f"o-{batch_idx}-{i}"

                intent = OrderIntent(
                    client_order_id=client_id,
                    signal_id="test-signal",
                    strategy_id="test-strategy",
                    symbol="BTC-USD",
                    side="BUY",
                    final_qty=Decimal("0.01"),
                    order_type="LIMIT",
                    price=Decimal("50000"),
                    reduce_only=False,
                    post_only=True,
                    viable=True,
                    planner_version="test",
                )
                self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)

                orders.append(
                    {
                        "order_id": order_id,
                        "client_order_id": client_id,
                        "product_id": "BTC-USD",
                        "status": "OPEN",
                        "number_of_fills": "0",
                    }
                )

            self.oms.handle_user_event("snapshot", orders)

            if batch_idx < 2:
                # Bootstrap NO completo (50 == 50)
                assert not self.oms.is_bootstrap_complete()
            else:
                # Bootstrap completo (9 < 50)
                assert self.oms.is_bootstrap_complete()

        stats = self.oms.get_stats()
        assert stats["snapshot_batches"] == 3
        assert stats["orders_in_snapshot"] == 109


class TestOMSStateTransitions:
    """Tests para transiciones de estado de órdenes."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

        idem_path = os.path.join(self.temp_dir, "idempotency.db")
        self.idempotency = IdempotencyStore(db_path=idem_path)

        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.ledger = TradeLedger("BTC-USD", db_path=ledger_path)

        self.fill_fetcher = MagicMock(return_value=[])
        self.oms = OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=self.fill_fetcher,
        )

        # Agregar orden OPEN_PENDING al idempotency store
        self.order_id = "o-123"
        self.client_order_id = "c-123"
        intent = OrderIntent(
            client_order_id=self.client_order_id,
            signal_id="test-signal",
            strategy_id="test-strategy",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.1"),
            order_type="LIMIT",
            price=Decimal("50000"),
            reduce_only=False,
            post_only=True,
            viable=True,
            planner_version="test",
        )
        self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_transition_open_pending_to_filled(self):
        """Transición OPEN_PENDING -> FILLED."""
        # Mock fill_fetcher para retornar un fill
        self.fill_fetcher.return_value = [
            {
                "trade_id": "t-1",
                "side": "BUY",
                "size": "0.1",
                "price": "50000",
                "commission": "5",
                "trade_time": "2024-01-01T00:00:00Z",
            }
        ]

        update = {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }

        self.oms.handle_user_event("update", [update])

        # Verificar que el estado cambió a FILLED
        record = self.idempotency.get_by_client_order_id(self.client_order_id)
        assert record.state == OrderState.FILLED

    def test_state_transition_open_pending_to_cancelled(self):
        """Transición OPEN_PENDING -> CANCELLED."""
        update = {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": "BTC-USD",
            "status": "CANCELLED",
            "number_of_fills": "0",
        }

        self.oms.handle_user_event("update", [update])

        # Verificar que el estado cambió a CANCELLED
        record = self.idempotency.get_by_client_order_id(self.client_order_id)
        assert record.state == OrderState.CANCELLED

    def test_state_transition_cancel_queued_to_cancelled(self):
        """Transición CANCEL_QUEUED -> CANCELLED."""
        # Cambiar estado a CANCEL_QUEUED
        self.idempotency.update_state(self.client_order_id, OrderState.CANCEL_QUEUED)

        update = {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": "BTC-USD",
            "status": "CANCELLED",
            "number_of_fills": "0",
        }

        self.oms.handle_user_event("update", [update])

        # Verificar que el estado cambió a CANCELLED
        record = self.idempotency.get_by_client_order_id(self.client_order_id)
        assert record.state == OrderState.CANCELLED


class TestOMSFillsDeduplication:
    """Tests para deduplicación de fills."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

        idem_path = os.path.join(self.temp_dir, "idempotency.db")
        self.idempotency = IdempotencyStore(db_path=idem_path)

        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.ledger = TradeLedger("BTC-USD", db_path=ledger_path)

        self.fill_fetcher = MagicMock()
        self.oms = OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=self.fill_fetcher,
        )

        self.order_id = "o-123"
        self.client_order_id = "c-123"
        intent = OrderIntent(
            client_order_id=self.client_order_id,
            signal_id="test-signal",
            strategy_id="test-strategy",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.1"),
            order_type="LIMIT",
            price=Decimal("50000"),
            reduce_only=False,
            post_only=True,
            viable=True,
            planner_version="test",
        )
        self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_list_fills_deduplicates_trade_ids(self):
        """
        Deduplicación de fills vía trade_id.

        El mismo trade_id no debe aplicarse dos veces al ledger.
        """
        # Primer reconcile: 2 fills nuevos
        self.fill_fetcher.return_value = [
            {
                "trade_id": "t-1",
                "side": "BUY",
                "size": "0.05",
                "price": "50000",
                "commission": "2.5",
                "trade_time": "2024-01-01T00:00:00Z",
            },
            {
                "trade_id": "t-2",
                "side": "BUY",
                "size": "0.05",
                "price": "50100",
                "commission": "2.5",
                "trade_time": "2024-01-01T00:01:00Z",
            },
        ]

        update1 = {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": "BTC-USD",
            "status": "OPEN",
            "number_of_fills": "2",
        }
        self.oms.handle_user_event("update", [update1])

        # Debe tener 2 fills en ledger
        assert len(self.ledger.fills) == 2

        # Segundo reconcile: mismo número de fills (sin cambios)
        self.oms.handle_user_event("update", [update1])

        # No debe agregar fills duplicados
        assert len(self.ledger.fills) == 2

        # Tercer reconcile: 1 fill nuevo (t-3)
        self.fill_fetcher.return_value = [
            {
                "trade_id": "t-1",
                "side": "BUY",
                "size": "0.05",
                "price": "50000",
                "commission": "2.5",
                "trade_time": "2024-01-01T00:00:00Z",
            },
            {
                "trade_id": "t-2",
                "side": "BUY",
                "size": "0.05",
                "price": "50100",
                "commission": "2.5",
                "trade_time": "2024-01-01T00:01:00Z",
            },
            {
                "trade_id": "t-3",
                "side": "BUY",
                "size": "0.05",
                "price": "50200",
                "commission": "2.5",
                "trade_time": "2024-01-01T00:02:00Z",
            },
        ]

        update2 = {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "product_id": "BTC-USD",
            "status": "OPEN",
            "number_of_fills": "3",
        }
        self.oms.handle_user_event("update", [update2])

        # Debe tener 3 fills (t-1, t-2, t-3)
        assert len(self.ledger.fills) == 3
        trade_ids = {f.trade_id for f in self.ledger.fills}
        assert trade_ids == {"t-1", "t-2", "t-3"}


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
