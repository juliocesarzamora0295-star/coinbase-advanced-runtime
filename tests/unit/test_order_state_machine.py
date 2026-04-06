"""
Tests de máquina de estados de órdenes — contra IdempotencyStore real.

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
from decimal import Decimal

import pytest

from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent


def make_intent(
    symbol: str = "BTC-USD",
    side: str = "BUY",
    order_type: str = "MARKET",
) -> OrderIntent:
    """Factory para OrderIntent de prueba."""
    return OrderIntent(
        client_order_id=str(uuid.uuid4()),
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol=symbol,
        side=side,
        final_qty=Decimal("0.001"),
        order_type=order_type,
        price=Decimal("50000") if order_type == "LIMIT" else None,
        reduce_only=False,
        post_only=False,
        viable=True,
        planner_version="test",
    )


@pytest.fixture
def store(tmp_path):
    """IdempotencyStore con SQLite temporal."""
    db_path = str(tmp_path / "test_idempotency.db")
    return IdempotencyStore(db_path=db_path)


class TestMarketOrderTransitions:
    """Transiciones para MARKET orders."""

    def test_market_order_initial_state_is_new(self, store):
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record is not None
        assert record.state == OrderState.NEW

    def test_market_order_after_ack_is_open_pending(self, store):
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            client_order_id=intent.client_order_id,
            state=OrderState.OPEN_PENDING,
            exchange_order_id="exchange-id-001",
        )

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.OPEN_PENDING
        assert record.exchange_order_id == "exchange-id-001"
        assert not record.is_terminal

    def test_market_order_not_filled_directly_on_ack(self, store):
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_PENDING)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state != OrderState.FILLED

    def test_market_order_fills_via_user_event(self, store):
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_PENDING)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.FILLED)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.FILLED
        assert record.is_terminal

    def test_market_order_cancelled_flow(self, store):
        intent = make_intent(order_type="MARKET")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_PENDING)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCEL_QUEUED)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active

        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCELLED)
        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.CANCELLED
        assert record.is_terminal


class TestLimitOrderTransitions:

    def test_limit_order_after_ack_is_open_resting(self, store):
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            client_order_id=intent.client_order_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="exchange-limit-001",
        )

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.OPEN_RESTING
        assert not record.is_terminal

    def test_limit_order_fills(self, store):
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_RESTING)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.FILLED)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.FILLED
        assert record.is_terminal

    def test_limit_order_cancel_flow(self, store):
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_RESTING)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCEL_QUEUED)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active

        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCELLED)
        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.CANCELLED
        assert record.is_terminal

    def test_limit_order_expired(self, store):
        intent = make_intent(order_type="LIMIT")
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_RESTING)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.EXPIRED)

        record = store.get_by_client_order_id(intent.client_order_id)
        assert record.state == OrderState.EXPIRED
        assert record.is_terminal


class TestActiveStateInvariants:

    def test_new_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        assert store.get_by_client_order_id(intent.client_order_id).is_active

    def test_open_pending_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_PENDING)
        assert store.get_by_client_order_id(intent.client_order_id).is_active

    def test_open_resting_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.OPEN_RESTING)
        assert store.get_by_client_order_id(intent.client_order_id).is_active

    def test_cancel_queued_is_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCEL_QUEUED)
        assert store.get_by_client_order_id(intent.client_order_id).is_active

    def test_filled_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.FILLED)
        record = store.get_by_client_order_id(intent.client_order_id)
        assert not record.is_active
        assert record.is_terminal

    def test_cancelled_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCELLED)
        record = store.get_by_client_order_id(intent.client_order_id)
        assert not record.is_active
        assert record.is_terminal

    def test_failed_is_terminal_not_active(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.FAILED)
        record = store.get_by_client_order_id(intent.client_order_id)
        assert not record.is_active
        assert record.is_terminal

    def test_cancel_queued_in_get_pending_or_open(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCEL_QUEUED)

        active_ids = [r.client_order_id for r in store.get_pending_or_open()]
        assert intent.client_order_id in active_ids

    def test_filled_not_in_get_pending_or_open(self, store):
        intent = make_intent()
        store.save_intent(intent, OrderState.NEW)
        store.update_state(client_order_id=intent.client_order_id, state=OrderState.FILLED)

        active_ids = [r.client_order_id for r in store.get_pending_or_open()]
        assert intent.client_order_id not in active_ids

    def test_all_records_in_get_pending_or_open_are_active(self, store):
        states = [
            OrderState.NEW,
            OrderState.OPEN_RESTING,
            OrderState.OPEN_PENDING,
            OrderState.CANCEL_QUEUED,
        ]
        for state in states:
            intent = make_intent()
            store.save_intent(intent, state)

        terminal = make_intent()
        store.save_intent(terminal, OrderState.FILLED)

        active = store.get_pending_or_open()
        for record in active:
            assert record.is_active


class TestRestartRecovery:

    def test_state_persists_after_restart(self, tmp_path):
        db_path = str(tmp_path / "restart.db")

        store1 = IdempotencyStore(db_path=db_path)
        intent = make_intent(order_type="LIMIT")
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(
            client_order_id=intent.client_order_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="exchange-persisted-001",
        )

        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_client_order_id(intent.client_order_id)

        assert record is not None
        assert record.state == OrderState.OPEN_RESTING
        assert record.exchange_order_id == "exchange-persisted-001"
        assert record.is_active

    def test_open_orders_recovered_on_restart(self, tmp_path):
        db_path = str(tmp_path / "restart_open.db")

        store1 = IdempotencyStore(db_path=db_path)
        open_ids = []
        for _ in range(3):
            intent = make_intent()
            store1.save_intent(intent, OrderState.OPEN_RESTING)
            open_ids.append(intent.client_order_id)

        terminal = make_intent()
        store1.save_intent(terminal, OrderState.FILLED)

        store2 = IdempotencyStore(db_path=db_path)
        active_ids = [r.client_order_id for r in store2.get_pending_or_open()]

        for cid in open_ids:
            assert cid in active_ids
        assert terminal.client_order_id not in active_ids

    def test_cancel_queued_recovered_on_restart(self, tmp_path):
        db_path = str(tmp_path / "restart_cancel_queued.db")

        store1 = IdempotencyStore(db_path=db_path)
        intent = make_intent()
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(client_order_id=intent.client_order_id, state=OrderState.CANCEL_QUEUED)

        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_client_order_id(intent.client_order_id)

        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active
        active_ids = [r.client_order_id for r in store2.get_pending_or_open()]
        assert intent.client_order_id in active_ids
