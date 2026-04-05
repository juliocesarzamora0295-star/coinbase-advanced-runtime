"""Tests para sistema de idempotencia."""

import os
import shutil
import tempfile
from decimal import Decimal

import pytest

from src.execution.idempotency import (
    DuplicateIntentError,
    IdempotencyStore,
    OrderState,
    StoredIntent,
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

        intent = StoredIntent(
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

        intent = StoredIntent(
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
            intent = StoredIntent(
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

    def test_duplicate_intent_id_raises(self, temp_db):
        """Mismo intent_id → DuplicateIntentError. No sobrescritura silenciosa."""
        store = IdempotencyStore(db_path=temp_db)

        intent = StoredIntent(
            intent_id="dup-intent-id",
            client_order_id="coid-001",
            product_id="BTC-USD",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0.001"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=1234567890,
        )
        store.save_intent(intent, OrderState.NEW)

        # Segunda inserción con mismo intent_id debe fallar
        with pytest.raises(DuplicateIntentError, match="intent_id"):
            store.save_intent(intent, OrderState.NEW)

    def test_duplicate_client_order_id_raises(self, temp_db):
        """
        Mismo client_order_id con intent_id distinto → DuplicateIntentError.

        Invariante central: un client_order_id nunca puede mapear a dos intents.
        Un client_order_id duplicado indica el mismo signal enviado dos veces
        (bug en capa de planificación), no un retry legítimo.
        """
        store = IdempotencyStore(db_path=temp_db)

        intent_a = StoredIntent(
            intent_id="intent-a",
            client_order_id="shared-coid",
            product_id="BTC-USD",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0.001"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=1234567890,
        )
        intent_b = StoredIntent(
            intent_id="intent-b",  # distinto intent_id
            client_order_id="shared-coid",  # mismo client_order_id
            product_id="BTC-USD",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0.002"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=1234567891,
        )
        store.save_intent(intent_a, OrderState.NEW)

        with pytest.raises(DuplicateIntentError, match="client_order_id"):
            store.save_intent(intent_b, OrderState.NEW)

    def test_original_record_unchanged_after_collision(self, temp_db):
        """Tras colisión, el registro original permanece intacto."""
        store = IdempotencyStore(db_path=temp_db)

        intent_a = StoredIntent(
            intent_id="intent-orig",
            client_order_id="coid-orig",
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=111,
        )
        store.save_intent(intent_a, OrderState.NEW)

        # Intentar sobrescribir
        intent_dup = StoredIntent(
            intent_id="intent-orig",
            client_order_id="coid-orig",
            product_id="ETH-USD",  # distinto producto
            side="SELL",
            order_type="MARKET",
            qty=Decimal("9.999"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=999,
        )
        with pytest.raises(DuplicateIntentError):
            store.save_intent(intent_dup, OrderState.NEW)

        # El registro original no fue modificado
        record = store.get_by_intent_id("intent-orig")
        assert record is not None
        assert record.intent.product_id == "BTC-USD"
        assert record.intent.qty == Decimal("0.001")
        assert record.state == OrderState.NEW

    def test_update_state_after_save_works(self, temp_db):
        """update_state() sigue funcionando normalmente tras inserción estricta."""
        store = IdempotencyStore(db_path=temp_db)

        intent = StoredIntent(
            intent_id="intent-upd",
            client_order_id="coid-upd",
            product_id="BTC-USD",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0.001"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=1234567890,
        )
        store.save_intent(intent, OrderState.NEW)
        store.update_state("intent-upd", OrderState.OPEN_PENDING, exchange_order_id="ex-001")

        record = store.get_by_intent_id("intent-upd")
        assert record.state == OrderState.OPEN_PENDING
        assert record.exchange_order_id == "ex-001"

    def test_cleanup_old(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        # Crear intent completado
        intent = StoredIntent(
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
