"""
Tests dirigidos: cobertura de paths de error en OrderExecutor.

Cubre mediante mocks de CoinbaseRESTClient:
- CoinbaseAPIError en submit_order (market) -> OrderResult(success=False, FAILED)
- CoinbaseAPIError persiste estado FAILED en OMS
- cancel_order cuando no hay exchange_order_id -> False
- CoinbaseAPIError en cancel_order -> False
- get_order_status() con intent existente y no existente
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.coinbase_exchange import CoinbaseAPIError
from src.core.quantization import ProductInfo, Quantizer
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.execution.orders import OrderExecutor

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def make_quantizer() -> Quantizer:
    return Quantizer(
        ProductInfo(
            product_id="BTC-USD",
            base_increment=Decimal("0.00000001"),
            quote_increment=Decimal("0.01"),
            min_market_funds=Decimal("1"),
            base_currency="BTC",
            quote_currency="USD",
        )
    )


def make_executor(tmp_path, client_mock) -> OrderExecutor:
    store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
    quantizer = make_quantizer()
    return OrderExecutor(
        client=client_mock,
        idempotency=store,
        quantizer=quantizer,
    )


def make_client_mock(order_id: str = "ex-mock-001") -> MagicMock:
    mock = MagicMock()
    mock.create_market_order.return_value = {"order_id": order_id}
    mock.create_limit_order_gtc.return_value = {"order_id": order_id}
    mock.cancel_orders.return_value = [{"order_id": order_id}]
    return mock


def make_test_intent(
    symbol="BTC-USD",
    side="BUY",
    order_type="MARKET",
    qty="0.001",
    price=None,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=str(uuid.uuid4()),
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol=symbol,
        side=side,
        final_qty=Decimal(qty),
        order_type=order_type,
        price=Decimal(price) if price else None,
        reduce_only=False,
        post_only=order_type == "LIMIT",
        viable=True,
        planner_version="test",
    )


# ──────────────────────────────────────────────
# submit_order (market): CoinbaseAPIError
# ──────────────────────────────────────────────


class TestSubmitMarketOrderAPIError:

    def test_api_error_returns_failed_result(self, tmp_path):
        """CoinbaseAPIError -> OrderResult(success=False, FAILED)."""
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("timeout")
        executor = make_executor(tmp_path, client)

        intent = make_test_intent(qty="0.01")
        result = executor.submit_order(intent)

        assert result.success is False
        assert result.state == OrderState.FAILED
        assert "timeout" in (result.error_message or "")

    def test_api_error_persists_failed_state_in_oms(self, tmp_path):
        """CoinbaseAPIError -> estado FAILED guardado en IdempotencyStore."""
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("rate_limit")
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        intent = make_test_intent(qty="0.01")
        result = executor.submit_order(intent)

        record = store.get_by_client_order_id(result.client_order_id)
        assert record is not None
        assert record.state == OrderState.FAILED


# ──────────────────────────────────────────────
# cancel_order: no exchange_order_id
# ──────────────────────────────────────────────


class TestCancelOrderNoExchangeId:

    def test_cancel_without_exchange_id_returns_false(self, tmp_path):
        """Orden sin exchange_order_id -> cancel_order retorna False."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        client = make_client_mock()
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        # Guardar intent en OPEN_RESTING sin exchange_order_id
        intent = make_test_intent(order_type="LIMIT", qty="0.1", price="50000")
        store.save_intent(intent, OrderState.OPEN_RESTING)

        result = executor.cancel_order(intent.client_order_id)
        assert result is False


# ──────────────────────────────────────────────
# cancel_order: CoinbaseAPIError
# ──────────────────────────────────────────────


class TestCancelOrderAPIError:

    def _make_intent_with_exchange_id(self, store: IdempotencyStore) -> str:
        intent = make_test_intent(order_type="LIMIT", qty="0.1", price="50000")
        store.save_intent(intent, OrderState.OPEN_RESTING)
        store.update_state(
            client_order_id=intent.client_order_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="ex-cancel-err-001",
        )
        return intent.client_order_id

    def test_api_error_returns_false(self, tmp_path):
        """CoinbaseAPIError en cancel_orders -> cancel_order retorna False."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        client = MagicMock()
        client.cancel_orders.side_effect = CoinbaseAPIError("network error")
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        client_order_id = self._make_intent_with_exchange_id(store)
        result = executor.cancel_order(client_order_id)
        assert result is False


# ──────────────────────────────────────────────
# get_order_status
# ──────────────────────────────────────────────


class TestGetOrderStatus:

    def test_existing_order_returns_state(self, tmp_path):
        """Intent existente -> retorna su OrderState."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=make_client_mock(),
            idempotency=store,
            quantizer=make_quantizer(),
        )

        intent = make_test_intent(order_type="LIMIT", qty="0.1", price="50000")
        store.save_intent(intent, OrderState.OPEN_RESTING)

        status = executor.get_order_status(intent.client_order_id)
        assert status == OrderState.OPEN_RESTING

    def test_nonexistent_order_returns_none(self, tmp_path):
        """Intent no existente -> retorna None."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=make_client_mock(),
            idempotency=store,
            quantizer=make_quantizer(),
        )
        status = executor.get_order_status("nonexistent-id")
        assert status is None
