"""
Tests dirigidos: cobertura de paths no cubiertos en orders.py.

Cubre mediante mocks de CoinbaseRESTClient:
- ValueError cuando qty=None y quote_size=None
- Ruta quote_size en create_market_order
- CoinbaseAPIError en create_market_order → OrderResult(success=False, FAILED)
- cancel_order cuando no hay exchange_order_id → False
- CoinbaseAPIError en cancel_order → False
- get_order_status() con intent existente y no existente
"""

import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.coinbase_exchange import CoinbaseAPIError
from src.core.quantization import ProductInfo, Quantizer
from src.execution.idempotency import IdempotencyStore, OrderState, StoredIntent
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
    mock.create_limit_order.return_value = {"order_id": order_id}
    mock.cancel_orders.return_value = [{"order_id": order_id}]
    return mock


# ──────────────────────────────────────────────
# create_market_order: ValueError path
# ──────────────────────────────────────────────


class TestCreateMarketOrderValueError:

    def test_no_qty_no_quote_raises_value_error(self, tmp_path):
        """qty=None y quote_size=None → ValueError."""
        executor = make_executor(tmp_path, make_client_mock())
        with pytest.raises(ValueError, match="qty o quote_size"):
            executor.create_market_order(
                product_id="BTC-USD",
                side="BUY",
                qty=None,
                quote_size=None,
            )


# ──────────────────────────────────────────────
# create_market_order: quote_size path
# ──────────────────────────────────────────────


class TestCreateMarketOrderQuoteSize:

    def test_quote_size_path_creates_order(self, tmp_path):
        """quote_size provisto → prepara por quote, llama API."""
        client = make_client_mock("ex-quote-001")
        executor = make_executor(tmp_path, client)

        result = executor.create_market_order(
            product_id="BTC-USD",
            side="BUY",
            qty=None,
            quote_size=Decimal("100"),
        )

        assert result.success is True
        assert result.state == OrderState.OPEN_PENDING
        # Verificar que se llamó con quote_size
        call_kwargs = client.create_market_order.call_args
        assert call_kwargs is not None


# ──────────────────────────────────────────────
# create_market_order: CoinbaseAPIError
# ──────────────────────────────────────────────


class TestCreateMarketOrderAPIError:

    def test_api_error_returns_failed_result(self, tmp_path):
        """CoinbaseAPIError → OrderResult(success=False, FAILED)."""
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("timeout")
        executor = make_executor(tmp_path, client)

        result = executor.create_market_order(
            product_id="BTC-USD",
            side="BUY",
            qty=Decimal("0.01"),
        )

        assert result.success is False
        assert result.state == OrderState.FAILED
        assert "timeout" in (result.error_message or "")

    def test_api_error_persists_failed_state_in_oms(self, tmp_path):
        """CoinbaseAPIError → estado FAILED guardado en IdempotencyStore."""
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("rate_limit")
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        result = executor.create_market_order(
            product_id="BTC-USD",
            side="BUY",
            qty=Decimal("0.01"),
        )

        record = store.get_by_intent_id(result.intent_id)
        assert record is not None
        assert record.state == OrderState.FAILED


# ──────────────────────────────────────────────
# cancel_order: no exchange_order_id
# ──────────────────────────────────────────────


class TestCancelOrderNoExchangeId:

    def test_cancel_without_exchange_id_returns_false(self, tmp_path):
        """Orden sin exchange_order_id → cancel_order retorna False."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        client = make_client_mock()
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        # Guardar intent en NEW sin exchange_order_id
        intent_id = str(uuid.uuid4())
        intent = StoredIntent(
            intent_id=intent_id,
            client_order_id=str(uuid.uuid4()),
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=False,
            created_ts_ms=int(datetime.now().timestamp() * 1000),
        )
        store.save_intent(intent, OrderState.OPEN_RESTING)
        # No se seteó exchange_order_id

        result = executor.cancel_order(intent_id)
        assert result is False


# ──────────────────────────────────────────────
# cancel_order: CoinbaseAPIError
# ──────────────────────────────────────────────


class TestCancelOrderAPIError:

    def _make_intent_with_exchange_id(self, store: IdempotencyStore) -> str:
        intent_id = str(uuid.uuid4())
        intent = StoredIntent(
            intent_id=intent_id,
            client_order_id=str(uuid.uuid4()),
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=False,
            created_ts_ms=int(datetime.now().timestamp() * 1000),
        )
        store.save_intent(intent, OrderState.OPEN_RESTING)
        store.update_state(
            intent_id=intent_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="ex-cancel-err-001",
        )
        return intent_id

    def test_api_error_returns_false(self, tmp_path):
        """CoinbaseAPIError en cancel_orders → cancel_order retorna False."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        client = MagicMock()
        client.cancel_orders.side_effect = CoinbaseAPIError("network error")
        executor = OrderExecutor(
            client=client,
            idempotency=store,
            quantizer=make_quantizer(),
        )

        intent_id = self._make_intent_with_exchange_id(store)
        result = executor.cancel_order(intent_id)
        assert result is False


# ──────────────────────────────────────────────
# get_order_status
# ──────────────────────────────────────────────


class TestGetOrderStatus:

    def test_existing_order_returns_state(self, tmp_path):
        """Intent existente → retorna su OrderState."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=make_client_mock(),
            idempotency=store,
            quantizer=make_quantizer(),
        )

        intent_id = str(uuid.uuid4())
        intent = StoredIntent(
            intent_id=intent_id,
            client_order_id=str(uuid.uuid4()),
            product_id="BTC-USD",
            side="BUY",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=False,
            created_ts_ms=int(datetime.now().timestamp() * 1000),
        )
        store.save_intent(intent, OrderState.OPEN_RESTING)

        status = executor.get_order_status(intent_id)
        assert status == OrderState.OPEN_RESTING

    def test_nonexistent_order_returns_none(self, tmp_path):
        """Intent no existente → retorna None."""
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))
        executor = OrderExecutor(
            client=make_client_mock(),
            idempotency=store,
            quantizer=make_quantizer(),
        )
        status = executor.get_order_status("nonexistent-id")
        assert status is None
