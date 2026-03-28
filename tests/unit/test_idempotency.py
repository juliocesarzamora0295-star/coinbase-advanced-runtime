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
