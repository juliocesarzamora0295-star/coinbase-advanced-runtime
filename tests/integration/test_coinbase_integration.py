"""
Tests de integración para Coinbase Advanced Trade API.

Requiere:
  - COINBASE_KEY_NAME
  - COINBASE_KEY_SECRET

Nota: Estos tests usan la API real (o sandbox si está disponible).
Ejecutar con precaución.
"""

import os
import time
from decimal import Decimal

import pytest

# Skip all tests unless explicitly enabled with COINBASE_RUN_LIVE_TESTS=1
pytestmark = pytest.mark.skipif(
    (
        not os.getenv("COINBASE_KEY_NAME")
        or not os.getenv("COINBASE_KEY_SECRET")
        or os.getenv("COINBASE_RUN_LIVE_TESTS", "0") != "1"
    ),
    reason="live integration tests disabled (set COINBASE_RUN_LIVE_TESTS=1 to enable)",
)


@pytest.fixture
def client():
    """Crear cliente de Coinbase."""
    from src.core.coinbase_exchange import CoinbaseRESTClient
    from src.core.jwt_auth import JWTAuth, load_credentials_from_env

    credentials = load_credentials_from_env()
    jwt_auth = JWTAuth(credentials)
    return CoinbaseRESTClient(jwt_auth)


class TestAuthentication:
    """Tests de autenticación JWT."""

    def test_list_accounts(self, client):
        """Verificar que podemos listar cuentas (autenticación funciona)."""
        accounts = client.list_accounts()
        assert isinstance(accounts, list)

        # Debería haber al menos una cuenta
        assert len(accounts) > 0

        # Verificar estructura
        account = accounts[0]
        assert "currency" in account
        assert "available_balance" in account

    def test_get_product(self, client):
        """Obtener información de un producto."""
        product = client.get_product("BTC-USD")

        assert product["product_id"] == "BTC-USD"
        assert "base_increment" in product
        assert "quote_increment" in product
        assert "min_market_funds" in product

        # Verificar que los increments son parseables
        base_inc = Decimal(product["base_increment"])
        quote_inc = Decimal(product["quote_increment"])
        assert base_inc > 0
        assert quote_inc > 0

    def test_get_transaction_summary(self, client):
        """Obtener resumen de transacciones (fees)."""
        summary = client.get_transaction_summary()

        assert "fee_tier" in summary
        fee_tier = summary["fee_tier"]

        assert "maker_fee_rate" in fee_tier
        assert "taker_fee_rate" in fee_tier

        # Verificar que los fees son parseables
        maker = Decimal(fee_tier["maker_fee_rate"])
        taker = Decimal(fee_tier["taker_fee_rate"])
        assert maker >= 0
        assert taker >= 0


class TestOrderLifecycle:
    """Tests del ciclo de vida de órdenes."""

    def test_create_limit_order_post_only(self, client):
        """
        Crear orden limit post-only (no debería ejecutarse inmediatamente).

        Esta orden se coloca lejos del precio de mercado para no ejecutarse.
        """
        import uuid

        # Obtener precio actual
        from src.core.quantization import create_quantizer_from_api_response

        product = client.get_product("BTC-USD")
        quantizer = create_quantizer_from_api_response(product)  # noqa: F841

        # Usar precio muy bajo para no ejecutar (post-only)
        extreme_price = Decimal("1000.00")  # Muy por debajo del mercado
        qty = Decimal("0.0001")

        client_order_id = str(uuid.uuid4())

        result = client.create_limit_order_gtc(
            client_order_id=client_order_id,
            product_id="BTC-USD",
            side="BUY",
            base_size=qty,
            limit_price=extreme_price,
            post_only=True,
        )

        assert "order_id" in result
        order_id = result["order_id"]

        # Verificar estado
        assert result["status"] in ("OPEN", "PENDING")

        # Cancelar la orden
        client.cancel_orders([order_id])

        # Verificar que se canceló
        order = client.get_order(order_id)
        assert order["status"] == "CANCELLED"

    def test_idempotency_same_client_order_id(self, client):
        """
        Verificar idempotencia: mismo client_order_id = misma orden.
        """
        import uuid

        client_order_id = str(uuid.uuid4())
        extreme_price = Decimal("1000.00")
        qty = Decimal("0.0001")

        # Primera llamada
        result1 = client.create_limit_order_gtc(
            client_order_id=client_order_id,
            product_id="BTC-USD",
            side="BUY",
            base_size=qty,
            limit_price=extreme_price,
            post_only=True,
        )

        # Segunda llamada con mismo client_order_id
        result2 = client.create_limit_order_gtc(
            client_order_id=client_order_id,
            product_id="BTC-USD",
            side="BUY",
            base_size=qty,
            limit_price=extreme_price,
            post_only=True,
        )

        # Debería retornar la misma orden
        assert result1["order_id"] == result2["order_id"]

        # Limpiar
        client.cancel_orders([result1["order_id"]])

    def test_list_orders(self, client):
        """Listar órdenes."""
        orders = client.list_orders(product_id="BTC-USD", limit=10)
        assert isinstance(orders, list)

    def test_list_fills(self, client):
        """Listar fills (requiere haber tenido trades)."""
        fills = client.list_fills(product_id="BTC-USD", limit=10)
        assert isinstance(fills, list)


class TestCuantization:
    """Tests de cuantización con API real."""

    def test_quantize_matches_api_increments(self, client):
        """
        Verificar que nuestra cuantización coincide con los increments de la API.
        """
        from src.core.quantization import create_quantizer_from_api_response

        product = client.get_product("BTC-USD")
        quantizer = create_quantizer_from_api_response(product)

        # Probar cuantización de cantidad
        raw_qty = Decimal("0.00123456789")
        q_qty = quantizer.quantize_qty(raw_qty)

        # Verificar que es múltiplo del base_increment
        base_inc = Decimal(product["base_increment"])
        remainder = q_qty % base_inc
        assert remainder == 0, f"Quantized qty {q_qty} is not multiple of {base_inc}"

        # Probar cuantización de precio
        raw_price = Decimal("50000.123456")
        q_price_buy = quantizer.quantize_price(raw_price, "BUY")
        q_price_sell = quantizer.quantize_price(raw_price, "SELL")

        quote_inc = Decimal(product["quote_increment"])

        # BUY debe ser floor
        assert q_price_buy <= raw_price
        # SELL debe ser ceil
        assert q_price_sell >= raw_price

        # Ambos deben ser múltiplos
        assert q_price_buy % quote_inc == 0
        assert q_price_sell % quote_inc == 0


class TestErrorHandling:
    """Tests de manejo de errores."""

    def test_invalid_product(self, client):
        """Intentar operar con producto inválido."""
        import uuid

        from src.core.coinbase_exchange import CoinbaseAPIError

        with pytest.raises(CoinbaseAPIError) as exc_info:
            client.create_limit_order_gtc(
                client_order_id=str(uuid.uuid4()),
                product_id="INVALID-PAIR",
                side="BUY",
                base_size=Decimal("0.001"),
                limit_price=Decimal("1000"),
                post_only=True,
            )

        assert exc_info.value.status_code in (400, 404)


class TestRateLimiting:
    """Tests de rate limiting."""

    def test_multiple_requests(self, client):
        """
        Hacer múltiples requests para verificar rate limiting.

        Nota: Este test puede fallar si se excede el rate limit.
        """
        # Hacer 5 requests rápidos
        for _ in range(5):
            accounts = client.list_accounts()
            assert isinstance(accounts, list)
            time.sleep(0.1)  # Pequeña pausa
