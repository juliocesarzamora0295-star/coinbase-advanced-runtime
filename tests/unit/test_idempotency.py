"""Tests para sistema de idempotencia."""

import os
import shutil
import tempfile
from decimal import Decimal

import pytest

from src.execution.idempotency import (
    IdempotencyStore,
    OrderState,
)
from src.execution.order_planner import OrderIntent


def _make_intent(client_order_id: str, **kwargs) -> OrderIntent:
    defaults = dict(
        client_order_id=client_order_id,
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol="BTC-USD",
        side="BUY",
        final_qty=Decimal("0.001"),
        order_type="LIMIT",
        price=Decimal("50000"),
        reduce_only=False,
        post_only=True,
        viable=True,
        planner_version="test",
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


@pytest.fixture
def temp_db():
    """Crear base de datos temporal para tests."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_idempotency.db")
    yield db_path
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestIdempotencyStore:
    def test_save_and_get_intent(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)
        intent = _make_intent("test-intent-1")

        store.save_intent(intent, OrderState.NEW)

        record = store.get_by_client_order_id("test-intent-1")
        assert record is not None
        assert record.state == OrderState.NEW
        assert record.client_order_id == "test-intent-1"

    def test_update_state(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)
        intent = _make_intent("test-intent-2")

        store.save_intent(intent, OrderState.NEW)
        store.update_state("test-intent-2", OrderState.FILLED, "order-123")

        record = store.get_by_client_order_id("test-intent-2")
        assert record.state == OrderState.FILLED
        assert record.exchange_order_id == "order-123"

    def test_get_pending_or_open(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)

        for i, state in enumerate(
            [
                OrderState.NEW,
                OrderState.OPEN_RESTING,
                OrderState.FILLED,
                OrderState.CANCELLED,
            ]
        ):
            intent = _make_intent(f"intent-{i}")
            store.save_intent(intent, state)

        pending = store.get_pending_or_open()
        assert len(pending) == 2  # NEW y OPEN_RESTING

    def test_cleanup_old(self, temp_db):
        store = IdempotencyStore(db_path=temp_db)
        intent = _make_intent("old-intent")
        store.save_intent(intent, OrderState.FILLED)

        # Forzar updated_ts_ms viejo
        import sqlite3

        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "UPDATE order_intents SET updated_ts_ms = 0 WHERE client_order_id = ?",
                ("old-intent",),
            )
            conn.commit()

        deleted = store.cleanup_old()
        assert deleted == 1

        record = store.get_by_client_order_id("old-intent")
        assert record is None

    def test_save_intent_idempotent(self, temp_db):
        """save_intent retorna False si el intent ya existe."""
        store = IdempotencyStore(db_path=temp_db)
        intent = _make_intent("dedup-intent")

        assert store.save_intent(intent, OrderState.NEW) is True
        assert store.save_intent(intent, OrderState.NEW) is False
