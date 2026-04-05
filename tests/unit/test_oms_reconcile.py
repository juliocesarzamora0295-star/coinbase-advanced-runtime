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


# ──────────────────────────────────────────────────────────────
# TestOMSRestReconcile — reconcile_open_orders() (REST path)
# ──────────────────────────────────────────────────────────────


def _make_oms_setup(temp_dir):
    """Construir IdempotencyStore + TradeLedger + OMSReconcileService."""
    idem_path = os.path.join(temp_dir, "idempotency.db")
    idempotency = IdempotencyStore(db_path=idem_path)

    ledger_path = os.path.join(temp_dir, "ledger.db")
    ledger = TradeLedger("BTC-USD", db_path=ledger_path)

    return idempotency, ledger


def _save_open_intent(
    idempotency: IdempotencyStore,
    intent_id: str,
    client_order_id: str,
    state: "OrderState" = None,
    exchange_order_id: str = None,
) -> None:
    """Guardar un intent en estado activo con exchange_order_id."""
    from src.execution.idempotency import OrderState as OS

    if state is None:
        state = OS.OPEN_RESTING

    intent = OrderIntent(
        intent_id=intent_id,
        client_order_id=client_order_id,
        product_id="BTC-USD",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.1"),
        price=Decimal("50000"),
        stop_price=None,
        post_only=True,
        created_ts_ms=1234567890,
    )
    idempotency.save_intent(intent, OrderState.NEW)
    if exchange_order_id:
        idempotency.update_state(
            intent_id=intent_id,
            state=state,
            exchange_order_id=exchange_order_id,
        )
    else:
        idempotency.update_state(intent_id=intent_id, state=state)


class TestOMSRestReconcile:
    """Tests para reconcile_open_orders() — path REST activo."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.idempotency, self.ledger = _make_oms_setup(self.temp_dir)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_oms(self, fill_fetcher=None):
        return OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=fill_fetcher,
        )

    # ─── Happy path ───

    def test_rest_reconcile_filled_updates_state(self):
        """REST dice FILLED → estado OMS actualizado a FILLED."""
        _save_open_intent(self.idempotency, "i-001", "c-001", exchange_order_id="ex-001")
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))

        stats = oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "0"})

        record = self.idempotency.get_by_intent_id("i-001")
        assert record.state == OrderState.FILLED
        assert stats["filled"] == 1
        assert stats["updated"] == 1

    def test_rest_reconcile_cancelled_updates_state(self):
        """REST dice CANCELLED → estado OMS actualizado a CANCELLED."""
        _save_open_intent(self.idempotency, "i-002", "c-002", exchange_order_id="ex-002")
        oms = self._make_oms()

        oms.reconcile_open_orders(lambda oid: {"status": "CANCELLED", "number_of_fills": "0"})

        record = self.idempotency.get_by_intent_id("i-002")
        assert record.state == OrderState.CANCELLED
        active_ids = [r.intent_id for r in self.idempotency.get_pending_or_open()]
        assert "i-002" not in active_ids

    def test_rest_reconcile_still_open_no_update(self):
        """REST dice OPEN → estado OMS sin cambio (ya es OPEN_RESTING)."""
        _save_open_intent(self.idempotency, "i-003", "c-003", exchange_order_id="ex-003")
        oms = self._make_oms()

        stats = oms.reconcile_open_orders(lambda oid: {"status": "OPEN", "number_of_fills": "0"})

        record = self.idempotency.get_by_intent_id("i-003")
        assert record.state == OrderState.OPEN_RESTING
        assert stats["updated"] == 0

    def test_rest_reconcile_filled_triggers_fill_fetcher(self):
        """REST dice FILLED con fills → fill_fetcher llamado, fill aplicado al ledger."""
        _save_open_intent(self.idempotency, "i-004", "c-004", exchange_order_id="ex-004")
        fill_fetcher = MagicMock(
            return_value=[
                {
                    "trade_id": "t-rest-001",
                    "side": "BUY",
                    "size": "0.1",
                    "price": "50000",
                    "commission": "0",
                    "trade_time": "2024-01-01T00:00:00Z",
                }
            ]
        )
        oms = self._make_oms(fill_fetcher=fill_fetcher)

        oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "1"})

        fill_fetcher.assert_called_once_with("ex-004")
        assert self.ledger.position_qty == Decimal("0.1")

    def test_rest_reconcile_cancel_queued_to_cancelled(self):
        """CANCEL_QUEUED en OMS, REST dice CANCELLED → actualizado correctamente."""
        _save_open_intent(
            self.idempotency,
            "i-005",
            "c-005",
            state=OrderState.CANCEL_QUEUED,
            exchange_order_id="ex-005",
        )
        oms = self._make_oms()

        oms.reconcile_open_orders(lambda oid: {"status": "CANCELLED", "number_of_fills": "0"})

        record = self.idempotency.get_by_intent_id("i-005")
        assert record.state == OrderState.CANCELLED

    # ─── Fail-open y deduplicación ───

    def test_rest_reconcile_skips_intent_without_exchange_id(self):
        """Intent sin exchange_order_id → ignorado (no llamar al fetcher)."""
        _save_open_intent(self.idempotency, "i-006", "c-006", exchange_order_id=None)
        fetcher_calls = []
        oms = self._make_oms()

        stats = oms.reconcile_open_orders(lambda oid: fetcher_calls.append(oid) or {})

        assert len(fetcher_calls) == 0
        assert stats["checked"] == 0

    def test_rest_reconcile_fetcher_returns_none_skipped(self):
        """Fetcher retorna None (orden no encontrada) → ignorada, sin excepción."""
        _save_open_intent(self.idempotency, "i-007", "c-007", exchange_order_id="ex-007")
        oms = self._make_oms()

        stats = oms.reconcile_open_orders(lambda oid: None)

        record = self.idempotency.get_by_intent_id("i-007")
        assert record.state == OrderState.OPEN_RESTING  # sin cambio
        assert stats["errors"] == 0

    def test_rest_reconcile_fetcher_exception_continues_others(self):
        """Fetcher lanza para una orden → error contado, demás órdenes reconciliadas."""
        _save_open_intent(self.idempotency, "i-008a", "c-008a", exchange_order_id="ex-008a")
        _save_open_intent(self.idempotency, "i-008b", "c-008b", exchange_order_id="ex-008b")

        def selective_fetcher(oid):
            if oid == "ex-008a":
                raise ConnectionError("timeout")
            return {"status": "CANCELLED", "number_of_fills": "0"}

        oms = self._make_oms()
        stats = oms.reconcile_open_orders(selective_fetcher)

        assert stats["errors"] == 1
        assert stats["cancelled"] == 1  # ex-008b fue reconciliada
        record_b = self.idempotency.get_by_intent_id("i-008b")
        assert record_b.state == OrderState.CANCELLED

    def test_rest_reconcile_fill_fetcher_exception_does_not_block_state_update(self):
        """fill_fetcher lanza → estado OMS igual actualizado a FILLED."""
        _save_open_intent(self.idempotency, "i-009", "c-009", exchange_order_id="ex-009")

        def exploding_fill_fetcher(oid):
            raise RuntimeError("REST error")

        oms = self._make_oms(fill_fetcher=exploding_fill_fetcher)
        oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "1"})

        record = self.idempotency.get_by_intent_id("i-009")
        assert record.state == OrderState.FILLED  # estado correcto
        assert self.ledger.position_qty == Decimal("0")  # sin fill (fetcher falló)

    def test_rest_reconcile_returns_stats_dict(self):
        """reconcile_open_orders retorna dict con conteos correctos."""
        for i in range(3):
            _save_open_intent(
                self.idempotency,
                f"i-010-{i}",
                f"c-010-{i}",
                exchange_order_id=f"ex-010-{i}",
            )
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))

        statuses = ["FILLED", "CANCELLED", "OPEN"]
        idx = [0]

        def rotating_fetcher(oid):
            s = statuses[idx[0] % len(statuses)]
            idx[0] += 1
            return {"status": s, "number_of_fills": "0"}

        stats = oms.reconcile_open_orders(rotating_fetcher)

        assert stats["checked"] == 3
        assert stats["filled"] == 1
        assert stats["cancelled"] == 1
        assert stats["updated"] == 2  # FILLED + CANCELLED actualizaron estado
        assert stats["errors"] == 0

    def test_rest_reconcile_fill_deduplication_across_calls(self):
        """Mismo trade_id en dos llamadas a reconcile → contado una sola vez."""
        _save_open_intent(self.idempotency, "i-011", "c-011", exchange_order_id="ex-011")
        fill_data = [
            {
                "trade_id": "t-dedup-rest",
                "side": "BUY",
                "size": "0.1",
                "price": "50000",
                "commission": "0",
                "trade_time": "2024-01-01T00:00:00Z",
            }
        ]
        fill_fetcher = MagicMock(return_value=fill_data)
        oms = self._make_oms(fill_fetcher=fill_fetcher)

        # Primera reconciliación: fill nuevo
        oms.reconcile_open_orders(lambda oid: {"status": "OPEN", "number_of_fills": "1"})
        qty_after_first = self.ledger.position_qty

        # Segunda: misma orden, mismo fill_count (no re-fetch)
        oms.reconcile_open_orders(lambda oid: {"status": "OPEN", "number_of_fills": "1"})

        assert self.ledger.position_qty == qty_after_first
        assert fill_fetcher.call_count == 1  # no llamado segunda vez (fill_count no aumentó)


# ──────────────────────────────────────────────────────────────
# TestOMSCertificationInvariants — 4 invariantes de CLAUDE.md
# ──────────────────────────────────────────────────────────────


class TestOMSCertificationInvariants:
    """
    Certificación explícita de los 4 invariantes OMS del CLAUDE.md:

    1. Idempotencia
    2. Deduplicación por trade_id
    3. Manejo de CANCEL_QUEUED
    4. Reconcile consistente con fills
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.idempotency, self.ledger = _make_oms_setup(self.temp_dir)

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_oms(self, fill_fetcher=None):
        return OMSReconcileService(
            idempotency=self.idempotency,
            ledger=self.ledger,
            fill_fetcher=fill_fetcher,
        )

    # ─── Invariante 1: Idempotencia ───

    def test_invariant_idempotence_same_event_twice(self):
        """
        Invariante 1 — Idempotencia:
        El mismo evento WS procesado dos veces no altera el estado terminal.
        """
        _save_open_intent(
            self.idempotency, "idem-001", "cidem-001", exchange_order_id="ex-idem-001"
        )
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))

        event = {
            "order_id": "ex-idem-001",
            "client_order_id": "cidem-001",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }

        oms.handle_user_event("update", [event])
        state_after_first = self.idempotency.get_by_intent_id("idem-001").state

        oms.handle_user_event("update", [event])  # mismo evento
        state_after_second = self.idempotency.get_by_intent_id("idem-001").state

        assert state_after_first == OrderState.FILLED
        assert state_after_second == OrderState.FILLED  # idempotente

    def test_invariant_idempotence_rest_reconcile_twice(self):
        """
        Invariante 1 — Idempotencia:
        reconcile_open_orders llamado dos veces con mismo resultado → stats updated=0 en segunda.
        """
        _save_open_intent(
            self.idempotency, "idem-002", "cidem-002", exchange_order_id="ex-idem-002"
        )
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=[]))

        stats1 = oms.reconcile_open_orders(
            lambda oid: {"status": "CANCELLED", "number_of_fills": "0"}
        )
        stats2 = oms.reconcile_open_orders(
            lambda oid: {"status": "CANCELLED", "number_of_fills": "0"}
        )

        assert stats1["updated"] == 1  # primera vez actualiza
        assert stats2["updated"] == 0  # segunda: estado ya es CANCELLED, sin update

    # ─── Invariante 2: Deduplicación por trade_id ───

    def test_invariant_dedup_same_trade_id_not_double_counted(self):
        """
        Invariante 2 — Deduplicación por trade_id:
        El mismo trade_id llegado por WS fill_fetcher y luego por REST
        reconcile no se aplica dos veces al ledger.
        """
        _save_open_intent(
            self.idempotency, "dedup-001", "cdedup-001", exchange_order_id="ex-dedup-001"
        )
        fill = [
            {
                "trade_id": "t-cert-dedup",
                "side": "BUY",
                "size": "0.1",
                "price": "50000",
                "commission": "0",
                "trade_time": "2024-01-01T00:00:00Z",
            }
        ]
        oms = self._make_oms(fill_fetcher=MagicMock(return_value=fill))

        # Primera vía: WS user channel
        oms.handle_user_event(
            "update",
            [
                {
                    "order_id": "ex-dedup-001",
                    "client_order_id": "cdedup-001",
                    "product_id": "BTC-USD",
                    "status": "FILLED",
                    "number_of_fills": "1",
                }
            ],
        )
        qty_after_ws = self.ledger.position_qty

        # Segunda vía: REST reconcile con mismo fill
        oms.fill_fetcher.return_value = fill  # mismo fill
        oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "1"})

        # Sin doble-cuenta (ledger deduplicó por trade_id)
        assert self.ledger.position_qty == qty_after_ws

    # ─── Invariante 3: CANCEL_QUEUED ───

    def test_invariant_cancel_queued_survives_ws_and_rest_paths(self):
        """
        Invariante 3 — CANCEL_QUEUED:
        CANCEL_QUEUED es estado activo (aparece en get_pending_or_open),
        y ambos paths (WS y REST) lo transicionan correctamente a CANCELLED.
        """
        # Crear orden en CANCEL_QUEUED
        _save_open_intent(
            self.idempotency,
            "cq-001",
            "ccq-001",
            state=OrderState.CANCEL_QUEUED,
            exchange_order_id="ex-cq-001",
        )

        # Verificar que está activo
        active_ids = [r.intent_id for r in self.idempotency.get_pending_or_open()]
        assert "cq-001" in active_ids, "CANCEL_QUEUED debe ser estado activo"

        # Path WS: CANCELLED via user channel
        oms = self._make_oms()
        oms.handle_user_event(
            "update",
            [
                {
                    "order_id": "ex-cq-001",
                    "client_order_id": "ccq-001",
                    "product_id": "BTC-USD",
                    "status": "CANCELLED",
                    "number_of_fills": "0",
                }
            ],
        )

        record = self.idempotency.get_by_intent_id("cq-001")
        assert record.state == OrderState.CANCELLED
        active_ids_after = [r.intent_id for r in self.idempotency.get_pending_or_open()]
        assert "cq-001" not in active_ids_after

    def test_invariant_cancel_queued_resolved_by_rest_reconcile(self):
        """
        Invariante 3 — CANCEL_QUEUED:
        CANCEL_QUEUED resuelto por REST reconcile cuando WS no entregó confirmación.
        """
        _save_open_intent(
            self.idempotency,
            "cq-002",
            "ccq-002",
            state=OrderState.CANCEL_QUEUED,
            exchange_order_id="ex-cq-002",
        )
        oms = self._make_oms()

        oms.reconcile_open_orders(lambda oid: {"status": "CANCELLED", "number_of_fills": "0"})

        record = self.idempotency.get_by_intent_id("cq-002")
        assert record.state == OrderState.CANCELLED

    # ─── Invariante 4: Reconcile consistente con fills ───

    def test_invariant_reconcile_fills_consistent_ws_path(self):
        """
        Invariante 4 — Reconcile consistente:
        Fill aplicado via WS actualiza ledger consistentemente con el estado OMS.
        """
        _save_open_intent(self.idempotency, "rc-001", "crc-001", exchange_order_id="ex-rc-001")
        fill_fetcher = MagicMock(
            return_value=[
                {
                    "trade_id": "t-rc-ws-001",
                    "side": "BUY",
                    "size": "0.05",
                    "price": "50000",
                    "commission": "2.5",
                    "trade_time": "2024-01-01T00:00:00Z",
                }
            ]
        )
        oms = self._make_oms(fill_fetcher=fill_fetcher)

        oms.handle_user_event(
            "update",
            [
                {
                    "order_id": "ex-rc-001",
                    "client_order_id": "crc-001",
                    "product_id": "BTC-USD",
                    "status": "FILLED",
                    "number_of_fills": "1",
                }
            ],
        )

        record = self.idempotency.get_by_intent_id("rc-001")
        assert record.state == OrderState.FILLED
        assert self.ledger.position_qty == Decimal("0.05")

    def test_invariant_reconcile_fills_consistent_rest_path(self):
        """
        Invariante 4 — Reconcile consistente:
        Fill aplicado via REST reconcile es consistente: estado FILLED + fill en ledger.
        """
        _save_open_intent(self.idempotency, "rc-002", "crc-002", exchange_order_id="ex-rc-002")
        fill_fetcher = MagicMock(
            return_value=[
                {
                    "trade_id": "t-rc-rest-001",
                    "side": "BUY",
                    "size": "0.08",
                    "price": "50000",
                    "commission": "0",
                    "trade_time": "2024-01-01T00:00:00Z",
                }
            ]
        )
        oms = self._make_oms(fill_fetcher=fill_fetcher)

        oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "1"})

        record = self.idempotency.get_by_intent_id("rc-002")
        assert record.state == OrderState.FILLED
        assert self.ledger.position_qty == Decimal("0.08")

    def test_invariant_reconcile_multiple_partial_fills(self):
        """
        Invariante 4 — Reconcile consistente con fills parciales:
        N fills parciales del mismo order_id → posición = suma de amounts.
        """
        _save_open_intent(self.idempotency, "rc-003", "crc-003", exchange_order_id="ex-rc-003")
        partial_fills = [
            {
                "trade_id": f"t-partial-{i}",
                "side": "BUY",
                "size": "0.03",
                "price": "50000",
                "commission": "0",
                "trade_time": f"2024-01-01T00:0{i}:00Z",
            }
            for i in range(4)
        ]
        fill_fetcher = MagicMock(return_value=partial_fills)
        oms = self._make_oms(fill_fetcher=fill_fetcher)

        oms.reconcile_open_orders(lambda oid: {"status": "FILLED", "number_of_fills": "4"})

        assert self.ledger.position_qty == Decimal("0.12")  # 4 × 0.03


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
