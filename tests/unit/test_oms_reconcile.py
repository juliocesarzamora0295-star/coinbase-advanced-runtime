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
from src.execution.idempotency import IdempotencyStore, OrderIntent, OrderState
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
                intent_id=f"intent-{i}",
                client_order_id=order["client_order_id"],
                product_id="BTC-USD",
                side="BUY",
                order_type="LIMIT",
                qty=Decimal("0.01"),
                price=Decimal("50000"),
                stop_price=None,
                post_only=True,
                created_ts_ms=1234567890 + i,
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
                    intent_id=f"intent-{idx}",
                    client_order_id=client_id,
                    product_id="BTC-USD",
                    side="BUY",
                    order_type="LIMIT",
                    qty=Decimal("0.01"),
                    price=Decimal("50000"),
                    stop_price=None,
                    post_only=True,
                    created_ts_ms=1234567890 + idx,
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
            intent_id="intent-123",
            client_order_id=self.client_order_id,
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
        )
        self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_transition_open_pending_to_reconcile_resolved(self):
        """Transición OPEN_PENDING -> RECONCILE_RESOLVED cuando fills convergen."""
        # fill_fetcher retorna 1 fill y fill_count=1 → ledger converge
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

        # El estado debe ser RECONCILE_RESOLVED (no FILLED): ledger convergió
        record = self.idempotency.get_by_client_order_id(self.client_order_id)
        assert record.state == OrderState.RECONCILE_RESOLVED
        assert record.is_terminal is True

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
        self.idempotency.update_state("intent-123", OrderState.CANCEL_QUEUED)

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
            intent_id="intent-123",
            client_order_id=self.client_order_id,
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
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


class TestOMSReconcileConflictStates:
    """
    Tests para estados RECONCILE_PENDING / RECONCILE_CONFLICT / RECONCILE_RESOLVED.

    Invariante clave: OMS no marca terminal limpio si el ledger no convergió.
    """

    def _make_oms(self, fill_fetcher=None):
        self.temp_dir = tempfile.mkdtemp()
        idem_path = os.path.join(self.temp_dir, "idempotency.db")
        self.idempotency = IdempotencyStore(db_path=idem_path)
        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.ledger = TradeLedger("BTC-USD", db_path=ledger_path)

        oms = OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=fill_fetcher,
        )

        intent = OrderIntent(
            intent_id="intent-x",
            client_order_id="c-x",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
        )
        self.idempotency.save_intent(intent, state=OrderState.OPEN_PENDING)
        return oms

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _filled_update(self, fill_count: int) -> dict:
        return {
            "order_id": "o-x",
            "client_order_id": "c-x",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": str(fill_count),
        }

    def test_filled_zero_fills_stays_filled(self):
        """FILLED con fill_count=0 → estado FILLED (sin fills que verificar)."""
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))
        oms.handle_user_event("update", [self._filled_update(0)])

        record = self.idempotency.get_by_client_order_id("c-x")
        assert record.state == OrderState.FILLED
        assert record.is_terminal is True

    def test_filled_fills_converge_resolved(self):
        """FILLED con fill_count=2 y 2 fills aplicados → RECONCILE_RESOLVED."""
        fetcher = MagicMock(
            return_value=[
                {
                    "trade_id": "t-1",
                    "side": "buy",
                    "size": "0.05",
                    "price": "50000",
                    "commission": "2.5",
                    "trade_time": "2024-01-01T00:00:00Z",
                },
                {
                    "trade_id": "t-2",
                    "side": "buy",
                    "size": "0.05",
                    "price": "50100",
                    "commission": "2.5",
                    "trade_time": "2024-01-01T00:01:00Z",
                },
            ]
        )
        oms = self._make_oms(fill_fetcher=fetcher)
        oms.handle_user_event("update", [self._filled_update(2)])

        record = self.idempotency.get_by_client_order_id("c-x")
        assert record.state == OrderState.RECONCILE_RESOLVED
        assert record.is_terminal is True
        assert record.is_ledger_conflict is False

    def test_filled_fills_diverge_conflict(self):
        """FILLED con fill_count=3 pero fetcher retorna 1 → RECONCILE_CONFLICT."""
        # fetcher retorna solo 1 fill de 3 esperados
        fetcher = MagicMock(
            return_value=[
                {
                    "trade_id": "t-1",
                    "side": "buy",
                    "size": "0.05",
                    "price": "50000",
                    "commission": "2.5",
                    "trade_time": "2024-01-01T00:00:00Z",
                }
            ]
        )
        oms = self._make_oms(fill_fetcher=fetcher)
        oms.handle_user_event("update", [self._filled_update(3)])

        record = self.idempotency.get_by_client_order_id("c-x")
        assert record.state == OrderState.RECONCILE_CONFLICT
        assert record.is_terminal is False  # ledger no convergió → no es terminal limpio
        assert record.is_ledger_conflict is True

    def test_filled_no_fetcher_pending(self):
        """FILLED con fill_count>0 y sin fill_fetcher → RECONCILE_PENDING."""
        oms = self._make_oms(fill_fetcher=None)
        oms.handle_user_event("update", [self._filled_update(1)])

        record = self.idempotency.get_by_client_order_id("c-x")
        assert record.state == OrderState.RECONCILE_PENDING
        assert record.is_terminal is False
        assert record.is_active is True  # activo: esperando resolución

    def test_reconcile_conflict_counted_in_stats(self):
        """Stats reporta el número de conflictos detectados."""
        fetcher = MagicMock(return_value=[])  # retorna 0 fills pero fill_count=2
        oms = self._make_oms(fill_fetcher=fetcher)
        oms.handle_user_event("update", [self._filled_update(2)])

        stats = oms.get_stats()
        assert stats["reconcile_conflicts"] == 1

    def test_no_conflict_zero_fills_stats(self):
        """Sin conflictos cuando fill_count=0."""
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))
        oms.handle_user_event("update", [self._filled_update(0)])

        stats = oms.get_stats()
        assert stats["reconcile_conflicts"] == 0

    def test_conflict_fetch_exception_marks_conflict(self):
        """Si fetch falla, la orden queda en RECONCILE_CONFLICT (fail-closed)."""
        fetcher = MagicMock(side_effect=RuntimeError("API timeout"))
        oms = self._make_oms(fill_fetcher=fetcher)
        oms.handle_user_event("update", [self._filled_update(1)])

        record = self.idempotency.get_by_client_order_id("c-x")
        assert record.state == OrderState.RECONCILE_CONFLICT
        assert record.is_terminal is False


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
