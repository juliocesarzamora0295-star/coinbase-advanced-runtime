"""
Cuantización de precios y cantidades para Coinbase Advanced Trade.

CORREGIDO P0:
  - quote_size usa quote_increment (no base_increment)
  - MARKET por quote_size validado
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal


@dataclass
class ProductInfo:
    """Información de un producto para cuantización."""

    product_id: str
    base_increment: Decimal
    quote_increment: Decimal
    min_market_funds: Decimal
    base_currency: str
    quote_currency: str


class Quantizer:
    """
    Cuantizador side-aware para órdenes.

    CORREGIDO P0: quote_size usa quote_increment.
    """

    def __init__(self, product: ProductInfo):
        self.product = product

    def quantize_qty(self, qty: Decimal) -> Decimal:
        """
        Cuantizar cantidad (base_size) a base_increment.

        Siempre floor para no exceder capital disponible.
        """
        increment = self.product.base_increment
        return (qty // increment) * increment

    def quantize_quote_size(self, quote_size: Decimal) -> Decimal:
        """
        CORREGIDO P0: Cuantizar quote_size a quote_increment.

        Para órdenes MARKET por monto en quote (ej: comprar $100 de BTC).
        """
        increment = self.product.quote_increment
        return (quote_size // increment) * increment

    def quantize_price(
        self,
        price: Decimal,
        side: Literal["BUY", "SELL"],
    ) -> Decimal:
        """
        Cuantizar precio limit a quote_increment (side-aware).

        BUY: floor (menos agresivo)
        SELL: ceil (más agresivo)
        """
        increment = self.product.quote_increment

        # Usar to_integral_value con rounding explícito para Decimal
        steps = (price / increment).to_integral_value(
            rounding=ROUND_FLOOR if side == "BUY" else ROUND_CEILING
        )
        return steps * increment

    def quantize_stop_price(
        self,
        price: Decimal,
        position_side: Literal["LONG", "SHORT"],
        stop_type: Literal["STOP_LOSS", "TAKE_PROFIT"],
    ) -> Decimal:
        """Cuantizar stop price (conservador)."""
        increment = self.product.quote_increment

        # LONG + STOP_LOSS: floor (stop más bajo = más lejos del precio)
        # LONG + TAKE_PROFIT: ceil (TP más alto)
        # SHORT + STOP_LOSS: ceil (stop más alto = más lejos)
        # SHORT + TAKE_PROFIT: floor (TP más bajo)
        rounding = {
            ("LONG", "STOP_LOSS"): ROUND_FLOOR,
            ("LONG", "TAKE_PROFIT"): ROUND_CEILING,
            ("SHORT", "STOP_LOSS"): ROUND_CEILING,
            ("SHORT", "TAKE_PROFIT"): ROUND_FLOOR,
        }[(position_side, stop_type)]

        steps = (price / increment).to_integral_value(rounding=rounding)
        return steps * increment

    def validate_min_notional(self, qty: Decimal, price: Decimal) -> bool:
        """Validar que la orden cumple el mínimo notional."""
        notional = qty * price
        return notional >= self.product.min_market_funds

    def validate_quote_size(self, quote_size: Decimal) -> bool:
        """
        CORREGIDO P0: Validar quote_size para MARKET orders.

        El quote_size debe ser >= min_market_funds.
        """
        return quote_size >= self.product.min_market_funds

    def prepare_limit_order(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Preparar orden limit cuantizada."""
        q_qty = self.quantize_qty(qty)
        q_price = self.quantize_price(price, side)

        if not self.validate_min_notional(q_qty, q_price):
            raise ValueError(
                f"Min notional not met: {q_qty} * {q_price} = "
                f"{q_qty * q_price} < {self.product.min_market_funds}"
            )

        return q_qty, q_price

    def prepare_market_order_by_base(
        self,
        base_size: Decimal,
    ) -> Decimal:
        """Preparar orden market por base_size."""
        q_base = self.quantize_qty(base_size)

        # Para market orders, validamos que el valor estimado >= min_market_funds
        # Usamos un precio estimado (esto se valida mejor en el risk engine)
        return q_base

    def prepare_market_order_by_quote(
        self,
        quote_size: Decimal,
    ) -> Decimal:
        """
        CORREGIDO P0: Preparar orden market por quote_size.

        Usa quote_increment, no base_increment.
        """
        q_quote = self.quantize_quote_size(quote_size)

        if not self.validate_quote_size(q_quote):
            raise ValueError(f"Quote size below min: {q_quote} < {self.product.min_market_funds}")

        return q_quote


def create_quantizer_from_api_response(product_data: dict) -> Quantizer:
    """Crear Quantizer desde respuesta de GET /products/{product_id}."""
    return Quantizer(
        ProductInfo(
            product_id=product_data["product_id"],
            base_increment=Decimal(product_data["base_increment"]),
            quote_increment=Decimal(product_data["quote_increment"]),
            min_market_funds=Decimal(product_data.get("min_market_funds", "0")),
            base_currency=product_data["base_currency_id"],
            quote_currency=product_data["quote_currency_id"],
        )
    )
