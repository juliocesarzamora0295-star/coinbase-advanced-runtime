"""Tests para sistema de idempotencia."""

import os
import shutil
import tempfile
from decimal import Decimal

import pytest

from src.execution.idempotency import (
    IdempotencyStore,
    OrderIntent,
    OrderState,
)


@pytest.fixture
def temp_db():
    """Crear base de datos temporal para tests.

    Usa mkdtemp + shutil.rmtree(ignore_errors=True) en lugar de
    TemporaryDirectory para evitar el fallo en Windows donde sqlite3
    mantiene el file handle abierto hasta GC, impidiendo el borrado
    por TemporaryDirectory.__exit__.
    """
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_idempotency.db")
    yield db_path
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestIdempotencyStore:
    def test_save_and_get_intent(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        intent = OrderIntent(
            intent_id="test-intent-1",
            client_order_id="test-intent-1",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
        )

        store.save_intent(intent, OrderState.NEW)

        record = store.get_by_intent_id("test-intent-1")
        assert record is not None
        assert record.state == OrderState.NEW
        assert record.client_order_id == "test-intent-1"

    def test_update_state(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        intent = OrderIntent(
            intent_id="test-intent-2",
            client_order_id="test-intent-2",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
        )

        store.save_intent(intent, OrderState.NEW)
        store.update_state("test-intent-2", OrderState.FILLED, "order-123")

        record = store.get_by_intent_id("test-intent-2")
        assert record.state == OrderState.FILLED
        assert record.exchange_order_id == "order-123"

    def test_get_pending_or_open(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        # Crear intents en diferentes estados
        for i, state in enumerate(
            [
                OrderState.NEW,
                OrderState.OPEN_RESTING,
                OrderState.FILLED,
                OrderState.CANCELLED,
            ]
        ):
            intent = OrderIntent(
                intent_id=f"intent-{i}",
                client_order_id=f"intent-{i}",
                product_id="BTC-USD",
                side="BUY",
                order_type="LIMIT",
                qty=Decimal("0.001"),
                price=Decimal("50000"),
                stop_price=None,
                post_only=True,
                created_ts_ms=1234567890,
            )
            store.save_intent(intent, state)

        pending = store.get_pending_or_open()
        assert len(pending) == 2  # NEW y OPEN_RESTING

    def test_reconcile_states_in_enum(self, temp_db):
        """Los tres estados de reconciliación existen en el enum."""
        assert hasattr(OrderState, "RECONCILE_PENDING")
        assert hasattr(OrderState, "RECONCILE_CONFLICT")
        assert hasattr(OrderState, "RECONCILE_RESOLVED")

    def test_reconcile_conflict_not_terminal(self, temp_db):
        """RECONCILE_CONFLICT no es terminal: ledger no convergió."""
        store = IdempotencyStore(db_path=temp_db)
        intent = OrderIntent(
            intent_id="i1",
            client_order_id="i1",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.01"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1000,
        )
        store.save_intent(intent, OrderState.RECONCILE_CONFLICT)
        record = store.get_by_intent_id("i1")
        assert record.is_terminal is False
        assert record.is_ledger_conflict is True

    def test_reconcile_pending_not_terminal_but_active(self, temp_db):
        """RECONCILE_PENDING no es terminal y está activo (esperando fills)."""
        store = IdempotencyStore(db_path=temp_db)
        intent = OrderIntent(
            intent_id="i2",
            client_order_id="i2",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.01"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1000,
        )
        store.save_intent(intent, OrderState.RECONCILE_PENDING)
        record = store.get_by_intent_id("i2")
        assert record.is_terminal is False
        assert record.is_active is True
        assert record.is_ledger_conflict is False

    def test_reconcile_resolved_is_terminal(self, temp_db):
        """RECONCILE_RESOLVED es terminal: todos los fills confirmados."""
        store = IdempotencyStore(db_path=temp_db)
        intent = OrderIntent(
            intent_id="i3",
            client_order_id="i3",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.01"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1000,
        )
        store.save_intent(intent, OrderState.RECONCILE_RESOLVED)
        record = store.get_by_intent_id("i3")
        assert record.is_terminal is True
        assert record.is_active is False
        assert record.is_ledger_conflict is False

    def test_get_pending_or_open_includes_reconcile_pending(self, temp_db):
        """RECONCILE_PENDING aparece en get_pending_or_open (ledger pendiente)."""
        store = IdempotencyStore(db_path=temp_db)
        for i, state in enumerate([
            OrderState.RECONCILE_PENDING,
            OrderState.RECONCILE_CONFLICT,  # no activo
            OrderState.RECONCILE_RESOLVED,  # terminal
        ]):
            intent = OrderIntent(
                intent_id=f"ir-{i}",
                client_order_id=f"ir-{i}",
                product_id="BTC-USD",
                side="BUY",
                order_type="LIMIT",
                qty=Decimal("0.01"),
                price=Decimal("50000"),
                stop_price=None,
                post_only=True,
                created_ts_ms=1000 + i,
            )
            store.save_intent(intent, state)
        pending = store.get_pending_or_open()
        states = {r.state for r in pending}
        assert OrderState.RECONCILE_PENDING in states
        assert OrderState.RECONCILE_CONFLICT not in states
        assert OrderState.RECONCILE_RESOLVED not in states

    def test_cleanup_old(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        # Crear intent completado
        intent = OrderIntent(
            intent_id="old-intent",
            client_order_id="old-intent",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=0,  # Muy viejo
        )
        store.save_intent(intent, OrderState.FILLED)

        # Forzar updated_ts_ms viejo modificando directamente
        import sqlite3

        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "UPDATE order_intents SET updated_ts_ms = 0 WHERE intent_id = ?", ("old-intent",)
            )
            conn.commit()

        # Cleanup debería eliminarlo
        deleted = store.cleanup_old()
        assert deleted == 1

        record = store.get_by_intent_id("old-intent")
        assert record is None
