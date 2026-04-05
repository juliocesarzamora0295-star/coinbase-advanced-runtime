"""
Ejecutor de órdenes con idempotencia y cuantización.

Consume OrderIntent canónico del planner. No genera IDs propios.
Trazabilidad: signal_id → client_order_id → exchange_order_id.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from src.core.coinbase_exchange import CoinbaseAPIError, CoinbaseRESTClient
from src.core.quantization import Quantizer
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent

logger = logging.getLogger("OrderExecutor")


@dataclass
class OrderResult:
    """Resultado de ejecución de orden."""

    success: bool
    client_order_id: str
    exchange_order_id: Optional[str]
    state: OrderState
    error_message: Optional[str] = None

    # Compat alias
    @property
    def intent_id(self) -> str:
        return self.client_order_id


class OrderExecutor:
    """
    Ejecutor de órdenes con idempotencia.

    Recibe OrderIntent canónico del planner.
    No genera UUIDs operativos — usa client_order_id del planner.
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

    def submit_order(self, intent: OrderIntent) -> OrderResult:
        """
        Enviar OrderIntent al exchange con idempotencia.

        El client_order_id viene del planner (determinista).
        Si el intent ya existe en el store, retorna sin reenviar.

        Args:
            intent: OrderIntent canónico del planner.

        Returns:
            OrderResult con el resultado de la operación.
        """
        # Check idempotencia: si ya existe, retornar estado actual
        existing = self.idempotency.get_by_client_order_id(intent.client_order_id)
        if existing:
            logger.info(
                f"Intent already exists: {intent.client_order_id} state={existing.state.name}"
            )
            return OrderResult(
                success=existing.is_active or existing.state == OrderState.FILLED,
                client_order_id=intent.client_order_id,
                exchange_order_id=existing.exchange_order_id,
                state=existing.state,
            )

        # Persistir intent antes de enviar al exchange
        self.idempotency.save_intent(intent, OrderState.NEW)

        # Cuantizar qty
        if intent.order_type == "LIMIT" and intent.price is not None:
            q_qty, q_price = self.quantizer.prepare_limit_order(
                intent.side, intent.final_qty, intent.price
            )
            return self._send_limit(intent, q_qty, q_price)
        else:
            q_qty = self.quantizer.prepare_market_order_by_base(intent.final_qty)
            return self._send_market(intent, q_qty)

    def _send_limit(
        self, intent: OrderIntent, q_qty: Decimal, q_price: Decimal
    ) -> OrderResult:
        """Enviar orden limit al exchange."""
        try:
            response = self.client.create_limit_order_gtc(
                client_order_id=intent.client_order_id,
                product_id=intent.symbol,
                side=intent.side,
                base_size=q_qty,
                limit_price=q_price,
                post_only=intent.post_only,
            )

            exchange_order_id = response.get("order_id")

            self.idempotency.update_state(
                client_order_id=intent.client_order_id,
                state=OrderState.OPEN_RESTING,
                exchange_order_id=exchange_order_id,
            )

            logger.info(f"Limit order created: {exchange_order_id}")
            self._record_order_timestamp()

            return OrderResult(
                success=True,
                client_order_id=intent.client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_RESTING,
            )

        except CoinbaseAPIError as e:
            self.idempotency.update_state(
                client_order_id=intent.client_order_id,
                state=OrderState.FAILED,
                error_message=str(e),
            )
            logger.error(f"Failed to create limit order: {e}")

            return OrderResult(
                success=False,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                state=OrderState.FAILED,
                error_message=str(e),
            )

    def _send_market(self, intent: OrderIntent, q_qty: Decimal) -> OrderResult:
        """Enviar orden market al exchange."""
        try:
            response = self.client.create_market_order(
                client_order_id=intent.client_order_id,
                product_id=intent.symbol,
                side=intent.side,
                base_size=q_qty,
                quote_size=None,
            )

            exchange_order_id = response.get("order_id")

            # ACK de market ≠ FILLED. Estado terminal llega via user channel o reconcile.
            self.idempotency.update_state(
                client_order_id=intent.client_order_id,
                state=OrderState.OPEN_PENDING,
                exchange_order_id=exchange_order_id,
            )

            logger.info(f"Market order created: {exchange_order_id}")
            self._record_order_timestamp()

            return OrderResult(
                success=True,
                client_order_id=intent.client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_PENDING,
            )

        except CoinbaseAPIError as e:
            self.idempotency.update_state(
                client_order_id=intent.client_order_id,
                state=OrderState.FAILED,
                error_message=str(e),
            )
            logger.error(f"Failed to create market order: {e}")

            return OrderResult(
                success=False,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                state=OrderState.FAILED,
                error_message=str(e),
            )

    def cancel_order(self, client_order_id: str) -> bool:
        """
        Cancelar orden por client_order_id.

        Returns:
            True si la solicitud de cancelación fue aceptada.
        """
        record = self.idempotency.get_by_client_order_id(client_order_id)

        if not record:
            logger.warning(f"Intent not found: {client_order_id}")
            return False

        if not record.is_active:
            logger.warning(f"Order not active: {record.state.name}")
            return False

        if not record.exchange_order_id:
            logger.warning(f"No exchange order ID for: {client_order_id}")
            return False

        try:
            self.client.cancel_orders([record.exchange_order_id])

            # CANCEL_QUEUED, no CANCELLED. Terminal llega via user channel o reconcile.
            self.idempotency.update_state(
                client_order_id=client_order_id,
                state=OrderState.CANCEL_QUEUED,
            )

            logger.info(
                f"Cancel request accepted: {record.exchange_order_id} -> CANCEL_QUEUED"
            )
            return True

        except CoinbaseAPIError as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

    def get_order_status(self, client_order_id: str) -> Optional[OrderState]:
        """Obtener estado actual de una orden."""
        record = self.idempotency.get_by_client_order_id(client_order_id)
        return record.state if record else None

    def _record_order_timestamp(self) -> None:
        """Registrar timestamp de orden creada para rate limiting."""
        import time

        now_ms = int(time.time() * 1000)
        self._order_timestamps_ms.append(now_ms)
        cutoff_ms = now_ms - 60000
        self._order_timestamps_ms = [
            ts for ts in self._order_timestamps_ms if ts > cutoff_ms
        ]

    def get_orders_last_minute(self) -> int:
        """Obtener cantidad de órdenes creadas en el último minuto."""
        import time

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - 60000
        self._order_timestamps_ms = [
            ts for ts in self._order_timestamps_ms if ts > cutoff_ms
        ]
        return len(self._order_timestamps_ms)
