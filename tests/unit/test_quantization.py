"""Tests para cuantización side-aware."""

from decimal import Decimal

import pytest

from src.core.quantization import ProductInfo, Quantizer


@pytest.fixture
def btc_usd_product():
    return ProductInfo(
        product_id="BTC-USD",
        base_increment=Decimal("0.00000001"),  # 1 satoshi
        quote_increment=Decimal("0.01"),  # 1 cent
        min_market_funds=Decimal("10.0"),  # $10 min
        base_currency="BTC",
        quote_currency="USD",
    )


class TestQuantizer:
    def test_quantize_qty_floor(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # Debe hacer floor al base_increment
        result = q.quantize_qty(Decimal("0.123456789"))
        assert result == Decimal("0.12345678")

    def test_quantize_quote_size(self, btc_usd_product):
        """CORREGIDO P0: quote_size usa quote_increment."""
        q = Quantizer(btc_usd_product)

        result = q.quantize_quote_size(Decimal("100.123"))
        assert result == Decimal("100.12")  # floor a centavos

    def test_quantize_price_buy_floor(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # BUY: floor (menos agresivo)
        result = q.quantize_price(Decimal("50000.123"), "BUY")
        assert result == Decimal("50000.12")

    def test_quantize_price_sell_ceil(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # SELL: ceil (más agresivo)
        result = q.quantize_price(Decimal("50000.123"), "SELL")
        assert result == Decimal("50000.13")

    def test_quantize_stop_long_sl(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # LONG + STOP_LOSS: floor (stop más bajo = más lejos)
        result = q.quantize_stop_price(Decimal("49000.50"), "LONG", "STOP_LOSS")
        assert result == Decimal("49000.50")

    def test_quantize_stop_long_tp(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # LONG + TAKE_PROFIT: ceil (TP más alto)
        # Usar precio no cuantizado para que ceil tenga efecto
        result = q.quantize_stop_price(Decimal("55000.501"), "LONG", "TAKE_PROFIT")
        assert result == Decimal("55000.51")

    def test_validate_min_notional(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        # Válido: 0.001 BTC @ $50,000 = $50 >= $10 min
        assert q.validate_min_notional(Decimal("0.001"), Decimal("50000"))

        # Inválido: 0.0001 BTC @ $50,000 = $5 < $10 min
        assert not q.validate_min_notional(Decimal("0.0001"), Decimal("50000"))

    def test_prepare_limit_order(self, btc_usd_product):
        q = Quantizer(btc_usd_product)

        qty, price = q.prepare_limit_order(
            side="BUY",
            qty=Decimal("0.00123456"),
            price=Decimal("50000.123"),
        )

        assert qty == Decimal("0.00123456")
        assert price == Decimal("50000.12")  # floor para BUY
