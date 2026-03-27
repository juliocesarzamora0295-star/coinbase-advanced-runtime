"""
Tests de idempotencia de OrderExecutor.

Invariantes testeadas:
- LIMIT order creada exitosamente → estado OPEN_RESTING (no FILLED)
- MARKET order tras ACK → estado OPEN_PENDING (no FILLED) — P1 FIX
- exchange_order_id y client_order_id enlazados correctamente
- error en exchange → estado FAILED, intent persistido
- cancel de orden activa → succeeds
- cancel de orden inexistente → False
- cancel de orden terminal (FILLED) → False, estado no cambia
- cancel transiciona a CANCEL_QUEUED (no directamente a CANCELLED)
- orders_last_minute tracking funciona
"""
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.coinbase_exchange import CoinbaseAPIError
from src.core.quantization import Quantizer, ProductInfo
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.orders import OrderExecutor


def make_store(tmp_path, name: str = "orders") -> IdempotencyStore:
    db_path = str(tmp_path / f"{name}.db")
    return IdempotencyStore(db_path=db_path)


def make_quantizer() -> Quantizer:
    return Quantizer(ProductInfo(
        product_id="BTC-USD",
        base_increment=Decimal("0.00000001"),
        quote_increment=Decimal("0.01"),
        min_market_funds=Decimal("1"),
        base_currency="BTC",
        quote_currency="USD",
    ))


def make_executor(store: IdempotencyStore, mock_client: MagicMock) -> OrderExecutor:
    return OrderExecutor(
        client=mock_client,
        idempotency=store,
        quantizer=make_quantizer(),
    )


class TestLimitOrderCreation:

    def test_limit_order_created_with_open_resting(self, tmp_path):
        """Limit order creada exitosamente → estado OPEN_RESTING."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-limit-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order(
            product_id="BTC-USD",
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        assert result.success is True
        assert result.state == OrderState.OPEN_RESTING
        assert result.exchange_order_id == "ex-limit-001"

    def test_limit_order_exchange_and_client_ids_linked(self, tmp_path):
        """exchange_order_id y client_order_id enlazados correctamente en store."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-linked-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        record = store.get_by_client_order_id(result.client_order_id)
        assert record is not None
        assert record.exchange_order_id == "ex-linked-001"
        assert record.client_order_id == result.client_order_id

    def test_limit_order_intent_persisted_on_success(self, tmp_path):
        """Intent persiste en store tras creación exitosa."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-persist-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        record = store.get_by_intent_id(result.intent_id)
        assert record is not None
        assert record.state == OrderState.OPEN_RESTING

    def test_limit_order_exchange_error_sets_failed(self, tmp_path):
        """Error en exchange → estado FAILED, intent guardado en store."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("rejected by exchange")

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        assert result.success is False
        assert result.state == OrderState.FAILED

        record = store.get_by_intent_id(result.intent_id)
        assert record is not None
        assert record.state == OrderState.FAILED

    def test_limit_order_failed_not_in_pending_or_open(self, tmp_path):
        """Intent FAILED no aparece en get_pending_or_open()."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("error")

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert result.intent_id not in active_ids


class TestMarketOrderCreation:

    def test_market_order_after_ack_is_open_pending(self, tmp_path):
        """
        MARKET order tras ACK → OPEN_PENDING, NO FILLED.

        P1 FIX: ACK confirma recepción, no ejecución.
        Solo user channel o REST reconcile puede mover a FILLED.
        """
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-001"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "BUY", qty=Decimal("0.001"))

        assert result.success is True
        assert result.state == OrderState.OPEN_PENDING
        assert result.state != OrderState.FILLED

    def test_market_order_state_not_filled_in_store(self, tmp_path):
        """MARKET order en store tras ACK: OPEN_PENDING, no FILLED."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-002"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "SELL", qty=Decimal("0.001"))

        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.OPEN_PENDING
        assert record.state != OrderState.FILLED

    def test_market_order_exchange_id_linked_in_store(self, tmp_path):
        """exchange_order_id enlazado en store para market orders."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-link"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "BUY", qty=Decimal("0.001"))

        record = store.get_by_client_order_id(result.client_order_id)
        assert record is not None
        assert record.exchange_order_id == "ex-market-link"


class TestCancelOrder:

    def test_cancel_active_limit_order_succeeds(self, tmp_path):
        """Cancelar orden OPEN_RESTING activa → retorna True."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-cancel-001"}
        client.cancel_orders.return_value = [{"success": True}]

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is True

    def test_cancel_transitions_to_cancel_queued(self, tmp_path):
        """
        cancel_order → estado CANCEL_QUEUED (no directamente CANCELLED).

        El estado final CANCELLED solo llega via user channel o reconcile.
        CANCEL_QUEUED indica que la solicitud fue aceptada pero no confirmada aún.
        """
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-cq-001"}
        client.cancel_orders.return_value = [{"success": True}]

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        executor.cancel_order(result.intent_id)

        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.CANCEL_QUEUED, (
            f"cancel_order debe transicionar a CANCEL_QUEUED, got {record.state.name}. "
            "El estado final CANCELLED llega via user channel/reconcile."
        )

    def test_cancel_nonexistent_order_returns_false(self, tmp_path):
        """Cancelar intent inexistente → False."""
        store = make_store(tmp_path)
        client = MagicMock()
        executor = make_executor(store, client)

        result = executor.cancel_order("nonexistent-intent-id-xyz")
        assert result is False

    def test_cancel_already_filled_returns_false(self, tmp_path):
        """Cancelar orden en estado FILLED → False, estado no cambia."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-filled-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        # Simular user channel marcando como FILLED
        store.update_state(intent_id=result.intent_id, state=OrderState.FILLED)

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is False

        # Estado terminal no modificado
        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.FILLED

    def test_cancel_already_cancelled_returns_false(self, tmp_path):
        """Cancelar orden ya CANCELLED → False."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-already-cancelled"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))
        store.update_state(intent_id=result.intent_id, state=OrderState.CANCELLED)

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is False


class TestOrdersLastMinute:

    def test_orders_last_minute_zero_initially(self, tmp_path):
        """orders_last_minute es 0 al inicio."""
        store = make_store(tmp_path)
        client = MagicMock()
        executor = make_executor(store, client)

        assert executor.get_orders_last_minute() == 0

    def test_orders_last_minute_increments_after_limit_order(self, tmp_path):
        """Crear orden límite incrementa orders_last_minute."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-rate-001"}

        executor = make_executor(store, client)
        executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        assert executor.get_orders_last_minute() == 1

    def test_failed_order_does_not_count_in_rate(self, tmp_path):
        """Orden fallida (exchange error) no debe contar en rate limit."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("error")

        executor = make_executor(store, client)
        executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        # Orden fallida no incrementa el rate (no llegó al exchange)
        assert executor.get_orders_last_minute() == 0
