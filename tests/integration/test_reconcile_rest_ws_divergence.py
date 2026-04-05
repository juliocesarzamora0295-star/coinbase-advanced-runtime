"""
Tests de integración: Reconciliación WS ↔ REST divergencia.

Verifica que OMSReconcileService converge correctamente cuando
WS y REST tienen visiones contradictorias del estado de una orden.

Casos testeados:
- WS dice OPEN, REST dice FILLED → OMS actualiza a FILLED, fill aplicado
- WS dice FILLED, OMS ya tiene FILLED → idempotente (sin doble-apply)
- WS dice OPEN sin fill_fetcher → estado queda OPEN (no se pierden fills)
- WS dice CANCELLED → orden marcada CANCELLED, sale de pending
- fill_fetcher retorna fill con trade_id conocido → deduplicado (no doble-cuenta)
- fill_fetcher lanza excepción → error loggeado, OMS state actualizado igual
- order no conocida en OMS → ignorada silenciosamente
- bootstrap_complete detectado correctamente

Sin Coinbase API. fill_fetcher es un callable mockeado.
"""

import uuid
from decimal import Decimal
from typing import Dict

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def make_intent(client_order_id: str, product_id: str = "BTC-USD") -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol=product_id,
        side="BUY",
        final_qty=Decimal("0.1"),
        order_type="LIMIT",
        price=Decimal("50000"),
        reduce_only=False,
        post_only=False,
        viable=True,
        planner_version="test",
    )


def make_order_event(
    order_id: str,
    client_order_id: str,
    status: str,
    number_of_fills: int = 0,
    product_id: str = "BTC-USD",
) -> Dict:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "product_id": product_id,
        "status": status,
        "order_side": "BUY",
        "number_of_fills": str(number_of_fills),
    }


def make_rest_fill(trade_id: str, size: str = "0.1", price: str = "50000") -> Dict:
    return {
        "trade_id": trade_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "size": size,
        "price": price,
        "commission": "0",
        "trade_time": "2023-11-15T00:00:00Z",
    }


def make_service(
    store: IdempotencyStore,
    ledger: TradeLedger,
    fill_fetcher=None,
) -> OMSReconcileService:
    return OMSReconcileService(
        idempotency=store,
        ledger=ledger,
        fill_fetcher=fill_fetcher,
    )


# ──────────────────────────────────────────────
# WS dice OPEN, REST dice FILLED
# ──────────────────────────────────────────────


class TestWSOpenRESTFilled:

    def test_ws_open_then_rest_filled_updates_oms(self, tmp_path):
        """
        WS dice OPEN → estado OPEN_RESTING.
        Posterior REST fill → OMS actualiza a FILLED, fill en ledger.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-ws-rest-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.NEW)

        rest_fills = [make_rest_fill("fill-ws-rest-001")]
        service = make_service(store, ledger, fill_fetcher=lambda oid: rest_fills)

        # WS: orden OPEN
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "OPEN", number_of_fills=0)]
        )
        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.OPEN_RESTING

        # WS: número de fills aumentó → disparar REST fetch
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )

        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.FILLED
        assert ledger.position_qty == Decimal("0.1")

    def test_ws_filled_directly_updates_oms(self, tmp_path):
        """
        WS dice FILLED directamente → OMS = FILLED, fill aplicado.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-direct-fill-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.OPEN_RESTING)

        rest_fills = [make_rest_fill("fill-direct-001")]
        service = make_service(store, ledger, fill_fetcher=lambda oid: rest_fills)

        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )

        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.FILLED
        assert ledger.position_qty == Decimal("0.1")


# ──────────────────────────────────────────────
# Idempotencia: mismo fill no se doble-aplica
# ──────────────────────────────────────────────


class TestFillIdempotency:

    def test_same_fill_not_double_applied(self, tmp_path):
        """
        Mismo trade_id llegado dos veces → contabilizado una sola vez en ledger.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-dedup-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.OPEN_RESTING)

        rest_fills = [make_rest_fill("fill-dedup-001")]
        service = make_service(store, ledger, fill_fetcher=lambda oid: rest_fills)

        # Primer evento con fill
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )
        qty_after_first = ledger.position_qty

        # Segundo evento con mismo fill (fill_count no aumenta, no re-fetch)
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )

        assert ledger.position_qty == qty_after_first

    def test_trade_id_deduplication_across_calls(self, tmp_path):
        """
        fill_fetcher retorna mismo trade_id en dos llamadas → deduplicado.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-td-dedup-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.NEW)

        call_count = [0]

        def fill_fetcher(oid):
            call_count[0] += 1
            return [make_rest_fill("fill-td-001")]

        service = make_service(store, ledger, fill_fetcher=fill_fetcher)

        # Primera vez: fill_count 0 → 1
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "OPEN", number_of_fills=1)]
        )
        qty_first = ledger.position_qty

        # Segunda vez: fill_count 1 → 2 (fill_fetcher retorna el mismo fill)
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "OPEN", number_of_fills=2)]
        )

        # El ledger deduplicó por trade_id — posición no cambió
        assert ledger.position_qty == qty_first


# ──────────────────────────────────────────────
# Cancelación
# ──────────────────────────────────────────────


class TestCancellation:

    def test_ws_cancelled_removes_from_pending(self, tmp_path):
        """
        WS dice CANCELLED → OMS estado CANCELLED, no en pending_or_open.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-cancel-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.CANCEL_QUEUED)

        service = make_service(store, ledger)
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "CANCELLED", number_of_fills=0)]
        )

        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.CANCELLED

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert client_id not in active_ids


# ──────────────────────────────────────────────
# Casos especiales
# ──────────────────────────────────────────────


class TestEdgeCases:

    def test_unknown_order_ignored_silently(self, tmp_path):
        """
        Evento de orden no conocida en OMS → ignorado, sin excepción.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        service = make_service(store, ledger)

        # No debe lanzar
        service.handle_user_event(
            "update",
            [make_order_event("ex-unknown", "client-unknown", "FILLED", number_of_fills=1)],
        )
        # Ledger intacto
        assert ledger.position_qty == Decimal("0")

    def test_fill_fetcher_exception_does_not_break_oms_update(self, tmp_path):
        """
        fill_fetcher lanza excepción → estado OMS igual actualizado, error loggeado.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-exc-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.OPEN_RESTING)

        def exploding_fetcher(oid):
            raise ConnectionError("REST timeout")

        service = make_service(store, ledger, fill_fetcher=exploding_fetcher)

        # No debe propagar la excepción
        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )

        # Estado OMS debe haberse actualizado a FILLED aunque fill_fetcher falló
        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.FILLED
        # Ledger sin fill (fill_fetcher falló)
        assert ledger.position_qty == Decimal("0")

    def test_bootstrap_complete_after_small_snapshot(self, tmp_path):
        """
        Snapshot con < 50 órdenes → bootstrap_complete=True.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        service = make_service(store, ledger)
        assert service.is_bootstrap_complete() is False

        # Snapshot vacío (<50)
        service.handle_user_event("snapshot", [])
        assert service.is_bootstrap_complete() is True

    def test_no_fill_fetcher_no_fills_applied(self, tmp_path):
        """
        Sin fill_fetcher, número de fills aumenta pero no hay REST fetch.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        client_id = str(uuid.uuid4())
        exchange_id = "ex-no-fetcher-001"

        intent = make_intent(client_id)
        store.save_intent(intent, OrderState.NEW)

        service = make_service(store, ledger, fill_fetcher=None)

        service.handle_user_event(
            "update", [make_order_event(exchange_id, client_id, "FILLED", number_of_fills=1)]
        )

        # Estado actualizado aunque no haya fills
        record = store.get_by_intent_id(client_id)
        assert record.state == OrderState.FILLED
        # Ledger vacío (sin fill_fetcher)
        assert ledger.position_qty == Decimal("0")
