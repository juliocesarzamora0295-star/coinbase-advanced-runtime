"""
Tests de máquina de estados de órdenes — contra IdempotencyStore real.

Reescritura completa. Todos los tests ejercen código real de idempotency.py.

Invariantes testeadas:
- NEW → OPEN_PENDING (market) — nunca directo a FILLED en ACK
- NEW → OPEN_RESTING (limit)
- OPEN_PENDING → FILLED via user event
- OPEN_RESTING → CANCEL_QUEUED → CANCELLED
- CANCEL_QUEUED aparece en get_pending_or_open()
- is_active() coherente con get_pending_or_open()
- Estados terminales: is_terminal=True, is_active=False
- Restart recovery: nuevo store con mismo DB → mismo estado
- exchange_order_id enlazado correctamente
"""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from src.execution.idempotency import (
    IdempotencyStore,
    OrderState,
    StoredIntent,
)


def make_intent(
    product_id: str = "BTC-USD",
    side: str = "BUY",
    order_type: str = "MARKET",
) -> StoredIntent:
    """Factory para StoredIntent de prueba."""
    return StoredIntent(
        intent_id=str(uuid.uuid4()),
        client_order_id=str(uuid.uuid4()),
        product_id=product_id,
        side=side,
        order_type=order_type,
        qty=Decimal("0.001"),
        price=Decimal("50000") if order_type == "LIMIT" else None,
        stop_price=None,
        post_only=False,
        created_ts_ms=int(datetime.now().timestamp() * 1000),
    )


@pytest.fixture
def store(tmp_path):
    """IdempotencyStore con SQLite temporal."""
    db_path = str(tmp_path / "test_idempotency.db")
    return IdempotencyStore(db_path=db_path)


class TestMarketOrderTransitions:
    """Transiciones para MARKET orders."""

    def test_market_order_initial_state_is_new(self, store):
        """save_intent con state=NEW → estado es NEW."""
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)

        record = store.get_by_intent_id(intent.intent_id)
        assert record is not None
        assert record.state == OrderState.NEW

    def test_market_order_after_ack_is_open_pending(self, store):
        """
        MARKET order tras ACK de exchange → OPEN_PENDING.

        P1 FIX: El ACK confirma recepción, no ejecución.
        No debe ir directamente a FILLED.
        """
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            intent_id=intent.intent_id,
            state=OrderState.OPEN_PENDING,
            exchange_order_id="exchange-id-001",
        )

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.OPEN_PENDING
        assert record.exchange_order_id == "exchange-id-001"
        assert not record.is_terminal

    def test_market_order_not_filled_directly_on_ack(self, store):
        """
        MARKET order tras ACK nunca está en FILLED.

        P1 FIX: Solo user channel o REST reconcile puede mover a FILLED.
        """
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_PENDING)

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state != OrderState.FILLED

    def test_market_order_fills_via_user_event(self, store):
        """OPEN_PENDING → FILLED via user channel event."""
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_PENDING)
        store.update_state(intent_id=intent.intent_id, state=OrderState.FILLED)

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.FILLED
        assert record.is_terminal

    def test_market_order_cancelled_flow(self, store):
        """OPEN_PENDING → CANCEL_QUEUED → CANCELLED."""
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_PENDING)
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCEL_QUEUED)

        # Verificar estado intermedio: activo pero en proceso de cancelación
        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active

        # User channel confirma cancelación
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCELLED)
        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.CANCELLED
        assert record.is_terminal


class TestLimitOrderTransitions:
    """Transiciones para LIMIT orders."""

    def test_limit_order_after_ack_is_open_resting(self, store):
        """LIMIT order tras ACK → OPEN_RESTING."""
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            intent_id=intent.intent_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="exchange-limit-001",
        )

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.OPEN_RESTING
        assert not record.is_terminal

    def test_limit_order_fills(self, store):
        """OPEN_RESTING → FILLED via user channel."""
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_RESTING)
        store.update_state(intent_id=intent.intent_id, state=OrderState.FILLED)

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.FILLED
        assert record.is_terminal

    def test_limit_order_cancel_flow(self, store):
        """OPEN_RESTING → CANCEL_QUEUED → CANCELLED."""
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_RESTING)
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCEL_QUEUED)

        # Intermedio: activo, en cola de cancelación
        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active

        # Confirmación via user channel
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCELLED)
        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.CANCELLED
        assert record.is_terminal

    def test_limit_order_expired(self, store):
        """OPEN_RESTING → EXPIRED (time-in-force expirado)."""
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_RESTING)
        store.update_state(intent_id=intent.intent_id, state=OrderState.EXPIRED)

        record = store.get_by_intent_id(intent.intent_id)
        assert record.state == OrderState.EXPIRED
        assert record.is_terminal


class TestActiveStateInvariants:
    """Coherencia entre is_active y get_pending_or_open."""

    def test_new_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        assert store.get_by_intent_id(intent.intent_id).is_active

    def test_open_pending_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_PENDING)
        assert store.get_by_intent_id(intent.intent_id).is_active

    def test_open_resting_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.OPEN_RESTING)
        assert store.get_by_intent_id(intent.intent_id).is_active

    def test_cancel_queued_is_active(self, store):
        """CANCEL_QUEUED es activo — P0 fix: consistencia con get_pending_or_open."""
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCEL_QUEUED)
        assert store.get_by_intent_id(intent.intent_id).is_active

    def test_filled_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.FILLED)
        record = store.get_by_intent_id(intent.intent_id)
        assert not record.is_active
        assert record.is_terminal

    def test_cancelled_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCELLED)
        record = store.get_by_intent_id(intent.intent_id)
        assert not record.is_active
        assert record.is_terminal

    def test_failed_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.FAILED)
        record = store.get_by_intent_id(intent.intent_id)
        assert not record.is_active
        assert record.is_terminal

    def test_cancel_queued_in_get_pending_or_open(self, store):
        """CANCEL_QUEUED aparece en get_pending_or_open() — P0 fix."""
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.CANCEL_QUEUED)

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert intent.intent_id in active_ids

    def test_filled_not_in_get_pending_or_open(self, store):
        """FILLED no aparece en get_pending_or_open()."""
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(intent_id=intent.intent_id, state=OrderState.FILLED)

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert intent.intent_id not in active_ids

    def test_all_records_in_get_pending_or_open_are_active(self, store):
        """
        Todos los registros en get_pending_or_open() tienen is_active=True.
        Verifica coherencia total entre is_active y get_pending_or_open.
        """
        states = [
            OrderState.NEW,
            OrderState.OPEN_RESTING,
            OrderState.OPEN_PENDING,
            OrderState.CANCEL_QUEUED,
        ]
        for state in states:
            intent = make_intent()
            store.save_intent(intent, state)

        # Crear uno terminal (no debe aparecer)
        terminal = make_intent()
        store.save_intent(terminal, OrderState.FILLED)

        active = store.get_pending_or_open()
        for record in active:
            assert record.is_active, (
                f"Record state={record.state.name} en get_pending_or_open() "
                f"pero is_active=False"
            )


class TestRestartRecovery:
    """Restart recovery: nuevo store con mismo DB → mismo estado."""

    def test_state_persists_after_restart(self, tmp_path):
        """Estado persiste cuando se crea nuevo IdempotencyStore con el mismo DB."""
        db_path = str(tmp_path / "restart.db")

        store1 = IdempotencyStore(db_path=db_path)
        intent = make_intent(order_type="LIMIT")
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(
            intent_id=intent.intent_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="exchange-persisted-001",
        )

        # Simular restart: nueva instancia con mismo archivo
        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_intent_id(intent.intent_id)

        assert record is not None
        assert record.state == OrderState.OPEN_RESTING
        assert record.exchange_order_id == "exchange-persisted-001"
        assert record.is_active

    def test_open_orders_recovered_on_restart(self, tmp_path):
        """get_pending_or_open() tras restart devuelve órdenes abiertas pre-crash."""
        db_path = str(tmp_path / "restart_open.db")

        store1 = IdempotencyStore(db_path=db_path)
        open_ids = []
        for _ in range(3):
            intent = make_intent()
            store1.save_intent(intent, OrderState.OPEN_RESTING)
            open_ids.append(intent.intent_id)

        # Terminal no debe aparecer
        terminal = make_intent()
        store1.save_intent(terminal, OrderState.FILLED)

        store2 = IdempotencyStore(db_path=db_path)
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]

        for intent_id in open_ids:
            assert intent_id in active_ids, f"Intent {intent_id} no recuperado tras restart"
        assert terminal.intent_id not in active_ids

    def test_cancel_queued_recovered_on_restart(self, tmp_path):
        """CANCEL_QUEUED persiste y se recupera tras restart."""
        db_path = str(tmp_path / "restart_cancel_queued.db")

        store1 = IdempotencyStore(db_path=db_path)
        intent = make_intent()
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(intent_id=intent.intent_id, state=OrderState.CANCEL_QUEUED)

        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_intent_id(intent.intent_id)

        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active
        # Debe aparecer en get_pending_or_open()
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]
        assert intent.intent_id in active_ids
