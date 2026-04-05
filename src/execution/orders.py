"""
Ejecutor de órdenes con idempotencia y cuantización.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from src.core.coinbase_exchange import CoinbaseAPIError, CoinbaseRESTClient
from src.core.quantization import Quantizer
from src.execution.idempotency import (
    IdempotencyStore,
    OrderState,
    StoredIntent,
)
from src.execution.order_planner import OrderIntent

logger = logging.getLogger("OrderExecutor")


@dataclass
class OrderResult:
    """Resultado de ejecución de orden."""

    success: bool
    intent_id: str
    client_order_id: str
    exchange_order_id: Optional[str]
    state: OrderState
    error_message: Optional[str] = None


class OrderExecutor:
    """
    Ejecutor de órdenes con idempotencia.

    Garantiza que un intent nunca resulta en dos órdenes.
    """

    def __init__(
        self,
        client: CoinbaseRESTClient,
        idempotency: IdempotencyStore,
        quantizer: Quantizer,
        max_retries: int = 3,
    ):
        self.client = client
        self.idempotency = idempotency
        self.quantizer = quantizer
        self.max_retries = max_retries

        # Tracking de órdenes para rate limiting (orders per minute)
        self._order_timestamps_ms: List[int] = []

    def execute(self, intent: OrderIntent) -> OrderResult:
        """
        Ejecutar un OrderIntent canónico del planner.

        Usa intent.client_order_id — no genera identidad propia.
        Despacha a create_limit_order o create_market_order según intent.order_type.
        """
        if intent.order_type == "LIMIT" and intent.price is not None:
            return self.create_limit_order(
                product_id=intent.symbol,
                side=intent.side,
                qty=intent.final_qty,
                price=intent.price,
                post_only=False,
                client_order_id=intent.client_order_id,
            )
        else:
            return self.create_market_order(
                product_id=intent.symbol,
                side=intent.side,
                qty=intent.final_qty,
                client_order_id=intent.client_order_id,
            )

    def create_limit_order(
        self,
        product_id: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        post_only: bool = True,
        *,
        client_order_id: str,
    ) -> OrderResult:
        """
        Crear orden limit con idempotencia.

        Args:
            product_id: Símbolo (ej: "BTC-USD")
            side: "BUY" o "SELL"
            qty: Cantidad en base (ej: BTC)
            price: Precio limit
            post_only: Si es True, la orden no se ejecuta como taker
            client_order_id: ID determinista provisto por OrderPlanner.
                             No se genera internamente.

        Returns:
            OrderResult con el resultado de la operación
        """
        # Cuantizar
        q_qty, q_price = self.quantizer.prepare_limit_order(side, qty, price)

        # intent_id: ID interno de store (separado de client_order_id)
        intent_id = str(uuid.uuid4())

        intent = StoredIntent(
            intent_id=intent_id,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_type="LIMIT",
            qty=q_qty,
            price=q_price,
            stop_price=None,
            post_only=post_only,
            created_ts_ms=int(__import__("time").time() * 1000),
        )

        # Inserción estricta: DuplicateIntentError si client_order_id ya existe.
        # Esto indica un bug en OrderPlanner (mismo signal enviado dos veces).
        # No es recuperable — propagar al caller.
        self.idempotency.save_intent(intent, OrderState.NEW)

        try:
            # Enviar a exchange — usa client_order_id del planner
            response = self.client.create_limit_order_gtc(
                client_order_id=client_order_id,
                product_id=product_id,
                side=side,
                base_size=q_qty,
                limit_price=q_price,
                post_only=post_only,
            )

            exchange_order_id = response.get("order_id")

            # Actualizar estado
            self.idempotency.update_state(
                intent_id=intent_id,
                state=OrderState.OPEN_RESTING,
                exchange_order_id=exchange_order_id,
            )

            logger.info(f"Limit order created: {exchange_order_id}")

            # Registrar timestamp para rate limiting
            self._record_order_timestamp()

            return OrderResult(
                success=True,
                intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_RESTING,
            )

        except CoinbaseAPIError as e:
            # Actualizar estado a FAILED
            self.idempotency.update_state(
                intent_id=intent_id,
                state=OrderState.FAILED,
                error_message=str(e),
            )

            logger.error(f"Failed to create limit order: {e}")

            return OrderResult(
                success=False,
                intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                state=OrderState.FAILED,
                error_message=str(e),
            )

    def create_market_order(
        self,
        product_id: str,
        side: str,
        qty: Optional[Decimal] = None,
        quote_size: Optional[Decimal] = None,
        *,
        client_order_id: str,
    ) -> OrderResult:
        """
        Crear orden market con idempotencia.

        Args:
            product_id: Símbolo (ej: "BTC-USD")
            side: "BUY" o "SELL"
            qty: Cantidad en base (opcional)
            quote_size: Cantidad en quote (opcional)
            client_order_id: ID determinista provisto por OrderPlanner.
                             No se genera internamente.

        Returns:
            OrderResult con el resultado de la operación
        """
        if qty is None and quote_size is None:
            raise ValueError("Debe proporcionar qty o quote_size")

        # Cuantizar
        if qty is not None:
            q_qty = self.quantizer.prepare_market_order_by_base(qty)
            q_quote = None
        else:
            assert quote_size is not None  # guaranteed by ValueError check above
            q_qty = None
            q_quote = self.quantizer.prepare_market_order_by_quote(quote_size)

        # intent_id: ID interno de store (separado de client_order_id)
        intent_id = str(uuid.uuid4())

        intent = StoredIntent(
            intent_id=intent_id,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_type="MARKET",
            qty=q_qty or Decimal("0"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=int(__import__("time").time() * 1000),
        )

        # Inserción estricta: DuplicateIntentError si client_order_id ya existe.
        self.idempotency.save_intent(intent, OrderState.NEW)

        try:
            response = self.client.create_market_order(
                client_order_id=client_order_id,
                product_id=product_id,
                side=side,
                base_size=q_qty,
                quote_size=q_quote,
            )

            exchange_order_id = response.get("order_id")

            # P1: Un ACK de market order no equivale a FILLED confirmado
            # El estado cambia a terminal tras evento user o reconcile REST
            self.idempotency.update_state(
                intent_id=intent_id,
                state=OrderState.OPEN_PENDING,
                exchange_order_id=exchange_order_id,
            )

            logger.info(f"Market order created: {exchange_order_id}")

            # Registrar timestamp para rate limiting
            self._record_order_timestamp()

            return OrderResult(
                success=True,
                intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_PENDING,
            )

        except CoinbaseAPIError as e:
            self.idempotency.update_state(
                intent_id=intent_id,
                state=OrderState.FAILED,
                error_message=str(e),
            )

            logger.error(f"Failed to create market order: {e}")

            return OrderResult(
                success=False,
                intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                state=OrderState.FAILED,
                error_message=str(e),
            )

    def cancel_order(self, intent_id: str) -> bool:
        """
        Cancelar orden por intent_id.

        Args:
            intent_id: ID del intent a cancelar

        Returns:
            True si se canceló exitosamente
        """
        record = self.idempotency.get_by_intent_id(intent_id)

        if not record:
            logger.warning(f"Intent not found: {intent_id}")
            return False

        if not record.is_active:
            logger.warning(f"Order not active: {record.state.name}")
            return False

        if not record.exchange_order_id:
            logger.warning(f"No exchange order ID for intent: {intent_id}")
            return False

        try:
            self.client.cancel_orders([record.exchange_order_id])

            # Transicionar a CANCEL_QUEUED, no directamente a CANCELLED.
            # El estado CANCELLED solo se confirma via user channel o REST reconcile.
            # CANCEL_QUEUED indica que la solicitud fue aceptada por el exchange.
            self.idempotency.update_state(
                intent_id=intent_id,
                state=OrderState.CANCEL_QUEUED,
            )

            logger.info(f"Cancel request accepted: {record.exchange_order_id} -> CANCEL_QUEUED")
            return True

        except CoinbaseAPIError as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

    def get_order_status(self, intent_id: str) -> Optional[OrderState]:
        """Obtener estado actual de una orden."""
        record = self.idempotency.get_by_intent_id(intent_id)
        return record.state if record else None

    def _record_order_timestamp(self) -> None:
        """Registrar timestamp de orden creada para rate limiting."""
        import time

        now_ms = int(time.time() * 1000)
        self._order_timestamps_ms.append(now_ms)
        # Limpiar timestamps antiguos (> 60 segundos)
        cutoff_ms = now_ms - 60000
        self._order_timestamps_ms = [ts for ts in self._order_timestamps_ms if ts > cutoff_ms]

    def get_orders_last_minute(self) -> int:
        """
        Obtener cantidad de órdenes creadas en el último minuto.

        Returns:
            Número de órdenes en los últimos 60 segundos.
        """
        import time

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - 60000
        # Limpiar y contar
        self._order_timestamps_ms = [ts for ts in self._order_timestamps_ms if ts > cutoff_ms]
        return len(self._order_timestamps_ms)
